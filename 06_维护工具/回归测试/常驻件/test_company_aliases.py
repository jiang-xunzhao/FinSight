"""零成本：检查新别名规则的效果与跨公司歧义。"""
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402

companies = [c for c in assistant.get_company_list() if not str(c.get("stock_code")).startswith("FILE_")]
print(f"真实代码公司 {len(companies)} 家")

owners: dict[str, set[str]] = defaultdict(set)
for item in companies:
    name = str(item.get("stock_name") or "").strip()
    for alias in assistant.company_alias_candidates(name, unique_only=False):
        owners[alias].add(name)

shared = {a: t for a, t in owners.items() if len(t) > 1}
print(f"候选别名 {len(owners)} 个，其中跨公司歧义 {len(shared)} 个（应已被过滤）")
kept = set()
for item in companies:
    kept.update(assistant.company_aliases(str(item.get("stock_name") or "")))
leaked = sorted(alias for alias in shared if alias in kept)
print(f"实际保留下来的歧义别名 {len(leaked)} 个: {leaked[:10]}")

print("\n=== 简称识别效果 ===")
CASES = [
    ("云南白药的营业收入是多少", "云南白药", 1),
    ("对比四川九洲和云南白药的营业收入和净利润", "四川九洲", 2),
    ("对比中恒集团和莱美药业的营业收入", "中恒", 2),
    ("泸州老窖的净利润", "泸州老窖", 1),
    ("广西能源的资产负债率", "广西能源", 1),
    ("招商证券的营业收入", "招商证券", 1),
    ("股份有限公司有几家", "", 0),  # 通名不该锁定到某家公司
]
failed = 0
for question, expect_name, min_companies in CASES:
    comp = assistant.identify_comparison_companies(question)
    ident = assistant.identify_company_from_question(question)
    filt = assistant.identify_company_filter(question)
    names = [c["name"] for c in comp] + ([ident["name"]] if ident else []) + ([filt[1]] if filt[1] else [])
    if expect_name:
        ok = any(expect_name in n for n in names) and (len(comp) >= min_companies or min_companies == 1)
    else:
        ok = not names
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {question}")
    print(f"      比较={[(c['ticker'], c['name'][:12]) for c in comp]} 单个={(ident or {}).get('ticker')} 过滤={filt[0]!r}")

print("\n结论：" + ("识别全部符合预期" if not failed else f"{failed} 条不符合预期"))
sys.exit(1 if failed else 0)
