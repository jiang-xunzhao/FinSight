"""钉住四方案对比基准件（`06_维护工具/comparison_benchmark.py`）的基准、断言列与判据。

实测缺陷（2026-10-27 取证；`storage/comparison_report.json` 里 generated_at 2026-09-17
那份是解析层修复前跑的，数字不可比）：
① fixture 的 `net_profit` 期望值抄的是**修复前**的库值（当时解析层把归母金额串填进了
   `net_profit`、`net_profit_parent` 为空）⇒「本系统答对了真值」反被判错。逐值取证于
   PDF 原文（`6_rag_chunks`）：600008 五、净利润 1,168,715,925.13 / 归母 839,444,514.59；
   600098 净利润 992,406,445.48 / 归母 945,907,060.15；000801 五、净利润 96,309,075.55 /
   归母 81,198,372.22。题目问「净利润」⇒ 按利润表「五、净利润」行＝合并口径判。
② 断言里钉身份列会误杀答对的一方（实测两向都杀）：本系统 C01 返回行有 `stock_code` 无
   `stock_name`，仅 Text-to-SQL C04 返回行有 `stock_name` 无 `stock_code` 且数字全对，
   却因缺列被判 0/6。⇒ 期望行只钉 `revenue` + `net_profit`，口径靠**值本身**
   （填成归母 839,444,514.59 就不等）与 expected_text 的亿元数（11.69=合并、8.39=归母）锁。
③ `no_data` 题原用 600007 中国国际贸易中心，2026-10-27 实测它两张表都已有值 ⇒ 前提失效，
   换成从未入库的 600519 贵州茅台。
④ 判据 `if not rows: return True` 让「什么都不回答」也算诚实：9/17 那份报告里
   仅 Text-to-SQL 对本题返回空串 `[]`（无任何“无数据”表述）被记为通过。⇒ 改为
   「返回行即 False，且文本必须命中无数据类提示词」。
不发网络请求、不写库、不花钱。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from decimal import Decimal
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
BENCH = ROOT / "06_维护工具" / "comparison_benchmark.py"
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import assistant  # noqa: E402
import data  # noqa: E402

failures: list[str] = []

# 逐值取证于 PDF 原文（见文件头①）——本件唯一的「真基准」，改基准先改这里
TRUTH = {
    "600008": {
        "revenue": Decimal("8359349204.54"),
        "net_profit": Decimal("1168715925.13"),
        "net_profit_parent": Decimal("839444514.59"),
    },
    "600098": {
        "revenue": Decimal("25487271557.38"),
        "net_profit": Decimal("992406445.48"),
        "net_profit_parent": Decimal("945907060.15"),
    },
    "000801": {
        "revenue": Decimal("2595626743.19"),
        "net_profit": Decimal("96309075.55"),
        "net_profit_parent": Decimal("81198372.22"),
    },
}
PERIOD = "2026年半年报"
STALE = ("121275639.08", "534669504.45")  # 修复前的假基准，不得复活
REQUIRED = ("revenue", "net_profit")  # 题目实际问的两项
BANNED_IDENTITY = ("stock_code", "stock_name", "company_name", "net_profit_parent")
# 题面里没写代码，期望行又不钉身份列 ⇒ 题号到公司的对应必须由本件显式记住
CASE_CODES = {"C01": ["600008"], "C02": ["600098"], "C03": ["000801"], "C04": ["600008", "600098"]}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{'' if ok else ' :: ' + detail}")
    if not ok:
        failures.append(name)


def load_bench():
    spec = importlib.util.spec_from_file_location("comparison_bench_under_test", str(BENCH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    bench = load_bench()
    source = BENCH.read_text(encoding="utf-8")
    cases = {case["id"]: case for case in bench.CASES}
    check("基准件可导入且六题齐全", len(cases) == 6, f"实际 {sorted(cases)}")
    check("离线重判入口存在", hasattr(bench, "rescore_report") and hasattr(bench, "summarize"))

    expected_by_code: dict[str, dict[str, Any]] = {}
    # ①②：fixture == PDF 真基准，且不钉身份列
    for case in bench.CASES:
        codes = CASE_CODES.get(case["id"]) or []
        rows = case.get("expected_rows") or []
        if codes:
            check(
                f"{case['id']} 期望行数与题目公司数一致",
                len(rows) == len(codes),
                f"rows={len(rows)} codes={codes}",
            )
        for code, exp in zip(codes, rows):
            for banned in BANNED_IDENTITY:
                check(
                    f"{case['id']} {code} 期望行不钉 {banned}（缺列会误杀答对的一方）",
                    banned not in exp,
                    str(exp),
                )
            for field in REQUIRED:
                check(
                    f"{case['id']} {code} 断言了 {field}",
                    field in exp,
                    str(exp),
                )
                want = TRUTH[code][field]
                got = exp.get(field)
                check(
                    f"{case['id']} {code} 期望 {field} 与 PDF 真值一致",
                    got is not None and Decimal(str(got)) == want,
                    f"fixture={got} 真值={want}",
                )
            if code not in expected_by_code:
                expected_by_code[code] = {f: exp[f] for f in REQUIRED if f in exp}
            else:
                check(
                    f"{code} 各题期望值互相一致",
                    expected_by_code[code] == {f: exp[f] for f in REQUIRED if f in exp},
                    f"{expected_by_code[code]} vs {exp}",
                )

    # ① 的配套：prose 判据（亿元口径）必须由真值换算出来，不能各写一套
    for case in bench.CASES:
        if case["kind"] not in ("numbers", "comparison"):
            continue
        for code in CASE_CODES.get(case["id"]) or []:
            for field in REQUIRED:
                want = f"{TRUTH[code][field] / Decimal(100000000):.2f}"
                check(
                    f"{case['id']} expected_text 含 {code} 的{field}亿元数 {want}",
                    want in case["expected_text"],
                    f"expected_text={case['expected_text']}",
                )

    # ④ 库内实测 == 真基准（库里漂了也会红），并且期望行必须能被库内行匹配上
    for code, truth in TRUTH.items():
        rows = data.query_all(
            "SELECT stock_code, revenue, net_profit, net_profit_parent FROM `2_performance_report` "
            "WHERE stock_code=%s AND report_period=%s",
            (code, PERIOD),
        )
        if not rows:
            # 库是你随时在删/导的：整家公司都不在里面 ⇒ 判「数据面缺席」跳过（导入后自动恢复检查）；
            # 公司在库却没有这一期 ⇒ 是真问题，继续红。
            in_db = data.query_all(
                "SELECT 1 FROM `2_performance_report` WHERE stock_code=%s LIMIT 1", (code,)
            )
            if in_db:
                check(f"库内存在 {code} {PERIOD}", False, "查无此行")
            else:
                print(f"  SKIP 库内没有 {code}（数据面缺席，未导入/已删除）")
            continue
        for field, want in truth.items():
            got = rows[0].get(field)
            check(
                f"库内 {code} {field} == 真值",
                got is not None and Decimal(str(got)) == want,
                f"库={got} 真值={want}",
            )
        exp_row = expected_by_code.get(code)
        if exp_row:
            check(
                f"{code} fixture 期望行匹配库内真值行",
                assistant.eval_rows_match([rows[0]], [exp_row]),
                f"exp={exp_row} db={rows[0]}",
            )

    # ① 的反面：假基准不得再出现在文件里
    for stale in STALE:
        check(f"基准件不再含假基准 {stale}", stale not in source, "仍能在源码里搜到")

    questions = json.dumps([case["question"] for case in bench.CASES], ensure_ascii=False)

    # ③ 无数据题的前提必须仍然成立：这家公司库里从来没有
    for name in ("中国国际贸易中心", "600007"):
        check(f"无数据题不再用已有数据的 {name}", name not in questions, f"questions={questions}")
    no_data = [case for case in bench.CASES if case["kind"] == "no_data"]
    check("存在无数据题", bool(no_data))
    for case in no_data:
        marker = next((key for key in ("贵州茅台", "茅台") if key in case["question"]), None)
        if not marker:
            check("无数据题的公司名可识别", False, case["question"])
            continue
        basic = data.query_all(
            "SELECT COUNT(*) AS n FROM `1_stock_basic` WHERE stock_name LIKE %s",
            (f"%{marker}%",),
        )[0]["n"]
        docs = data.query_all(
            "SELECT COUNT(*) AS n FROM `7_document_index` WHERE company_name LIKE %s",
            (f"%{marker}%",),
        )[0]["n"]
        check(
            f"无数据题 {marker} 确实未入库（一旦导入要换题）",
            basic == 0 and docs == 0,
            f"1_stock_basic={basic} 7_document_index={docs}",
        )

    # ④' 判据：空回答不算诚实（只看可执行行——函数里那段说明注释原样引用了旧写法）
    body = "\n".join(
        line
        for line in inspect.getsource(bench.score_answer).splitlines()
        if not line.strip().startswith("#")
    )
    check("no_data 判据已改为「有行即否」", "if rows:" in body, body[:200])
    check("no_data 判据不再无条件放过空行", "if not rows:" not in body, body[:200])

    print(f"\n共 {len(failures)} 项失败")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
