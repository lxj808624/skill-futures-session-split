"""时段拆解：把一根分钟 bar 归入夜盘 / 日盘，并按交易日聚合出三段价格锚点。

约定（与 references/session-rules.md 一致）：
- bar 时间按“收盘时刻”解释，15:00 那根覆盖 14:59–15:00；
- 08:00–20:00 归日盘，其余归夜盘（21:00–02:30 的品种差异由数据自身决定，不用猜测）；
- 交易日归属一律用行情自带 trading_date，不自行推断。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

DAY_START_HOUR = 8
DAY_END_HOUR = 20

REQUIRED_COLUMNS = ("datetime", "open", "close", "trading_date")


@dataclass
class DaySnapshot:
    """单个品种、单个交易日的三段锚点。"""

    symbol: str
    trading_date: str
    prev_close: float | None = None
    night_close: float | None = None
    day_open: float | None = None
    day_close: float | None = None
    night_bars: int = 0
    day_bars: int = 0
    first_bar: str | None = None
    last_bar: str | None = None
    flags: list[str] = field(default_factory=list)

    def has_full_day(self) -> bool:
        return self.day_open is not None and self.day_close is not None

    def has_night(self) -> bool:
        return self.night_close is not None


def classify_bar_minute(ts: pd.Timestamp) -> str:
    hour = ts.hour
    if DAY_START_HOUR <= hour < DAY_END_HOUR:
        return "day"
    return "night"


def _to_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series)


def check_columns(bars: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"分钟行情缺少字段: {missing}")


def split_by_session(bars: pd.DataFrame) -> pd.DataFrame:
    """返回带 session 列的副本，按时间升序。"""

    check_columns(bars)
    out = bars.copy()
    out["datetime"] = _to_ts(out["datetime"])
    out["session"] = out["datetime"].map(classify_bar_minute)
    out = out.sort_values("datetime").reset_index(drop=True)
    return out


def expected_minute_grid(bars: pd.DataFrame, min_share: float = 0.9) -> set[str]:
    """由全样本中高频出现的分钟点推导期望网格，不写死交易所规则。

    口径：某分钟点在“多少个交易日出现过”的占比，而不是该分钟在总 bar 中的占比。
    """

    stamps = bars.assign(hm=bars["datetime"].dt.strftime("%H:%M"))
    n_days = stamps["trading_date"].nunique()
    if n_days == 0:
        return set()
    presence = stamps.groupby("hm")["trading_date"].nunique() / n_days
    return set(presence[presence >= min_share].index)


def build_days(bars: pd.DataFrame, symbol: str) -> list[DaySnapshot]:
    """把一个品种的分钟 bar 聚合成按交易日排列的 DaySnapshot 列表。"""

    data = split_by_session(bars)
    if data.empty:
        return []

    grid = expected_minute_grid(data)
    days: list[DaySnapshot] = []
    prev_close: float | None = None

    for trading_date, group in data.groupby("trading_date", sort=True):
        snap = DaySnapshot(symbol=symbol, trading_date=str(trading_date))
        night = group[group["session"] == "night"]
        day = group[group["session"] == "day"]

        snap.night_bars = int(len(night))
        snap.day_bars = int(len(day))
        snap.first_bar = group["datetime"].iloc[0].isoformat()
        snap.last_bar = group["datetime"].iloc[-1].isoformat()

        if not night.empty:
            snap.night_close = float(night["close"].iloc[-1])
        if not day.empty:
            snap.day_open = float(day["open"].iloc[0])
            snap.day_close = float(day["close"].iloc[-1])

        present = set(group["datetime"].dt.strftime("%H:%M"))
        # 覆盖率只按「日盘网格」判定：节后首日、或品种本身没有夜盘时，夜盘缺失是正常的，
        # 不能因此把整个交易日丢掉（2026-11-17 R1 复算发现：整日剔除会让样本外收益虚高 5.6pp）。
        day_grid = {m for m in grid if classify_bar_minute(
            pd.Timestamp(f"2026-01-01 {m}")) == "day"}
        coverage = (len(present & day_grid) / len(day_grid)) if day_grid else 0.0
        if coverage < 0.8:
            snap.flags.append("incomplete")
        if len(group) == 0:
            snap.flags.append("halted")
        if len(night) == 0 and snap.night_bars == 0:
            snap.flags.append("no_night_bars")

        snap.prev_close = prev_close
        prev_close = snap.day_close if snap.day_close is not None else prev_close
        days.append(snap)

    return days


def segment_returns(snap: DaySnapshot) -> dict[str, float] | None:
    """返回夜盘段 / 闭市跳空 / 日盘段 / 全天 的简单收益。

    无夜盘品种：夜盘段为 None，跳空段以昨收为起点。
    """

    if snap.prev_close is None or not snap.has_full_day():
        return None

    night = None
    if snap.has_night():
        night = snap.night_close / snap.prev_close - 1.0
        gap_start = snap.night_close
    else:
        gap_start = snap.prev_close

    gap = snap.day_open / gap_start - 1.0
    day = snap.day_close / snap.day_open - 1.0
    full = snap.day_close / snap.prev_close - 1.0

    return {"night": night, "gap": gap, "day": day, "full": full}


def attribution_check(bars: pd.DataFrame) -> dict[str, int]:
    """交易日归属校验：夜盘 bar 的自然日与 trading_date 不一致的条数等。"""

    data = split_by_session(bars)
    natural = data["datetime"].dt.strftime("%Y%m%d")
    trading = data["trading_date"].astype(str)
    mismatch = data[(data["session"] == "night") & (natural != trading)]
    friday_night = data[(data["session"] == "night") & (data["datetime"].dt.weekday == 4)]
    return {
        "night_bars": int((data["session"] == "night").sum()),
        "night_date_mismatch": int(len(mismatch)),
        "friday_night_bars": int(len(friday_night)),
        "bars": int(len(data)),
        "trading_days": int(data["trading_date"].nunique()),
    }


def trading_day_gaps(days: list[DaySnapshot]) -> list[tuple[str, str]]:
    """相邻交易日之间相隔超过 3 个自然日（周末/节假日），用于标记跳空窗口。"""

    parsed = [dt.datetime.strptime(d.trading_date, "%Y%m%d") for d in days]
    out = []
    for prev, cur in zip(parsed, parsed[1:]):
        if (cur - prev).days > 3:
            out.append((prev.strftime("%Y%m%d"), cur.strftime("%Y%m%d")))
    return out
