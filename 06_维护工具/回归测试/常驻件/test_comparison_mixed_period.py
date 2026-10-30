"""比较表跨指标混合报告期的诚实性验证（无共同报告期分支）。

复现的 bug：assistant.py 里 `row["report_period"] = selected.get("report_period")`
写在指标循环内部，无共同期时后算的指标会覆盖先算的，整行最终只报「最后一个指标」
的期间，而其它格子的数值其实来自别的期间——表格宣称的口径和真实口径不一致。

做法：把 query_metric_series 换成假序列（一家公司的资产负债率只有 2025 年年报、
营业收入只有 2026 年半年报），绕开「全库语料凑不出跨公司无共同期」的限制。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402

FAIL = 0


def check(label: str, got, want) -> None:
    global FAIL
    if got == want:
        print(f"PASS {label}")
        return
    FAIL += 1
    print(f"FAIL {label}\n      got ={got!r}\n      want={want!r}")


SERIES = {
    # (ticker, column) -> 按报告期倒序的序列
    ("600001", "__debt_ratio__"): [{"report_period": "2025年年报", "value": 0.42}],
    ("600001", "revenue"): [{"report_period": "2026年半年报", "value": 123.4}],
    ("600002", "__debt_ratio__"): [{"report_period": "2025年年报", "value": 0.55}],
    ("600002", "revenue"): [{"report_period": "2026年半年报", "value": 88.8}],
}

assistant.query_metric_series = lambda ticker, table, column: list(
    SERIES.get((str(ticker), str(column)), [])
)

companies = [
    {"ticker": "600001", "name": "甲公司"},
    {"ticker": "600002", "name": "乙公司"},
]
result = assistant.answer_with_multi_company_data(
    "比较甲公司和乙公司的资产负债率和营业收入", "yi", "general", companies
)

if not result:
    print("FAIL 没有产出比较结果")
    sys.exit(1)

table = result.get("table") or []
print("表格:", table)
print("摘要:", str(result.get("summary"))[:160])

check("两家公司都进表", len(table), 2)
row = table[0]
debt_cell = str(row.get("资产负债率") or "")
revenue_cell = str(row.get("营业收入") or "")

# 1) 不许整行只报一个期间却混着两个期间的数值：
#    与行期间不一致的格子必须自己标出来源期间。
row_period = str(row.get("report_period") or "")
check("行报告期非空", bool(row_period), True)
if row_period == "2026年半年报":
    check("资产负债率格子标注真实期间 2025年年报", "2025年年报" in debt_cell, True)
    check("营业收入格子不必多余标注", "2025年年报" in revenue_cell, False)
else:
    print(f"      行期间被写成 {row_period}，检查是否仍是「最后一个指标覆盖」的写法")
    check(
        "无论落在哪个期间，不一致的格子都要自标期间",
        ("2025年年报" in debt_cell) or ("2026年半年报" in revenue_cell),
        True,
    )

# 2) 摘要要说清是各自最新期，不能假装统一口径
summary = str(result.get("summary") or "")
check(
    "摘要不许在无共同期时说「统一口径」而不加限定",
    ("统一口径" not in summary) or ("最新可用" in summary) or ("各公司" in summary),
    True,
)

# 3) 有共同期时必须回归原行为：整行同一个期间、格子不带标注
assistant.query_metric_series = lambda ticker, table_, column: [
    {"report_period": "2026年半年报", "value": 7.7}
]
aligned = assistant.answer_with_multi_company_data(
    "比较甲公司和乙公司的资产负债率和营业收入", "yi", "general", companies
)
aligned_row = (aligned.get("table") or [{}])[0]
check(
    "有共同期时行报告期=共同期",
    str(aligned_row.get("report_period") or ""),
    "2026年半年报",
)
check(
    "有共同期时格子不带期间后缀",
    ("2026年半年报" in str(aligned_row.get("资产负债率") or "")),
    False,
)

print("结论:", "全部通过" if FAIL == 0 else f"{FAIL} 项失败")
sys.exit(1 if FAIL else 0)
