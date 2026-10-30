"""analysis_mode 词汇不对称的回归（响应写 smart、请求过去只认 deepseek）。

复现的真 bug（2026-10-27 实测）：`POST /api/chat` 用 `analysis_mode="deepseek"`
⇒ 9.4s、`mode=smart`；但把响应自己报告的 `mode` 值 `"smart"` 原样回填进请求
⇒ **0.3s、`mode=local`、`warnings=[]`** ⇒ 用户以为在用智能分析，其实静默退回本地。
根因：`configure_analysis_mode` 的白名单只认 `{local, deepseek}`，其余一律降级；
而响应侧的 `mode` 写的是 `"smart"`（`assistant.py` 的 `result["mode"]` 与
`local_fallback["mode"]` 两处）。

修法：把 smart / llm / api / intelligent 与任何含 deepseek 的写法归一到
`"deepseek"`（`-`/`_`/空格先去掉再比，大小写不敏感）。本件只验归一本身，
**不发真实请求、不碰库、不碰 Qdrant、不花钱**。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for d in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / d))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import assistant  # noqa: E402

FAIL = 0


def resolved(analysis_mode) -> str:
    """跑一遍 configure_analysis_mode，只取归一后的 analysis_mode。

    临时塞一把假密钥：智能模式缺 `DEEPSEEK_API_KEY` 会 raise（那是另一条
    正确的护栏，本件不测它），这里要的是「归一成了哪种模式」。
    """
    saved = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "test-key-not-real"
    try:
        return assistant.configure_analysis_mode({"analysis_mode": analysis_mode})["analysis_mode"]
    finally:
        if saved is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = saved


def check(label: str, got, want) -> None:
    global FAIL
    if got == want:
        print(f"PASS {label}")
        return
    FAIL += 1
    print(f"FAIL {label}\n      got ={got!r}\n      want={want!r}")


# ① 响应侧的词汇回填进请求，必须还是智能模式（这条就是踩过的坑）
check("回填响应里的 smart 仍是智能", resolved("smart"), "deepseek")
check("smart 大写仍智能", resolved("SMART"), "deepseek")
check("带横线 deep-seek 仍智能", resolved("deep-seek"), "deepseek")
check("带下划线 deep_seek 仍智能", resolved("DEEP_SEEK"), "deepseek")
check("首尾空格不影响", resolved("  smart  "), "deepseek")
check("deepseek-v4 这类带型号写法仍智能", resolved("deepseek-v4-pro"), "deepseek")
check("llm 别名 → 智能", resolved("llm"), "deepseek")
check("api 别名 → 智能", resolved("api"), "deepseek")

# ② 本地模式与旧行为不能被动过
check("local 仍是本地", resolved("local"), "local")
check("LOCAL 大写仍是本地", resolved("LOCAL"), "local")
check("空值仍是本地（默认不变）", resolved(""), "local")
check("整个请求没带这个键 ⇒ 本地", assistant.configure_analysis_mode({})["analysis_mode"], "local")

# ③ 真正的乱写仍然降级为本地（不能因为扩别名就把垃圾当智能）
check("瞎写的 mode 落回本地", resolved("gpt-5"), "local")
check("中文写法落回本地", resolved("智能"), "local")

# ④ 智能模式仍要求密钥（缺 key 必须 raise，不能被静默兜底）
saved = os.environ.pop("DEEPSEEK_API_KEY", None)
try:
    assistant.configure_analysis_mode({"analysis_mode": "smart"})
    check("缺 DEEPSEEK_API_KEY 时必须报错", "没报错", "ValueError")
except ValueError as exc:
    check("缺 DEEPSEEK_API_KEY 时必须报错", str(exc), "未检测到 DEEPSEEK_API_KEY 环境变量")
finally:
    if saved is not None:
        os.environ["DEEPSEEK_API_KEY"] = saved

# ⑤ 智能模式的固定配置照旧写入（前端只读展示靠这几个字段）
os.environ["DEEPSEEK_API_KEY"] = "test-key-not-real"
try:
    req = assistant.configure_analysis_mode({"analysis_mode": "smart"})
    check("智能模式注入密钥", bool(req.get("api_key")), True)
    check("固定 base_url", req.get("base_url") == assistant.DEEPSEEK_BASE_URL, True)
    check("固定 llm_model", req.get("llm_model") == assistant.DEEPSEEK_MODEL, True)
    local_req = assistant.configure_analysis_mode({"analysis_mode": "local"})
    check("本地模式不带密钥", local_req.get("api_key"), "")
finally:
    if saved is None:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    else:
        os.environ["DEEPSEEK_API_KEY"] = saved

print(f"\n===== analysis_mode 词汇归一：{'全绿' if FAIL == 0 else f'{FAIL} 项失败'} =====")
sys.exit(1 if FAIL else 0)
