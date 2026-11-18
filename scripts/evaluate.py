"""规则回放与评估：把三段收益组装成可执行持仓规则，扣成本后比较。

规则（详见 docs/研究方案.md 第 4 节）：
  B0 全天持有 / B1 仅日盘 / B2 跳空门控隔夜 / B3 夜盘方向门控隔夜 / D1 跳空过滤日盘

口径：百分比收益，等权组合，逐日再平衡；成本按“每手名义价值”折算成 bp。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 244


@dataclass(frozen=True)
class CostModel:
    """常规成本口径：手续费按名义价值双边 bp，滑点按 tick/边。"""

    multiplier: float
    tick: float
    commission_bp: float
    slippage_ticks: float = 1.0

    def round_turn_bp(self, price: float) -> float:
        """一开一平的总成本，单位 bp。"""

        if price <= 0:
            return float("nan")
        slip_bp = 2.0 * self.slippage_ticks * self.tick / price * 1e4
        return self.commission_bp + slip_bp


def prepare_panel(days: pd.DataFrame, costs: dict[str, CostModel]) -> pd.DataFrame:
    """输入逐日收益面板，补上成本列与规则所需上下文。"""

    out = days.copy()
    out["cost_bp"] = [
        costs[row.symbol].round_turn_bp(row.day_close)
        if row.symbol in costs and pd.notna(row.day_close)
        else np.nan
        for row in out.itertuples()
    ]
    return out


def rule_returns(panel: pd.DataFrame, rule: str, tau: float = 0.01) -> pd.DataFrame:
    """按规则生成逐日收益（已扣成本）与往返次数。

    无前视约束：任何门控只能用决策时点已可观测的信息。
      - 隔夜是否持有，决策点在「前一日日盘收盘」或「夜盘开盘」；
      - 夜盘段方向本身在决策时不可知，因此 B3 用前一交易日夜盘方向（滞后 1 期）。
    """

    df = panel.copy().sort_values(["symbol", "trading_date"]).reset_index(drop=True)
    has_night = df["r_night"].notna()
    gap = df["r_gap"].abs() <= tau          # 夜盘开盘即可观测，可在夜盘开盘后决策
    night = df["r_night"].astype("float64").fillna(0.0)
    day = df["r_day"].fillna(0.0)
    prev_night_up = night.shift(1) > 0       # 滞后一期，无前视

    if rule == "B0":
        # 修正（v3）：全天持有同样是「夜盘开仓 + 日盘收盘平仓」= 1 个往返，
        # 旧版把 turns 记为 0，使基准不带成本、与其他规则不可比。
        gross = df["r_full"]
        turns = 1.0
    elif rule == "B1":
        gross = day
        turns = 1.0
    elif rule == "B2":
        # 跳空门控：夜盘开盘跳空收敛才持有夜盘段；跳空本身不计入收益
        gross = day + np.where(gap & has_night, night, 0.0)
        # 修正（v3）：B2 每天都做日盘（达标时连做夜盘），每天 1 个往返；
        # 旧版写反成「持仓 0、空仓 1」，把成本记在空仓日。
        turns = 1.0
    elif rule == "B3":
        # 夜盘动量门控：前一交易日夜盘上涨才顺势过夜（含跳空段）
        keep = prev_night_up & has_night
        gross = day + np.where(keep, night + df["r_gap"].fillna(0.0), 0.0)
        # 修正（v3）：B3 每天都做日盘（门控通过时连做夜盘+跳空），每天 1 个往返；
        # 旧版同样写反。保守起见不对「跨日连续持仓」折减换手。
        turns = 1.0
    elif rule == "D1":
        # 跳空过滤日盘：跳空收敛才做当日日盘
        gross = np.where(gap, day, 0.0)
        turns = np.where(gap, 1.0, 0.0)
    else:
        raise ValueError(f"未知规则: {rule}")

    df["gross"] = pd.Series(gross, index=df.index).fillna(0.0)
    df["turns"] = pd.Series(turns, index=df.index).fillna(1.0)
    df["net_bp"] = df["gross"] * 1e4 - df["turns"] * df["cost_bp"]
    df["net"] = df["net_bp"] / 1e4
    df["rule"] = rule
    return df


def portfolio_series(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """等权组合：按交易日对品种取均值。"""

    stacked = pd.concat(frames, ignore_index=True)
    agg = stacked.groupby(["trading_date", "rule"], as_index=False).agg(
        net=("net", "mean"),
        net_bp=("net_bp", "mean"),
        turns=("turns", "mean"),
        cost_bp=("cost_bp", "mean"),
        symbols=("symbol", "nunique"),
    )
    return agg.sort_values("trading_date")


def summarize(series: pd.DataFrame) -> dict[str, float]:
    """组合层指标。"""

    r = series["net"].astype(float).to_numpy()
    r = r[~np.isnan(r)]
    if len(r) < 2:
        return {"n": float(len(r))}

    ann_ret = float(np.mean(r) * TRADING_DAYS)
    ann_vol = float(np.std(r, ddof=1) * np.sqrt(TRADING_DAYS))
    sharpe = ann_ret / ann_vol if ann_vol > 0 else float("nan")
    curve = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(curve)
    max_dd = float(np.min(curve / peak - 1.0))
    t_stat = float(np.mean(r) / (np.std(r, ddof=1) / np.sqrt(len(r)))) if np.std(r, ddof=1) > 0 else float("nan")

    return {
        "n": float(len(r)),
        "ann_ret": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "hit_rate": float(np.mean(r > 0)),
        "mean_bp": float(np.mean(series["net_bp"].astype(float))),
        "cost_bp": float(np.mean(series["turns"].astype(float) * series["cost_bp"].astype(float)))
        if "cost_bp" in series
        else float("nan"),
        "t_stat": t_stat,
    }


def bootstrap_ci(series: pd.DataFrame, block: int = 20, n_iter: int = 2000, seed: int = 7) -> tuple[float, float]:
    """block bootstrap 的日均收益 95% 置信区间（bp）。"""

    r = series["net_bp"].astype(float).to_numpy()
    r = r[~np.isnan(r)]
    if len(r) < block * 3:
        return (float("nan"), float("nan"))

    rng = np.random.default_rng(seed)
    means = []
    n_blocks = len(r) // block
    for _ in range(n_iter):
        idx = rng.integers(0, n_blocks, n_blocks)
        sample = np.concatenate([r[i * block:(i + 1) * block] for i in idx])
        means.append(sample.mean())
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def yearly_table(series: pd.DataFrame) -> pd.DataFrame:
    df = series.copy()
    df["year"] = df["trading_date"].astype(str).str[:4]
    rows = []
    for year, group in df.groupby("year"):
        s = summarize(group)
        s["year"] = year
        rows.append(s)
    return pd.DataFrame(rows)


def compare_to_baseline(frames: dict[str, pd.DataFrame], baseline: str = "B0") -> pd.DataFrame:
    """各规则相对冻结基线的增量（年化、Sharpe）。"""

    rows = []
    base = summarize(frames[baseline])
    for rule, series in frames.items():
        if rule == baseline:
            continue
        cur = summarize(series)
        rows.append(
            {
                "rule": rule,
                "delta_ann_ret": cur["ann_ret"] - base["ann_ret"],
                "delta_sharpe": cur["sharpe"] - base["sharpe"],
                "delta_mean_bp": cur["mean_bp"] - base["mean_bp"],
                "max_dd": cur["max_dd"],
                "t_stat": cur["t_stat"],
            }
        )
    return pd.DataFrame(rows)
