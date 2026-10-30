"""常驻件：模型自造的金额别名（net_profit_wan / revenue_yi）必须还能显示成金额。

溯源（2026-10-27 实测）：智能模式问「中国国贸2026年半年报净利润是多少万元？」，
模型写了 `ROUND(net_profit_parent/10000, 2) AS net_profit_wan`，展示层
data.drop_internal_columns 因为 `net_profit_wan` 没有中文标签而**整列丢弃**
⇒ 正文报了 63345.86 万元、表格却只剩 股票代码/公司名称/报告期。
修法两层：data.derived_amount_alias 认「已知字段 + 单位后缀」并按声明单位折算；
FINANCIAL_SCHEMA_PROMPT 同时禁止模型自己折算单位。
本件不调模型；别名/展示层是纯函数，排行两项只读 MySQL（不碰内嵌 Qdrant，服务在不在都能跑）。
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
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    for sub in ("01_基础配置", "02_向量存储", "03_数据层"):
        path = str(root / sub)
        if path not in sys.path:
            sys.path.insert(0, path)
    module = None
    for name in ("data", "assistant"):
        spec = importlib.util.spec_from_file_location(name, root / {"data": "03_数据层/data.py", "assistant": "04_智能分析/assistant.py"}[name])
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module

def main() -> int:
    data = boot()

    # 1) 别名解析
    check("net_profit_wan → net_profit ×1e4",
          data.derived_amount_alias("net_profit_wan") == ("net_profit", 10_000.0),
          str(data.derived_amount_alias("net_profit_wan")))
    check("revenue_yi → revenue ×1e8",
          data.derived_amount_alias("revenue_YI") == ("revenue", 100_000_000.0))
    check("net_profit_parent_wan 认归母口径",
          data.derived_amount_alias("net_profit_parent_wan") == ("net_profit_parent", 10_000.0))
    check("is_net_profit 不当别名（照旧丢弃）", data.derived_amount_alias("is_net_profit") is None)
    check("profit_yoy_wan 不折算成金额", data.derived_amount_alias("profit_yoy_wan") is None)
    check("total_assets_qian 认千元", data.derived_amount_alias("total_assets_qian") == ("total_assets", 1_000.0))

    # 2) 只有别名时，表格必须有金额列（23:30 那次丢列的原样输入）
    rows = data.localize_financial_table(
        [{"stock_code": "600007", "report_period": "2026年半年报", "net_profit_wan": "63345.86"}], "自动")
    check("丢列场景已修：净利润回来了", rows and rows[0].get("净利润") == "6.33 亿元", str(rows))

    # 3) 原始列 + 别名同时存在 ⇒ 原始列优先（23:41 的正常行为不能被别名覆盖）
    rows = data.localize_financial_table(
        [{"stock_code": "600007", "report_period": "2026年半年报",
          "net_profit": "633844810.0000", "net_profit_parent": "633458596.0000",
          "net_profit_wan": "63384.48", "net_profit_parent_wan": "63345.86"}], "自动")
    check("原始列优先：净利润 6.34 亿元", rows[0].get("净利润") == "6.34 亿元", str(rows))
    check("两个口径都还在且分得开", rows[0].get("归母净利润") == "6.33 亿元", str(rows))

    # 4) 认不出单位的键照旧整列丢弃（挡住 is_net_profit=81198372.22 那类）
    rows = data.localize_financial_table(
        [{"stock_code": "000801", "report_period": "2026年半年报", "is_net_profit": "81198372.2200"}], "自动")
    check("未知键仍被丢弃", "净利润" not in rows[0] and "is_net_profit" not in rows[0], str(rows))

    # 5) 提示词层必须禁止模型自己折算单位
    prompt = str(getattr(data, "FINANCIAL_SCHEMA_PROMPT", ""))
    check("FINANCIAL_SCHEMA_PROMPT 有写 SQL 的规则", "【写 SQL 的规则】" in prompt)
    check("规则禁止 ROUND 折算/自造别名",
          "不要自己 ROUND" in prompt and "_wan/_yi" in prompt)

    # 6) 两口径撞车降级（上一轮修的）不能被这次改动破坏
    rows = data.localize_financial_table(
        [{"stock_code": "002595", "report_period": "2026年半年报",
          "net_profit": "1134069896.0000", "net_profit_parent": "1133633584.6800"}], "自动")
    check("豪迈两口径仍分得开",
          rows[0].get("净利润") == "113,406.99 万元" and rows[0].get("归母净利润") == "113,363.36 万元",
          str(rows))

    # 7) 功能守卫：无公司主体的排行问法 + 本地模式不再索要云端密钥
    assistant = __import__("assistant")
    check("全库排行件已存在", callable(getattr(assistant, "answer_cross_company_ranking", None)))
    check("报告期折算对（半年报）", assistant.rank_report_period("2026年半年报里净利润最高的前5家") == "2026年半年报")
    check("报告期折算对（年报）", assistant.rank_report_period("2025年报营业收入排名") == "2025年年报")
    ranked = assistant.answer_cross_company_ranking("2026年半年报里净利润最高的前5家公司是哪些？", "自动")
    check("排行题真出 5 行", bool(ranked) and len(ranked["table"]) == 5, str(ranked and ranked["table"]))
    check("排行答案说明范围仅限已入库", bool(ranked) and "已入库公司" in ranked["answer"])
    check("有公司主体时不抢答排行",
          assistant.answer_cross_company_ranking("豪迈科技净利润最高的一年", "自动") is None)
    help_text = pathlib.Path(str(data.__file__)).resolve().parent.parent.joinpath("04_智能分析", "assistant.py").read_text(encoding="utf-8")
    check("本地兜底文案不再提云端密钥", "请先填写云端 API 密钥" not in help_text)
    check("本地兜底文案给了可照抄的问法", "豪迈科技 2026年半年报 营业收入" in help_text)

    print(f"通过 {PASSED} 项")
    return 0

if __name__ == "__main__":
    sys.exit(main())
