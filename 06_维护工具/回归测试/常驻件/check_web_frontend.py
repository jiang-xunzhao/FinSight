"""把页面内联 JS 抽出来交给 node --check 做语法校验，并冒烟几个只读接口。"""
import re
import shutil
import subprocess
import sys
import urllib.request

# 跨平台找 node：优先 PATH，其次开发机专用运行时目录（Linux 服务器上没有后者，找不到时下方会跳过语法检查）
NODE = shutil.which("node") or r"C:\Users\35999\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
BASE = "http://127.0.0.1:8501"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import time  # noqa: E402


def get(url, timeout):
    # 登录闸门开启时，本机回环带此标记放行（详见 web.py auth_ok 注释）
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"X-FinSight-Test": "1"}), timeout=timeout
    )


for _ in range(60):
    try:
        with get(BASE + "/api/system/version", 2) as handle:
            print("服务版本:", handle.read().decode("utf-8")[:60])
            break
    except Exception:
        time.sleep(1)
else:
    print("FAIL 服务没起来")
    sys.exit(1)

html = get(BASE + "/", 10).read().decode("utf-8")
scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
inline = [body for body in scripts if body.strip()]
print(f"内联脚本 {len(inline)} 段，共 {sum(len(b) for b in inline)} 字符")

failed = 0
if not shutil.which(NODE) and not __import__("os").path.exists(NODE):
    print("WARN 未找到 node，跳过内联 JS 语法检查")
    inline = []
for index, body in enumerate(inline):
    import tempfile

    path = __import__("os").path.join(tempfile.gettempdir(), f"finsight_inline_{index}.js")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    result = subprocess.run([NODE, "--check", path], capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        failed += 1
        print(f"FAIL 第 {index} 段脚本语法错误：\n{result.stderr[:1200]}")
    else:
        print(f"PASS 第 {index} 段脚本语法通过（node --check）")

for endpoint in ("/api/summary", "/api/history", "/api/documents?query=", "/api/validation", "/api/data-quality", "/api/vector-index", "/api/recommendations"):
    try:
        with get(BASE + endpoint, 30) as handle:
            print(f"PASS {endpoint} -> {handle.status}, {len(handle.read())} 字节")
    except Exception as exc:
        failed += 1
        print(f"FAIL {endpoint} -> {exc}")

print("结论：" + ("前端脚本与只读接口全部正常" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
