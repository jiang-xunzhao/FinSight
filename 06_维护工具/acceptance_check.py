"""FinSight 核心能力验收脚本。"""
from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIRS = (
    PROJECT_ROOT / "01_基础配置",
    PROJECT_ROOT / "02_向量存储",
    PROJECT_ROOT / "03_数据层",
    PROJECT_ROOT / "04_智能分析",
    PROJECT_ROOT / "05_Web服务",
)
for candidate in (PROJECT_ROOT, *MODULE_DIRS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import assistant
import data


def run_checks() -> dict:
    """运行可重复的核心验收检查。"""
    checks: dict[str, dict] = {}

    vector = data.load_vector_index_status()
    qdrant = vector.get("qdrant") or {}
    checks["vector_index"] = {
        "passed": bool(
            vector.get("ready")
            and qdrant.get("ready")
            and int(qdrant.get("count") or 0) == int(vector.get("total") or 0)
        ),
        "detail": vector,
    }

    primary = assistant.identify_company_from_question(
        "广西能源股份有限公司前10名股东持股情况表"
    )
    mentioned = assistant.identify_company_from_question(
        "招商证券股份有限公司的主要经营情况如何？"
    )
    checks["company_identity"] = {
        "passed": bool(
            primary
            and primary.get("primary")
            and primary.get("ticker") == "600310"
            and mentioned
            and not mentioned.get("primary")
        ),
        "detail": {"primary": primary, "mentioned": mentioned},
    }

    request = {
        "locked_company": {
            "ticker": "000801",
            "name": "四川九洲电器股份有限公司",
        },
        "locked_deal": {
            "deal_id": "000801:current_transaction",
            "label": "本次交易",
        },
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "api_key": "",
    }
    chunks = assistant.retrieve_rag_chunks(
        "四川九洲主要靠什么赚钱，有哪些核心业务",
        top_k=10,
        request=request,
    )
    tickers = {
        assistant.chunk_company_ticker(chunk)
        for chunk in chunks
    }
    checks["hybrid_retrieval"] = {
        "passed": bool(chunks) and tickers == {"000801"},
        "detail": {"count": len(chunks), "tickers": sorted(tickers)},
    }

    safe_select = assistant.validate_sql_safety(
        "SELECT report_period, revenue FROM 2_performance_report "
        "WHERE stock_code='000801'"
    )
    write_rejected = False
    try:
        assistant.validate_sql_safety("DELETE FROM 2_performance_report")
    except assistant.SQLValidationError:
        write_rejected = True
    checks["sql_security"] = {
        "passed": "LIMIT 200" in safe_select and write_rejected,
        "detail": {"safe_select": safe_select, "write_rejected": write_rejected},
    }

    comparison_question = "600007和600310的营业收入、净利润对比"
    comparison_companies = assistant.identify_comparison_companies(
        comparison_question
    )
    comparison_plan = {
        "action": "plan",
        "intent": "multi_company_comparison",
        "companies": comparison_companies,
        "subtasks": [
            {
                "id": f"sql_{item['ticker']}",
                "kind": "sql",
                "company": item,
                "depends_on": [],
                "sql": (
                    "SELECT stock_code, report_period, revenue, net_profit "
                    "FROM 2_performance_report "
                    f"WHERE stock_code='{item['ticker']}' "
                    "ORDER BY report_period DESC LIMIT 1"
                ),
                "expect_columns": [
                    "stock_code",
                    "report_period",
                    "revenue",
                    "net_profit",
                ],
            }
            for item in comparison_companies
        ],
    }
    plan_errors = assistant.validate_plan(
        comparison_plan,
        {"comparison_companies": comparison_companies},
    )
    checks["multi_company_planning"] = {
        "passed": len(comparison_companies) == 2 and not plan_errors,
        "detail": {
            "companies": comparison_companies,
            "errors": plan_errors,
        },
    }

    unified_rows, _, _ = assistant.unified_table_from_plan_results(
        {
            "results": [
                {
                    "status": "completed",
                    "company": comparison_companies[0],
                    "rows": [
                        {
                            "stock_code": comparison_companies[0]["ticker"],
                            "report_period": "2026H1",
                            "metric": "营业收入",
                            "value": 100,
                        }
                    ],
                },
                {
                    "status": "completed",
                    "company": comparison_companies[1],
                    "rows": [
                        {
                            "stock_code": comparison_companies[1]["ticker"],
                            "report_period": "2026H1",
                            "metric": "营业收入",
                            "value": 200,
                        }
                    ],
                },
            ],
            "warnings": [],
        }
    )
    checks["unified_comparison_table"] = {
        "passed": (
            len(unified_rows) == 2
            and all("营业收入" in row for row in unified_rows)
            and all(
                "stock_code" in row and "report_period" in row
                for row in unified_rows
            )
        ),
        "detail": unified_rows,
    }

    formatted_rows = data.localize_financial_table(
        [
            {
                "stock_code": "600008",
                "stock_name": "北京首创生态环保集团股份有限公司",
                "report_period": "2026年半年报",
                "营业收入": "83.59 亿元",
                "净利润": "1.21 亿元",
            },
            {
                "stock_code": "600098",
                "stock_name": "广州发展集团股份有限公司",
                "report_period": "2026年半年报",
                "营业收入": "254.87 亿元",
                "净利润": "5.35 亿元",
            },
        ]
    )
    checks["formatted_comparison_table"] = {
        "passed": (
            len(formatted_rows) == 2
            and formatted_rows[0].get("营业收入") == "83.59 亿元"
            and formatted_rows[1].get("净利润") == "5.35 亿元"
        ),
        "detail": formatted_rows,
    }

    legacy_session = data.normalize_chat_session(
        {
            "id": "legacy-comparison",
            "question": "对比北京首创和广州发展的营业收入和净利润",
            "result": {
                "answer": "统一口径比较如下。",
                "summary": "统一口径比较如下。",
                "table": [
                    {"净利润": "暂无数据", "营业收入": "暂无数据"},
                    {"净利润": "暂无数据", "营业收入": "暂无数据"},
                ],
                "raw_table": [
                    {
                        "stock_code": "600008",
                        "stock_name": "北京首创生态环保集团股份有限公司",
                        "report_period": "2026年半年报",
                        "营业收入": "83.59 亿元",
                        "净利润": "1.21 亿元",
                    },
                    {
                        "stock_code": "600098",
                        "stock_name": "广州发展集团股份有限公司",
                        "report_period": "2026年半年报",
                        "营业收入": "254.87 亿元",
                        "净利润": "5.35 亿元",
                    },
                ],
                "chart": None,
                "sources": [],
            },
        }
    )
    legacy_table = legacy_session["messages"][-1]["result"]["table"]
    checks["legacy_table_recovery"] = {
        "passed": (
            len(legacy_table) == 2
            and legacy_table[0].get("营业收入") == "83.59 亿元"
            and legacy_table[1].get("净利润") == "5.35 亿元"
        ),
        "detail": legacy_table,
    }

    correction_state = {"attempts": 0}

    def correction_execute(value):
        correction_state["attempts"] += 1
        if correction_state["attempts"] == 1:
            raise ValueError("模拟首次失败")
        return f"已修复：{value}"

    correction_result = assistant.run_with_correction(
        "初始值",
        correction_execute,
        lambda value, result: "",
        lambda value, error: "修复值",
        max_attempts=2,
    )
    checks["generic_self_correction"] = {
        "passed": bool(
            correction_result.get("ok")
            and correction_result.get("value") == "修复值"
            and correction_result.get("attempts") == 2
        ),
        "detail": correction_result,
    }

    table_cases = (
        ("广西能源股份有限公司", "600310"),
        ("重庆美心翼申机械股份有限公司", "920833"),
        ("四川九洲电器股份有限公司", "000801"),
    )
    table_results = []
    for company_name, ticker in table_cases:
        table_request = {
            "locked_company": {"ticker": ticker, "name": company_name},
            "locked_deal": {
                "deal_id": f"{ticker}:current_transaction",
                "label": "本次交易",
            },
            "embedding_model": "BAAI/bge-small-zh-v1.5",
            "api_key": "",
        }
        result = assistant.answer_with_local_rag(
            f"{company_name}前10名股东持股情况表",
            table_request,
        ) or {}
        rows = result.get("table") or []
        table_results.append(
            {
                "ticker": ticker,
                "rows": len(rows),
                "chart": (result.get("chart") or {}).get("type"),
            }
        )
    checks["shareholding_tables"] = {
        "passed": all(
            item["rows"] == 10 and item["chart"] == "bar"
            for item in table_results
        ),
        "detail": table_results,
    }

    stacked = {
        "type": "stackedBar",
        "x": "period",
        "y": ["revenue", "profit"],
        "data": [
            {"period": "2025H1", "revenue": 1, "profit": 2},
            {"period": "2026H1", "revenue": 3, "profit": 4},
        ],
    }
    checks["chart_validation"] = {
        "passed": assistant.validate_chart_points(stacked),
        "detail": stacked,
    }

    scope_result = assistant.enforce_company_scope(
        {
            "answer": "中国广核电力股份有限公司 003816",
            "summary": "",
            "key_points": [],
            "sources": [{"content": "四川九洲电器股份有限公司"}],
            "chart": None,
        },
        {
            "locked_company": {
                "ticker": "000801",
                "name": "四川九洲电器股份有限公司",
            }
        },
    )
    checks["answer_scope_guard"] = {
        "passed": scope_result.get("mode") == "guide",
        "detail": scope_result,
    }

    risk_result = assistant.answer_with_risk_analysis(
        "四川九洲电器股份有限公司有哪些主要经营风险？",
        request,
    ) or {}
    risk_answer = str(risk_result.get("answer") or "")
    checks["risk_analysis"] = {
        "passed": bool(
            risk_answer
            and "风险" in risk_answer
            and "接待调研" not in risk_answer
            and "企业所得税" not in risk_answer
        ),
        "detail": {
            "mode": risk_result.get("mode"),
            "answer": risk_answer[:500],
        },
    }

    evidence = assistant.build_evidence_chain(
        "四川九洲电器股份有限公司有哪些主要经营风险？",
        risk_result,
        request,
    )
    checks["evidence_chain"] = {
        "passed": bool(evidence),
        "detail": {
            "count": len(evidence),
            "items": evidence[:2],
        },
    }

    quality = data.get_data_quality_report()
    checks["data_quality_report"] = {
        "passed": quality.get("company_count", 0) > 0
        and quality.get("vector_index", {}).get("ready") is True,
        "detail": {
            "company_count": quality.get("company_count"),
            "report_coverage": quality.get("report_coverage"),
            "missing_files": quality.get("documents", {}).get("missing_files"),
        },
    }

    checks["overall_passed"] = {
        "passed": all(
            item.get("passed")
            for key, item in checks.items()
            if key != "overall_passed"
        )
    }
    return checks


if __name__ == "__main__":
    report = run_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["overall_passed"]["passed"] else 1)
