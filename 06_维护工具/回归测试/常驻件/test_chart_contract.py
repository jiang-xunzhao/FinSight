"""钉住轮廿二图表改造的三条实测结论（都是当时真出过问题的地方）。

① 排行榜的图曾画成「排名 1、2、3，单位元」的假图（实测 2026-10-29，问
   「2026年半年报里净利润最高的前5家，做个柱状图」返回
   `x=股票代码 y=排名 series=排名|元|1.0`）。根因：`build_chart_from_rows` 在自动挑
   数值列时把排行表自带的「排名」行号列当成财务数值，真正的金额列（格式化文本）
   反而没被选中。⇒ 修法：`CHART_SKIP_COLUMNS_RE` 在挑 y / 挑 x 时都排除序号列；
   `answer_cross_company_ranking` 直接产出原始数值的 `ranking_chart`（x=公司名称、
   y=中文指标名），不再依赖自动挑列。

② 图表契约升级：`attach_chart_series_meta` 产出 `series:[{key,label,unit,scale,axis,rate}]`。
   单位判据与 `localize_financial_table` 内部的 `column_unit` 一致，但**只报单位不折算数值**
   （`scale=1e8` 给前端除）——否则后端除一次、前端 `chartNumber` 再除一次，
   营收 218.84 亿会显示成 2.19 亿。`x/y/data` 一律不动 ⇒ `validate_chart_points` 零回归。
   顺序必须是**先按英文字段名定单位、再本地化列名**（`localize_financial_chart_with_meta`）。

③ 跨公司对比的长表（`x=指标、y=通用「数值」列、另有一列公司名`）以前所有公司挤进
   同一条系列 ⇒ 图例只显示「数值」，看不出哪条是谁（用户 m05092 要求改）。
   ⇒ `pivot_long_chart` 按主体列拆成「一家一个系列」，x 换成指标列；
   **主体只有一个时绝不拆**（单公司趋势图不能被搅乱）。
"""
from __future__ import annotations

import sys
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

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def series_of(chart):
    return {str(item.get("key")): item for item in (chart.get("series") or [])}


# ── ① 序号列不能被当数值 ────────────────────────────────────────────────
print("[1] 排行榜：序号列排除 + 原始数值")
skip = assistant.CHART_SKIP_COLUMNS_RE
check("独立序号列命中排除",
      all(skip.search(k) for k in ("排名", "名次", "序号", "位次", "编号", "rank", "No.", "idx")))
check("复合序号列也命中（rank 前缀类）",
      all(skip.search(k) for k in ("company_rank", "rank_no", "global_idx", "序号", "net_profit_rank")))
check("带前缀/后缀的序号列命中，但普通字段不误伤",
      all(skip.search(k) for k in ("公司排名", "排名列", "公司名称序号", "排名"))
      and not any(skip.search(k) for k in ("股票代码", "证券代码", "指标数值", "行业名称")))
check("财务指标列不误伤",
      not any(skip.search(k) for k in ("营业收入", "净利润", "净利润同比", "净资产收益率",
                                      "排名靠前的公司数", "股数", "股票代码", "2026年半年报")))


def picked_y(rows, keys=None):
    """复刻 build_chart_from_rows 的挑列判据（不连库、不依赖真实数据）。"""
    keys = keys or list(rows[0].keys())
    scored = []
    for key in keys:
        if key in assistant.CHART_META_COLUMNS or skip.search(str(key)):
            continue
        count = sum(1 for row in rows if assistant.chart_number(row.get(key)) is not None)
        if count >= 2:
            scored.append((count, key))
    return max(scored, key=lambda item: item[0])[1] if scored else ""


RANK_ROWS = [
    {"排名": 1, "股票代码": "600150", "公司名称": "中国船舶", "报告期": "2026年半年报", "净利润": "106.69亿元"},
    {"排名": 2, "股票代码": "601633", "公司名称": "长城汽车", "报告期": "2026年半年报", "净利润": "57.24亿元"},
    {"排名": 3, "股票代码": "000538", "公司名称": "云南白药", "报告期": "2026年半年报", "净利润": "36.97亿元"},
]
check("排名列不参与选列（真库里排行表就长这样）", picked_y(RANK_ROWS) != "排名",
      f"picked={picked_y(RANK_ROWS)}")
check("只有排名一列是数时 ⇒ 宁可不画也不画假图",
      assistant.build_chart_from_rows("净利润排行", RANK_ROWS, title="净利润排行") is None,
      "实测旧行为会画 x=股票代码 y=排名 的假图")
NUMERIC_ROWS = [
    {"排名": 1, "公司名称": "A", "净利润": 106.69, "营业收入": 780.0},
    {"排名": 2, "公司名称": "B", "净利润": 57.24, "营业收入": 740.0},
    {"排名": 3, "公司名称": "C", "净利润": 36.97, "营业收入": 218.0},
]
check("数值行也优先选指标列而不是排名", picked_y(NUMERIC_ROWS) != "排名",
      f"picked={picked_y(NUMERIC_ROWS)}")
check("排名列不当 x 轴",
      "排名" not in str(assistant.build_chart_from_rows("排行", NUMERIC_ROWS) or {}), "")

one = data.localize_financial_chart_with_meta(
    {"type": "bar", "x": "股票代码", "y": "net_profit",
     "data": [{"股票代码": "600150", "net_profit": 10668802707.38},
              {"股票代码": "000538", "net_profit": 3697000000.0}]})
meta = series_of(one)
check("亿元单位带 scale=1e8 且不折算数值",
      meta.get("净利润", {}).get("unit") == "亿元"
      and meta.get("净利润", {}).get("scale") == 1e8
      and one["data"][0]["净利润"] == 10668802707.38,
      f"series={one.get('series')}")
check("比率列判成 % 且 scale=1",
      data.chart_series_unit("profit_yoy", [-98.4641, 3.2]) == ("%", 1.0))
check("万元档",
      data.chart_series_unit("管理费用", [123456.0, 98765.0])[0] == "万元")

# ── ③ 长表拆系列 ────────────────────────────────────────────────────────
print("[3] 长表按主体拆系列")
long_chart = {
    "type": "bar", "title": "对比", "x": "指标", "y": "数值", "unit": "亿元",
    "data": [
        {"指标": "营业收入", "数值": "21884075971.15", "公司名称": "云南白药"},
        {"指标": "净利润", "数值": "3697000000.0", "公司名称": "云南白药"},
        {"指标": "营业收入", "数值": "410000000000.0", "公司名称": "美的集团"},
        {"指标": "净利润", "数值": "38000000000.0", "公司名称": "美的集团"},
    ],
}
pivoted = data.localize_financial_chart_with_meta(long_chart)
check("y 变成公司名（一主体一系列）",
      pivoted["y"] == ["云南白药", "美的集团"], f"y={pivoted['y']}")
check("x 换成指标列", pivoted["x"] == "指标", f"x={pivoted['x']}")
check("每行同时有两家公司的值",
      set(pivoted["data"][0]) == {"指标", "云南白药", "美的集团"}, f"row={pivoted['data'][0]}")
check("系列标签不再是通用「数值」",
      all(str(item["label"]) != "数值" for item in pivoted.get("series") or [])
      and len(pivoted.get("series") or []) == 2, f"series={pivoted.get('series')}")
check("长表的通用单位不沿用", pivoted.get("unit") in ("亿元", ""), f"unit={pivoted.get('unit')}")

single = data.localize_financial_chart_with_meta(
    {"type": "line", "x": "报告期", "y": "数值",
     "data": [{"报告期": "2025年年报", "数值": 1.0, "公司名称": "单家"},
              {"报告期": "2026年半年报", "数值": 2.0, "公司名称": "单家"}]})
check("主体只有一个 ⇒ 绝不拆",
      single["y"] == "数值" and single["x"] == "报告期", f"x={single['x']} y={single['y']}")
wide = data.localize_financial_chart_with_meta(
    {"type": "bar", "x": "公司名称", "y": ["营业收入", "净利润"],
     "data": [{"公司名称": "A", "营业收入": 1.0, "净利润": 2.0},
              {"公司名称": "B", "营业收入": 3.0, "净利润": 4.0}]})
check("宽表（本来就有两个指标列）不受影响",
      wide["x"] == "公司名称" and wide["y"] == ["营业收入", "净利润"], f"{wide['x']}/{wide['y']}")
check("pivot 对非法输入不炸",
      data.pivot_long_chart(None) is None and data.pivot_long_chart({"type": "bar"}) == {"type": "bar"})

# ── ④ 对比图：一指标一系列，横轴是公司 ──────────────────────────────────
print("[4] 对比图拆系列（build_comparison_chart）")
CMP_ROWS = [
    {"公司名称": "云南白药", "revenue": 21884075971.15, "net_profit": 3697247984.17, "debt_ratio": 25.5},
    {"公司名称": "长城汽车", "revenue": 64423456789.0, "net_profit": 5724000000.0, "debt_ratio": 60.2},
]
cmp_chart = assistant.build_comparison_chart(CMP_ROWS, {"type": "bar"}, "对比营业收入和净利润")
check("横轴是公司", cmp_chart and cmp_chart["x"] == "公司名称", f"x={cmp_chart and cmp_chart.get('x')}")
check("多个指标都成系列（不再只挑一列）",
      bool(cmp_chart) and isinstance(cmp_chart["y"], list) and len(cmp_chart["y"]) >= 2,
      f"y={cmp_chart and cmp_chart.get('y')}")
cmp_meta = data.localize_financial_chart_with_meta(cmp_chart)
meta_map = series_of(cmp_meta)
check("英文字段名被本地化成中文图例",
      "营业收入" in meta_map and "净利润" in meta_map, f"series={list(meta_map)}")
check("比率列挂到右轴且单位是 %",
      meta_map.get("资产负债率", {}).get("axis") == "y1"
      and meta_map.get("资产负债率", {}).get("unit") == "%",
      f"debt={meta_map.get('资产负债率')}")
check("金额列单位亿元、scale=1e8",
      meta_map.get("营业收入", {}).get("unit") == "亿元"
      and meta_map.get("营业收入", {}).get("scale") == 1e8, f"{meta_map.get('营业收入')}")
no_company = [{"股票代码": "000538", "营业收入": 21884075971.15},
              {"股票代码": "601633", "营业收入": 64423456789.0}]
check("有股票代码列时也能出图（横轴用代码）",
      bool(assistant.build_comparison_chart(no_company, {"type": "bar"}, "对比")), "")
check("行里没有公司标识 ⇒ 返回 None 不硬画",
      assistant.build_comparison_chart(
          [{"指标": "营业收入", "数值": 1.0}, {"指标": "净利润", "数值": 2.0}],
          {"type": "bar"}, "对比") is None, "")
check("格式化文本不会被当成数值系列",
      assistant.build_comparison_chart(
          [{"公司名称": "A", "报告期": "2026年半年报", "营业收入": "218.84 亿元"},
           {"公司名称": "B", "报告期": "2026年半年报", "营业收入": "644.23 亿元"}],
          {"type": "bar"}, "对比") is not None, "至少能解析出数值（chart_number 认带单位文本）")

# ── ② 前端资产：确定性文案必须在源码里（防被回退） ──────────────────────
print("[2] 前端外壳与柱顶数值")
web_text = (ROOT / "05_Web服务" / "web.py").read_text(encoding="utf-8")
for needle in ("function chartCardHtml", "function hydrateChartShell",
               'id: "finsightBarValues"', "function renderGlassTooltip",
               "devicePixelRatio", "function chartTipElement"):
    check(f"web.py 含 {needle}", needle in web_text)
check("画布不再写死 width=1200", 'class="chart-canvas" width="1200"' not in web_text)
check("tooltip 走 external 回调", "external: renderGlassTooltip" in web_text)
check("SQL 右栏收窄到 44px", "--rail-w: 44px" in web_text)

# ── 真库排行（连得上才测；接口自己开连接，这里不猜内部方法名） ──────────
print("[5] 真库排行（连得上才测）")
try:
    result = assistant.answer_cross_company_ranking("2026年半年报净利润最高的前3家")
except Exception as exc:
    print(f"  skip 排行接口不可用：{type(exc).__name__}: {str(exc)[:70]}")
    result = None
if result and result.get("chart"):
    rc = result["chart"]
    check("排行接口产出图表", bool(rc.get("data")), f"chart keys={list(rc)[:6]}")
    check("排行图表 x=公司名称", str(rc.get("x")) == "公司名称", f"x={rc.get('x')}")
    check("排行图表 y 是指标名且不是「排名」",
          str(rc.get("y") or "") not in ("排名", "None", ""), f"y={rc.get('y')}")
    check("排行图表数值是原始数（交给前端按 scale 折算）",
          all(isinstance(row.get(str(rc.get("y"))), (int, float)) for row in rc.get("data") or []),
          f"data0={(rc.get('data') or [{}])[0]}")
    check("排行图表带 series 元数据",
          bool(rc.get("series")) and all(item.get("unit") for item in rc["series"]),
          f"series={rc.get('series')}")
elif result:
    print("  skip 库里没有可排的数据（chart=None）")

print()
print("[6] 缺数据 / 单位混着 的出图闸门")
check("缺数据的对比题 → 直接说数据不足，不降级成单公司图",
      (lambda: (lambda out: bool(out) and "数据不足" in str(out.get("answer")) and out.get("chart") is None
                and out.get("mode") == "guide")(
          assistant.answer_missing_comparison_targets("对比 000538 和 000002 2026年半年报 营业收入 净利润",
                                                      [{"ticker": "000538", "name": "云南白药"}])))(),
      "000002 未入库时应回「数据不足：000002 还没有入库…」")
check("两家都在库里 → 不该拦（交回正常链路）",
      assistant.answer_missing_comparison_targets("对比 000538 和 600007 2026年半年报 营业收入",
                                                  [{"ticker": "000538", "name": "云南白药"},
                                                   {"ticker": "600007", "name": "某公司"}]) is None,
      "resolved 有 2 家且无缺失 → return None")
check("只点名一家 → 不该拦（单公司题正常出数）",
      assistant.answer_missing_comparison_targets("000002 2026年半年报 净利润", []) is None,
      "只一家时不能把「另一家没给」当缺失")
check("年份粘在代码上（20250630/20260630）→ 不当成第二家点名",
      assistant.answer_missing_comparison_targets("对比 20250630 和 20260630 的数值", []) is None,
      "纯 8 位日期串不能进 missing")
LONG_MIXED = {"type": "bar", "x": "指标", "y": "数值",
              "data": [{"指标": "营业收入", "数值": 21884000000.0},
                       {"指标": "净利润", "数值": 3697000000.0},
                       {"指标": "资产负债率", "数值": 25.56}]}
check("混合单位长表：单位闸门直接不画（金额和百分比不能挤同一根柱子）",
      data.localize_financial_chart_with_meta(dict(LONG_MIXED)) is None,
      "必须 return None，否则线上又会出现 25 和 218 亿同高的柱")
PURE_AMOUNT = {"type": "bar", "x": "指标", "y": "数值",
               "data": [{"指标": "营业收入", "数值": 21884000000.0},
                        {"指标": "净利润", "数值": 3697000000.0}]}
_ok = data.localize_financial_chart_with_meta(dict(PURE_AMOUNT))
check("纯金额多指标照旧出图（别把好的也拦掉）",
      bool(_ok) and isinstance(_ok, dict) and _ok.get("unit") == "亿元",
      f"unit={(_ok or {}).get('unit')}")
WIDE_MIX = {"type": "bar", "x": "公司名称",
            "y": ["营业收入", "资产负债率"],
            "data": [{"公司名称": "甲公司", "营业收入": 21884000000.0, "资产负债率": 25.56},
                     {"公司名称": "乙公司", "营业收入": 12000000000.0, "资产负债率": 40.1}]}
_wide = data.localize_financial_chart_with_meta(dict(WIDE_MIX))
check("宽表金额+比率：走左右双轴，仍然出图（闸门不能一刀切）",
      bool(_wide) and any(s.get("axis") == "y1" for s in (_wide or {}).get("series") or []),
      f"series={(_wide or {}).get('series')}")
check("闸门拦下图表时留下原因，出口读它写进 chart_note（文案在 data.py、消费在 assistant.py）",
      "这些指标单位不一样" in (ROOT / "03_数据层" / "data.py").read_text(encoding="utf-8")
      and "gate_note" in (ROOT / "04_智能分析" / "assistant.py").read_text(encoding="utf-8"),
      "没图必须有解释，否则用户以为程序坏了")
WIDE_IDENTITY = {"type": "bar", "x": "公司名称", "y": "数值",
                 "data": [{"公司名称": "甲公司", "股票代码": "000538", "报告期": "2026年半年报",
                           "营业收入": 21884000000.0, "净利润": 3697000000.0},
                          {"公司名称": "乙公司", "股票代码": "600061", "报告期": "2026年半年报",
                           "营业收入": 7974000000.0, "净利润": 2156000000.0}]}
_ident = data.localize_financial_chart_with_meta(dict(WIDE_IDENTITY))
check("股票代码/报告期不许当指标画（000538 会被读成数字 538）",
      bool(_ident) and not any(
          str(item.get("label")) in ("股票代码", "报告期", "股票代码（万元）")
          for item in _ident.get("series") or []),
      f"series={(_ident or {}).get('series')}")
WIDE_MANY = {"type": "bar", "x": "公司名称",
             "y": [f"指标{i}" for i in range(8)],
             "data": [{**{"公司名称": f"公司{row}"} , **{f"指标{i}": row * (i + 1) * 1e8
                                                         for i in range(8)}} for row in (1, 2)]}
_many = data.localize_financial_chart_with_meta(WIDE_MANY)
check("系列多到挤成一团（>6）⇒ 拦下并留下原因",
      _many is None and bool(WIDE_MANY.get("gate_note")),
      f"gate_note={WIDE_MANY.get('gate_note')!r} 实际={type(_many).__name__}")

print()
print("[7] 名字型对比不再被越界保护删掉")
check("600007 这种「代码后面紧跟年份」必须认得出第二家",
      len(assistant.identify_comparison_companies(
          "对比 000538 和 600007 2026年半年报 营业收入 净利润 资产负债率")) >= 2,
      "年份粘在代码上时旧正则会把 600007 判成非独立代码")
check("问题里点了两家（第二家不在库、纯名字）→ 不许走单公司越界删除",
      assistant.question_names_multiple_companies(
          "对比云南白药和长城汽车的2026年半年报营业收入") is True,
      "误判的代价只是少删一份答案，漏判会删掉正确结果")
check("单公司题不许误判成两家",
      assistant.question_names_multiple_companies(
          "云南白药2026年半年报的营业收入是多少") is False,
      "否则跨公司越界保护就形同虚设")

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: " + " | ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
