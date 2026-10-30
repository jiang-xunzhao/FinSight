"""回归件：同一 (公司, 报告期) 有多份文档时，**信息更全的那一行赢**，与导入顺序无关。

背景：摘要 PDF 只有六个指标、全文 PDF 有二十多个，旧的先删后插等于「谁后处理谁赢」。
跑法：<venv python> test_richer_row_wins.py   （要建一次性库 finance_report_richer，跑完删掉）
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import pymysql  # noqa: E402

import data  # noqa: E402

SCRATCH = "finance_report_richer"
failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def admin_cursor(db: str):
    cfg = dict(data.db_config())
    cfg.pop("database", None)
    cfg["cursorclass"] = pymysql.cursors.Cursor
    conn = pymysql.connect(**cfg)
    return conn, conn.cursor()


def filled_of(db, table: str, code: str, period: str) -> int:
    cfg = dict(data.db_config())
    cfg["database"] = db
    cfg["cursorclass"] = pymysql.cursors.Cursor
    conn = pymysql.connect(**cfg)
    try:
        with conn.cursor() as cur:
            cols = " + ".join(f"COUNT(`{c}`)" for c in data.MySQL.STATEMENT_COLUMNS[table])
            cur.execute(
                f"SELECT {cols} FROM `{table}` WHERE stock_code=%s AND report_period=%s",  # noqa: S608
                (code, period),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        conn.close()


def main() -> int:
    conn, cur = admin_cursor(SCRATCH)
    cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
    cur.execute(f"CREATE DATABASE `{SCRATCH}` CHARACTER SET utf8mb4")
    cur.close()
    conn.close()

    original_db = data.CONFIG["mysql_database"]
    data.CONFIG["mysql_database"] = SCRATCH
    try:
        db = data.MySQL()
        db.config["database"] = SCRATCH
        db.init_database()

        code, period = "600999", "2026年半年报"
        sparse = (Decimal("1000"), Decimal("100"), None, None, None, None)
        rich = (Decimal("1000"), Decimal("100"), Decimal("90"), Decimal("0.5"), Decimal("12"), Decimal("8"))

        conn = pymysql.connect(**db.config)
        try:
            with conn.cursor() as cursor:
                # 先灌稀疏行
                check("空表写稀疏行 → written",
                      db._write_statement_row(cursor, "2_performance_report", code, period, sparse, "摘要.pdf"),
                      "written")
                conn.commit()
                check("稀疏行落库字段数", filled_of(SCRATCH, "2_performance_report", code, period), 2)

                # 再灌更全的行：必须赢
                check("稀疏之后写更全 → written（新的赢）",
                      db._write_statement_row(cursor, "2_performance_report", code, period, rich, "全文.pdf"),
                      "written")
                conn.commit()
                check("字段数升到 6", filled_of(SCRATCH, "2_performance_report", code, period), 6)

                # 反过来：已有更全的行时，稀疏文档不许把它删掉
                check("更全之后写稀疏 → kept（原行保住）",
                      db._write_statement_row(cursor, "2_performance_report", code, period, sparse, "摘要.pdf"),
                      "kept")
                conn.commit()
                check("字段数仍是 6（没被删成 2）", filled_of(SCRATCH, "2_performance_report", code, period), 6)

                # 全是空值：不动任何东西
                check("整行没数 → empty",
                      db._write_statement_row(cursor, "2_performance_report", code, period, (None,) * 6, "空.pdf"),
                      "empty")
                conn.commit()
                check("字段数仍是 6", filled_of(SCRATCH, "2_performance_report", code, period), 6)

                # 四张表都要走同一套。
                # 元组长度**必须从 STATEMENT_COLUMNS 现算**，不许写死数字：
                # 加一列（fx_cash）时写死的件会直接 TypeError: not enough arguments for
                # format string，把「产品没问题、测试件过期」伪装成产品缺陷。
                for table in ("3_income_statement", "4_balance_sheet", "5_cash_flow_statement"):
                    arity = len(db.STATEMENT_COLUMNS[table])
                    check(f"{table} 可写（{arity} 列）",
                          db._write_statement_row(cursor, table, code, period, (Decimal("1"),) * arity, "全文.pdf"),
                          "written")
                conn.commit()
        finally:
            conn.close()

        # 等值重写（同一份文档重导入）必须仍然覆盖，不能变成 kept
        conn = pymysql.connect(**db.config)
        try:
            with conn.cursor() as cursor:
                check("同等信息量重导入 → written（不幂等卡死）",
                      db._write_statement_row(cursor, "2_performance_report", code, period, rich, "全文.pdf"),
                      "written")
                conn.commit()
        finally:
            conn.close()
    finally:
        data.CONFIG["mysql_database"] = original_db
        conn, cur = admin_cursor(SCRATCH)
        cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
        cur.execute("COMMIT")
        cur.close()
        conn.close()

    print("\n" + ("✅ 多份文档谁赢已跟信息量挂钩、与顺序无关" if not failures else f"❌ 未通过：{failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
