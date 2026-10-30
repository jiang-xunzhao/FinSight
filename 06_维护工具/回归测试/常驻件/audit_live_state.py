"""live 库体检：一次跑完所有「身份/单位/口径」不变量。只读。

用法：python audit_live_state.py
（服务可在跑，本脚本不碰 Qdrant；向量条数对比走 /api/vector-index）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import pymysql  # noqa: E402
import data  # noqa: E402

FACT_TABLES = (
    "2_performance_report",
    "3_income_statement",
    "4_balance_sheet",
    "5_cash_flow_statement",
)

cfg = dict(data.db_config())
cfg["cursorclass"] = pymysql.cursors.Cursor
conn = pymysql.connect(**cfg)
cur = conn.cursor()


def one(sql: str):
    cur.execute(sql)
    return cur.fetchall()


fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got} want={want}")
    if not ok:
        fails.append(label)


cur.execute("SELECT stock_code, stock_name FROM `1_stock_basic` ORDER BY stock_code")
entities = cur.fetchall()
fileish = [e for e in entities if str(e[0]).startswith("FILE_")]
print(f"1_stock_basic 主体 = {len(entities)}，其中 FILE_ = {len(fileish)} -> {fileish}")

docs = one("SELECT COUNT(*) FROM `7_document_index`")[0][0]
chunks = one("SELECT COUNT(*) FROM `6_rag_chunks`")[0][0]
print(f"文档 = {docs}，切片 = {chunks}")
for t in FACT_TABLES:
    print(f"  {t} = {one(f'SELECT COUNT(*) FROM `{t}`')[0][0]}")

# 1) 营收量级：A股半年报营收不可能 <10万元 或 >5000亿元（单位漏乘/错乘的直接信号）
absurd = one(
    "SELECT stock_code, report_period, revenue FROM `2_performance_report` "
    "WHERE revenue IS NOT NULL AND (revenue > 500000000000 OR revenue < 100000)"
)
print("营收量级可疑行 =", absurd)
check("营收量级可疑行数为 0", len(absurd), 0)

# 2) 事实表里不许有 FILE_ 前缀主体（非定期件的垃圾事实已清）
file_facts = sum(one(f"SELECT COUNT(*) FROM `{t}` WHERE stock_code LIKE 'FILE%%'")[0][0] for t in FACT_TABLES)
check("FILE_ 主体在事实表里的行数", file_facts, 0)

# 3) 主数据名字不许被文档 OCR 名覆盖
bad_names = []
for code, name in entities:
    want = data.COMPANY_MASTER.get(code)
    if want and not data.company_names_match(name, want):
        bad_names.append((code, name, want))
print("主数据名与库里名不符 =", bad_names)
check("主数据名与库里名不符数", len(bad_names), 0)

# 4) 切片身份必须与文档索引一致（第三真相）
mis = one(
    "SELECT COUNT(*) FROM `6_rag_chunks` c JOIN `7_document_index` d ON c.file_path = d.file_path "
    "WHERE NOT (c.stock_code <=> d.stock_code) OR NOT (c.company_name <=> d.company_name)"
)[0][0]
check("切片身份与文档索引不一致行数", mis, 0)

# 5) 同一 (公司, 报告期) 不许在业绩表里出现两行（删插漏斗的不变量）
dup = one(
    "SELECT stock_code, report_period, COUNT(*) FROM `2_performance_report` "
    "GROUP BY stock_code, report_period HAVING COUNT(*) > 1"
)
print("业绩表重复 (公司,报告期) =", dup)
check("业绩表重复 (公司,报告期) 组数", len(dup), 0)

# 6) 报告期不许是未来（闸门的不变量）
future = one("SELECT stock_code, report_period FROM `2_performance_report` WHERE CAST(LEFT(report_period,4) AS UNSIGNED) > YEAR(CURDATE())")
check("未来报告期行数", len(future), 0)

# 7) 归母路由哨兵：修复前全库 2_performance_report.net_profit_parent 是 0 行
#    （摘要表「归属于上市公司股东的净利润」被子串「净利润」抢走）。
#    以后谁把 match_metric 的路由改回 first-wins，这里会立刻归零。
parent_filled = one("SELECT COUNT(*) FROM `2_performance_report` WHERE net_profit_parent IS NOT NULL")[0][0]
print("业绩行里填了归母的 =", parent_filled)
check("归母路由不得回到全库为空", parent_filled >= 10, True)

# 8) 同键多份文档不许被摘要盖掉：只有「同一个 (公司,报告期) 有 ≥2 份**中文**文档」时才判，
#    因为只交了摘要的公司本来就只有 2~3 项，那是数据源的问题不是路由的问题。
#    英文版文档要排除：实测 000100 两份都是英文版（摘要表 + 正文，主表单位是 RMB'000），
#    护栏按量级把不一致的利润列丢了 ⇒ 只填 2 项是**正确结果**，不该算稀疏（见 data.PROFIT_FAMILY_CODES 注释）。
income_cols = data.MySQL.STATEMENT_COLUMNS["3_income_statement"]
income_filled = " + ".join(f"SUM(`{column}` IS NOT NULL)" for column in income_cols)
ENGLISH_NAME_FILTER = (
    "(file_name LIKE '%英文%' OR file_name LIKE '%Semi-annual%' "
    "OR file_name LIKE '%Annual Report%' OR file_name LIKE '%Interim%')"
)
sparse = one(
    "SELECT d.stock_code, d.report_period, d.filled, g.n_docs, g.n_cn_docs FROM ("  # noqa: S608
    f"  SELECT stock_code, report_period, {income_filled} AS filled FROM `3_income_statement`"
    "  GROUP BY stock_code, report_period"
    ") d JOIN ("
    "  SELECT stock_code, report_period, COUNT(*) AS n_docs,"
    f"    SUM(CASE WHEN {ENGLISH_NAME_FILTER} THEN 0 ELSE 1 END) AS n_cn_docs"
    "  FROM `7_document_index`"
    "  GROUP BY stock_code, report_period"
    ") g ON g.stock_code = d.stock_code AND g.report_period = d.report_period"
    " WHERE g.n_cn_docs > 1 AND d.filled < 3"
)
print("多份中文文档却只填了稀疏一行的组 =", sparse)
check("利润表稀疏行（摘要盖全文的表征）数为 0", len(sparse), 0)

# 9) 归母与利润表各列必须同量级（英文主表千元口径的兜底哨兵，
#    对应 data.drop_unit_mismatched_profits；一旦路由/单位改动让差 100× 的数进库，这里立刻报错）
#    注意必须用 ABS()：亏损公司归母是负数，`>= 10000` 会把整行放过（实测 600115/002571 就这样漏掉）。
mismatch = one(
    "SELECT stock_code, report_period, net_profit_parent, net_profit FROM `3_income_statement` "
    "WHERE net_profit_parent IS NOT NULL AND net_profit IS NOT NULL AND ABS(net_profit_parent) >= 10000 "
    "AND net_profit <> 0 AND (ABS(net_profit_parent / net_profit) > 100 OR ABS(net_profit / net_profit_parent) > 100)"
)
print("归母与净利润量级差 100× 以上的行 =", mismatch)
check("归母/净利润量级不一致行数为 0", len(mismatch), 0)

# 10) 散文行把科目取成小整数的表征：营收上亿却「净利润不到 10 万元」
tiny = one(
    "SELECT stock_code, report_period, operating_revenue, net_profit FROM `3_income_statement` "
    "WHERE operating_revenue >= 100000000 AND net_profit IS NOT NULL AND ABS(net_profit) < 100000"
)
print("营收上亿但净利润不足 10 万元的行 =", tiny)
check("散文小整数污染行数为 0", len(tiny), 0)

# 11) 只报告、不断言：|净利润| 与 |归母| 倍率 > 2 的行。
#     实测重导入后两表各 12/49 行（000010 315×、600031 31.2×、600325 18.2×…），
#     大多是「净利润取到母公司利润表行」的系统性取行问题，占 24% ⇒ 证据不足以自动清列，
#     交人工确认（交付 SQL 里也只发 SELECT 不发 UPDATE）。护栏只清 >100× 的确定单位错。
ratio = one(
    "SELECT stock_code, report_period, net_profit, net_profit_parent FROM `3_income_statement` "
    "WHERE net_profit IS NOT NULL AND net_profit_parent IS NOT NULL "
    "AND net_profit <> 0 AND net_profit_parent <> 0 AND ABS(net_profit_parent) >= 10000 "
    "AND GREATEST(ABS(net_profit), ABS(net_profit_parent)) / "
    "LEAST(ABS(net_profit), ABS(net_profit_parent)) > 2 ORDER BY stock_code"
)
print(f"[报告] 净利润与归母倍率>2 的行数（不判定，待人工看）= {len(ratio)}")

print("\n" + ("✅ 全部不变量通过" if not fails else f"❌ 未通过：{fails}"))
sys.exit(1 if fails else 0)
