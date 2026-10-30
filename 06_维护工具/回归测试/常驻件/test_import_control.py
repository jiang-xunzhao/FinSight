"""轮十四常驻件：导入任务的「暂停 / 继续 / 结束」与进度归属。

用户原话（m02806）：「增加暂停上传功能 点完后把上传中的一个上传完就停就好了
然后还要有继续和结束功能 还有就是 导入状态不要再历史会话界面……删除的话 对应的
数据检验的内容也要删 推荐内容加入刷新功能」。

这个件钉住四件事：
1. 控制语义：暂停 = 手上那份做完就停（不是立刻掐断），能继续，能结束；
   已结束/已完成的任务不许被改回 running（否则前端永远等不到结果）。
2. 投递窗口：以前整批 `submit` 压在池子内部队列里撤不回来，暂停就是假的。
3. 导入不再往历史会话里写东西（`ensure_import_chat_session` 不新建会话、
   `update_import_session_status` 已删）。
4. 删文档时把「数字已经不在库里」的校验结论一起清掉（限定在这一期）。

不碰生产库：`find_duplicate` 被顶成「重复」，`process_one` 走 skip 分支，
一份都不会登记、不会切片；批末两个 prune 也被顶掉。
"""
from __future__ import annotations

import ast
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
for part in ("01_基础配置", "02_向量存储", "03_数据层"):
    sys.path.insert(0, str(PROJECT / part))

import data  # noqa: E402

SOURCE = (PROJECT / "03_数据层" / "data.py").read_text(encoding="utf-8")
WEB = (PROJECT / "05_Web服务" / "web.py").read_text(encoding="utf-8")

failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global failed
    if ok:
        print(f"PASS {label}")
    else:
        failed += 1
        print(f"FAIL {label}{(' :: ' + extra) if extra else ''}")


def func(name: str):
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ① 控制语义（源码结构）
check("有 set_import_job_control 这个唯一入口", func("set_import_job_control") is not None)
check("已结束的任务不被改写状态",
      'IMPORT_DONE_STATUSES' in SOURCE and '"completed", "stopped", "failed"' in SOURCE)
check("pause/resume/stop 三种动作都收", "IMPORT_CONTROL_ACTIONS" in SOURCE
      and all(a in SOURCE for a in ('"pause"', '"resume"', '"stop"')))
ctrl = ast.get_source_segment(SOURCE, func("set_import_job_control") or ast.Pass()) or ""
check("resume 把 status 放回 running", 'job["status"] = "running"' in ctrl)
check("控制函数不越权写 paused（只由工作线程写）", '"paused"' not in ctrl)

# ② 主循环：窗口投递 + 暂停/结束的落点
loop = ast.get_source_segment(SOURCE, func("run_import_job") or ast.Pass()) or ""
check("不再整批预投（as_completed 已不用）", "as_completed" not in loop and "as_completed" not in SOURCE)
check("投递受窗口限制", "len(futures) < IMPORT_WINDOW" in loop)
check("窗口与线程池 max_workers 对齐",
      "IMPORT_WINDOW = 2" in SOURCE and "max_workers=2" in (PROJECT / "01_基础配置" / "config.py").read_text(encoding="utf-8"))
check("暂停时把状态写成 paused", 'job["status"] = "paused" if control == "pause" else "running"' in loop)
check("结束后写明 stopped 并统计取消数",
      'job["status"] = "stopped" if stopped else "completed"' in loop and 'job["cancelled"] = len(queue)' in loop)
check("stop 不掐断在跑的那份（等它写完）", "for future in list(futures)" in loop)

# ③ 导入不再污染历史会话
ensure = ast.get_source_segment(SOURCE, func("ensure_import_chat_session") or ast.Pass()) or ""
check("ensure_import_chat_session 不再新建会话条目",
      "append" not in ensure and "save_chat_sessions" not in ensure)
check("update_import_session_status 已删除", func("update_import_session_status") is None)
check("run_import_job 里不再引用它", "update_import_session_status" not in loop)
check("前端不再把导入塞进会话上下文/历史",
      'updateSessionContext("导入任务"' not in WEB and "后台导入中" not in WEB)

# ④ 删除时同步清校验结论
delete_seg = ast.get_source_segment(SOURCE, func("delete_document") or ast.Pass()) or ""
prune_seg = ast.get_source_segment(SOURCE, func("prune_orphan_validation_results") or ast.Pass()) or ""
check("prune_orphan_validation_results 可限定到某一期",
      "def prune_orphan_validation_results(stock_code: str = \"\", report_period: str = \"\") -> int:" in SOURCE)
check("限定范围用的是 AND v.stock_code / v.report_period",
      "AND v.stock_code = %s AND v.report_period = %s" in SOURCE)
check("删除后按这一期清校验结论",
      "prune_orphan_validation_results(str(stock_code), str(report_period))" in delete_seg)
check("清校验失败只打日志不炸删除",
      "[删除] 校验结论未清理" in delete_seg and "validation_dropped" in delete_seg)
all_del = ast.get_source_segment(SOURCE, func("delete_all_documents") or ast.Pass()) or ""
check("全库删除仍然清空整张校验表",
      "9_validation_results" in all_del and "DELETE FROM `{table}`" in all_del)
check("全部删除不碰磁盘原文", "unlink" not in all_del and "rmtree" not in all_del)

# ⑤ web 层路由与界面
check("三条控制路由都在", all(k in WEB for k in
      ('"/api/import/pause/"', '"/api/import/resume/"', '"/api/import/stop/"')))
check("路由打到 set_import_job_control", WEB.count("set_import_job_control(") >= 3)
for element in ("importProgress", "importPhase", "importCounts", "importBar",
                "importPauseBtn", "importResumeBtn", "importStopBtn", "importNote",
                "refreshSuggestions"):
    check(f"界面有 #{element}", f'id="{element}"' in WEB)
check("按钮 disabled 初始态正确",
      'id="importResumeBtn" disabled' in WEB)
check("换一批 = 重新拉推荐再挑", "await loadRecommendations()" in WEB and "pickSuggestions()" in WEB)

# ⑥ 真跑一次：暂停 → 继续 → 结束（不写库）
_real = {name: getattr(data, name) for name in
         ("find_duplicate", "prune_orphan_entities", "prune_orphan_validation_results")}
tmp = Path(tempfile.mkdtemp(prefix="gmm_import_ctrl_"))
for i in range(8):
    (tmp / f"幽灵年报{i}.pdf").write_bytes(b"%PDF-1.4\n")


def _slow_duplicate(_file_hash: str) -> str:
    time.sleep(0.35)
    return "duplicate"


status_log: list[str] = []
try:
    data.find_duplicate = _slow_duplicate
    data.prune_orphan_entities = lambda: []
    data.prune_orphan_validation_results = lambda *args: 0
    snapshot = data.start_bulk_import_task(str(tmp))
    job_id = str(snapshot["id"])
    time.sleep(0.6)
    data.set_import_job_control(job_id, "pause")
    deadline = time.time() + 12
    paused = False
    while time.time() < deadline:
        with data.IMPORT_JOBS_LOCK:
            job = data.IMPORT_JOBS.get(job_id) or {}
            state = str(job.get("status"))
            processed = int(job.get("processed") or 0)
            total = int(job.get("total") or 0)
        status_log.append(state)
        if state == "paused":
            paused = True
            break
        time.sleep(0.15)
    check("暂停能停住（状态 paused）", paused, f"status={state}")
    with data.IMPORT_JOBS_LOCK:
        job = data.IMPORT_JOBS.get(job_id) or {}
        processed = int(job.get("processed") or 0)
        total = int(job.get("total") or 0)
    check("暂停时确实没把整批投出去", processed < total, f"processed={processed}/{total}")
    time.sleep(0.8)
    with data.IMPORT_JOBS_LOCK:
        still = str((data.IMPORT_JOBS.get(job_id) or {}).get("status"))
        again = int((data.IMPORT_JOBS.get(job_id) or {}).get("processed") or 0)
    check("暂停后不会自己偷偷继续", still == "paused" and again == processed,
          f"status={still} processed={again}!={processed}")
    data.set_import_job_control(job_id, "resume")
    time.sleep(1.0)
    with data.IMPORT_JOBS_LOCK:
        job = data.IMPORT_JOBS.get(job_id) or {}
        running = str(job.get("status"))
        moved = int(job.get("processed") or 0)
    check("继续之后恢复 running 并且真的在走",
          running in {"running", "completed"} and moved > processed,
          f"status={running} processed={moved}")
    data.set_import_job_control(job_id, "stop")
    deadline = time.time() + 20
    while time.time() < deadline:
        with data.IMPORT_JOBS_LOCK:
            job = data.IMPORT_JOBS.get(job_id) or {}
            state = str(job.get("status"))
        if state in {"stopped", "completed"}:
            break
        time.sleep(0.15)
    check("结束能收到 stopped", state == "stopped", f"status={state}")
    check("结束写明取消了几份",
          int(job.get("cancelled") or -1) >= 0 and "cancelled" in job,
          f"cancelled={job.get('cancelled')}")
    check("结束后一份都没进库（本件不写库）",
          int(job.get("success") or 0) == 0, f"success={job.get('success')}")
    done_snapshot = data.set_import_job_control(job_id, "resume")
    check("已完成/已结束的任务不许被改回 running",
          str(done_snapshot.get("status")) == "stopped",
          f"status={done_snapshot.get('status')}")
    try:
        data.set_import_job_control("不存在的任务", "pause")
        check("未知任务报 KeyError（路由会转成 404）", False, "没抛异常")
    except KeyError:
        check("未知任务报 KeyError（路由会转成 404）", True)
    try:
        data.set_import_job_control(job_id, "shake")
        check("未知动作报 ValueError（路由会转成 400）", False, "没抛异常")
    except ValueError:
        check("未知动作报 ValueError（路由会转成 400）", True)
    with data.IMPORT_JOBS_LOCK:
        data.IMPORT_JOBS.pop(job_id, None)
finally:
    for name, value in _real.items():
        setattr(data, name, value)
    for pdf in tmp.glob("*.pdf"):
        pdf.unlink(missing_ok=True)

# ---- 段⑤：留下的 pending 向量自动补齐（用户批准的新增功能，m06629）----
# 症状：导入被「结束 / 暂停后退出」打断、或进程半路被关掉 ⇒ 切片已经进了
# `6_rag_chunks`，但 `embedding_json` 还是空的（pending>0），RAG 只能走词面
# ⇒ 语义搜不到；这以前只能靠用户自己去界面点「重建向量索引」。
def _calls_hook(source: str, label: str) -> bool:
    """容忍两种引号写法，只看「有没有在这个位置调用」。"""
    return (
        'auto_backfill_pending_vectors("' + label + '")' in source
        or "auto_backfill_pending_vectors('" + label + "')" in source
    )

check("有 auto_backfill_pending_vectors 这个入口",
      callable(getattr(data, "auto_backfill_pending_vectors", None)))
check("补嵌只补 pending、绝不 force（已嵌好的一条都不重烧）",
      "start_vector_index_build(force=False)" in SOURCE)
check("导入任务收尾会自查自补", _calls_hook(SOURCE, "导入任务收尾"))
check("服务启动会自查自补（上次进程被打断也能补上）", _calls_hook(WEB, "服务启动"))

_pending_calls: list[object] = []
_status_backup = data.load_vector_index_status
_build_backup = data.start_vector_index_build
try:
    data.load_vector_index_status = lambda: {"pending": 0, "total": 10}
    data.start_vector_index_build = (
        lambda force=False: (_pending_calls.append(force), {"status": "running"})[1]
    )
    _idle = data.auto_backfill_pending_vectors("本件")
    check("pending=0 时不启动任务、不发 embedding",
          _idle.get("status") == "idle" and not _pending_calls,
          "result=" + repr(_idle) + " calls=" + repr(_pending_calls))
    data.load_vector_index_status = lambda: {"pending": 7, "total": 10}
    _fired = data.auto_backfill_pending_vectors("本件")
    check("pending=7 时按这个数启动补嵌、且 force=False",
          _pending_calls == [False] and _fired.get("pending") == 7,
          "calls=" + repr(_pending_calls) + " result=" + repr(_fired))
    data.load_vector_index_status = lambda: (_ for _ in ()).throw(RuntimeError("库不通"))
    _skipped = data.auto_backfill_pending_vectors("本件")
    check("读不到状态只是跳过，不把导入收尾 / 起服带崩",
          _skipped.get("status") == "skipped", "result=" + repr(_skipped))
finally:
    data.load_vector_index_status = _status_backup
    data.start_vector_index_build = _build_backup

print(f"状态序列：{' → '.join(status_log)}")
print("FAIL" if failed else "OK", f"失败 {failed} 项")
sys.exit(1 if failed else 0)
