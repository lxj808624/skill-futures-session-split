# skill-futures-session-split

量枢院（QuantSkills）本期任务的交付 skill：**期货时段拆分研究**——把同一信号的收益拆成夜盘 / 日盘 / 闭市跳空三段，比较全天持有、仅日盘、隔夜过滤三种持仓窗口，并检验成本、交易日归属与样本外稳定性。

## 一句话结论

**时段结构真实存在，但没有一种持仓窗口能在统一成本下赚钱（v3 修正后）。** 12 品种 / 22 个月（441 个交易日，399,672 行真实分钟线、等成本口径）上，五条规则**全部为负**：全天持有 -11.42%（OOS -6.37%）、仅日盘 -22.70%（t=-3.99）、跳空门控 -18.02%、夜盘动量 -21.38%、跳空过滤日盘 -20.98%（t=-4.06）。唯一统计显著的结构是**日盘暴露亏钱**，而不是「全天持有赚钱」。样本 A（12 品种 4 个月）里全天持有 +8.93%/年（t=0.55，与 0 无法区分），看着像「夜盘赚钱」，实际是品种 β——LC/AG/AU/IF 四个品种占总贡献 **92.8%**，其中贡献最大的 LC 根本没有夜盘、收益全在跳空段；9 个有夜盘品种里 TA/SC/MA 的夜盘均值为负。

> **v3 重要修正**：v2 的「全天持有 +24.77% / 仅日盘 -7.91% / 成本占 16.6pp」来自两处成本记账错误——B0 未扣自己的往返成本、B2/B3 把成本记在空仓日。修正过程与全部新数字见 `docs/成本口径修正.md`，代码修正在 `scripts/evaluate.py`（`修正（v3）` 注释），复算脚本 `scripts/cost_audit.py`。

**等成本后 3 档滑点（0/1/2 tick）里排名都是 B0 > B2 > B3 > B1 > D1，但这是「负收益里谁亏得少」的排序，不是可上线的正收益排序。** 4 品种时曾出现的「样本外头名翻转」在补齐品种后消失——翻转是品种构成造成的，不是换月口径造成的。

当前状态：**v3 已定稿（成本口径修正）**（12 品种 4 个月 + 同 12 品种 22 个月），数据全量重跑、独立复算校验通过，详见 `docs/研究报告.md`

## 目录

```
config/varieties.yaml     12 个品种：乘数、tick、手续费 bp、夜盘收盘时间
config/study.yaml         τ、样本内外切分、成本档、bootstrap 参数（冻结）
scripts/sessions.py       时段划分、交易日归属、三段收益、恒等式校验
scripts/panel.py          逐日面板组装、不完整交易日剔除、数据质量
scripts/evaluate.py       成本模型、B0/B1/B2/B3/D1 规则回放、指标、bootstrap
scripts/fetch_real.py     通过平台 PandaData MCP 授权取数（分片处理 110 行上限）
scripts/fetch_minute.py   早期 panda_data SDK 版本，本机直连会被拒（保留作参考）
scripts/run_study.py      编排：面板 → 规则 → 差异账本 / 指纹 / 排名翻转
scripts/selfcheck.py      合成数据自检（不联网）
references/               时段规则、成本口径、方法论、失败边界
docs/研究方案.md           研究设计与验收对照
```

## 快速开始

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/selfcheck.py                 # 1. 自检，必须全绿
.venv/bin/python scripts/fetch_real.py --varieties RB,CU --start 20250701 --end 20251031 --frequency 5m
.venv/bin/python scripts/run_study.py --slippage-ticks 1 --tau 0.01
```

合成数据端到端验证（不需要真实授权）：

```bash
.venv/bin/python scripts/run_study.py \
  --config-dir /tmp/qs-synth/config --raw-dir /tmp/qs-synth/raw --out-dir /tmp/qs-synth/out
```

## 产出

| 文件 | 用途 |
| --- | --- |
| `outputs/summary.csv` | 五条规则的年化、波动、Sharpe、回撤、命中率、成本拖累、bootstrap 区间 |
| `outputs/baseline_compare.csv` | 差异账本：相对 B0 的逐项差值 |
| `outputs/is_oos.csv` | 样本内外对比 |
| `outputs/fingerprint.json` | 版本指纹（数据与配置 SHA-256） |
| `outputs/rank_flip.csv` | 排名翻转：成本档 × 换月含/剔除 × IS/OOS |
| `outputs/inputs_outputs_index.json` | 输入输出索引 |

## 口径声明

成本为**行业常规假设**（手续费名义价值双边 2 bp、金融 1 bp；滑点 1 tick/边；0/1/2 tick 敏感性），不是原 Q2 交付物的实测口径。相对基线的增量只作方向性参考。取得 Q2 原始口径后，只改 `config/varieties.yaml` 的 `commission_bp`，其余不动。

价格一律用实际合约成交价，不复权、不拼接连续合约；换月日及前后各 1 个交易日单列。

## 规则

| 编号 | 含义 |
| --- | --- |
| B0 | 全天持有（冻结基线） |
| B1 | 仅日盘 |
| B2 | 跳空门控隔夜（夜盘开盘 `|gap| ≤ τ` 才持有夜盘段） |
| B3 | 夜盘动量门控隔夜（前一交易日夜盘上涨才顺势过夜） |
| D1 | 跳空过滤日盘 |
| D2 | 三段归因（不可交易） |

无前视约束：任何门控只用决策时点已可观测的信息。

## 边界

能做：时段拆分、持仓窗口比较、成本敏感性、交易日归属校验、样本外稳定性、排名翻转。

不能做：不做新日内信号（属 Q3）、不重建回测引擎（引用 Q15）、不在复权价算盈亏。

失败边界与处置见 `references/failure-boundaries.md`。

## 已知未验证

- `fetch_minute.py` 的换月切段逻辑未在真实授权下运行过；`get_future_dominant` 返回字段做了自适应，首次真实取数需人工核对。
- 12 个品种的手续费 bp 为常规假设，未与交易所实际费率逐项核对。

## 仓库说明

- 本仓库**不含**分钟行情原始缓存（`outputs/raw/*.parquet`，约 10MB，且属平台授权数据不宜二次分发）；用 `scripts/fetch_real.py` 可从平台 MCP 完整重建。
- 报告 §4.5 专门交代了统计显著性与检验能力：样本 A 的 83 天只能分辨约 18bp/日 的差异，**B0 +8.93%（t=0.55）与 B2 +0.07%（t=0.03）都与 0 无法区分**（bootstrap 区间均跨零）；441 天上统计显著的是「含日盘暴露的四条规则亏钱」（t −2.66 ~ −4.06）。
- 平台一键运行不满足（issue #24 / PR #25）；期货模拟赛未连接账户，等价回放范围见报告 §9。
