"""钉住轮十两条实测结论：SQL 报告期字面量的执行前归一 + profit_yoy 的口径标注。

① 报告期字面量（实测 2026-10-28，`_tools/_hist/trace_flaky_gm.log`）：同一题
   「中国国贸2026年半年报净利润是多少万元？」连问 6 次，模型 5 次把报告期写成 ISO
   形式 `report_period='2026-06-30'` / `LIKE '2026-06%'` / `LIKE '2026%06%'`，而库里存的是
   中文口径 `2026年半年报` ⇒ **首跑必 0 行**，全靠 attempts=2 的事后纠错
   （`repair_sql_for_missing_metrics`）改回来。纠错链一断就退化成
   「未查询到匹配数据，SQL执行正常。」⇒ 表现为同一题一次有一次没有的 flaky。
   ⇒ 修法：`04_智能分析/assistant.py` 新增 `canonical_period_from_literal` +
     `normalize_period_literals`，并在 `execute_sql_subtask_with_correction` 把归一放到
     **执行前**（initial_value 位置），一次命中。
   只到年的写法（`LIKE '2026%'`）**不动**：它在库里 LIKE 得到，改了反而把范围缩窄。

② profit_yoy 口径（实测 2026-10-28，`_tools/_hist/trace_profit_yoy.log`，抽样 8 家 8/8）：
   这一列是按「五、净利润」（含少数股东）现算的**合并口径**，不是公告「主要会计数据」
   里披露的**归母同比**。少数股东损益大时方向都会相反——
   002195 岩山科技 2025年年报：本列 -98.4641%，公告披露 +184.19%。
   ⇒ 修法：标签/建表注释/写 SQL 的规则三处都把口径写明白（`data.py` COLUMN_LABELS、
     CREATE TABLE 注释 + 一次性 `MODIFY COLUMN` 补注释、FINANCIAL_SCHEMA_PROMPT 第 4 条）。
     数值本身不动：合并口径是有效口径，改算法会连带影响 52 行且需要全量重导。
   下面第 ④ 段把 002195 的三个数钉成档案，防止谁「顺手」把口径改成归母却不留痕。
"""
from __future__ import annotations

import inspect
import sys
from decimal import Decimal
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import assistant  # noqa: E402
import data  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{'' if ok else ' :: ' + detail}")
    if not ok:
        failures.append(name)


GM_QUESTION = "中国国贸2026年半年报净利润是多少万元？"

# ============================================================ ① 报告期字面量归一：正例
POSITIVE = [
    (
        "SELECT net_profit FROM 2_performance_report WHERE stock_code='600007' "
        "AND report_period='2026-06-30' LIMIT 1",
        GM_QUESTION,
        "report_period='2026年半年报'",
    ),
    (
        "SELECT net_profit FROM 2_performance_report WHERE report_period LIKE '2026-06%'",
        GM_QUESTION,
        "report_period='2026年半年报'",
    ),
    (
        "SELECT net_profit FROM 2_performance_report WHERE report_period LIKE '2026%06%'",
        GM_QUESTION,
        "report_period='2026年半年报'",
    ),
    (
        "SELECT net_profit FROM 2_performance_report WHERE r.report_period='2026-06-30'",
        "随便问问",  # 字面量自带月份 ⇒ 不依赖题面
        "r.report_period='2026年半年报'",
    ),
    (
        "SELECT net_profit FROM 2_performance_report WHERE report_period='2025-12-31'",
        "广州发展2025年年报营业收入",
        "report_period='2025年年报'",
    ),
]
for index, (sql, question, expect) in enumerate(POSITIVE, start=1):
    got = assistant.normalize_period_literals(sql, question)
    check(f"①{index} ISO 报告期归一", expect in got, got)

# ============================================================ ① 反例：不该动的绝对不动
NEGATIVE = [
    (
        "SELECT net_profit FROM 2_performance_report WHERE report_period LIKE '2026%'",
        GM_QUESTION,
        "只到年的写法不能改成某一期（库里 2026 有两种口径）",
    ),
    (
        "SELECT net_profit FROM 2_performance_report WHERE report_period='2026年半年报'",
        GM_QUESTION,
        "已经是库内口径要原样保留",
    ),
    (
        "SELECT net_profit FROM 2_performance_report WHERE report_period='2026Q3'",
        "净利润多少",
        "认不出月份、题面也没期间 ⇒ 不能瞎猜",
    ),
]
for index, (sql, question, why) in enumerate(NEGATIVE, start=1):
    check(f"①反{index} 不误改", assistant.normalize_period_literals(sql, question) == sql, why)

check(
    "①归一为空操作时也不报错",
    assistant.normalize_period_literals("SELECT 1", "") == "SELECT 1",
)

# ============================================================ ② 归一确实接在执行前
BODY = inspect.getsource(assistant.execute_sql_subtask_with_correction)
check(
    "②执行入口用归一后的 SQL 作首轮初始值",
    'normalize_period_literals(str(subtask.get("sql") or ""), question)' in BODY
    and "run_with_correction(\n        subtask.get(\"sql\")" not in BODY,
    [line.strip() for line in BODY.splitlines() if "run_with_correction" in line or "normalize_period_literals" in line],
)

# ============================================================ ③ 口径标注三处齐全
check(
    "③展示标签写明合并口径",
    "合并口径" in str(data.COLUMN_LABELS.get("profit_yoy", "")),
    repr(data.COLUMN_LABELS.get("profit_yoy")),
)
check(
    "③建表注释写明合并口径",
    "合并口径" in data.SCHEMA_SQL,
    [line.strip() for line in data.SCHEMA_SQL.splitlines() if "profit_yoy" in line],
)
check(
    "③写 SQL 的规则里有口径提醒",
    "合并口径" in data.FINANCIAL_SCHEMA_PROMPT and "002195" in data.FINANCIAL_SCHEMA_PROMPT,
    data.FINANCIAL_SCHEMA_PROMPT[-200:],
)
_llm_answer_source = inspect.getsource(assistant.answer_with_llm)
check(
    "③正文回答要求也写明合并口径",
    "合并口径" in _llm_answer_source and "归母净利润的同比" in _llm_answer_source,
    _llm_answer_source[-1200:] if "合并口径" not in _llm_answer_source else "",
)
_leaked = assistant.replace_field_names_with_labels("核心结论：合并口径净利润同比为-98.4641%（profit_yoy）。")
check(
    "③正文漏出的字段名换成中文标签",
    "profit_yoy" not in _leaked and "净利润同比（合并口径）" in _leaked,
    _leaked,
)
_mixed = assistant.replace_field_names_with_labels(
    "net_profit_parent 与 net_profit 与 EPS 与 100eps 混排"
)
check(
    "③字段名替换按长名优先且不误伤普通词",
    all(name not in _mixed for name in assistant.FIELD_NAME_LEAKS) and "100eps" in _mixed and "EPS" in _mixed,
    _mixed,
)

# ============================================================ ④ 库里 002195 的三值档案（只读）
row = data.query_all(
    "SELECT net_profit, net_profit_parent, profit_yoy FROM `2_performance_report` "
    "WHERE stock_code=%s AND report_period=%s",
    ("002195", "2025年年报"),
)
if not row:
    # 库是你随时在删/导的：002195 整家不在了 ⇒ 数据面缺席，跳过；公司在库却没有这一期 ⇒ 真问题，继续红。
    in_db = data.query_all(
        "SELECT 1 FROM `2_performance_report` WHERE stock_code='002195' LIMIT 1"
    )
    if in_db:
        check("④002195 2025年年报在库", False, "查不到行")
    else:
        print("  SKIP ④002195 整家未入库（三值档案与列注释两条不检查）")
else:
    live = row[0]
    check(
        "④合并净利润 -110,869,660.17",
        Decimal(str(live["net_profit"])) == Decimal("-110869660.17"),
        str(live),
    )
    check(
        "④归母净利润 +92,252,668.82（与合并反号 ⇒ 口径背离是真的）",
        Decimal(str(live["net_profit_parent"])) == Decimal("92252668.82"),
        str(live),
    )
    check(
        "④profit_yoy = 合并口径自算 -98.4641（公告披露的归母同比是 +184.19%）",
        Decimal(str(live["profit_yoy"])) == Decimal("-98.4641"),
        str(live),
    )
    comment = data.query_all(
        "SELECT COLUMN_COMMENT AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='2_performance_report' "
        "AND COLUMN_NAME='profit_yoy'"
    )
    check(
        "④线上库的列注释也带口径",
        bool(comment) and "合并口径" in str(comment[0]["c"]),
        str(comment),
    )

print("=" * 60)
print("汇总：失败", len(failures), "项", failures or "")
sys.exit(1 if failures else 0)
