"""轮十一：三处「静默吞异常」改成只加日志的源码守卫 + 行为不变实测。

背景（2026-10-28 静态 AST 审计，276 个函数）：类别1/3-8 全为 0，唯一有料的是类别2 吞异常。
用户决定：**只加日志，不改行为**。改的三处：
  ① `03_数据层/data.py` `read_pdf` 每页表格提取块（原 `except Exception: pass` ⇒ 某页表格
     静默丢失，事后无从判断）。探针 `_tools/_hist/probe_swallow_tables_20261028.log`
     实测现有样本 0 次触发 ⇒ 是潜在风险，不是现网缺陷。
  ② `04_智能分析/assistant.py:175-185` `classify_question_task` 里 `extract_metrics`
     异常被吞 ⇒ 意图静默降级成 general，整条链路换掉却没痕迹。
  ③ `03_数据层/data.py` 文档索引写库 / 写文件两处（`[文档索引]`）。
日志走 `print`：`finsight_launch.py:25` 把服务 stdout 重定向到 `%TEMP%\\finsight-server.log`，
且带 `-u` 实时落盘，所以不需要新日志基建（与既有 `[指标提取]` 同风格）。
本件不建库、不写库、不调模型。
"""
from __future__ import annotations

import contextlib
import inspect
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析"):
    sys.path.insert(0, str(ROOT / sub))

import assistant  # noqa: E402
import data  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f" :: {detail}"))
    if not ok:
        FAILS.append(name)


# ============================================================ ① 三处都改成记日志（源码守卫）
read_pdf_source = inspect.getsource(data.read_pdf)
check("①read_pdf 表格提取块有日志前缀", "[表格提取]" in read_pdf_source)
check(
    "①read_pdf 表格提取块不再是空 pass",
    "except Exception:\n                    pass" not in read_pdf_source,
    [line for line in read_pdf_source.splitlines() if "except Exception" in line][-3:],
)

intent_source = inspect.getsource(assistant.classify_question_task)
check("②classify_question_task 有日志前缀", "[意图识别]" in intent_source)
check(
    "②意图分支不再是空 pass",
    "except Exception:\n        pass" not in intent_source,
    intent_source[-400:],
)

index_source = inspect.getsource(data)
check("③文档索引写库/写文件两处都有日志", index_source.count("[文档索引]") >= 2, f"count={index_source.count('[文档索引]')}")

# ============================================================ ② 行为不变：异常照旧降级，但现在留痕
original_extract = assistant.extract_metrics
try:
    assistant.extract_metrics = lambda _question: (_ for _ in ()).throw(RuntimeError("boom-探针"))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        thrown_result = assistant.classify_question_task("今天天气怎么样")
    logged = buffer.getvalue()
finally:
    assistant.extract_metrics = original_extract

check("②指标抽取抛异常时返回值仍是 general（行为未变）", thrown_result == "general", thrown_result)
check("②但这次日志里有痕迹与前缀", "[意图识别]" in logged and "boom-探针" in logged, repr(logged[:200]))

normal_result = assistant.classify_question_task("今天天气怎么样")
check("②不打日志的正常路径结果一致", normal_result == thrown_result, f"{normal_result} vs {thrown_result}")

# ============================================================ ③ 既有意图路由没被改坏
ROUTING = (
    ("豪迈科技2026年半年报净利润是多少", "financial_metric"),
    ("四川九洲2026年半年报有哪些风险", "risk_text"),
    ("豪迈科技的主营业务是什么", "operating_overview"),
    ("贵州茅台前十大股东持股情况", "shareholding_table"),
)
for question, expect in ROUTING:
    got = assistant.classify_question_task(question)
    check(f"③意图路由 {expect}", got == expect, got)

print("=" * 60)
print(f"汇总：失败 {len(FAILS)} 项" + (f" {FAILS}" if FAILS else ""))
sys.exit(1 if FAILS else 0)
