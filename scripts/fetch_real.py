"""通过 QuantStudio 平台的 PandaData MCP 授权取真实分钟行情。

数据来源：平台 OAuth 授权（~/.dsh-quantstudio/quantskills/panda-mcp/oauth.json），
          不直接用本机 panda_data 账号登录（那条路会被服务端拒绝，错误码 200006）。

用法：
  python scripts/fetch_real.py --varieties RB,CU --start 20250901 --end 20251031
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "outputs" / "raw"
OAUTH_PATH = Path.home() / ".dsh-quantstudio" / "quantskills" / "panda-mcp" / "oauth.json"
MCP_URL = "https://pandadatamcp.pandaaiquant.com/mcp"
PREVIEW_ROWS = 2000


TOKEN_CACHE = Path.home() / ".dsh-logs" / "panda_token.json"
TOKEN_ENDPOINT = "https://pandadatamcp.pandaaiquant.com/token"


class McpClient:
    """令牌来自平台 OAuth；过期时用 refresh_token 自动续期，续期结果写入本地缓存。"""

    def __init__(self) -> None:
        oauth = json.loads(OAUTH_PATH.read_text())
        self.client_id = oauth["client"]["client_id"]
        tokens = oauth.get("tokens") or {}
        if TOKEN_CACHE.exists():
            cached = json.loads(TOKEN_CACHE.read_text())
            tokens = {**tokens, **cached}
        self.token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token")

    def _adopt_cache(self) -> bool:
        """采纳磁盘上更新的令牌（其他进程可能已续期，refresh_token 会轮换）。"""

        if not TOKEN_CACHE.exists():
            return False
        tokens = json.loads(TOKEN_CACHE.read_text())
        if tokens.get("access_token") == self.token:
            return False
        self.token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token", self.refresh_token)
        return True

    def refresh(self) -> None:
        if self._adopt_cache():
            print("  复用其他进程已续期的令牌")
            return
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }).encode()
        req = urllib.request.Request(TOKEN_ENDPOINT, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                tokens = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if self._adopt_cache():
                print("  续期被拒，但磁盘令牌可用，改为复用")
                return
            raise RuntimeError(
                "令牌续期失败（refresh_token 可能已被其他进程消费）："
                f"{exc.code} {exc.read().decode('utf-8', 'ignore')[:120]}"
            ) from exc
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps(tokens, ensure_ascii=False, indent=1))
        self.token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token", self.refresh_token)
        print("  令牌已续期")

    def _post(self, body: dict) -> dict:
        for attempt in range(2):
            req = urllib.request.Request(
                MCP_URL,
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read().decode("utf-8", "ignore")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self.refresh()
                    continue
                raise
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {"raw": raw}

    def init(self) -> None:
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "futures-session-split", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, method: str, params: dict, preview_rows: int | None = None) -> dict:
        arguments = {"method": method, "params": params}
        if preview_rows:
            arguments["preview_rows"] = preview_rows
        res = self._post({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": "call_pandadata", "arguments": arguments}})
        if "result" not in res:
            raise RuntimeError(f"MCP 异常：{json.dumps(res, ensure_ascii=False)[:300]}")
        return res["result"]["structuredContent"]


def dominant_map(client: McpClient, code: str, start: str, end: str) -> dict[str, str]:
    sc = client.call("get_future_dominant", {"underlying_symbol": code, "start_date": start, "end_date": end})
    if not sc.get("ok"):
        raise RuntimeError(f"{code} 主力查询失败：{sc.get('error')}")
    rows = sc["result"] if isinstance(sc["result"], list) else sc["result"].get("rows", [])
    return {str(r["date"]): r["symbol"] for r in rows}


TIME_WINDOWS = (("20:00", "23:59"), ("00:00", "02:30"), ("08:00", "11:30"), ("13:00", "15:30"))


def _one_call(client: McpClient, contract: str, dates: list[str], freq: str,
              window: tuple[str, str] | None) -> tuple[pd.DataFrame, bool]:
    params = {"symbol": contract, "start_date": dates[0], "end_date": dates[-1], "frequency": freq}
    if window:
        params["time_zone"] = list(window)
    sc = client.call("get_future_min", params, preview_rows=PREVIEW_ROWS)
    if not sc.get("ok"):
        raise RuntimeError(f"{contract} 取数失败：{sc.get('error')} {sc.get('detail')}")
    res = sc["result"]
    df = pd.DataFrame(res["rows"], columns=res["columns"]) if res.get("rows") else pd.DataFrame()
    return df, bool(res.get("truncated"))


def minutes(client: McpClient, contract: str, dates: list[str], freq: str = "1m") -> pd.DataFrame:
    """按交易日与时段分片取数：单次返回上限约 110 行，超出即拆分。"""

    if not dates:
        return pd.DataFrame()
    df, truncated = _one_call(client, contract, dates, freq, None)
    if not truncated:
        return df

    if len(dates) > 1:
        # 单次上限约 110 行：逐日请求比二分拆分请求更少
        frames = []
        for d in dates:
            frames.append(minutes(client, contract, [d], freq))
            time.sleep(0.15)
        return pd.concat(frames, ignore_index=True)

    frames = []
    for window in TIME_WINDOWS:
        part, still = _one_call(client, contract, dates, freq, window)
        frames.append(part)
        if still:
            print(f"    警告：{contract} {dates[0]} {window} 仍被截断，行数 {len(part)}")
        time.sleep(0.2)
    return pd.concat(frames, ignore_index=True)


def fetch_variety(client: McpClient, code: str, start: str, end: str, freq: str = "1m") -> pd.DataFrame:
    mapping = dominant_map(client, code, start, end)
    if not mapping:
        raise RuntimeError(f"{code} 区间内没有主力合约记录")
    frames = []
    contracts = sorted(set(mapping.values()))
    for contract in contracts:
        dates = sorted(d for d, c in mapping.items() if c == contract)
        if not dates:
            continue
        print(f"  {code} {contract}: {dates[0]}~{dates[-1]} ({len(dates)} 个交易日)")
        frames.append(minutes(client, contract, dates, freq))
        time.sleep(0.3)
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["symbol", "datetime"]).sort_values("datetime").reset_index(drop=True)
    return df


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--varieties", default="RB,CU,I,M")
    parser.add_argument("--start", default="20250901")
    parser.add_argument("--end", default="20251031")
    parser.add_argument("--frequency", default="1m", help="1m / 5m / 15m / 60m")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client = McpClient()
    client.init()
    print(f"授权令牌长度 {len(client.token)}")

    summary = []
    for code in [c.strip().upper() for c in args.varieties.split(",") if c.strip()]:
        try:
            df = fetch_variety(client, code, args.start, args.end, args.frequency)
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {code}：{exc}")
            continue
        path = RAW_DIR / f"{code}_minute.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            df = (pd.concat([old, df], ignore_index=True)
                  .drop_duplicates(subset=["datetime"])
                  .sort_values("datetime"))
            print(f"  合并既有缓存：{len(old)} + 新增 → {len(df)} 行")
        df.to_parquet(path, index=False)
        summary.append({"code": code, "rows": len(df), "day_first": str(df["trading_date"].min()),
                        "day_last": str(df["trading_date"].max()), "file": path.name})
        print(f"  → 保存 {path.name}：{len(df)} 行，交易日 {df['trading_date'].min()}~{df['trading_date'].max()}")

    if summary:
        path = ROOT / "outputs" / "fetch_summary.csv"
        new = pd.DataFrame(summary)
        if path.exists():
            old = pd.read_csv(path)
            new = pd.concat([old[~old["code"].isin(new["code"])], new], ignore_index=True)
        new.to_csv(path, index=False)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
