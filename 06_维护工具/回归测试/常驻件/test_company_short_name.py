"""常驻回归件：证券简称识别。

用户只写简称（豪迈科技 / 中国国贸 / 首创环保 / 五粮液）时，以前必须写全称或六位代码才查得到数，
简称会退化成「倒一大段 OCR 原文」甚至「请补充需要分析的公司名称或股票代码」。
本件钉住四件事：
① 1_stock_basic.alias 列存在且被回填（来源只有两种：切片里的「股票/证券/公司简称」标签、文件名前缀）
② 抽出来的简称必须是本公司全称的子序列，且不能撞车到别的公司（防止 招商证券 被写成 美心翼申）
③ assistant.company_alias_candidates 把登记简称放进候选池
④ 端到端：只说简称也能识别到正确代码（identify_company_from_question + 本地 identify_company_filter）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务", "06_维护工具"):
    sys.path.insert(0, str(ROOT / sub))
sys.path.insert(0, str(ROOT))

import assistant  # noqa: E402
import data  # noqa: E402

FAIL: list[str] = []
PASS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(f"{label} :: {detail}")


# ① 列在不在
cols = {r["Field"] for r in data.query_all("SHOW COLUMNS FROM `1_stock_basic`")}
check("① alias 列存在", "alias" in cols, f"1_stock_basic 列={sorted(cols)}")

registered = data.company_short_names()
check("① 已登记简称不为空", len(registered) >= 30, f"当前 {len(registered)} 家")

# ② 抽得对不对：这几家是用户真实问过的叫法
EXPECT = {
    "002595": "豪迈科技",
    "600007": "中国国贸",
    "600008": "首创环保",
    "920833": "美心翼申",
    "000801": "四川九洲",
    "002571": "德力股份",
}
for code, want in EXPECT.items():
    got = registered.get(code) or ""
    check(f"② {code} 简称={want}", got == want, f"实际抽到 {got!r}")

# ② 反例：切片里提到别家公司的简称，不许挂到自己头上
check(
    "② 招商证券(600999) 没被写成别家简称",
    (registered.get("600999") or "") not in {"美心翼申", "股票"},
    f"实际 {registered.get('600999')!r}",
)
check(
    "② 没有任何简称抽成「股票」",
    not any(v in {"股票", "证券", "简称"} for v in registered.values()),
    str([k for k, v in registered.items() if v in {"股票", "证券", "简称"}]),
)

# ② 撞车检查：同一个简称不能属于两家
owners: dict[str, set[str]] = {}
for code, short in registered.items():
    owners.setdefault(short, set()).add(code)
dup = {s: sorted(c) for s, c in owners.items() if len(c) > 1}
check("② 简称不撞车", not dup, str(dup))

# ② 简称必须能从全称里按顺序读出（子序列），否则说明抽到了别家/噪声
def is_subsequence(short: str, full: str) -> bool:
    core = re.sub(r"^\*?ST", "", short, flags=re.I)
    it = iter(full)
    return all(ch in it for ch in core)


bad = []
for code, short in registered.items():
    full = (data.query_all(
        "SELECT stock_name FROM `1_stock_basic` WHERE stock_code=%s", (code,)
    ) or [{}])[0].get("stock_name", "")
    if not is_subsequence(short, str(full)):
        bad.append((code, short, full))
check("② 简称都是全称的子序列", not bad, str(bad[:5]))

# ③ 候选池里确实带上了登记简称
pool = assistant.company_alias_candidates("山东豪迈机械科技股份有限公司")
check("③ 候选池含 豪迈科技", "豪迈科技" in pool, str([p for p in pool if len(p) <= 6][:12]))

# ④ 端到端：只说简称
for q, want in (
    ("豪迈科技2026年半年报营业收入和净利润是多少？", "002595"),
    ("中国国贸2026年半年报的总资产和资产负债率", "600007"),
    ("首创环保上半年赚了多少？", "600008"),
):
    hit = assistant.identify_company_from_question(q) or {}
    got = str(hit.get("ticker") or hit.get("stock_code") or "")
    check(f"④ 智能识别 {q[:12]}", got == want, f"实际 {got!r}")

# ⑤ 端到端：**代码 + 紧跟年份**（2026-10-29 实测事故）
# 「000538 2026年半年报里提到的主要经营风险有哪些」压掉空格后是 `0005382026年半…`，
# `(?<!\d)(\d{6})(?!\d)` 会因代码后面紧跟数字而判它不是独立代码 ⇒ identify=None
# ⇒ 锁不上公司 ⇒ 风险分支 `if not ticker: return None` ⇒ 答「没有检索到」。
# 修复：抓代码前先把 `(?:19|20)\d{2}\s*年` 摘掉（别名匹配仍用原 compact）。
for q, want in (
    ("000538 2026年半年报里提到的主要经营风险有哪些", "000538"),
    ("6000072026年半年报 净利润", "600007"),
    ("000538 2026年半年报营业收入", "000538"),
):
    hit = assistant.identify_company_from_question(q) or {}
    got = str(hit.get("ticker") or "")
    check(f"⑤ 代码粘年份仍识别 {q[:16]}", got == want, f"实际 {got!r}")
check("⑤ 8 位日期不许被当成证券代码",
      assistant.identify_company_from_question("20250630 的营业收入") is None,
      "把日期当代码会锁错公司")

print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
for line in FAIL:
    print("  ✗", line)
sys.exit(1 if FAIL else 0)
