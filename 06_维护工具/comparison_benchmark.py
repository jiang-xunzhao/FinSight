"""四方案对比评测。

用法：
    python 06_维护工具/comparison_benchmark.py

对纯大模型、仅 Text-to-SQL、关键词 RAG 和本系统运行同一套固定问题，
输出回答准确率、证据可核验率、平均响应时间和逐题日志。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
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


SYSTEMS = ("纯大模型", "仅 Text-to-SQL", "关键词 RAG", "本系统")

# 期望值一律取 PDF 原文（不再抄库内数），2026-10-27 逐值取证于 6_rag_chunks：
#   600008 五、净利润 1,168,715,925.13 / 归母 839,444,514.59（旧基准 121,275,639.08
#          是修复前把归母串填进 net_profit 的产物，两个口径都不是）
#   600098 净利润 992,406,445.48 / 归母 945,907,060.15（旧基准 534,669,504.45 同上）
#   000801 五、净利润 96,309,075.55 / 归母 81,198,372.22（旧基准填的是归母，口径不一致）
# 题目问“净利润”，按利润表“五、净利润”行＝合并口径判。
# 断言里**不放身份列**（stock_code / stock_name 都不放）——实测两个方向都会误杀：
# 本系统 C01 返回行有 stock_code 无 stock_name，仅 Text-to-SQL C04 返回行有 stock_name
# 无 stock_code，两边数字都对，钉任何一列都会把答对的一方判成答错（那 0/6 里就有一条是这么来的）。
# 口径靠两件事锁死：`net_profit` 的**值**本身（填成归母 839,444,514.59 就不等）＋
# expected_text 里的亿元数（“11.69”＝合并口径，“8.39”是归母，写错就判不过）。
CASES: list[dict[str, Any]] = [
    {
        "id": "C01",
        "kind": "numbers",
        "question": "北京首创生态环保集团股份有限公司2026年半年报营业收入和净利润是多少？",
        "expected_rows": [
            {
                "revenue": "8359349204.54",
                "net_profit": "1168715925.13",
            }
        ],
        "expected_text": ["83.59", "11.69"],
    },
    {
        "id": "C02",
        "kind": "numbers",
        "question": "广州发展集团股份有限公司2026年半年报营业收入和净利润是多少？",
        "expected_rows": [
            {
                "revenue": "25487271557.38",
                "net_profit": "992406445.48",
            }
        ],
        "expected_text": ["254.87", "9.92"],
    },
    {
        "id": "C03",
        "kind": "numbers",
        "question": "四川九洲电器股份有限公司2026年半年报营业收入和净利润是多少？",
        "expected_rows": [
            {
                "revenue": "2595626743.19",
                "net_profit": "96309075.55",
            }
        ],
        "expected_text": ["25.96", "0.96"],
    },
    {
        "id": "C04",
        "kind": "comparison",
        "question": "对比北京首创生态环保集团股份有限公司和广州发展集团股份有限公司的营业收入和净利润。",
        "expected_rows": [
            {
                "revenue": "8359349204.54",
                "net_profit": "1168715925.13",
            },
            {
                "revenue": "25487271557.38",
                "net_profit": "992406445.48",
            },
        ],
        "expected_text": ["83.59", "11.69", "254.87", "9.92"],
    },
    {
        # 无数据题要挑“库里从来没有的”公司：600519 实测 1_stock_basic、7_document_index
        # 均 0 行。原题目用 600007 中国国际贸易中心，2026-10-27 实测它两份表都已有值，
        # 前提已不成立，故换掉；若日后真导入茅台需再换一家。
        "id": "C05",
        "kind": "no_data",
        "question": "贵州茅台酒股份有限公司2026年半年报营业收入和净利润是多少？",
        "expected_rows": [],
        "expected_text": [
            "暂无数据",
            "未检索到",
            "无数据",
            "无法提供",
            "尚未披露",
            "未披露",
            "没有公布",
        ],
    },
    {
        "id": "C06",
        "kind": "keywords",
        "question": "四川九洲电器股份有限公司有哪些主要经营风险？",
        "expected_rows": [],
        "expected_text": ["原材料", "汇率", "应收账款", "价格波动"],
        "min_keyword_hits": 2,
    },
]


def normalize_text(value: Any) -> str:
    """去除空白和千分位，便于比较数字和中文片段。"""
    return re.sub(r"[\s,，]", "", str(value or "")).lower()


def rows_match(case: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    expected = case.get("expected_rows") or []
    return bool(rows) and assistant.eval_rows_match(rows, expected)


def score_answer(
    case: dict[str, Any],
    answer: str,
    rows: list[dict[str, Any]],
) -> bool:
    """按数字、关键词或无数据提示判断本次回答是否通过。"""
    if case["kind"] == "no_data":
        # 旧判据 `if not rows: return True` 让“什么都不回答”也算诚实：实测 2026-09-17
        # 那份报告里 仅 Text-to-SQL 对本题返回空串（rows=[]）就被记为通过，测不出东西。
        # 诚实 = 明确说库里没有 ⇒ 既不返回行，文本里也要有“无数据”类提示。
        if rows:
            return False
        normalized = normalize_text(answer)
        return any(
            normalize_text(item) in normalized
            for item in case.get("expected_text") or []
        )

    if case["kind"] in {"numbers", "comparison"}:
        if rows_match(case, rows):
            return True
        normalized = normalize_text(answer)
        return all(
            normalize_text(item) in normalized
            for item in case.get("expected_text") or []
        )

    normalized = normalize_text(answer)
    hits = sum(
        1
        for item in case.get("expected_text") or []
        if normalize_text(item) in normalized
    )
    return hits >= int(case.get("min_keyword_hits") or 1)


def create_client() -> assistant.OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("未设置 DEEPSEEK_API_KEY")
    return assistant.make_client(api_key, assistant.DEEPSEEK_BASE_URL)


def run_pure_llm(
    question: str,
    client: assistant.OpenAI,
) -> tuple[str, list[dict[str, Any]]]:
    response = client.chat.completions.create(
        model=assistant.DEEPSEEK_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是通用大模型。直接回答用户问题，不要调用数据库、"
                    "检索工具或外部文件。"
                ),
            },
            {"role": "user", "content": question},
        ],
        temperature=0,
    )
    return response.choices[0].message.content or "", []


def run_text_to_sql(
    question: str,
    client: assistant.OpenAI,
) -> tuple[str, list[dict[str, Any]]]:
    prompt = (
        "你是财报 Text-to-SQL 生成器。只允许使用以下表结构生成一条 SELECT SQL。"
        "只输出 JSON，格式为 {\"sql\":\"SELECT ...\"}，不要输出解释，"
        "SQL 不要以分号结尾。\n\n"
        f"{assistant.FINANCIAL_SCHEMA_PROMPT}\n\n"
        f"用户问题：{question}"
    )
    response = client.chat.completions.create(
        model=assistant.DEEPSEEK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    plan = assistant.parse_json_from_text(
        response.choices[0].message.content or ""
    )
    raw_sql = str(plan.get("sql") or "").strip().rstrip(";").rstrip()
    sql = assistant.validate_sql_safety(raw_sql)
    rows = assistant.query_all(sql)
    return json.dumps(rows, ensure_ascii=False, default=str), rows


def keyword_terms(question: str) -> set[str]:
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", question)
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", question))
    terms.update(
        compact[index : index + 2]
        for index in range(max(0, len(compact) - 1))
    )
    return {term for term in terms if len(term) >= 2}


def run_keyword_rag(question: str) -> tuple[str, list[dict[str, Any]]]:
    terms = keyword_terms(question)
    company = assistant.identify_company_from_question(question)
    if isinstance(company, list):
        company = company[0] if company else {}
    ticker = str((company or {}).get("ticker") or "")
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in assistant.RAG_CHUNKS:
        content = str(chunk.get("content") or "")
        if not content:
            continue
        chunk_ticker = str(
            chunk.get("stock_code")
            or chunk.get("company_ticker")
            or ""
        )
        score = sum(1.0 for term in terms if term in content)
        if ticker and chunk_ticker:
            score += 20.0 if chunk_ticker == ticker else -20.0
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [item[1] for item in scored[:5]]
    answer = "\n".join(str(item.get("content") or "") for item in selected)
    return answer[:12000], selected


def run_current_system(
    question: str,
    client: assistant.OpenAI,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    del client
    session_id = f"benchmark-{uuid.uuid4().hex}"
    try:
        result = assistant.handle_chat_request(
            {
                "question": question,
                "session_id": session_id,
                "analysis_mode": "deepseek",
                "unit_mode": "自动",
                "audience": "综合分析师",
            }
        )
    finally:
        assistant.delete_chat_session(session_id)
    rows = result.get("raw_table") or result.get("table") or []
    return str(result.get("answer") or ""), rows, result


def run_system(
    system: str,
    case: dict[str, Any],
    client: assistant.OpenAI,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        if system == "纯大模型":
            answer, rows = run_pure_llm(case["question"], client)
            result = {}
        elif system == "仅 Text-to-SQL":
            answer, rows = run_text_to_sql(case["question"], client)
            result = {}
        elif system == "关键词 RAG":
            answer, rows = run_keyword_rag(case["question"])
            result = {}
        else:
            answer, rows, result = run_current_system(case["question"], client)
        passed = score_answer(case, answer, rows)
        if system == "关键词 RAG":
            verifiable = bool(rows)
        elif system == "仅 Text-to-SQL":
            verifiable = bool(rows)
        elif system == "本系统":
            evidence = result.get("evidence") or []
            verifiable = bool(
                rows
                or evidence
                or (result.get("evidence_summary") or {}).get("metric_count")
            )
        else:
            verifiable = False
        return {
            "passed": passed,
            "verifiable": verifiable,
            "latency_seconds": time.perf_counter() - started,
            "answer": answer,
            "rows": rows,
            "error": "",
        }
    except Exception as exc:
        return {
            "passed": False,
            "verifiable": False,
            "latency_seconds": time.perf_counter() - started,
            "answer": "",
            "rows": [],
            "error": str(exc),
        }


def build_report() -> dict[str, Any]:
    client = create_client()
    details: list[dict[str, Any]] = []
    for system in SYSTEMS:
        for case in CASES:
            print(
                f"[评测] {system} / {case['id']} ...",
                flush=True,
            )
            outcome = run_system(system, case, client)
            details.append(
                {
                    "system": system,
                    "case_id": case["id"],
                    "question": case["question"],
                    **outcome,
                }
            )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sample_count": len(CASES),
        "systems": summarize(details),
        "details": details,
    }


def summarize(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for system in SYSTEMS:
        items = [
            item for item in details if item["system"] == system
        ]
        total = max(1, len(items))
        latencies = sorted(item["latency_seconds"] for item in items)
        accuracy = sum(item["passed"] for item in items) / total * 100
        verifiable = sum(item["verifiable"] for item in items) / total * 100
        summaries.append(
            {
                "system": system,
                "passed": sum(item["passed"] for item in items),
                "total": len(items),
                "accuracy_percent": round(accuracy, 2),
                "verifiable_percent": round(verifiable, 2),
                "average_latency_seconds": round(
                    sum(latencies) / total,
                    2,
                ),
                "p95_latency_seconds": round(
                    assistant.percentile(latencies, 0.95),
                    2,
                ),
            }
        )
    return summaries


def rescore_report(path: Path) -> dict[str, Any]:
    """用当前 CASES 与判据**重判已存报告里的答案**，一次模型都不调。

    基准或判据改了、而答案与响应时间仍是同一轮实测 ⇒ 重判比再烧一次额度既省钱又诚实
    （同一份答卷换判据，才是真正在测判据的变化）。实测时间记在 `generated_at`，
    重判时间另记 `rescored_at`，逐题另记 `rescore_note`。
    """
    report = json.loads(path.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in CASES}
    for item in report.get("details") or []:
        case = by_id.get(item.get("case_id"))
        if case is None:
            item["passed"] = False
            item["rescore_note"] = "题面已不在当前 CASES 中，无法重判"
            continue
        item["question"] = case["question"]
        item["passed"] = score_answer(
            case,
            str(item.get("answer") or ""),
            list(item.get("rows") or []),
        )
        item["rescore_note"] = "按当前基准重判（未重新调用模型）"
    report["systems"] = summarize(report.get("details") or [])
    report["rescored_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "| 方案 | 回答准确率 | 证据可核验率 | 平均响应时间 | P95 | 通过/样本 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["systems"]:
        lines.append(
            f"| {item['system']} | {item['accuracy_percent']:.2f}% | "
            f"{item['verifiable_percent']:.2f}% | "
            f"{item['average_latency_seconds']:.2f}s | "
            f"{item['p95_latency_seconds']:.2f}s | "
            f"{item['passed']}/{item['total']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="四方案对比评测")
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "storage"),
        help="报告输出目录",
    )
    parser.add_argument(
        "--rescore",
        nargs="?",
        const="",
        default=None,
        metavar="REPORT_JSON",
        help="不调模型，用当前 CASES 与判据重判已有报告（省略参数就用 output-dir 里那份）",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "comparison_report.json"
    markdown_path = output_dir / "comparison_report.md"

    if args.rescore is None:
        report = build_report()
    else:
        source = Path(args.rescore) if args.rescore else json_path
        print(f"[重判] 读 {source}，一次模型都不调", flush=True)
        report = rescore_report(source)

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(
        markdown_report(report),
        encoding="utf-8",
    )
    print(markdown_report(report))
    if report.get("rescored_at"):
        print(
            f"实测时间 {report.get('generated_at')} ⇒ 重判时间 {report['rescored_at']}"
        )
    print(f"JSON：{json_path}")
    print(f"Markdown：{markdown_path}")


if __name__ == "__main__":
    main()
