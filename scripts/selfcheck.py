"""无数据自检：用合成分钟行情验证时段拆分、交易日归属、三段恒等式与规则回放。

运行：python scripts/selfcheck.py
无需 PandaData 授权，也不产生任何“真实结果”。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate import CostModel, prepare_panel, rule_returns, summarize  # noqa: E402
from panel import snapshots_to_panel  # noqa: E402
from sessions import attribution_check, build_days  # noqa: E402


def _bars_for_day(trading_date: dt.date, prev_calendar_day: dt.date, base: float, with_night: bool) -> list[dict]:
    rows = []
    price = base

    def emit(ts: dt.datetime, step: float, trading: dt.date):
        nonlocal price
        o = price
        price = price + step
        rows.append(
            {
                "datetime": ts,
                "open": round(o, 4),
                "close": round(price, 4),
                "trading_date": trading.strftime("%Y%m%d"),
            }
        )

    if with_night:
        for i in range(120):  # 21:00–22:59
            emit(dt.datetime.combine(prev_calendar_day, dt.time(21, 0)) + dt.timedelta(minutes=i), 0.002, trading_date)

    # 日盘 09:01–10:00 + 13:31–14:00（合成用短时段，只为验证拆分，不代表真实时段）
    start = dt.datetime.combine(trading_date, dt.time(9, 1))
    for i in range(60):
        emit(start + dt.timedelta(minutes=i), 0.001, trading_date)
    start2 = dt.datetime.combine(trading_date, dt.time(13, 31))
    for i in range(30):
        emit(start2 + dt.timedelta(minutes=i), 0.001, trading_date)
    return rows


def build_fixture(with_night: bool) -> pd.DataFrame:
    # 2026-11-13 周五、11-16 周一（跨周末）
    days = [
        (dt.date(2026, 11, 9), dt.date(2026, 11, 6)),
        (dt.date(2026, 11, 10), dt.date(2026, 11, 9)),
        (dt.date(2026, 11, 11), dt.date(2026, 11, 10)),
        (dt.date(2026, 11, 12), dt.date(2026, 11, 11)),
        (dt.date(2026, 11, 16), dt.date(2026, 11, 13)),
    ]
    rows = []
    for trading, prev in days:
        rows.extend(_bars_for_day(trading, prev, base=3000.0, with_night=with_night))
    df = pd.DataFrame(rows)
    df["symbol"] = "SYN.SHF" if with_night else "SYN2.CFE"
    return df


def main() -> int:
    failures = []

    bars = build_fixture(with_night=True)
    days = build_days(bars, "SYN.SHF")
    assert len(days) == 5, f"交易日数量应为 5，实际 {len(days)}"

    check = attribution_check(bars)
    if check["night_date_mismatch"] != check["night_bars"]:
        failures.append("夜盘 bar 的自然日应全部与 trading_date 不同")
    if check["friday_night_bars"] == 0:
        failures.append("应识别到周五夜盘 bar")

    panel = snapshots_to_panel(days)
    composed = (1 + panel["r_night"]) * (1 + panel["r_gap"]) * (1 + panel["r_day"]) - 1
    max_diff = float((composed - panel["r_full"]).abs().max())
    if max_diff > 1e-9:
        failures.append(f"三段恒等式不成立，最大偏差 {max_diff}")

    flat = build_fixture(with_night=False)
    flat_days = build_days(flat, "SYN2.CFE")
    flat_panel = snapshots_to_panel(flat_days)
    if flat_panel["r_night"].notna().any():
        failures.append("无夜盘品种不应有夜盘段收益")
    flat_composed = (1 + flat_panel["r_gap"]) * (1 + flat_panel["r_day"]) - 1
    if float((flat_composed - flat_panel["r_full"]).abs().max()) > 1e-9:
        failures.append("无夜盘品种的跳空/日盘两段恒等式不成立")

    costs = {"SYN.SHF": CostModel(multiplier=10, tick=1, commission_bp=2.0, slippage_ticks=1)}
    prepared = prepare_panel(panel, costs)
    cost_bp = prepared["cost_bp"].iloc[0]
    if not (2.0 <= cost_bp < 100):
        failures.append(f"成本折算异常：{cost_bp}")

    for rule in ("B0", "B1", "B2", "B3", "D1"):
        result = rule_returns(prepared, rule, tau=0.01)
        if result["net"].isna().all():
            failures.append(f"规则 {rule} 未产出收益")
        if rule != "B0" and (result["turns"] < 0).any():
            failures.append(f"规则 {rule} 换手为负")

    summary = summarize(rule_returns(prepared, "B1"))
    if summary["n"] < 2:
        failures.append("B1 汇总样本不足")

    print("自检结果：")
    # 夜盘 bar 的自然日与归属交易日不同是设计使然（夜盘属于下一交易日），
    # 这里的计数是「符合预期的数量」，不是缺陷数。历史上这行字面被误读过，故改写。
    print(f"  夜盘 bar: {check['night_bars']}，其中归到下一交易日（自然日≠交易日，符合预期）: "
          f"{check['night_date_mismatch']}，周五夜盘: {check['friday_night_bars']}")
    print(f"  三段恒等偏差: {max_diff:.3e}")
    print(f"  单次往返成本: {cost_bp:.2f} bp")
    print(f"  B1 汇总: {summary}")

    if failures:
        print("\n未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\n全部通过：时段拆分、交易日归属、两段/三段恒等式、成本折算、规则回放可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
