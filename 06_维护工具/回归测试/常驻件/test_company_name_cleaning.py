"""公司名清洗接线单测：脏前缀、关于头、随机文件名、证券简称不被误杀。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import data  # noqa: E402

failed = 0


def check(name, ok, got=""):
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  -> {got!r}" if got != "" else ""))


print("=== 1) 正文提取：脏前缀必须被清掉 ===")
CASES = [
    ("Chinesename重庆长安汽车股份有限公司\n", "000625", "重庆长安汽车股份有限公司"),
    ("sChinesename重庆长安汽车股份有限公司\n", "000625", "重庆长安汽车股份有限公司"),
    ("CompanynameinChinese广西梧州中恒集团股份有限公司\n", "600252", "广西梧州中恒集团股份有限公司"),
    ("关于重庆美心翼申机械股份有限公司的重大事项公告\n", "920833", "重庆美心翼申机械股份有限公司"),
    ("公司名称：TCL科技集团股份有限公司\n", "000100", "TCL科技集团股份有限公司"),
    ("公司名称：云南白药集团股份有限公司（证券代码：000538）\n", "000538", "云南白药集团股份有限公司"),
    ("证券简称：五粮液\n股票简称无关\n", "000858", "五粮液"),
]
for text, code, expect in CASES:
    got = data.extract_company_name(text, code)
    check(f"extract_company_name({text.splitlines()[0][:22]}…)", got == expect, got)

print("\n=== 2) 文件名兜底 ===")
STEMS = [
    ("老板电器：2026年半年度报告（英文版）", "老板电器"),
    ("长  安Ｂ：2026年半年度报告摘要", "长安Ｂ"),
    ("920833_20260430_HTXY", ""),
    ("f5793fdcafee441990fe8d0c781503fe", ""),
    ("四川九洲电器股份有限公司2026年半年度报告全文", "四川九洲电器股份有限公司"),
    ("云南白药集团股份有限公司", "云南白药集团股份有限公司"),
]
for stem, expect in STEMS:
    got = data.company_name_from_file_stem(stem)
    check(f"from_stem({stem[:30]})", got == expect, got)
check(
    "随机名不会当公司名入库",
    data.is_opaque_file_stem("f5793fdcafee441990fe8d0c781503fe")
    and data.is_generated_financial_stem("920833_20260430_HTXY"),
)

print("\n=== 3) finalize 的门槛只卡全称，不杀简称 ===")
check("finalize 全称", data.finalize_company_name("关于重庆美心翼申机械股份有限公司") == "重庆美心翼申机械股份有限公司")
check("finalize 短名被拒（全称出口）", data.finalize_company_name("五粮液") == "")
check("clean 短名保留（简称出口传的是已剥标签的名字）", data.clean_company_name("五粮液") == "五粮液")
check("整行带标签时不误当成公司名", data.clean_company_name("证券简称：五粮液") == "")

print("\n结论：" + ("公司名清洗全部通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
