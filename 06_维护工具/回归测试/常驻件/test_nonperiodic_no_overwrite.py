"""非定期文档（临时公告/重组件/审计报告）不许顶掉已有的定期事实。

背景（本轮实测过的真实形态）：920833 名下 27 份、600999 名下 15 份文件名都叫
「_2026年半年报」，正文其实全是重大资产购买重组件（独立财务顾问报告 / 资产评估报告 /
法律意见书 / 备考合并财务报表 / 董事会决议）。解析层用 `looks_like_periodic_report`
判型后把 values 清空，于是落到 `MySQL.save()` 的是一份「有身份、没数字」的文档。

危险点：`MySQL.save()` 是 **先 DELETE 再 INSERT** 的破坏性写库。如果清空 values
之后仍然执行删除，那么一份重组件就会把该公司同报告期真正来自定期报告的数字全删掉
（而且是「删了再也回不来」，因为删的是主键组合）。本件就是钉住这条边界。

用法：python test_nonperiodic_no_overwrite.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

for _sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / _sub))

import pymysql  # noqa: E402
import data  # noqa: E402

SCRATCH = "finance_report_nonperiodic"
PDF = Path("重大资产购买报告书（草案）.pdf")
FAILS: list[str] = []

GOOD_NAME = data.COMPANY_MASTER.get("600999", "招商证券股份有限公司")


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


RICH_VALUES = {
    "operating_revenue": sv("operating_revenue", "营业收入", "1234567.89", "1100000.00"),
    "net_profit": sv("net_profit", "净利润", "200000.00", "180000.00"),
    "parent_net_profit": sv("parent_net_profit", "归属于母公司股东的净利润", "190000.00", "170000.00"),
    "basic_eps": sv("basic_eps", "基本每股收益", "0.16", "0.14"),
    "operating_cost": sv("operating_cost", "营业成本", "900000.00", "850000.00"),
}


def call_save(db: data.MySQL, values: dict[str, data.StatementValue]) -> tuple[int, int]:
    return db.save(
        path=PDF,
        page_count=42,
        company={
            "stock_code": "600999",
            "company_name": GOOD_NAME,
            "industry_name": "证券",
            "list_date": None,
        },
        metadata={
            "period_type": "semiannual",
            "period_start": "2026-01-01",
            "period_end": "2026-06-30",
        },
        values=dict(values),
        validations=[],
        tables=[],
    )


TABLES = (
    "2_performance_report",
    "3_income_statement",
    "4_balance_sheet",
    "5_cash_flow_statement",
)


def snapshot(conn_cfg: dict) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    with pymysql.connect(**{**conn_cfg, "cursorclass": pymysql.cursors.Cursor}) as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"SELECT * FROM `{table}` ORDER BY stock_code, report_period")
                out[table] = list(cur.fetchall())
            cur.execute("SELECT stock_code, stock_name FROM `1_stock_basic` ORDER BY stock_code")
            out["1_stock_basic"] = list(cur.fetchall())
    return out


def main() -> int:
    # ---------------------------------------------------- 1) 判型函数本身
    print("### 1) 判型：重组件/审计报告必须判为非定期")
    cases = [
        ("独立财务顾问报告（重组）",
         "招商证券股份有限公司关于重庆美心翼申机械股份有限公司重大资产购买之独立财务顾问报告", False),
        ("备考合并财务报表",
         "备考合并财务报表 单位：万元 交易前 交易后 变动比率", False),
        ("审计报告",
         "泰州鑫宇精工股份有限公司 审计报告 信会师报字[2026]第 ZH50196 号", False),
        ("董事会决议公告",
         "第 X 届董事会第 X 次会议决议公告 暂不召开股东会", False),
        ("真半年报",
         "重庆美心翼申机械股份有限公司 2026 年半年度报告", True),
        ("中国广核那种「境内同步披露公告」抬头 + 正文是半年报",
         "境内同步披露公告\n中国广核电力股份有限公司 2026 年半年度报告", True),
    ]
    for label, head, expect_periodic in cases:
        got = data.looks_like_periodic_report(head, "")
        want = "定期" if expect_periodic else "非定期"
        check(f"判型应为{want}：{label}", got is expect_periodic, f"got={got}")

    # ---------------------------------------------------- 2) 空 values 不许删旧行
    print("\n### 2) 临时库真写：非定期文档（values 空）不得删掉定期事实")
    db = data.MySQL()
    base_cfg = dict(db.config)
    with pymysql.connect(**{k: v for k, v in base_cfg.items() if k != "database"}) as probe:
        with probe.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")
            cur.execute(
                f"CREATE DATABASE `{SCRATCH}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    original_db = data.CONFIG["mysql_database"]
    data.CONFIG["mysql_database"] = SCRATCH
    try:
        db.config["database"] = SCRATCH
        db.init_database()
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

        # 2a 先写一份「定期报告」的完整事实
        got_rich = call_save(db, RICH_VALUES)
        after_rich = snapshot(db.config)
        rich_rows = sum(len(after_rich[t]) for t in TABLES)
        # 注意：`MySQL.save()` 成功时固定返回 (0, 0)（真正的隔离信号只有闸门的 (-1, -1)），
        # 所以这里判「不是隔离信号 + 真的落了行」，不能判 company_id>0。
        check(
            "定期报告写入成功（未被闸门拦下，且落了行）",
            got_rich != (-1, -1) and rich_rows > 0,
            f"{got_rich} rows={rich_rows}",
        )

        # 2b 再写同一 (公司, 报告期) 的「非定期文档」：values 已被判型清空
        got_empty = call_save(db, {})
        after_empty = snapshot(db.config)
        same = all(after_empty[t] == after_rich[t] for t in TABLES)
        check(
            "非定期文档：四张事实表逐字节没变（旧数字没被删）",
            same,
            f"{ {t: (len(after_rich[t]), len(after_empty[t])) for t in TABLES} }",
        )
        check(
            "非定期文档：公司名也没被改写",
            after_empty["1_stock_basic"] == after_rich["1_stock_basic"],
            f"{after_empty['1_stock_basic']}",
        )
        check(
            "非定期文档：返回的不是隔离信号（它照样登记、照样可检索）",
            got_empty[0] >= 0 and got_empty != (-1, -1),
            f"{got_empty}",
        )

        # 2c 反证：如果判型失效（把重组件当定期）且真解析出数字，就会被覆盖 —— 说明第 1 段判型是唯一的防线
        mutated = dict(RICH_VALUES)
        mutated["operating_revenue"] = sv("operating_revenue", "营业收入", "990000.00", "1100000.00")
        call_save(db, mutated)
        after_mutated = snapshot(db.config)
        check(
            "对照：带数字的写入确实会改写同键旧行（证明 2b 的不变不是假象）",
            after_mutated["2_performance_report"] != after_rich["2_performance_report"],
            "",
        )
    finally:
        data.CONFIG["mysql_database"] = original_db
        with pymysql.connect(**{k: v for k, v in base_cfg.items() if k != "database"}) as cleanup:
            with cleanup.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH}`")

    print("\n" + ("❌ 有 %d 项不符合预期" % len(FAILS) if FAILS else "✅ 全部符合预期"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
