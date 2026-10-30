"""数据层修复单测：营业成本别名 + 报告期年份。"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import data  # noqa: E402

print("=== 1) 科目匹配：营业总成本 不该再冒充 营业成本 ===")
CASES = [
    ("二、营业总成本", None),
    ("营业总成本", None),
    ("其中：营业成本", "operating_cost"),
    ("营业成本", "operating_cost"),
    ("一、营业总收入", "operating_revenue"),
    ("营业收入", "operating_revenue"),
    ("税金及附加", "taxes_and_surcharges"),
]
failed = 0
for label, expect in CASES:
    got = data.match_metric(label)
    ok = got == expect
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} match_metric({label!r}) -> {got!r}（期望 {expect!r}）")

print("\n=== 2) 对照：把别名加回去，就会看到污染重现 ===")
saved = dict(data.ALIASES)
print("现在:", data.match_metric("二、营业总成本"))
data.ALIASES["营业总成本"] = "operating_cost"
print("旧行为:", data.match_metric("二、营业总成本"))
data.ALIASES.clear()
data.ALIASES.update(saved)

print("\n=== 3) 报告期识别 ===")
PERIODS = [
    ("重庆长安汽车股份有限公司 2025年年度报告\n目录 第一章", date(2026, 4, 15), ("annual", date(2025, 12, 31), 2025)),
    ("四川九洲电器股份有限公司 2026年半年度报告", date(2026, 8, 31), ("semiannual", date(2026, 6, 30), 2026)),
    ("2025年年度报告" + "\n filler " * 800 + "本公司已于 2025年半年度报告 中披露过该事项", date(2026, 3, 20), ("annual", date(2025, 12, 31), 2025)),
    ("广州发展集团股份有限公司 2026年第一季度报告", date(2026, 4, 28), ("first_quarter", date(2026, 3, 31), 2026)),
    ("某公司三季度报告", date(2026, 10, 20), ("semiannual", date(2026, 6, 30), 2026)),
]
for text, filename_date, expect in PERIODS:
    period_type, period_end, period_start, year = data.extract_report_period(text, filename_date)
    got = (period_type, period_end, year)
    ok = got == expect
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {text.splitlines()[0][:26]:28} -> {got}（期望 {expect}）")

print("\n结论：" + ("全部通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
