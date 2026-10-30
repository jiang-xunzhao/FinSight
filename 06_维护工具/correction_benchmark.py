"""SQL 纠错与降级基准评测。

用法：
    python 06_维护工具/correction_benchmark.py

脚本通过可控 SQL 样本触发空结果、错误字段等情况，输出纠错成功率、
修复成功数、检出异常数、修复样本和降级样本。默认使用本地修复规则，
不需要调用云端模型。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


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

import assistant  # noqa: E402


CASES: list[dict[str, Any]] = [
    {
        "id": "SQL-FIX-001",
        "scenario": "INNER JOIN 导致主表数据被空结果过滤",
        "question": "查询公司的营业收入和总资产",
        "expected_rows": [
            {
                "stock_code": "000010",
                "revenue": "195619899.0600",
            }
        ],
        "subtask": {
            "company": {"ticker": "000010", "name": "北京城建投资发展股份有限公司"},
            "expect_columns": ["stock_code", "report_period", "revenue"],
            "sql": (
                "SELECT p.stock_code, p.report_period, p.revenue, b.total_assets "
                "FROM 2_performance_report p "
                "INNER JOIN 4_balance_sheet b "
                "ON b.stock_code=p.stock_code "
                "AND b.report_period=p.report_period "
                "WHERE p.stock_code='000010' "
                "ORDER BY p.report_period DESC LIMIT 1"
            ),
        },
    },
    {
        "id": "SQL-FIX-002",
        "scenario": "普通 JOIN 导致可选指标查询为空",
        "question": "查询公司的营业收入和总资产",
        "expected_rows": [
            {
                "stock_code": "000010",
                "revenue": "195619899.0600",
            }
        ],
        "subtask": {
            "company": {"ticker": "000010", "name": "北京城建投资发展股份有限公司"},
            "expect_columns": ["stock_code", "report_period", "revenue"],
            "sql": (
                "SELECT p.stock_code, p.report_period, p.revenue, b.total_assets "
                "FROM 2_performance_report p "
                "JOIN 4_balance_sheet b "
                "ON b.stock_code=p.stock_code "
                "AND b.report_period=p.report_period "
                "WHERE p.stock_code='000010' "
                "ORDER BY p.report_period DESC LIMIT 1"
            ),
        },
    },
    {
        "id": "SQL-DEG-001",
        "scenario": "字段拼写错误且本地规则无法修复",
        "question": "查询公司的营业收入",
        "expected_rows": [],
        "subtask": {
            "company": {"ticker": "000010", "name": "北京城建投资发展股份有限公司"},
            "expect_columns": ["stock_code", "report_period", "revenue"],
            "sql": (
                "SELECT stock_code, report_period, revenue, missing_metric "
                "FROM 2_performance_report "
                "WHERE stock_code='000010' LIMIT 1"
            ),
        },
    },
    {
        "id": "SQL-DEG-002",
        "scenario": "条件无匹配数据，触发空结果降级",
        "question": "查询测试公司的营业收入",
        "expected_rows": [],
        "subtask": {
            "company": {"ticker": "999999", "name": "测试公司"},
            "expect_columns": ["stock_code", "report_period", "revenue"],
            "sql": (
                "SELECT stock_code, report_period, revenue, net_profit "
                "FROM 2_performance_report "
                "WHERE stock_code='999999' LIMIT 1"
            ),
        },
    },
    {
        "id": "SQL-FIX-003",
        "scenario": "报告期格式写错导致有数据却查询为空",
        "question": "北京首创2026年半年报营业收入和净利润是多少",
        "expected_rows": [
            {
                "stock_code": "600008",
                "revenue": "8359349204.5400",
                "net_profit": "1168715925.1300",  # 取证：半年报「五、净利润」1,168,715,925.13（含少数股东；归母是 839,444,514.59，别混）
            }
        ],
        "subtask": {
            "company": {"ticker": "600008", "name": "北京首创生态环保集团股份有限公司"},
            "expect_columns": ["stock_code", "report_period", "revenue", "net_profit"],
            "sql": (
                "SELECT stock_code, report_period, revenue, net_profit "
                "FROM 2_performance_report "
                "WHERE stock_code='600008' "
                "AND report_period='2026-06-30' "
                "ORDER BY report_period DESC LIMIT 1"
            ),
        },
    },
]


def run_case(case: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """执行一个异常样本并整理评测结果。"""
    subtask = dict(case["subtask"])
    subtask["id"] = case["id"]
    result = assistant.execute_sql_subtask_with_correction(
        subtask,
        str(case["question"]),
        request,
    )
    rows = result.get("rows") or []
    matched = assistant.eval_rows_match(rows, case.get("expected_rows") or [])
    detected = bool(result.get("correction_log"))
    repaired = (
        detected
        and result.get("status") == "completed"
        and matched
    )
    return {
        "id": case["id"],
        "scenario": case["scenario"],
        "detected": detected,
        "repaired": repaired,
        "status": result.get("status"),
        "attempts": result.get("attempts", 0),
        "initial_sql": case["subtask"]["sql"],
        "final_sql": result.get("sql") or "",
        "expected_rows": case.get("expected_rows") or [],
        "actual_rows": rows,
        "error": result.get("error") or "",
        "correction_log": result.get("correction_log") or [],
    }


def build_report(online: bool = False) -> dict[str, Any]:
    """运行全部样本并计算页面需要的纠错指标。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if online and not api_key:
        raise ValueError("在线评测需要先设置 DEEPSEEK_API_KEY")
    request = {
        "api_key": api_key if online else "",
        "analysis_mode": "deepseek" if online else "local",
        "base_url": assistant.DEEPSEEK_BASE_URL,
        "llm_model": assistant.DEEPSEEK_MODEL,
    }
    samples = [run_case(case, request) for case in CASES]
    detected = [item for item in samples if item["detected"]]
    repaired = [item for item in detected if item["repaired"]]
    # 降级只算「检出了异常却没修好」的样本。原来的 `not item["repaired"]` 会把
    # 本来就没异常、压根不需要修的样本也计成降级（实测 degradation_count=5 ==
    # sample_count=5，而其中 SQL-FIX-001/002 的 detected=false 且返回行与期望一致）。
    degraded = [item for item in detected if not item["repaired"]]
    correction_rate = (
        len(repaired) / len(detected) * 100
        if detected
        else 0.0
    )
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "deepseek_benchmark" if online else "local_rule_benchmark",
        "summary": {
            "detected_anomaly_count": len(detected),
            "repair_success_count": len(repaired),
            "correction_rate_percent": round(correction_rate, 2),
            "degradation_count": len(degraded),
            "sample_count": len(samples),
        },
        "repair_samples": repaired,
        "degradation_samples": degraded,
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SQL 纠错与降级基准评测")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "storage" / "correction_report.json"),
        help="报告输出路径",
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="同时打印完整 JSON 报告",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="使用 DEEPSEEK_API_KEY 测试模型修复能力",
    )
    args = parser.parse_args()

    try:
        report = build_report(online=args.online)
    except ValueError as exc:
        parser.error(str(exc))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    summary = report["summary"]
    print(
        "SQL 纠错评测完成："
        f"检出异常 {summary['detected_anomaly_count']}，"
        f"修复成功 {summary['repair_success_count']}，"
        f"纠错成功率 {summary['correction_rate_percent']:.2f}%，"
        f"降级样本 {summary['degradation_count']}。"
    )
    print(f"报告文件：{output}")
    if args.print_report:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
