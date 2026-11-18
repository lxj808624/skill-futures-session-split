"""v3 全量重算：等成本口径下的主结果、τ/滑点敏感性、4 品种对照及排名表。

纯标准库；输入为已落盘的两块逐日面板，无需重读 parquet。
输出（覆盖 v2 同名产物，v2 原件先归档到 outputs/archive/v2/）：
  outputs/summary.csv            样本 A（83 天）
  outputs/is_oos.csv             样本 B（441 天，ALL/IS/OOS）
  outputs/series_B0..D1.csv      样本 A 逐日净收益
  outputs/rank_flip.csv          with_roll × 滑点 0/1/2 × OOS
  outputs/sensitivity_tau.csv    样本 A，τ=0.5/1/2%
  outputs/sensitivity_slip.csv   样本 A，滑点 0/1/2 tick
  outputs/is_oos_b4_v3.csv       4 品种子样本（LC/AG/AU/IF）OOS 对照
"""
import csv, json, math, statistics as st, os, shutil, re
from collections import defaultdict

# ---------- 配置 ----------
txt = open("config/varieties.yaml").read()
CFG = {}
for m in re.finditer(r"- code: (\w+)\n(?:.*\n)*?    tick: ([\d.]+)\n(?:.*\n)*?    commission_bp: ([\d.]+)", txt):
    CFG[m.group(1)] = {"tick": float(m.group(2)), "comm": float(m.group(3))}

RULES = ("B0", "B1", "B2", "B3", "D1")

def cost(sym, price, slip):
    c = CFG[sym]
    return c["comm"] + 2.0 * slip * c["tick"] / price * 1e4

def load(path):
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        for k in ("r_night", "r_gap", "r_day", "r_full", "day_close"):
            try:
                r[k] = float(r[k]) if r[k] not in ("", "nan", "None") else None
            except ValueError:
                r[k] = None
    return rows

def rule_net(rows, tau=0.01, slip=1.0):
    """返回 {rule: {date: [per-symbol net bp]}}"""
    by_sym = defaultdict(list)
    for r in rows: by_sym[r["symbol"]].append(r)
    out = defaultdict(lambda: defaultdict(list))
    for sym, rs in by_sym.items():
        rs.sort(key=lambda r: r["trading_date"])
        prev_night = None
        for r in rs:
            if r["day_close"] is None:
                prev_night = r["r_night"]; continue
            c = cost(sym, r["day_close"], slip)
            night = r["r_night"] or 0.0
            day = r["r_day"] or 0.0
            gap = r["r_gap"] or 0.0
            full = r["r_full"] if r["r_full"] is not None else night + gap + day
            has_night = r["r_night"] is not None
            d = r["trading_date"]
            ok = abs(gap) <= tau
            vals = {
                "B0": (full, 1.0),
                "B1": (day, 1.0),
                "B2": (day + (night if ok and has_night else 0.0), 1.0),
                "B3": (day + (night + gap if (prev_night is not None and prev_night > 0 and has_night) else 0.0), 1.0),
                "D1": (day if ok else 0.0, 1.0 if ok else 0.0),
            }
            for k, (gr, turns) in vals.items():
                out[k][d].append(gr * 1e4 - turns * c)
            prev_night = r["r_night"]
    return out

def port(by_date):
    dates = sorted(by_date)
    return dates, [st.mean(by_date[d]) for d in dates]

def stats(bp):
    r = [x / 1e4 for x in bp]; n = len(r)
    eq = 1.0
    for x in r: eq *= (1 + x)
    ar = eq ** (252 / n) - 1
    vol = st.stdev(r) * math.sqrt(252)
    mdd, peak, cur = 0.0, 1.0, 1.0
    for x in r:
        cur *= 1 + x; peak = max(peak, cur); mdd = min(mdd, cur / peak - 1)
    return dict(n=n, ann_ret=ar, ann_vol=vol, sharpe=ar / vol if vol else float("nan"),
                max_dd=mdd, hit_rate=sum(1 for x in r if x > 0) / n,
                mean_bp=st.mean(bp), t_stat=st.mean(bp) / st.stdev(bp) * math.sqrt(n))

def write_csv(path, head, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(head); [w.writerow(r) for r in rows]

# ---------- 归档 v2 ----------
arch = "outputs/archive/v2"
os.makedirs(arch, exist_ok=True)
for f in ["summary.csv", "is_oos.csv", "rank_flip.csv", "baseline_compare.csv"] + [f"series_{r}.csv" for r in RULES]:
    p = os.path.join("outputs", f)
    if os.path.exists(p) and not os.path.exists(os.path.join(arch, f)):
        shutil.move(p, os.path.join(arch, f))
print("archived v2 outputs ->", arch)

# ---------- 样本 A ----------
A = load("outputs/is_oos_b12/../panel_daily.csv") if False else load("outputs/panel_daily.csv")
nets = rule_net(A, tau=0.01, slip=1.0)
sum_rows = []
for rule in RULES:
    dates, bp = port(nets[rule]); s = stats(bp)
    sum_rows.append([rule, s["n"], round(s["ann_ret"], 6), round(s["ann_vol"], 6), round(s["sharpe"], 4),
                     round(s["max_dd"], 6), round(s["hit_rate"], 4), round(s["mean_bp"], 4), round(s["t_stat"], 4)])
write_csv("outputs/summary.csv", ["rule", "n", "ann_ret", "ann_vol", "sharpe", "max_dd", "hit_rate", "mean_bp", "t_stat"], sum_rows)
print("summary.csv", sum_rows)

for rule in RULES:
    dates, bp = port(nets[rule])
    write_csv(f"outputs/series_{rule}.csv", ["trading_date", "net_bp"], [[d, round(x, 6)] for d, x in zip(dates, bp)])
print("series written")

# ---------- 样本 B ----------
B = load("outputs/is_oos_b12/panel_daily.csv")
nb = rule_net(B, tau=0.01, slip=1.0)
io_rows = []
for rule in RULES:
    dates, bp = port(nb[rule])
    for label, sel in (("IS", [i for i, d in enumerate(dates) if d < "20250101"]),
                       ("OOS", [i for i, d in enumerate(dates) if d >= "20250101"]),
                       ("ALL", list(range(len(dates))))):
        s = stats([bp[i] for i in sel])
        io_rows.append([rule, label, s["n"], round(s["ann_ret"], 6), round(s["ann_vol"], 6), round(s["sharpe"], 4),
                        round(s["max_dd"], 6), round(s["hit_rate"], 4), round(s["mean_bp"], 4), round(s["t_stat"], 4)])
write_csv("outputs/is_oos.csv", ["rule", "period", "n", "ann_ret", "ann_vol", "sharpe", "max_dd", "hit_rate", "mean_bp", "t_stat"], io_rows)
print("is_oos.csv rows", len(io_rows))

# ---------- rank_flip：with_roll × 滑点 × OOS ----------
rk_rows = []
for slip in (0.0, 1.0, 2.0):
    n2 = rule_net(A, tau=0.01, slip=slip)
    sh = {}
    for rule in RULES:
        dates, bp = port(n2[rule]); s = stats(bp); sh[rule] = (s["sharpe"], s["ann_ret"])
    ranks = sorted(RULES, key=lambda r: -sh[r][0])
    for i, rule in enumerate(ranks, 1):
        rk_rows.append(["with_roll", int(slip), "OOS", rule, round(sh[rule][0], 4), round(sh[rule][1], 6), i])
write_csv("outputs/rank_flip.csv", ["roll_mode", "cost_ticks", "period", "rule", "sharpe", "ann_ret", "rank"], rk_rows)
print("rank_flip.csv", [(r[1], r[3], r[6]) for r in rk_rows])

# ---------- τ 敏感性 ----------
tau_rows = []
for tau in (0.005, 0.01, 0.02):
    n3 = rule_net(A, tau=tau, slip=1.0)
    row = [tau]
    for rule in ("B1", "B2", "B3", "D1"):
        dates, bp = port(n3[rule]); row.append(round(stats(bp)["ann_ret"], 4))
    tau_rows.append(row)
write_csv("outputs/sensitivity_tau.csv", ["tau", "B1", "B2", "B3", "D1"], tau_rows)
print("tau", tau_rows)

# ---------- 滑点敏感性 ----------
slip_rows = []
for slip in (0.0, 1.0, 2.0):
    n4 = rule_net(A, tau=0.01, slip=slip)
    row = [int(slip)]
    for rule in RULES:
        dates, bp = port(n4[rule]); row.append(round(stats(bp)["ann_ret"], 4))
    slip_rows.append(row)
write_csv("outputs/sensitivity_slip.csv", ["slip_ticks"] + list(RULES), slip_rows)
print("slip", slip_rows)

# ---------- 4 品种对照（LC/AG/AU/IF）----------
B4 = load("outputs/is_oos_b4/panel_daily.csv")
n5 = rule_net(B4, tau=0.01, slip=1.0)
b4_rows = []
for rule in RULES:
    dates, bp = port(n5[rule])
    sel = [i for i, d in enumerate(dates) if d >= "20250101"]
    s = stats([bp[i] for i in sel]) if sel else stats(bp)
    b4_rows.append([rule, s["n"], round(s["ann_ret"], 4), round(s["sharpe"], 4), round(s["mean_bp"], 4)])
write_csv("outputs/is_oos_b4_v3.csv", ["rule", "n", "ann_ret_oos", "sharpe_oos", "mean_bp"], b4_rows)
print("b4", b4_rows)
