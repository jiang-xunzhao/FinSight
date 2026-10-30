"""删除-重建与「全部删除」回归：删一份不许带走同期另一份的数，清库不许碰磁盘原文。

缺陷（本件的由来，有实据）：`delete_document` 按 `(stock_code, report_period)` 整行删四张
事实表，却不看这一期还剩几份文件。库里实测有 **36 组**「同一公司同一期多份文件」（摘要 +
全文、或两个版本）⇒ 删掉其中一份，另一份贡献的数值也被一起带走，可它的切片还在
⇒ Text-to-SQL 查不到数、RAG 却能引原文，表现是「同一个问题一次有一次没有」。

修法（轮十三）：出锁后用留下来的那份重新走一遍入库链；只有这一期一份不剩时才作废它的
校验结论。新增 `delete_all_documents()` + `DELETE /api/documents` + 界面「全部删除」。

本件**不碰生产库、不删任何文件**：只做源码结构断言 + 两条空/缺文件的纯路径调用
（都在建连接之前返回），所以可以跟其它件一样在回归里反复跑。
"""
import ast
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
for part in ("01_基础配置", "02_向量存储", "03_数据层"):
    sys.path.insert(0, str(PROJECT / part))

import data  # noqa: E402

DATA_SOURCE = (PROJECT / "03_数据层" / "data.py").read_text(encoding="utf-8")
WEB_SOURCE = (PROJECT / "05_Web服务" / "web.py").read_text(encoding="utf-8")

failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {label}{('：' + extra) if extra else ''}")


def function_tree(source: str) -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            found.setdefault(node.name, node)
    return found


data_functions = function_tree(DATA_SOURCE)
delete_document = data_functions.get("delete_document")
rebuild = data_functions.get("reimport_report_period_documents")
delete_all = data_functions.get("delete_all_documents")

check("delete_document 存在", delete_document is not None)
check("reimport_report_period_documents 存在", rebuild is not None)
check("delete_all_documents 存在", delete_all is not None)
if not (delete_document and rebuild and delete_all):
    raise SystemExit(1)

# 取**原始源码片段**（带原来的引号），不要用 ast.unparse——它把双引号统一成单引号，
# 字符串断言会整批假红（实测 12 项 FAIL 全来自这里）。
delete_text = ast.get_source_segment(DATA_SOURCE, delete_document) or ""
rebuild_text = ast.get_source_segment(DATA_SOURCE, rebuild) or ""
all_text = ast.get_source_segment(DATA_SOURCE, delete_all) or ""

# ① 必须先算出「这一期还剩几份」，而且排除被删的那一份本身
check(
    "delete_document 会算 survivors（同代码同期、排除自己）",
    "survivors" in delete_text and "item is not target" in delete_text,
)
check(
    "survivors 按 stock_code + report_period 双条件配对",
    'item.get("stock_code")' in delete_text and 'item.get("report_period")' in delete_text,
)

# ② 校验结论只在「一份不剩」时才删——还有剩就绝不能删：重灌那份自己会 DELETE+INSERT，
#    而且它「保留原行」的分支是故意留着旧结论的（先删会造出「有数字没校验」的空档）。
guard_tests = [ast.unparse(node.test) for node in ast.walk(delete_document) if isinstance(node, ast.If)]
survivor_guards = [test for test in guard_tests if "survivors" in test]
check("有 `if not survivors` 判断（不是正向）", any("not survivors" in test for test in survivor_guards), str(survivor_guards))
validation_nodes = [
    node
    for node in ast.walk(delete_document)
    if isinstance(node, ast.If) and "not survivors" in ast.unparse(node.test)
]
check(
    "9_validation_results 只在 not survivors 分支里删",
    any("9_validation_results" in ast.unparse(node) for node in validation_nodes)
    and "DELETE FROM 9_validation_results" in delete_text,
    f"not survivors 分支 {len(validation_nodes)} 段",
)
check(
    "四张事实表仍按 (代码, 期间) 删（没被改坏）",
    delete_text.count("DELETE FROM `{table}` WHERE stock_code=%s AND report_period=%s") == 1,
)

# ③ 重建必须发生在 DOCUMENT_INDEX_LOCK 之外：config.py:61/:63 是非重入 threading.Lock，
#    锁内调用 register_document(:4792) / build_rag_chunks_from_file(:5436) 会当场死锁。
locked_regions: list[str] = []
for node in ast.walk(delete_document):
    if isinstance(node, ast.With):
        for item in node.items:
            if "DOCUMENT_INDEX_LOCK" in ast.unparse(item.context_expr):
                locked_regions.append(ast.unparse(item))
check(
    "重建调用不在锁内",
    all("reimport_report_period_documents" not in region for region in locked_regions)
    and "reimport_report_period_documents(survivors)" in delete_text,
    f"锁内语句 {len(locked_regions)} 段",
)
check(
    "重建结果回给调用方（rebuilt / rebuild_failed）",
    'result["rebuilt"]' in delete_text and 'result["rebuild_failed"]' in delete_text,
)

# ④ 两处静默吞异常必须留话（轮十一原则：只加日志、不改行为）
check(
    "向量点删除失败会打日志",
    "[删除] 向量点未清理" in delete_text,
)
check(
    "6_rag_chunks 删除失败会打日志",
    "[删除] 6_rag_chunks 未清理" in delete_text,
)

# ⑤ 重建本身：小的先、大的后（让最完整那份最后写）；缺文件/被闸门拦下要计入 failed；
#    只有真要重灌时才建连接（全是缺文件的入参不该碰库）。
check(
    "重建按 file_size 升序（摘要先、全文后）",
    "sorted(entries" in rebuild_text and 'int(one.get("file_size") or 0)' in rebuild_text,
)
check(
    "重建尊重入库闸门（quarantined 不登记不切片）",
    'metadata.get("quarantined")' in rebuild_text,
)
check(
    "重建是惰性建连（缺文件时不碰库）",
    "database is None" in rebuild_text,
)
check(
    "重建顺序 = 先登记再切片（切片身份只认文档索引）",
    rebuild_text.index("register_document(") < rebuild_text.index("build_rag_chunks_from_file("),
)

# ⑥ 全部删除：清库清切片清向量，但**绝不删磁盘原文**（托管目录里躺着的可能正是唯一原文，
#    实测被误删过 25 份）。
check("全部删除不 unlink 任何文件", "unlink" not in all_text and "rmtree" not in all_text)
for table in (
    "2_performance_report",
    "3_income_statement",
    "4_balance_sheet",
    "5_cash_flow_statement",
    "6_rag_chunks",
    "9_validation_results",
):
    check(f"全部删除会清 {table}", f'"{table}"' in all_text)
check("全部删除会清文档索引", "save_document_index([])" in all_text)
check("全部删除会清空内存切片", "RAG_CHUNKS[:] = []" in all_text)
check("全部删除会 reset 向量集合", "reset_collection()" in all_text)
check(
    "全部删除会如实回 status（cleared / failed）",
    '"cleared"' in all_text and '"failed"' in all_text,
)

# ⑦ 路由与界面：DELETE /api/documents 必须在 /api/documents/<id> 之前判，
#    否则 startswith 抢先命中，id 变成空串走 delete_document("") ⇒ 500。
route_index_all = WEB_SOURCE.index('path == "/api/documents"')
route_index_one = WEB_SOURCE.index('path.startswith("/api/documents/")')
check("DELETE 路由：整库分支在前、单份分支在后", route_index_all < route_index_one)
check("DELETE 路由：整库分支调 delete_all_documents()", "delete_all_documents()" in WEB_SOURCE[route_index_all:route_index_one])
check("界面有「全部删除」按钮", 'id="deleteAllFilesBtn"' in WEB_SOURCE)
check("「全部删除」有二次确认", 'getElementById("deleteAllFilesBtn")' in WEB_SOURCE and "window.confirm" in WEB_SOURCE)
check(
    "确认后打 DELETE /api/documents",
    'fetch("/api/documents", { method: "DELETE" })' in WEB_SOURCE,
)
check(
    "删除后刷新列表/概览/推荐",
    all(name in WEB_SOURCE for name in ("loadDocuments();", "loadSummary();", "loadRecommendations()")),
)

# ⑧ 两条纯路径实跑（不碰库、不删文件）：空入参直接返回，缺文件计入 failed。
empty = data.reimport_report_period_documents([])
check("重建：空入参返回空结果且不碰库", empty == {"rebuilt": [], "failed": []}, str(empty))
ghost = data.reimport_report_period_documents(
    [{"file_path": "Z:/不可能存在的目录/幽灵年报.pdf", "file_size": 1, "managed_copy": False, "file_hash": ""}]
)
check(
    "重建：原文不在磁盘 ⇒ 计入 failed、不抛异常",
    ghost["rebuilt"] == [] and len(ghost["failed"]) == 1 and "原文已不在磁盘" in ghost["failed"][0],
    str(ghost),
)

# ⑨ 生产库只读体检：这个缺陷在现网真实存在（多文件同期组必须还在），别把修复当死代码。
try:
    entries = data.load_document_index()
    groups: dict[tuple[str, str], int] = {}
    for item in entries:
        code = str(item.get("stock_code") or "")
        period = str(item.get("report_period") or "")
        if code and period:
            groups[(code, period)] = groups.get((code, period), 0) + 1
    multi = sum(1 for count in groups.values() if count > 1)
    check(
        "现网确有「同一公司同一期多份文件」（修复不是死代码）",
        multi > 0,
        f"{len(groups)} 组 / 其中 {multi} 组多份 / 共 {len(entries)} 份文档",
    )
except Exception as exc:
    check("现网确有「同一公司同一期多份文件」（修复不是死代码）", False, f"{type(exc).__name__}: {exc}")

print(f"\n===== test_delete_rebuild：{'全过' if not failed else str(failed) + ' 项失败'} =====")
raise SystemExit(1 if failed else 0)
