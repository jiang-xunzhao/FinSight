"""回归件：百分比单元格与分部口径不许当金额（002195 岩山科技 那类垃圾值的根因）。

跑法：python test_percent_and_scope_guards.py [--with-doc]
  --with-doc 额外用真文档复算一遍（要读 PDF，慢几十秒），断言 2025 年报营业收入。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import data  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


# 1) 表格路径：带 % 的单元格不进数值列
label, values = data.parse_label_and_values(["营业收入（元）", "342,048,970.79", "316,809,770.98", "7.97%"])
check("表格：百分比列被排除", [v for _, v in values], ["342,048,970.79", "316,809,770.98"])
check("表格：科目名", label, "营业收入（元）")

# 2) 占比行：全是百分比 ⇒ 没有数值候选
label2, values2 = data.parse_label_and_values(["营业收入", "100.00%", "100.00%"])
check("表格：整行都是百分比", values2, [])

# 3) 正文补充：分部口径不许当合并数
probe_values: dict[str, object] = {}
probe_prev: dict[str, object] = {}
data.extract_values_from_text(
    "\n".join(
        [
            "2026 年上半年，公司人工智能业务营业收入为 771,614.38 元，较 2025 年同期减少 91.90%。",
            "占公司营业收入或营业利润 10%以上的行业、产品或地区情况：不适用",
            "营业收入（元） 342,048,970.79 316,809,770.98 7.97%",
        ]
    ),
    probe_values,  # type: ignore[arg-type]
    probe_prev,  # type: ignore[arg-type]
)
revenue = probe_values.get("operating_revenue")
check(
    "正文：只认表格样那行、且忽略 7.97%",
    str(getattr(revenue, "current", None)),
    "342048970.79",
)
check(
    "正文：上年同期取到 316,809,770.98",
    str(probe_prev.get("operating_revenue")),
    "316809770.98",
)

# 4) 分部口径单独出现时不许造出营业收入
empty_v: dict[str, object] = {}
empty_p: dict[str, object] = {}
data.extract_values_from_text("2025 年互联网业务营业收入为 611,874,584.96 元，占公司营业收入总额的 93.68%。", empty_v, empty_p)  # type: ignore[arg-type]
check("正文：分部口径被拦", "operating_revenue" in empty_v, False)

# 6) 科目路由：归母与净利润分列，扣非行不许冒充任何一列
check("路由：摘要表归母行 → parent_net_profit",
      data.match_metric("归属于上市公司股东的净利润（元） 1,133,633,584.68 1,196,828,824.50 -5.28%"),
      "parent_net_profit")
check("路由：利润表「四、净利润」→ net_profit",
      data.match_metric("四、净利润（净亏损以“—”号填列） 977,530,758.06 1,032,894,086.64"),
      "net_profit")
check("路由：扣非行不入库（库里没有这一列）",
      data.match_metric("扣除非经常性损益后的归属于上市公司股东的净利润 1,001,000,000.00"),
      None)
check("路由：营业收入不被营业总收入抢",
      data.match_metric("营业收入（元） 6,016,260,725.89 5,265,115,116.12 14.27%"),
      "operating_revenue")

# 5) 可选：真文档复算（慢）
if "--with-doc" in sys.argv:
    import pymysql  # noqa: E402

    cfg = dict(data.db_config())
    cfg["cursorclass"] = pymysql.cursors.DictCursor
    conn = pymysql.connect(**cfg)
    cur = conn.cursor()
    cur.execute(
        "SELECT file_path FROM `7_document_index` WHERE stock_code='002195' AND report_period='2025年年报'"
    )
    path = Path(cur.fetchone()["file_path"])
    pages, _tables = data.read_pdf(path)
    text = "\n".join(str(p.get("text") or "") for p in pages)
    vals, _prev = data.extract_values(pages, text)
    data.extract_values_from_text(text, vals, _prev)
    check(
        "真文档：002195 2025年报 营业收入不再是 100.00",
        str(vals["operating_revenue"].current),
        "653194311.02",
    )

# 7) 报表口径：母公司表的行绝不能当成本公司（合并）指标；附注/合并表标题要把口径切回来。
# 实测反面教材：600208 存 net_profit=382,273,538.06 是 p40 母公司利润表 `四、净利润` 的数，
# 合并真值在 p38 `五、净利润… 32,234,254.86`；600325 存的也是母公司表的数。
scope_text = """合并利润表
一、营业收入 100,000,000.00 90,000,000.00
母公司利润表
四、净利润（净亏损以“—”号填列） 382,273,538.06 219,623,000.00
财务报表附注
五、净利润（净亏损以“—”号填列） 32,234,254.86 164,087,683.70
"""
scope_v: dict = {}
scope_p: dict = {}
data.extract_values_from_text(scope_text, scope_v, scope_p)  # type: ignore[arg-type]
check("口径：母公司表的净利润不入库",
      any(str(item.current) == "382273538.06" for item in scope_v.values()), False)
check("口径：附注切回合并口径后取到净利润",
      str(scope_v["net_profit"].current) if "net_profit" in scope_v else None, "32234254.86")
check("口径：合并利润表的营业收入照旧",
      str(scope_v["operating_revenue"].current) if "operating_revenue" in scope_v else None,
      "100000000.00")

# 8) 单位护栏必须**双向**判：附注的「单位：万元」盖到元表行会把数放大 1e4。
# 实测 600008 首创环保：净利润 1,168,715,925.13 被乘成 11,687,159,251,300，
# 归母 839,444,514.59 —— 旧写法只判「比归母小 100 倍」，放大了 13922 倍反而放过。
def _mk(code: str, value: str) -> data.StatementValue:
    return data.StatementValue(
        metric_code=code,
        metric_name=data.METRICS[code][0],
        statement_type=data.METRICS[code][1],
        current=data.Decimal(value),
        previous=None,
        current_raw=value,
        previous_raw=None,
        page_no=0,
        row_index=0,
        confidence=data.Decimal("0.65"),
    )

big_v = {
    "parent_net_profit": _mk("parent_net_profit", "839444514.59"),
    "net_profit": _mk("net_profit", "11687159251300"),
    "total_profit": _mk("total_profit", "11687159251300"),
}
check("单位护栏：放大约 1e4 的利润列也被丢弃",
      sorted(data.drop_unit_mismatched_profits(big_v)), ["net_profit", "total_profit"])
small_v = {
    "parent_net_profit": _mk("parent_net_profit", "3808272967"),
    "net_profit": _mk("net_profit", "2243266"),
}
check("单位护栏：缩小约 1e3 的利润列照旧丢弃",
      sorted(data.drop_unit_mismatched_profits(small_v)), ["net_profit"])
ok_v = {
    "parent_net_profit": _mk("parent_net_profit", "839444514.59"),
    "net_profit": _mk("net_profit", "1168715925.13"),
}
check("单位护栏：同量级不误伤", data.drop_unit_mismatched_profits(ok_v), [])

# 9) 折行科目接续不许接给「别的行项目」：实测 002195 岩山科技 2024 年报
# `五、净利润` 行没有数字，下一行 `六、综合收益总额 402,632,550.37` 被接成净利润
# （库里 net 402,632,550.37 vs 归母 32,461,729.28 差 12.4 倍）。
text = """五、净利润（净亏损以“-”号填列）
六、综合收益总额 402,632,550.37 358,280,826.90
归属于母公司所有者的净利润 32,461,729.28 28,000,000.00
"""
guard_values: dict = {}
data.extract_values_from_text(text, guard_values, {})
check(
    "接续：综合收益总额没有被当成本年净利润",
    "net_profit" in guard_values,
    False,
)
check(
    "接续：归母自己的数字照常入库",
    str(guard_values["parent_net_profit"].current)
    if "parent_net_profit" in guard_values
    else None,
    "32461729.28",
)

print("\n" + ("✅ 百分比/口径护栏符合预期" if not failures else f"❌ 未通过：{failures}"))
sys.exit(1 if failures else 0)
