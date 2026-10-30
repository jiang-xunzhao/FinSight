r"""常驻回归件：校验结论（`9_validation_results`）的生命周期。

历史 bug（实测取证，不是猜的）：

1. **摘要盖完整版那次，整组结论被删光再不回填 ⇒ 18 个事实行没有任何校验记录**
   `MySQL.save` 里 richer-row 护栏（`_write_statement_row` 返回 `kept`）会把 `validations`
   清空（行没重写，就不许把描述上一份（更全）文档的结论换成这一份（更稀疏）文档的结论），
   但紧接的 `DELETE FROM 9_validation_results WHERE stock_code=%s AND report_period=%s`
   **原来是无条件执行** ⇒ kept 那次「删了但不回填」。
   实测后果（去重前的库）：`9_validation_results` 只有 **328 行 / 36 主体**，而事实行有
   52/52/50/44；「有事实但无校验结论」的 (代码, 报告期) 组 **18 个**
   （`000010 000100 000625 000801 001227 002048 002531 002571 002595 300539 300600
   600007 600031 600061 600085 600155 600252 600325`，全是 2026 半年报）。
   修法：DELETE 移进 `if validations:` 分支（与 INSERT 同一事务），另加
   `elif not kept_tables:` 保留「这批数字压根没算出结论（非定期公告）⇒ 旧结论一起作废」的原语义。

2. **陈旧结论让 `/api/validation` 显示与当前数据矛盾的「异常」**
   重导只清四张事实表、**不清 `9_validation_results`** ⇒ 留下实测 **56 行**孤儿：
   `002030 2030年半年报` ×3（解析层年份上限修复之前造出来的错期间，事实行早没了）、
   `FILE_4080B9E7 / FILE_E664CE98 2026年年报`（`现金流量净额勾稽 异常`/`现金余额勾稽 异常`
   等，身份已归并改名后的旧结论）。
   修法：`prune_orphan_validation_results()`（判据=四张事实表里一张都没有这个
   (代码, 报告期) ⇒ 这条结论描述的数字不存在），接进批量导入收尾。

本件断言（建一次性库 `finance_report_vallc`，跑完删掉，不碰真库）：
①写全行 + 3 条结论 ⇒ 3 行；②再写稀疏行（触发 kept）⇒ **结论必须还是那 3 条、不许变 0**；
③写全空（非定期）⇒ 旧结论一起作废；④孤儿结论被 `prune_orphan_validation_results()` 删掉、
有事实的结论不许动；⑤源码级护栏：`kept` 分支之后不得再出现**无条件** DELETE。
"""
from __future__ import annotations

import inspect
import re
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层"):
    sys.path.insert(0, str(ROOT / sub))

import pymysql  # noqa: E402

import data  # noqa: E402

SCRATCH = "finance_report_vallc"
CODE = "600999"
PERIOD = "2026年半年报"
failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def admin_cursor():
    cfg = dict(data.db_config())
    cfg.pop("database", None)
    cfg["cursorclass"] = pymysql.cursors.Cursor
    conn = pymysql.connect(**cfg)
    return conn, conn.cursor()


def raw(sql: str, params: tuple = ()) -> list[tuple]:
    conn = pymysql.connect(**{**data.db_config(database=True), "database": SCRATCH,
                              "cursorclass": pymysql.cursors.Cursor})
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def write(sql: str, params: tuple = ()) -> None:
    conn = pymysql.connect(**{**data.db_config(database=True), "database": SCRATCH,
                              "cursorclass": pymysql.cursors.Cursor})
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def value(code: str, name: str, statement: str, current, previous=None) -> data.StatementValue:
    return data.StatementValue(
        metric_code=code,
        metric_name=name,
        statement_type=statement,
        current=Decimal(str(current)),
        previous=Decimal(str(previous)) if previous is not None else None,
        current_raw=str(current),
        previous_raw=str(previous) if previous is not None else None,
        page_no=1,
        row_index=0,
        confidence=Decimal("0.90"),
    )


def rich_values() -> dict[str, data.StatementValue]:
    return {
        "operating_revenue": value("operating_revenue", "营业收入", "income", 2000, 1000),
        "operating_cost": value("operating_cost", "营业成本", "income", 1200, 700),
        "operating_profit": value("operating_profit", "营业利润", "income", 400, 200),
        "total_profit": value("total_profit", "利润总额", "income", 380, 190),
        "net_profit": value("net_profit", "净利润", "income", 300, 100),
        "parent_net_profit": value("parent_net_profit", "归属于母公司股东的净利润", "income", 250, 90),
        "basic_eps": value("basic_eps", "基本每股收益", "income", 0.5, 0.2),
        "total_assets": value("total_assets", "资产总计", "balance", 9000, 8000),
        "total_liabilities": value("total_liabilities", "负债合计", "balance", 5000, 4500),
        "total_equity": value("total_equity", "所有者权益合计", "balance", 4000, 3500),
        "current_assets": value("current_assets", "流动资产合计", "balance", 6000, 5500),
        "current_liabilities": value("current_liabilities", "流动负债合计", "balance", 3000, 2800),
        "net_cash_flow_operating": value("net_cash_flow_operating", "经营活动现金流量净额", "cash", 500, 400),
        "net_cash_flow_investing": value("net_cash_flow_investing", "投资活动现金流量净额", "cash", -300, -200),
        "net_cash_flow_financing": value("net_cash_flow_financing", "筹资活动现金流量净额", "cash", -100, 50),
        "exchange_rate_effect_cash": value("exchange_rate_effect_cash", "汇率变动对现金的影响", "cash", -10, -5),
        "net_increase_cash": value("net_increase_cash", "现金及现金等价物净增加额", "cash", 90, 245),
    }


def sparse_values() -> dict[str, data.StatementValue]:
    return {
        "operating_revenue": value("operating_revenue", "营业收入", "income", 2000, 1000),
        "net_profit": value("net_profit", "净利润", "income", 300, 100),
    }


def validations(tag: str) -> list[dict[str, str]]:
    return [
        {"name": f"资产等于负债和所有者权益_{tag}", "status": "通过", "message": f"{tag} 勾稽一致"},
        {"name": f"利润表净利润勾稽_{tag}", "status": "通过", "message": f"{tag} 净利润可得"},
        {"name": f"现金流余额勾稽_{tag}", "status": "待补充", "message": f"{tag} 缺期初现金"},
    ]


def rules_of_that_period() -> list[str]:
    return sorted(r[0] for r in raw(
        "SELECT rule_name FROM `9_validation_results` WHERE stock_code=%s AND report_period=%s",
        (CODE, PERIOD),
    ))


def main() -> int:
    conn, cur = admin_cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
    cur.execute(f"CREATE DATABASE `{SCRATCH}` CHARACTER SET utf8mb4")
    cur.close()
    conn.close()

    original_db = data.CONFIG["mysql_database"]
    data.CONFIG["mysql_database"] = SCRATCH
    tmp_dir = Path(tempfile.mkdtemp(prefix="vallc_"))
    try:
        db = data.MySQL()
        db.config = {**db.config, "database": SCRATCH}
        db.init_database()

        company = {"stock_code": CODE, "company_name": "招商证券股份有限公司",
                   "industry_name": "证券", "list_date": None}
        metadata = {"period_type": "semiannual", "period_start": date(2026, 1, 1),
                    "period_end": date(2026, 6, 30)}

        # ① 全行 + 3 条结论
        full_path = tmp_dir / "招商证券股份有限公司_2026年半年报.pdf"
        full_path.write_bytes(b"%PDF-1.4 fake")
        db.save(full_path, 120, company, metadata, rich_values(), validations("全文"), [])
        check("① 写全行后结论行数", len(rules_of_that_period()), 3)
        check("① 结论描述的是全文那批",
              all("全文" in r for r in rules_of_that_period()), True)

        # ② 稀疏文档盖上来 ⇒ kept ⇒ 结论必须原样保住（修复前这里变成 0 行）
        sparse_path = tmp_dir / "招商证券股份有限公司_2026年半年报_摘要.pdf"
        sparse_path.write_bytes(b"%PDF-1.4 fake")
        db.save(sparse_path, 4, company, metadata, sparse_values(), validations("摘要"), [])
        check("② kept 之后结论条数不变（修复前=0）", len(rules_of_that_period()), 3)
        check("② 保住的是全文那批、不是摘要那批",
              all("全文" in r for r in rules_of_that_period()), True)

        # ③ 非定期公告（一个数都没有）⇒ 没 kept 也没结论 ⇒ 旧结论一起作废
        empty_values: dict[str, data.StatementValue] = {}
        none_path = tmp_dir / "招商证券股份有限公司_重大事项公告.pdf"
        none_path.write_bytes(b"%PDF-1.4 fake")
        db.save(none_path, 2, company, metadata, empty_values, [], [])
        check("③ 全空且不 kept ⇒ 旧结论作废", len(rules_of_that_period()), 0)

        # ④ 孤儿结论：描述的数字已不在库 ⇒ 必须被清掉；有事实的结论不许动
        write(
            "INSERT INTO `9_validation_results` (stock_code, report_period, rule_name, status, message) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("600001", "2030年半年报", "资产等于负债和所有者权益", "异常", "陈旧行：期间是解析修复前的错值"),
        )
        write(
            "INSERT INTO `9_validation_results` (stock_code, report_period, rule_name, status, message) "
            "VALUES (%s, %s, %s, %s, %s)",
            (CODE, PERIOD, "利润表净利润勾稽", "通过", "有事实支撑"),
        )
        deleted = data.prune_orphan_validation_results()
        check("④ 清掉的孤儿行数", deleted, 1)
        left = raw("SELECT stock_code, report_period FROM `9_validation_results`")
        check("④ 有事实的结论保住", (CODE, PERIOD) in {tuple(r) for r in left}, True)
        check("④ 再跑一次是幂等的", data.prune_orphan_validation_results(), 0)

        # ⑤ 源码级护栏：kept 分支之后不得再出现无条件 DELETE
        src = inspect.getsource(data.MySQL.save)
        check("⑤ DELETE 只在两处（有结论时 / 全空且未 kept 时）",
              len(re.findall(r"DELETE FROM 9_validation_results", src)), 2)
        check("⑤ 存在 elif not kept_tables 分支", "elif not kept_tables:" in src, True)
        check("⑤ 结论删除与插入同分支（先删后插都在 `if validations:` 里）",
              re.search(r"if validations:\s*\n\s*cursor\.execute\(\s*\"DELETE FROM 9_validation_results", src) is not None,
              True)
    finally:
        data.CONFIG["mysql_database"] = original_db
        conn, cur = admin_cursor()
        cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
        cur.execute("COMMIT")
        cur.close()
        conn.close()

    print("\n" + ("✅ 校验结论与事实行同进同退、陈旧行可清且幂等" if not failures else f"❌ 未通过：{failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
