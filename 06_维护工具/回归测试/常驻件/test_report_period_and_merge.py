"""常驻件：本地模式必须尊重题目点名的报告期；长表列名要能被合并（不再报「未知公司」）。

溯源一（2026-10-28 实测）：`data.answer_with_structured_data` 只做 `latest = series[0]`，
**完全不看题目里的年份/年报/半年报**，而 `data.query_metric_series` 是
`... ORDER BY report_period DESC LIMIT 8` ⇒ 用户问「四川九洲2024年年报的净利润」
照样回 **2026年半年报**（答错期间还不说）；问「广州发展最近三年年报」也只回 1 行。
修法：`report_period_hint` + `period_kind` + `filter_series_by_period`，
点名期间库里没有就如实说「没有你要的那一期；库里已有的报告期：…」。

溯源二（2026-10-28 实测）：问「豪迈科技的主营业务是什么？」，答案对但 warnings 是
`['未知公司 2026年半年报 字段 指标 存在多个值…', '未知公司 2026年半年报 字段 本期数值 …']`。
`localize_financial_table` 的长表实际列名是 报告期/指标/**本期数值**/变化说明，
而 `assistant.unified_table_from_plan_results` 的 `value_keys` 只有 value/数值/metric_value
⇒ 长表行走不进合并分支，落到按列合并兜底，把每行的「指标」当成同一字段的多值；
长表又不带身份列 ⇒ group_key 退化成 ('', 期间) ⇒ 公司名写成「未知公司」。
本件不调模型；两条路径都是纯函数 + 只读 MySQL（服务在不在都能跑）。
"""
from __future__ import annotations

import io
import pathlib
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        print(f"[FAIL] {label} {detail}")
        sys.exit(1)
    PASSED += 1
    print(f"[ok]   {label}")


def boot():
    import importlib.util

    root = pathlib.Path(__file__).resolve().parents[3]
    for sub in ("01_基础配置", "02_向量存储", "03_数据层"):
        path = str(root / sub)
        if path not in sys.path:
            sys.path.insert(0, path)
    modules = {}
    for name, rel in (("data", "03_数据层/data.py"), ("assistant", "04_智能分析/assistant.py")):
        spec = importlib.util.spec_from_file_location(name, root / rel)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules["data"], modules["assistant"]


def main() -> None:
    data, assistant = boot()

    # ① 报告期意图解析（「年报」是「半年报」的子串，必须先判半年报 ⇒ 用否定后视）
    hint = data.report_period_hint("广州发展最近三年年报的营业收入")
    check("近三年年报：口径=年报、跨度限 3",
          hint["kinds"] == {"年报"} and hint["span"] and hint["limit"] == 3 and not hint["years"], str(hint))
    hint = data.report_period_hint("四川九洲2024年年报的净利润是多少？")
    check("点名 2024 年报", hint["years"] == [2024] and hint["kinds"] == {"年报"}, str(hint))
    hint = data.report_period_hint("豪迈科技2026年半年报的营业收入和净利润")
    check("2026年半年报**不**算要年报", hint["kinds"] == {"半年报"} and hint["years"] == [2026], str(hint))
    check("没点名期间就不算 explicit", data.report_period_hint("002595 净利润")["explicit"] is False)

    # ② 序列筛选：三种库内写法都要归对类
    series = [{"report_period": "2026年半年报", "value": 1},
              {"report_period": "2025年年报", "value": 2},
              {"report_period": "2024年年报", "value": 3}]
    check("period_kind 归类", data.period_kind("2026年半年报") == "半年报"
          and data.period_kind("2025年年报") == "年报" and data.period_kind("") == "")
    got = [r["report_period"] for r in data.filter_series_by_period(series, data.report_period_hint("最近三年年报"))]
    check("年报跨度筛掉半年报", got == ["2025年年报", "2024年年报"], str(got))
    got = [r["report_period"] for r in data.filter_series_by_period(series, data.report_period_hint("2024年年报"))]
    check("点名 2024 年年报只剩那一期", got == ["2024年年报"], str(got))
    got = [r["report_period"] for r in data.filter_series_by_period(series, data.report_period_hint("豪迈科技2026年半年报的净利润"))]
    check("点名半年报不误取年报", got == ["2026年半年报"], str(got))
    got = [r["report_period"] for r in data.filter_series_by_period(series, data.report_period_hint("净利润最高的前5家"))]
    check("没点名期间时原样返回", got == [r["report_period"] for r in series], str(got))

    # ③ 端到端（只读库）：点名的期间库里没有 ⇒ 如实说，不许回最新一期
    result = data.answer_with_structured_data("四川九洲1999年年报的净利润是多少？", "自动")
    check("点名未入库期间 ⇒ 有结构化应答", bool(result), str(result)[:80])
    if result:
        text = str(result.get("answer") or "")
        check("不再偷答最新一期", "1,198" not in text and "9,630.91" not in text, text[:90])
        # 库是你随时在删/导的：整家公司都不在里面时正确话术是「还没有任何已入库的财务数据」，
        # 「没有你要的那一期 + 列出已有期间」要等它入库才谈得上。
        if "还没有任何已入库" in text:
            print("  SKIP 四川九洲整家未入库 ⇒「没有你要的那一期」两条不检查")
        else:
            check("如实说没有那一期并列出已有期间", "没有你要的那一期" in text and "库里已有的报告期" in text, text[:120])
            check("警告里写明缺的是哪个指标", any("未入库" in str(w) for w in result.get("warnings") or []),
                  str(result.get("warnings")))
    result = data.answer_with_structured_data("豪迈科技2026年半年报的净利润", "自动")
    check("点名已入库期间仍正常出数", bool(result) and "11.34" in str(result.get("answer") or ""),
          str(result.get("answer"))[:80] if result else "None")

    # ④ 长表合并：本期数值 必须算数值列，否则按列合并兜底会造假冲突
    source = pathlib.Path(str(assistant.__file__)).read_text(encoding="utf-8")
    check("value_keys 含 本期数值", 'value_keys = {"value", "数值", "metric_value", "本期数值"}' in source)
    execution = {"results": [
        {"status": "completed", "company": {}, "sources": [], "rows": [
            {"报告期": "2026年半年报", "指标": "营业收入", "本期数值": "60.16 亿元", "变化说明": ""},
            {"报告期": "2026年半年报", "指标": "净利润", "本期数值": "11.34 亿元", "变化说明": ""}]},
    ], "warnings": []}
    merged_rows, _merged_sources, merged_warnings = assistant.unified_table_from_plan_results(execution)
    check("长表两行合并成一行两字段",
          len(merged_rows) == 1 and str(merged_rows[0].get("营业收入") or "") == "60.16 亿元"
          and str(merged_rows[0].get("净利润") or "") == "11.34 亿元", str(merged_rows)[:200])
    check("长表合并不再吐「未知公司」假告警",
          not any("未知公司" in str(w) for w in merged_warnings), str(merged_warnings))

    print(f"通过 {PASSED} 项")


if __name__ == "__main__":
    main()
