"""走 HTTP 真跑一次「重复文件导入」，验证明单导入链路 + prune 调用点 + 任务可轮询。

只导入一个「库里已存在同一 sha256」的文件：应当走 duplicate → skipped，
不产生任何写库副作用（导入前后 7_document_index / 2_performance_report / 1_stock_basic 计数必须一致）。
"""
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for _sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / _sub))
import data  # noqa: E402

BASE = "http://127.0.0.1:8501"
FAILS: list[str] = []


def get(path, timeout=60):
    # 登录闸门开启时，本机回环带此标记放行（详见 web.py auth_ok 注释）
    req = urllib.request.Request(BASE + path, headers={"X-FinSight-Test": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-FinSight-Test": "1"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def counts():
    # query_all 返回 list[dict]，别按元组解包
    rows = data.query_all(
        "SELECT (SELECT COUNT(*) FROM `1_stock_basic`) basic,"
        "(SELECT COUNT(*) FROM `7_document_index`) docs,"
        "(SELECT COUNT(*) FROM `2_performance_report`) perf,"
        "(SELECT COUNT(*) FROM `6_rag_chunks`) chunks"
    )
    return dict(rows[0])


def check(label, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + label + (f"：{extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


before = counts()
print("导入前", before)

# 挑一个库里已有的文档，复制到临时目录（非受管副本，不会被删）
docs = data.query_all("SELECT file_path FROM `7_document_index` WHERE file_path <> '' LIMIT 40")
src = None
for row in docs:
    p = Path(str(row.get("file_path") or ""))
    if p.is_file():
        src = p
        break
if src is None:
    print("SKIP：找不到可复制的已导入文件")
    sys.exit(0)

tmp = Path(tempfile.mkdtemp(prefix="fin_dup_"))
copy = tmp / src.name
shutil.copy2(src, copy)
print("用文件", copy.name, "（sha256 已在库内）")

try:
    job = post("/api/import/start", {"local_dir": str(tmp)})
    job_id = str(job["id"])
    print("任务", job_id, job.get("status"), "total=", job.get("total"))
    session_id = str(job.get("session_id") or "")
    status = None
    for _ in range(120):
        status = get(f"/api/import/status/{job_id}")
        if status.get("status") != "running":
            break
        time.sleep(1)
    print("结束状态", {k: status.get(k) for k in ("status", "total", "processed", "success", "skipped", "failed", "errors")})
    check("重复文件被跳过而不是重写库", status.get("skipped") == 1 and status.get("success") == 0,
          f"skipped={status.get('skipped')} success={status.get('success')}")
    check("任务 completed 而非 failed", status.get("status") == "completed",
          f"status={status.get('status')} errors={status.get('errors')}")
    after = counts()
    print("导入后", after)
    check("四张表计数不变", after == before, f"{before} -> {after}")
    # 服务里的任务表应仍受控（裁剪调用点没把刚建的任务裁掉）
    check("刚建的任务仍可查询", bool(status.get("id")))
    if session_id:
        try:
            # DELETE /api/history/{id} 只删这一次探针会话；绝不打 /api/history（那是清空全部历史）
            req = urllib.request.Request(f"{BASE}/api/history/{session_id}", method="DELETE",
                                         headers={"X-FinSight-Test": "1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read().decode("utf-8"))
            print("（已清掉这次探针产生的导入会话）")
        except Exception as exc:
            print(f"NOTE：探针会话 {session_id[:8]} 未自动清理（{type(exc).__name__}），可手工删除")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("结论：", "HTTP 单文件重复导入通过" if not FAILS else f"{len(FAILS)} 条不符合预期 -> {FAILS}")
sys.exit(0 if not FAILS else 1)
