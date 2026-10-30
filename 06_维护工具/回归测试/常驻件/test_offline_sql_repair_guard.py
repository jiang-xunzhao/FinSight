"""钉住两条实测缺陷的修法：
① 离线模式（`api_key` 为空）**绝不构造云端 client**。原缺陷：`repair_sql_using_llm`
   一上来就 `make_client("")` ⇒ OpenAI SDK 抛 `Missing credentials. Please pass an
   api_key…`，被 `run_with_correction` 记成 repair_failed ⇒ 评测/日志把这条第三方文案
   当成「修复失败原因」；若机器上恰好有 `OPENAI_API_KEY`，离线模式还会静默打到
   api.openai.com。⇒ 修法在 `04_智能分析/assistant.py:1940` 的 `repair_sql_using_llm`。
② SQL 纠错基准件的统计口径与假基准。原缺陷：`degraded = [s for s in samples if not s["repaired"]]`
   把「本来就没异常」的样本也算降级（实测 降级 5 == 样本 5）；`SQL-FIX-003` 的期望
   `net_profit=121275639.08` 在文档里 0 命中（真值 1,168,715,925.13），于是「明明已经
   改对 SQL 并查回真值」的样本被判修复失败 ⇒ 报告打出纠错成功率 0.00%（假红）。
   ⇒ 修法在 `06_维护工具/correction_benchmark.py`。
不发任何网络请求、不写库。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import assistant  # noqa: E402
import data  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{'' if ok else ' :: ' + detail}")
    if not ok:
        failures.append(name)


def source_of(func: object) -> str:
    return inspect_source(str(func.__code__.co_filename), int(func.__code__.co_firstlineno))


def inspect_source(path: str, start_line: int) -> str:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    for line in lines[start_line - 1 :]:
        body.append(line)
        # 函数体到下一个顶层 def/class 为止
        if len(body) > 1 and re.match(r"^(def |class |@)", line):
            body.pop()
            break
    return "\n".join(body)


# ---------------------------------------------------------------- ① 离线不建 client
src = source_of(assistant.repair_sql_using_llm)
guard_at = src.find("不进行云端修复")
client_at = src.find("make_client(")
check("守卫在 make_client 之前", 0 < guard_at < client_at, f"guard={guard_at} client={client_at}")

calls: list[str] = []
real_make_client = assistant.make_client


def exploding_make_client(api_key: str, base_url: str):  # noqa: ANN001
    raise AssertionError(f"make_client 被调用了：api_key={api_key!r}")


assistant.make_client = exploding_make_client
try:
    assistant.repair_sql_using_llm(
        "查询营业收入",
        "SELECT missing_metric FROM 2_performance_report",
        "(1054, \"Unknown column 'missing_metric'\")",
        {"api_key": "", "base_url": assistant.DEEPSEEK_BASE_URL, "llm_model": assistant.DEEPSEEK_MODEL},
    )
    check("离线时必须报错停下", False, "没抛异常")
except AssertionError as exc:  # 说明仍去建 client
    check("离线时不建云端 client", False, str(exc))
except ValueError as exc:
    message = str(exc)
    check("离线时报的是自己的话", "离线模式" in message, message)
    check("离线时不再甩第三方凭据文案", "Missing credentials" not in message, message)
    check("离线时没走到建连", True)
finally:
    assistant.make_client = real_make_client

# 正向对照：带了密钥就必须真的往下走去建连（用哨兵拦住，不发网络请求）
sentinel = object()


def sentinel_make_client(api_key: str, base_url: str):  # noqa: ANN001
    calls.append(api_key)
    return sentinel


assistant.make_client = sentinel_make_client
try:
    assistant.repair_sql_using_llm(
        "查询营业收入",
        "SELECT missing_metric FROM 2_performance_report",
        "boom",
        {"api_key": "sk-present-but-not-used", "base_url": assistant.DEEPSEEK_BASE_URL},
    )
    check("有密钥时守卫不拦", calls == ["sk-present-but-not-used"], f"calls={calls}")
except Exception as exc:  # 建连之后必然还要发请求，这里允许它在哨兵之后失败
    check("有密钥时守卫不拦", calls == ["sk-present-but-not-used"], f"calls={calls} exc={exc}")
finally:
    assistant.make_client = real_make_client

# 闭环语义：repair 抛异常 ⇒ run_with_correction 记 repair_failed 并放弃（不浪费第二次执行）
outcome = assistant.run_with_correction(
    "SELECT 1",
    lambda _sql: [],
    lambda _sql, _rows: "SQL 查询结果为空",
    lambda _sql, _error: assistant.repair_sql_using_llm(
        "q", "SELECT 1", "SQL 查询结果为空", {"api_key": ""}
    ),
    max_attempts=2,
)
statuses = [str(entry.get("status")) for entry in outcome.get("log") or []]
check("闭环仍记 failed + repair_failed", statuses == ["failed", "repair_failed"], f"{statuses}")
check("闭环不会重复执行", outcome.get("attempts") == 2, f"attempts={outcome.get('attempts')}")

# ---------------------------------------------------------------- ② 基准件口径与真基准
bench_path = ROOT / "06_维护工具" / "correction_benchmark.py"
spec = importlib.util.spec_from_file_location("correction_benchmark", bench_path)
assert spec is not None and spec.loader is not None
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

bench_src = bench_path.read_text(encoding="utf-8")
degraded_line = next(
    (line.strip() for line in bench_src.splitlines() if line.strip().startswith("degraded =")), ""
)
check(
    "降级判据只算检出异常的样本",
    "for item in detected" in degraded_line,
    degraded_line,
)

by_id = {case["id"]: case for case in bench.CASES}
fixture = (by_id["SQL-FIX-003"].get("expected_rows") or [{}])[0]
rows = data.query_all(
    "SELECT revenue, net_profit FROM 2_performance_report "
    "WHERE stock_code=%s AND report_period=%s LIMIT 1",
    ("600008", "2026年半年报"),
)
check("SQL-FIX-003 样本在库里有行", bool(rows), str(rows))
if rows:
    live = rows[0]
    for column in ("revenue", "net_profit"):
        expected = str(fixture.get(column) or "")
        actual = f"{float(live[column] or 0):.4f}"
        check(
            f"SQL-FIX-003 期望值 {column} 与库一致",
            expected == actual,
            f"fixture={expected} 库里={actual}",
        )
    check(
        "假基准已清除（不再是 121275639.08）",
        "121275639.08" not in bench_src,
        "文件里仍能搜到 121275639.08",
    )

print()
if failures:
    print(f"===== 离线修复守卫与纠错基准：{len(failures)} 项失败 =====")
    raise SystemExit(1)
print("===== 离线修复守卫与纠错基准：全绿 =====")
