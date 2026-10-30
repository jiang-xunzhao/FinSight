"""本地模式两轮追问：验证会话历史快照改动后，指代追问仍能接上公司。"""
import json
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8501"


def ask(question, session_id=None, mode="local"):
    payload = {"question": question, "analysis_mode": mode, "unit_mode": "亿元"}
    if session_id:
        payload["session_id"] = session_id
    request = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-FinSight-Test": "1"},  # 登录闸门放行标记，见 web.py auth_ok
        method="POST",
    )
    start = time.time()
    with urllib.request.urlopen(request, timeout=180) as handle:
        result = json.loads(handle.read().decode("utf-8"))
    return result, time.time() - start


failed = 0


def check(name, ok, extra=""):
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {extra}" if extra else ""))


first, t1 = ask("广西能源 2026 年半年度的营业收入是多少？")
print(f"第 1 轮 {t1:.1f}s mode={first.get('mode')} company={first.get('company_name')}")
print("   回答:", str(first.get("answer") or "")[:90])
session_id = first.get("session_id")
check("第 1 轮锁定了公司", bool(first.get("company_name")), first.get("company_name"))
check("第 1 轮有会话 id", bool(session_id), session_id)

second, t2 = ask("那净利润呢？", session_id)
print(f"\n第 2 轮 {t2:.1f}s mode={second.get('mode')} company={second.get('company_name')}")
print("   回答:", str(second.get("answer") or "")[:90])
check("追问不需要重报公司名", str(second.get("company_name") or "") == str(first.get("company_name") or ""), second.get("company_name"))
second_answer = str(second.get("answer") or "")
# 这件的本意是「追问能接上公司 + 缺指标时点名」。**库里有没有这个数是会变的**
# （广西能源补完全文后净利润就有数了），所以两种正确行为都算过：
#   缺 ⇒ 必须点名「净利润尚未入库」+ 列出已有指标 + 记进 warnings（含糊说「没有任何数据」不行）
#   有 ⇒ 必须直接把数报出来
import re  # noqa: E402

missing_named = "净利润" in second_answer and "尚未入库" in second_answer
gave_value = bool(re.search(r"净利润[^。；]{0,24}?[\d.]+\s*(?:亿|元|万)", second_answer))
check(
    "追问要么点名缺哪个指标、要么直接把数报出来",
    missing_named or gave_value,
    second_answer[:70],
)
if missing_named:
    check("追问列出该期已有指标", "已入库的指标有" in second_answer, second_answer[70:140])
    check("缺失记进 warnings", any("净利润" in str(item) for item in (second.get("warnings") or [])), second.get("warnings"))
else:
    print("  SKIP 该期净利润已在库（直接给数），缺指标话术两条不检查")
check("不再含糊说没有任何数据", "当前没有可用于回答的已入库财务数据" not in second_answer)
check("两轮都没有报错", not (second.get("error") or second.get("detail") or first.get("error")), "")

third, t3 = ask("它资产负债率怎么样？", session_id)
print(f"\n第 3 轮 {t3:.1f}s mode={third.get('mode')} company={third.get('company_name')}")
print("   回答:", str(third.get("answer") or "")[:90])
check("代词「它」也接在同一家公司", str(third.get("company_name") or "") == str(first.get("company_name") or ""), third.get("company_name"))

print("\n结论：" + ("多轮指代正常" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
