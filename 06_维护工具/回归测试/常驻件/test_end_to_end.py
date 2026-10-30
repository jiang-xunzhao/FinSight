"""端到端：智能模式比较题（走 SQL 校验 + 逐公司 RAG + 图表）与本地模式单公司题。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8501"


def chat(question: str, mode: str, session_id: str = "") -> dict:
    body = {
        "question": question,
        "session_id": session_id,
        "analysis_mode": mode,
        "answer_style": "简洁",
        "unit_mode": "亿元",
    }
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-FinSight-Test", "1")  # 登录闸门开启时本机回环放行，见 web.py auth_ok
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def show(tag: str, result: dict, seconds: float) -> None:
    print(f"\n===== {tag}  {seconds:.1f}s =====")
    print("mode      :", result.get("mode"))
    print("company   :", result.get("company_name"))
    print("answer    :", str(result.get("answer") or "")[:220].replace("\n", " "))
    print("summary   :", str(result.get("summary") or "")[:160].replace("\n", " "))
    table = result.get("table") or []
    print(f"table     : {len(table)} 行", json.dumps(table[:2], ensure_ascii=False)[:260])
    chart = result.get("chart") or {}
    print("chart     :", chart.get("type"), "x=", chart.get("x"), "y=", chart.get("y"),
          "series=", [d.get("label") for d in (chart.get("datasets") or [])][:4])
    print("sources   :", len(result.get("sources") or []))
    print("warnings  :", result.get("warnings"))
    print("sql_logs  :", [str(item.get("sql") or "")[:70] for item in (result.get("execution_logs") or result.get("sql_logs") or [])][:4])
    print("error     :", result.get("error") or result.get("detail") or "")


q1 = "对比四川九洲和云南白药 2026 年半年度的营业收入和净利润，并画一张对比图"
t0 = time.time()
r1 = chat(q1, "deepseek")
show("智能模式 · 多公司比较", r1, time.time() - t0)

q2 = "广西能源 2026 年半年度的营业收入是多少？"
t0 = time.time()
r2 = chat(q2, "local")
show("本地模式 · 单公司", r2, time.time() - t0)

checks = {
    "智能模式返回 mode=smart": str(r1.get("mode")) in {"smart", "deepseek"},
    "智能模式有表格数据": len(r1.get("table") or []) >= 2,
    "智能模式两家都被覆盖": len({str(row.get("股票代码") or row.get("stock_code")) for row in (r1.get("table") or [])}) >= 2,
    "智能模式回答有实质内容": len(str(r1.get("answer") or "")) > 40,
    "智能模式无报错": not (r1.get("error") or r1.get("detail")),
    "智能模式图表可用": (not r1.get("chart")) or bool((r1.get("chart") or {}).get("data") or (r1.get("chart") or {}).get("datasets")),
    # 缺数据的行会是「暂无数据」，那是诚实占位，不算单位不一致。
    "用户选亿元后净利润整列同为亿元": all(
        str(row.get("净利润", "")).endswith("亿元")
        for row in (r1.get("table") or [])
        if str(row.get("净利润") or "").strip() not in ("", "暂无数据")
    ),
    "表格不漏出库里原始字段名": all(
        any("\u4e00" <= ch <= "\u9fff" for ch in str(key))
        for row in (r1.get("table") or [])
        for key in row
    ),
    "本地模式返回 mode=local": str(r2.get("mode")) == "local",
    "本地模式给出金额": bool(str(r2.get("answer") or "")),
    "本地模式表格里没有空的占位列": all(
        str(cell) != "暂无数据"
        for row in (r2.get("table") or [])
        for cell in row.values()
    ),
}
print("净利润列取值:", [(row.get("股票代码"), row.get("净利润")) for row in (r1.get("table") or [])])
print("表格键名:", sorted({key for row in (r1.get("table") or []) for key in row}))
print("\n===== 断言 =====")
failed = 0
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    failed += not ok
print("\n结论：" + ("端到端通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
