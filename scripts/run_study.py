"""编排：从缓存 → 面板 → 规则回放 → 指标 → 差异账本 / 指纹 / 排名翻转。

用法：
  python scripts/run_study.py --start 20210104 --end 20251031 --is-end 20241231
  python scripts/run_study.py --slippage-ticks 1 --tau 0.01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate import CostModel, bootstrap_ci, compare_to_baseline, portfolio_series, prepare_panel, rule_returns, summarize, yearly_table  # noqa: E402
from panel import data_quality, drop_incomplete, snapshots_to_panel  # noqa: E402
from sessions import attribution_check, build_days  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "outputs" / "raw"
OUT_DIR = ROOT / "outputs"
CONFIG_DIR = ROOT / "config"
CONFIG_FILES = ("varieties.yaml", "study.yaml")
RULES = ("B0", "B1", "B2", "B3", "D1")


def load_config(config_dir: Path | None = None) -> tuple[dict, dict]:
    base = config_dir or (ROOT / "config")
    varieties = yaml.safe_load((base / "varieties.yaml").read_text())
    study = yaml.safe_load((base / "study.yaml").read_text())
    return varieties, study


def build_cost_models(varieties: dict, slippage_ticks: float) -> dict[str, CostModel]:
    models = {}
    for item in varieties["varieties"]:
        fee = item.get("commission_bp")
        if not isinstance(fee, (int, float)):
            raise SystemExit(f"{item['code']}: 手续费未配置（当前 {fee!r}），请先补 config/varieties.yaml")
        models[item["code"]] = CostModel(
            multiplier=float(item["multiplier"]),
            tick=float(item["tick"]),
            commission_bp=float(fee),
            slippage_ticks=slippage_ticks,
        )
    return models


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def roll_windows(bars: pd.DataFrame, pad: int = 1) -> set[str]:
    """按合约代码变化识别换月日，并向前后各扩 pad 个交易日。"""

    if "symbol" not in bars.columns:
        return set()
    dates = sorted(bars["trading_date"].astype(str).unique())
    last_contract = (
        bars.assign(trading_date=bars["trading_date"].astype(str))
        .groupby("trading_date")["symbol"]
        .last()
        .reindex(dates)
    )
    changed = [d for d, prev in zip(dates[1:], last_contract.iloc[:-1]) if last_contract[d] != prev]
    idx = {dates.index(d) for d in changed if d in dates}
    covered = set()
    for i in idx:
        for j in range(max(0, i - pad), min(len(dates), i + pad + 1)):
            covered.add(dates[j])
    return covered


def evaluate_rules(panels: list[pd.DataFrame], slippage: float, tau: float) -> dict[str, pd.DataFrame]:
    costs_applied = [prepare_panel(p, build_cost_models_current(slippage)) for p in panels]
    return {rule: portfolio_series([rule_returns(p, rule, tau=tau) for p in costs_applied]) for rule in RULES}


def build_cost_models_current(slippage: float) -> dict[str, CostModel]:
    varieties, _ = load_config(CONFIG_DIR)
    return build_cost_models(varieties, slippage)


def split_period(series: pd.DataFrame, is_end: str) -> dict[str, pd.DataFrame]:
    digits = is_end.replace("-", "")
    return {
        "IS": series[series["trading_date"].astype(str) <= digits],
        "OOS": series[series["trading_date"].astype(str) > digits],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20210104")
    parser.add_argument("--end", default="20251031")
    parser.add_argument("--is-end", default=None, help="样本内结束日，默认取 config/study.yaml")
    parser.add_argument("--slippage-ticks", type=float, default=1.0)
    parser.add_argument("--tau", type=float, default=None, help="跳空门控阈值，默认取 config/study.yaml")
    parser.add_argument("--exclude-roll", action="store_true", help="剔除换月日 ±1 个交易日")
    parser.add_argument("--config-dir", default=None, help="配置目录，默认 config/")
    parser.add_argument("--raw-dir", default=None, help="分钟数据缓存目录，默认 outputs/raw")
    parser.add_argument("--out-dir", default=None, help="产出目录，默认 outputs")
    args = parser.parse_args()

    global RAW_DIR, OUT_DIR, CONFIG_DIR
    if args.config_dir:
        CONFIG_DIR = Path(args.config_dir).expanduser().resolve()
    if args.raw_dir:
        RAW_DIR = Path(args.raw_dir).expanduser().resolve()
    if args.out_dir:
        OUT_DIR = Path(args.out_dir).expanduser().resolve()

    varieties, study = load_config(CONFIG_DIR)
    is_end = args.is_end or str(study["is_end"])
    tau = args.tau if args.tau is not None else float(study["tau"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    panels, attribution, quality_rows = [], [], []
    roll_dates: set[str] = set()
    for item in varieties["varieties"]:
        code = item["code"]
        path = RAW_DIR / f"{code}_minute.parquet"
        if not path.exists():
            print(f"跳过 {code}：缺少缓存 {path.name}（先跑 fetch_minute.py）")
            continue
        bars = pd.read_parquet(path)
        attribution.append({"symbol": code, **attribution_check(bars)})
        roll_dates |= roll_windows(bars)
        days = build_days(bars, code)
        panel = snapshots_to_panel(days)
        panel, _ = drop_incomplete(panel, min_bars=int(study["min_day_bars"]))
        # 日期过滤：此前 --start/--end 只被解析、未生效，导致「样本 A」实际跑成了全样本
        td = panel["trading_date"].astype(str)
        panel = panel[(td >= str(args.start)) & (td <= str(args.end))].reset_index(drop=True)
        panels.append(panel)
        quality_rows.append({"symbol": code, **data_quality(panel).iloc[0].to_dict()})

    if not panels:
        raise SystemExit("没有任何品种可用：请先取数")

    if args.exclude_roll and roll_dates:
        panels = [p[~p["trading_date"].astype(str).isin(roll_dates)].reset_index(drop=True) for p in panels]
        print(f"剔除换月窗口 {len(roll_dates)} 个交易日")

    full = pd.concat(panels, ignore_index=True)
    full.to_csv(OUT_DIR / "panel_daily.csv", index=False)
    pd.DataFrame(attribution).to_csv(OUT_DIR / "attribution_check.csv", index=False)
    pd.DataFrame(quality_rows).to_csv(OUT_DIR / "data_quality.csv", index=False)

    frames = evaluate_rules(panels, args.slippage_ticks, tau)
    for rule, series in frames.items():
        series.to_csv(OUT_DIR / f"series_{rule}.csv", index=False)

    summary_rows = []
    for rule, series in frames.items():
        row = {"rule": rule, **summarize(series)}
        lo, hi = bootstrap_ci(series, block=int(study["bootstrap_block"]), n_iter=int(study["bootstrap_iter"]))
        row["ci_low_bp"], row["ci_high_bp"] = lo, hi
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False)

    comparison = compare_to_baseline(frames, baseline="B0")
    comparison.to_csv(OUT_DIR / "baseline_compare.csv", index=False)

    split_rows = []
    for rule, series in frames.items():
        for label, part in split_period(series, is_end).items():
            if len(part) < 2:
                continue
            split_rows.append({"rule": rule, "period": label, **summarize(part)})
    pd.DataFrame(split_rows).to_csv(OUT_DIR / "is_oos.csv", index=False)

    for rule, series in frames.items():
        yearly_table(series).to_csv(OUT_DIR / f"yearly_{rule}.csv", index=False)

    # 排名翻转检查：成本档 × 换月含/剔除 × IS/OOS
    flip_rows = []
    levels = study.get("slippage_levels", [0, 1, 2])
    for roll_label, roll_days_active in (("with_roll", False), ("ex_roll", True)):
        if roll_days_active and not roll_dates:
            continue
        subset = panels
        if roll_days_active:
            subset = [p[~p["trading_date"].astype(str).isin(roll_dates)].reset_index(drop=True) for p in panels]
        for slippage in levels:
            costs = build_cost_models(varieties, float(slippage))
            local_frames = {rule: portfolio_series([rule_returns(prepare_panel(p, costs), rule, tau=tau) for p in subset]) for rule in RULES}
            for rule, series in local_frames.items():
                for label, part in split_period(series, is_end).items():
                    if len(part) >= 2:
                        part_stats = summarize(part)
                        flip_rows.append({
                            "roll_mode": roll_label, "cost_ticks": slippage, "period": label,
                            "rule": rule, "sharpe": part_stats.get("sharpe"), "ann_ret": part_stats.get("ann_ret"),
                        })
    flip = pd.DataFrame(flip_rows)
    if not flip.empty:
        flip["rank"] = flip.groupby(["roll_mode", "cost_ticks", "period"])["sharpe"].rank(ascending=False, method="min")
    flip.to_csv(OUT_DIR / "rank_flip.csv", index=False)

    config_hashes = {name: sha256(CONFIG_DIR / name) for name in CONFIG_FILES}
    data_hashes = {p.name: sha256(p) for p in sorted(RAW_DIR.glob("*.parquet"))}
    fingerprint = {
        "config": config_hashes,
        "data": data_hashes,
        "data_rows": {p.name: int(pd.read_parquet(p).shape[0]) for p in sorted(RAW_DIR.glob("*.parquet"))},
        "args": vars(args),
        "tau": tau,
        "is_end": is_end,
    }
    fingerprint["combined"] = hashlib.sha256(
        json.dumps({**config_hashes, **data_hashes}, sort_keys=True).encode()
    ).hexdigest()
    (OUT_DIR / "fingerprint.json").write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2), encoding="utf-8")

    def rel(path: Path) -> str:
        """优先输出相对 ROOT 的路径，保证索引在别的机器上也能对得上（配 root 说明）。"""

        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    index = {
        "root": str(ROOT),
        "path_convention": "相对路径以 root 为基准；不在 root 下的给出绝对路径",
        "inputs": {
            "config": [rel(CONFIG_DIR / n) for n in CONFIG_FILES],
            "data_cache": sorted(rel(p) for p in RAW_DIR.glob("*.parquet")),
            "args": vars(args),
        },
        "outputs": sorted(rel(p) for p in OUT_DIR.glob("*.csv")) + [rel(OUT_DIR / "fingerprint.json")],
        "rules": list(RULES),
        "roll_days_excluded": len(roll_dates) if args.exclude_roll else 0,
    }
    (OUT_DIR / "inputs_outputs_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    print(summary.to_string(index=False))
    print(f"\n产出目录：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
