"""取数：主力合约映射 + 1 分钟行情，落 parquet 缓存。

凭据只从环境变量或 ~/.pandadata/pandadata.env 读取，不写入任何产物。
在 QuantStudio 平台内运行时，由平台的 PandaData 连接注入凭据。

用法：
  python scripts/fetch_minute.py --variety RB --start 20210104 --end 20261030
  python scripts/fetch_minute.py --all --start 20210104 --end 20261030
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

import pandas as pd

DEFAULT_BASE_URL = "https://pandadata.pandaaiquant.com"
ENV_FILE = Path.home() / ".pandadata" / "pandadata.env"
CACHE_DIR = Path(__file__).resolve().parent.parent / "outputs" / "raw"

DOMINANT_CODE_CANDIDATES = ("dominant_id", "trading_code", "symbol", "code")
DOMINANT_DATE_CANDIDATES = ("date", "trading_date")


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def init_client(base_url: str | None = None):
    load_env()
    try:
        import panda_data
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 panda_data，请先 pip install -r requirements.txt") from exc

    user = os.getenv("DEFAULT_USERNAME", "")
    password = os.getenv("DEFAULT_PASSWORD", "")
    base = base_url or os.getenv("JAVA_SERVICE_BASE_URL", DEFAULT_BASE_URL)
    if not user or not password:
        raise SystemExit("缺少 PandaData 凭据：请在平台内连接 PandaData，或配置 DEFAULT_USERNAME / DEFAULT_PASSWORD")
    try:
        panda_data.init_token(username=user, password=password, base_url=base)
    except Exception as exc:  # SDK 异常类型不稳定，统一转成可读信息
        raise SystemExit(f"PandaData 登录失败：{type(exc).__name__}: {exc}") from exc
    return panda_data


def dominant_segments(api, variety: str, start: str, end: str) -> pd.DataFrame:
    """取主力合约映射，返回 [start_date, contract_id] 分段表。"""

    raw = api.get_future_dominant(symbol=variety, start_date=start, end_date=end)
    if raw is None or len(raw) == 0:
        raise SystemExit(f"{variety}: 主力映射为空")

    code_col = next((c for c in DOMINANT_CODE_CANDIDATES if c in raw.columns), None)
    date_col = next((c for c in DOMINANT_DATE_CANDIDATES if c in raw.columns), None)
    if code_col is None or date_col is None:
        raise SystemExit(f"{variety}: 主力映射字段无法识别，实际字段 {list(raw.columns)}；请补 --contracts 手工分段")

    out = raw[[date_col, code_col]].rename(columns={date_col: "date", code_col: "contract"})
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y%m%d")
    out = out.sort_values("date").drop_duplicates("date", keep="last")

    # 连续相同合约合并成区间；换月处切段
    out["segment"] = (out["contract"] != out["contract"].shift()).cumsum()
    segments = out.groupby("segment").agg(contract=("contract", "first"), start=("date", "min"), end=("date", "max"))
    return segments.reset_index(drop=True)


def fetch_minutes(api, contract: str, start: str, end: str, frequency: str = "1m") -> pd.DataFrame:
    """拉单合约分钟线；返回标准列。"""

    fields = ["symbol", "datetime", "trading_date", "open", "high", "low", "close", "volume"]
    raw = api.get_future_min(symbol=contract, start_date=start, end_date=end, fields=fields, frequency=frequency)
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=fields)
    df = raw.copy()
    for col in fields:
        if col not in df.columns:
            df[col] = None
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["contract"] = contract
    return df[fields + ["contract"]]


def fetch_variety(api, variety: str, start: str, end: str, out_dir: Path = CACHE_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    segments = dominant_segments(api, variety, start, end)

    frames = []
    for row in segments.itertuples():
        code = row.contract
        # 主力映射给的是品种代码，补交易所后缀；已带后缀则保持
        contract = code if "." in code else f"{code}.{_exchange_for(code)}"
        frame = fetch_minutes(api, contract, row.start, row.end)
        if len(frame):
            frames.append(frame)

    if not frames:
        raise SystemExit(f"{variety}: 未取到任何分钟数据")

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["contract", "datetime"]).sort_values("datetime")
    path = out_dir / f"{variety}_minute.parquet"
    df.to_parquet(path, index=False)
    return path


def _exchange_for(contract: str) -> str:
    """按合约代码推交易所后缀；未识别的品种直接报错，不猜测。"""

    mapping = {
        "RB": "SHF", "HC": "SHF", "CU": "SHF", "AL": "SHF", "ZN": "SHF", "NI": "SHF",
        "AU": "SHF", "AG": "SHF", "I": "DCE", "M": "DCE", "Y": "DCE", "P": "DCE",
        "TA": "CZC", "MA": "CZC", "FG": "CZC", "SC": "INE", "LC": "GFE", "SI": "GFE",
        "PS": "GFE", "IF": "CFE", "IH": "CFE", "IC": "CFE", "IM": "CFE", "T": "CFE",
        "TF": "CFE", "TS": "CFE", "TL": "CFE",
    }
    root = "".join(ch for ch in contract if ch.isalpha()).upper()
    if root not in mapping:
        raise SystemExit(f"未登记的品种代码 {contract}，请在 fetch_minute.py 的 _exchange_for 补充，不猜测交易所")
    return mapping[root]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variety", help="品种代码，如 RB")
    parser.add_argument("--all", action="store_true", help="按 config/varieties.yaml 全量取数")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()

    api = init_client()
    varieties = [args.variety] if args.variety else []
    if args.all:
        import yaml

        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "varieties.yaml").read_text())
        varieties = [v["code"] for v in cfg["varieties"]]
    if not varieties:
        raise SystemExit("请指定 --variety 或 --all")

    for variety in varieties:
        path = fetch_variety(api, variety, args.start, args.end)
        print(f"{variety}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
