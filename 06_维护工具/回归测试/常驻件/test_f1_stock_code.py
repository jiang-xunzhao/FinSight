"""F1 验证：正文代码提取要跳过子公司代码；文件名代码优先（第 8 页真实上下文当用例）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import data  # noqa: E402

# 真实抓自 600252_20260831_HTXY.pdf 第 8 页
ZHONGHENG_P8 = (
    "公司所属行业及主营业务情况说明（一）公司主要业务介绍"
    "中恒集团以医药制造为核心主导产业，旗下控股9家重要子公司，其中："
    "上市公司1家（莱美药业，股票代码：300006）。业务涵盖医药制造、健康消费、"
    "医药研发、中药资源、日化美妆、医药流通等领域。"
)
CASES = [
    ("中恒第8页（只有子公司代码）", ZHONGHENG_P8, ""),
    ("正常封面", "广西梧州中恒集团股份有限公司\n证券代码：600252\n股票简称：中恒集团", "600252"),
    ("本公司在前", "证券代码：600252 股票简称：中恒集团\n参股公司股票代码：300006", "600252"),
    ("空文本", "", ""),
    ("无标签的裸数字", "公司营业收入 300006 万元", ""),
]
failed = 0
for name, text, expected in CASES:
    got = data.extract_stock_code_from_text(text)
    ok = got == expected
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}: got={got!r} expect={expected!r}")

# 文件名优先：模拟 data.py 里的判定
def decide(path_name: str, full_text: str) -> tuple[str, str]:
    filename_code = data.extract_stock_code_from_name(Path(path_name))
    text_code = data.extract_stock_code_from_text(full_text)
    if filename_code and not filename_code.startswith("FILE_"):
        return filename_code, "文件名优先"
    return (text_code or filename_code), "正文优先"

combo = [
    ("600252_20260831_HTXY.pdf", ZHONGHENG_P8, "600252"),
    ("600252_20260831_HTXY.pdf", "证券代码：600252", "600252"),
    ("招商证券股份有限公司.pdf", "证券代码：600999", "600999"),
    ("老板电器：2026年半年度报告（英文版）.pdf", "无代码正文", "FILE_07848DB2"),
]
for name, text, expected in combo:
    got, why = decide(name, text)
    ok = got == expected
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name} -> {got} ({why})")

print("\n全部通过" if not failed else f"{failed} 个用例失败")
sys.exit(1 if failed else 0)
