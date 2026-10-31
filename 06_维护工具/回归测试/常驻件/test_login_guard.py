"""登录闸门回归：令牌签名纯函数 + 活体自适应验证。

闸门由 .env 的 LOGIN_USER/LOGIN_PASSWORD 控制，两台机器配置可能不同，
所以活体部分先探测「服务上闸门开没开」，开了就验全套（302/401/cookie/测试头），
没开就验「/login 应重定向回主页」这一条防呆。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    p = str(Path(__file__).resolve().parents[3] / _sub)
    if p not in sys.path:
        sys.path.insert(0, p)

BASE = "http://127.0.0.1:8501"
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + label + (f"：{extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPEN = urllib.request.build_opener(NoRedirect())


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPEN.open(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(200).decode("utf-8", "replace")


# —— 纯函数：不依赖服务，永远跑 ——
import web  # noqa: E402

# 先存下 .env 真实账号，纯函数段会大改环境变量，活体段要用真账号验 cookie 往返
REAL_USER = os.getenv("LOGIN_USER", "")
REAL_PASS = os.getenv("LOGIN_PASSWORD", "")

os.environ["LOGIN_USER"] = "_t_user"
os.environ["LOGIN_PASSWORD"] = "_t_pass"
tok = web.make_session_token()
check("令牌往返可验", web.verify_session_token(tok))
digest = tok.split(".", 1)[1]
check("改签名被拒", not web.verify_session_token(tok.replace(digest, "0" * len(digest))))
check("改过期时间被拒（签名对不上）", not web.verify_session_token(str(int(time.time()) + 999) + "." + digest))
check("空令牌被拒", not web.verify_session_token(""))
check("烂格式被拒", not web.verify_session_token("abc"))
os.environ["LOGIN_PASSWORD"] = "_t_pass2"
check("换口令=换密钥，旧令牌立刻作废", not web.verify_session_token(tok))
os.environ["FINSIGHT_DISABLE_LOGIN"] = "1"
check("FINSIGHT_DISABLE_LOGIN=1 时闸门强制关闭", not web.auth_enabled())
del os.environ["FINSIGHT_DISABLE_LOGIN"]
os.environ["LOGIN_USER"] = ""
check("只配口令不配账号 = 闸门不开（半配置防呆）", not web.auth_enabled())
os.environ["LOGIN_USER"] = "_t_user"
os.environ["LOGIN_PASSWORD"] = ""
check("只配账号不配口令 = 闸门不开（半配置防呆）", not web.auth_enabled())
os.environ["LOGIN_USER"] = REAL_USER
os.environ["LOGIN_PASSWORD"] = REAL_PASS

# —— 活体：看服务上真实闸门状态，自适应 ——
s, h, _ = call("GET", "/")
gate_on = s == 302 and h.get("Location") == "/login"
if gate_on:
    s2, _, b2 = call("GET", "/api/history")
    check("闸门开：未登录 API → 401 请先登录", s2 == 401 and "登录" in b2, f"{s2}")
    s3, _, _ = call("GET", "/login")
    check("闸门开：/login 页面 200", s3 == 200, f"{s3}")
    user, pwd = os.getenv("LOGIN_USER", ""), os.getenv("LOGIN_PASSWORD", "")
    if user and pwd:
        s4, h4, _ = call("POST", "/api/login", {"username": user, "password": pwd})
        cookie = str(h4.get("Set-Cookie") or "")
        check("正确口令 → 200 且下发 HttpOnly cookie", s4 == 200 and "fsession=" in cookie and "HttpOnly" in cookie, f"{s4}")
        if "fsession=" in cookie:
            val = cookie.split("fsession=", 1)[1].split(";", 1)[0]
            s5, _, _ = call("GET", "/", headers={"Cookie": f"fsession={val}"})
            check("带 cookie GET / → 200", s5 == 200, f"{s5}")
        s6, _, _ = call("POST", "/api/login", {"username": user, "password": pwd + "X"})
        check("错口令 → 401", s6 == 401, f"{s6}")
        # 轮卅②：登录页与主界面互斥——已登录访问 /login 必须被弹回 /
        s9, h9, _ = call("GET", "/login", headers={"Cookie": cookie})
        check("已登录访问 /login → 302 回主页", s9 == 302 and h9.get("Location") == "/", f"{s9} {h9.get('Location')}")
        s10, _, b10 = call("GET", "/api/me", headers={"Cookie": cookie})
        check("/api/me 已登录且带版本号", s10 == 200 and "version" in b10, f"{s10}")
    else:
        print("NOTE：测试进程环境没有 LOGIN_USER/LOGIN_PASSWORD，跳过 cookie 往返（服务却开着闸门=配置不同步？）")
    s7, _, _ = call("GET", "/api/history/does-not-exist", headers={"X-FinSight-Test": "1"})
    check("测试头放行后 404 语义不变", s7 == 404, f"{s7}")
else:
    check("闸门关：/login 重定向回主页", True, f"探测到 / 返回 {s}")
    s8, h8, _ = call("GET", "/login")
    check("闸门关：/login → 302 /", s8 == 302 and h8.get("Location") == "/", f"{s8} {h8.get('Location')}")

print()
print("登录闸门：" + ("全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}"))
sys.exit(1 if FAILS else 0)
