r"""常驻回归件：现金流「汇率变动」段 + 页断截断标签兜底。

历史 bug（都实测取证，不是猜的）：

1. **600085 北京同仁堂 2026 半年报 —— 汇率行被跨页截断，整行不入库**
   原文（`北京同仁堂股份有限公司_2026年半年报.pdf`，166 页）正文行实测：
     1389 `四、汇率变动对现金及现金等价物的`   ← 标签缺「影响」二字、本行无金额
     1390 `-79,697,897.58 -20,182,739.05`     ← 金额单独成行
     1391 `影响`                               ← 标签的后半截跑到下一行
     1392 `五、现金及现金等价物净增加额 -567,781,767.56 325,576,656.18`
   `match_metric` 靠「别名 ⊂ 归一后的标签」子串命中，截断标签是别名
   `汇率变动对现金及现金等价物的影响` 的**前缀**而不是超串 ⇒ 不命中 ⇒ 丢弃。
   后果：`经营+投资+筹资 = -488,083,869.98` vs 净增加额 `-567,781,767.56`，
   差额 `-79,697,897.58` **恰好就是这一行**。
   修法：`truncated_alias_metric_code()`（剥行首序号 ⇒ 长度门槛 ⇒ 别名前缀**唯一**才认）
   接进 `extract_values_from_text` 的「本行无金额」分支。

2. **600031 三一重工 2026 半年报 —— 解析一直没错，是库里没有列存它**
   `四、汇率变动对现金及现金等价物的影响 -768,603 -320,706`（单位：千元）完整可得
   ⇒ `-768,603,000`，而 `经营+投资+筹资 = -6,374,510,000` vs 净增加额 `-7,143,113,000`，
   差额恰为 `-768,603,000`。`5_cash_flow_statement` 原来只有四列 ⇒ 勾稽在库里永远配不平，
   而 `CF_BRIDGE` 校验规则**早就把 `exchange_rate_effect_cash` 当成分项**（data.py:1705）。
   修法：新增 `fx_cash` 列（DDL + 幂等 ALTER + STATEMENT_COLUMNS + 写入元组 +
   `METRIC_QUERY_MAP` + `COLUMN_LABELS` + `FINANCIAL_SCHEMA_PROMPT` + 金额列白名单）。

本件三组断言：①页断形态的汇率标签必须被收进来（放行面）②短前缀/散文不得被认成科目
（守卫面，防止放行面扩大成串号）③`fx_cash` 的入库触点必须齐（防以后有人改列忘了改映射）。
"""

from __future__ import annotations

import inspect
import sys
from decimal import Decimal
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层"):
    sys.path.insert(0, str(PROJECT / sub))

import data  # noqa: E402

FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    if str(got) == str(want):
        print(f"PASS {name}: {got}")
    else:
        print(f"FAIL {name}: got={got!r} want={want!r}")
        FAILED.append(name)


# ---------------------------------------------------------------- ① 截断标签必须放行
FX_TRIMMED = "四、汇率变动对现金及现金等价物的"
check("截断汇率标签 → exchange_rate_effect_cash",
      data.truncated_alias_metric_code(FX_TRIMMED), "exchange_rate_effect_cash")

# 600085 原样页断：标签行 → 金额行 → 残标签行
TEXT_600085 = """合并资产负债表
资产总计 29,946,012,856.72 28,689,470,431.05
负债合计 8,787,685,375.30 8,254,944,551.10
所有者权益（或股东权益）合计 21,158,327,481.42 20,434,525,879.95

合并现金流量表
项目 单位：元
一、经营活动产生的现金流量净额 1,727,991,763.46 1,610,630,401.21
二、投资活动产生的现金流量净额 -912,245,335.39 -1,235,000,000.00
三、筹资活动产生的现金流量净额 -1,303,830,298.05 -100,000,000.00
四、汇率变动对现金及现金等价物的
-79,697,897.58 -20,182,739.05
影响
五、现金及现金等价物净增加额 -567,781,767.56 325,576,656.18
"""
values: dict[str, object] = {}
data.extract_values_from_text(TEXT_600085, values, {})
fx = values.get("exchange_rate_effect_cash")
check("页断汇率行已入库（本期数）",
      getattr(fx, "current", None), Decimal("-79697897.58"))
check("页断汇率行已入库（上期数）",
      getattr(fx, "previous", None), Decimal("-20182739.05"))
check("经营活动净额仍正确（没被页断带偏）",
      getattr(values.get("net_cash_flow_operating"), "current", None),
      Decimal("1727991763.46"))

# ---------------------------------------------------------------- ② 短前缀不得放行
for label, why in (
    ("净利润", "短前缀会撞上十几个别名"),
    ("所有者权益", "短前缀会撞上权益/权益合计/权益变动"),
    ("关于净利润的说明如下", "散文，且剥完序号后首字不是科目名"),
    ("主要财务数据见下表", "散文，没有任何别名以它为前缀"),
):
    check(f"不得放行：{label}（{why}）",
          data.truncated_alias_metric_code(label), None)

# 散文里出现「经营活动产生的现金流量净额」的截断形态，但下一行是散文 ⇒ 不得污染
TEXT_PROSE = """合并现金流量表
经营活动产生的现金流量净
额如下所述，本期情况稳定。
投资活动产生的现金流量净额 -912,245,335.39 -1,235,000,000.00
"""
prose_values: dict[str, object] = {}
data.extract_values_from_text(TEXT_PROSE, prose_values, {})
check("散文行不得被当成分项金额",
      getattr(prose_values.get("net_cash_flow_operating"), "current", None), None)
check("投资活动净额照旧收到",
      getattr(prose_values.get("net_cash_flow_investing"), "current", None),
      Decimal("-912245335.39"))

# ---------------------------------------------------------------- ③ fx_cash 入库触点必须齐
cash_columns = data.MySQL.STATEMENT_COLUMNS["5_cash_flow_statement"]
check("STATEMENT_COLUMNS 含 fx_cash 且在 cash_increase 之前",
      (cash_columns.index("fx_cash") < cash_columns.index("cash_increase"),
       len(cash_columns)), (True, 5))
check("COLUMN_LABELS 含 fx_cash",
      data.COLUMN_LABELS.get("fx_cash"), "汇率变动对现金的影响")
check("METRIC_QUERY_MAP 含 汇率变动现金流",
      data.METRIC_QUERY_MAP.get("汇率变动现金流"),
      ("5_cash_flow_statement", "fx_cash", "汇率变动对现金的影响"))
check("FINANCIAL_SCHEMA_PROMPT 认这一列",
      "fx_cash" in data.FINANCIAL_SCHEMA_PROMPT, True)
# 金额列白名单是函数内的局部集合 ⇒ 用源码断言（防「加了列没进单位换算白名单」）
source = inspect.getsource(data)
check("金额列白名单含 fx_cash（单位换算）",
      'amount_fields = {' in source and '"fx_cash",\n' in source, True)
check("DDL 里有 fx_cash 列", "ADD COLUMN fx_cash" in source, True)
check("勾稽规则仍把汇率项当成分项",
      "exchange_rate_effect_cash" in source, True)

print("=" * 50)
if FAILED:
    print(f"❌ 现金流汇率/截断标签回归失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("✅ 现金流汇率段与截断标签兜底：全部 PASS")
