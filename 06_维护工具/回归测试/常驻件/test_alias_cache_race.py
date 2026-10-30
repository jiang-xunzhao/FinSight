"""别名归属缓存的并发一致性验证（ThreadingHTTPServer 多线程真实场景）。

复现的 bug：assistant.py `derived_alias_owners()` 里
    _ALIAS_OWNERS_CACHE["signature"] = signature   # 先写凭证
    _ALIAS_OWNERS_CACHE["index"] = owners          # 后写载荷
两个线程并发重建时，慢线程可以先发布凭证、再发布旧载荷，抢在快线程之后落盘，
于是缓存变成「凭证=新列表 / 索引=旧列表」。因为读路径只比凭证（237 行），
这个错配会**长期**留着：别名歧义判定用的是旧公司集合，简称明明已经唯一了
还被当成歧义 → 公司识别不到、界面变成追问。导入期间主体名/代码在变，正是
最容易撞上的窗口。

判据：每次调用后检查 (a) 缓存凭证 == 我刚刚设置的公司列表签名，
(b) 探针别名的归属数 == 该列表应有的归属数。违规次数修复前 >0、修复后必须 0。
"""
import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402

PROBE_ALIAS = "甲乙丙丁测试"


def build_variant(drop_sharer: bool) -> list[dict[str, str]]:
    """200 家公司；sharer 那家在 B 变体里被删掉，探针别名随之从歧义变唯一。

    两家必须共享同一个 4-6 字前缀切片：「甲乙丙丁测试股份…」和「甲乙丙丁测试实业…」
    都能切出「甲乙丙丁测试」。注意别写成「甲乙丙丁实测…」——那样 5 字前缀就分叉了，
    探针别名永远只有 1 个归属，测出来的全是假信号（这里踩过一次）。
    """
    companies = [
        {"stock_code": f"6{i:05d}", "stock_name": f"测试公司{i:03d}股份有限公司"}
        for i in range(200)
    ]
    companies.append({"stock_code": "700001", "stock_name": "甲乙丙丁测试股份有限公司"})
    if not drop_sharer:
        companies.append({"stock_code": "700002", "stock_name": "甲乙丙丁测试实业有限公司"})
    return companies


VARIANTS = {
    "A": build_variant(False),  # 探针别名 2 个归属（歧义）
    "B": build_variant(True),   # 探针别名 1 个归属（唯一）
}
EXPECT = {"A": 2, "B": 1}
CURRENT = {"which": "A"}

# 把公司列表换成测试可控的版本，并保证缓存不会命中真实库
assistant.cached_company_list = lambda ttl=30.0: VARIANTS[CURRENT["which"]]
assistant._ALIAS_OWNERS_CACHE.clear()
assistant._COMPANY_ALIAS_MEMO.clear()

violations: dict[str, int] = {"credential": 0, "payload": 0, "torn": 0, "calls": 0}
lock = threading.Lock()
stop_at = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 6.0)


def signature_of(which: str) -> tuple:
    return tuple(sorted(str(item["stock_code"]) for item in VARIANTS[which]))


def worker(idx: int) -> None:
    rng = random.Random(idx)
    local = {"credential": 0, "payload": 0, "torn": 0, "calls": 0}
    while time.time() < stop_at:
        which = rng.choice(["A", "B"])
        CURRENT["which"] = which
        owners = assistant.derived_alias_owners()
        cached_sig = assistant._ALIAS_OWNERS_CACHE.get("signature")
        got = len(owners.get(PROBE_ALIAS, set()))
        # 只有「凭证声称对应当前列表、载荷却属于另一个列表」才是真 bug——
        # 读路径就信凭证，这种错配会长期留着。单纯凭证不同只是被别的线程翻了列表，
        # 属于正常竞争，只计数不判失败。
        if cached_sig == signature_of(which) and got != EXPECT[which]:
            local["torn"] += 1
        elif cached_sig == signature_of("A" if which == "B" else "B"):
            local["credential"] += 1
        if got != EXPECT[which]:
            local["payload"] += 1
        local["calls"] += 1
    with lock:
        for key, value in local.items():
            violations[key] = violations.get(key, 0) + value


threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

calls_note = "8 线程并发重建"
print(
    f"{calls_note}：共 {violations['calls']} 次调用；单次调用采样到的过期 {violations['credential']} 次"
    f"（这只代表竞争激烈，不判失败）"
)

# 判据 1（确定性，不靠运气撞窗口）：手工造出老代码能产生的错配缓存
# ——「凭证说是 A 的列表、索引其实是 B 的列表」，读路径必须发现载荷不成对并重算。
owners_b = {}
for item in VARIANTS["B"]:
    for alias in assistant.company_alias_candidates(str(item["stock_name"]), unique_only=False):
        owners_b.setdefault(alias, set()).add(str(item["stock_name"]))
assistant._ALIAS_OWNERS_CACHE.clear()
assistant._COMPANY_ALIAS_MEMO.clear()
assistant._ALIAS_OWNERS_CACHE["signature"] = signature_of("A")
assistant._ALIAS_OWNERS_CACHE["index"] = owners_b
CURRENT["which"] = "A"
served = assistant.derived_alias_owners()
stale_served = len(served.get(PROBE_ALIAS, set())) != EXPECT["A"]
print(
    f"判据1 手工注入错配缓存（凭证=A/载荷=B）：服务到的归属 "
    f"{len(served.get(PROBE_ALIAS, set()))}（应 {EXPECT['A']}）"
    f" -> {'仍在供错数据' if stale_served else '已识别并重算'}"
)

# 判据 2：并发停下来之后，把列表钉死成某一个变体，读路径必须给出与它匹配的载荷。
# 老代码「先写凭证、后写载荷」，两个线程交叉发布就会留下 凭证=新 / 索引=旧 的组合，
# 而读路径只比凭证（237 行），所以这种错配会一直留着——用户看到的就是
# 「简称明明已经唯一了还被当成歧义，公司识别不到、界面变成追问」。
pin_failures = []
assistant._ALIAS_OWNERS_CACHE.clear()
assistant._COMPANY_ALIAS_MEMO.clear()
for which in ("A", "B", "A", "B"):
    CURRENT["which"] = which
    owners = assistant.derived_alias_owners()
    got = len(owners.get(PROBE_ALIAS, set()))
    cached_sig = assistant._ALIAS_OWNERS_CACHE.get("signature")
    if got != EXPECT[which]:
        pin_failures.append(f"{which}: 载荷给出 {got} 个归属，应为 {EXPECT[which]}")
    print(
        f"  钉住 {which}：归属 {got}（应 {EXPECT[which]}），缓存凭证匹配"
        f"={'是' if cached_sig == signature_of(which) else '否'}"
    )

race_free = not pin_failures
ok = race_free and not stale_served
for line in pin_failures:
    print(f"FAIL 并发后残留长期错配 -> {line}")
if stale_served:
    print("FAIL 读路径只信凭证，把不成对的旧索引当新数据供了出去")
print(f"{'PASS' if ok else 'FAIL'} 并发下不许留下「新凭证 + 旧索引」的长期错配")
sys.exit(0 if ok else 1)
