---
name: futures-session-split
description: Use when an agent needs to split futures returns into night session, day
  session and close-to-open gap, then compare full-day hold / day-only / overnight-filtered
  holding under one frozen signal — with round-turn cost, trading-date attribution and
  out-of-sample stability checked explicitly.
quantSkills:
  project_type: skill
  category: futures
  tags:
  - futures
  - night-session
  - overnight-gap
  - trading-date-attribution
  - holding-window
  - cost-sensitivity
  - out-of-sample-stability
  - bootstrap-ci
  - rank-flip-check
  - pandadata
  platforms:
  - claude-code
  - codex
  - openclaw
  - cursor
  status: 已交付 v3（12 品种 / 22 个月真实数据全量重跑；v3 修正成本记账：B0 扣自身往返成本、B2/B3 成本改记在交易日；平台内一键运行受宿主故障限制，见报告第 7 节）
  validation_level: real-data-verified
  maintainer_type: community
  summary_zh: 期货时段拆分：把同一信号的收益拆成夜盘 / 日盘 / 闭市跳空三段，对比全天持有、仅日盘、跳空门控隔夜三种持仓窗口，检验成本敏感性、交易日归属正确性与样本外稳定性，并给出排名翻转检查。12 品种 / 22 个月真实分钟线（399,672 行）实测（等成本口径，v3）：五条规则全样本全部为负——全天持有 -11.42%、仅日盘 -22.70%（t=-3.99）、跳空门控 -18.02%、夜盘动量 -21.38%、跳空过滤日盘 -20.98%（t=-4.06）；唯一统计显著的结构是「日盘暴露亏钱」。样本 A（83 天）全天持有 +8.93%/年、t=0.55 与 0 无法区分，「夜盘赚钱」是 LC/AG/AU/IF 的品种 β（占 92.8%），不是时段 α。夜盘交易日归属直接采用平台 trading_date 字段。v3 修正了 v2 的两处成本记账错误（B0 未扣自身往返成本、B2/B3 成本记在空仓日），见 docs/成本口径修正.md。
  summary_en: Splits futures returns into night session, day session and the close-to-open gap, then compares full-day, day-only and gap-gated overnight holding under one frozen signal, with cost sensitivity, trading-date attribution and out-of-sample stability, plus a rank-flip check. Uses PandaData minute bars; night sessions inherit the platform's own trading_date.
  license: MIT
  repository: https://github.com/lxj808624/trading-ip
---

```json qsh-form
{
  "version": 1,
  "task": {
    "placeholder": "补充研究区间、品种范围、成本口径或对时段拆分的具体要求"
  },
  "fields": [
    {
      "key": "varieties",
      "label": "品种范围",
      "type": "select",
      "default": "12",
      "help": "默认覆盖六个交易所共 12 个品种；规模可缩小",
      "options": [
        { "value": "12", "label": "全样本：RB/CU/AU/AG/I/M/TA/MA/SC/LC/IF/T" },
        { "value": "night", "label": "有夜盘品种（10 个）" },
        { "value": "metal", "label": "金属组：CU/AU/AG/I" },
        { "value": "agri", "label": "农产品与化工组：M/TA/MA" }
      ]
    },
    {
      "key": "range",
      "label": "研究区间",
      "type": "select",
      "default": "2021-01-01~2025-10-31",
      "options": [
        { "value": "2021-01-01~2025-10-31", "label": "主区间：2021-01 起" },
        { "value": "2023-01-01~2025-10-31", "label": "近三年" },
        { "value": "2025-01-01~2025-10-31", "label": "当年（样本外）" }
      ]
    },
    {
      "key": "cost_ticks",
      "label": "滑点档位",
      "type": "select",
      "default": "1",
      "help": "手续费固定为名义价值双边 2bp（金融 1bp）",
      "options": [
        { "value": "0", "label": "0 tick（仅手续费）" },
        { "value": "1", "label": "1 tick / 边（基准）" },
        { "value": "2", "label": "2 tick / 边（压力）" },
        { "value": "0,1,2", "label": "三档并列（排名翻转检查）" }
      ]
    },
    {
      "key": "gap_tau",
      "label": "跳空阈值 τ",
      "type": "select",
      "default": "0.01",
      "help": "跳空门控隔夜规则 B2 使用：|隔夜跳空| ≤ τ 才持仓过夜",
      "options": [
        { "value": "0.0025", "label": "0.25%" },
        { "value": "0.005", "label": "0.5%" },
        { "value": "0.01", "label": "1%（先验）" },
        { "value": "0.02", "label": "2%" }
      ]
    },
    {
      "key": "roll_rule",
      "label": "换月规则",
      "type": "select",
      "default": "oi_confirm",
      "help": "换月日及前后各 1 个交易日单列，另出剔除换月对照",
      "options": [
        { "value": "oi_confirm", "label": "持仓量确认（默认）" },
        { "value": "fixed_date", "label": "固定日期" },
        { "value": "gradual", "label": "渐进换月" }
      ]
    }
  ],
  "prompt_template": "{{#task}}任务与材料：\n{{task}}\n\n{{/task}}请对品种范围 {{varieties}}、区间 {{range}} 的期货分钟行情做时段拆分研究：把收益拆成夜盘、日盘、闭市跳空三段，在同一冻结信号下对比全天持有（B0）、仅日盘（B1）、跳空门控隔夜（B2）、夜盘方向门控隔夜（B3）四种持仓窗口，并做跳空过滤日盘（D1）与三段归因（D2）。成本按滑点 {{cost_ticks}} tick、手续费双边 2bp（金融 1bp），跳空阈值 τ={{gap_tau}}，换月规则 {{roll_rule}}。必须核对夜盘 bar 的交易日归属、换月日 ±1 的影响、以及样本外（2025 年）的稳定性，输出中文报告与差异账本。"
}
```

# 期货时段拆分研究 (Futures Session Split)

> 同一个信号，换个持仓窗口，收益结构会不会变？本 skill 把收益拆成**夜盘 / 日盘 / 闭市跳空**三段，在同一冻结信号下比较三种持仓方式，并把成本、交易日归属、样本外稳定性逐项查清楚。

## 管线定位

```
数据接入 → 时段拆分 → 规则回放 → 成本核算 → 稳定性检验 → 差异账本
   PandaData   本 skill 核心    B0/B1/B2/B3     tick+bp      OOS/翻转     vs Q2 基线
```

## 核心恒等式

同一交易日 `d` 上，按分钟收盘价 `p` 定义：

```
r_full(d) = r_night(d) + r_gap(d) + r_day(d)
r_night(d) = p(night_end, d) / p(prev_day_end, d-1) - 1     # 夜盘段（含前日收盘到夜盘开盘）
r_day(d)   = p(day_end, d)   / p(night_end, d) - 1
r_gap(d)   = p(night_open, d) / p(day_end, d-1) - 1          # 无夜盘品种：r_night ≡ 0
```

三段相乘恒等于全天。selfcheck 用合成数据验证该恒等式偏差为 0。

## 核心规则

1. **交易日归属直接用平台字段**。PandaData 分钟 bar 自带 `trading_date`，夜盘 bar（例如 21:03）的 `trading_date` 是下一个交易日，不需要自己推。只做**一致性校验**：按日历推出来的归属必须与字段一致，不一致要计数并报异常。
2. **不复权、不拼连续价**。收益必须来自实际合约成交价；主力连续是拼接价，跨合约那段收益是假的。
3. **换月日隔离**。换月当天跨合约跳价会污染夜盘段与跳空段；换月日及前后各 1 个交易日单列，必须同时出「含换月」与「剔除换月」两套结果。
4. **成本按手数口径**。手续费按名义价值双边 bp 计，滑点按 tick/边计，开平各一次；给出 0 / 1 / 2 tick 三档。
5. **只比较持仓窗口，不改信号**。信号冻结，换的只是「什么时候持有」。同一根 bar 上不同规则不得使用不同信号。
6. **样本外独立**。IS/OOS 切分固定，OOS 至少覆盖一个完整年度；OOS 结论不得回头调参。
7. **负结果照实报**。时段效应不显著、或加成本后消失，同样是有效结论，且要在报告开头给出。

## 规则集

| 编号 | 名称 | 定义 |
| --- | --- | --- |
| B0 | 全天持有（冻结基线） | 信号发出后持有到下一交易日收盘，跨夜盘 |
| B1 | 仅日盘 | 日盘开盘建仓、收盘平仓，不持有隔夜 |
| B2 | 跳空门控隔夜 | 夜盘开盘 `|gap| ≤ τ` 才持有夜盘段（不含跳空本身），否则空仓过夜 |
| B3 | 夜盘动量门控隔夜 | 前一交易日夜盘上涨才顺势过夜（滞后一期，无前视） |
| D1 | 跳空过滤日盘 | 日盘按 `gap` 方向过滤，只做与跳空方向一致的日盘 |
| D2 | 三段归因 | 不可交易：把 B0 收益分解到 night / gap / day 三段 |

## 工作流

```
1. 取数：scripts/fetch_real.py --varieties RB,CU,... --start 20250701 --end 20251031 --frequency 5m
        → outputs/raw/<code>.parquet（含 symbol/datetime/trading_date/OHLC/volume/oi）
2. 时段拆分：scripts/sessions.py 构建交易日、分钟网格、night/gap/day/full 收益
3. 面板：scripts/panel.py 对齐品种、剔除不完整交易日、输出数据质量表
4. 规则回放：scripts/evaluate.py 跑 B0/B1/B2/B3/D1，D2 做归因
5. 成本与稳定性：tick 三档 × 换月含/剔除 × IS/OOS，bootstrap 置信区间
6. 交付：scripts/run_study.py 产出 summary / baseline_compare / is_oos / yearly /
        fingerprint.json / rank_flip.csv / inputs_outputs_index.json
```

## 输出文件

| 文件 | 内容 |
| --- | --- |
| `outputs/summary.csv` | 每条规则的年化、波动、Sharpe、最大回撤、命中率、成本拖累 |
| `outputs/baseline_compare.csv` | 差异账本：各规则相对 B0 的逐项差值（IS / OOS 各一套） |
| `outputs/is_oos.csv` | 样本内外对比 |
| `outputs/yearly.csv` | 逐年表现 |
| `outputs/fingerprint.json` | 版本指纹：数据与配置的 SHA-256 |
| `outputs/rank_flip.csv` | 排名翻转检查：成本档 × 换月 × IS/OOS |
| `outputs/inputs_outputs_index.json` | 输入输出索引：每个产物对应的输入、参数、行数 |

## 成本口径（近似，必须声明）

- 手续费：名义价值双边 2 bp（SHF/DCE/CZC/INE/GFE）、1 bp（CFE）
- 滑点：1 tick / 边，敏感性 0 / 1 / 2 tick
- 不含：保证金占用、涨跌停无法成交、盘中强平、交割与仓储
- **声明**：以上为常规假设，不是原 Q2 交付物的实测口径；取得 Q2 原始口径后只替换 `config/varieties.yaml` 的成本字段，代码不变。

## 使用边界

**能做的**：分钟级期货行情的时段拆分、持仓窗口比较、成本敏感性、交易日归属校验、样本外稳定性与排名翻转检查。

**不能做的**：不做新的日内信号挖掘（那是 Q3 的范畴）；不重建回测引擎或策略注册表（引用 Q15 的统一基准即可）；不在复权价上算盈亏。

**失败边界**：见 `references/failure-boundaries.md`。

## 依赖

```bash
pip install -r requirements.txt
```

## 复跑

```bash
python scripts/selfcheck.py                                   # 合成自检，不联网
python scripts/fetch_minute.py --varieties RB,CU --start 2025-01-01 --end 2025-10-31
python scripts/run_study.py --slippage 1 --tau 0.01
```

真实取数需在 QuantStudio 平台内完成 PandaData 授权（OAuth），本机直接用 `panda_data` 账号登录会被服务端拒绝（错误码 200006）。
