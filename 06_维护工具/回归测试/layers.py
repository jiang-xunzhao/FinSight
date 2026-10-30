"""四层深测：L1 核心报表勾稽 / L2 格式 / L3 异常 / L4 边界。

设计约束（都是这个会话踩出来的）：
- **只读**：全部走 SQL 与已入库的 `6_rag_chunks` 文本，绝不重跑 OCR、绝不写库。
  需要重解析的验证交给 `_tools\reimport_by_code.py` + 常驻件，不在这里做。
- 每条缺陷必须能定位到**文档名**（从 `7_document_index` 反查），否则报告没法复现。
- 期望值一律来自 `samples.ORACLE`（唯一一份真值登记表），这里不写死数字。
- 判定与报告分离：P0/P1 计入结论，P2/P3 只报告（倍率>2 这类证据不足的不许自动定罪，
  实测占比 24%，见 `probe_minority_identity.py`）。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(HERE.parent.parent / _d))
sys.path.insert(0, str(HERE))

import data  # noqa: E402
from samples import (  # noqa: E402
    BALANCE_COLUMNS,
    CASH_COLUMNS,
    HISTORIC_BUG_DOCS,
    INCOME_COLUMNS,
    LAYER_SAMPLES,
    ORACLE,
    PERF_COLUMNS,
)

PERIOD_RE = re.compile(r"^\d{4}年(年报|半年报|一季报|三季报)$")
UNIT_HINT_RE = re.compile(
    r"单位\s*[：:]\s*(?:人民币)?\s*(百万元|万元|千元)|Unit\s*[：:.]?\s*RMB\s*'?(?:0,000|000|10,000)|thousand",
    re.I,
)


def finding(layer: str, severity: str, doc: str, code: str, period: str,
            field: str, expected: str, actual: str, repro: str, log: str) -> dict[str, Any]:
    return {
        "layer": layer, "severity": severity, "doc": doc, "code": code, "period": period,
        "field": field, "expected": expected, "actual": actual, "repro": repro, "log": log,
    }


def docs_of(code: str) -> str:
    rows = data.query_all(
        "SELECT file_name, COUNT(*) AS n FROM 7_document_index WHERE stock_code=%s "
        "GROUP BY file_name ORDER BY MIN(id)", (code,),
    )
    if not rows:
        return "(无文档)"
    parts = [f"{r['file_name']} ×{r['n']}" if int(r["n"]) > 1 else str(r["file_name"]) for r in rows]
    return "; ".join(parts)


def latest_row(table: str, code: str, period: str | None = None) -> dict[str, Any] | None:
    if period:
        rows = data.query_all(
            f"SELECT * FROM `{table}` WHERE stock_code=%s AND report_period=%s ORDER BY id DESC LIMIT 1",
            (code, period),
        )
    else:
        rows = data.query_all(
            f"SELECT * FROM `{table}` WHERE stock_code=%s ORDER BY report_period DESC, id DESC LIMIT 1",
            (code,),
        )
    return rows[0] if rows else None


def as_dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def close(actual: Decimal, expected: Decimal, tol: str = "0.001") -> bool:
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / abs(expected) <= Decimal(tol)


# ------------------------------------------------------------------ L1 核心报表勾稽
def layer_core_reports() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in LAYER_SAMPLES["core_reports"]:
        doc = docs_of(code)
        bal = latest_row("4_balance_sheet", code)
        if bal:
            assets = as_dec(bal.get("total_assets"))
            liab = as_dec(bal.get("total_liability"))
            equity = as_dec(bal.get("total_equity"))
            if assets and liab is not None and equity is not None:
                if abs(assets - (liab + equity)) / max(abs(assets), Decimal(1)) > Decimal("0.01"):
                    out.append(finding(
                        "L1 核心报表勾稽", "P1", doc, code, str(bal["report_period"]),
                        "total_assets = total_liability + total_equity",
                        f"差额/资产 ≤ 1%", f"资产 {assets} vs 负债+权益 {liab + equity}",
                        f"SELECT * FROM `4_balance_sheet` WHERE stock_code='{code}'",
                        "恒等式不平（多为单位错位或取了母公司权益）",
                    ))
        perf = latest_row("2_performance_report", code)
        # 逐值 oracle：只在 samples.ORACLE 里登记过的字段上断言
        expect = ORACLE.get(code, {})
        period = expect.get("report_period")
        for table, cols in (
            ("2_performance_report", {"revenue": "revenue", "net_profit": "net_profit",
                                      "net_profit_parent": "net_profit_parent"}),
            ("3_income_statement", {"revenue": "operating_revenue", "net_profit": "net_profit",
                                    "net_profit_parent": "net_profit_parent"}),
            ("4_balance_sheet", {"total_assets": "total_assets", "total_liability": "total_liability",
                                 "total_equity": "total_equity"}),
        ):
            row = latest_row(table, code, period)
            if not row:
                continue
            for key, column in cols.items():
                want = expect.get(key)
                if want is None:
                    continue
                got = as_dec(row.get(column))
                if got is None or not close(got, want):
                    out.append(finding(
                        "L1 核心报表勾稽", "P0", doc, code, str(row["report_period"]),
                        f"{table}.{column}", str(want), "NULL" if got is None else str(got),
                        f"SELECT {column} FROM `{table}` WHERE stock_code='{code}'",
                        "与已核验原文值不符（见 samples.ORACLE 的 note）",
                    ))
        # 净利润 vs 归母：倍率>2 只报告，不自动定罪
        if perf:
            net = as_dec(perf.get("net_profit"))
            parent = as_dec(perf.get("net_profit_parent"))
            if net and parent and abs(parent) >= 10000:
                # **只报「合并远大于母公司」这一个方向**：合并 < 母公司是正常现象（子公司亏损）。
                # 实测反例：600208 2026H1 合并 32,234,254.86、母公司 169,143,269.77（0.19×），
                # 两个数都与原文一致 ⇒ 双向倍率是误报。
                # 真缺陷的方向是母公司的数串进合并行：002195 曾 net=402,632,550.37
                # （其实是「六、综合收益总额」）vs parent=32,461,729.28 = 12.4×。
                ratio = net / parent
                if ratio > Decimal("2"):
                    out.append(finding(
                        "L1 核心报表勾稽", "P2", doc, code, str(perf["report_period"]),
                        "net_profit vs net_profit_parent",
                        "合并净利润不应远大于母公司（子公司亏损只会让合并更小）", f"倍率 {ratio:.1f}×",
                        "python _tools\\probe_minority_identity.py " + code,
                        "多半是把「综合收益总额」或母公司净利润接到了合并口径上 ⇒ 回原文判",
                    ))
        for key in expect.get("expect_dropped", []) or []:
            row = latest_row("3_income_statement", code, period)
            if row and as_dec(row.get(key)) is not None:
                out.append(finding(
                    "L1 核心报表勾稽", "P1", doc, code, str(row["report_period"]), key,
                    "NULL（与归母量级差 >100× 应被单位护栏丢弃）", str(row.get(key)),
                    f"SELECT {key} FROM `3_income_statement` WHERE stock_code='{code}'",
                    "单位不一致的利润列没被丢弃",
                ))
    return out


# ------------------------------------------------------------------ L2 格式
def layer_format() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bad_period = data.query_all(
        "SELECT DISTINCT stock_code, report_period FROM 2_performance_report WHERE report_period NOT REGEXP %s",
        (r"^[0-9]{4}年(年报|半年报|一季报|三季报)$",),
    )
    for row in bad_period:
        code = str(row["stock_code"])
        out.append(finding(
            "L2 格式", "P1", docs_of(code), code, str(row["report_period"]),
            "report_period", "形如 2026年半年报 / 2025年年报", str(row["report_period"]),
            "SELECT DISTINCT report_period FROM `2_performance_report`",
            "报告期标签格式不规范，界面按它排序与选期会错乱",
        ))
    bad_names = data.query_all(
        "SELECT stock_code, stock_name FROM 1_stock_basic "
        "WHERE stock_name REGEXP '^[0-9]' OR stock_name LIKE '%% %%' OR stock_name = ''"
    )
    for row in bad_names:
        code = str(row["stock_code"])
        out.append(finding(
            "L2 格式", "P1", docs_of(code), code, "-",
            "1_stock_basic.stock_name", "无数字前缀、无空格、非空", repr(str(row["stock_name"])),
            "SELECT stock_code, stock_name FROM `1_stock_basic`",
            "OCR 把条目号粘进公司名（历史：2中国东方航空股份有限公司）",
        ))
    overflow = data.query_all(
        "SELECT stock_code, report_period, revenue FROM 2_performance_report "
        "WHERE ABS(revenue) >= 1000000000000"
    )
    for row in overflow:
        code = str(row["stock_code"])
        out.append(finding(
            "L2 格式", "P1", docs_of(code), code, str(row["report_period"]),
            "revenue 量级", "< 1 万亿", str(row["revenue"]),
            "SELECT stock_code, revenue FROM `2_performance_report` WHERE ABS(revenue)>=1e12",
            "单位声明放大过头（历史：600008 被附注「单位：万元」乘成 1.17e13）",
        ))
    yoy = data.query_all(
        "SELECT stock_code, report_period, revenue_yoy, profit_yoy FROM 2_performance_report "
        "WHERE ABS(COALESCE(revenue_yoy,0)) > 10000 OR ABS(COALESCE(profit_yoy,0)) > 10000"
    )
    for row in yoy:
        code = str(row["stock_code"])
        out.append(finding(
            "L2 格式", "P2", docs_of(code), code, str(row["report_period"]),
            "revenue_yoy/profit_yoy", "|值| ≤ 10000（bounded_percent 已限）",
            f"{row['revenue_yoy']} / {row['profit_yoy']}",
            "SELECT * FROM `2_performance_report` WHERE ABS(revenue_yoy)>10000",
            "百分比列越界",
        ))
    for code in LAYER_SAMPLES["format"]:
        row = latest_row("2_performance_report", code)
        if row and not PERIOD_RE.match(str(row["report_period"])):
            out.append(finding(
                "L2 格式", "P1", docs_of(code), code, str(row["report_period"]),
                "report_period", PERIOD_RE.pattern, str(row["report_period"]),
                f"SELECT * FROM `2_performance_report` WHERE stock_code='{code}'",
                "样本期内标签不规范",
            ))
    return out


# ------------------------------------------------------------------ L3 异常
def layer_anomaly() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # 1) 切片身份 vs 文档索引（第三真相）
    mismatch = data.query_all(
        "SELECT c.file_path, c.stock_code AS chunk_code, d.stock_code AS doc_code, "
        "c.company_name AS chunk_name, d.company_name AS doc_name "
        "FROM `6_rag_chunks` c JOIN `7_document_index` d ON c.file_path = d.file_path "
        "WHERE NOT (c.stock_code <=> d.stock_code) OR NOT (c.company_name <=> d.company_name) "
        "LIMIT 20"
    )
    for row in mismatch:
        out.append(finding(
            "L3 异常", "P0", str(row["file_path"]), str(row["chunk_code"]), "-",
            "切片身份", "与 7_document_index 一致",
            f"chunk {row['chunk_code']}/{row['chunk_name']} vs doc {row['doc_code']}/{row['doc_name']}",
            "SELECT ... FROM `6_rag_chunks` c JOIN `7_document_index` d ON c.file_path=d.file_path",
            "检索命中口径与库内口径不一致（交付 SQL ⑤c 修）",
        ))
    # 2) 非定期文档写了事实
    for entry in HISTORIC_BUG_DOCS:
        if entry["check"] != "nonperiodic_no_fact":
            continue
        code = entry["stock_code"]
        rows = data.query_all("SELECT COUNT(*) AS c FROM `2_performance_report` WHERE stock_code=%s", (code,))
        got = int(rows[0]["c"]) if rows else 0
        if got:
            out.append(finding(
                "L3 异常", entry["severity"], docs_of(code), code, "-",
                "四张事实表行数", entry["expect"], f"{got} 行",
                f"SELECT * FROM `2_performance_report` WHERE stock_code='{code}'",
                "重组件/评估报告这类非定期文档不该写财务事实（判型：looks_like_periodic_report）",
            ))
    # 3) 未来报告期
    future = data.query_all(
        "SELECT stock_code, report_period FROM 2_performance_report "
        "WHERE CAST(LEFT(report_period,4) AS UNSIGNED) > YEAR(CURDATE())"
    )
    for row in future:
        code = str(row["stock_code"])
        out.append(finding(
            "L3 异常", "P0", docs_of(code), code, str(row["report_period"]),
            "report_period", f"≤ {datetime.now().year}", str(row["report_period"]),
            "SELECT * FROM `2_performance_report` WHERE LEFT(report_period,4) > YEAR(CURDATE())",
            "报告期年超过披露年（历史：002030 正文 2030 年半年度）",
        ))
    # 4) 定期文档但零事实（能力缺失）——判型用**已入库的首页切片**跑，不重跑 OCR
    zero = data.query_all(
        "SELECT d.stock_code AS code FROM `7_document_index` d "
        "LEFT JOIN `2_performance_report` p ON p.stock_code = d.stock_code "
        "WHERE p.stock_code IS NULL GROUP BY d.stock_code"
    )
    for row in zero:
        code = str(row["code"])
        heads = data.query_all(
            "SELECT file_name, content FROM `6_rag_chunks` WHERE stock_code=%s "
            "AND chunk_index=0 ORDER BY page_no, file_name LIMIT 60",
            (code,),
        )
        periodic = [
            str(item["file_name"])
            for item in heads
            if data.looks_like_periodic_report(str(item["content"])[:900], str(item["file_name"]))
        ]
        if not periodic:
            continue  # 全是非定期件（重组件/审计报告）⇒ 零事实是设计行为，不是缺陷
        out.append(finding(
            "L3 异常", "P1", docs_of(code), code, "-",
            "定期报告 → 财务事实", "至少 1 行", f"0 行（判型为定期的文档 {len(periodic)} 份）",
            f"SELECT file_name, content FROM `6_rag_chunks` WHERE stock_code='{code}' AND chunk_index=0",
            f"被判为定期报告的文档抽不出指标，最近一份：{periodic[0]}",
        ))
    # 5) 主体与名称：改名不删行的历史 bug
    for entry in HISTORIC_BUG_DOCS:
        if entry["check"] != "entity_exists_named":
            continue
        rows = data.query_all("SELECT stock_name FROM `1_stock_basic` WHERE stock_code=%s", (entry["stock_code"],))
        if not rows:
            out.append(finding(
                "L3 异常", entry["severity"], entry["file_needle"], entry["stock_code"], "-",
                "1_stock_basic", f"存在且名为 {data.COMPANY_MASTER.get(entry['stock_code'], '')}", "行不存在",
                f"SELECT * FROM `1_stock_basic` WHERE stock_code='{entry['stock_code']}'",
                entry["bug"] + " ⇒ 真实代码只能改名不能删行",
            ))
        elif str(rows[0]["stock_name"]) != data.COMPANY_MASTER.get(entry["stock_code"], str(rows[0]["stock_name"])):
            out.append(finding(
                "L3 异常", entry["severity"], entry["file_needle"], entry["stock_code"], "-",
                "1_stock_basic.stock_name", data.COMPANY_MASTER.get(entry["stock_code"], ""),
                str(rows[0]["stock_name"]),
                f"SELECT * FROM `1_stock_basic` WHERE stock_code='{entry['stock_code']}'",
                "主数据名不符（seed_company_master 应已 upsert）",
            ))
    # 6) FILE_ 主体必须停在已定性的终态
    file_rows = data.query_all(
        "SELECT stock_code, stock_name FROM `1_stock_basic` WHERE stock_code LIKE 'FILE\\_%' ESCAPE '\\\\'"
    )
    for row in file_rows:
        code = str(row["stock_code"])
        if code not in {"FILE_4080B9E7", "FILE_E664CE98"}:
            out.append(finding(
                "L3 异常", "P2", docs_of(code), code, "-",
                "FILE_ 主体", "只剩已定性的泰州两家", str(row["stock_name"]),
                "SELECT * FROM `1_stock_basic` WHERE stock_code LIKE 'FILE[_]%'".replace("[_]", "\\_"),
                "又出现新的「代码抽不出来」主体，需要人工定身份",
            ))
    # 7) 回归集自检：历史 bug 用例必须**真的被某条断言吃到**，否则只是装饰（实测踩过：
    #    登记了 check="oracle_value" 但 samples.ORACLE 里没有该 code ⇒ 四层跑完一片绿，其实什么都没验）。
    all_docs = {str(r["file_name"]) for r in data.query_all("SELECT DISTINCT file_name FROM `7_document_index`")}
    for entry in HISTORIC_BUG_DOCS:
        kind = str(entry["check"])
        code = str(entry["stock_code"])
        needle = str(entry["file_needle"])
        # `entity_exists_named` 断言的是**主体**存在与名字，不是文档存在（实测 300006 莱美药业在库里 0 份文档
        # 但主体必须保留 —— 「改名不删行」这条历史 bug 就是这样）⇒ 这类用例不查 needle。
        if kind == "entity_exists_named":
            continue
        if not any(needle in name for name in all_docs):
            out.append(finding(
                "L3 异常", "P2", needle, code, "-", "HISTORIC_BUG_DOCS.file_needle",
                "库里至少有一份文件名含该串", "无匹配文档",
                "SELECT DISTINCT file_name FROM `7_document_index`",
                "用例指向的文档已不在库里 ⇒ 这条历史 bug 不再被守护：" + entry["bug"],
            ))
            continue
        if kind not in {"oracle_value", "entity_exists_named", "nonperiodic_no_fact",
                        "code_name_match", "no_future_period"}:
            out.append(finding(
                "L3 异常", "P1", needle, code, "-", "HISTORIC_BUG_DOCS.check",
                "oracle_value|entity_exists_named|nonperiodic_no_fact|code_name_match|no_future_period",
                kind, "06_维护工具\\回归测试\\layers.py 的 layer_anomaly",
                "出现了没人消费的 check 类型 ⇒ 该用例静默失效",
            ))
            continue
        if kind == "oracle_value" and not ORACLE.get(code):
            out.append(finding(
                "L3 异常", "P1", needle, code, "-", "samples.ORACLE[stock_code]",
                "有逐值真值（oracle_value 必须有）", "未登记",
                "06_维护工具\\回归测试\\samples.py",
                "登记了历史 bug 但没登记真值 ⇒ L1 逐值断言永远不会碰它：" + entry["bug"],
            ))
        if kind == "oracle_value" and code not in LAYER_SAMPLES["core_reports"]:
            out.append(finding(
                "L3 异常", "P1", needle, code, "-", "LAYER_SAMPLES['core_reports']",
                f"包含 {code}", "未列入 ⇒ L1 不遍历该公司",
                "06_维护工具\\回归测试\\samples.py",
                "真值登记了但公司不在 L1 采样清单 ⇒ 断言不执行：" + entry["bug"],
            ))
        if kind == "code_name_match":
            # 这条历史 bug 的实质是「文档挂在了别人的代码下」⇒ 逐条按代码反查文档名与主体名是否一致，
            # 不靠全局检查兜（全局只查切片↔文档，查不到「文档↔主体名」这层）。
            bad = data.query_all(
                "SELECT d.file_name, d.company_name, b.stock_name FROM `7_document_index` d "
                "JOIN `1_stock_basic` b ON b.stock_code = d.stock_code "
                "WHERE d.stock_code=%s AND d.company_name <> b.stock_name LIMIT 5", (code,),
            )
            for row in bad:
                out.append(finding(
                    "L3 异常", "P0", str(row["file_name"]), code, "-", "文档公司名 ↔ 主体名",
                    str(row["stock_name"]), str(row["company_name"]),
                    f"SELECT * FROM `7_document_index` WHERE stock_code='{code}'",
                    "错挂残留（这条历史 bug 的回归断言）：" + entry["bug"],
                ))

    # 8) 向量与库条数漂移（内嵌 Qdrant 被服务独占时记 P3，不误判也不静默跳过）

    try:
        db_chunks = int(data.query_all("SELECT COUNT(*) AS c FROM `6_rag_chunks`")[0]["c"])
        status = data.load_vector_index_status()
        qdrant = status.get("qdrant") or {}
        vector_count = int(qdrant.get("count") or 0)
        if not vector_count:
            out.append(finding(
                "L3 异常", "P3", "(全库)", "-", "-", "向量点数", f"{db_chunks}",
                f"读不到：{qdrant.get('error') or 'count=0'}",
                "停服后跑本层，或看 GET /api/vector-index", "占用导致的不可读不算缺陷，但必须记账",
            ))
        elif vector_count != db_chunks:
            out.append(finding(
                "L3 异常", "P2", "(全库)", "-", "-", "向量点数 vs 6_rag_chunks",
                f"{db_chunks}", f"{vector_count}", "GET /api/vector-index",
                "第三真相漂移：切片边界变了要按 file_path 先删后灌",
            ))
    except Exception as error:  # 内嵌 Qdrant 被服务独占是正常情况
        out.append(finding(
            "L3 异常", "P3", "(全库)", "-", "-", "向量点数", "可读", f"读不到：{type(error).__name__}",
            "停服后跑，或看 /api/vector-index", "占用导致的不可读不算缺陷",
        ))

    # 9) 校验结论生命周期 + 重复导入（实测教训见 test_validation_rows_lifecycle.py 的 docstring）
    #    「有事实但一条结论都没有」= 那次写入把结论删光没回填；「无事实却有结论」= 陈旧行；
    #    两者都会让 /api/validation 与库里的数字互相矛盾，是「看着像 bug」的主要来源。
    gap = data.query_all(
        """
        SELECT f.stock_code, f.report_period, COUNT(v.stock_code) AS n FROM (
            SELECT stock_code, report_period FROM `2_performance_report`
            UNION SELECT stock_code, report_period FROM `3_income_statement`
            UNION SELECT stock_code, report_period FROM `4_balance_sheet`
            UNION SELECT stock_code, report_period FROM `5_cash_flow_statement`
        ) f
        LEFT JOIN `9_validation_results` v
          ON v.stock_code = f.stock_code AND v.report_period = f.report_period
        GROUP BY f.stock_code, f.report_period
        HAVING n = 0
        ORDER BY f.stock_code, f.report_period
        """
    )
    if gap:
        out.append(finding(
            "L3 异常", "P2", "(全库)", "-", "-", "校验结论覆盖", "每个事实 (代码,期间) 都有结论",
            f"{len(gap)} 组一条结论都没有：" + "、".join(f"{r['stock_code']} {r['report_period']}" for r in gap[:12]),
            "SELECT … UNION 四张事实表 LEFT JOIN 9_validation_results GROUP BY … HAVING COUNT(v.stock_code)=0",
            "MySQL.save 里 kept 分支清空 validations 又无条件 DELETE ⇒ 删了不回填；"
            "重导受影响代码即可补上（判据：跑完这 18 个代码该组数应回到 0）",
        ))
    stale = data.query_all(
        """
        SELECT v.stock_code, v.report_period, COUNT(*) AS n FROM `9_validation_results` v
        LEFT JOIN (
            SELECT stock_code, report_period FROM `2_performance_report`
            UNION SELECT stock_code, report_period FROM `3_income_statement`
            UNION SELECT stock_code, report_period FROM `4_balance_sheet`
            UNION SELECT stock_code, report_period FROM `5_cash_flow_statement`
        ) f ON f.stock_code = v.stock_code AND f.report_period = v.report_period
        WHERE f.stock_code IS NULL
        GROUP BY v.stock_code, v.report_period
        """
    )
    if stale:
        out.append(finding(
            "L3 异常", "P2", "(全库)", "-", "-", "陈旧校验结论", "0 行",
            f"{sum(int(r['n']) for r in stale)} 行：" + "、".join(
                f"{r['stock_code']} {r['report_period']}×{r['n']}" for r in stale[:12]),
            "SELECT … FROM 9_validation_results LEFT JOIN (四张事实表 UNION) … WHERE f.stock_code IS NULL",
            "描述的数字已不在库里 ⇒ data.prune_orphan_validation_results() 应把它清掉（批量导入收尾已接线）",
        ))
    dup = data.query_all(
        "SELECT stock_code, report_period, file_name, COUNT(*) AS n FROM `7_document_index` "
        "GROUP BY stock_code, report_period, file_name HAVING COUNT(*) > 1 "
        "ORDER BY COUNT(*) DESC"
    )
    if dup:
        out.append(finding(
            "L3 异常", "P2", "、".join(str(r["file_name"]) for r in dup[:3]), "-", "-", "重复导入文档",
            "同 (代码,期间,文件名) 只留最完整的一份",
            f"{len(dup)} 组、多余 {sum(int(r['n']) - 1 for r in dup)} 行：" + "、".join(
                f"{r['stock_code']}×{r['n']}" for r in dup[:8]),
            "SELECT stock_code, report_period, file_name, COUNT(*) FROM 7_document_index "
            "GROUP BY 1,2,3 HAVING COUNT(*)>1",
            "同一份报告被反复上传（多数是截断件）⇒ _tools\\dedupe_documents.py 按「切片最多」保留一份",
        ))
    # 10) 文档行必须有原文（实测事故：`POST /api/import/start {}` 曾把 25 份已入库原文删掉）
    missing_files = [
        row
        for row in data.query_all("SELECT id AS i, file_name AS n, file_path AS p FROM `7_document_index`")
        if not Path(str(row["p"])).exists()
    ]
    if missing_files:
        out.append(finding(
            "L3 异常", "P0", "、".join(str(r["n"]) for r in missing_files[:3]), "-", "-", "文档原文丢失",
            "0 行的 file_path 指向不存在的文件",
            f"{len(missing_files)} 行：file_path 在盘上不存在",
            "SELECT id, file_name, file_path FROM 7_document_index（在 python 里逐条 exists 判定）",
            "点开文档报「文件不存在」、也没法重导 ⇒ 恢复：_tools\\restore_missing_uploads.py（按备份的"
            "文件名+sha256 复核）；防再犯：_tools\\dedupe_documents.py 不搬文件、"
            "test_import_delete_safety.py 钉住「删之前先查引用」",
        ))
    return out


# ------------------------------------------------------------------ L4 边界
def layer_boundary() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in LAYER_SAMPLES["boundary"]:
        doc = docs_of(code)
        perf = latest_row("2_performance_report", code)
        if not perf:
            continue
        revenue = as_dec(perf.get("revenue"))
        parent = as_dec(perf.get("net_profit_parent"))
        # 单位声明 vs 量级：切片里有「单位：万元/千元」的公司，营收若 <1e8 多半漏乘
        hint_rows = data.query_all(
            "SELECT content FROM `6_rag_chunks` WHERE stock_code=%s AND content REGEXP %s LIMIT 3",
            (code, "单位[：:]|Unit[ ]?[：:.]?[ ]?RMB"),
        )
        if hint_rows and revenue is not None and abs(revenue) < Decimal("100000000"):
            out.append(finding(
                "L4 边界", "P1", doc, code, str(perf["report_period"]), "revenue 量级",
                "带单位声明的报表营收通常 ≥ 1e8 元", str(revenue),
                f"SELECT content FROM `6_rag_chunks` WHERE stock_code='{code}' LIMIT 3",
                "单位声明行与金额行不同行时倍数丢失（历史：001227 千元、600085 万元、600115 百万元）",
            ))
        # 极小值：营收为正但归母绝对值 < 1e5，量级不像真数
        if revenue is not None and abs(revenue) >= Decimal("100000000"):
            if parent is not None and abs(parent) < Decimal("100000"):
                out.append(finding(
                    "L4 边界", "P1", doc, code, str(perf["report_period"]), "net_profit_parent",
                    "营收上亿时归母不应是个位/千位量级", str(parent),
                    f"SELECT * FROM `2_performance_report` WHERE stock_code='{code}'",
                    "取了条目号/页码/比率当金额（历史：1.00、1000.00、6000.00）",
                ))
        # 恒等式反解只该在真错位时触发：不平且比值落在 1e3/1e4/1e6 ⇒ 报告
        bal = latest_row("4_balance_sheet", code)
        if bal:
            assets = as_dec(bal.get("total_assets"))
            liab = as_dec(bal.get("total_liability"))
            equity = as_dec(bal.get("total_equity"))
            if assets and liab is not None and equity is not None and (liab + equity) != 0:
                ratio = assets / (liab + equity)
                if any(abs(Decimal(int(k)) - ratio) / Decimal(int(k)) <= Decimal("0.01")
                       for k in (1000, 10000, 1000000)):
                    out.append(finding(
                        "L4 边界", "P1", doc, code, str(bal["report_period"]),
                        "assets/(liability+equity)", "≈1（反解应已把负债/权益换算回元）",
                        f"{ratio:.4f}", f"SELECT * FROM `4_balance_sheet` WHERE stock_code='{code}'",
                        "恒等式还差整千/万/百万倍 ⇒ recover_balance_sheet_scale 没生效",
                    ))
        # 现金流四段之和 vs 净增加额：经营 + 投资 + 筹资 + 汇率变动 = 净增加额。
        # 汇率段（fx_cash）以前没入库 ⇒ 三列和对不上净增加额被误报成解析缺陷
        # （实测 600031 差 -768,603,000、000525 差 -2,302,037.33 都恰好等于汇率项）。
        cash = latest_row("5_cash_flow_statement", code)
        if cash:
            parts = [as_dec(cash.get(col)) for col in ("operate_cash", "invest_cash", "finance_cash")]
            fx = as_dec(cash.get("fx_cash"))
            if fx is not None:
                parts.append(fx)
            total = as_dec(cash.get("cash_increase"))
            if total and all(p is not None for p in parts):
                diff = abs(sum(parts) - total) / max(abs(total), Decimal(1))
                if diff > Decimal("0.05"):
                    out.append(finding(
                        "L4 边界", "P2", doc, code, str(cash["report_period"]),
                        "经营+投资+筹资(+汇率) vs 现金净增加额", "四段之和应等于净增加额",
                        f"和 {sum(parts)} vs 净增加 {total}（差 {diff:.1%}）",
                        f"SELECT * FROM `5_cash_flow_statement` WHERE stock_code='{code}'",
                        "差额过大 ⇒ 漏收某一段（常见是页断把「汇率变动」标签截断）或取错列/错行",
                    ))
    return out


LAYERS = {
    "L1 核心报表勾稽": layer_core_reports,
    "L2 格式": layer_format,
    "L3 异常": layer_anomaly,
    "L4 边界": layer_boundary,
}
