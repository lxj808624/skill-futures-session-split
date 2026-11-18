"""成本口径审计与修正（v3）：发现两处记账错误并给出等成本口径的规则对比。

错误 1：B0（全天持有）turns 记 0 → 基准不带成本，与其他规则（带成本）不可比。
错误 2：B2 / B3 的 turns 写成「空仓日记 1、持仓日记 0」，与 D1 相反 → 成本被记在没交易的日子，
        导致 B2/B3 少扣 (1 - 空仓比例) × 单日往返成本。

修正口径（对全部规则统一）：
- 只要当日建立并平掉一次仓位 = 1 个往返，成本 = 1 × cost_bp(symbol, day_close)
- B0 夜盘开仓、日盘收盘平仓 → 1 往返
- B1 日盘开、日盘收 → 1
- B2 每日都做日盘（跳空达标时连做夜盘）→ 1
- B3 每日都做日盘（前日夜盘上涨时连做夜盘+跳空）→ 1
- D1 仅跳空达标日做日盘 → 达标日 1，否则 0
"""
import csv, json, math, statistics as st, random
from collections import defaultdict

CFG = {}
for line in open("config/varieties.yaml"):
    pass
import re
txt = open("config/varieties.yaml").read()
for m in re.finditer(r"- code: (\w+)\n(?:.*\n)*?    tick: ([\d.]+)\n(?:.*\n)*?    commission_bp: ([\d.]+)", txt):
    CFG[m.group(1)] = (float(m.group(2)), float(m.group(3)))
print("varieties:", len(CFG), CFG)

def cost_bp(sym, price, slip_ticks=1.0):
    tick, comm = CFG[sym]
    return comm + 2.0 * slip_ticks * tick / price * 1e4

def load(path):
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        for k in ("r_night","r_gap","r_day","r_full","day_close"):
            try: r[k] = float(r[k]) if r[k] not in ("","nan") else None
            except ValueError: r[k] = None
    return rows

def rules(rows):
    """返回 {rule: {date: net_bp}} —— 按品种算净收益后再等权平均。"""
    # 按品种排序，供 B3 使用前一交易日夜盘方向
    by_sym = defaultdict(list)
    for r in rows: by_sym[r["symbol"]].append(r)
    out = defaultdict(lambda: defaultdict(list))   # rule -> date -> [net_bp by symbol]
    gross_acc = defaultdict(lambda: defaultdict(list))
    for sym, rs in by_sym.items():
        rs.sort(key=lambda r: r["trading_date"])
        for i, r in enumerate(rs):
            if r["day_close"] is None: continue
            c = cost_bp(sym, r["day_close"])
            night = r["r_night"] or 0.0; day = r["r_day"] or 0.0
            gap = r["r_gap"] or 0.0; full = r["r_full"] or 0.0
            prev_night = rs[i-1]["r_night"] if i > 0 and rs[i-1]["r_night"] is not None else None
            has_night = r["r_night"] is not None
            d = r["trading_date"]
            g = {}
            g["B0"] = (full, 1.0)
            g["B1"] = (day, 1.0)
            g["B2"] = (day + (night if abs(gap) <= 0.01 and has_night else 0.0), 1.0)
            keep = (prev_night is not None and prev_night > 0 and has_night)
            g["B3"] = (day + (night + gap if keep else 0.0), 1.0)
            g["D1"] = (day if abs(gap) <= 0.01 else 0.0, 1.0 if abs(gap) <= 0.01 else 0.0)
            for k, (gr, turns) in g.items():
                out[k][d].append(gr * 1e4 - turns * c)
                gross_acc[k][d].append(gr * 1e4 - turns * c)
    return out

def series(net_by_date):
    dates = sorted(net_by_date)
    return dates, [st.mean(net_by_date[d]) for d in dates]

def stats(series_bp):
    r = [x / 1e4 for x in series_bp]
    n = len(r)
    eq = [1.0]
    for x in r: eq.append(eq[-1] * (1 + x))
    ar = eq[-1] ** (252 / n) - 1
    vol = st.stdev(r) * math.sqrt(252)
    sh = ar / vol if vol else float("nan")
    peak, mdd = 1.0, 0.0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    hit = sum(1 for x in r if x > 0) / n
    mean_bp = st.mean(series_bp)
    t = mean_bp / st.stdev(series_bp) * math.sqrt(n)
    # 块 bootstrap（block=20，2000 次）
    random.seed(20261118)
    B, L = 2000, 20
    boot = []
    for _ in range(B):
        s = []
        while len(s) < n:
            i = random.randrange(n)
            s.extend(series_bp[i:i+L])
        s = s[:n]
        boot.append(st.mean(s))
    boot.sort()
    return dict(n=n, ann_ret=ar, ann_vol=vol, sharpe=sh, max_dd=mdd, hit_rate=hit,
                mean_bp=mean_bp, t_stat=t, ci_low_bp=boot[int(0.025*B)], ci_high_bp=boot[int(0.975*B)])

def run(panel_path, label, periods=None):
    rows = load(panel_path)
    nets = rules(rows)
    res = {}
    for rule, bydate in nets.items():
        dates, s = series(bydate)
        if periods:
            for pname, (lo, hi) in periods.items():
                idx = [i for i, d in enumerate(dates) if lo <= d <= hi]
                if len(idx) > 20:
                    res[(rule, pname)] = stats([s[i] for i in idx])
        res[(rule, "ALL")] = stats(s)
    print(f"\n===== {label} =====")
    print(f"{'rule':4} {'period':4} {'n':>4} {'ann':>9} {'vol':>8} {'sharpe':>7} {'mdd':>8} {'hit':>6} {'meanbp':>8} {'t':>7} {'CI':>16}")
    for (rule, p), v in sorted(res.items()):
        print(f"{rule:4} {p:4} {v['n']:4d} {v['ann_ret']*100:8.2f}% {v['ann_vol']*100:7.2f}% {v['sharpe']:7.2f} "
              f"{v['max_dd']*100:7.2f}% {v['hit_rate']*100:5.1f}% {v['mean_bp']:8.2f} {v['t_stat']:7.2f} "
              f"[{v['ci_low_bp']:+7.2f},{v['ci_high_bp']:+7.2f}]")
    return res

A = run("outputs/panel_daily.csv", "样本A 12品种 83天（修正成本口径）")
B = run("outputs/is_oos_b12/panel_daily.csv", "样本B 12品种 441天（修正成本口径）",
        periods={"IS": ("00000000", "20241231"), "OOS": ("20250101", "99999999")})

out = {"sample_a": {f"{k[0]}|{k[1]}": v for k, v in A.items()},
       "sample_b": {f"{k[0]}|{k[1]}": v for k, v in B.items()}}
with open("outputs/cost_audit_corrected.json", "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("\nwrote outputs/cost_audit_corrected.json")
