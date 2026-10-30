"""回归测试：SQL 白名单绕过 + chunk key 一致性 + 用历史真实 SQL 当语料防误杀。"""
import hashlib
import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402
import vector_store  # noqa: E402

MUST_BLOCK = [
    ("逗号多表读系统库", "SELECT a.stock_code, b.user FROM 1_stock_basic a, mysql.user b"),
    ("逗号多表读聊天历史", "SELECT q.question FROM 1_stock_basic s, 8_chat_history q"),
    ("逗号多表读文件库路径", "SELECT s.stock_code FROM 1_stock_basic s, 7_document_index d"),
    ("UNION 读越权表", "SELECT stock_name FROM 1_stock_basic UNION SELECT file_path FROM 7_document_index"),
    ("子查询读越权表", "SELECT (SELECT content FROM 6_rag_chunks LIMIT 1) AS x FROM 1_stock_basic"),
    ("INTO OUTFILE 写文件", "SELECT stock_code INTO OUTFILE 'C:/tmp/x.txt' FROM 1_stock_basic"),
    ("INTO DUMPFILE", "SELECT stock_code INTO DUMPFILE 'C:/tmp/x.bin' FROM 1_stock_basic"),
    ("LOAD_FILE 读文件", "SELECT LOAD_FILE('C:/windows/win.ini') FROM 1_stock_basic"),
    ("# 注释吃掉追加的 LIMIT", "SELECT stock_code FROM 1_stock_basic #"),
    ("读 information_schema", "SELECT table_name FROM information_schema.tables"),
    ("SLEEP 拖垮连接", "SELECT SLEEP(30) FROM 1_stock_basic"),
    ("多语句", "SELECT stock_code FROM 1_stock_basic; DROP TABLE 1_stock_basic"),
    ("行注释", "SELECT stock_code FROM 1_stock_basic -- x"),
    ("DELETE", "DELETE FROM 2_performance_report WHERE stock_code='000801'"),
]
MUST_ALLOW = [
    ("普通查询", "SELECT report_period, revenue FROM 2_performance_report WHERE stock_code='000801' LIMIT 20"),
    ("LEFT JOIN", "SELECT p.revenue FROM 2_performance_report p LEFT JOIN 4_balance_sheet b ON p.stock_code=b.stock_code"),
    ("两张合法表逗号连接", "SELECT s.stock_code FROM 1_stock_basic s, 2_performance_report p WHERE s.stock_code=p.stock_code"),
    ("CTE", "WITH t AS (SELECT stock_code FROM 1_stock_basic) SELECT t.stock_code FROM t"),
    ("CTE + 排序", "WITH t AS (SELECT stock_code, revenue FROM 2_performance_report) SELECT stock_code FROM t ORDER BY revenue DESC LIMIT 5"),
    ("大小写混合的合法表名", "SELECT stock_code FROM 3_Income_Statement"),
    ("逻辑别名 income_statement", "SELECT revenue FROM income_statement WHERE stock_code='600007'"),
    ("不带 LIMIT 会自动补", "SELECT stock_code FROM 1_stock_basic"),
    ("字面量里含 into 不该误杀", "SELECT stock_name FROM 1_stock_basic WHERE stock_name='Into the future 股份有限公司'"),
    ("反引号表名", "SELECT `stock_code` FROM `1_stock_basic`"),
    ("库名前缀写法", "SELECT stock_code FROM finance_report.1_stock_basic"),
]

failed = 0
print("=== A. SQL 安全校验：必须拦截 ===")
for name, sql in MUST_BLOCK:
    try:
        kept = assistant.validate_sql_safety(sql)
        print(f"FAIL {name} -> 放行为: {kept}")
        failed += 1
    except assistant.SQLValidationError as exc:
        print(f"PASS {name} -> {exc}")

print("\n=== B. SQL 安全校验：必须放行（不能误杀）===")
for name, sql in MUST_ALLOW:
    try:
        kept = assistant.validate_sql_safety(sql)
        auto = " [自动补LIMIT]" if kept.endswith("LIMIT 200") and "limit" not in sql.lower() else ""
        print(f"PASS {name} -> {kept[:74]}{auto}")
    except assistant.SQLValidationError as exc:
        print(f"FAIL {name} 被误拒: {exc}")
        failed += 1

print("\n=== C. 历史真实 SQL 语料（一条都不许误拒）===")
corpus: list[str] = []
sessions_file = ROOT / "storage" / "chat_history.json"
if sessions_file.exists():
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"sql", "expected_sql", "query"} and isinstance(value, str) and value.strip():
                    corpus.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    try:
        walk(json.loads(sessions_file.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"（读取历史失败：{exc}）")
seen, rejected = set(), []
for sql in corpus:
    if sql in seen:
        continue
    seen.add(sql)
    try:
        assistant.validate_sql_safety(sql)
    except assistant.SQLValidationError as exc:
        rejected.append(f"{exc} <- {sql[:100]}")
print(f"语料 {len(seen)} 条去重，误拒 {len(rejected)} 条")
for line in rejected[:10]:
    print("  FAIL", line)
    failed += 1
if not corpus:
    print("  （没有历史语料，跳过）")

print("\n=== D. chunk key 一致性 ===")
source = inspect.getsource(assistant.retrieve_rag_chunks)
ok = "hashlib.sha1" not in source and "vector_store.build_chunk_key" in source
print(f"{'PASS' if ok else 'FAIL'} retrieve_rag_chunks 复用 vector_store.build_chunk_key")
failed += not ok
for chunk in [
    {"file_path": "/a/b/x.pdf", "page_no": 3, "chunk_index": 1},
    {"file_path": "/a/b/y.pdf", "page_no": 0, "chunk_index": 0},
    {"file_path": "/a/b/z.pdf", "page_no": 169, "chunk_index": 7},
]:
    store_key = vector_store.build_chunk_key(chunk)
    old_key = hashlib.sha1(  # 修前 assistant.py:3036 的内联算法
        str(chunk.get("file_path") or "").encode()
        + str(chunk.get("page_no") or "").encode()
        + str(chunk.get("chunk_index") or "").encode()
    ).hexdigest()
    print(f"  page={chunk['page_no']:>3} store={store_key[:12]} 修前内联={old_key[:12]} "
          f"{'一致' if store_key == old_key else '不一致（向量分永远合不上）'}")

print("\n结论：" + ("全部通过" if failed == 0 else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
