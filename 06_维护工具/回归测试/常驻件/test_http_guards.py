"""HTTP 层回归：跨站写请求拦截、错误不外泄、launcher 兼容（不需 API）。"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8501"


def call(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-FinSight-Test", "1")  # 登录闸门开启时本机回环放行，见 web.py auth_ok
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(400).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


CASES = [
    ("GET 首页", "GET", "/", None, None, lambda s, b: s == 200),
    ("GET /assets/ 目录不再 500", "GET", "/assets/", None, None, lambda s, b: s == 404),
    ("跨站 Origin 的 POST 被拒", "POST", "/api/system/shutdown-noop", {}, {"Origin": "http://evil.example", "Content-Type": "application/json"}, lambda s, b: s == 403),
    ("Sec-Fetch-Site: cross-site 被拒", "POST", "/api/system/shutdown-noop", {}, {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, lambda s, b: s == 403),
    ("本机页面同源 POST 放行", "POST", "/api/chat/cancel-nonexistent", {}, {"Origin": BASE, "Content-Type": "application/json"}, lambda s, b: s != 403),
    ("无 Origin 的命令行 POST 放行", "POST", "/api/chat/cancel-nonexistent", {}, {"Content-Type": "application/x-www-form-urlencoded"}, lambda s, b: s != 403),
    ("跨站 DELETE 被拒", "DELETE", "/api/history/nonexistent", None, {"Origin": "http://evil.example"}, lambda s, b: s == 403),
    # 部署到服务器后要能对外访问：`APP_HOST=0.0.0.0` 时用户是从 `http://<服务器IP>:8501`
    # 打开页面的，浏览器带的 Origin 就是那个 IP。原来白名单写死 127.0.0.1/localhost/[::1]
    # ⇒ 页面打得开、但每次 POST /api/chat 都被自己挡成 403。现在改成「Origin 的 host:port
    # 等于本次请求的 Host 就算同源」。两条新针：异名同机要放行、伪装同端口要照旧挡。
    ("localhost 打 127.0.0.1 的同源 POST 放行", "POST", "/api/chat/cancel-nonexistent", {}, {"Origin": "http://localhost:8501", "Content-Type": "application/json"}, lambda s, b: s != 403),
    ("伪装成同端口的域名仍被拒 403", "POST", "/api/chat/cancel-nonexistent", {}, {"Origin": "http://127.0.0.1:8501.evil.example", "Content-Type": "application/json"}, lambda s, b: s == 403),
    ("Origin 带路径前缀的伪装仍被拒 403", "POST", "/api/chat/cancel-nonexistent", {}, {"Origin": "http://evil.example/redirect?to=http://127.0.0.1:8501", "Content-Type": "application/json"}, lambda s, b: s == 403),
    ("500 不回显内部细节", "GET", "/api/history/does-not-exist", None, None, lambda s, b: ("File" not in b and "line" not in b)),
    # 实测教训（接口全扫）：`do_GET` 有 `except KeyError → 404`，`do_POST` 漏了这一支，
    # 于是「聊天任务不存在」这种 404 语义掉进 `except Exception` 变 500；
    # 客户端见 500 会当服务端坏了去重试。三条 404 必须钉住。
    ("pause 不存在的任务回 404（曾 500）", "POST", "/api/chat/pause/does-not-exist", {}, {"Content-Type": "application/json"}, lambda s, b: s == 404),
    ("resume 不存在的任务回 404（曾 500）", "POST", "/api/chat/resume/does-not-exist", {}, {"Content-Type": "application/json"}, lambda s, b: s == 404),
    ("404 的说明不带 KeyError 的引号", "POST", "/api/chat/pause/does-not-exist", {}, {"Content-Type": "application/json"}, lambda s, b: "\"'" not in b and "'\"" not in b),
    # 空入参：`/api/chat/start` 以前不带 question 也 200 建任务、后台才失败（白占一个
    # CHAT_TASKS 槽）；`/api/import/start` 以前带空 `local_dir` 会**把服务自己的工程目录
    # 当导入目录扫**，实测删过 25 份已入库文档的原文（见 test_import_delete_safety.py）。
    ("空问题的异步聊天被拒 400（曾 200）", "POST", "/api/chat/start", {}, {"Content-Type": "application/json"}, lambda s, b: s == 400),
    ("纯空格问题被拒 400", "POST", "/api/chat/start", {"question": "   "}, {"Content-Type": "application/json"}, lambda s, b: s == 400),
    ("空导入目录被拒 400（曾扫工程目录）", "POST", "/api/import/start", {}, {"Content-Type": "application/json"}, lambda s, b: s == 400),
    ("相对导入目录被拒 400", "POST", "/api/import/start", {"local_dir": "storage"}, {"Content-Type": "application/json"}, lambda s, b: s == 400),
    ("圈进托管上传目录被拒 400", "POST", "/api/import/start", {"local_dir": "C:/Users/35999/PycharmProjects"}, {"Content-Type": "application/json"}, lambda s, b: s == 400),
]

failed = 0
for name, method, path, body, headers, check in CASES:
    status, text = call(method, path, body, headers)
    ok = check(status, text)
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}: [{status}] {text[:90]}")

print("\n结论：" + ("全部通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
