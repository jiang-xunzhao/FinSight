# -*- coding: utf-8 -*-
"""身份卫生回归：公司名清洗/主数据优先 + ON DUPLICATE 长名保护 + 鬼影主体判定。

跑法（服务可不停，只碰临时库 finance_report_hyg）：
    python _tools\\test_identity_hygiene.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层"):
    sys.path.insert(0, str(ROOT / sub))

import pymysql  # noqa: E402

import data  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SANDBOX = "finance_report_hyg"
FAIL = 0


def check(label: str, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


# ---------- 1. 名字选择逻辑（authoritative_stock_name）----------
print("== 1. authoritative_stock_name ==")
check(
    "英文模板残留被剥掉",
    data.authoritative_stock_name("002493", "CompanynameinChinese荣盛石化股份有限公司"),
    "荣盛石化股份有限公司",
)
check(
    "主数据名字优先于解析名",
    data.authoritative_stock_name("600999", "招商"),
    data.COMPANY_MASTER["600999"],
)
check(
    "解析不出名字时返回空串（不再落脏名）",
    data.authoritative_stock_name("999999", "   "),
    "",
)
check("FILE_ 代码走清洗分支", data.authoritative_stock_name("FILE_ABC", "XX科技股份有限公司"), "XX科技股份有限公司")

# ---------- 2. ON DUPLICATE 长名保护（临时库，逐字复刻 MySQL.save 里的语句）----------
print("== 2. 1_stock_basic 覆盖保护（临时库）==")
cfg = dict(data.db_config())
cfg.pop("database", None)
with pymysql.connect(**cfg) as admin:
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{SANDBOX}`")
        cur.execute(f"CREATE DATABASE `{SANDBOX}` DEFAULT CHARSET utf8mb4")
UPSERT = """
    INSERT INTO `{table}` (stock_code, stock_name, industry, list_date)
    VALUES (%s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE stock_name = IF(
        %s = 1 OR CHAR_LENGTH(VALUES(stock_name)) >= CHAR_LENGTH(stock_name),
        VALUES(stock_name),
        stock_name
    )
"""


def upsert(cur, table: str, code: str, raw_name: str) -> None:
    """复刻 MySQL.save 的写法：先过 authoritative_stock_name，再走同一条 ON DUPLICATE。"""
    cur.execute(
        UPSERT.format(table=table),
        (code, data.authoritative_stock_name(code, raw_name), None, None,
         1 if code in data.COMPANY_MASTER else 0),
    )


def read_name(cur, table: str, code: str) -> str:
    cur.execute(f"SELECT stock_name FROM `{table}` WHERE stock_code=%s", (code,))
    row = cur.fetchone() or {}
    return str(row.get("stock_name") or "")


with pymysql.connect(**cfg, database=SANDBOX) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE `t_basic` (`stock_code` varchar(32) NOT NULL, "
            "`stock_name` varchar(255) DEFAULT NULL, `industry` varchar(64) DEFAULT NULL, "
            "`list_date` date DEFAULT NULL, PRIMARY KEY (`stock_code`)) "
            "DEFAULT CHARSET=utf8mb4"
        )
        # 坏名先入库，再看清洗后的名字能否修好它
        upsert(cur, "t_basic", "002493", "CompanynameinChinese荣盛石化股份有限公司")
        check("脏名被清洗后写进去", read_name(cur, "t_basic", "002493"), "荣盛石化股份有限公司")
        # 更短的截断变体不许覆盖好名
        upsert(cur, "t_basic", "002493", "科技集团")
        check("短变体不覆盖长好名", read_name(cur, "t_basic", "002493"), "荣盛石化股份有限公司")
        # 主数据即使更短也必须赢
        upsert(cur, "t_basic", "600999", "招商证券股份有限公司超长垃圾名称占位")
        upsert(cur, "t_basic", "600999", "招商")
        check("主数据短名照样赢", read_name(cur, "t_basic", "600999"), data.COMPANY_MASTER["600999"])
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{SANDBOX}`")
    conn.commit()

# ---------- 3. 鬼影主体候选（只读，不删）----------
print("== 3. 鬼影主体候选（只读实盘）==")
rows = data.query_all("SELECT stock_code, stock_name FROM `1_stock_basic`")
ghosts = []
for row in rows:
    code = str(row["stock_code"])
    if not code.startswith("FILE_"):
        continue
    counts = data.query_all(
        """
        SELECT
          (SELECT COUNT(*) FROM `7_document_index` WHERE stock_code=%s) AS docs,
          (SELECT COUNT(*) FROM `2_performance_report` WHERE stock_code=%s) AS perf,
          (SELECT COUNT(*) FROM `3_income_statement` WHERE stock_code=%s) AS income,
          (SELECT COUNT(*) FROM `4_balance_sheet` WHERE stock_code=%s) AS balance,
          (SELECT COUNT(*) FROM `5_cash_flow_statement` WHERE stock_code=%s) AS cash,
          (SELECT COUNT(*) FROM `6_rag_chunks` WHERE stock_code=%s) AS chunks
        """,
        (code, code, code, code, code, code),
    )[0]
    owned = sum(int(counts.get(k) or 0) for k in ("docs", "perf", "income", "balance", "cash", "chunks"))
    if owned == 0:
        ghosts.append(code)
    print(f"  {code} {row['stock_name']!r} 持有={owned} {'<-- 鬼影' if owned == 0 else ''}")
print(f"鬼影候选: {ghosts}")

# ---------- 3b. 切片身份必须与文档索引一致（防止第三份真相）----------
# 6_rag_chunks 每行自带 stock_code/company_name。切片时若自己再抽一次身份，
# 就会和调和过的文档索引打架（实测 TCL 英文全文入库 000100、切片却记 FILE_ 码），
# 后果是按公司筛选向量时这家公司搜不到自己的正文。
print("== 3b. 切片身份与文档索引一致性 ==")
mismatch = data.query_all(
    "SELECT c.stock_code AS from_code, d.stock_code AS to_code, COUNT(*) AS n "
    "FROM `6_rag_chunks` c JOIN 7_document_index d ON c.file_path = d.file_path "
    "WHERE NOT (c.stock_code <=> d.stock_code) OR NOT (c.company_name <=> d.company_name) "
    "GROUP BY from_code, to_code ORDER BY n DESC"
)
check("切片身份与文档索引不一致行数", sum(int(r["n"]) for r in mismatch), 0)
for row in mismatch[:6]:
    print(f"  {row['from_code']} -> {row['to_code']}：{row['n']} 行")

# 正文代码抽取：英文半年报只写 "Stock code 000100"，而定义页里的子公司代码
# 带 .SZ 后缀，不能当本公司身份。
check(
    "英文标签能抽出本公司代码",
    data.extract_stock_code_from_text(
        "I. Corporate Information\nStock name TCL TECH. Stock code 000100"
    ),
    "000100",
)
check(
    "英文子公司代码（带后缀）不算",
    data.extract_stock_code_from_text(
        "TZE Refers to a subsidiary listed on the Shenzhen Stock Exchange "
        "(stock code: 002129.SZ)"
    ),
    "",
)
check(
    "英文标签残留清掉但保住全大写品牌",
    data.clean_company_name("CompanynameinChineseTCL科技集团股份有限公司", ""),
    "TCL科技集团股份有限公司",
)
check(
    "公司名唯一反查真实代码",
    data.resolve_stock_code_by_name("TCL科技集团股份有限公司"),
    "000100",
)

print("结论:", "全部通过" if FAIL == 0 else f"{FAIL} 项失败")

# ---------- 4. 主数据完整性（防止「空主体当噪音删掉」再犯）----------
print("== 4. COMPANY_MASTER 全部落库 ==")
basic_rows = {
    str(row["stock_code"]): str(row["stock_name"])
    for row in data.query_all("SELECT stock_code, stock_name FROM `1_stock_basic`")
}
for code, name in sorted(data.COMPANY_MASTER.items()):
    check(f"主数据 {code} 在库且名字正确", basic_rows.get(code), name)
# 300006 曾被当成幽灵删掉，实际是重庆莱美药业（名字历史被错标成中恒），
# 删行会让「对比中恒集团和莱美药业」从两家塌成一家。
check("300006 未被误删（莱美药业）", basic_rows.get("300006"), "重庆莱美药业股份有限公司")
real_ghosts = [
    code
    for code in basic_rows
    if not code.startswith("FILE_")
    and code not in data.COMPANY_MASTER
    and int(data.query_all(
        "SELECT (SELECT COUNT(*) FROM `7_document_index` WHERE stock_code=%s) "
        "+ (SELECT COUNT(*) FROM `2_performance_report` WHERE stock_code=%s) AS n",
        (code, code),
    )[0]["n"] or 0) == 0
]
print(f"提示：另有 {len(real_ghosts)} 个无文档无事实的真实代码（{real_ghosts}），按规则保留不删。")

sys.exit(1 if FAIL else 0)
