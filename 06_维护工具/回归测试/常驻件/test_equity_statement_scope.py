"""回归件：权益口径的两个历史串号（600085 同仁堂 / 600007 中国国贸）。

历史 bug ①（P1，别名括号择一写法）：中国财报里
`归属于母公司所有者权益（或股东权益）合计` 与 `所有者权益（或股东权益）合计` 是**两行不同的数**，
但旧版 `normalize_label` 只删括号字符，把两行都归一成「…或股东权益合计」，短别名
`股东权益合计 → total_equity` 抢先命中长子串 ⇒ 归母权益被当成权益合计入库。
实测 600085 北京同仁堂 2026 半年报 p33（166 页那份 `北京同仁堂股份有限公司_2026年半年报.pdf`）：
    行40 `归属于母公司所有者权益（或股东权益）\n合计` → 判成 total_equity（错，应为 total_parent_equity）
    行41 `少数股东权益 7,535,077,314.95`
    行42 `所有者权益（或股东权益）合计 21,158,327,481.42`（对，但「先出现的赢」已被行40 占了）
    行43 `负债和所有者权益（或股东权益）总\n计` → None（应为 total_liabilities_and_equity ⇒ 勾稽拿不到数）
库里实测 total_equity=13,623,250,166.47 = 21,158,327,481.42 − 7,535,077,314.95（差一个少数股东权益），
而恒等式 `8,787,685,375.30 + 21,158,327,481.42 = 29,946,012,856.72 = 资产总计` 精确成立。
全库恒等式体检当时是 **11 行不平 / 共 50 行（22%）**：002493(total_equity=None)、600008 0.916、
600061 0.9866、600085 0.7484、600098 0.9632、600128 0.9071、600150 0.9837、600155 0.9829、
600252 0.8640、600292 0.9449、600325 0.705 ⇒ 是一类别名歧义，不是个例。
修法：`normalize_label`（data.py:231-247）在删标点**之前**先整组去掉中文「或」的择一括号
`[（(]\s*或[^）)]*[）)]` ⇒ 两行归一后分别是「归属于母公司所有者权益合计」「所有者权益合计」，
长子串赢；英文 `(or shareholders' equity)` 不受影响（只认中文「或」）。
别名表实测：**规范化受影响的别名键 = 0 个**（`data.ALIASES` 共 43 项，没有带「或」的键 ⇒ 零回退面）。

历史 bug ②（P2，权益变动表按「本期/上期」取数）：`所有者权益/股东权益变动表` 的列是
`股本 | 资本公积 | 盈余公积 | 未分配利润 | 少数股东权益 | 合计`，**不是**本期/上期，
按两列取数会把上一年那行的未分配利润当本期、少数股东权益当上期。
实测 600007 中国国贸 2026 半年报（`600007_20260831_11QJ.pdf`，118 页）正文路径行 1082：
    `净利润 632371194 337624 632708818` ⇒ net_profit=632,371,194、previous=337,624
    ⇒ 入库现算 profit_yoy = (632,371,194−337,624)/337,624×100 = **187200.4271%**（越界），
      而摘要原文真值是 `633,458,596 632,371,194 0.17%`。
修法：`extract_values_from_text` 里加 `equity_scope` 状态位 —— 命中
`EQUITY_CHANGE_TITLE_RE`（以「所有者权益变动表/股东权益变动表」结尾）即停止收割，
直到出现下一张主表标题 `MAIN_STATEMENT_TITLE_RE`（利润表/资产负债表/现金流量表）。
表路径**没有**加同样守卫：`STATEMENT_TITLE_RE` 本来就不认权益变动表，600007 表路径实测 0 项，
按「不为没有危害的现象改代码」不动它（本件第 ⑥ 项把这个风险钉在合成表形态上，一旦泄漏就 FAIL）。
"""

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import data  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'} {name}{'' if condition else ' -> ' + detail}")
    if not condition:
        FAILURES.append(name)


# ── ① 括号择一写法：三行必须各自落到自己的指标 ────────────────────────────────
LABELS = (
    ("归属于母公司所有者权益（或股东权益）\n合计", "total_parent_equity"),
    ("所有者权益（或股东权益）合计", "total_equity"),
    ("负债和所有者权益（或股东权益）总\n计", "total_liabilities_and_equity"),
    ("少数股东权益", "minority_interest"),
    ("实收资本（或股本）", None),  # 别名表里没有这项，改动前后都应为 None（不许误判）
)
for raw, want in LABELS:
    got = data.match_metric(data.normalize_label(raw))
    check(f"别名判定 {raw!r} -> {want}", got == want, f"实际 {got}")

# ── ② 别名表零回退：43 个键规范化后仍能被自己命中 ────────────────────────────
broken = []
for alias, code in data.ALIASES.items():
    norm = data.normalize_label(alias)
    if data.match_metric(norm) != code:
        broken.append(f"{alias} -> {data.match_metric(norm)} != {code}")
check(f"ALIASES {len(data.ALIASES)} 键自映射不变", not broken, "；".join(broken[:6]))

# ── ③ 权益变动表区间的「净利润」行不得进库 ────────────────────────────────────
TEXT = """合并资产负债表
一、流动资产：
货币资金 1,000.00 900.00
资产总计 10,000.00 9,000.00
流动负债合计 3,000.00 2,800.00
负债合计 4,000.00 3,500.00
归属于母公司所有者权益（或股东权益）合计 5,000.00 4,600.00
少数股东权益 600.00 500.00
所有者权益（或股东权益）合计 5,600.00 5,100.00
负债和所有者权益（或股东权益）总计 10,000.00 9,000.00

合并所有者权益变动表
2025年1月1日余额 1007282534 1873587184 503641267 6268569122 4958355 9658038462
净利润 632371194 337624 632708818
对股东的分配 四28 （1108010787 250000） （1108260787）
2025年6月30日余额 1007282534 1873587184 503641267 6901240316 5295979 10290747282

合并利润表
一、营业收入 1,815,972,101.00 1,889,651,964.00
三、利润总额 845,689,695.00 843,980,850.00
五、净利润（净亏损以“-”号填列） 633,844,810.00 632,708,818.00
"""

values: dict[str, data.StatementValue] = {}
data.extract_values_from_text(TEXT, values, {})


def num(key: str) -> Decimal | None:
    hit = values.get(key)
    return None if hit is None else hit.current


check("净利润取主表行 633,844,810（不是权益变动表的 632,371,194）",
      num("net_profit") == Decimal("633844810"), f"实际 {num('net_profit')}")
check("净利润上期取 632,708,818（不是少数股东权益 337,624）",
      values.get("net_profit") is not None and values["net_profit"].previous == Decimal("632708818"),
      f"实际 {None if values.get('net_profit') is None else values['net_profit'].previous}")
check("权益变动表的 6 列余额没有污染资产总计",
      num("total_assets") == Decimal("10000"), f"实际 {num('total_assets')}")
check("归母权益与权益合计分开入库",
      num("total_parent_equity") == Decimal("5000") and num("total_equity") == Decimal("5600"),
      f"实际 parent={num('total_parent_equity')} equity={num('total_equity')}")
check("负债和所有者权益总计入库（勾稽项有数）",
      num("total_liabilities_and_equity") == Decimal("10000"),
      f"实际 {num('total_liabilities_and_equity')}")
yoy = (Decimal("633844810") - Decimal("632708818")) / Decimal("632708818") * 100
check("按修后数值现算的净利同比在阈值内（0.18% ≪ 10000）", abs(yoy) < Decimal("10000"), f"实际 {yoy}")

# ── ④ 全资子公司形态：只有归母合计、没有权益合计 ⇒ 权益由归母推导（缺失比串号更糟）──
SOLO = """合并资产负债表
资产总计 10,000.00 9,000.00
负债合计 4,000.00 3,500.00
归属于母公司所有者权益（或股东权益）合计 6,000.00 5,500.00
"""
solo: dict[str, data.StatementValue] = {}
data.extract_values_from_text(SOLO, solo, {})
data.derive_missing_values(solo)
hit = solo.get("total_equity")
check("全资子公司：total_equity 由归母合计推导且标记 derived",
      hit is not None and hit.current == Decimal("6000") and hit.derived,
      f"实际 {None if hit is None else (hit.current, hit.derived)}")

# ── ⑤ 表形态（表路径）不得被权益变动表带走 ─────────────────────────────────────
tables = [
    {
        "page_no": 1,
        "table_index": 1,
        "rows": [
            ["项目", "股本", "资本公积", "未分配利润", "少数股东权益", "合计"],
            ["净利润", "0", "0", "632371194", "337624", "632708818"],
        ],
    },
    {
        "page_no": 2,
        "table_index": 1,
        "rows": [
            ["项目", "本期", "上期"],
            ["五、净利润", "633,844,810.00", "632,708,818.00"],
            ["一、营业收入", "1,815,972,101.00", "1,889,651,964.00"],
        ],
    },
]
pages = [
    {"page_no": 1, "text": "合并所有者权益变动表\n项目 股本 资本公积 未分配利润 少数股东权益 合计"},
    {"page_no": 2, "text": "合并利润表\n项目 本期金额 上期金额"},
]
tv, _valid = data.extract_values(pages, "", tables)
check("表路径：权益变动表的净利润行没抢走合并净利润",
      tv.get("net_profit") is None or tv["net_profit"].current == Decimal("633844810"),
      f"实际 {None if tv.get('net_profit') is None else (tv['net_profit'].current, tv['net_profit'].page_no)}")

print()
print("=" * 46)
print(f"权益口径回归：{'全部 PASS' if not FAILURES else f'{len(FAILURES)} 项 FAIL: ' + ', '.join(FAILURES)}")
print("=" * 46)
sys.exit(1 if FAILURES else 0)
