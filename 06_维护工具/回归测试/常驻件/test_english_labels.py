# -*- coding: utf-8 -*-
"""英文版定期报告的科目路由与取数验证（只读 live，不写库）。

背景（实测）：库里 000100 / 000568 / 000858 / 002384 / 002493 / 002508 只上传了英文版，
解析器以前只认中文科目名 ⇒ 这 6 家 `2_performance_report` 一行都没有，界面上等于空公司。

验收真值取自**各自英文版第 7 页摘要表**（合并口径、单位就是元）原文，见 EXPECT 里的注释。
用法：python test_english_labels.py
"""
from __future__ import annotations

import io
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import data  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got: object, want: object) -> None:
    if got == want:
        print(f"PASS  {name}: {got!r}")
    else:
        print(f"FAIL  {name}: got={got!r} want={want!r}")
        FAILURES.append(name)


print("### 1) 科目路由：英文写法必须落到正确编码（写法全是样本原文）")
ROUTING = [
    ("Operating revenue (RMB) 28,416,674,541.77 23,509,972,048.65 20.87%", "operating_revenue"),
    ("Operating revenues (CNY) 10,472,224,575.78 16,453,732,904.65 -36.35%", "operating_revenue"),
    ("Operating income (RMB) 3,972,387,199.38 4,607,524,703.26 -13.78%", "operating_revenue"),
    ("I. Total operating revenue 27,797,683,972.88 16,955,163,898.89", "operating_revenue"),
    ("Incl.: Operating revenue 27,797,683,972.88 16,955,163,898.89", "operating_revenue"),
    ("I Total revenues 28,416,674,541.77 23,509,972,048.65", "operating_revenue"),
    ("Net profits attributable to the 3,808,272,967 1,883,499,452 102.19%", "parent_net_profit"),
    ("V. Net profits (“-” for net losses) 575,615,406.22 702,971,897.40", "net_profit"),
    ("5. Net profit (\"-\" for net loss) 4,376,950,333.58 7,683,660,159.62", "net_profit"),
    ("IV. Total profits (“-” for total losses) 680,499,322.96 837,348,789.79", "total_profit"),
    ("III. Operating profits (“-” for losses) 679,333,174.24 840,200,588.82", "operating_profit"),
    ("Total assets (RMB) 184,999,362,605.88 189,984,270,815.47 -2.62%", "total_assets"),
    ("Total liabilities 4,869,520,524.97 5,636,169,065.25", "total_liabilities"),
    ("Total owners’ equity 120,944,389,687.82 122,180,683,645.14", "total_equity"),
    ("Total owners' equity 45,882,913,659.10 49,894,082,380.46", "total_equity"),
    ("Total liabilities and owner’s equity 16,551,968,719.72 17,208,063,114.15", "total_liabilities_and_equity"),
]
for label, want in ROUTING:
    check(f"路由 {label[:34]}…", data.match_metric(label), want)

print("\n### 2) 英文噪音行必须判 None（不拦就会污染合并口径）")
NOISE = [
    "Non-operating income 19,590,137 0.85% No",
    "Add: non-operating income 3,414,476.34 1,015,943.00",
    "it had total assets of RMB 299,631.91 million and net assets of RMB 4,400.75",
    "it recorded operating revenue of RMB 10,163.56 million and net profit of RMB 378.56 million",
    "Mr. Li Dongsheng holds 901,265,855 shares representing 6.10% of total equity",
    "comprehensive income attributable to the -2,412,906.17 -387,130.35",
]
for label in NOISE:
    check(f"噪音 {label[:36]}…", data.match_metric(label), None)

print("\n### 3) 中文科目不能因为小写化/去引号而回归")
CHINESE = [
    ("归属于上市公司股东的净利润（元） 1,133,633,584.68 1,196,828,824.50 -5.28%", "parent_net_profit"),
    ("四、净利润（净亏损以“—”号填列） 977,530,758.06 1,032,894,086.64", "net_profit"),
    ("扣除非经常性损益的净利润 1,000,000.00", None),
    ("营业收入（元） 342,048,970.79 316,809,770.98 7.97%", "operating_revenue"),
    ("资产总计 184,999,362,605.88", "total_assets"),
]
for label, want in CHINESE:
    check(f"中文路由 {label[:26]}…", data.match_metric(label), want)

print("\n### 4) 真文档逐家取数（读 PDF 原文 + 恒等式单位反解 + 量级护栏，不写库）")
# 期望值 = 各家英文版第 7 页摘要表原文（合并口径、单位=元）
EXPECT = {
    # TCL 主表是 `Unit: RMB'0,000`：实测原文 237,525,394 + 127,619,575 = 365,144,969
    # = 资产的 1/1000 ⇒ 恒等式反解 ×1000 后才是元口径（负债/权益两列即期望值）
    "000100": {"operating_revenue": "88648186929", "total_assets": "365144968892",
               "parent_net_profit": "3808272967",
               "total_liabilities": "237525394000", "total_equity": "127619575000"},
    # 000568 的净利润行是 `5. Net profit ("-" for net loss) 4,376,950,333.58 …`：
    # 行首「5.」是条目号，剥掉之后第一个数字才是金额（不剥就存成 5）。
    "000568": {
        "operating_revenue": "10472224575.78",
        "total_assets": "64325250251.07",
        # 合并利润表 `5. Net profit ("-" for net loss) 4,376,950,333.58 7,683,660,159.62`；
        # 它后面还有 `5.1.1 Net profit from continuing`（多级小节号 5.1.1 会被当金额 5.1），
        # 所以这条同时验证「多级条目号剥离 + 序数不当金额」。
        "net_profit": "4376950333.58",
    },
    "000858": {"operating_revenue": "28416674541.77", "total_assets": "184999362605.88"},
    "002384": {"operating_revenue": "27797683972.88", "total_assets": "69167411001.34"},
    "002493": {
        "operating_revenue": "129406713606.78",
        "total_assets": "402822539719.62",
        # 摘要表里标签与金额折成两行（实测原文）：
        #   `Net profit attributable to shareholders of the`
        #   `5,110,652,552.96 602,084,104.39 748.83%`
        # 自身可核验：(5,110,652,552.96 - 602,084,104.39) / 602,084,104.39 = 748.83% ✓
        # 所以 51.11 亿才是本期归母，p157 的 1,294,767,235.20 是别处的数。
        "parent_net_profit": "5110652552.96",
        # 合并利润表原文（同时验证「散文行不再抢科目」）：
        #   `IV. Total profit … 10,859,732,454.79` / `V. Net profit … 9,259,822,121.24`
        # 修复前：`Compared with … total profit increased by 4.9%` 把利润总额存成 4.9、
        # `…impact of 10% or more on the Company’s net profit` 把净利润存成 25.00。
        "total_profit": "10859732454.79",
        "net_profit": "9259822121.24",
    },
    # 002508 以前会被 p7 的问句 `1. Whether there are differences in the net profit and
    # net asset disclosed in the Financial Report under…` 抢先命中，把净利润存成 1。
    "002508": {
        "operating_revenue": "3972387199.38",
        "total_assets": "16551968719.72",
        "net_profit": "575615406.22",
        # 标签折成三行、金额在中间那行（实测原文）：
        # `Net profit attributable to` / `shareholders of the listed 578,235,101.90 711,640,958.19 -18.75%`
        "parent_net_profit": "578235101.90",
        # 同为元、恒等式成立 ⇒ 恒等式反解必须判 0、不许改动这两列
        "total_liabilities": "4869520524.97",
        "total_equity": "11610674719.67",
    },
}

print("\n### 3b) 行首条目号剥离不能砍掉真小数")
for sample, want in [
    ("1.8 亿元 营业收入", "operating_revenue"),  # 「1.」后面是数字 8 ⇒ 不许剥
    ("V. Net profits (“-” for net losses) 575,615,406.22", "net_profit"),
]:
    stripped = data.LEADING_ENUMERATOR_RE.sub(" ", sample, count=1)
    keep_decimal_ok = "1.8" in stripped if sample.startswith("1.8") else True
    check(f"条目号剥离不砍小数 {sample[:14]}…", keep_decimal_ok, True)
    check(f"剥离后仍命中科目 {sample[:14]}…", data.match_metric(stripped), want)

# 单位不一致的列必须被 drop_unit_mismatched_profits 丢掉（英文主表是千元，摘要表是元）。
# 实测 000100：归母 3,808,272,967（元）vs 净利润 2,243,266 / 营业利润 2,457,031（千元）⇒ 差 1700×。
FORBID = {"000100": ("net_profit", "operating_profit", "total_profit")}

import os  # noqa: E402
import pymysql  # noqa: E402

cfg = dict(data.db_config())
cfg.pop("cursorclass", None)
conn = pymysql.connect(**cfg)
try:
    for code, want in EXPECT.items():
        # 用 PDF 原文而不是切片重建：实测 `6_rag_chunks` 按 (page_no, chunk_index) 拼出来的
        # 顺序 ≠ 页面视觉顺序，002493 的净利润在切片序里会被附注行抢先（3,898,849.00）。
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_path, file_name FROM `7_document_index` WHERE stock_code=%s",
                (code,),
            )
            docs = sorted(cur.fetchall(), key=lambda row: os.path.getsize(row[0]) if os.path.exists(row[0]) else -1, reverse=True)
        text = ""
        for path_text, _name in docs:
            if path_text and os.path.exists(path_text):
                pages, _ = data.read_pdf(Path(path_text))
                text = "\n".join(str(page.get("text") or "") for page in pages)
                break
        if not text:
            # 没入库/已删除 ⇒ 数据面缺席，跳过；**有入库记录但磁盘找不到原文** ⇒ 仍是真问题（记录与磁盘不一致），继续红。
            if not docs:
                print(f"  SKIP {code} 未入库（没有文档记录）")
                continue
            check(f"{code} 原文可读", "文档不在磁盘上", "PDF 原文")
            continue
        values: dict[str, data.StatementValue] = {}
        previous: dict[str, Decimal] = {}
        data.extract_values_from_text(text, values, previous)
        # 与 process_financial_document 同序：先按恒等式把负债/权益换算回元，
        # 再丢数量级仍与归母不一致的利润列（英文主表常用千元/万元）。
        data.recover_balance_sheet_scale(values)
        # 与 process_financial_document 一致：量级不一致的利润表列要丢掉（英文主表常用千元）。
        mismatched = data.drop_unit_mismatched_profits(values)
        for metric in FORBID.get(code, ()):
            label = f"{code} {metric}（单位不一致必须被丢弃）"
            if metric in mismatched or metric not in values:
                print(f"PASS  {label}")
            else:
                FAILURES.append(label)
                print(f"FAIL  {label}: 实得 {values[metric].current}")
        for metric, expect_raw in want.items():
            got = values.get(metric)
            check(
                f"{code} {metric}",
                str(got.current) if got else None,
                str(Decimal(expect_raw)),
            )
        # 母公司口径不能反过来盖掉合并数：营业收入只应出现摘要表那一个值
        rev = values.get("operating_revenue")
        if rev is not None:
            check(
                f"{code} 营业收入没有被母公司表/附注改写",
                str(rev.current),
                str(Decimal(want["operating_revenue"])),
            )
finally:
    conn.close()

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项不符：{FAILURES}")
    sys.exit(1)
print("✅ 英文科目路由与取数全部符合预期")
