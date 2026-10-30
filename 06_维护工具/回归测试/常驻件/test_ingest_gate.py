"""入库闸门（MySQL.save 写库前的唯一拒绝点）的常驻回归件。

分两层：
1) 纯函数层：`_ingest_reject_reason` 的判定口径（未来报告期 / 代码↔名串号 / 放行）。
2) 写库层：在一次性临时库 `finance_report_gate` 里真跑 `MySQL.save()`，证明
   「不合格的那次一个字节都没写、旧行原样保留」——这才是闸门的真正价值
   （旧行为是先 DELETE 再 INSERT，一次盖错就永远删不到）。

用法：python test_ingest_gate.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

for _sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / _sub))

import pymysql  # noqa: E402
import data  # noqa: E402

SCRATCH = "finance_report_gate"
FAILS: list[str] = []
PDF = Path("样例半年报.pdf")


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"：{extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


def sv(code: str, name: str, current: str, previous: str | None = None) -> data.StatementValue:
    return data.StatementValue(
        metric_code=code,
        metric_name=name,
        statement_type="income",
        current=Decimal(current),
        previous=Decimal(previous) if previous else None,
        current_raw=current,
        previous_raw=previous,
        page_no=3,
        row_index=1,
        confidence=Decimal("0.9"),
    )


VALUES = {
    "operating_revenue": sv("operating_revenue", "营业收入", "1234567.89", "1100000.00"),
    "net_profit": sv("net_profit", "净利润", "200000.00", "180000.00"),
    "parent_net_profit": sv("parent_net_profit", "归属于母公司股东的净利润", "190000.00", "170000.00"),
    "basic_eps": sv("basic_eps", "基本每股收益", "0.16", "0.14"),
    "operating_cost": sv("operating_cost", "营业成本", "900000.00", "850000.00"),
}


def call_save(db: data.MySQL, stock_code: str, company_name: str, period_end: str) -> tuple[int, int]:
    return db.save(
        path=PDF,
        page_count=42,
        company={
            "stock_code": stock_code,
            "company_name": company_name,
            "industry_name": "证券",
            "list_date": None,
        },
        metadata={
            "period_type": "semiannual",
            "period_start": f"{period_end[:4]}-01-01",
            "period_end": period_end,
        },
        values=dict(VALUES),
        validations=[],
        tables=[],
    )


# ---------------------------------------------------------------- 1) 纯函数层
db = data.MySQL()
# 用主数据里的规范全称，避免我手写的名字和 COMPANY_MASTER 播种值对不上
GOOD_NAME = data.COMPANY_MASTER.get("600999", "招商证券股份有限公司")
cases = [
    ("未来报告期要拦", ("002030", "广州达安基因股份有限公司", "2030年半年报"), True),
    ("代码已归属别人要拦", ("600999", "广州达安基因股份有限公司", "2026年半年报"), True),
    ("代码与名一致要放行", ("600999", GOOD_NAME, "2026年半年报"), False),
    ("FILE_ 占位码不拦（没有权威名可比）", ("FILE_F1D40D62", "某未知公司股份有限公司", "2026年半年报"), False),
    ("库里没见过的新代码不拦", ("888888", "全新上市公司股份有限公司", "2026年半年报"), False),
    ("报告期没有年份不拦", ("600999", GOOD_NAME, "年半年报"), False),
]
print("### 1) _ingest_reject_reason 判定口径")
for label, args, expect_reject in cases:
    reason = db._ingest_reject_reason(*args)
    check(label, bool(reason) is expect_reject, f"reason={reason or '（放行）'}")

# ---------------------------------------------------------------- 2) 写库层
print("\n### 2) 临时库真写：不合格不许动旧行")
base_cfg = dict(db.config)
with pymysql.connect(**{k: v for k, v in base_cfg.items() if k != "database"}) as probe:
    with probe.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
        cur.execute(
            f"CREATE DATABASE `{SCRATCH}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
data.CONFIG["mysql_database"] = SCRATCH
try:
    db.config["database"] = SCRATCH
    db.init_database()
    # init_database() 已经跑过 seed_company_master（COMPANY_MASTER 里就有 600999），
    # 所以这里必须是 INSERT IGNORE；再显式 UPDATE 一次，保证断言用的名字与播种值一致。
    with pymysql.connect(**{**db.config, "cursorclass": pymysql.cursors.Cursor}) as seed:
        with seed.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO `1_stock_basic` (stock_code, stock_name) VALUES (%s, %s)",
                ("600999", GOOD_NAME),
            )
            cur.execute(
                "UPDATE `1_stock_basic` SET stock_name = %s WHERE stock_code = %s",
                (GOOD_NAME, "600999"),
            )
        seed.commit()

    # 2a 未来报告期：必须 0 行
    got = call_save(db, "002030", "广州达安基因股份有限公司", "2030-06-30")
    with pymysql.connect(**{**db.config, "cursorclass": pymysql.cursors.Cursor}) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM `2_performance_report`")
            n_future = cur.fetchone()[0]
    check("未来报告期：返回拒绝信号且没写业绩行", got == (-1, -1) and n_future == 0, f"{got} rows={n_future}")

    # 2b 正常入库
    got_ok = call_save(db, "600999", GOOD_NAME, "2026-06-30")
    with pymysql.connect(**{**db.config, "cursorclass": pymysql.cursors.Cursor}) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, report_period, revenue, net_profit, eps, revenue_yoy, profit_yoy "
                "FROM `2_performance_report` ORDER BY stock_code, report_period"
            )
            rows_after_good = cur.fetchall()
            cur.execute("SELECT stock_code, stock_name FROM `1_stock_basic` ORDER BY stock_code")
            basic_after_good = cur.fetchall()
    check("正常报告期：写进去了", len(rows_after_good) == 1, f"{got_ok} rows={rows_after_good}")

    # 2c 串号：必须被拒，且旧行一个字节都不许变
    got_bad = call_save(db, "600999", "广州达安基因股份有限公司", "2026-06-30")
    with pymysql.connect(**{**db.config, "cursorclass": pymysql.cursors.Cursor}) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, report_period, revenue, net_profit, eps, revenue_yoy, profit_yoy "
                "FROM `2_performance_report` ORDER BY stock_code, report_period"
            )
            rows_after_bad = cur.fetchall()
            cur.execute("SELECT stock_code, stock_name FROM `1_stock_basic` ORDER BY stock_code")
            basic_after_bad = cur.fetchall()
    check("串号：返回拒绝信号", got_bad == (-1, -1), f"{got_bad}")
    check("串号：业绩行原样保留（旧行为会先删后插把好数据盖掉）",
          rows_after_bad == rows_after_good, f"{rows_after_bad}")
    check("串号：公司名没被脏名覆盖", basic_after_bad == basic_after_good,
          f"{basic_after_bad}")

    # 2d 主数据名优先：脏模板名进来也不许污染 stock_name
    call_save(db, "600999", "CompanynameinChinese招商证券股份有限公司", "2026-06-30")
    with pymysql.connect(**{**db.config, "cursorclass": pymysql.cursors.Cursor}) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stock_name FROM `1_stock_basic` WHERE stock_code = '600999'")
            name_now = cur.fetchone()[0]
    check("脏模板名被清洗且没截断好名", name_now == GOOD_NAME, f"name={name_now}")
    # 2e 被闸门拦下的文档，一页都不许进 RAG —— 否则「MySQL 没有、Qdrant 搜得到」
    probe_pdf = Path(__file__).resolve().parent / "_gate_probe.pdf"
    probe_pdf.write_bytes(b"%PDF-1.4\n")
    calls = {"register": 0, "chunks": 0}
    _real = {name: getattr(data, name) for name in (
        "process_financial_document", "register_document", "build_rag_chunks_from_file",
        "prune_orphan_entities", "find_duplicate",
        "IMPORT_EXECUTOR")}

    class _SyncExecutor:
        def submit(self, fn, *args, **kwargs):
            from concurrent.futures import Future
            future: Future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001
                future.set_exception(exc)
            return future

    try:
        # 轮卅六后 process_one 会带 pages_override=（消二次解析），mock 得接住 kwargs
        data.process_financial_document = lambda path, database, **kw: {"stock_code": "600999",
                                                                        "company_name": "串号公司", "quarantined": True}
        data.register_document = lambda *a, **k: calls.__setitem__("register", calls["register"] + 1)
        data.build_rag_chunks_from_file = lambda *a, **k: calls.__setitem__("chunks", calls["chunks"] + 1)
        data.prune_orphan_entities = lambda: []
        data.find_duplicate = lambda file_hash: None
        data.IMPORT_EXECUTOR = _SyncExecutor()
        job = {"id": "gate-test-job", "status": "running", "total": 1, "processed": 0,
               "success": 0, "failed": 0, "skipped": 0, "errors": [], "session_id": ""}
        with data.IMPORT_JOBS_LOCK:
            data.IMPORT_JOBS["gate-test-job"] = job
        data.run_import_job("gate-test-job", [probe_pdf])
        check("拦下的文档没登记 7_document_index", calls["register"] == 0, f"register={calls['register']}")
        check("拦下的文档没做向量切片", calls["chunks"] == 0, f"chunks={calls['chunks']}")
        check("任务里计为失败并写明原因",
              job["failed"] == 1 and job["success"] == 0 and any("入库闸门已拦下" in e for e in job["errors"]),
              f"failed={job['failed']} success={job['success']} errors={job['errors']}")
    finally:
        with data.IMPORT_JOBS_LOCK:
            data.IMPORT_JOBS.pop("gate-test-job", None)
        for name, value in _real.items():
            setattr(data, name, value)
        probe_pdf.unlink(missing_ok=True)
finally:
    data.CONFIG["mysql_database"] = base_cfg.get("database", "finance_report")
    with pymysql.connect(**{k: v for k, v in base_cfg.items() if k != "database"}) as probe:
        with probe.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
    # 名称缓存指向临时库时读到过空表，恢复到 live 口径
    data._COMPANY_NAME_MAP_STATE["map"] = {}
    data._COMPANY_NAME_MAP_STATE["at"] = 0.0

print()
print("结论：", "入库闸门全部符合预期" if not FAILS else f"{len(FAILS)} 条不符合预期 -> {FAILS}")
sys.exit(0 if not FAILS else 1)
