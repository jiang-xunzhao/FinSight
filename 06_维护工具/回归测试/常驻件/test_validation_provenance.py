"""勾稽校验单测：推导值不再伪装成真实校验、所得税为负时不再误报异常。"""
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import data  # noqa: E402


def mk(code, value, derived=False, statement="balance_sheet"):
    return data.StatementValue(
        metric_code=code,
        metric_name=data.METRICS[code][0],
        statement_type=statement,
        current=Decimal(value),
        previous=None,
        current_raw=str(value),
        previous_raw=None,
        page_no=1,
        row_index=1,
        confidence=Decimal("0.80"),
        derived=derived,
    )


failed = 0


def check(name, condition, extra=""):
    global failed
    failed += not condition
    print(f"{'PASS' if condition else 'FAIL'} {name}{('  ' + extra) if extra else ''}")


print("=== 1) derive 会打标记 ===")
values = {"current_assets": mk("current_assets", 100), "non_current_assets": mk("non_current_assets", 200)}
data.derive_missing_values(values)
check("total_assets 被推导出来", "total_assets" in values)
check("推导值带 derived 标记", values["total_assets"].derived is True)

print("\n=== 2) 勾稽状态与说明 ===")
rows = {row["rule"]: row for row in data.build_validation_rows(values)}
bs = rows["BS_ASSET"]
check("构造性通过仍标 通过", bs["status"] == "通过", f"status={bs['status']}")
check("说明里写明是推导值", "推导值" in bs["message"], f"message={bs['message']!r}")

real = {
    "current_assets": mk("current_assets", 100),
    "non_current_assets": mk("non_current_assets", 200),
    "total_assets": mk("total_assets", 300),
}
rows2 = {row["rule"]: row for row in data.build_validation_rows(real)}
check("全原文数据不带推导说明", rows2["BS_ASSET"]["message"] == "", f"message={rows2['BS_ASSET']['message']!r}")

print("\n=== 3) 所得税费用为负（税项返还）===")
income = {
    "total_profit": mk("total_profit", 1000, statement="income_statement"),
    "income_tax_expense": mk("income_tax_expense", -50, statement="income_statement"),
    "net_profit": mk("net_profit", 1050, statement="income_statement"),
}
row = {r["rule"]: r for r in data.build_validation_rows(income)}["IS_NET"]
check("负所得税按正确符号勾稽为通过", row["status"] == "通过", f"status={row['status']} message={row['message']!r}")
old_ok = abs(1000 - abs(Decimal(-50)) - 1050) == 0
check("旧 abs 公式会误报（对照）", not old_ok, "旧公式判定为不通过 -> 确实是误报来源")

bad = dict(income)
bad["net_profit"] = mk("net_profit", 900, statement="income_statement")
row_bad = {r["rule"]: r for r in data.build_validation_rows(bad)}["IS_NET"]
check("真不一致仍报异常", row_bad["status"] == "异常", f"message={row_bad['message']!r}")

print("\n结论：" + ("勾稽校验修复通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
