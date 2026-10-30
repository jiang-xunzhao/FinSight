"""跑四层深测并产出两份交付：缺陷清单 + 覆盖矩阵（都带时间戳，历史不许覆盖）。

用法（venv python，服务可不停，本件全程只读）：
    python run_layers.py                # 全四层
    python run_layers.py --only L1 L3   # 只跑某几层
    python run_layers.py --json         # 额外打 JSON 到 stdout

退出码：0 = 无 P0/P1；1 = 有 P0/P1；2 = 跑崩了。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import layers  # noqa: E402
from samples import LAYER_SAMPLES  # noqa: E402

REPORT_DIR = HERE / "报告"
SEVERITY_ORDER = ["P0", "P1", "P2", "P3"]

# 覆盖矩阵的维度 = 这个系统真出过事故的面向，每维列出由谁负责。
DIMENSIONS = [
    ("合并/母公司口径", "L1 勾稽 + L1 逐值 oracle + 600208 样本", ["L1 核心报表勾稽"]),
    ("金额单位（元/千/万/百万、中英文声明）", "L4 单位声明 vs 量级 + L4 恒等式反解残留 + 2 万亿越界", ["L4 边界", "L2 格式"]),
    ("折行与跨行科目接续", "L1 逐值 oracle（002508 归母折行 / 002195 归母）", ["L1 核心报表勾稽"]),
    ("定期 vs 非定期判型", "L3 非定期写了事实 + L3 定期零事实", ["L3 异常"]),
    ("身份：代码↔名称↔主数据", "L3 FILE_ 终态 + L3 改名不删行 + L2 坏名", ["L3 异常", "L2 格式"]),
    ("第三真相：切片身份/向量条数", "L3 切片 vs 文档索引 + L3 向量漂移", ["L3 异常"]),
    ("报告期与格式规范", "L2 标签正则 + L3 未来报告期", ["L2 格式", "L3 异常"]),
    ("量级边界与恒等式", "L1 资产恒等式 + L4 极小值 + L4 现金流差额", ["L1 核心报表勾稽", "L4 边界"]),
    ("口径歧义（净利润 vs 归母）", "L1 倍率>2 报告项（不自动定罪）", ["L1 核心报表勾稽"]),
]


def key_of(title: str) -> str:
    return title.split()[0]


def run(wanted: list[str] | None) -> tuple[list[dict], dict[str, str]]:
    findings: list[dict] = []
    errors: dict[str, str] = {}
    for title, func in layers.LAYERS.items():
        if wanted and key_of(title) not in wanted:
            continue
        try:
            got = func()
        except Exception as error:  # 单层崩了不能带走别的层
            errors[title] = f"{type(error).__name__}: {error}"
            continue
        findings.extend(got)
    return findings, errors


def write_findings(findings: list[dict], errors: dict[str, str], stamp: str) -> Path:
    counts = Counter(f["severity"] for f in findings)
    lines = [
        "# FinSight 分层深测 · 缺陷清单",
        "",
        f"- 生成时间：{stamp}",
        f"- 缺陷总数：**{len(findings)}**；" + " / ".join(f"{s}={counts.get(s, 0)}" for s in SEVERITY_ORDER),
        "- 口径：P0=库里是错数或身份串号（用户可见）；P1=解析缺陷导致缺数/错口径；"
        "P2=一致性或幂等风险，需人工判；P3=只报告不可判定（如资源被占用）。",
        "- 严重级别定义与复现约定见同目录 `README.md`。",
        "",
    ]
    if errors:
        lines += ["## ⚠️ 跑崩的层（结果不完整，先修件）", ""]
        for title, message in errors.items():
            lines.append(f"- {title}：`{message}`")
        lines.append("")
    for title in layers.LAYERS:
        rows = [f for f in findings if f["layer"] == title]
        lines.append(f"## {title} — {len(rows)} 条")
        lines.append("")
        if not rows:
            lines.append("无缺陷。")
            lines.append("")
            continue
        lines.append("| 级别 | 代码 | 期间 | 字段 | 文档名 | 预期 | 实际 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in sorted(rows, key=lambda item: (SEVERITY_ORDER.index(item["severity"]), item["code"])):
            lines.append(
                f"| {row['severity']} | `{row['code']}` | {row['period']} | `{row['field']}` "
                f"| {row['doc']} | {row['expected']} | {row['actual']} |"
            )
        lines.append("")
        lines.append("**复现步骤与日志**")
        lines.append("")
        for row in sorted(rows, key=lambda item: (SEVERITY_ORDER.index(item["severity"]), item["code"])):
            lines.append(f"- `{row['code']}` {row['field']}（{row['severity']}）")
            lines.append(f"  - 预期：{row['expected']}")
            lines.append(f"  - 实际：{row['actual']}")
            lines.append(f"  - 复现：`{row['repro']}`")
            lines.append(f"  - 日志/依据：{row['log']}")
            lines.append(f"  - 相关文档：{row['doc']}")
        lines.append("")
    path = REPORT_DIR / f"缺陷清单_{stamp.replace(' ', '_').replace(':', '')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    (REPORT_DIR / f"缺陷清单_{stamp.replace(' ', '_').replace(':', '')}.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


def write_matrix(findings: list[dict], errors: dict[str, str], stamp: str) -> Path:
    lines = [
        "# FinSight 分层深测 · 覆盖矩阵",
        "",
        f"- 生成时间：{stamp}",
        f"- 冒烟全量：文档 **{len(data_doc_count())}** 份在册；分层深测样本数见下表。",
        "- 说明：深测**不做逐文档全量重解析**（那是冒烟导入的事），只按层选典型样本 + 全库只读不变量。",
        "",
        "| 维度 | 覆盖手段 | 涉及层 | 深测样本数 | 缺陷数(P0/P1) | 结论 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, how, owners in DIMENSIONS:
        rows = [f for f in findings if f["layer"] in owners]
        samples = sum(len(LAYER_SAMPLES.get(key, [])) for key in ("core_reports", "format", "anomaly", "boundary"))
        p01 = sum(1 for f in rows if f["severity"] in ("P0", "P1"))
        verdict = "❌ 有实缺陷" if p01 else ("⚠️ 仅报告项" if rows else "✅ 无缺陷")
        if any(title in owners for title in errors):
            verdict = "🛑 该层跑崩，结论不完整"
        lines.append(f"| {name} | {how} | {'、'.join(owners)} | {samples} | {len(rows)}（{p01}） | {verdict} |")
    lines += [
        "",
        "## 每层样本与用例归属",
        "",
        "| 层 | 深测样本（代码） | 条数 | 历史 bug 样本是否在内 |",
        "| --- | --- | --- | --- |",
    ]
    historic = {entry["stock_code"] for entry in layers.HISTORIC_BUG_DOCS}
    mapping = {
        "L1 核心报表勾稽": "core_reports",
        "L2 格式": "format",
        "L3 异常": "anomaly",
        "L4 边界": "boundary",
    }
    for title, bucket in mapping.items():
        picked = LAYER_SAMPLES.get(bucket, [])
        hit = sorted(historic & set(picked))
        lines.append(f"| {title} | {', '.join(picked)} | {len(picked)} | {', '.join(hit) or '—'} |")
    # 件数必须现数，别写死 —— 写死过一次，加件之后矩阵就一直报错数。
    fixtures = sorted(p.name for p in (HERE / "常驻件").glob("*.py"))
    lines += [
        "",
        "## 常驻回归（固化件，与深测互补）",
        "",
        f"- `06_维护工具\\回归测试\\常驻件\\` 共 **{len(fixtures)}** 件；跑法 `python run_all.py`（项目内唯一入口，串行）。",
        "- 历史 bug → 固化件对照：非定期判型 `test_nonperiodic_no_overwrite.py`；同键谁赢 `test_richer_row_wins.py`；"
        "口径与折行 `test_percent_and_scope_guards.py`；英文科目 `test_english_labels.py`；单位反解 `test_balance_unit_recovery.py`；"
        "串号与闸门 `test_ingest_gate.py`；报告期 `test_report_period_label.py`；名称清洗 `test_company_name_cleaning.py`；"
        "主数据与改名不删行 `test_identity_hygiene.py`/`test_company_aliases.py`；混合口径展示 `test_comparison_mixed_period.py`。",
        "",
    ]
    path = REPORT_DIR / f"覆盖矩阵_{stamp.replace(' ', '_').replace(':', '')}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def data_doc_count() -> list[dict]:
    import data

    return data.query_all("SELECT id FROM `7_document_index`")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None, help="层号，如 L1 L3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    findings, errors = run(args.only)
    counts = Counter(f["severity"] for f in findings)
    report = write_findings(findings, errors, stamp)
    matrix = write_matrix(findings, errors, stamp)
    print(f"缺陷 {len(findings)} 条：P0={counts.get('P0', 0)} P1={counts.get('P1', 0)} "
          f"P2={counts.get('P2', 0)} P3={counts.get('P3', 0)}")
    for title, message in errors.items():
        print(f"⚠️ {title} 跑崩：{message}")
    print(f"清单 {report}")
    print(f"矩阵 {matrix}")
    if args.json:
        print(json.dumps(findings, ensure_ascii=False))
    return 2 if errors else (1 if counts.get("P0", 0) or counts.get("P1", 0) else 0)


if __name__ == "__main__":
    sys.exit(main())
