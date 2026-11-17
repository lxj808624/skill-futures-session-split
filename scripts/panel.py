"""把 DaySnapshot 列表组装成逐日收益面板（品种 × 交易日）。"""

from __future__ import annotations

import pandas as pd

from sessions import DaySnapshot, segment_returns


PANEL_COLUMNS = [
    "symbol", "trading_date", "prev_close", "night_close", "day_open", "day_close",
    "night_bars", "day_bars", "flags", "r_night", "r_gap", "r_day", "r_full",
]


def snapshots_to_panel(days: list[DaySnapshot]) -> pd.DataFrame:
    rows = []
    for snap in days:
        returns = segment_returns(snap)
        row = {
            "symbol": snap.symbol,
            "trading_date": snap.trading_date,
            "prev_close": snap.prev_close,
            "night_close": snap.night_close,
            "day_open": snap.day_open,
            "day_close": snap.day_close,
            "night_bars": snap.night_bars,
            "day_bars": snap.day_bars,
            "flags": ",".join(snap.flags),
        }
        if returns is None:
            row.update({"r_night": None, "r_gap": None, "r_day": None, "r_full": None})
        else:
            row.update(
                {
                    "r_night": returns["night"],
                    "r_gap": returns["gap"],
                    "r_day": returns["day"],
                    "r_full": returns["full"],
                }
            )
        rows.append(row)

    panel = pd.DataFrame(rows, columns=PANEL_COLUMNS)
    return panel.sort_values(["symbol", "trading_date"]).reset_index(drop=True)


def drop_incomplete(panel: pd.DataFrame, min_bars: int = 30) -> tuple[pd.DataFrame, pd.DataFrame]:
    """剔除覆盖不足的交易日，返回 (可用面板, 剔除清单)。"""

    bad = panel[panel["flags"].str.contains("incomplete") | (panel["r_full"].isna())]
    good = panel.drop(bad.index)
    good = good[good["day_bars"] >= min_bars]
    return good.reset_index(drop=True), bad.reset_index(drop=True)


def data_quality(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, group in panel.groupby("symbol"):
        rows.append(
            {
                "symbol": symbol,
                "days": len(group),
                "usable_days": int(group["r_full"].notna().sum()),
                "incomplete_days": int(group["flags"].str.contains("incomplete").sum()),
                "no_night_days": int(group["flags"].str.contains("no_night_bars").sum()),
                "night_bar_mean": float(group["night_bars"].mean()),
                "day_bar_mean": float(group["day_bars"].mean()),
            }
        )
    return pd.DataFrame(rows)
