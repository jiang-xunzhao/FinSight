"""钉住 002595 山东豪迈机械科技 2026 半年报暴露的三条实测缺陷的修法：

① 「公司名称 暂无数据」：智能模式里 LLM 写的 SQL 只 SELECT 得到 `stock_code`——事实表
   `2_performance_report` / `3_income_statement` **根本没有 stock_name 列**——于是
   `unified_table_from_plan_results` 建出来的 `stock_name` 是空串，`localize_financial_table`
   的 `format_value` 把空串渲染成「暂无数据」；而顶层 `company_name` 明明是对的。
   ⇒ 修法：`04_智能分析/assistant.py:2715-2729` 按代码从主数据回填名称 +
     证据链 `assistant.py` 里 `公司名称` 命中占位符时不再短路掉真名。
② 「净利润为什么等于归母净利润」：库里两个口径是分开的两列（净利润 1,134,069,896.00 /
   归母净利润 1,133,633,584.68，差 436,311.32 = 少数股东损益，PDF p49 原文可对），
   但亿元两位小数把两者都显示成「11.34 亿元」⇒ 差异被**显示层**抹掉。
   ⇒ 修法：`03_数据层/data.py:3457-3484` 只把同一行里格式化后撞车的金额列逐级降到
     万元/元，其余列不动。
③ 每股收益抓错：第三节「股份变动对最近一年和最近一期基本每股收益…的影响」那张重算表
   （p38，值 3.0005 = 2025 **年度**按转增前股本重算）被科目投票判成 income_statement 收进
   合并口径，且 p38 排在真报表（p50 合并利润表尾 0.9802）之前 ⇒ 撞上「同一标签首次出现即锁定」。
   pdfplumber 把表头「项目 股本变动前 股本变动后」切在**表外**，所以判据只能用页内上文。
   ⇒ 修法：`03_数据层/data.py:1212-1224` `restated_indicator_table` +
     `data.py:1320-1327` 表格分支整张跳过。

本件查库（只读）但不发网络请求；服务起没起都能跑。
"""
from __future__ import annotations

import re
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


# ============================================================ ① 按代码回填公司名称
CODE = "002595"
NAME = "山东豪迈机械科技股份有限公司"

# 复现实测形状：SQL 只带 stock_code，item 里也没有 company ⇒ 名称只能靠回填
execution = {
    "warnings": [],
    "results": [
        {
            "status": "completed",
            "sources": [],
            "rows": [
                {
                    "stock_code": CODE,
                    "report_period": "2026年半年报",
                    "revenue": Decimal("6016260725.8900"),
                    "net_profit": Decimal("1134069896.0000"),
                    "net_profit_parent": Decimal("1133633584.6800"),
                }
            ],
        }
    ],
}
merged, _sources, _warn = assistant.unified_table_from_plan_results(execution)
check("合并后只有一行", len(merged) == 1, f"{merged}")
row0 = merged[0] if merged else {}
check("空 stock_name 已按代码回填", str(row0.get("stock_name") or "") == NAME, f"实际={row0.get('stock_name')!r}")
localized = data.localize_financial_table(merged)
cell = str((localized[0] if localized else {}).get("公司名称", ""))
check("展示层不再出现「暂无数据」", cell == NAME, f"公司名称={cell!r}")

# 主数据里查不到的代码 ⇒ 不硬造名字，留空让「全表无身份就不出这两列」的既有规则兜底
ghost = assistant.unified_table_from_plan_results(
    {"warnings": [], "results": [{"status": "completed", "sources": [], "rows": [
        {"stock_code": "999999", "report_period": "2026年半年报", "revenue": Decimal("1")}]}]})
check("查不到的代码不编造名称", not str((ghost[0][0] if ghost[0] else {}).get("stock_name") or ""),
      f"{ghost[0]}")

# 证据链：占位符不能短路掉顶层真名（源码守卫，写法一改就报）
assistant_src = (ROOT / "04_智能分析" / "assistant.py").read_text(encoding="utf-8")
check("证据链过滤占位符（源码守卫）",
      'not in {"", "暂无数据", "无", "None"}' in assistant_src,
      "找不到 assistant.py:4530 那段占位符过滤")

# ============================================================ ② 同形金额列降级
collision = data.localize_financial_table([
    {"stock_code": CODE, "stock_name": NAME, "report_period": "2026年半年报",
     "revenue": Decimal("6016260725.8900"),
     "net_profit": Decimal("1134069896.0000"),
     "net_profit_parent": Decimal("1133633584.6800"),
     "revenue_yoy": Decimal("14.2665"), "profit_yoy": Decimal("-5.2341")}])[0]
np_text, parent_text = str(collision.get("净利润")), str(collision.get("归母净利润"))
check("亿元同形已消除", np_text != parent_text, f"净利润={np_text} 归母={parent_text}")
check("两个口径都还带单位", bool(re.search(r"万元|亿元|元$", np_text)) and bool(re.search(r"万元|亿元|元$", parent_text)),
      f"{np_text} / {parent_text}")
def wan(text: str) -> Decimal:
    """把「113,406.99 万元」这种显示串还原成万元数值。"""
    match = re.search(r"([\d,]+(?:\.\d+)?)", text)
    return Decimal((match.group(1) if match else "0").replace(",", ""))


check("降到的单位能显出差额 43.63", np_text.endswith("万元") and parent_text.endswith("万元")
      and (wan(np_text) - wan(parent_text)).quantize(Decimal("0.01")) == Decimal("43.63"),
      f"{np_text} - {parent_text}")
check("没撞车的营业收入不动", str(collision.get("营业收入")) == "60.16 亿元", str(collision.get("营业收入")))
check("同比列没被牵连", str(collision.get("营收同比")) == "14.27%", str(collision.get("营收同比")))

# 真相等的两列不该被降级（否则「万元」噪声会骗用户说有差异）
same = data.localize_financial_table([{"net_profit": Decimal("100000000"), "net_profit_parent": Decimal("100000000")}])[0]
check("数值真相等时保持同形", str(same.get("净利润")) == str(same.get("归母净利润")) == "1.00 亿元", f"{same}")
# 三列里两列**本来相等**（营业收入 == 净利润）时：相等的那两列必须继续显示相等，
# 只有真的不同的那列要显出差别——不能为了「好看」把差异抹平，也不能把等值拆成两个数。
triple = data.localize_financial_table([{"revenue": Decimal("1134069896.0"), "net_profit": Decimal("1134069896.0"),
                                         "net_profit_parent": Decimal("1133633584.68")}])
t0 = triple[0]
check("三列撞车：相等的不拆、不同的分开",
      str(t0.get("营业收入")) == str(t0.get("净利润")) != str(t0.get("归母净利润")), f"{t0}")

# ============================================================ ③ 重算表闸门
# 实测 002595 p38（第三节 股份变动）：pdfplumber 只给这三行，表头在表外
RESTATED_ROWS = [
    ["基本每股收益（元/股）", "", "", "3.0005", "2.0693"],
    ["稀释每股收益（元/股）", "", "", "3.0005", "2.0693"],
    ["归属于公司普通股股东的每股净资产", "（元/股）", "", "15.03", "10.37"],
]
RESTATED_CTX = ("…拟以资本公积金向全体股东每10股转增1股，合计转增60,000,000股，"
                "转增后总股本增至1,160,000,000股。公司按照调整后的股本列示2025年度相应财务指标。"
                "项目股本变动前股本变动后")
check("p38 重算表被拦", data.restated_indicator_table(RESTATED_ROWS, RESTATED_CTX) is True)
# 反例 1：真合并利润表尾（p50），上文是「八、每股收益：」——不能误伤
REAL_ROWS = [["（一）基本每股收益", "", "0.9802", "1.0348"], ["（二）稀释每股收益", "", "0.9802", "1.0348"]]
check("真每股收益行不误伤", data.restated_indicator_table(REAL_ROWS, "八、每股收益：") is False)
# 反例 2：上文有股份变动但没有重算列头 ⇒ 不拦
check("无变动前后列头不拦", data.restated_indicator_table(REAL_ROWS, "第三节 股份变动情况 股东总数 12,345 户") is False)
# 反例 3：股份变动区里**不含每股指标**的表（期初/期末股本那种）⇒ 不拦，避免扩大影响面
check("不含每股指标不拦", data.restated_indicator_table([["期初股本", "1,100,000,000", "1,160,000,000"]],
                                                        "项目股本变动前股本变动后") is False)
# 表内就带「变动前/变动后」的其它写法（表头进了表里）也得拦
check("表头在表内也拦", data.restated_indicator_table([["项目", "股本变动前", "股本变动后"],
                                                       ["基本每股收益（元/股）", "3.0005", "2.0693"]], "") is True)

# ============================================================ ④ 库里真值（只读）
perf = data.query_all(
    "SELECT revenue, net_profit, net_profit_parent, eps, revenue_yoy, profit_yoy "
    "FROM `2_performance_report` WHERE stock_code=%s AND report_period=%s", (CODE, "2026年半年报"))
check("库里有这一行", len(perf) == 1, f"{perf}")
if perf:
    live = perf[0]
    check("eps = PDF 真值 0.9802", Decimal(str(live["eps"])) == Decimal("0.9802"), f"eps={live['eps']}")
    check("eps 不再是 p38 的 3.0005", Decimal(str(live["eps"])) != Decimal("3.0005"), f"eps={live['eps']}")
    check("净利润 = 合并口径 1,134,069,896.00",
          Decimal(str(live["net_profit"])) == Decimal("1134069896.0000"), f"net_profit={live['net_profit']}")
    check("归母净利润 ≠ 净利润（差额 436,311.32 = 少数股东损益）",
          Decimal(str(live["net_profit"])) != Decimal(str(live["net_profit_parent"]))
          and Decimal(str(live["net_profit"])) - Decimal(str(live["net_profit_parent"])) == Decimal("436311.3200"),
          f"{live['net_profit']} / {live['net_profit_parent']}")
    check("营业收入没被改坏", Decimal(str(live["revenue"])) == Decimal("6016260725.8900"), f"revenue={live['revenue']}")
    check("同比两列还在", live["revenue_yoy"] is not None and live["profit_yoy"] is not None, f"{live}")

income = data.query_all(
    "SELECT basic_eps, net_profit, net_profit_parent FROM `3_income_statement` "
    "WHERE stock_code=%s AND report_period=%s", (CODE, "2026年半年报"))
if income:
    check("利润表 basic_eps 同样归位 0.9802", Decimal(str(income[0]["basic_eps"])) == Decimal("0.9802"),
          f"basic_eps={income[0]['basic_eps']}")
else:
    check("利润表有这一行", False, "查不到 3_income_statement 的行")

# 全库不该再有「p38 值」串味的每股收益：实测 3.0005 只有 002595 这一家、两处
stray = data.query_all(
    "SELECT stock_code, report_period FROM `2_performance_report` WHERE eps=%s", ("3.0005",))
check("全库 eps 不再等于 3.0005", not stray, f"{stray}")

print()
if failures:
    print(f"===== 身份回填 / 同形降级 / 重算表闸门：{len(failures)} 项失败 =====")
    raise SystemExit(1)
print("===== 身份回填 / 同形降级 / 重算表闸门：全绿 =====")
