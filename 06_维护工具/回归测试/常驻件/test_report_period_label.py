"""报告期标签 helper 单测 + 统计文件库里「期间与文件名不符」的既有污染。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import data  # noqa: E402

print("=== helper 单测 ===")
CASES = [
    ({"period_type": "annual", "period_end": "2025-12-31"}, "2025年年报"),
    ({"period_type": "semiannual", "period_end": "2026-06-30"}, "2026年半年报"),
    ({"period_type": "first_quarter", "period_end": "2026-03-31"}, "2026年一季报"),
    ({"period_type": "third_quarter", "period_end": "2026-09-30"}, "2026年三季报"),
    ({}, ""),
    ({"period_type": "weird", "period_end": "2026-06-30"}, ""),
    ({"period_type": "annual"}, ""),
    (None, ""),
]
failed = 0
for meta, expect in CASES:
    got = data.report_period_from_metadata(meta)  # type: ignore[arg-type]
    ok = got == expect
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {meta} -> {got!r}（期望 {expect!r}）")

print("\n=== 文件库既有污染（登记的期间 vs 文件名里的期间）===")
rows = data.query_all("SELECT file_name, stock_code, report_period FROM `7_document_index`")
def label_from_name(name: str) -> str:
    match = re.search(r"(20\d{2})\s*年", name)
    if not match:
        return ""
    year = match.group(1)
    if "半年度" in name:
        return f"{year}年半年报"
    if "年度" in name or name.endswith("年报"):
        return f"{year}年年报"
    if "第一季度" in name or "一季报" in name:
        return f"{year}年一季报"
    if "第三季度" in name or "三季报" in name:
        return f"{year}年三季报"
    return ""

mismatch = []
no_name_period = 0
for row in rows:
    name = str(row["file_name"] or "")
    expect = label_from_name(name)
    if not expect:
        no_name_period += 1
    elif expect != str(row["report_period"] or ""):
        mismatch.append((name[:46], str(row["report_period"]), expect))
print(f"索引 {len(rows)} 条；文件名可判定期间的 {len(rows)-no_name_period} 条；其中与登记期间不符 {len(mismatch)} 条")
for name, stored, expect in mismatch[:12]:
    print(f"   {name:48} 登记={stored:12} 应为={expect}")

print("\n结论：" + ("helper 全部通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
