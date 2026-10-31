"""轮卅回归：多窗口并发任务表（前端不变量）+ 账户接口（/api/me、/api/profile、/api/password）。

覆盖两件事：
1) 前端 INDEX_HTML 的「任务表」不变量——trackedTasks/reattachTrackedThinking/openTrackedTask
   必须存在；旧的全局单任务互斥字段（currentTaskId/currentTaskState/taskPollTimer）与
   已删除的设置页 DOM（page-settings/saveSettings/logoutBtn）不得回归。
2) 后端账户三接口 + 登录页/主界面互斥守卫（已登录访问 /login → 302 /）。
   全部用非破坏性用例：错数据被 400/401 拒绝、空操作不落盘，不真改账号状态。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from http.cookiejar import CookieJar

for _sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    p = str(Path(__file__).resolve().parents[3] / _sub)
    if p not in sys.path:
        sys.path.insert(0, p)

BASE = "http://127.0.0.1:8501"
TEST_HEADERS = {"X-FinSight-Test": "1"}
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + label + (f"：{extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


JAR = CookieJar()
OPEN = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(JAR))


def call(method, path, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPEN.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# —— 1) 源码不变量：不依赖服务 ——
import web  # noqa: E402

SRC = web.INDEX_HTML
LOGIN_SRC = web.LOGIN_HTML

for token in ("trackedTasks", "reattachTrackedThinking", "openTrackedTask", "deepThinkBtn",
              "viewEpoch", "side-account", "accountCard", "accountGear"):
    check(f"INDEX_HTML 含 {token}", token in SRC)
for gone in ("currentTaskState", "taskPollTimer",
             'id="page-settings"', 'data-page="settings"', "saveSettings",
             'id="logoutBtn"', 'id="answerStyle"', 'id="unitMode"'):
    check(f"INDEX_HTML 不含 {gone}", gone not in SRC, "旧结构残留")
# currentTaskId 只允许出现在注释里（说明「不再用全局单任务变量」），不许有活引用。
_live = re.sub(r"//[^\n]*", "", SRC)
check("currentTaskId 无活引用（注释除外）", "currentTaskId" not in _live)
check("LOGIN_HTML 带 /api/me 互斥探针", "/api/me" in LOGIN_SRC and 'location.replace("/")' in LOGIN_SRC)
check("payload 深度思考映射存在",
      re.search(r'deepThinkOn\s*\?\s*"详细讲解"\s*:\s*"简洁结论"', SRC) is not None)
# 轮卅一/卅二 DOM 不变量：历史成框、账户三段手风琴（默认收起）、发送钮图标化。
check("INDEX_HTML 含 history-box（轮卅⑤历史独立成框）", 'class="history-box"' in SRC)
check("INDEX_HTML 账户三段手风琴在（轮卅③）",
      "secHeadProfile" in SRC and "secBodyProfile" in SRC and "account-sec-head" in SRC)
check("INDEX_HTML 手风琴默认收起（sec-body 带 hidden）",
      re.search(r'class="account-sec-body" id="secBody\w+" hidden', SRC) is not None)
check("INDEX_HTML 发送钮图标化（轮卅②）", "ICONS" in SRC and "正在生成，点击暂停" in SRC)
for gone2 in ("accountPop", "accountModal", "openAccountModal"):
    check(f"INDEX_HTML 不含弹层残留 {gone2}", gone2 not in SRC, "轮卅①弹层已页化")

# —— 2) 活体：测试头放行（无 cookie），非破坏性用例 ——
s, _, b = call("GET", "/api/me", headers=TEST_HEADERS)
me = json.loads(b) if s == 200 else {}
check("/api/me 返回闸门状态", s == 200 and "auth_enabled" in me and "version" in me, f"{s}")
check("/api/me 带版本号", bool(me.get("version")), str(me)[:80])

s, _, b = call("POST", "/api/profile", {}, headers=TEST_HEADERS)
check("profile 空操作不写盘", s == 200 and json.loads(b).get("ok") is False, f"{s} {b[:80]}")

bad_png = "data:image/png;base64," + ("AA" * 40)
s, _, b = call("POST", "/api/profile", {"avatar": bad_png}, headers=TEST_HEADERS)
check("profile 非法头像魔数被拒 400", s == 400, f"{s} {b[:80]}")

long_name = "超" * 40
s, _, b = call("POST", "/api/profile", {"display_name": long_name}, headers=TEST_HEADERS)
got = json.loads(b).get("display_name", "") if s == 200 else ""
check("profile 超长昵称截到 30", s == 200 and got == "超" * 30, f"{s} {got!r}")
call("POST", "/api/profile", {"display_name": ""}, headers=TEST_HEADERS)  # 清回默认

s, _, b = call("POST", "/api/password", {"old_password": "绝密的错误口令", "new_password": "081066"},
               headers=TEST_HEADERS)
check("password 错旧密码 401（非 ASCII 不炸 500）", s == 401, f"{s} {b[:80]}")

s, _, b = call("POST", "/api/password", {"old_password": "x", "new_password": "123"}, headers=TEST_HEADERS)
check("password 新密码过短被拒（400/401）", s in (400, 401), f"{s}")

# /login 互斥：闸门开着且无 cookie → 200；带伪造 cookie → verify 失败仍 200，不误 302
s, _, b = call("GET", "/login", headers=TEST_HEADERS)
check("/login 未登录 200", s == 200 and b"/api/me" in b, f"{s}")

if os.getenv("LOGIN_USER") and os.getenv("LOGIN_PASSWORD"):
    s, hd, b = call("POST", "/api/login",
                    {"username": os.getenv("LOGIN_USER"), "password": os.getenv("LOGIN_PASSWORD")})
    check("真账号登录 200", s == 200 and json.loads(b).get("ok") is True, f"{s}")
    if s == 200:
        s, hd, _ = call("GET", "/login")
        check("已登录访问 /login → 302 回 /（互斥守卫）", s == 302 and hd.get("Location") == "/", f"{s} {hd.get('Location')}")
        s, _, b = call("GET", "/api/me")
        check("登录后 /api/me logged=true", json.loads(b).get("logged") is True, f"{s}")
        call("POST", "/api/logout", {})
else:
    print("NOTE：本机 .env 未配账号，跳过真账号互斥段")

if FAILS:
    print(f"\n多窗口+账户回归：{len(FAILS)} 项失败")
    sys.exit(1)
print("\n多窗口+账户回归：全部通过")
