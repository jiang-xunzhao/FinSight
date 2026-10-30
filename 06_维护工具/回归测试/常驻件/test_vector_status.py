"""向量状态真实性验证：Qdrant 目录被别的进程占用时不许谎报就绪。

注意「服务在跑」不等于「服务占住了目录」——本地内嵌 Qdrant 是懒打开的，
刚重启还没人查过向量库时锁是空的，所以这里一律自己起一个占位子进程，
并且把「子进程也被锁挡住」当成同样有效的占用场景（说明服务真的占着）。
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import vector_store  # noqa: E402

FLAG = Path(tempfile.gettempdir()) / f"gmm_qdrant_holder_{os.getpid()}.flag"
if FLAG.exists():
    FLAG.unlink()

# 子进程：能独占打开就写 locked 并睡住；打不开（说明别人占着）就写 busy。
# 必须把 client 绑到模块级名字上——不绑定的话 CPython 引用计数会当场回收它、
# 顺手释放锁，占位就成了「只锁了一瞬间」，主进程照样能打开（实测踩过）。
CHILD_CODE = (
    "import pathlib, time, traceback\n"
    f"flag = pathlib.Path({str(FLAG)!r})\n"
    "_holder_client = None\n"
    "try:\n"
    "    from qdrant_client import QdrantClient\n"
    f"    _holder_client = QdrantClient(path={str(vector_store.QDRANT_PATH)!r})\n"
    "    flag.write_text('locked')\n"
    "    time.sleep(20)\n"
    "except Exception:\n"
    "    flag.write_text('busy')\n"
    "    traceback.print_exc()\n"
)

holder = subprocess.Popen([sys.executable, "-c", CHILD_CODE])
deadline = time.time() + 30
while time.time() < deadline and not FLAG.exists():
    time.sleep(0.5)
mode = FLAG.read_text() if FLAG.exists() else ""
if mode == "locked":
    print("占位子进程已独占目录（说明 Web 服务当前没占着，也是有效的占用场景）")
elif mode == "busy":
    print("占位子进程打不开目录 —— Web 服务真的占着锁，同样是占用场景")
else:
    print("FAIL 占位进程既没锁上也没报忙，测不了")
    holder.kill()
    sys.exit(1)

ok = False
try:
    # 关键：这是本进程第一次碰 vector_store，不能先成功打开再测
    status = vector_store.get_status()
    print(
        f"占用中的状态: ready={status.get('ready')} count={status.get('count')} "
        f"error={str(status.get('error') or '')[:120]!r}"
    )
    ok = status.get("ready") is False and bool(str(status.get("error") or ""))
    print(
        f"{'PASS' if ok else 'FAIL'} 占用中必须 ready=false 且 error 写明原因"
        "（旧代码这里报 ready:true count:0，看着像向量库被清空，会骗人去重建索引）"
    )
    if ok and mode == "locked":
        # 反证：占位进程关掉之后，必须重新报真数，不许一直报坏。
        holder.kill()
        holder.wait(timeout=5)
        for _ in range(40):
            time.sleep(0.5)
            try:
                FLAG.unlink()
                break
            except FileNotFoundError:
                pass
        idle = vector_store.get_status()
        free_ok = idle.get("ready") is True and int(idle.get("count") or 0) > 0
        print(
            f"释放后的状态: ready={idle.get('ready')} count={idle.get('count')} "
            f"error={str(idle.get('error') or '')[:80]!r}"
        )
        print(f"{'PASS' if free_ok else 'FAIL'} 锁释放后必须恢复 ready 且报真实条数")
        ok = ok and free_ok
finally:
    if holder.poll() is None:
        holder.kill()
        holder.wait(timeout=5)
    if FLAG.exists():
        FLAG.unlink()
sys.exit(0 if ok else 1)
