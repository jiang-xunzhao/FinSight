"""会话与后台任务并发单测：终态任务不被暂停/恢复改写、任务表会清理、历史读取不炸。"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402

failed = 0


def check(name, ok, got=""):
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  -> {got!r}" if got != "" else ""))


def fake_task(status, created_at=None):
    task_id = f"t-{status}-{len(assistant.CHAT_TASKS)}"
    assistant.CHAT_TASKS[task_id] = {
        "id": task_id,
        "status": status,
        "stage": "",
        "paused": False,
        "event": threading.Event(),
        "result": {"answer": "x"} if status == "completed" else None,
        "error": "",
        "logs": [],
        "created_at": created_at if created_at is not None else time.time(),
    }
    return task_id


print("=== 1) 已结束的任务：暂停/恢复不许改写状态 ===")
done = fake_task("completed")
assistant.pause_chat_task(done)
status = assistant.CHAT_TASKS[done]["status"]
check("completed 后点暂停仍保持 completed", status == "completed", status)
check("暂停没把结果清掉", assistant.CHAT_TASKS[done]["result"] is not None)
assistant.resume_chat_task(done)
status2 = assistant.CHAT_TASKS[done]["status"]
check("completed 后点恢复不会变回 running", status2 == "completed", status2)
check("恢复不会把已结束任务的事件误开", not assistant.CHAT_TASKS[done]["event"].is_set())

live = fake_task("running")
assistant.pause_chat_task(live)
check("运行中的任务照常可暂停", assistant.CHAT_TASKS[live]["status"] == "paused")
assistant.resume_chat_task(live)
check("暂停后可恢复", assistant.CHAT_TASKS[live]["status"] == "running")

print("\n=== 2) 任务表清理 ===")
assistant.CHAT_TASKS.clear()
old = [fake_task("completed", created_at=time.time() - 3600) for _ in range(5)]
recent = [fake_task("completed", created_at=time.time()) for _ in range(200)]
running = fake_task("running", created_at=time.time() - 7200)
removed = assistant.prune_chat_tasks(assistant.CHAT_TASKS)
check("过期已完成任务被清掉", all(key not in assistant.CHAT_TASKS for key in old), removed)
check("在跑的任务不会被清", running in assistant.CHAT_TASKS)
check("总量收敛到上限内", len(assistant.CHAT_TASKS) <= assistant.CHAT_TASK_MAX_ENTRIES + 1, len(assistant.CHAT_TASKS))

print("\n=== 3) 历史读取与写入并发 ===")
session = {"id": "s1", "title": "t", "updated_at": "", "messages": [], "locked_company": {}, "analysis_context": {}}
assistant.CHAT_SESSIONS[:] = [session]
# 别让并发测试真去写 storage/chat_history.json
assistant.save_chat_sessions = lambda: None
errors = []
stop = threading.Event()


def writer() -> None:
    try:
        while not stop.is_set():
            assistant.append_session_exchange(
                session, "问题" + str(time.time()), {"answer": "答", "company_name": "四川九洲"}
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(("writer", repr(exc)))


def reader() -> None:
    try:
        for _ in range(400):
            history = assistant.extract_session_history(assistant.find_chat_session("s1"))
            for item in history:
                item["question"][:1]
    except Exception as exc:  # noqa: BLE001
        errors.append(("reader", repr(exc)))


threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
threads += [threading.Thread(target=reader, daemon=True) for _ in range(5)]
for t in threads:
    t.start()
time.sleep(1.2)
stop.set()
for t in threads:
    t.join(timeout=5)
check("并发读写无异常", not errors, errors[:2])
written = len([m for m in session["messages"] if m.get("role") == "user"])
history = assistant.extract_session_history(session)
check("写入的轮次都在会话里", written > 100, written)
check("上下文只回最近 5 轮（设计行为）", len(history) == min(5, written), f"history={len(history)} written={written}")
check("历史内容取得到", all(h["company_name"] == "四川九洲" for h in history))

print("\n结论：" + ("会话/任务并发修复通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
