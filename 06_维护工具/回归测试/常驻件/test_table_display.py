"""表格显示修复单测：整列统一单位 + 空身份列不再输出。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402
import data  # noqa: E402

failed = 0


def check(name, ok, extra=""):
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}{('  ' + str(extra)) if extra else ''}")


rows = [
    {"stock_code": "000801", "report_period": "2026年半年报", "net_profit": 81198372.22, "revenue": 2595626743.19},
    {"stock_code": "000538", "report_period": "2026年半年报", "net_profit": 3697247984.17, "revenue": 21884075971.15},
]

print("=== 1) 用户选了亿元 ===")
out = data.localize_financial_table(rows, "亿元")
for row in out:
    print("   ", {k: v for k, v in row.items() if k in ("净利润", "营业收入")})
check("净利润整列都是亿元", all(str(r["净利润"]).endswith("亿元") for r in out), out[0]["净利润"])
check("营业收入整列都是亿元", all(str(r["营业收入"]).endswith("亿元") for r in out))

print("\n=== 2) 自动单位：同一列必须同一单位 ===")
out = data.localize_financial_table(rows, "自动")
for row in out:
    print("   ", {k: v for k, v in row.items() if k in ("净利润", "营业收入")})
units = {str(r["净利润"]).split()[-1] for r in out} | {str(r["营业收入"]).split()[-1] for r in out}
check("净利润列内单位一致", len({str(r["净利润"]).split()[-1] for r in out}) == 1, units)
check("小值跟随整列峰值（不再一格万元）", str(out[0]["净利润"]).endswith("亿元"), out[0]["净利润"])

small = [{"report_period": "2026年半年报", "net_profit": 1234.5}, {"report_period": "2025年年报", "net_profit": 9876.5}]
out_small = data.localize_financial_table(small, "自动")
check("小额列仍走万元/元", all(str(r["净利润"]).endswith(("万元", "元")) for r in out_small), [r["净利润"] for r in out_small])

print("\n=== 3) 空身份列不再输出 ===")
execution_no_identity = {
    "results": [
        {"status": "completed", "company": {}, "rows": [{"report_period": "2026年半年报", "revenue": 1.0}], "sources": []}
    ],
    "warnings": [],
}
table, _sources, _warn = assistant.unified_table_from_plan_results(execution_no_identity)
check("不带身份信息的表不再多出两列空值", all("stock_code" not in r and "stock_name" not in r for r in table), table)

execution_with_identity = {
    "results": [
        {"status": "completed", "company": {"ticker": "000801", "name": "四川九洲电器股份有限公司"},
         "rows": [{"report_period": "2026年半年报", "revenue": 1.0}], "sources": []},
        {"status": "completed", "company": {"ticker": "000538", "name": "云南白药集团股份有限公司"},
         "rows": [{"report_period": "2026年半年报", "revenue": 2.0}], "sources": []},
    ],
    "warnings": [],
}
table2, _s2, _w2 = assistant.unified_table_from_plan_results(execution_with_identity)
check("有身份信息时两列保留", all(r.get("stock_code") for r in table2), table2)

print("\n=== 4) 模型多带的库里字段名不能漏进表格 ===")
leaky = [
    {"股票代码": "000801", "净利润": 81198372.22, "is_net_profit": "81198372.2200"},
    {"股票代码": "000538", "净利润": 3697247984.17},
]
out_leak = data.localize_financial_table(leaky, "亿元")
check("漏出的原始列被丢掉", all("is_net_profit" not in r for r in out_leak), out_leak[0])
check("中文列不受影响", all(str(r.get("净利润", "")).endswith("亿元") for r in out_leak), out_leak[0].get("净利润"))
only_internal = [{"foo": 1, "bar": 2}]
out_only = data.localize_financial_table(only_internal, "亿元")
check("整行都是未知键时不清空", out_only == only_internal, out_only)

print("\n结论：" + ("显示层修复通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
