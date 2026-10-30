# -*- coding: utf-8 -*-
"""IMPORT_JOBS 裁剪回归：旧任务不再无限堆积，但正在用的一定还在。

data.prune_import_jobs：只裁「已结束 + 完成超过 1 小时 + 超出最近 20 个名额」的任务。
本测试不起线程、不碰聊天会话（会话相关函数一律 monkeypatch），只操作内存字典。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import config  # noqa: E402
import data  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {label}{('：' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


def make_job(job_id: str, status: str, finished_minutes_ago: float | None) -> dict:
    now = datetime.now()
    return {
        "id": job_id,
        "status": status,
        "total": 1,
        "processed": 1,
        "success": 1,
        "failed": 0,
        "skipped": 0,
        "errors": [],
        "started_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": (
            None
            if finished_minutes_ago is None
            else (now - timedelta(minutes=finished_minutes_ago)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        ),
        "locked_company": None,
        "session_id": "",
    }


def reset_jobs(specs: list[tuple[str, str, float | None]]) -> None:
    config.IMPORT_JOBS.clear()
    for job_id, status, minutes in specs:
        config.IMPORT_JOBS[job_id] = make_job(job_id, status, minutes)


# ---- 1. 未超出名额：什么都不裁 ------------------------------------------
reset_jobs([(f"old{i:02d}", "completed", 600) for i in range(10)])
removed = data.prune_import_jobs()
check("名额内不裁剪", removed == [] and len(config.IMPORT_JOBS) == 10,
      f"removed={removed} size={len(config.IMPORT_JOBS)}")

# ---- 2. 运行中 / 刚结束 / keep_id 都不动，只裁「完成时间最早」的超额任务 --
# 注意注入方向：编号越大 = 完成得越早（minutes_ago = 120 + i）。
specs = [("running1", "running", None), ("fresh1", "completed", 2)]
specs += [(f"old{i:03d}", "completed", 120 + i) for i in range(30)]
reset_jobs(specs)
removed = data.prune_import_jobs(keep_id="running1")
size = len(config.IMPORT_JOBS)
check("超额旧任务被裁", size == data.IMPORT_JOBS_KEEP, f"size={size}")
check("运行中任务保留", "running1" in config.IMPORT_JOBS)
check("刚结束任务保留", "fresh1" in config.IMPORT_JOBS)
oldest_batch = {f"old{i:03d}" for i in range(18, 30)}  # 完成最早的 12 个
check("只裁完成最早的超额部分",
      oldest_batch.isdisjoint(config.IMPORT_JOBS)
      and all(f"old{i:03d}" in config.IMPORT_JOBS for i in range(18)),
      f"removed={sorted(removed)}")
check("裁剪数量正确", sorted(removed) == sorted(oldest_batch),
      f"removed={sorted(removed)}")

# ---- 3. 幂等：再裁一次没有可裁的 ---------------------------------------
check("二次裁剪幂等", data.prune_import_jobs() == [], f"size={len(config.IMPORT_JOBS)}")

# ---- 4. 建任务入口真的会触发裁剪（stub 掉线程与会话写入） ----------------
class _StubThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass


BEFORE = "--before" in sys.argv  # 顶掉裁剪 = 改前「只插不删」的行为，用来做前后对比
# 轮十四起导入不再回写会话标题（`update_import_session_status` 已删），
# 所以这里只顶 Thread 与 ensure_import_chat_session 两个。
_real_thread, _real_ensure = data.threading.Thread, data.ensure_import_chat_session
_real_prune = data.prune_import_jobs
if BEFORE:
    data.prune_import_jobs = lambda keep_id="": []
tmp_pdf = Path(__file__).resolve().parent / "_prune_probe.pdf"
tmp_pdf.write_bytes(b"%PDF-1.4\n")
data.threading.Thread = _StubThread
data.ensure_import_chat_session = lambda session_id="": "prune-test-session"
try:
    reset_jobs([(f"stale{i:03d}", "completed", 300 + i) for i in range(28)])
    before = len(config.IMPORT_JOBS)
    snapshot = data.start_import_task_for_paths([str(tmp_pdf)])
    new_id = str(snapshot["id"])
    after = len(config.IMPORT_JOBS)
    if BEFORE:
        check("改前基线：任务表只增不减", after == before + 1, f"after={after}")
        sys.exit(0 if not FAILS else 1)
    check("建任务后总量受限", after == data.IMPORT_JOBS_KEEP,
          f"before={before} after={after}")
    check("新任务在表内且为 running",
          new_id in config.IMPORT_JOBS
          and config.IMPORT_JOBS[new_id]["status"] == "running")
    check("完成最早的 stale 已清", "stale027" not in config.IMPORT_JOBS,
          f"keys={sorted(config.IMPORT_JOBS)[:3]}")
    check("较新的历史任务仍在", "stale000" in config.IMPORT_JOBS)
finally:
    data.threading.Thread = _real_thread
    data.ensure_import_chat_session = _real_ensure
    data.prune_import_jobs = _real_prune
    config.IMPORT_JOBS.clear()
    tmp_pdf.unlink(missing_ok=True)

print()
if FAILS:
    print(f"结论：{len(FAILS)} 项不符合预期 -> {FAILS}")
    sys.exit(1)
print("结论：IMPORT_JOBS 裁剪全部符合预期")
