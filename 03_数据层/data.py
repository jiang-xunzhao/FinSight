"""数据层模块。负责文档解析、财务指标提取、数据库读写、文件库和 RAG 数据管理。"""
from __future__ import annotations  # 启用延迟类型注解，兼容较早的 Python 版本

# Python 标准库：文件、网络、并发、文本解析和日期处理。
import errno  # 识别端口占用等系统错误码
import hashlib  # 计算文件和文本的 SHA256 摘要
import json  # 读写 JSON 配置、索引和模型结果
import math  # 判断百分比是否为 inf/NaN，避免越界值把整份文件的入库带崩
import mimetypes  # 判断上传文件和静态资源的 MIME 类型
import os  # 处理路径和操作系统相关操作
import random  # 随机生成推荐问题顺序
import re  # 使用正则表达式解析财务文本和 Markdown
import socket  # 检测本地端口和网络连接
import subprocess  # 调用 Windows OCR 和安装依赖
import ssl  # 复用 MySQL 的 TLS 上下文，避免每条连接重新枚举系统证书库
import sys  # 管理模块路径、标准输出和程序退出
import tempfile  # 创建 PDF 渲染和 OCR 的临时文件
import threading  # 提供线程锁和后台线程能力
import time  # 生成时间戳并控制重试间隔
import unicodedata  # 规范化财务字段和公司名称
import uuid  # 生成文档和会话唯一标识
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait  # 管理后台任务、并发导入与「暂停/继续/结束」的投递窗口
from dataclasses import dataclass, replace  # 定义财务指标数据结构
from datetime import date, datetime, timedelta  # 处理报告期、日期和导入时间
from decimal import Decimal, InvalidOperation  # 精确处理财务金额和比例
from email import policy  # 解析 HTTP multipart 上传数据
from email.parser import BytesParser  # 解析上传请求的邮件格式内容
from http import HTTPStatus  # 使用标准 HTTP 状态码
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # 提供基础 HTTP 服务和请求处理
from pathlib import Path  # 使用跨平台路径对象定位项目文件
from typing import Any  # 提供类型注解，提高代码可读性
from urllib.parse import parse_qs, quote, urlparse  # 解析 URL 和编码文件名
import urllib.request  # 访问本地服务并发送 HTTP 请求

# 第三方依赖：表格、PDF、Word、数据库和图像识别。
import pandas as pd  # 生成和展示财务数据表
import pdfplumber  # 提取 PDF 文本和表格
import pymysql  # 连接和操作 MySQL 数据库
import pypdfium2 as pdfium  # 渲染扫描版 PDF 页面
import numpy as np  # 计算本地向量相似度
import certifi  # 提供 Hugging Face 模型下载所需的 CA 证书
from docx import Document  # 读取 Word 文档内容
from openai import OpenAI  # 调用云端大模型和嵌入模型
from pymysql.cursors import DictCursor  # 使用字典游标读取数据库结果

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# 项目内部模块：读取统一配置和共享运行状态。
from config import *  # 导入项目配置、路径和共享状态
import vector_store  # 提供 Qdrant 向量库读写

_EMBEDDER_LOCK = threading.Lock()
_LOCAL_EMBEDDER: Any = None
_VECTOR_INDEX_LOCK = threading.Lock()
_CHUNK_NAME_INDEX: dict[str, Any] = {"count": -1, "map": {}}
_DOCUMENT_INDEX_CACHE: dict[str, Any] = {"rows": None}

METRICS: dict[str, tuple[str, str]] = {
    "cash_and_equivalents": ("货币资金", "balance_sheet"),
    "accounts_receivable": ("应收账款", "balance_sheet"),
    "prepayments": ("预付款项", "balance_sheet"),
    "other_receivables": ("其他应收款", "balance_sheet"),
    "inventories": ("存货", "balance_sheet"),
    "other_current_assets": ("其他流动资产", "balance_sheet"),
    "current_assets": ("流动资产合计", "balance_sheet"),
    "long_term_equity_investment": ("长期股权投资", "balance_sheet"),
    "investment_property": ("投资性房地产", "balance_sheet"),
    "fixed_assets": ("固定资产", "balance_sheet"),
    "right_of_use_assets": ("使用权资产", "balance_sheet"),
    "intangible_assets": ("无形资产", "balance_sheet"),
    "deferred_tax_assets": ("递延所得税资产", "balance_sheet"),
    "other_non_current_assets": ("其他非流动资产", "balance_sheet"),
    "non_current_assets": ("非流动资产合计", "balance_sheet"),
    "total_assets": ("资产总计", "balance_sheet"),
    "accounts_payable": ("应付账款", "balance_sheet"),
    "contract_liabilities": ("合同负债", "balance_sheet"),
    "employee_benefits_payable": ("应付职工薪酬", "balance_sheet"),
    "taxes_payable": ("应交税费", "balance_sheet"),
    "other_payables": ("其他应付款", "balance_sheet"),
    "current_liabilities": ("流动负债合计", "balance_sheet"),
    "long_term_loans": ("长期借款", "balance_sheet"),
    "lease_liabilities": ("租赁负债", "balance_sheet"),
    "non_current_liabilities": ("非流动负债合计", "balance_sheet"),
    "total_liabilities": ("负债合计", "balance_sheet"),
    "share_capital": ("股本", "balance_sheet"),
    "capital_reserve": ("资本公积", "balance_sheet"),
    "surplus_reserve": ("盈余公积", "balance_sheet"),
    "retained_earnings": ("未分配利润", "balance_sheet"),
    "total_parent_equity": ("归属于母公司股东权益合计", "balance_sheet"),
    "minority_interest": ("少数股东权益", "balance_sheet"),
    "total_equity": ("股东权益合计", "balance_sheet"),
    "total_liabilities_and_equity": ("负债和股东权益总计", "balance_sheet"),
    "operating_revenue": ("营业收入", "income_statement"),
    "operating_cost": ("营业成本", "income_statement"),
    "taxes_and_surcharges": ("税金及附加", "income_statement"),
    "selling_expenses": ("销售费用", "income_statement"),
    "administrative_expenses": ("管理费用", "income_statement"),
    "rnd_expenses": ("研发费用", "income_statement"),
    "financial_expenses": ("财务费用", "income_statement"),
    "interest_expense": ("利息费用", "income_statement"),
    "interest_income": ("利息收入", "income_statement"),
    "other_income": ("其他收益", "income_statement"),
    "investment_income": ("投资收益", "income_statement"),
    "operating_profit": ("营业利润", "income_statement"),
    "non_operating_income": ("营业外收入", "income_statement"),
    "non_operating_expense": ("营业外支出", "income_statement"),
    "total_profit": ("利润总额", "income_statement"),
    "income_tax_expense": ("所得税费用", "income_statement"),
    "net_profit": ("净利润", "income_statement"),
    "parent_net_profit": ("归属于母公司股东的净利润", "income_statement"),
    "minority_profit_loss": ("少数股东损益", "income_statement"),
    "basic_eps": ("基本每股收益", "income_statement"),
    "diluted_eps": ("稀释每股收益", "income_statement"),
    "cash_received_from_sales": ("销售商品、提供劳务收到的现金", "cash_flow_statement"),
    "total_cash_inflow_operating": ("经营活动现金流入小计", "cash_flow_statement"),
    "cash_paid_for_goods_services": ("购买商品、接受劳务支付的现金", "cash_flow_statement"),
    "cash_paid_to_employees": ("支付给职工以及为职工支付的现金", "cash_flow_statement"),
    "taxes_paid": ("支付的各项税费", "cash_flow_statement"),
    "total_cash_outflow_operating": ("经营活动现金流出小计", "cash_flow_statement"),
    "net_cash_flow_operating": ("经营活动产生的现金流量净额", "cash_flow_statement"),
    "total_cash_inflow_investing": ("投资活动现金流入小计", "cash_flow_statement"),
    "total_cash_outflow_investing": ("投资活动现金流出小计", "cash_flow_statement"),
    "net_cash_flow_investing": ("投资活动产生的现金流量净额", "cash_flow_statement"),
    "cash_repaid_for_debts": ("偿还债务支付的现金", "cash_flow_statement"),
    "cash_paid_for_dividends_interest": ("分配股利、利润或偿付利息支付的现金", "cash_flow_statement"),
    "total_cash_outflow_financing": ("筹资活动现金流出小计", "cash_flow_statement"),
    "net_cash_flow_financing": ("筹资活动产生的现金流量净额", "cash_flow_statement"),
    "exchange_rate_effect_cash": ("汇率变动对现金及现金等价物的影响", "cash_flow_statement"),
    "net_increase_cash": ("现金及现金等价物净增加额", "cash_flow_statement"),
    "beginning_cash": ("期初现金及现金等价物余额", "cash_flow_statement"),
    "ending_cash": ("期末现金及现金等价物余额", "cash_flow_statement"),
}

_RECOMMENDATION_CACHE: list[str] = []
_RECOMMENDATION_CACHE_AT = 0.0
_RECOMMENDATION_CACHE_SECONDS = 600
_RECOMMENDATION_LOCK = threading.Lock()

COMPANY_EXCLUDE_WORDS = (
    "会计师事务所",
    "律师事务所",
    "评估有限公司",
    "资产评估",
    "审计机构",
    "保荐机构",
    "中华人民共和国注册成立",
)

ALIASES = {
    "资产总计": "total_assets",
    "资产合计": "total_assets",
    "负债合计": "total_liabilities",
    "负债总计": "total_liabilities",
    "股东权益合计": "total_equity",
    "所有者权益合计": "total_equity",
    "归属于母公司股东权益合计": "total_parent_equity",
    "归属于母公司所有者权益合计": "total_parent_equity",
    "负债和股东权益总计": "total_liabilities_and_equity",
    "负债和所有者权益总计": "total_liabilities_and_equity",
    "非流动资合计": "non_current_assets",
    "非流动负债合": "non_current_liabilities",
    "营业总收入": "operating_revenue",
    "营业总收人": "operating_revenue",
    "主营业务收入": "operating_revenue",
    # 不要把「营业总成本」当「营业成本」：前者含税金及附加与四项期间费用，
    # 利润表里它排在「其中：营业成本」之前，first-wins 会把毛利率算成负数。
    "归属于母公司股东的净利润": "parent_net_profit",
    "归属于母公司所有者的净利润": "parent_net_profit",
    # 摘要表「主要会计数据」用的是另一种写法（豪迈科技 2026 半年报第 7 页原文），
    # 以前没有别名，短的「净利润」子串先命中，归母金额就全落进 net_profit 列了。
    "归属于上市公司股东的净利润": "parent_net_profit",
    "归属母公司股东的净利润": "parent_net_profit",
    # ---- 英文科目名 ----
    # 实测库里有 6 家公司（000100/000568/000858/002384/002493/002508）**只上传了英文版**，
    # 英文标签进不了中文 METRICS ⇒ 这 6 家在库里一行事实都没有（`2_performance_report`
    # 查不到任何报告期），界面上等于空公司。写法是英文版定期报告的原文（第 7 页摘要表 +
    # 合并财务报表），例如：
    #   `Operating revenue (RMB) 28,416,674,541.77 23,509,972,048.65 20.87%`（000858）
    #   `Operating income (RMB) 3,972,387,199.38 …`（002508）、`Operating revenues (CNY) …`（000568）
    #   `Net profits attributable to the 3,808,272,967 …`（000100，OCR 把后面「listed company」吞了）
    #   `V. Net profits (“-” for net losses) 575,615,406.22`（002508 合并利润表）
    #   `Total owners’ equity 120,944,389,687.82`（000858）/ `Total liabilities and owner’s equity …`（002508）
    # 只收录**在样本里真出现过**的写法，不猜。英文报表同样是「合并表在前、母公司表在后」，
    # 而正文补充规则是先出现的赢（`extract_values_from_text` 里 `metric_code in values` 就跳过），
    # 所以第 7 页摘要表（合并口径、单位是元）会盖掉后面母公司表里的同名科目。
    "Operating revenue": "operating_revenue",
    "Operating revenues": "operating_revenue",
    "Operating income": "operating_revenue",
    "Total operating revenue": "operating_revenue",
    "Total operating income": "operating_revenue",
    "Total revenues": "operating_revenue",
    "Net profit attributable to": "parent_net_profit",
    "Net profits attributable to": "parent_net_profit",
    "Net income attributable to": "parent_net_profit",
    "Net profit": "net_profit",
    "Net profits": "net_profit",
    "Net income": "net_profit",
    "Total profit": "total_profit",
    "Total profits": "total_profit",
    "Operating profit": "operating_profit",
    "Operating profits": "operating_profit",
    "Total assets": "total_assets",
    "Total liabilities": "total_liabilities",
    "Total owners' equity": "total_equity",
    "Total owner's equity": "total_equity",
    "Total shareholders' equity": "total_equity",
    "Total liabilities and owners' equity": "total_liabilities_and_equity",
    "Total liabilities and owner's equity": "total_liabilities_and_equity",
    "Total liabilities and shareholders' equity": "total_liabilities_and_equity",
}

@dataclass
class StatementValue:
    """保存单个财务指标的数值、来源页、单位换算和置信度。"""
    metric_code: str
    metric_name: str
    statement_type: str
    current: Decimal
    previous: Decimal | None
    current_raw: str
    previous_raw: str | None
    page_no: int
    row_index: int
    confidence: Decimal
    derived: bool = False

def normalize_label(value: str) -> str:
    """规范化字段名称，便于统一匹配财务科目。

    顺带去掉中英文弯引号并统一小写：英文报告的科目名写成
    `Total owners’ equity`（U+2019）和 `Total owners' equity` 都有（实测
    000858 用 ’、002508 用 ’ 且少一个 s），不去引号就匹配不上；
    小写是为了让 `Operating revenue` / `OPERATING REVENUE` 命中同一个别名。
    """
    value = unicodedata.normalize("NFKC", value or "")
    # 「（或…）」是财报模板的**择一写法**，不是科目名的一部分，整组先去掉再删标点。
    # 不去掉会串号（实测 600085 p33）：行40「归属于母公司所有者权益（或股东权益）\n合计」
    # 只删括号会变成「归属于母公司所有者权益或股东权益合计」，里面正好含子串「股东权益合计」
    # ⇒ 命中 `total_equity` 别名，而 `total_parent_equity` 的别名「归属于母公司所有者权益合计」
    # 因为夹着「或股东权益」反而匹配不上 ⇒ 先出现的赢，行42 真正的「所有者权益合计」被挡住：
    # 库里 total_equity = 归母 13,623,250,166.47，真实权益合计 21,158,327,481.42，
    # 少的 7,535,077,314.95 恰是同一页行41 的少数股东权益 ⇒ 恒等式差 7.53B（P1）。
    value = re.sub(r"[（(]\s*或[^）)]*[）)]", "", value)
    return re.sub(r"[\s:：,，.。;；()（）\[\]【】’‘“”'\"]", "", value).lower()

def parse_number(value: str) -> Decimal | None:
    """将财务文本解析为 Decimal 数值。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    if not text or text in {"--", "-", "—", "不适用", "无"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace(" ", "")
    text = re.sub(r"(人民币|元|万元|千元|百万元|股|/股)$", "", text)
    try:
        number = Decimal(text)
    except InvalidOperation:
        match = re.search(r"[+\-]?\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = Decimal(match.group(0))
    return -number if negative else number

def detect_amount_unit_scale(text: str) -> int:
    """根据单位文字识别金额换算倍数。"""
    normalized = normalize_label(text)
    if "百万元" in normalized:
        return 1_000_000
    if "万元" in normalized:
        return 10_000
    if "千元" in normalized:
        return 1_000
    return 1

# 表头单位声明：实测兰州银行 001227「单位：人民币千元」/同仁堂 600085「单位：万元
# 币种：人民币」/东航 600115「单位：百万元」都写在表头行，而金额在后面的行里，
# 旧规则只看金额那一行 ⇒ 倍数丢失，4,179,417（千元=41.79 亿）被按元存进库，
# 跨公司比较直接差 10^3~10^6。只认「单位：…」这种声明，散文里
# 「注册资本 1,500.00 万元」不算声明，避免误乘。
UNIT_CAPTION_RE = re.compile(r"单位\s*[：:]\s*(?:人民币)?\s*(百万元|万元|千元|元)")
UNIT_CAPTION_SCALE = {"百万元": 1_000_000, "万元": 10_000, "千元": 1_000, "元": 1}
# 附注条目：只有**附注**才用阿拉伯数字编号科目（「13、应收账款」），主表用「一、二、三」。
NOTES_ITEM_LINE_RE = re.compile(r"^(\d{1,2})、([\u4e00-\u9fff]{2,14})$")

# 英文表头单位声明（实测 6 份英文半年报的原始写法）：
#   000100 TCL 主表 `Unit: RMB'0,000`（千元，p27/28/31/32/46/51/52/63/65…）、
#            p26 `Unit: RMB’000,000,000`（十亿）、附注 p68 `The unit of the notes … is: RMB’000`
#   002493 荣盛 附注 `Unit: RMB 10,000` / `Unit: RMB10,000`（万元，p23/27/46/75/170/179…）
#   002508 老板 附注 p30 `Unit: RMB 10,000`
#   000568 泸州老窖 / 000858 五粮液 / 002384 东山精密：全文没有英文声明 ⇒ 主表就是元，不受影响。
# 只认 `Unit: RMB…` 这种声明行，散文里的 `RMB 290 million` 不算（要求 unit 前缀）。
# 摘要表在第 7 页、最早的声明在第 23/26 页 ⇒ 「往上找最近声明」的 80 行窗口碰不到摘要表，
# 摘要表（元）先命中的科目不会被误乘；主表里只有资产负债表才出现的列（负债/权益）则按声明放大。
UNIT_CAPTION_EN_RE = re.compile(
    r"unit\s*[：:]?\s*(?:of\s+)?(?:the\s+)?(?:rmb|cny)\s*[’'ˊ`]?\s*"
    r"(000,000,000|0,000,000|000,000|10,000|10000|0,000|000|million|thousand)",
    re.I,
)
UNIT_CAPTION_EN_SCALE = {
    "000": 1_000,
    "0,000": 1_000,
    "thousand": 1_000,
    "000,000": 1_000_000,
    "0,000,000": 1_000_000,
    "million": 1_000_000,
    "10,000": 10_000,
    "10000": 10_000,
    "000,000,000": 1_000_000_000,
}

def caption_amount_scale(text: str) -> int:
    """取文本里第一条单位声明（中文「单位：…」或英文 `Unit: RMB'0,000`）的倍数；没声明返回 0。"""
    match = UNIT_CAPTION_RE.search(text or "")
    if match:
        return UNIT_CAPTION_SCALE[match.group(1)]
    english = UNIT_CAPTION_EN_RE.search(text or "")
    if english:
        return UNIT_CAPTION_EN_SCALE[english.group(1)]
    return 0

def line_amount_scale(lines: list[str], index: int, window: int = 80) -> int:
    """第 index 行金额应有的倍数：本行声明优先，否则往上找最近的表头声明。"""
    own = caption_amount_scale(lines[index])
    if own:
        return own
    for previous in range(index - 1, max(-1, index - window), -1):
        scale = caption_amount_scale(lines[previous])
        if scale:
            return scale
    return 1

# 文档类型：实测 920833 名下 27 份全是重大资产购买重组系列（独立财务顾问报告/
# 资产评估报告/审计报告/董事会决议/备考报表），600999 名下 15 份全是招商证券
# 作为独立财务顾问出的核查意见，文件名却统一叫「_2026年半年报」——文件名是导入
# 者起的，不能当判型依据；首页正文可靠（实测 920833 定期正文 0 份 / 临时 23 份）。
PERIODIC_REPORT_RE = re.compile(
    r"(半年度报告|年度报告|中期报告|一季度报告|第一季度报告|三季度报告|第三[一二]?季度报告|"
    r"季度报告|业绩报告|业绩快报|Semi-?annual\s*Report|Interim\s*Report|Annual\s*Report)",
    re.I,
)
NON_PERIODIC_DOC_RE = re.compile(
    r"(独立财务顾问|财务顾问报告|法律意见书|律师事务所|资产评估报告|评估报告|审计报告|"
    r"审阅报告|备考|重大资产|资产重组|重组报告书|核查意见|专项说明|自查报告|承诺函|"
    r"决议公告|决议和公告|进展公告|风险提示|问询|回复|本次交易|价格波动|股价波动|"
    r"内幕信息知情人|暂不召开股东会|独立意见)"
)
ANNOUNCEMENT_HEADER_RE = re.compile(r"公告编号\s*[：:]")

def looks_like_periodic_report(head_text: str, file_name: str = "") -> bool:
    """判断文档是不是定期报告（只有定期报告允许写财务事实）。

    判不出来按定期报告处理：宁可沿用旧口径，也不要用一个没把握的规则把真数据判没了。
    实测两类边界：920833/600999 的重组系列公告首页固定是「证券代码+简称+公告编号
    +关于本次交易…的说明」，正文里还会引用「2026 年半年度报告」，所以必须靠标题词
    判掉；而 003816 中国广核的「境内同步披露公告」首页也只有公告编号，但它正文就是
    中期报告本身，只能放行——区别在于标题区有没有交易/中介类字样。
    """
    normalized = re.sub(r"\s+", " ", head_text or "")
    title = normalized[:200]
    body = normalized[:900]
    if NON_PERIODIC_DOC_RE.search(title):
        return False
    if PERIODIC_REPORT_RE.search(title):
        return True
    if NON_PERIODIC_DOC_RE.search(body[:400]):
        return False
    if PERIODIC_REPORT_RE.search(body):
        return True
    if ANNOUNCEMENT_HEADER_RE.search(title):
        return False
    if file_name and PERIODIC_REPORT_RE.search(file_name.replace("半年报", "半年度报告")):
        return True
    return True

def detect_statement_type(text: str, fuzzy: bool = False) -> tuple[str | None, str]:
    """判断表格属于利润表、资产负债表或现金流量表。"""
    normalized = normalize_label(text)
    if "母公司资产负债表" in normalized:
        return "balance_sheet", "parent"
    if "合并资产负债表" in normalized or "资产负债表" in normalized:
        return "balance_sheet", "consolidated"
    if "母公司利润表" in normalized:
        return "income_statement", "parent"
    if "合并利润表" in normalized or "利润表" in normalized:
        return "income_statement", "consolidated"
    if "母公司现金流量表" in normalized:
        return "cash_flow_statement", "parent"
    if "合并现金流量表" in normalized or "现金流量表" in normalized:
        return "cash_flow_statement", "consolidated"
    if fuzzy and "资产" in normalized and "债" in normalized:
        return "balance_sheet", "consolidated"
    if fuzzy and "利润" in normalized:
        return "income_statement", "consolidated"
    if fuzzy and "现金流" in normalized:
        return "cash_flow_statement", "consolidated"
    return None, "consolidated"

# 百分比原文：金额列绝不能收它（营业收入占比 100.00% 不是 100 元）。
PERCENT_TEXT_RE = re.compile(r"[%％]")
PERCENT_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[%％]")
# 行首条目号（`1. Whether…` / `V. Net profits…` / `3、营业收入`）。`(?=\D)` 是为了
# 不把「1.8 亿元」这种小数开头的行砍成「8 亿元」。只允许 1~2 位数字，避开金额本身。
LEADING_ENUMERATOR_RE = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2})*|[IVXLC]{1,5})\s*[.、](?=\s*[^\d])")
# 行内金额候选：数字后面不能紧跟字母或数字。否则 `14th Five-Year Plan`（泸州老窖英文版管理层
# 讨论里，净利润真被取成 14）、`net profit of RMB 378.56 million` 这类序数/带单位的散文数会被
# 当成金额。中文不受影响（`1,133,633,584.68 元` 的数字后是空格）。
AMOUNT_IN_LINE_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?(?![A-Za-z0-9])")
# 正文补充规则里，科目名前面出现这些词 ⇒ 这行说的不是本公司合并口径。
TEXT_SUPPLEMENT_SCOPE_BAN_RE = re.compile(
    r"占比|比例|比重|业务|板块|分部|分行业|分地区|境内|境外|备考|模拟|同行业"
)
# 英文报告的口径地雷（实测样本，见 ALIASES 英文块注释）：
#   `Non-operating income 19,590,137` —— 含子串 operating income，不拦就把营业外收入当营业收入；
#   `it had total assets of RMB 299,631.91 million and net profit of RMB 378.56 million`
#     —— 说的是**子公司**、单位还是**百万**，不是本公司合并数（002493 第 28 页一整片这种话术）。
# 这里在 match_metric 里做**源头**拦截，表格路径和正文路径一起受保护。
# 注意：`normalized` 已经把空格删光了，含 \s 的模式必须拿原始文本来匹配。
EN_SCOPE_BAN_RE = re.compile(
    r"non[\s\-]?operating|\bof\s+(?:rmb|cny)\b|\b(?:million|billion|'0,000|’0,000)\b"
    r"|accounting for|representing"
    # 英文的「扣除非经常性损益后的净利润」：库里根本没有这一列，取了就是错口径。
    # 实测 000100 第 7 页 `Net profits after deducting` + `non-recurring gains and 313,988 155,874 101.44%`
    # + `losses (RMB'0,000)` —— 既没单位（万元）也不是常规净利润；
    # 002493 也有 `... excluding non-recurring 5,343,525,369.10 754,957,891.58 607.79%`。
    r"|after\s+deducting|deducting\s+non|excluding\s+non|non[\s\-]?recurring",
    re.I,
)

# 报表行的形状是「科目在行首、金额跟在后面」；散文句子只是**提到**科目。实测这三行
# 把英文报告的小整数垃圾值全带出来了（000100 净利润 9.00、002493 净利润 25.00、利润总额 4.9）：
#   `grew rapidly, driving t9’s net profit to more than double year‑on‑year. The Company`
#   `Major subsidiaries and associates with an impact of 10% or more on the Company’s net profit`
#   `Compared with the same period of the previous year, total assets grew by 4.9%, while total profit increased by`
# 所以正文补充路径额外要求：第一个金额**之前**的那段文字，去掉行首序号与「其中/加/减」这类
# 连接词之后，必须以该科目的名字开头。
HEAD_ENUMERATOR_RE = re.compile(
    r"^(?:\(?[一二三四五六七八九十百]{1,4}\)?[、.．)）]|[IVXLCivxlc]{1,5}[.、．)）]|\d{1,2}(?:\.\d{1,2})*[.、．)）])[ \u3000]*"
)
HEAD_CONNECTOR_RE = re.compile(
    r"^(?:其中|加上|减去|加|减|以及|及其|包含|含|其中:|其中：|其中·)[ \u3000]*[：:.、]?[ \u3000]*",
    re.I,
)
# 中文序号**粘在科目名上**（顿号被 OCR 吃掉）：`四净利润`、`四汇率变动对现金及现金等价物的影响`。
# 只在 `HEAD_ENUMERATOR_RE`（要求序号后带「、/.」）判定失败之后才用它兜一次。
GLUED_HEAD_ENUMERATOR_RE = re.compile(r"^[一二三四五六七八九十]{1,3}(?=[\u4e00-\u9fffA-Za-z])")

# 页间断行把科目名**截断**了：实测 600085 北京同仁堂 2026 半年报的合并现金流量表
#   1389 行 `四、汇率变动对现金及现金等价物的`（缺末尾「影响」，本行没有金额）
#   1390 行 `-79,697,897.58 -20,182,739.05`（金额单独成行）
#   1391 行 `影响`
# `match_metric` 靠「别名 ⊂ 标签」子串命中，而这里是标签 ⊂ 别名 ⇒ 认不出 ⇒ 这一项整行丢失，
# 结果现金流三列和比净增加额少 79,697,897.58（恰为该汇率项）。兜底只认**唯一且足够长**的前缀：
# 短前缀（「净利润」「所有者权益」）一律不认，免得把别的行项目接过来。
TRUNCATED_ALIAS_MIN_CHARS = 12


def truncated_alias_metric_code(label: str) -> str | None:
    """被折行/页间断掉的科目名前缀：剥掉行首序号后长度够、且**只有一个**别名以它为前缀时才认。"""
    normalized = normalize_label(label)
    if not normalized:
        return None
    for pattern in (HEAD_ENUMERATOR_RE, GLUED_HEAD_ENUMERATOR_RE):
        normalized = pattern.sub("", normalized, count=1)
    normalized = HEAD_CONNECTOR_RE.sub("", normalized, count=1)
    if len(normalized) < TRUNCATED_ALIAS_MIN_CHARS:
        return None
    hits = {code for name, code in ALIASES.items() if normalize_label(name).startswith(normalized)}
    # 别名表里**没有**「汇率变动对现金及现金等价物的影响」这一项 —— 它只作为 METRICS 的规范名存在
    # （实测 `data.ALIASES` 里值为 exchange_rate_effect_cash 的键：0 个），而 `match_metric` 两张表都查，
    # 所以只扫 ALIASES 会让截断兜底对这类「只有规范名」的科目永远失效（600085 就是这一项）。
    hits |= {code for code, (name, _statement) in METRICS.items() if normalize_label(name).startswith(normalized)}
    return hits.pop() if len(hits) == 1 else None


def _label_head_starts_with_metric(head: str, metric_code: str) -> bool:
    """剥掉序号/连接词之后的这段行首，是否以该科目的名字开头。"""
    normalized = normalize_label(head)
    if not normalized:
        return False
    for name, code in ALIASES.items():
        if code == metric_code and normalized.startswith(normalize_label(name)):
            return True
    for code, (name, _statement) in METRICS.items():
        if code == metric_code and normalized.startswith(normalize_label(name)):
            return True
    return False


def metric_label_at_line_start(head: str, metric_code: str) -> bool:
    """判断「金额之前的这段文字」是不是以该科目的名字开头（报表行），
    而不是散文句子里顺带提到科目名。"""
    text = head or ""
    for _ in range(4):
        stripped = HEAD_ENUMERATOR_RE.sub("", text, count=1)
        stripped = HEAD_CONNECTOR_RE.sub("", stripped, count=1)
        if stripped == text:
            break
        text = stripped
    if _label_head_starts_with_metric(text, metric_code):
        return True
    # 扫描件 OCR 会把顿号吃掉：实测 600007 中国国贸 2026 半年报合并利润表那行是
    # `四净利润 633844810 632708818 635098030 630706347`（行首「四」和「净利润」粘在一起），
    # `HEAD_ENUMERATOR_RE` 要求序号后面有「、/.」⇒ 粘字号判定失败 ⇒ 这张真合并利润表的
    # 净利润行不收，`net_profit` 反而被**附注**里 `41 每股收益` 段的
    # `净利润 633,458,596 632,371,194`（那是**归母**数）抢走。同一形状的还有
    # `四汇率变动对现金及现金等价物的影响 84258 …`（现金流勾稽缺这一项就是 600031 那类
    # 三列和与净增加额对不上的来源之一）。
    # 只在**常规判定失败后**再试一次剥掉紧贴的中文序号，且剥完必须以已知科目名开头 ⇒
    # 「三期限」「十、」这类散文不会被误收（剥完剩下的首字不是科目名就返回 False）。
    glued = GLUED_HEAD_ENUMERATOR_RE.sub("", text, count=1)
    if glued != text:
        glued = HEAD_CONNECTOR_RE.sub("", glued, count=1)
        return _label_head_starts_with_metric(glued, metric_code)
    return False


def parse_label_and_values(row: list[str]) -> tuple[str, list[tuple[int, str]]]:
    """提取表格行的科目名称和数值候选项。"""
    first_numeric = len(row)
    for index, cell in enumerate(row):
        if cell and not re.search(r"[\u4e00-\u9fff]", cell) and parse_number(cell) is not None and not PERCENT_TEXT_RE.search(cell):
            first_numeric = index
            break
    values = [
        (index, cell)
        for index, cell in enumerate(row[first_numeric:], start=first_numeric)
        if cell
        and not re.search(r"[\u4e00-\u9fff]", cell)
        and parse_number(cell) is not None
        # 百分比单元格绝不能当金额：实测 002195 岩山科技 2025 年报第 73 页
        # 「营业收入占比 100.00%」被取成营业收入 100.00 元，2026 半年报
        # 「利润总额 10.10%」同样取成 10.10 元。parse_number 的兜底正则会把
        # 百分号丢掉，所以必须在这里按原文拦掉。
        and not PERCENT_TEXT_RE.search(cell)
    ]
    return "".join(cell for cell in row[:first_numeric] if cell), values

def match_metric(label: str) -> str | None:
    """将财务科目名称匹配到标准指标编码。

    取**最长**命中：「归属于上市公司股东的净利润（元）…」里同时含「净利润」和
    「归属于母公司…净利润」的别名，按字典顺序命中短名会把归母金额写进 net_profit 列
    （实测全库 `net_profit_parent` 46 行全为空就是这个原因）。
    """
    normalized = normalize_label(label)
    if not normalized:
        return None
    # 英文口径地雷先拦掉（子公司散文 / 非营业收入），必须用**原始文本**判，
    # 因为 normalized 里空格已经没了，`of RMB`、`non-operating` 这类模式匹配不上。
    if EN_SCOPE_BAN_RE.search(label or ""):
        return None
    # 库里没有「扣除非经常性损益的净利润」这一列（METRICS/ALIASES/表结构都查过），
    # 摘要表里它和归母净利润紧挨着出现，以前会被短的「净利润」子串命中、当成净利润存进去。
    if "扣除非经常性损益" in normalized or "扣非" in normalized:
        return None
    for alias, code in ALIASES.items():
        if normalize_label(alias) == normalized:
            return code
    candidates: list[tuple[int, str]] = []
    for alias, code in ALIASES.items():
        normalized_alias = normalize_label(alias)
        if normalized_alias and normalized_alias in normalized:
            candidates.append((len(normalized_alias), code))
    for code, (name, _) in METRICS.items():
        normalized_name = normalize_label(name)
        if normalized_name and normalized_name in normalized:
            candidates.append((len(normalized_name), code))
    if not candidates:
        return None
    return max(candidates)[1]

def extract_current_previous_values(values: list[tuple[int, str]]) -> tuple[tuple[int, str], tuple[int, str] | None]:
    """区分本期和上期数值。"""
    if len(values) >= 2:
        first = parse_number(values[0][1])
        second = parse_number(values[1][1])
        if (
            first is not None
            and second is not None
            and abs(first) <= 1000
            and abs(second) > abs(first) * 10
        ):
            values = values[1:]
    return values[0], (values[1] if len(values) > 1 else None)

def extract_year(text: str, fallback: date) -> int:
    """从文本中提取报告年份。"""
    match = re.search(r"(20\d{2})\s*年", text[:5000])
    return int(match.group(1)) if match else fallback.year

def extract_period_year(head: str, filename_date: date) -> int:
    """报告期年份：紧贴「半年度报告/年度报告」的年份最可信。

    文件名里的日期是披露日（年报必在次年披露），拿它当期末年会把
    2025 年报存成「2026年年报」，等真 2026 年报进来时按 stock_code+report_period
    先删后插，上一年的数据被整行抹掉。

    报告期年份还不可能晚于披露年份：正文里偶发的「2030年半年度」
    （业绩承诺、可转债到期日）会误命中关键词正则，把这份数据挂到一个
    未来报告期上，之后再按 (代码, 报告期) 删插就永远删不到旧行，越攒越乱。
    实测 002030 达安基因 2026 半年报就被标成了 2030 年半年报。
    """
    match = re.search(
        r"(20\d{2})\s*年\s*(?:半年度|中期|年度|第?一季度|第?三季度)",
        head or "",
    )
    year = int(match.group(1)) if match else None
    if year is None or year > filename_date.year:
        year = extract_year(head or "", filename_date)
    if year > filename_date.year:
        year = filename_date.year
    return year

def extract_report_period(text: str, filename_date: date) -> tuple[str, date, date | None, int]:
    """从正文和文件名识别报告期及起止日期。"""
    head = (text or "")[:4000]  # 只看标题区，正文里的「半年度报告」字样不该把年报判成半年报
    year = extract_period_year(head, filename_date)
    if "半年度报告" in head or "中期报告" in head:
        period_type = "semiannual"
        period_end = date(year, 6, 30)
    elif "第一季度报告" in head:
        period_type = "first_quarter"
        period_end = date(year, 3, 31)
    elif "第三季度报告" in head:
        period_type = "third_quarter"
        period_end = date(year, 9, 30)
    elif "年度报告" in head:
        period_type = "annual"
        period_end = date(year, 12, 31)
    else:
        period_type = "semiannual"
        period_end = date(filename_date.year, 6, 30)
    period_start = {
        "annual": date(period_end.year, 1, 1),
        "semiannual": date(period_end.year, 1, 1),
        "first_quarter": date(period_end.year, 1, 1),
        "third_quarter": date(period_end.year, 7, 1),
    }[period_type]
    return period_type, period_end, period_start, period_end.year

REPORT_PERIOD_LABELS = {
    "annual": "年报",
    "semiannual": "半年报",
    "first_quarter": "一季报",
    "third_quarter": "三季报",
}

def report_period_from_metadata(metadata: dict[str, Any]) -> str:
    """用文档自己的报告期生成标签。

    文件库登记原来直接取库里最新的一期冒充：先有 2026 半年报、再补导入
    2025 年报时，那份年报会被标成「2026年半年报」，显示名与筛选条件一起错。
    """
    year = str((metadata or {}).get("period_end") or "")[:4]
    label = REPORT_PERIOD_LABELS.get(str((metadata or {}).get("period_type") or ""), "")
    if len(year) == 4 and year.isdigit() and label:
        return f"{year}年{label}"
    return ""

def extract_company_name(text: str, stock_code: str) -> str:
    """从报告正文提取公司名称。"""
    if not text:
        return ""
    lines = text.splitlines()
    for line in lines[:400]:
        compact = re.sub(r"\s+", "", line)
        for match in re.finditer(
            r"([\u4e00-\u9fffA-Za-z0-9（）()]{2,50}?(?:股份有限公司|有限公司))"
            r"(?=拟支付|拟收购|拟购买|拟出售|拟转让|拟受让)",
            compact,
        ):
            candidate = match.group(1)
            if "编制" in candidate:
                candidate = candidate.rsplit("编制", 1)[-1]
            candidate = re.sub(r"^[为系由拟向与及将把在的]+", "", candidate)
            candidate = finalize_company_name(candidate, stock_code)
            if candidate:
                return candidate
    for line in lines[:800]:
        compact = re.sub(r"\s+", "", line)
        for match in re.finditer(
            r"([\u4e00-\u9fffA-Za-z0-9（）()]{6,50}?(?:股份有限公司|有限公司))",
            compact,
        ):
            candidate = clean_company_name(match.group(1), stock_code)
            if candidate and len(candidate) >= 6:
                return candidate
    labeled_patterns = (
        r"(?:公司名称|中文名称|注册名称|上市公司名称|发行人名称)[：:\s]*"
        r"(.{2,60}?(?:股份有限公司|有限公司))",
        r"(?:证券简称|股票简称|公司简称)[：:\s]*"
        r"([\u4e00-\u9fffA-Za-z0-9]{2,20}?)(?=\s|公告编号|$)",
    )
    for line in lines[:800]:
        compact = re.sub(r"\s+", "", line)
        for pattern in labeled_patterns:
            match = re.search(pattern, compact)
            if match:
                # 「证券简称」栏本来就短（云南白药、TCL科技），只清理不设 6 字门槛；
                # 「公司名称」栏取到的是全称，清理后仍带股份有限公司字样，够用。
                candidate = clean_company_name(match.group(1).strip(), stock_code)
                if candidate:
                    return candidate
    for line in lines[:800]:
        compact = re.sub(r"\s+", "", line)
        for match in re.finditer(
            r"([\u4e00-\u9fffA-Za-z0-9（）()]{2,50}?(?:股份有限公司|有限公司))"
            r"(?=拟支付|拟收购|拟购买|拟出售|拟转让|拟受让|将|与|及|$)",
            compact,
        ):
            candidate = match.group(1)
            if "编制" in candidate:
                candidate = candidate.rsplit("编制", 1)[-1]
            candidate = re.sub(r"^[为系由拟向与及将把在的]+", "", candidate)
            candidate = finalize_company_name(candidate, stock_code)
            if candidate:
                return candidate
        for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9（）()]{2,40}?(?:股份有限公司|有限公司))", compact):
            candidate = match.group(1)
            if "编制" in candidate:
                candidate = candidate.rsplit("编制", 1)[-1]
            candidate = finalize_company_name(candidate, stock_code)
            if candidate:
                return candidate
    return ""

def extract_stock_code_from_name(path: Path) -> str:
    """从文件名提取证券代码。"""
    if is_opaque_file_stem(path.stem):
        return "FILE_" + hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8].upper()
    valid_prefix = r"(?:600|601|603|605|688|689|000|001|002|003|300|301|302|430|8\d{2})"
    match = re.search(
        rf"(?<!\d)({valid_prefix}\d{{3}})(?!\d)",
        path.stem,
    )
    if not match:
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", path.stem)
    if match:
        code = match.group(1)
        return code[-6:] if len(code) == 7 else code
    return "FILE_" + hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8].upper()

def is_opaque_file_stem(stem: str) -> bool:
    """判断文件名是否为随机 ID。"""
    return bool(re.fullmatch(r"[0-9a-fA-F]{24,}", stem or ""))

def is_generated_financial_stem(stem: str) -> bool:
    """判断文件名是否为代码日期随机码格式。"""
    return bool(
        re.fullmatch(
            r"\d{6}_\d{8}_[A-Za-z0-9]{4,12}",
            stem or "",
            flags=re.I,
        )
    )

def clean_company_name(name: str, stock_code: str = "") -> str:
    """清理公司名称中的冗余标签。"""
    candidate = re.sub(r"\s+", "", str(name or "")).strip()
    if not candidate or candidate == stock_code or is_opaque_file_stem(candidate):
        return ""
    candidate = re.split(
        r"(?:公告编号|证券代码|股票代码|证券简称|股票简称)",
        candidate,
        maxsplit=1,
    )[0]
    candidate = re.sub(r"^\d+(?=[\u4e00-\u9fffA-Za-z])", "", candidate)
    # 表号/行号粘在名字头上。实测：600115 在库里存成「2中国东方航空股份有限公司」，
    # 那个「2」是报表表格序号被一起抓进姓名列的。上市公司名称不会以 ASCII 数字开头
    # （「三六零」这类用的是中文数字），所以后面紧跟中文/字母就剥掉。
    # 英文报告的标签残留：如 Chinesename / CompanynameinChinese / sChinesename 粘在中文公司名前。
    # 只删「含小写字母」的拉丁前缀，保住 TCL 这类全大写公司名开头。
    # 注意残留与品牌会连成一串（"CompanynameinChineseTCL" + 中文名），所以要先
    # 把结尾的全大写 token（真品牌）保护起来，再删它前面的小写残留；否则
    # TCL 英文半年报的公司名会被砍成「科技集团股份有限公司」，主体变成 FILE_ 鬼影。
    prefix_match = re.match(r"([A-Za-z][A-Za-z0-9_]*)(?=[\u4e00-\u9fff])", candidate)
    if prefix_match:
        run = prefix_match.group(1)
        brand = re.search(r"[A-Z]{2,}$", run)
        cut = brand.start() if brand else len(run)
        if cut >= 4 and re.search(r"[a-z]", run[:cut]):
            candidate = candidate[cut:]
    candidate = re.sub(r"^(?:关于对|关于|对)", "", candidate)
    candidate = re.sub(r"^[为系由拟向与及将把在的]+", "", candidate)
    if any(word in candidate for word in COMPANY_EXCLUDE_WORDS):
        return ""
    return candidate

def company_name_from_file_stem(stem: str) -> str:
    """文件名兜底：切掉冒号后的报告标题，只留公司简称。

    分隔符里不能放裸数字或空白：「长  安Ｂ：2026年半年度报告」会被切成单字「长」，
    「f5793f…」这类随机名会在第一个数字处断开变成「f」。
    """
    raw = str(stem or "").strip()
    if not raw:
        return ""
    head = re.split(r"[：:（(—\-_]", raw, maxsplit=1)[0].strip()
    if re.search(r"[\u4e00-\u9fff]", head):
        head = re.sub(r"\s+", "", head)
    head = re.split(r"(?=20\d{2}\s*年)", head, maxsplit=1)[0].strip()
    head = re.sub(
        r"(?:年报|半年报|年度报告|季度报告|季报|中报|摘要|全文|正文|英文版|英文|Chinese|Version)$",
        "",
        head,
        flags=re.I,
    ).strip()
    if re.fullmatch(r"[0-9a-fA-F]{6,}", head) or len(head) < 2:
        return ""
    return head

def finalize_company_name(candidate: str, stock_code: str = "") -> str:
    """统一走一遍清理，够长才认作公司名。"""
    cleaned = clean_company_name(candidate, stock_code)
    return cleaned if len(cleaned) >= 6 else ""

def build_document_display_name(
    path: Path,
    stock_code: str = "",
    company_name: str = "",
    report_period: str = "",
    report_label: str = "",
) -> str:
    """为随机或自动化文件名生成可读的文件显示名。"""
    if not (
        is_opaque_file_stem(path.stem)
        or is_generated_financial_stem(path.stem)
    ):
        return path.name
    clean_name = clean_company_name(company_name, stock_code)
    code = stock_code if stock_code and not stock_code.startswith("FILE_") else ""
    base = "_".join(
        part
        for part in (clean_name, report_period, report_label)
        if part
    ) or code or "未识别公司"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return f"{base or '未识别公司'}{path.suffix.lower() or '.pdf'}"

def extract_stock_code_from_text(text: str) -> str:
    """从正文中提取本公司证券代码，跳过子公司/参股公司等第三方代码。"""
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text[:12000])
    other_entity = re.compile(
        r"(?:子公司|参股|控股|旗下|关联|联营|合营|投资标的|被投资单位|并购标的)"
    )
    for match in re.finditer(
        r"(?:证券代码|股票代码|证券代号|股票代号)[：:\-]?(\d{6})",
        compact,
    ):
        # 命中点前面 40 字内出现“子公司/参股/控股”等，说明说的是别的公司。
        if other_entity.search(compact[max(0, match.start() - 40) : match.start()]):
            continue
        return match.group(1)
    # 英文半年报的 Corporate Information 段：「Stock name TCL TECH. Stock code 000100」。
    # 这个「Stock name + Stock code」的组合只会出现在本公司信息表里，所以可以在更宽的
    # 窗口找；单独的 "stock code" 会撞上定义页里的子公司代码（实测 TCL 英文全文
    # 第 5 页写 "(stock code: 002129.SZ)"），那种带交易所后缀的一律不作数。
    wide = re.sub(r"\s+", "", text[:200000])
    tight = re.search(
        r"stockname.{0,60}?stockcode[：:]?(\d{6})(?![.\d])",
        wide,
        flags=re.I,
    )
    if tight:
        return tight.group(1)
    return ""

def extract_report_date_from_name(path: Path) -> date:
    """从文件名提取报告日期。"""
    for match in re.finditer(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", path.stem):
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
    return date.today()

WINDOWS_OCR_SCRIPT = r'''
param(
  [Parameter(Mandatory = $true)][string]$ImagePath,
  [Parameter(Mandatory = $true)][string]$OutputPath,
  [string]$Language = "zh-Hans"
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
})[0]
function Await-Result($Operation, [Type]$ResultType) {
  $task = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  $task.Wait(-1) | Out-Null
  $task.Result
}
$file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $ImagePath).Path)) ([Windows.Storage.StorageFile])
$stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw "Windows OCR 不可用" }
$result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$words = @(
  foreach ($line in $result.Lines) {
    foreach ($word in $line.Words) {
      $rect = $word.BoundingRect
      [pscustomobject]@{ text = $word.Text; x = [double]$rect.X; y = [double]$rect.Y; width = [double]$rect.Width; height = [double]$rect.Height }
    }
  }
)
$json = ConvertTo-Json -InputObject $words -Compress
[IO.File]::WriteAllText($OutputPath, $json, [Text.UTF8Encoding]::new($false))
'''

def render_pdf_page(path: Path, page_no: int, dpi: int) -> Path:
    """将 PDF 页面渲染为图片，供 OCR 使用。"""
    document = pdfium.PdfDocument(str(path))
    try:
        image = document[page_no - 1].render(scale=dpi / 72).to_pil()
        temp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        temp.close()
        image_path = Path(temp.name)
        image.save(image_path, "PNG")
        return image_path
    finally:
        document.close()

def ocr_words(image_path: Path, language: str) -> list[dict[str, Any]]:
    """调用 Windows OCR 识别图片文字。"""
    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", encoding="utf-8-sig", delete=False
    )
    output_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    script_path = Path(script_file.name)
    output_path = Path(output_file.name)
    output_file.close()
    try:
        script_file.write(WINDOWS_OCR_SCRIPT)
        script_file.close()
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-ImagePath",
                str(image_path),
                "-OutputPath",
                str(output_path),
                "-Language",
                language,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Windows OCR 偶发卡在 WinRT 调用里；没有超时会把整个导入线程挂死。
            timeout=120,
        )
        payload = json.loads(
            output_path.read_text(encoding="utf-8-sig")
            if output_path.exists()
            else "[]"
        )
        return payload if isinstance(payload, list) else [payload]
    finally:
        script_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)

def words_to_rows(words: list[dict[str, Any]]) -> list[list[str]]:
    """将 OCR 文字块整理为表格行。"""
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (float(item["y"]), float(item["x"]))):
        center = float(word["y"]) + float(word["height"]) / 2
        if not lines or abs(
            center
            - sum(float(item["y"]) + float(item["height"]) / 2 for item in lines[-1])
            / len(lines[-1])
        ) > max(7.0, float(word["height"]) * 0.45):
            lines.append([word])
        else:
            lines[-1].append(word)
    rows: list[list[str]] = []
    for line in lines:
        cells: list[dict[str, Any]] = []
        for word in sorted(line, key=lambda item: float(item["x"])):
            x0, x1 = float(word["x"]), float(word["x"]) + float(word["width"])
            if not cells or x0 - cells[-1]["x1"] > max(35.0, float(word["height"]) * 1.6):
                cells.append({"x1": x1, "parts": [word["text"]]})
            else:
                cells[-1]["parts"].append(word["text"])
                cells[-1]["x1"] = max(cells[-1]["x1"], x1)
        row = ["".join(cell["parts"]).strip() for cell in cells]
        row = [cell for cell in row if cell]
        if row:
            rows.append(row)
    return rows

def read_pdf(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取 PDF 正文和表格。"""
    pages: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
            except Exception:
                text = ""
            rows: list[list[str]] = []
            compact_length = len(normalize_label(text))
            recognized_chars = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text))
            recognition_ratio = recognized_chars / max(1, len(text))
            needs_ocr = compact_length < 80 or recognition_ratio < 0.35
            used_ocr = False
            if needs_ocr and getattr(page, "images", None):
                image_path = None
                try:
                    image_path = render_pdf_page(path, page_no, CONFIG["ocr_dpi"])
                    words = ocr_words(image_path, CONFIG["ocr_language"])
                    rows = words_to_rows(words)
                    ocr_text = "\n".join(" ".join(row) for row in rows)
                    ocr_recognized = len(
                        re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", ocr_text)
                    )
                    if (
                        ocr_recognized > recognized_chars * 1.15
                        or compact_length < 40
                    ):
                        text = ocr_text
                        used_ocr = True
                except Exception as exc:
                    print(f"⚠️ 第 {page_no} 页 OCR 失败，保留可提取文本：{exc}")
                finally:
                    if image_path is not None:
                        image_path.unlink(missing_ok=True)
            else:
                try:
                    for table_index, table in enumerate(page.find_tables(), start=1):
                        extracted = table.extract() or []
                        clean_rows = [
                            [str(cell).strip() if cell is not None else "" for cell in row]
                            for row in extracted
                            if any(cell not in (None, "") for cell in row)
                        ]
                        if clean_rows:
                            headers = clean_rows[0]
                            data_rows = clean_rows[1:]
                            rows.extend(data_rows)
                            tables.append(
                                {
                                    "page_no": page_no,
                                    "table_index": table_index,
                                    "headers": headers,
                                    "rows": data_rows,
                                }
                            )
                except Exception as error:
                    # 2026-10-28 静态审计（类别2）：这里整块吞异常会让「某一页表格解析失败」
                    # 变成静默丢数据，事后无从判断。探针（_tools/_hist/probe_swallow_tables_20261028.log）
                    # 实测现有样本 0 次触发 ⇒ 属潜在风险；按用户决定**只加日志、不改行为**（照旧跳过该页）。
                    print(
                        f"[表格提取] {path.name} 第{page_no}页表格提取失败，该页表格已跳过："
                        f"{type(error).__name__}: {str(error)[:160]}"
                    )
            pages.append(
                {
                    "page_no": page_no,
                    "text": text,
                    "rows": rows,
                    "is_scanned": used_ocr and not text.strip(),
                }
            )
    return pages, tables

def read_docx(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取 Word 正文和表格。"""
    document = Document(path)
    text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
    tables: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    for index, table in enumerate(document.tables, start=1):
        raw = [
            [cell.text.strip() for cell in row.cells]
            for row in table.rows
            if any(cell.text.strip() for cell in row.cells)
        ]
        if raw:
            rows.extend(raw[1:])
            tables.append(
                {
                    "page_no": 1,
                    "table_index": index,
                    "headers": raw[0],
                    "rows": raw[1:],
                }
            )
    return [{"page_no": 1, "text": text, "rows": rows, "is_scanned": False}], tables

# 一页可能并排两张报表（实测 600208 p40：页首是「合并现金流量表」标题，下面接着
# 母公司利润表的行）。按页判口径会把母公司表的数写进本公司（合并）那行 ⇒ 分层深测
# L1 报出的两条 P0 就是这个：库里 net_profit=382,273,538.06（母公司），合并真值
# 32,234,254.86（p38 `五、净利润`）。所以这里按**每张表在页面文本里的位置**找它自己的标题。
STATEMENT_TITLE_RE = re.compile(
    r"(?:母公司及合并|合并|母公司)?\s*(?:利润表|资产负债表|现金流量表)"
)


def notes_heading_offset(page_text: str) -> int | None:
    """这一页里附注起点的偏移（在**去掉空白**的页文本上算），没有则 None。

    附注标题必须**整行**成行：报表表头里就有「项目 | 附注 | 本期数」这种列名，按子串匹配会把
    每一页报表都判成附注区。两种标志：①整行「（财务/会计）报表附注 / 附注」；②整行
    「13、应收账款」这种**阿拉伯条目号 + 已知科目名** —— 主表用「一、二、三」编号，只有附注才用
    「1、2、3」编号。实测 000525 红太阳 p72「13、应收账款」、002366 融发核电 p54「13、应收账款」
    就是附注起点；而未换算的 `营业收入 637,502.92` 在 000525 **p144** 的附注表里（合并利润表在 p48，
    摘要写的真值是 `1,562,045,738.41 元`）—— 附注区的表里也带「合并资产负债表」这类字样，
    会被重新当成主报表收割，金额直接差 10^4。
    """
    normalized = re.sub(r"\s+", "", page_text)
    offset = 0
    for raw_line in page_text.splitlines():
        compact = re.sub(r"\s+", "", raw_line)
        position = normalized.find(compact, offset) if compact else -1
        if compact and len(compact) <= 24:
            if re.fullmatch(
                r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})?、?(?:财务|会计)?报表附注",
                compact,
            ):
                return position if position >= 0 else None
            item = NOTES_ITEM_LINE_RE.match(compact)
            if item and match_metric(item.group(2)):
                return position if position >= 0 else None
        offset += len(compact)
    return None


def has_notes_heading(page_text: str) -> bool:
    """这一页是否进入「附注」区（判据见 `notes_heading_offset`）。"""
    return notes_heading_offset(page_text) is not None


def statement_titles(normalized_page: str) -> list[tuple[int, str, str]]:
    """返回页面上每张报表标题的 (位置, 表类型, 口径)，按出现顺序。"""
    found: list[tuple[int, str, str]] = []
    for match in STATEMENT_TITLE_RE.finditer(normalized_page):
        head = match.group(0)
        scope = "parent" if head.startswith("母公司") and "及合并" not in head else "consolidated"
        statement_type, _ = detect_statement_type(head)
        if statement_type:
            found.append((match.start(), statement_type, scope))
    return found


def table_first_label(rows: list[list[str]]) -> str:
    """取表格里第一个「像科目名」的单元格，用来在页面文本里定位这张表。"""
    for row in rows:
        for cell in row:
            text = re.sub(r"\s+", "", str(cell or ""))
            if len(text) >= 4 and not AMOUNT_IN_LINE_RE.fullmatch(text):
                return text
    return ""


def table_statement_type(rows: list[list[str]]) -> str | None:
    """按**这张表自己的科目**投票它属于哪张主报表（资产负债表 / 利润表 / 现金流量表）。

    位置规则（标题偏移 <= 表格片段偏移）在扫描件上会失手：实测 000525 p48 的「合并利润表」标题
    落在页内文本偏移 895，而该页表格片段起点 index=29 ⇒ 位置规则判定「这张表在标题之前」，
    于是继承了上一页的母公司口径整张跳过。科目名不会骗人：一行里出现「营业收入 / 净利润」
    就是利润表，出现「资产总计 / 负债合计」就是资产负债表。
    命中少于 2 个科目时不投票（宁可不判，也不要瞎猜把数灌错表）。
    """
    votes: dict[str, int] = {}
    total = 0
    for row in rows:
        label, numeric = parse_label_and_values(row)
        if not numeric:
            continue
        code = match_metric(label)
        if not code:
            continue
        total += 1
        target = METRICS[code][1]
        votes[target] = votes.get(target, 0) + 1
    if total < 2 or not votes:
        return None
    return max(votes, key=lambda item: votes[item])


# 「股本变动对最近一年和最近一期基本每股收益和稀释每股收益、归属于公司普通股股东的每股净资产
# 等财务指标的影响」那张表**不是主报表**，但它的行名就叫「基本每股收益」⇒ 科目投票
# （:1290 `table_statement_type`）把它判成 income_statement 收进合并口径。
# 实测 002595 山东豪迈机械科技 2026 半年度报告全文 p38（第三节 股份变动），pdfplumber 抽出的
# 这张表就三行、**表头「项目 股本变动前 股本变动后」不在表内**：
#   [基本每股收益（元/股）, '', '', 3.0005, 2.0693]
#   [稀释每股收益（元/股）, '', '', 3.0005, 2.0693]
#   [归属于公司普通股股东的每股净资产（元/股）, '', '', 15.03, 10.37]
# 3.0005 是 **2025 年度**按转增前股本重算的每股收益；真值在 p7 主要会计数据、p50 合并利润表尾、
# p135 净资产收益率表 三处一致 = **0.9802**（错值比真值大 3 倍，且 p38 在文档顺序里排在真报表之前
# ⇒ 撞上「同一标签首次出现即锁定」⇒ 真值再也进不来）。
# 判据只能用**表在页内的上文**（表头不在表里，按表头判永不触发——第一版就是这么失效的）：
# 上文同时出现「股份变动/股本变动」和「变动前/变动后」⇒ 这是重算影响表，整张不收。
# 真报表不会长这样：合并利润表的上文是「八、每股收益：」，主会计数据表的上文是「主要会计数据」。
RESTATED_CONTEXT_RE = re.compile(r"(?:股份变动|股本变动)[^。]{0,80}(?:变动前|变动后)|变动前\s*变动后")


def restated_indicator_table(rows: list[list[str]], context_before: str = "") -> bool:
    """这张表是「股本变动对指标的影响」重算表吗？表内前 2 行或页内上文命中即算。"""
    head = " ".join(" ".join(str(cell or "") for cell in row) for row in (rows or [])[:2])
    text = f"{head} {context_before or ''}"
    if not RESTATED_CONTEXT_RE.search(text):
        return False
    # 只在表里确实有「每股收益」这类指标行时才拦：纯股份变动表本来也收不到东西，不必扩大影响面。
    return bool(re.search(r"每股收益|每股净资产", text))


def extract_values(
    pages: list[dict[str, Any]], report_text: str, tables: list[dict[str, Any]] | None = None
) -> tuple[dict[str, StatementValue], dict[str, Decimal]]:
    """从报表页面提取标准财务指标。

    给了 `tables` 就按**每张表**判类型与口径（一页两张表时这是唯一正确的做法）；
    没给就退回按页判（旧行为，docx / 无表格结构时仍走这条路）。
    """
    harvest: list[tuple[list[list[str]], str, int, int]] = []
    if tables:
        page_map = {page["page_no"]: page for page in pages}
        # 中国财报的每张报表都**跨页**：实测 600208 p37 是「合并利润表」开头，p38 没有标题
        # 但接着列 `五、净利润 32,234,254.86`（合并真值）；p39 是「母公司利润表」，p40 没有标题
        # 却接着列 `四、净利润 382,273,538.06`（母公司数），而 p40 页底才是「合并现金流量表」。
        # 所以口径必须**按文档顺序延续**（running），而不是按页取第一个标题：
        #   · p38 无标题 ⇒ 继承 p37 的合并 ⇒ 合并净利润先入库；
        #   · p40 无标题 ⇒ 继承 p39 的母公司 ⇒ 整张跳过，母公司的数进不来。
        running: tuple[str, str] | None = None
        running_scale = 0
        pending: tuple[str, str] | None = None
        in_notes = False
        for table in tables:
            page = page_map.get(table["page_no"])
            if page is None:
                continue
            normalized_page = re.sub(r"\s+", "", page["text"])
            if in_notes:
                continue
            titles = statement_titles(normalized_page)
            key = table_first_label(table["rows"])
            index = normalized_page.find(key) if key else -1
            notes_at = notes_heading_offset(page["text"])
            if notes_at is not None:
                # 主报表一定排在附注之前 ⇒ 进入附注区后**后面的表全部不再收割**：附注里的表也带
                # 「合并资产负债表」这类字样，会被重新当成主报表。实测 000525 红太阳 p144 的附注表把
                # `营业收入 637,502.92`（万元、还是分部/母子公司列）灌进了合并营业收入，而合并利润表在
                # p48、摘要写的真值是 `1,562,045,738.41 元` ⇒ 金额差 10^4；002366 融发核电同形状
                # （p96 的 `46,017.73` vs 摘要真值 `361,904,468.86 元`）。
                # 但本页附注标题**之前**的表照收（有些 PDF 把最后一张表和附注首页排在同一页）。
                running = None
                running_scale = 0
                if index < 0 or index >= notes_at:
                    in_notes = True
                    continue
            new_title_here = False
            matched_by_position = False
            for offset, title_type, title_scope in titles:
                if index >= 0 and offset <= index:
                    if running != (title_type, title_scope):
                        new_title_here = True
                    matched_by_position = True
                    running = (title_type, title_scope)
            if matched_by_position:
                pending = None
            after = [(title_type, title_scope) for offset, title_type, title_scope in titles if index < 0 or offset > index]
            if after:
                pending = after[-1]
            # **内容兜底**（实测 000525 / 002366 的位置规则失手形状）：扫描件里报表标题在页内文本流中
            # 常常落在表格片段**之后**（000525 p48 合并利润表标题在偏移 895，而该页表片段起点 index=29；
            # p49 干脆没有标题），于是「按位置延续」让 p49 的合并利润表继续用上一页母公司资产负债表的
            # `parent` 口径 ⇒ 被整张跳过 ⇒ `net_profit` 从 104,095,143.10 退化成 None。
            # 判据用**这张表自己的科目投票**：投票类型与继承到的类型不一致 ⇒ 说明换了一张报表，
            # 用本页同名标题、否则用上一页**尚未被任何表格消费掉**的标题（`pending`）；
            # 投票类型与继承一致 ⇒ 就是同一张报表的续页（600208 p38/p40/p41 的形态），保持继承。
            vote = table_statement_type(table["rows"])
            if vote and (running is None or running[0] != vote):
                adopted: tuple[str, str] | None = None
                for _offset, title_type, title_scope in titles:
                    if title_type == vote:
                        adopted = (title_type, title_scope)
                if adopted is None and pending is not None and pending[0] == vote:
                    adopted = pending
                if adopted is not None and adopted != running:
                    new_title_here = True
                    running = adopted
            if running is None:
                # 定位不到、前面也没有标题 ⇒ 不猜（旧写法拿页底标题当整页标题用，正是这次串口径的原因）
                continue
            statement_type, page_scope = running
            if page_scope == "parent":
                continue
            # 单位声明**同样属于整张报表**：实测 600031（三一重工）「单位：千元」只在报表首页声明，
            # 续页没有 ⇒ 按页取倍数会把续页的数按「元」入库，同一行里出现
            # 「营业收入 53,506,463,000（×1000 了）+ 净利润 5,724,043（没 ×1000）」两种倍率
            # （600031 / 000525 红太阳 / 002366 融发核电 实测都是这个形状，差额正好 10^3~10^4）。
            # ⇒ 本页有声明就用并记住；续页没有就沿用这张报表先前的声明；
            #   新报表开头却没有任何声明 ⇒ 不沿用上一张的单位，退回按页猜。
            here = normalized_page[:index] if index > 0 else normalized_page
            page_scale = caption_amount_scale(here) or caption_amount_scale(normalized_page)
            if page_scale:
                running_scale = page_scale
            elif new_title_here:
                running_scale = 0
            unit_scale = (
                page_scale
                or running_scale
                or detect_amount_unit_scale(page["text"][:1500])
            )
            # 「股本变动对最近一年和最近一期基本每股收益…的影响」那张重算表不是主报表
            # （判据与实测见 `restated_indicator_table` 上方注释）⇒ 整张跳过，不动 running。
            # 传的是**这张表在页内的上文**：pdfplumber 把表头切在表外，只能靠上文认出重算列。
            if restated_indicator_table(
                table["rows"], normalized_page[:index] if index > 0 else page["text"]
            ):
                continue
            harvest.append((table["rows"], statement_type, unit_scale, table["page_no"]))
    else:
        for page in pages:
            statement_type, page_scope = detect_statement_type(
                page["text"][:1500], fuzzy=page["is_scanned"]
            )
            if statement_type is None:
                continue
            # 「母公司报表」的数不能盖到本公司（合并）口径上：四张目标宽表按
            # (stock_code, report_period) 只留一行，一旦母公司行先命中，同一行里
            # 就会出现「营业收入是合并、净利润是母公司」的混口径。
            if page_scope == "parent":
                continue
            # 单位声明优先整页找第一条「单位：万元/千元/百万元」：实测兰州银行/同仁堂/
            # 东航 的声明都在页面 1500 字之后，旧写法直接退化成 1 倍。
            unit_scale = caption_amount_scale(page["text"]) or detect_amount_unit_scale(
                page["text"][:1500]
            )
            harvest.append((page["rows"], statement_type, unit_scale, page["page_no"]))

    values: dict[str, StatementValue] = {}
    previous_values: dict[str, Decimal] = {}
    for rows, statement_type, unit_scale, page_no in harvest:
        for row_index, row in enumerate(rows, start=1):
            label, numeric = parse_label_and_values(row)
            metric_code = match_metric(label)
            if metric_code is None or not numeric:
                continue
            current_cell, previous_cell = extract_current_previous_values(numeric)
            current = parse_number(current_cell[1])
            previous = parse_number(previous_cell[1]) if previous_cell else None
            if current is None:
                continue
            scale = 1 if metric_code in {"basic_eps", "diluted_eps"} else unit_scale
            if metric_code not in values:
                values[metric_code] = StatementValue(
                    metric_code=metric_code,
                    metric_name=METRICS[metric_code][0],
                    statement_type=statement_type,
                    current=current * scale,
                    previous=previous * scale if previous is not None else None,
                    current_raw=current_cell[1],
                    previous_raw=previous_cell[1] if previous_cell else None,
                    page_no=page_no,
                    row_index=row_index,
                    confidence=Decimal("0.80"),
                )
            if previous is not None and metric_code not in previous_values:
                previous_values[metric_code] = previous * scale
    return values, previous_values

# 报表口径标题（正文补充路径用）。中国财报的固定顺序是「合并资产负债表 →
# 母公司资产负债表 → 合并利润表 → 母公司利润表 → 合并现金流量表 → 母公司现金流量表」，
# 所以必须跟着标题切换口径：四张目标宽表按 (stock_code, report_period) 只留一行，
# 母公司表的行一旦盖进去，同一行里就会出现「营业收入是合并、净利润是母公司」的混口径。
PARENT_STATEMENT_TITLE_RE = re.compile(
    r"^母公司(?:利润表|资产负债表|现金流量表|所有者权益变动表|股东权益变动表)"
)
CONSOLIDATED_SCOPE_RE = re.compile(
    r"^(?:合并|母公司及合并)(?:利润表|资产负债表|现金流量表|所有者权益变动表|股东权益变动表)"
    r"|附注|注释|备查文件"
)
# 「折行科目接续」的反例：利润表里真实存在、但库里没有对应列的行项目。
# 它们出现在数字行开头，就说明这行的数字属于**另一行**，不能接给上一行的科目名
# （002195 的 `六、综合收益总额 402,632,550.37` 就是这样被接成净利润的）。
PENDING_COMPETING_ITEM_RE = re.compile(
    r"收益|损益|费用|利润|亏损|税|权益|公积|收入|支出|"
    r"income|profit|loss|expense|revenue|equity|gain"
)
# 「（所有者权益|股东权益）变动表」的列**不是**「本期 / 上期」，而是
# 「股本 | 资本公积 | 盈余公积 | 未分配利润 | 少数股东权益 | 合计」——按两列取数会把
# **上一年**那一行的未分配利润当本期、少数股东权益当上期。实测 600007 中国国贸 2026 半年报
# 正文路径命中 `净利润 632371194 337624 632708818`（权益变动表里 2025 年 1-6 月那一行）⇒
# 存成 net_profit=632,371,194、previous=337,624 ⇒ 入库现算出 profit_yoy=187200.4271%（深测
# 报的 P2 越界就是这条），而摘要原文真值是 `633,458,596 632,371,194 0.17%`。
EQUITY_CHANGE_TITLE_RE = re.compile(r"(?:所有者权益|股东权益)变动表$")
MAIN_STATEMENT_TITLE_RE = re.compile(r"(?:母公司及合并|合并|母公司)?\s*(?:利润表|资产负债表|现金流量表)$")

def next_line_owns_the_numbers(lines: list[str], index: int) -> bool:
    """数字单独成行时，看下一行（跳过空行，最多看 3 行）行首是不是**另一个科目**。
    是 ⇒ 这行的数字属于下一行，不能接给上一段留下的科目名。
    实测 002195 岩山科技 2024 年报：`五、净利润` 那行没有数字，数字落在下一行、而再下一行是
    `六、综合收益总额 402,632,550.37` ⇒ 旧接续把综合收益当成净利润（库里 net 402,632,550.37
    vs 归母 32,461,729.28 差 12.4 倍就是这么来的）。
    """
    for follow in lines[index + 1 : index + 4]:
        compact = re.sub(r"\s+", "", follow)
        if not compact:
            continue
        code = match_metric(follow)
        if code is None:
            return False
        match = AMOUNT_IN_LINE_RE.search(follow)
        head = follow[: match.start()] if match else follow
        return metric_label_at_line_start(head, code)
    return False


def extract_values_from_text(
    full_text: str,
    values: dict[str, StatementValue],
    previous_values: dict[str, Decimal],
) -> None:
    """使用文本规则补充缺失指标。"""
    lines = full_text.splitlines()
    # OCR 常把科目名折成两行、金额落在后一行：`Net profit attributable to` +
    # `shareholders of the listed 578,235,101.90 711,640,958.19 -18.75%`（实测 002508 老板
    # 电器英文版、002493 荣盛石化英文版都这样）。上一行「命中科目但一个数字都没有」时，
    # 把科目带到下一行；下一行必须带千分位金额才算数，避免把 `1.8` 这类小噪声当金额。
    pending_metric: str | None = None
    # 当前是否落在「母公司报表」区间里（遇到合并表标题或附注就切回来）。
    # 实测 600208 存的净利润 382,273,538.06 是 p40 母公司表 `四、净利润` 的数，
    # 合并真值 32,234,254.86 在 p38；600325 存的数来自母公司表，合并是 -3,367,904,730.95。
    parent_scope = False
    # 是否落在「所有者权益/股东权益变动表」区间里（列语义与利润表完全不同，见上面注释）。
    equity_scope = False
    for row_index, raw_line in enumerate(lines, start=1):
        compact_title = HEAD_ENUMERATOR_RE.sub("", re.sub(r"\s+", "", raw_line))
        if PARENT_STATEMENT_TITLE_RE.match(compact_title):
            parent_scope = True
        elif CONSOLIDATED_SCOPE_RE.search(compact_title):
            parent_scope = False
        elif parent_scope:
            # 母公司报表区间内的行一律不填、也不记折行科目：宁可少一列，
            # 也不要把母公司的数写进本公司（合并）口径的那一行。
            continue
        if EQUITY_CHANGE_TITLE_RE.search(compact_title):
            equity_scope = True
        elif MAIN_STATEMENT_TITLE_RE.search(compact_title):
            equity_scope = False
        elif equity_scope:
            continue
        line = re.sub(
            # 连「2026年」这种只有年份的写法也要抹掉：否则正文补充规则会把
            # 年份本身当成第一个数字金额存进营业收入/净利润。
            r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?",
            " ",
            raw_line,
        )
        # 百分数整段抹掉：正文补充规则里「同比增长 7.97%」「占比 100.00%」后面的
        # 数字不是金额（实测 002195 岩山科技 2025 年报就靠这条把 100.00 存成营业收入）。
        line = PERCENT_NUMBER_RE.sub(" ", line)
        # 行首的条目号不是金额：`5. Net profit ("-" for net loss) 4,376,950,333.58` 的 5 是序号，
        # 而 `1. Whether there are differences in the net profit and net asset disclosed…`
        # 是问句（实测 002508 老板电器英文版靠这行把净利润存成 1）。剥掉「数字/罗马数字 + 点」的
        # 行首序号后，问句里就没有数字了 ⇒ 自动跳过，利润表那行才拿得到真金额。
        line = LEADING_ENUMERATOR_RE.sub(" ", line, count=1)
        metric_code = match_metric(line)
        matches = list(AMOUNT_IN_LINE_RE.finditer(line))
        if matches:
            if metric_code is None and pending_metric is not None:
                # 上一行是折过来的科目名，本行只带金额（`Net profit attributable to` / `… 578,235,101.90`）。
                # 但**接续只在「本行数字之前没有别的行项目」时**才成立：实测 002195 岩山科技的
                # `五、净利润` 行只有科目名没有数字，下一行是 `六、综合收益总额 402,632,550.37`，
                # 而「综合收益总额」在科目表里没有对应列 ⇒ match_metric 返回 None，看着就像
                # 「无科目的数字行」，于是被接成净利润（库里 2024 年报 net 402,632,550.37 vs
                # 归母 32,461,729.28 差 12.4 倍就是这么来的）。
                if PENDING_COMPETING_ITEM_RE.search(line[: matches[0].start()]):
                    pending_metric = None
                elif next_line_owns_the_numbers(lines, row_index - 1):
                    # 数字单独成行、真正的科目名在**下一行**行首（实测 600208 p40：
                    # `四、净利润` 后面数字自成一排，下一行才是 `五、综合收益总额`）⇒ 这串数
                    # 不属于上一行的科目，接了就是把别的行项目当净利润。
                    pending_metric = None
                else:
                    metric_code = pending_metric
                    pending_metric = None
            else:
                pending_metric = None
                if metric_code is not None and not metric_label_at_line_start(
                    line[: matches[0].start()], metric_code
                ):
                    # 散文句子里顺带提到的科目不算报表行（实测 TCL/荣盛英文版的净利润 9.00、25.00
                    # 就是这么来的），这里直接丢掉本行。
                    metric_code = None
        else:
            # 科目名单独占一行：只有它本身就像个科目（行首、不是散文）才记下来，
            # 否则 `…impact of 10% or more on the Company’s net profit` 这种句子尾巴会把下一行的
            # 子公司小计数当成本公司净利润。
            if metric_code is not None and metric_label_at_line_start(line, metric_code):
                pending_metric = metric_code
            elif metric_code is None:
                # 页间断行截掉的科目名前缀（实测 600085 的 `四、汇率变动对现金及现金等价物的`，
                # 「影响」二字和金额都跑到下一行）⇒ 认成待接续的科目。
                pending_metric = truncated_alias_metric_code(line)
            else:
                pending_metric = None
        if metric_code is None or metric_code in values:
            continue
        if not matches:
            continue
        # 第一个数字之前的话术决定这一行是不是「本公司合并口径」：
        # 「2026 年上半年，公司人工智能业务营业收入为 771,614.38 元」是分部口径，
        # 不能当合并营业收入（实测这类行是 revenue 垃圾值的主要来源之一）。
        if TEXT_SUPPLEMENT_SCOPE_BAN_RE.search(line[: matches[0].start()]):
            continue
        numeric = [match.group(0) for match in matches]
        raw_candidates = [(index, value) for index, value in enumerate(numeric)]
        current_cell, previous_cell = extract_current_previous_values(raw_candidates)
        current = parse_number(current_cell[1])
        previous = parse_number(previous_cell[1]) if previous_cell else None
        if current is None:
            continue
        scale = (
            1
            if metric_code in {"basic_eps", "diluted_eps"}
            else line_amount_scale(lines, row_index - 1)
        )
        values[metric_code] = StatementValue(
            metric_code=metric_code,
            metric_name=METRICS[metric_code][0],
            statement_type=METRICS[metric_code][1],
            current=current * scale,
            previous=previous * scale if previous is not None else None,
            current_raw=current_cell[1],
            previous_raw=previous_cell[1] if previous_cell else None,
            page_no=0,
            row_index=row_index,
            confidence=Decimal("0.65"),
        )
        if previous is not None:
            previous_values[metric_code] = previous * scale

# 量级自洽阈值：归母、净利润、营业利润、利润总额在真实报表里是同一个数量级
# （少数股东损益差 100 倍也正常，见下，所以**不纳入**这个家族）。
PROFIT_UNIT_RATIO = Decimal("100")
# 故意不含 minority_profit_loss：少数股东损益天然可以比归母小几个数量级。
# 实测（6_rag_chunks 原文 + 勾稽验证）：
#   600007 p38  少数股东损益 386,214 / 归母 633,458,596 / 净利润 633,844,810
#               → 633,458,596 + 386,214 = 633,844,810，勾稽正好成立，是真值不是单位错
#   600299 p58  净利润 905,697,394 − 归母 904,848,549 = 848,845（同样只差千分之一）
# 早先把它算进家族时，重导入会误丢这两家的少数股东损益（日志 7 条里有 3 条是它）。
PROFIT_FAMILY_CODES = ("net_profit", "operating_profit", "total_profit")


def drop_unit_mismatched_profits(values: dict[str, StatementValue]) -> list[str]:
    """英文报表的主表有时是**千元/万元**（实测 000100 合并利润表 `Operating profit 2,457,031 437,709`
    对应的就是 RMB'000，而同一家公司第 7 页摘要表的归母 3,808,272,967 是元），
    而 `line_amount_scale` 只认中文「单位：」声明 ⇒ 差 1000× 的数会直接进库。
    这里用「归母 vs 利润表各列必须同量级」把它兜住：差 >100× 就丢掉，
    宁可少一列，也不写一个差三个数量级的数（摘要表口径是元，一定保留）。
    实测六家：000100 净利润 2,243,266 / 营业利润 2,457,031 会被丢；
    000568 4.38e9 vs 4.34e9、000858 9.0e9 vs 8.75e9、002384 2.98e9 vs 2.96e9、
    002493 9.26e9 vs 5.11e9、002508 5.76e8 vs 5.78e8 全部保留（比例都在 2 以内）。"""
    anchor = values.get("parent_net_profit")
    if anchor is None or not anchor.current or abs(anchor.current) < Decimal("10000"):
        return []
    dropped: list[str] = []
    anchor_abs = abs(anchor.current)
    for code in PROFIT_FAMILY_CODES:
        item = values.get(code)
        if item is None or not item.current:
            continue
        item_abs = abs(item.current)
        # 必须双向判：只判「比归母小 100 倍」会漏掉**附注的万元声明盖到元表行**
        # 这类放大（实测 600008 首创环保净利润 1,168,715,925.13 被「单位：万元」乘成
        # 11,687,159,251,300，比归母 839,444,514.59 大 13922 倍，旧写法放过了它）。
        if max(anchor_abs / item_abs, item_abs / anchor_abs) > PROFIT_UNIT_RATIO:
            values.pop(code, None)
            dropped.append(code)
    return dropped


BALANCE_RECOVER_SCALES = (1_000, 10_000, 1_000_000)


def recover_balance_sheet_scale(values: dict[str, StatementValue]) -> int:
    """用恒等式「资产总计 = 负债合计 + 股东权益合计」把资产负债表列的单位倍数纠回来。

    实测 000100 TCL 英文版：摘要表（单位=元）资产总计 365,144,968,892 在行#165，
    而资产负债表（表头 `Unit: RMB'0,000`，即千元）的负债 237,525,394 + 权益 127,619,575
    = 365,144,969，正好是资产的 1/1000（行#3463/3477）。
    单位声明行离金额行有 2000 多行，`line_amount_scale` 的窗口只有 80 行 ⇒ 倍数丢失。
    故意不放大窗口：同一份文件里还有 `Unit: RMB’000,000,000`（十亿）这类声明，
    扩窗会把无关行乘错。所以反过来用恒等式解倍数——只有 (负债+权益)×k 与资产
    相差 ≤1% 才乘，k 只试 1e3/1e4/1e6；恒等式本来就 1:1 成立的一律不动
    （实测 002493 荣盛、002508 老板都是 1:1，不会被误乘）。"""
    assets = values.get("total_assets")
    liabilities = values.get("total_liabilities")
    equity = values.get("total_equity")
    if assets is None or liabilities is None or equity is None:
        return 0
    total_assets = assets.current
    subtotal = liabilities.current + equity.current
    if not total_assets or not subtotal or total_assets < Decimal("100000000"):
        return 0
    ratio = total_assets / subtotal
    for scale in BALANCE_RECOVER_SCALES:
        if abs(Decimal(scale) - ratio) / Decimal(scale) <= Decimal("0.01"):
            values["total_liabilities"] = replace(liabilities, current=liabilities.current * scale,
                                                 previous=liabilities.previous * scale if liabilities.previous is not None else None)
            values["total_equity"] = replace(equity, current=equity.current * scale,
                                            previous=equity.previous * scale if equity.previous is not None else None)
            return scale
    return 0


def derive_missing_values(values: dict[str, StatementValue]) -> None:
    """根据勾稽关系推导缺失指标。"""
    formulas = {
        "total_assets": ("current_assets", "non_current_assets"),
        "total_liabilities": ("current_liabilities", "non_current_liabilities"),
        "total_equity": ("total_parent_equity", "minority_interest"),
        "net_profit": ("parent_net_profit", "minority_profit_loss"),
    }
    for target, sources in formulas.items():
        if target in values or any(source not in values for source in sources):
            continue
        left, right = (values[source] for source in sources)
        values[target] = StatementValue(
            metric_code=target,
            metric_name=METRICS[target][0],
            statement_type=left.statement_type,
            current=left.current + right.current,
            previous=(
                left.previous + right.previous
                if left.previous is not None and right.previous is not None
                else None
            ),
            current_raw=str(left.current + right.current),
            previous_raw=None,
            page_no=left.page_no,
            row_index=left.row_index,
            confidence=min(left.confidence, right.confidence),
            derived=True,
        )
    # 全资子公司报表会整行省略「少数股东权益」⇒ 归母合计就是权益合计。
    # 这条兜底是配上面别名修正的必要保险：别名改对以后，「归属于母公司所有者权益（或股东权益）合计」
    # 不再冒充 total_equity，若这一页又没有「所有者权益（或股东权益）合计」行，
    # 权益列会整列变 None（数据缺失比串号更糟）⇒ 只在**确实没有** minority 行时才兜底，
    # 且置信度压到 0.4 让校验环节能看出它是推出来的。
    if "total_equity" not in values and "total_parent_equity" in values and "minority_interest" not in values:
        parent_equity = values["total_parent_equity"]
        values["total_equity"] = replace(
            parent_equity,
            metric_code="total_equity",
            metric_name=METRICS["total_equity"][0],
            confidence=Decimal("0.4"),
            derived=True,
        )
    reverse_formulas = {
        "non_current_assets": ("total_assets", "current_assets"),
        "non_current_liabilities": ("total_liabilities", "current_liabilities"),
    }
    for target, (total_code, part_code) in reverse_formulas.items():
        if target in values or total_code not in values or part_code not in values:
            continue
        total = values[total_code]
        part = values[part_code]
        values[target] = StatementValue(
            metric_code=target,
            metric_name=METRICS[target][0],
            statement_type=total.statement_type,
            current=total.current - part.current,
            previous=(
                total.previous - part.previous
                if total.previous is not None and part.previous is not None
                else None
            ),
            current_raw=str(total.current - part.current),
            previous_raw=None,
            page_no=total.page_no,
            row_index=total.row_index,
            confidence=min(total.confidence, part.confidence),
            derived=True,
        )
    if (
        "net_increase_cash" not in values
        and all(
            code in values
            for code in (
                "net_cash_flow_operating",
                "net_cash_flow_investing",
                "net_cash_flow_financing",
                "exchange_rate_effect_cash",
            )
        )
    ):
        parts = [
            values[code]
            for code in (
                "net_cash_flow_operating",
                "net_cash_flow_investing",
                "net_cash_flow_financing",
                "exchange_rate_effect_cash",
            )
        ]
        values["net_increase_cash"] = StatementValue(
            metric_code="net_increase_cash",
            metric_name=METRICS["net_increase_cash"][0],
            statement_type="cash_flow_statement",
            current=sum(part.current for part in parts),
            previous=None,
            current_raw=str(sum(part.current for part in parts)),
            previous_raw=None,
            page_no=parts[0].page_no,
            row_index=parts[0].row_index,
            confidence=min(part.confidence for part in parts),
            derived=True,
        )

def build_validation_rows(values: dict[str, StatementValue]) -> list[dict[str, str]]:
    """执行财务报表勾稽校验。"""
    def value(code: str) -> Decimal | None:
        """读取指定指标的本期数值。"""
        return values[code].current if code in values else None

    rules = [
        ("BS_BALANCE", "资产等于负债和所有者权益", ("total_assets", "total_liabilities_and_equity"), lambda: value("total_assets") == value("total_liabilities_and_equity")),
        ("BS_ASSET", "资产结构勾稽", ("current_assets", "non_current_assets", "total_assets"), lambda: value("current_assets") + value("non_current_assets") == value("total_assets")),
        ("BS_LIABILITY", "负债结构勾稽", ("current_liabilities", "non_current_liabilities", "total_liabilities"), lambda: value("current_liabilities") + value("non_current_liabilities") == value("total_liabilities")),
        ("BS_EQUITY", "权益结构勾稽", ("total_parent_equity", "minority_interest", "total_equity"), lambda: value("total_parent_equity") + value("minority_interest") == value("total_equity")),
        ("IS_PROFIT", "利润总额勾稽", ("operating_profit", "non_operating_income", "non_operating_expense", "total_profit"), lambda: value("operating_profit") + value("non_operating_income") - abs(value("non_operating_expense")) == value("total_profit")),
        # 所得税费用可以为负（税项返还 / 递延收益），取绝对值会把
        # 「利润总额 - 所得税费用 = 净利润」算成假的不勾稽。
        ("IS_NET", "净利润勾稽", ("total_profit", "income_tax_expense", "net_profit"), lambda: value("total_profit") - value("income_tax_expense") == value("net_profit")),
        ("CF_BRIDGE", "现金流量净额勾稽", ("net_cash_flow_operating", "net_cash_flow_investing", "net_cash_flow_financing", "exchange_rate_effect_cash", "net_increase_cash"), lambda: value("net_cash_flow_operating") + value("net_cash_flow_investing") + value("net_cash_flow_financing") + value("exchange_rate_effect_cash") == value("net_increase_cash")),
        ("CF_BALANCE", "现金余额勾稽", ("ending_cash", "beginning_cash", "net_increase_cash"), lambda: value("ending_cash") - value("beginning_cash") == value("net_increase_cash")),
    ]
    results = []
    for code, name, required, check in rules:
        missing = [item for item in required if item not in values]
        if missing:
            results.append({"rule": code, "name": name, "status": "待补充", "message": "缺少：" + "、".join(missing)})
        else:
            passed = check()
            # 推导出来的合计值本身就是这条勾稽等式算出来的，拿它再验一次等式恒成立，
            # 这不是「校验通过」，是自我证明。状态保留，但把来路写进说明。
            derived_items = [item for item in required if getattr(values[item], "derived", False)]
            note = f"（含推导值：{'、'.join(METRICS[item][0] for item in derived_items)}）" if derived_items else ""
            results.append(
                {
                    "rule": code,
                    "name": name,
                    "status": "通过" if passed else "异常",
                    "message": ("" if passed else "数值不一致") + note,
                }
            )
    return results

# 下面这段 fr_* 表定义是早期方案留下的，全项目没有任何查询用到它。
# 原来它和真正的 SCHEMA_SQL 同名，导入时被后面那份直接覆盖——所以改这里不会生效，
# 也让「业务表从来没被建出来」这个问题一直没人发现。现在把它显式改名留档。
LEGACY_FR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fr_companies (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  stock_code VARCHAR(16) NOT NULL UNIQUE,
  company_name VARCHAR(255) NULL,
  industry_name VARCHAR(255) NULL,
  list_date DATE NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS fr_source_documents (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  company_id BIGINT UNSIGNED NULL,
  file_name VARCHAR(512) NOT NULL,
  file_hash CHAR(64) NOT NULL UNIQUE,
  file_path VARCHAR(1024) NULL,
  page_count INT NULL,
  report_type VARCHAR(32) NULL,
  report_date DATE NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS fr_financial_facts (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  document_id BIGINT UNSIGNED NOT NULL,
  company_id BIGINT UNSIGNED NOT NULL,
  statement_type VARCHAR(32) NOT NULL,
  metric_code VARCHAR(64) NOT NULL,
  metric_name VARCHAR(255) NOT NULL,
  value_decimal DECIMAL(30,6) NULL,
  value_raw VARCHAR(128) NULL,
  page_no INT NULL,
  row_index INT NULL,
  confidence DECIMAL(6,5) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_fact (document_id, metric_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS fr_validation_results (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  document_id BIGINT UNSIGNED NOT NULL,
  rule_code VARCHAR(64) NOT NULL,
  rule_name VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL,
  message VARCHAR(512) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS fr_extracted_tables (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  document_id BIGINT UNSIGNED NOT NULL,
  page_no INT NOT NULL,
  table_index INT NOT NULL,
  headers_json JSON NULL,
  rows_json JSON NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_table (document_id, page_no, table_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS `1_stock_basic` (
  stock_code VARCHAR(20) NOT NULL COMMENT '股票代码',
  stock_name VARCHAR(100) NOT NULL COMMENT '公司全称',
  alias VARCHAR(64) NOT NULL DEFAULT '' COMMENT '证券简称（公告登记名，如 豪迈科技）',
  industry VARCHAR(50) NULL COMMENT '所属行业',
  list_date VARCHAR(20) NULL COMMENT '上市日期',
  PRIMARY KEY (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='公司基本信息';
CREATE TABLE IF NOT EXISTS `2_performance_report` (
  id INT AUTO_INCREMENT PRIMARY KEY,
  stock_code VARCHAR(20) NOT NULL COMMENT '股票代码',
  report_period VARCHAR(20) NOT NULL COMMENT '报告期，如2023年报',
  revenue DECIMAL(20,4) NULL COMMENT '营业收入，单位：元',
  net_profit DECIMAL(20,4) NULL COMMENT '净利润，单位：元',
  net_profit_parent DECIMAL(20,4) NULL COMMENT '归母净利润',
  eps DECIMAL(16,4) NULL COMMENT '基本每股收益',
  revenue_yoy DECIMAL(16,4) NULL COMMENT '营收同比增速，单位：%',
  profit_yoy DECIMAL(16,4) NULL COMMENT '净利润同比增速（合并口径，按五、净利润现算），单位：%',
  KEY idx_performance_stock (stock_code, report_period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='业绩报表';
CREATE TABLE IF NOT EXISTS `3_income_statement` (
  id INT AUTO_INCREMENT PRIMARY KEY,
  stock_code VARCHAR(20) NOT NULL COMMENT '股票代码',
  report_period VARCHAR(20) NOT NULL COMMENT '报告期',
  operating_revenue DECIMAL(20,4) NULL COMMENT '营业总收入',
  operating_cost DECIMAL(20,4) NULL COMMENT '营业成本',
  operating_profit DECIMAL(20,4) NULL COMMENT '营业利润',
  total_profit DECIMAL(20,4) NULL COMMENT '利润总额',
  net_profit DECIMAL(20,4) NULL COMMENT '净利润',
  net_profit_parent DECIMAL(20,4) NULL COMMENT '归母净利润',
  basic_eps DECIMAL(16,4) NULL COMMENT '基本每股收益',
  KEY idx_income_stock (stock_code, report_period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='利润表';
CREATE TABLE IF NOT EXISTS `4_balance_sheet` (
  id INT AUTO_INCREMENT PRIMARY KEY,
  stock_code VARCHAR(20) NOT NULL COMMENT '股票代码',
  report_period VARCHAR(20) NOT NULL COMMENT '报告期',
  total_assets DECIMAL(20,4) NULL COMMENT '总资产',
  total_liability DECIMAL(20,4) NULL COMMENT '总负债',
  total_equity DECIMAL(20,4) NULL COMMENT '股东权益合计',
  current_assets DECIMAL(20,4) NULL COMMENT '流动资产合计',
  current_liability DECIMAL(20,4) NULL COMMENT '流动负债合计',
  KEY idx_balance_stock (stock_code, report_period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='资产负债表';
CREATE TABLE IF NOT EXISTS `5_cash_flow_statement` (
  id INT AUTO_INCREMENT PRIMARY KEY,
  stock_code VARCHAR(20) NOT NULL COMMENT '股票代码',
  report_period VARCHAR(20) NOT NULL COMMENT '报告期',
  operate_cash DECIMAL(20,4) NULL COMMENT '经营活动现金流净额',
  invest_cash DECIMAL(20,4) NULL COMMENT '投资活动现金流净额',
  finance_cash DECIMAL(20,4) NULL COMMENT '筹资活动现金流净额',
  fx_cash DECIMAL(20,4) NULL COMMENT '汇率变动对现金及现金等价物的影响',
  cash_increase DECIMAL(20,4) NULL COMMENT '现金及等价物净增加额',
  KEY idx_cash_flow_stock (stock_code, report_period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='现金流量表';
CREATE TABLE IF NOT EXISTS `6_rag_chunks` (
  `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `file_name` VARCHAR(255) NOT NULL,
  `file_path` VARCHAR(1024) NOT NULL,
  `stock_code` VARCHAR(20) NOT NULL,
  `company_name` VARCHAR(255) DEFAULT NULL,
  `page_no` INT NOT NULL DEFAULT 0,
  `chunk_index` INT NOT NULL DEFAULT 0,
  `content` LONGTEXT NOT NULL,
  `embedding_json` LONGTEXT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_rag_file` (`file_path`(255)),
  KEY `idx_rag_stock` (`stock_code`),
  FULLTEXT KEY `ft_rag_content` (`content`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS `7_document_index` (
  `id` VARCHAR(32) PRIMARY KEY,
  `stock_code` VARCHAR(20) DEFAULT NULL,
  `company_name` VARCHAR(255) DEFAULT NULL,
  `report_period` VARCHAR(20) DEFAULT NULL,
  `file_name` VARCHAR(255) NOT NULL,
  `file_path` VARCHAR(1024) NOT NULL,
  `file_type` VARCHAR(16) DEFAULT NULL,
  `file_size` BIGINT DEFAULT 0,
  `file_hash` CHAR(64) DEFAULT NULL,
  `managed_copy` TINYINT(1) DEFAULT 0,
  `imported_at` VARCHAR(32) DEFAULT NULL,
  KEY `idx_document_stock` (`stock_code`),
  KEY `idx_document_file` (`file_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS `8_chat_history` (
  `id` VARCHAR(32) PRIMARY KEY,
  `title` VARCHAR(255) DEFAULT NULL,
  `updated_at` VARCHAR(32) DEFAULT NULL,
  `question` TEXT,
  `result_json` LONGTEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS `9_validation_results` (
  `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `stock_code` VARCHAR(20) NOT NULL,
  `report_period` VARCHAR(20) NOT NULL,
  `rule_name` VARCHAR(255) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `message` VARCHAR(512) DEFAULT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_validation_stock` (`stock_code`, `report_period`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def bounded_percent(value: Any) -> float | None:
    """把非有限、不可解析、大得没有业务意义的百分比收成 None。

    调用方拿到的「上期金额」来自 PDF 表格，垃圾单元格（把页码、比例尺当成
    金额）会算出 1e14 这种同比；DECIMAL(16,4) 也装不下，抛 1264 就会让
    整份文件白解析一场。超过 ±1e8 % 的一律当噪声丢掉，宁可缺数不可脏数。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > 1e8:
        return None
    return round(number, 4)

class MySQL:
    """封装 MySQL 建库、建表、数据保存和查询操作。"""
    def __init__(self) -> None:
        """初始化 MySQL 连接配置。"""
        self.config = dict(
            host=CONFIG["mysql_host"],
            port=CONFIG["mysql_port"],
            user=CONFIG["mysql_user"],
            password=CONFIG["mysql_password"],
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )
        context = _shared_mysql_ssl_context()
        if context is not None:
            self.config["ssl"] = context

    def init_database(self) -> None:
        """初始化数据库和业务表结构。"""
        connection = pymysql.connect(**self.config)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{CONFIG['mysql_database']}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                cursor.execute(f"USE `{CONFIG['mysql_database']}`")
                for statement in SCHEMA_SQL.split(";"):
                    if statement.strip():
                        cursor.execute(statement)
                # 排序规则统一。原来这里写死了四张表（6/7/8/9），
                # `1_stock_basic` 与四张事实表从来没被转 ⇒ **新建库必炸**：
                # SCHEMA_SQL 只写 `DEFAULT CHARSET=utf8mb4`，新表跟着 MySQL 8 的
                # 字符集默认值走 `utf8mb4_0900_ai_ci`，而 6/7/8/9 转成了
                # `utf8mb4_unicode_ci` ⇒ 同一库里两种排序规则混着，
                # 任何跨表字符串比较直接报
                #   (1267, "Illegal mix of collations (utf8mb4_0900_ai_ci,IMPLICIT)
                #    and (utf8mb4_unicode_ci,IMPLICIT) for operation '='")
                # 实测：一次性库里 `9_validation_results` JOIN `2_performance_report`
                # 即触发（常驻件 test_validation_rows_lifecycle.py 第④项抓到）；
                # 线上 finance_report 九张表实测全是 unicode_ci ⇒ 现库不用动，
                # 改成「按 information_schema 扫，不对的才转」以后新装也一致。
                cursor.execute(
                    "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' "
                    "AND TABLE_COLLATION <> 'utf8mb4_unicode_ci'",
                    (CONFIG["mysql_database"],),
                )
                for row in cursor.fetchall() or []:
                    cursor.execute(
                        f"ALTER TABLE `{row['t']}` "
                        "CONVERT TO CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_unicode_ci"
                    )
                try:
                    cursor.execute(
                        "ALTER TABLE 6_rag_chunks "
                        "ADD COLUMN embedding_json LONGTEXT NULL"
                    )
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] != 1060:
                        raise
                # 证券简称列：用户只写「豪迈科技」这种公告登记名时，以前必须写全称或六位代码。
                # 建表语句改了，但已存在的库不会自动加列（CREATE TABLE IF NOT EXISTS 直接跳过），
                # 所以这里补一次 ALTER；1060 = 重复列名，说明已经有了。
                try:
                    cursor.execute(
                        "ALTER TABLE `1_stock_basic` "
                        "ADD COLUMN alias VARCHAR(64) NOT NULL DEFAULT '' "
                        "COMMENT '证券简称（公告登记名，如 豪迈科技）' AFTER stock_name"
                    )
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] != 1060:
                        raise
                # profit_yoy 的口径标注：这一列是按「五、净利润」（含少数股东）现算的合并口径，
                # 不是公告「主要会计数据」里披露的归母同比。抽样 8 家 8/8 都对不上，最严重的
                # 002195 岩山科技 2025年年报：本列 -98.4641%，公告 +184.19%（方向相反）。
                # 建表语句改了注释，老库不会自动跟（CREATE TABLE IF NOT EXISTS 跳过），补一次
                # 纯注释的 MODIFY（列类型与库里现有一致 ⇒ 元数据变更，不动数据）。
                try:
                    cursor.execute(
                        "ALTER TABLE `2_performance_report` "
                        "MODIFY COLUMN profit_yoy DECIMAL(16,4) NULL "
                        "COMMENT '净利润同比增速（合并口径，按五、净利润现算），单位：%'"
                    )
                except pymysql.err.OperationalError as exc:
                    # 1054 = 列不存在（老库结构异常，交给上层校验发现），其余错误一律抛出
                    if exc.args[0] != 1054:
                        raise
                # 现金流勾稽的第四段：`经营 + 投资 + 筹资 + 汇率变动 = 现金净增加额`。
                # 校验规则 CF_BRIDGE（本文件上面）早就把「汇率变动对现金及现金等价物的影响」
                # 当成分项，只有存库层把它丢了 ⇒ 库里三列和永远对不上净增加额
                # （实测 600085 同仁堂 2026 半年报差 -79,697,897.58 = 恰为该行）。
                try:
                    cursor.execute(
                        "ALTER TABLE `5_cash_flow_statement` "
                        "ADD COLUMN fx_cash DECIMAL(20,4) NULL COMMENT '汇率变动对现金及现金等价物的影响'"
                    )
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] != 1060:
                        raise
                # 同比列以前是 DECIMAL(8,4)，上限 ±9999.9999%。基数极小的报表
                # 真能算出几万 %（实测一份 170 页的报告 profit_yoy=33933.97），
                # 一个字段溢出会让这份文件的四张表、校验结果、分块、文件索引
                # 全部写不进去。每股收益同理（垃圾单元格能算出几千块 EPS）。
                # 这里做幂等迁移，从旧 dump 还原回来的库也会被拉宽。
                widening = {
                    "2_performance_report": ("revenue_yoy", "profit_yoy", "eps"),
                    "3_income_statement": ("basic_eps",),
                }
                for wide_table, wide_columns in widening.items():
                    for column_name in wide_columns:
                        cursor.execute(
                            """
                            SELECT NUMERIC_PRECISION
                            FROM information_schema.COLUMNS
                            WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
                            """,
                            (CONFIG["mysql_database"], wide_table, column_name),
                        )
                        precision_row = cursor.fetchone()
                        if precision_row and int(precision_row.get("NUMERIC_PRECISION") or 0) < 16:
                            cursor.execute(
                                f"ALTER TABLE `{wide_table}` "
                                f"MODIFY COLUMN `{column_name}` DECIMAL(16,4) NULL"
                            )
                # 权威主数据播种（只补不覆盖）：让代码↔名称有外部参照，重导入时
                # 招商/五粮液等真实公司能直接归到真码，而不是永远停在 FILE_ 碎片。
                seed_company_master(cursor)
            connection.commit()
        finally:
            connection.close()
        self.config["database"] = CONFIG["mysql_database"]

    STATEMENT_COLUMNS = {
        "2_performance_report": (
            "revenue", "net_profit", "net_profit_parent", "eps", "revenue_yoy", "profit_yoy",
        ),
        "3_income_statement": (
            "operating_revenue", "operating_cost", "operating_profit", "total_profit",
            "net_profit", "net_profit_parent", "basic_eps",
        ),
        "4_balance_sheet": (
            "total_assets", "total_liability", "total_equity", "current_assets", "current_liability",
        ),
        "5_cash_flow_statement": (
            "operate_cash", "invest_cash", "finance_cash", "fx_cash", "cash_increase",
        ),
    }

    def _write_statement_row(
        self,
        cursor: Any,
        table: str,
        stock_code: str,
        report_period: str,
        row_values: tuple[Any, ...],
        source_name: str,
    ) -> str:
        """按 (公司, 报告期) 重写一张宽表的一行；**已有信息更全的行就不动它**。

        同一个报告期常有「摘要 + 全文」两份 PDF（实测全库 37 组同键文档），
        先删后插的写法等于「谁后处理谁赢」：摘要只有六个指标，全文有二十多个，
        结果取决于导入顺序而不是数据质量。改成跟信息量挂钩、跟顺序无关。
        返回 "empty"（这份文档该项没数）/"written"（重写）/"kept"（原行更全，保留）。
        """
        if not any(value is not None for value in row_values):
            return "empty"
        columns = self.STATEMENT_COLUMNS[table]
        new_filled = sum(1 for value in row_values if value is not None)
        filled_sql = " + ".join(f"COUNT(`{column}`)" for column in columns)
        cursor.execute(
            f"SELECT {filled_sql} FROM `{table}` "  # noqa: S608 - 列名来自上面的常量表
            "WHERE stock_code = %s AND report_period = %s",
            (stock_code, report_period),
        )
        row = cursor.fetchone()
        existing_filled = 0
        if row is not None:
            raw = row[0] if not isinstance(row, dict) else next(iter(row.values()))
            existing_filled = int(raw or 0)
        if existing_filled > new_filled:
            print(
                f"↔️ {table} 已有一行信息更全（{existing_filled} > {new_filled} 项），"
                f"保留原行不重写：{source_name}"
            )
            return "kept"
        placeholders = ", ".join(["%s"] * (len(columns) + 2))
        column_sql = ", ".join(f"`{column}`" for column in columns)
        cursor.execute(
            f"DELETE FROM `{table}` WHERE stock_code = %s AND report_period = %s",
            (stock_code, report_period),
        )
        cursor.execute(
            f"INSERT INTO `{table}` (stock_code, report_period, {column_sql}) "
            f"VALUES ({placeholders})",
            (stock_code, report_period, *row_values),
        )
        return "written"

    def _ingest_reject_reason(
        self, stock_code: str, company_name: str, report_period: str
    ) -> str:
        """写库前的入库闸门：不合格返回原因字符串，合格返回空串。

        闸门拦的是「会污染身份主键」的两类问题——解析错了宁可拒绝写入、
        打一行「待人工确认」，也不要静默地把数据盖到 (代码, 报告期) 上：
        一次盖错，之后按这个键先删后插就永远删不到，脏行只会越攒越多。

        注意：「非定期报告不写事实」不放这里。这里返回原因＝整份文档隔离
        （不登记、不切片），而临时公告/中介件是有检索价值的，应该在
        process_financial_document 里把 values 清空、文档照常登记。
        """
        year_match = re.search(r"(20\d{2})", report_period or "")
        if year_match and int(year_match.group(1)) > date.today().year:
            return f"报告期 {report_period} 在未来（正文里的远期日期被误当报告期）"
        code = str(stock_code or "")
        if (
            len(code) == 6
            and not code.startswith("FILE_")
            and code.isdigit()
            and company_name
        ):
            known = load_company_name(code)
            if known and known != code and not company_names_match(known, company_name):
                # 简称/别名是同一主体的第二个名字（实测：正文只写「长安Ｂ」，
                # 主数据是「重庆长安汽车股份有限公司」）。只要这个名字能唯一
                # 反查到同一个代码，就不是串号——闸门要与升级分支同一口径。
                if resolve_stock_code_by_name(company_name) == code:
                    return ""
                return f"代码 {code}（已归属 {known}）与公司名 {company_name} 串号"
        return ""

    def save(
        self,
        path: Path,
        page_count: int,
        company: dict[str, Any],
        metadata: dict[str, Any],
        values: dict[str, StatementValue],
        validations: list[dict[str, str]],
        tables: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """将解析后的公司、文档、指标和校验结果写入数据库。"""
        def amount(code: str) -> Any:
            """读取指定财务指标的当前值。"""
            item = values.get(code)
            return item.current if item else None

        period_label = {
            "annual": "年报",
            "semiannual": "半年报",
            "first_quarter": "一季报",
            "third_quarter": "三季报",
        }.get(metadata["period_type"], "报告期")
        report_period = f"{str(metadata['period_end'])[:4]}年{period_label}"
        stock_code = company["stock_code"]

        revenue = amount("operating_revenue")
        net_profit = amount("net_profit")
        net_profit_parent = amount("parent_net_profit")
        revenue_previous = values.get("operating_revenue").previous if values.get("operating_revenue") else None
        net_previous = values.get("net_profit").previous if values.get("net_profit") else None
        revenue_yoy = bounded_percent(
            (revenue - revenue_previous) / abs(revenue_previous) * 100
            if revenue is not None and revenue_previous
            else None
        )
        profit_yoy = bounded_percent(
            (net_profit - net_previous) / abs(net_previous) * 100
            if net_profit is not None and net_previous
            else None
        )

        reject_reason = self._ingest_reject_reason(
            stock_code, company["company_name"], report_period
        )
        if reject_reason:
            print(f"⛔ 拒绝入库（待人工确认）{path.name}：{reject_reason}")
            # -1 是「被闸门拦下」的信号：调用方必须停止登记文档与向量切片，
            # 否则会出现「MySQL 里没有、RAG 里却能搜到」的第三种真相（0,0 是正常返回值）。
            return -1, -1

        connection = pymysql.connect(**self.config)
        try:
            with connection.cursor() as cursor:
                # 公司名一律先清洗再落库：OCR 会把模板里的英文标签「Company name in
                # Chinese」粘进公司名，原来这条语句直接用解析原名，还把已有好名覆盖掉
                # （实测 1_stock_basic 里存着「CompanynameinChinese荣盛石化股份有限公司」）。
                # 主数据里的名字有最高优先级；其余情况只允许更长的名字覆盖，防止截断的
                # OCR 变体把全称降级成半截名。
                master_name = COMPANY_MASTER.get(stock_code, "")
                stock_name = authoritative_stock_name(stock_code, str(company.get("company_name") or ""))
                cursor.execute(
                    """
                    INSERT INTO 1_stock_basic (stock_code, stock_name, industry, list_date)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE stock_name = IF(
                        %s = 1 OR CHAR_LENGTH(VALUES(stock_name)) >= CHAR_LENGTH(stock_name),
                        VALUES(stock_name),
                        stock_name
                    )
                    """,
                    (
                        stock_code,
                        stock_name,
                        company.get("industry_name"),
                        company.get("list_date"),
                        1 if master_name else 0,
                    ),
                )

                performance_data = (
                    revenue,
                    net_profit,
                    net_profit_parent,
                    amount("basic_eps"),
                    revenue_yoy,
                    profit_yoy,
                )
                income_data = (
                    revenue,
                    amount("operating_cost"),
                    amount("operating_profit"),
                    amount("total_profit"),
                    net_profit,
                    net_profit_parent,
                    amount("basic_eps"),
                )
                balance_data = (
                    amount("total_assets"),
                    amount("total_liabilities"),
                    amount("total_equity"),
                    amount("current_assets"),
                    amount("current_liabilities"),
                )
                cash_data = (
                    amount("net_cash_flow_operating"),
                    amount("net_cash_flow_investing"),
                    amount("net_cash_flow_financing"),
                    amount("exchange_rate_effect_cash"),
                    amount("net_increase_cash"),
                )
                kept_tables: list[str] = []
                for table, row_values in (
                    ("2_performance_report", performance_data),
                    ("3_income_statement", income_data),
                    ("4_balance_sheet", balance_data),
                    ("5_cash_flow_statement", cash_data),
                ):
                    if (
                        self._write_statement_row(
                            cursor, table, stock_code, report_period, row_values, path.name
                        )
                        == "kept"
                    ):
                        kept_tables.append(table)
                if kept_tables and validations:
                    # 校验结论是跟着这一批数字算出来的：行没重写，就不许把描述上一份（更全）
                    # 文档的结论，换成描述这一份（更稀疏）文档的结论。
                    print(f"↔️ 已保留原行，顺带不动这些表的校验结论：{'、'.join(kept_tables)}：{path.name}")
                    validations = []
                if validations:
                    cursor.execute(
                        "DELETE FROM 9_validation_results "
                        "WHERE stock_code=%s AND report_period=%s",
                        (stock_code, report_period),
                    )
                    cursor.executemany(
                        """
                        INSERT INTO 9_validation_results (
                          stock_code, report_period, rule_name, status, message
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                stock_code,
                                report_period,
                                item.get("name", item.get("rule", "")),
                                item.get("status", ""),
                                item.get("message", ""),
                            )
                            for item in validations
                        ],
                    )
                elif not kept_tables:
                    # 这批数字压根没算出结论（例如非定期公告没有可比字段）⇒ 旧结论一起作废。
                    # 但上面「保留原行」的情况绝不能删：行没动，结论是描述那一批还在库里的数字的。
                    # 实测教训：原先无条件 DELETE + `validations = []`，导致「摘要盖完整版」那次
                    # 整组结论被删光、再不回填 ⇒ 18 个事实行没有任何校验记录（覆盖只有 41/138 次写入）。
                    cursor.execute(
                        "DELETE FROM 9_validation_results "
                        "WHERE stock_code=%s AND report_period=%s",
                        (stock_code, report_period),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return 0, 0

def build_statement_dataframe(statement_type: str, values: dict[str, StatementValue]) -> pd.DataFrame:
    """将指标整理为指定报表类型的 DataFrame。"""
    rows = []
    for code, (name, metric_statement) in METRICS.items():
        if metric_statement != statement_type or code not in values:
            continue
        value = values[code]
        rows.append(
            {
                "指标": name,
                "本期金额": float(value.current),
                "上期金额": float(value.previous) if value.previous is not None else None,
                "来源页": value.page_no,
            }
        )
    return pd.DataFrame(rows)

def process_financial_document(
    path: Path,
    database: MySQL,
    pages_override: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """完成单个财报文件的解析、校验和入库。"""
    print("=" * 60)
    print(f"开始处理：{path.name}")
    suffix = path.suffix.lower()
    pages: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    read_error = ""
    try:
        if pages_override is not None:
            pages, tables = pages_override
        elif suffix == ".pdf":
            pages, tables = read_pdf(path)
        elif suffix == ".docx":
            pages, tables = read_docx(path)
        else:
            read_error = f"不支持的文件类型：{suffix or '未知'}"
    except Exception as exc:
        read_error = str(exc)
        print(f"⚠️ 文件解析未完全成功，继续以部分数据入库：{exc}")

    full_text = "\n".join(page["text"] for page in pages)
    filename_stock_code = extract_stock_code_from_name(path)
    text_stock_code = extract_stock_code_from_text(full_text)
    # 文件名里的代码是导入者给的权威线索；正文里的“证券代码”可能是子公司/参股公司。
    if filename_stock_code and not filename_stock_code.startswith("FILE_"):
        stock_code = filename_stock_code
        if text_stock_code and text_stock_code != filename_stock_code:
            print(
                f"⚠️ 正文代码 {text_stock_code} 与文件名代码 {filename_stock_code} 不一致，"
                f"按文件名入库：{path.name}"
            )
    else:
        stock_code = text_stock_code or filename_stock_code
    filename_date = extract_report_date_from_name(path)
    try:
        period_type, period_end, period_start, year = extract_report_period(
            full_text, filename_date
        )
    except Exception:
        period_type, period_end, period_start, year = (
            "semiannual",
            date(filename_date.year, 6, 30),
            date(filename_date.year, 1, 1),
            filename_date.year,
        )
    company_name = (
        extract_company_name(full_text, stock_code) if full_text else ""
    ) or (
        ""
        if is_opaque_file_stem(path.stem) or is_generated_financial_stem(path.stem)
        else company_name_from_file_stem(path.stem)
    ) or path.stem
    # 调和的另一半：文件名和正文都没给出真实代码时，只要公司名能在主数据/库里
    # 唯一反查到真实代码，就不该再留 FILE_ 鬼影主体——否则同一公司会因为一次
    # OCR 换身份而分裂成两套事实（实测：TCL 英文半年报卡在 FILE_0DD8E634）。
    # 反查要求「归一化全等 + 唯一命中」，猜不到就保持 FILE_，绝不硬并。
    if str(stock_code).startswith("FILE_") and company_name:
        upgraded = resolve_stock_code_by_name(company_name)
        if upgraded:
            print(
                f"⚠️ 文件名/正文都没给出真实代码，按公司名 {company_name} "
                f"归入已有主体 {upgraded}：{path.name}"
            )
            stock_code = upgraded
    # 正文抽到的“股票代码”可能是券商/集团报表持仓表里的别家代码：随机哈希文件名
    # 走不了「文件名优先」那条权威分支，于是 text 代码直接胜出。实测招商证券的
    # 半年报正文（含证券投资持仓表）被抽成持仓股 920833，把招商的数字写进了美心
    # 翼申名下。这里做一次调和：若该真实代码在库里已归属另一家公司，判定串号，
    # 优先改用公司名能唯一反查到的代码，反查不到就退回文件名生成的 FILE_ 码。
    filename_has_real_code = bool(filename_stock_code) and not str(
        filename_stock_code
    ).startswith("FILE_")
    if (
        stock_code
        and not stock_code.startswith("FILE_")
        and not filename_has_real_code
        and company_name
    ):
        known = load_company_name(stock_code)
        if known and known != stock_code and not company_names_match(known, company_name):
            fallback = resolve_stock_code_by_name(company_name)
            if fallback == stock_code:
                # 名字是简称/别名，反查回来是同一个主体：不算串号，也不用改代码。
                print(
                    f"ℹ️ 公司名 {company_name} 是 {stock_code}（{known}）的简称/别名，同一主体：{path.name}"
                )
            elif fallback:
                print(
                    f"⚠️ 正文代码 {stock_code}（库里归属 {known}）与公司名 {company_name} "
                    f"不符，改用反查代码 {fallback}：{path.name}"
                )
                stock_code = fallback
            else:
                file_code = "FILE_" + hashlib.sha1(
                    str(path.resolve()).encode("utf-8")
                ).hexdigest()[:8].upper()
                print(
                    f"⚠️ 正文代码 {stock_code}（库里归属 {known}）与公司名 {company_name} "
                    f"不符且反查不到唯一主体，退回文件码 {file_code}：{path.name}"
                )
                stock_code = file_code
    values: dict[str, StatementValue] = {}
    previous_values: dict[str, Decimal] = {}
    try:
        values, previous_values = extract_values(pages, full_text, tables)
        extract_values_from_text(full_text, values, previous_values)
        # 先把资产负债表的单位倍数纠回来，再让勾稽推导用一致的量级（顺序错了会推出鬼数）
        recovered_scale = recover_balance_sheet_scale(values)
        if recovered_scale:
            print(f"ℹ️ 资产负债表的负债/权益按恒等式判为 {recovered_scale} 倍单位，已换算回元（{path.name}）")
        derive_missing_values(values)
        mismatched = drop_unit_mismatched_profits(values)
        if mismatched:
            print(
                "⚠️ 利润表列与归母量级差 100× 以上，判为单位不一致，已丢弃："
                f"{'、'.join(mismatched)}（{path.name}）"
            )
    except Exception as exc:
        print(f"⚠️ 财务字段提取不完整，继续入库：{exc}")
    if values and not looks_like_periodic_report(full_text[:900], path.name):
        # 临时公告、独立财务顾问/律师/评估/审计等中介机构文件里的数字不是本公司本期
        # 报表数（实测 920833 的备考「交易后」列被当成营业收入 43,774.88 万元入库）。
        # 这类文档照常登记、照常切片段检索，只是不写财务事实；save() 在 values 全空时
        # 不会 DELETE 旧行，所以同一个真实报告期里已经入库的定期报告数据不会被抹掉。
        print(f"ℹ️ 非定期报告（临时公告/中介机构文件）：只登记文档与检索切片，不写财务事实：{path.name}")
        values = {}
        previous_values = {}
    validations = build_validation_rows(values)

    if pages:
        print(f"✅ 文件读取完成，共 {len(pages)} 页")
    else:
        print("⚠️ 未读取到可识别正文，仍保留文件基础记录")
    company_df = pd.DataFrame(
        [
            {
                "stock_code": stock_code,
                "stock_name": company_name,
                "industry": None,
                "list_date": None,
            }
        ]
    )
    print("\n📋 【公司基本信息】")
    print(company_df.to_string(index=True))

    net_profit = values.get("net_profit")
    net_previous = previous_values.get("net_profit")
    revenue = values.get("operating_revenue")
    revenue_previous = previous_values.get("operating_revenue")
    performance_df = pd.DataFrame(
        [
            {
                "stock_code": stock_code,
                "report_period": f"{year}年半年报" if period_type == "semiannual" else f"{year}年{period_type}",
                "revenue": float(revenue.current) if revenue else None,
                "revenue_yoy": round(float((revenue.current - revenue_previous) / abs(revenue_previous) * 100), 2)
                if revenue and revenue_previous
                else None,
                "net_profit": float(net_profit.current) if net_profit else None,
                "profit_yoy": round(float((net_profit.current - net_previous) / abs(net_previous) * 100), 2)
                if net_profit and net_previous
                else None,
                "eps": float(values["basic_eps"].current)
                if "basic_eps" in values
                else None,
            }
        ]
    )
    print("\n📋 【业绩报表】")
    print(performance_df.to_string(index=False))

    for title, statement_type in (
        ("利润表", "income_statement"),
        ("资产负债表", "balance_sheet"),
        ("现金流量表", "cash_flow_statement"),
    ):
        print(f"\n📋 【{title}】")
        frame = build_statement_dataframe(statement_type, values)
        if frame.empty:
            print("❌ 未找到")
        else:
            print(frame.to_string(index=False))

    print("\n" + "=" * 60)
    print("🔍 开始数据校验与入库")
    validation_df = pd.DataFrame(validations)
    print(validation_df.to_string(index=False))
    company_id, document_id = database.save(
        path=path,
        page_count=len(pages),
        company={
            "stock_code": stock_code,
            "company_name": company_name,
            "industry_name": None,
            "list_date": None,
        },
        metadata={
            "period_type": period_type,
            "period_start": period_start,
            "period_end": period_end,
        },
        values=values,
        validations=validations,
        tables=tables,
    )
    quarantined = company_id < 0
    if quarantined:
        print("\n⛔ 本次未入库（已隔离待人工确认）：不登记文档、不做向量切片，旧数据保持原样")
    else:
        print(
            # MySQL.save 成功统一返回 (0, 0)：调用方只需要「是否隔离」这个信号，
            # 真正的自增 ID 没人用。所以这里不能再打印 公司ID=0/文档ID=0，
            # 否则看起来像写失败了（历史踩过：界面日志一片 0 让人误判入库没生效）。
            f"\n✅ 入库完成：{company_name}（{stock_code}），"
            f"财务事实={len(values)} 条，校验结果={len(validations)} 条"
        )
    if read_error:
        print(f"⚠️ 本次以容错模式入库：{read_error}")
    report_label = ""
    text_head = full_text[:6000]
    if "半年度报告摘要" in text_head or "年度报告摘要" in text_head:
        report_label = "摘要"
    elif "半年度报告全文" in text_head or "年度报告全文" in text_head:
        report_label = "全文"
    return {
        "stock_code": stock_code,
        "company_name": company_name,
        "period_type": period_type,
        "period_end": period_end.isoformat(),
        "report_label": report_label,
        # 闸门拦下时置 True：批量导入据此跳过登记与切片，保持三份真相一致
        "quarantined": quarantined,
    }

def main() -> None:
    """兼容单文件模式的命令行处理入口。"""
    database = MySQL()
    print("✅ 数据库连接成功")
    database.init_database()
    source = Path(CONFIG["pdf_path"])
    files = (
        sorted(source.rglob("*"))
        if source.is_dir()
        else [source]
    )
    supported = [
        path for path in files if path.is_file() and path.suffix.lower() in {".pdf", ".docx"}
    ]
    if not supported:
        print("❌ 没有找到 PDF 或 DOCX 文件")
        return
    for path in supported:
        process_financial_document(path, database)

core = sys.modules[__name__]

def write_text_atomically(path: Path, text: str) -> None:
    """原子写入文本文件：先写同目录临时文件，再用 os.replace 顶上。

    原来直接 write_text：写到一半中断就会留下截断的 rag_chunks.json（三百 MB 级），
    下次启动 JSON 解析失败，整个知识库被当成空的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    last_error: Exception | None = None
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temp, path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.12 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        temp.unlink(missing_ok=True)

def normalize_chat_session(item: dict[str, Any]) -> dict[str, Any]:
    """将旧版单条问答历史转换为包含消息列表的会话结构。"""
    session_id = str(item.get("id") or uuid.uuid4().hex)
    if isinstance(item.get("messages"), list):
        messages = list(item["messages"])
    else:
        question = str(item.get("question") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        messages = []
        if question:
            messages.append(
                {
                    "id": f"{session_id}-user",
                    "role": "user",
                    "content": question,
                    "created_at": item.get("updated_at", ""),
                }
            )
        if result:
            messages.append(
                {
                    "id": f"{session_id}-assistant",
                    "role": "assistant",
                    "result": result,
                    "created_at": item.get("updated_at", ""),
                }
            )
    normalized_messages = []
    for message in messages:
        normalized_message = dict(message) if isinstance(message, dict) else message
        if isinstance(normalized_message, dict):
            result = normalized_message.get("result")
            if isinstance(result, dict):
                normalized_result = dict(result)
                table_rows = (
                    normalized_result.get("raw_table")
                    or normalized_result.get("table")
                    or []
                )
                chart = normalized_result.get("chart")
                if (
                    isinstance(chart, dict)
                    and isinstance(table_rows, list)
                    and len(table_rows) >= 2
                    and str(chart.get("y") or "") in {
                        "stock_code",
                        "股票代码",
                        "report_period",
                        "报告期",
                    }
                ):
                    x_key = (
                        "stock_name"
                        if table_rows[0].get("stock_name") is not None
                        else "公司名称"
                        if table_rows[0].get("公司名称") is not None
                        else "stock_code"
                    )
                    metric_candidates = (
                        "营业收入",
                        "revenue",
                        "净利润",
                        "net_profit",
                        "资产负债率",
                        "debt_ratio",
                    )
                    y_key = next(
                        (
                            metric
                            for metric in metric_candidates
                            if sum(
                                1
                                for row in table_rows
                                if re.search(
                                    r"-?\d+(?:\.\d+)?",
                                    str(row.get(metric) or ""),
                                )
                            )
                            >= 2
                        ),
                        "",
                    )
                    if y_key:
                        names = [
                            str(row.get(x_key) or "")
                            for row in table_rows
                            if row.get(x_key)
                        ]
                        normalized_result["chart"] = {
                            "type": (
                                str(chart.get("type") or "line")
                                if str(chart.get("type") or "") in {
                                    "line",
                                    "bar",
                                    "area",
                                }
                                else "line"
                            ),
                            "title": "、".join(names[:3]) + "经营指标对比",
                            "x": x_key,
                            "y": y_key,
                            "data": table_rows,
                            "colors": [
                                "#2dd4bf",
                                "#f0b35c",
                                "#60a5fa",
                                "#f472b6",
                                "#a78bfa",
                            ],
                        }
                normalized_result["table"] = localize_financial_table(
                    table_rows
                )
                if normalized_result.get("chart"):
                    normalized_result["chart"] = localize_financial_chart(
                        normalized_result["chart"]
                    )
                normalized_message["result"] = normalized_result
        normalized_messages.append(normalized_message)
    return {
        "id": session_id,
        "title": item.get("title") or "未命名会话",
        "updated_at": item.get("updated_at", ""),
        "messages": normalized_messages,
        "locked_company": (
            item.get("locked_company")
            if isinstance(item.get("locked_company"), dict)
            else {}
        ),
        "locked_deal": (
            item.get("locked_deal")
            if isinstance(item.get("locked_deal"), dict)
            else {}
        ),
        "analysis_context": (
            item.get("analysis_context")
            if isinstance(item.get("analysis_context"), dict)
            else {}
        ),
    }

def lock_chat_session_company(
    session_id: str,
    ticker: str,
    company_name: str,
) -> dict[str, str] | None:
    """在文件导入完成后为当前会话锁定公司。"""
    with CHAT_SESSIONS_LOCK:
        session_id = (session_id or "").strip()
        ticker = (ticker or "").strip()
        if not session_id or not ticker:
            return None
        session = next(
            (item for item in CHAT_SESSIONS if item.get("id") == session_id),
            None,
        )
        if session is None:
            return None
        locked = session.get("locked_company")
        if isinstance(locked, dict) and locked.get("ticker"):
            return {
                "ticker": str(locked.get("ticker") or ""),
                "name": str(locked.get("name") or ""),
            }
        name = resolve_company_name(company_name, ticker)
        session["locked_company"] = {"ticker": ticker, "name": name}
        session["locked_deal"] = {
            "deal_id": f"{ticker}:current_transaction",
            "label": "本次交易",
        }
        save_chat_sessions()
        return dict(session["locked_company"])

def ensure_import_chat_session(session_id: str) -> str:
    """导入只复用用户**已有**的会话；没有就返回空串。

    以前这里会给导入凭空造一条「文件导入会话」，再由
    `update_import_session_status` 把它的标题反复改写成
    「导入中：3/20 · 成功2 · 重复0 · 失败0」——于是历史会话列表里
    常年挂着一条不是聊天的条目（用户要求改掉：导入状态不放历史会话，
    改放「数据导入」页的进度卡）。会话为空时下游
    `lock_chat_session_company` 本来就会跳过，不影响导入本身。
    """
    with CHAT_SESSIONS_LOCK:
        session_id = (session_id or "").strip()
        if session_id and any(
            item.get("id") == session_id for item in CHAT_SESSIONS
        ):
            return session_id
        return ""

def normalize_db_chat_row(item: dict[str, Any]) -> dict[str, Any]:
    """兼容旧版结果字段和新版完整会话字段。"""
    payload = json.loads(item.get("result_json") or "{}")
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        payload.setdefault("id", item.get("id"))
        payload.setdefault("title", item.get("title") or "未命名会话")
        payload.setdefault("updated_at", item.get("updated_at", ""))
        return normalize_chat_session(payload)
    return normalize_chat_session(
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "updated_at": item.get("updated_at"),
            "question": item.get("question") or "",
            "result": payload,
        }
    )

def load_chat_sessions() -> list[dict[str, Any]]:
    """从数据库或 JSON 恢复聊天历史。"""
    try:
        connection = pymysql.connect(**db_config())  # 复用同一份连接参数与 SSL 上下文
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, title, updated_at, question, result_json
                    FROM 8_chat_history
                    ORDER BY updated_at DESC
                    LIMIT 200
                    """
                )
                rows = list(cursor.fetchall())
                if rows:
                    return [normalize_db_chat_row(item) for item in reversed(rows)]
        finally:
            connection.close()
    except Exception:
        pass
    if not CHAT_HISTORY_PATH.exists():
        return []
    try:
        return [
            normalize_chat_session(item)
            for item in json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        ]
    except Exception:
        return []

def save_chat_sessions() -> None:
    """串行保存聊天历史，避免多线程覆盖。"""
    with CHAT_SESSIONS_LOCK:
        _save_chat_sessions_unlocked()

def _save_chat_sessions_unlocked() -> None:
    """在持有会话锁时写入聊天历史。"""
    recent = CHAT_SESSIONS[-200:]
    records = []
    for item in recent:
        messages = item.get("messages") if isinstance(item.get("messages"), list) else []
        first_question = next(
            (
                str(message.get("content") or "")
                for message in messages
                if message.get("role") == "user"
            ),
            item.get("title", ""),
        )
        records.append(
            (
                item.get("id", uuid.uuid4().hex),
                item.get("title", ""),
                item.get("updated_at", ""),
                first_question,
                json.dumps(item, ensure_ascii=False, default=str),
            )
        )
    try:
        core.MySQL().init_database()
        connection = pymysql.connect(**db_config())
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM 8_chat_history")
                if recent:
                    cursor.executemany(
                        """
                        INSERT INTO 8_chat_history (
                          id, title, updated_at, question, result_json
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        records,
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception:
        pass
    try:
        write_text_atomically(
            CHAT_HISTORY_PATH,
            json.dumps(recent, ensure_ascii=False, indent=2, default=str),
        )
    except Exception:
        pass

def load_rag_chunks() -> list[dict[str, Any]]:
    """从数据库或 JSON 恢复 RAG 文本块。"""
    try:
        connection = pymysql.connect(**db_config())  # 复用同一份连接参数与 SSL 上下文
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, file_name, file_path, stock_code, company_name,
                           page_no, chunk_index, content, embedding_json
                    FROM 6_rag_chunks
                    ORDER BY id
                    """
                )
                rows = list(cursor.fetchall())
                if rows:
                    for row in rows:
                        if str(row.get("embedding_json") or "").strip():
                            row.setdefault("embedding_model", LOCAL_EMBEDDING_MODEL)
                            row.setdefault("embedding_dim", LOCAL_EMBEDDING_DIM)
                            row.setdefault("embedding_version", VECTOR_INDEX_VERSION)
                    return rows
        finally:
            connection.close()
    except Exception:
        pass
    if not RAG_CHUNKS_PATH.exists():
        return []
    try:
        payload = json.loads(RAG_CHUNKS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except Exception:
        return []

def save_rag_chunks() -> None:
    """将 RAG 文本块保存到 JSON。"""
    try:
        write_text_atomically(
            RAG_CHUNKS_PATH,
            json.dumps(RAG_CHUNKS[-50000:], ensure_ascii=False, indent=2),
        )
    except Exception:
        pass

def load_local_embedder() -> Any:
    """按需加载本地中文向量模型。"""
    global _LOCAL_EMBEDDER
    if _LOCAL_EMBEDDER is not None:
        return _LOCAL_EMBEDDER
    with _EMBEDDER_LOCK:
        if _LOCAL_EMBEDDER is None:
            from fastembed import TextEmbedding

            VECTOR_MODEL_CACHE.mkdir(parents=True, exist_ok=True)
            _LOCAL_EMBEDDER = TextEmbedding(
                model_name=LOCAL_EMBEDDING_MODEL,
                cache_dir=str(VECTOR_MODEL_CACHE),
            )
    return _LOCAL_EMBEDDER

def local_embedding_vectors(
    texts: list[str],
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> list[list[float]]:
    """使用本地模型生成文本向量。"""
    values = [str(text or "")[:3000] for text in texts]
    if not values:
        return []
    model = load_local_embedder()
    embeddings = model.embed(values, batch_size=max(1, int(batch_size)))
    return [
        np.asarray(vector, dtype=np.float32).tolist()
        for vector in embeddings
    ]

def load_vector_index_status() -> dict[str, Any]:
    """返回本地向量索引状态。"""
    total = len(RAG_CHUNKS)
    indexed = sum(
        1
        for chunk in RAG_CHUNKS
        if str(chunk.get("embedding_json") or "").strip()
    )
    current_model = sum(
        1
        for chunk in RAG_CHUNKS
        if str(chunk.get("embedding_model") or "") == LOCAL_EMBEDDING_MODEL
        and str(chunk.get("embedding_json") or "").strip()
    )
    return {
        "model": LOCAL_EMBEDDING_MODEL,
        "dimension": LOCAL_EMBEDDING_DIM,
        "version": VECTOR_INDEX_VERSION,
        "total": total,
        "indexed": indexed,
        "current_model": current_model,
        "pending": max(0, total - current_model),
        "ready": total > 0 and current_model == total,
        "qdrant": vector_store.get_status(),
    }

def _persist_vector_batch(
    chunks: list[dict[str, Any]],
) -> None:
    """将一批向量写回 MySQL，失败时保留 JSON 索引。"""
    if not chunks:
        return
    by_id = [
        (str(chunk.get("embedding_json") or ""), int(chunk["id"]))
        for chunk in chunks
        if chunk.get("id") is not None
    ]
    by_key = [
        (
            str(chunk.get("embedding_json") or ""),
            str(chunk.get("file_path") or ""),
            int(chunk.get("page_no") or 0),
            int(chunk.get("chunk_index") or 0),
        )
        for chunk in chunks
        if chunk.get("id") is None
    ]
    try:
        connection = pymysql.connect(**db_config())
        try:
            with connection.cursor() as cursor:
                if by_id:
                    cursor.executemany(
                        "UPDATE 6_rag_chunks SET embedding_json=%s WHERE id=%s",
                        by_id,
                    )
                if by_key:
                    cursor.executemany(
                        """
                        UPDATE 6_rag_chunks
                        SET embedding_json=%s
                        WHERE file_path=%s AND page_no=%s AND chunk_index=%s
                        """,
                        by_key,
                    )
            connection.commit()
        finally:
            connection.close()
    except Exception as exc:
        print(f"[VECTOR_INDEX] MySQL 写入失败，保留 JSON 索引：{exc}")

def build_rag_vector_index(
    force: bool = False,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> dict[str, Any]:
    """为未索引文本块生成并保存本地向量。"""
    with _VECTOR_INDEX_LOCK:
        pending = [
            chunk
            for chunk in RAG_CHUNKS
            if force
            or str(chunk.get("embedding_model") or "") != LOCAL_EMBEDDING_MODEL
            or not str(chunk.get("embedding_json") or "").strip()
        ]
        total_pending = len(pending)
        completed = 0
        print(
            "[VECTOR_INDEX] "
            f"model={LOCAL_EMBEDDING_MODEL} pending={total_pending}"
        )
        for start in range(0, total_pending, max(1, int(batch_size))):
            batch = pending[start : start + max(1, int(batch_size))]
            vectors = local_embedding_vectors(
                [str(chunk.get("content") or "") for chunk in batch],
                batch_size=batch_size,
            )
            updated = []
            for chunk, vector in zip(batch, vectors):
                encoded = json.dumps(vector, ensure_ascii=False)
                chunk["embedding_json"] = encoded
                chunk["embedding_model"] = LOCAL_EMBEDDING_MODEL
                chunk["embedding_dim"] = LOCAL_EMBEDDING_DIM
                chunk["embedding_version"] = VECTOR_INDEX_VERSION
                updated.append(chunk)
            _persist_vector_batch(updated)
            vector_store.upsert_chunks(updated)
            completed += len(updated)
            print(
                "[VECTOR_INDEX] "
                f"progress={completed}/{total_pending}"
            )
        save_rag_chunks()
        status = load_vector_index_status()
        status["processed"] = completed
        return status

def index_new_rag_chunks(chunks: list[dict[str, Any]], save: bool = True) -> str:
    """增量生成新导入文本块的向量。返回错误信息（"" 表示成功）。"""
    if not chunks:
        return ""
    try:
        # 幂等前提：先把这批文档在 Qdrant 里的旧向量删掉，再灌新点。
        # point id 由 file_path|page_no|chunk_index 派生（见 vector_store），
        # 重切同一份文件时若分页/切块边界变了，落在新 key 之外的旧点不会被覆盖，
        # 就成了孤儿——RAG 会检索到数据库里早已删除的旧 chunk，给出对不上的引用。
        # 6_rag_chunks 表是按 file_path 先删后插的，向量侧必须跟着按 file_path 先删，
        # 两边才对得上。delete_file_chunks 走的是 payload 过滤删除，本地模式下也是
        # 真删（不像 delete_collection 会把数据接回来）。
        file_paths = {
            str(chunk.get("file_path") or "")
            for chunk in chunks
            if chunk.get("file_path")
        }
        for file_path in file_paths:
            vector_store.delete_file_chunks(file_path)
        vectors = local_embedding_vectors(
            [str(chunk.get("content") or "") for chunk in chunks],
        )
        for chunk, vector in zip(chunks, vectors):
            chunk["embedding_json"] = json.dumps(vector, ensure_ascii=False)
            chunk["embedding_model"] = LOCAL_EMBEDDING_MODEL
            chunk["embedding_dim"] = LOCAL_EMBEDDING_DIM
            chunk["embedding_version"] = VECTOR_INDEX_VERSION
        _persist_vector_batch(chunks)
        vector_store.upsert_chunks(chunks)
        if save:
            save_rag_chunks()
        return ""
    except Exception as exc:
        print(f"[VECTOR_INDEX] 增量索引失败：{exc}")
        return str(exc)

def sync_qdrant_vector_index(
    force_reset: bool = False,
) -> dict[str, Any]:
    """将已经生成的本地向量同步到 Qdrant。"""
    if force_reset:
        vector_store.reset_collection()
    chunks = [
        chunk
        for chunk in RAG_CHUNKS
        if str(chunk.get("embedding_json") or "").strip()
    ]
    written = vector_store.upsert_chunks(chunks)
    return {
        "written": written,
        "qdrant": vector_store.get_status(),
    }

def start_vector_index_build(force: bool = False) -> dict[str, Any]:
    """在后台创建向量索引任务。"""
    with VECTOR_INDEX_JOB_LOCK:
        if VECTOR_INDEX_JOB.get("status") == "running":
            return dict(VECTOR_INDEX_JOB)
        VECTOR_INDEX_JOB.clear()
        VECTOR_INDEX_JOB.update(
            {
                "status": "running",
                "force": bool(force),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
                "error": "",
                "status_data": load_vector_index_status(),
            }
        )
        job = dict(VECTOR_INDEX_JOB)

    def worker() -> None:
        """后台执行向量索引构建并记录任务结果。"""
        try:
            result = build_rag_vector_index(force=force)
            with VECTOR_INDEX_JOB_LOCK:
                VECTOR_INDEX_JOB["status"] = "completed"
                VECTOR_INDEX_JOB["status_data"] = result
                VECTOR_INDEX_JOB["finished_at"] = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
        except Exception as exc:
            with VECTOR_INDEX_JOB_LOCK:
                VECTOR_INDEX_JOB["status"] = "failed"
                VECTOR_INDEX_JOB["error"] = str(exc)
                VECTOR_INDEX_JOB["finished_at"] = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

    threading.Thread(target=worker, daemon=True).start()
    return job

def auto_backfill_pending_vectors(reason: str = "") -> dict[str, Any]:
    """发现「切片已入库、向量还没嵌」就自己在后台补齐，不用人去界面点重建。

    两条触发路：① 导入任务收尾（正常完成/手动结束/暂停后退出都算，
    `index_new_rag_chunks` 只有跑到那条切片才会嵌上，中途停就留下洞）；
    ② 服务启动（上次进程被打断时根本走不到收尾）。
    只补 `pending`，**不 force** ⇒ 已嵌好的一条都不会重嵌（不重复烧 embedding）。
    `start_vector_index_build` 自己有 `VECTOR_INDEX_JOB_LOCK` + `status=="running"`
    挡板 ⇒ 与手动点的那次并发也只会有一趟。
    """
    try:
        pending = int(load_vector_index_status().get("pending") or 0)
    except Exception as exc:
        print(f"⚠️ 自动补嵌跳过（读向量状态失败）：{exc}")
        return {"status": "skipped", "reason": str(exc)}
    if pending <= 0:
        return {"status": "idle", "pending": 0}
    print(f"🧩 自动补嵌 {pending} 条切片的向量（触发：{reason or '未注明'}）")
    job = start_vector_index_build(force=False)
    job["pending"] = pending
    return job

def get_vector_index_status_api() -> dict[str, Any]:
    """返回向量索引状态和最近一次构建任务。"""
    status = load_vector_index_status()
    with VECTOR_INDEX_JOB_LOCK:
        status["job"] = dict(VECTOR_INDEX_JOB)
    return status

METRIC_QUERY_MAP = {
    "营业收入": ("2_performance_report", "revenue", "营业收入"),
    "营收": ("2_performance_report", "revenue", "营业收入"),
    "净利润": ("2_performance_report", "net_profit", "净利润"),
    "归母净利润": ("2_performance_report", "net_profit_parent", "归母净利润"),
    "每股收益": ("2_performance_report", "eps", "每股收益"),
    # 轮廿二：同比列一直在库里（revenue_yoy/profit_yoy），但没进指标映射，
    # 本地结构化永远答不出「营收同比」——补上入口。净利润同比是**合并口径**
    # 现算的，label 里带明（见 SCHEMA 规则 4，与公告归母同比不是一个数）。
    "营业收入同比": ("2_performance_report", "revenue_yoy", "营收同比"),
    "营收同比": ("2_performance_report", "revenue_yoy", "营收同比"),
    "净利润同比": ("2_performance_report", "profit_yoy", "净利润同比（合并口径）"),
    "总资产": ("4_balance_sheet", "total_assets", "总资产"),
    "资产总额": ("4_balance_sheet", "total_assets", "总资产"),
    "总负债": ("4_balance_sheet", "total_liability", "总负债"),
    "资产负债率": ("4_balance_sheet", "__debt_ratio__", "资产负债率"),
    "股东权益": ("4_balance_sheet", "total_equity", "股东权益"),
    "经营现金流": ("5_cash_flow_statement", "operate_cash", "经营活动现金流净额"),
    "现金流": ("5_cash_flow_statement", "operate_cash", "经营活动现金流净额"),
    "投资现金流": ("5_cash_flow_statement", "invest_cash", "投资活动现金流净额"),
    "筹资现金流": ("5_cash_flow_statement", "finance_cash", "筹资活动现金流净额"),
    "汇率变动现金流": ("5_cash_flow_statement", "fx_cash", "汇率变动对现金的影响"),
    "现金净增加额": ("5_cash_flow_statement", "cash_increase", "现金净增加额"),
}

CHART_WORDS = ("图", "图表", "趋势", "走势", "对比", "画")

PERSPECTIVE_PROMPTS = {
    "default": "你是一名专业的财报分析师，客观中立地分析数据，兼顾成长性、盈利能力和风险。",
    "investment_bank": "你是一名资深投行/卖方分析师。侧重分析企业的营收成长性、利润率变化、行业竞争格局与估值逻辑，多使用专业金融术语，并给出明确的投资倾向判断。",
    "management": "你是企业高级管理层（如CFO）。侧重分析成本控制、运营效率、资本开支合理性，并提出具体可落地的管理建议，语言务实、强调降本增效。",
    "risk_control": "你是风控与审计专家。高度关注企业的偿债能力、现金流健康度、应收账款周转、表外风险及财务造假预警信号，语气严谨、客观、审慎。",
    "retail": "你是面向散户投资者的投顾。用通俗易懂的语言解释财务数据，避免过多专业术语，重点说明这对股价、分红和投资风险可能产生的影响。",
}

AUDIENCE_PERSPECTIVE_KEYS = {
    "综合分析师": "default",
    "投行/卖方分析师": "investment_bank",
    "企业管理层": "management",
    "风险控制/审计": "risk_control",
    "个人投资者": "retail",
}

FINANCIAL_SCHEMA_PROMPT = """1_stock_basic(stock_code, stock_name, industry, list_date)
2_performance_report(stock_code, report_period, revenue, net_profit, net_profit_parent, eps, revenue_yoy, profit_yoy)
3_income_statement(stock_code, report_period, operating_revenue, operating_cost, operating_profit, total_profit, net_profit, net_profit_parent, basic_eps)
4_balance_sheet(stock_code, report_period, total_assets, total_liability, total_equity, current_assets, current_liability)
5_cash_flow_statement(stock_code, report_period, operate_cash, invest_cash, finance_cash, fx_cash, cash_increase)

【写 SQL 的规则】
1. 金额和比率一律直接 SELECT 原始列（库里金额单位是元、同比单位是百分数），
   不要自己 ROUND(x/10000) 折算，也不要给列起 _wan/_yi 之类的别名——展示层会统一换算单位。
2. 需要哪个口径就选哪个原列：净利润 net_profit、归母净利润 net_profit_parent，两个都要就两列都选。
3. 只查题目问到的列，别把整行 21 列都带上。
4. profit_yoy 是按**合并口径**现算的（(本期净利润-上期净利润)/|上期净利润|），和公告「主要会计数据」里
   披露的**归母同比**不是同一个数，少数股东损益大时两者方向都可能相反
   （实测 002195 岩山科技 2025年年报：本列 -98.46%，公告披露 +184.19%）。
   回答「净利润同比」时必须写明是合并口径，不要把它说成公告里披露的那个同比。"""

SQL_SELF_CORRECTION_PROMPT = """你是一个 MySQL 专家。
【用户问题】{question}
【之前生成的错误 SQL】{wrong_sql}
【执行报错信息】{error_msg}
【数据库表结构】
""" + FINANCIAL_SCHEMA_PROMPT + """
请根据报错信息，重新生成一条正确的、可执行的 SQL。
要求：只输出修正后的 SQL 语句，不要输出任何解释、Markdown 标记或其他文字。"""

INTENT_CLARIFICATION_PROMPT = """用户当前的问题比较模糊，缺少关键的查询要素（如具体公司、报告期或指标）。
请你以友好的语气，主动向用户提问，引导用户补充以下信息：
1. 想查询哪家公司（公司名称或股票代码）；
2. 想查询哪个报告期；
3. 想查询哪些具体财务指标。
不要尝试猜测数据，只负责提问引导。"""

MULTI_SOURCE_FUSION_PROMPT = """你是一名资深财报分析师。
【结构化财务数据】
{structured_data}
【非结构化研报证据】
{rag_evidence}
【用户问题】
{question}
请结合以上两类信息，进行深度的归因分析：
1. 用财务数据说明"发生了什么"（数据变化的量级和方向）；
2. 用研报证据说明"为什么发生"（行业、政策、竞争等定性原因）；
3. 给出你的专业判断和后续关注点。
不得编造任何数据或事实，如证据不足请明确说明。"""

PLANNER_FEW_SHOT = """【示例1】
用户：600007 2026年半年报营业收入和净利润是多少？
输出：{"action":"sql","sql":"SELECT report_period, revenue, net_profit FROM 2_performance_report WHERE stock_code='600007' ORDER BY report_period DESC LIMIT 1","chart":{"type":"none"}}
【示例2】
用户：画出600007近几期营业收入趋势图
输出：{"action":"sql","sql":"SELECT report_period, revenue FROM 2_performance_report WHERE stock_code='600007' ORDER BY report_period ASC LIMIT 8","chart":{"type":"line","x":"report_period","y":"revenue","title":"600007营业收入趋势"}}
【示例3】
用户：分析一下这家公司的主要经营风险
输出：{"action":"rag","sql":"","rag_query":"经营风险 不确定性 诉讼 担保 减值","chart":{"type":"none"}}
【示例4】
用户：净利润下降的原因是什么？
输出：{"action":"hybrid","sql":"SELECT report_period, net_profit, revenue, operating_cost FROM 2_performance_report WHERE stock_code='600007' ORDER BY report_period DESC LIMIT 4","rag_query":"净利润下降 原因 行业 竞争 成本","chart":{"type":"none"}}
【示例5】
用户：600007和600310的营业收入、净利润对比
输出：{"action":"plan","intent":"multi_company_comparison","companies":[{"ticker":"600007","name":""},{"ticker":"600310","name":""}],"subtasks":[{"id":"sql_600007","kind":"sql","company":{"ticker":"600007","name":""},"depends_on":[],"sql":"SELECT stock_code, report_period, revenue, net_profit FROM 2_performance_report WHERE stock_code='600007' ORDER BY report_period DESC LIMIT 1","purpose":"查询600007最新一期营业收入和净利润","expect_columns":["stock_code","report_period","revenue","net_profit"]},{"id":"sql_600310","kind":"sql","company":{"ticker":"600310","name":""},"depends_on":[],"sql":"SELECT stock_code, report_period, revenue, net_profit FROM 2_performance_report WHERE stock_code='600310' ORDER BY report_period DESC LIMIT 1","purpose":"查询600310最新一期营业收入和净利润","expect_columns":["stock_code","report_period","revenue","net_profit"]}],"combine":{"mode":"wide","group_by":["stock_code","report_period"]},"chart":{"type":"bar","x":"stock_name","y":"revenue","title":"营业收入对比"}}"""

def perspective_prompt_for(audience: str) -> str:
    """生成指定分析视角的提示词。"""
    key = AUDIENCE_PERSPECTIVE_KEYS.get(audience, "default")
    return PERSPECTIVE_PROMPTS.get(key, PERSPECTIVE_PROMPTS["default"])

AUDIENCE_FOCUS = {
    "综合分析师": "综合来看，还需结合盈利质量、成长性和现金流变化持续跟踪。",
    "投行/卖方分析师": "从投资逻辑看，后续应重点跟踪成长兑现、估值匹配和行业竞争格局。",
    "企业管理层": "从经营动作看，建议重点关注成本效率、资本开支回报和现金回收。",
    "风险控制/审计": "从风险角度，应重点核查偿债期限、现金流匹配和异常变动信号。",
    "个人投资者": "对个人投资者而言，还应关注盈利稳定性、分红能力和后续业绩持续性。",
}

TABLE_ALIASES = {
    "stock_basic": "1_stock_basic",
    "performance_report": "2_performance_report",
    "income_statement": "3_income_statement",
    "balance_sheet": "4_balance_sheet",
    "cash_flow_statement": "5_cash_flow_statement",
}

ALLOWED_TABLES = set(TABLE_ALIASES.values())

COLUMN_LABELS = {
    "id": "序号",
    "stock_code": "股票代码",
    "stock_name": "公司名称",
    "industry": "所属行业",
    "list_date": "上市日期",
    "report_period": "报告期",
    "revenue": "营业收入",
    "net_profit": "净利润",
    "net_profit_parent": "归母净利润",
    "eps": "每股收益",
    "revenue_yoy": "营收同比",
    # 实测（2026-10-28，_tools\_hist\trace_profit_yoy.log）：这一列是按「五、净利润」（含少数股东）
    # 现算的合并口径，和公告「主要会计数据」里披露的归母同比不是一个数，少数股东损益大时方向都会相反
    # （002195 岩山科技 2025年年报：本列 -98.4641%，公告 +184.19%）。标签必须把口径写出来。
    "profit_yoy": "净利润同比（合并口径）",
    "operating_revenue": "营业总收入",
    "operating_cost": "营业成本",
    "operating_profit": "营业利润",
    "total_profit": "利润总额",
    "basic_eps": "基本每股收益",
    "total_assets": "总资产",
    "total_liability": "总负债",
    "total_equity": "股东权益",
    "current_assets": "流动资产",
    "current_liability": "流动负债",
    "debt_ratio": "资产负债率",
    "operate_cash": "经营现金流净额",
    "invest_cash": "投资现金流净额",
    "finance_cash": "筹资现金流净额",
    "fx_cash": "汇率变动对现金的影响",
    "cash_increase": "现金净增加额",
    "company_name": "公司名称",
    "company_ticker": "股票代码",
    "period": "报告期",
    "metric": "指标",
    "metric_name": "指标",
    "label": "项目",
    "value": "数值",
    "指标": "指标",
    "数值": "数值",
    "本期数值": "本期数值",
    "变化说明": "变化说明",
}

SUGGESTION_POOL = [
    "600007 2026年半年报营业收入和净利润是多少？",
    "画出600007营业收入和净利润对比图",
    "600007的资产负债率是多少？",
    "分析600007经营活动现金流变化",
    "600007归母净利润是多少？",
    "查看600007最近几个报告期的营收趋势",
]

# 模型在 SQL 里自己折算金额时常用的单位后缀（实测 600007 那次写的是 net_profit_wan）。
DERIVED_AMOUNT_UNITS: tuple[tuple[str, float], ...] = (
    ("_wan", 10_000.0),
    ("_wy", 10_000.0),
    ("_yi", 100_000_000.0),
    ("_yy", 100_000_000.0),
    ("_qian", 1_000.0),
    ("_yuan", 1.0),
)

def derived_amount_alias(name: Any) -> tuple[str, float] | None:
    """把模型自造的金额别名（net_profit_wan / revenue_yi …）翻译回真实字段。

    返回 (库里真实字段名, 折算回「元」的倍数)；认不出返回 None。
    只认「已知字段 + 明确单位后缀」这一种组合，所以 is_net_profit 这种
    布尔列仍然拿不到中文标签、照旧被 drop_internal_columns 挡掉。
    """
    key = str(name).strip().lower()
    for suffix, factor in DERIVED_AMOUNT_UNITS:
        if not key.endswith(suffix):
            continue
        base = key[: -len(suffix)]
        if not base or base.endswith("_yoy") or base in {"debt_ratio", "roe", "roa"}:
            continue
        if base in COLUMN_LABELS:
            return base, factor
    return None

def localize_financial_table(rows: list[dict[str, Any]], unit_mode: str = "自动") -> list[dict[str, Any]]:
    """将查询结果字段转换为中文表头，并让同一列共用一个金额单位。

    原来每个格子各算各的单位，同一列里会同时出现「8,119.84 万元」和
    「36.97 亿元」，而且完全不看用户在界面上选的单位偏好。
    """
    amount_fields = {
        "revenue",
        "net_profit",
        "net_profit_parent",
        "operating_revenue",
        "operating_cost",
        "operating_profit",
        "total_profit",
        "total_assets",
        "total_liability",
        "total_equity",
        "current_assets",
        "current_liability",
        "operate_cash",
        "invest_cash",
        "finance_cash",
        "fx_cash",
        "cash_increase",
    }
    percent_fields = {"debt_ratio", "revenue_yoy", "profit_yoy"}
    amount_labels = {COLUMN_LABELS.get(item, item) for item in amount_fields}
    percent_labels = {COLUMN_LABELS.get(item, item) for item in percent_fields}

    peaks: dict[str, float] = {}
    for row in rows:
        row_keys = {str(field) for field in row}
        for field, value in row.items():
            name = str(field)
            derived = derived_amount_alias(name)
            if derived and derived[0] not in row_keys:
                # 别名折算回「元」量级再登记，单位选择才不会被 63345.86 这种万元数值带跑
                try:
                    derived_peak = abs(float(value)) * derived[1]
                except (TypeError, ValueError):
                    derived_peak = 0.0
                for alias in {derived[0], str(COLUMN_LABELS.get(derived[0], derived[0]))}:
                    peaks[alias] = max(peaks.get(alias, 0.0), derived_peak)
            if name not in amount_fields and name not in amount_labels:
                continue
            try:
                magnitude = abs(float(value))
            except (TypeError, ValueError):
                continue
            for alias in {name, COLUMN_LABELS.get(name, name)}:
                peaks[alias] = max(peaks.get(alias, 0.0), magnitude)
    preferred_unit = unit_mode if unit_mode in {"元", "万元", "亿元"} else ""

    def column_unit(name: str) -> str:
        if preferred_unit:
            return preferred_unit
        peak = peaks.get(name, 0.0)
        if peak >= 100_000_000:
            return "亿元"
        if peak >= 10_000:
            return "万元"
        return "元"

    def format_value(key: str, value: Any) -> Any:
        if value is None or value == "":
            return "暂无数据"
        if key in percent_fields or key in percent_labels:
            try:
                return f"{float(value):.2f}%"
            except (TypeError, ValueError):
                return str(value)
        if key in amount_fields or key in amount_labels:
            try:
                float(value)
            except (TypeError, ValueError):
                return str(value)
            return format_amount(value, column_unit(key))
        return value

    localized: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            name = str(key)
            derived = derived_amount_alias(name)
            if derived:
                base, factor = derived
                label = str(COLUMN_LABELS.get(base, base))
                if label in item:
                    continue  # 原始口径列已在 ⇒ 别名让位，不覆盖（实测 23:41 那次的正常行为要保住）
                try:
                    item[label] = format_value(base, float(value) * factor)
                except (TypeError, ValueError):
                    continue
                continue
            item[COLUMN_LABELS.get(name, name)] = format_value(name, value)
        # 同一行里两个金额列被四舍五入成**同一个字符串** ⇒ 差异被显示抹掉了。实测 002595
        # 山东豪迈机械科技 2026 半年报：净利润 1,134,069,896.00、归母净利润 1,133,633,584.68
        # （差额 436,311.32 = 少数股东损益，PDF p49 原文对得上），两个口径在库里是分开的两列，
        # 却都显示成「11.34 亿元」⇒ 用户直接问「净利润为什么等于归母净利润」。
        # 只把**撞车的这几列**降到还能分出高下的单位（亿元→万元→元），其余列一律不动。
        collision: dict[str, list[Any]] = {}
        for key, value in row.items():
            name = str(key)
            if name not in amount_fields and name not in amount_labels:
                continue
            try:
                raw = float(value)
            except (TypeError, ValueError):
                continue
            label = str(COLUMN_LABELS.get(name, name))
            text = str(item.get(label, ""))
            if not text or text == "暂无数据":
                continue
            collision.setdefault(text, []).append((name, raw))
        for text, cells in collision.items():
            if len(cells) < 2 or len({round(cell[1], 6) for cell in cells}) < 2:
                continue
            for name, raw in cells:
                label = str(COLUMN_LABELS.get(name, name))
                for finer in ("万元", "元"):
                    item[label] = format_amount(raw, finer)
                    if str(item[label]) != text:
                        break
        localized.append(item)
    return drop_internal_columns(localized)

def drop_internal_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉没有中文标签的原始字段名。

    智能模式的 SQL 是模型写的，它经常多带一列（实测漏出过
    is_net_profit = 81198372.2200），这种库里字段名直接进表格既看不懂，
    又绕过单位换算把裸金额摊给用户。只有当整行都是这类键时才原样保留，
    免得把自定义列名的表清空。
    """
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        kept = {
            key: value
            for key, value in row.items()
            if re.search(r"[\u4e00-\u9fff]", str(key))
        }
        cleaned.append(kept if kept else row)
    return cleaned

CHART_RATE_FIELDS = {"debt_ratio", "revenue_yoy", "profit_yoy"}

def chart_series_unit(name: Any, values: list[Any]) -> tuple[str, float]:
    """给一个数据列定「显示单位 + 折算回元的倍数」。

    判据和 `localize_financial_table` 内部的 `column_unit` 一致（峰值 ≥1e8 用亿、
    ≥1e4 用万），但那个是函数内闭包拿不到，这里独立实现一份；差异在于这里
    **只报单位、不折算数值** —— 数值保持库里原始金额，前端按 unit 格式化，
    避免出现「后端除过一次、前端 chartNumber 又除一次」的双重折算。
    """
    text = str(name or "")
    numbers = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and abs(number) != float("inf"):
            numbers.append(abs(number))
    peak = max(numbers) if numbers else 0.0
    if text in CHART_RATE_FIELDS or re.search(r"(%|百分|比率|率)", text):
        return "%", 1.0
    if re.search(r"(股|股本|股数)", text):
        return "股", 1.0
    if peak >= 1e8:
        return "亿元", 1e8
    if peak >= 1e4:
        return "万元", 1e4
    if peak > 0:
        return "元", 1.0
    return "", 1.0

CHART_LONG_VALUE_KEYS = {"数值", "value", "金额", "指标值", "metric_value", "本期数值", "number"}
CHART_LONG_GROUP_KEYS = ("公司名称", "公司", "stock_name", "公司简称", "名称", "股票名称", "证券简称", "证券名称")
CHART_LONG_LABEL_KEYS = ("指标", "项目", "科目", "metric", "name", "字段")

def pivot_long_chart(chart: Any) -> Any:
    """把「长表」图表拆成「一主体一个系列」。

    跨公司对比的 SQL 常写成 `SELECT 指标, 数值 FROM …` 再加一列公司名：这样图上是
    x=指标、y=通用「数值」列 ⇒ 所有公司挤进同一条系列，图例只能显示「数值」，
    看不出哪条是谁。这里按主体列（公司名称/证券简称…）拆系列，
    x 轴换成指标列；主体只有一个时原样返回（不硬拆，省得把单公司趋势图搅乱）。
    """
    if not isinstance(chart, dict):
        return chart
    rows = chart.get("data")
    if not isinstance(rows, list) or len(rows) < 2 or not isinstance(rows[0], dict):
        return chart
    keys = list(rows[0].keys())
    y_raw = chart.get("y")
    y_list = [str(item) for item in (y_raw if isinstance(y_raw, list) else [y_raw]) if item]
    if not y_list or not all(key in CHART_LONG_VALUE_KEYS for key in y_list):
        return chart
    x_key = str(chart.get("x") or "")
    group_key = next((key for key in keys if key in CHART_LONG_GROUP_KEYS), "")
    label_key = next(
        (key for key in keys if key in CHART_LONG_LABEL_KEYS and key != group_key),
        x_key if x_key and x_key != group_key else "",
    )
    if not group_key or not label_key:
        return chart
    groups: list[str] = []
    by_group: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        group = str(row.get(group_key) or "").strip()
        label = str(row.get(label_key) or "").strip()
        if not group or not label or group in ("None", "nan"):
            continue
        if group not in by_group:
            by_group[group] = {}
            groups.append(group)
        value = row.get(y_list[0])
        if label not in by_group[group] or by_group[group][label] in (None, ""):
            by_group[group][label] = value
    if len(groups) < 2:
        return chart
    labels = list(dict.fromkeys(
        str(row.get(label_key) or "").strip() for row in rows if isinstance(row, dict)
    ))
    labels = [label for label in labels if label]
    if not labels:
        return chart
    pivoted: list[dict[str, Any]] = []
    for label in labels:
        row = {label_key: label}
        for group in groups:
            row[group] = by_group[group].get(label)
        pivoted.append(row)
    localized = dict(chart)
    localized["x"] = label_key
    localized["y"] = groups
    localized["data"] = pivoted
    # series 的 key 要对得上（attach_chart_series_meta 还没跑过的老链路可能带着旧 series）
    if isinstance(localized.get("series"), list):
        localized["series"] = [
            dict(item, key=COLUMN_LABELS.get(str(item.get("key")), str(item.get("key"))))
            for item in localized["series"]
            if str(item.get("key")) in groups
        ] or None
    if localized.get("unit"):
        localized["unit"] = ""  # 数值重新按列取峰值定单位，别沿用长表那个通用单位
    localized.pop("subtitle", None)
    return localized

def chart_unit_kind(unit: str) -> str:
    """把单位归成三类量纲：金额 / 比率 / 股。空单位返回空串（不参与判定）。"""
    if unit == "%":
        return "比率"
    if unit == "股":
        return "股"
    return "金额" if unit else ""

# 身份信息列**不是指标**：`000538` 能被解析成数字、报告期里的年份也能 ⇒
# 「智能·两家对比」的柱状图里冒出过「股票代码（万元）」这种系列（16 条挤一起，
# 谁也看不清）。名称/代码/日期一律不进系列（x 轴不受影响，它本来就走 x_key）。
CHART_IDENTITY_COLUMNS_RE = re.compile(
    r"代码|编号|股票|证券|简称|名称|币种|货币|单位|报告期|日期|期间|截止|年度|季度|"
    r"序号|排名|名次|位次|row_?id|^id$",
    re.I,
)

# 一张柱图最多几条系列？超过就挤成马赛克 ⇒ 宁可不画，让用户看表格。
CHART_MAX_SERIES = 6

def attach_chart_series_meta(chart: Any) -> Any:
    """把图表契约从 {type,x,y,data} 升级成带 series 元数据。

    前端拿到 `series: [{key,label,unit,scale,axis}]` 后，单位徽章、轴标题、
    双轴归属都不用再猜（原来只能把 `y` 拼成轴标题）；`x/y/data` 一个都没动，
    老前端和 `validate_chart_points` 照旧可用。
    """
    if not isinstance(chart, dict):
        return chart
    rows = chart.get("data")
    if not isinstance(rows, list) or not rows:
        return chart
    localized = dict(chart)
    first = rows[0] if isinstance(rows[0], dict) else {}
    keys = list(first.keys())
    x_raw = str(chart.get("x") or "")
    x_label = COLUMN_LABELS.get(x_raw, x_raw)
    y_raw = chart.get("y")
    raw_list = y_raw if isinstance(y_raw, list) else ([y_raw] if y_raw else [])
    ordered = [str(item) for item in raw_list if str(item) in keys]
    ordered += [key for key in keys if key not in ordered and key != x_raw and key != x_label]
    series: list[dict[str, Any]] = []
    for key in ordered:
        if key == x_raw or key == x_label:
            continue
        column = str(key)
        if CHART_IDENTITY_COLUMNS_RE.search(column):
            continue
        values = [row.get(key) for row in rows if isinstance(row, dict)]
        if not any(value is not None and value != "" for value in values):
            continue
        unit, scale = chart_series_unit(column, values)
        series.append({
            "key": COLUMN_LABELS.get(column, column),
            "label": COLUMN_LABELS.get(column, column),
            "unit": unit,
            "scale": scale,
            "axis": "y1" if unit == "%" else "y",
            "rate": unit == "%",
        })
    # 比率列（0~100）和金额列（几十亿）量级差太大，同轴会把比率压成贴地直线
    # ⇒ 只要两种同时存在就把比率挪到右轴（原来要求 ≥2 条比率才挪，一条比率的
    #   对比图——比如「营业收入 + 资产负债率」——就被压在左轴读不出来）。
    rate_count = len([item for item in series if item["axis"] == "y1"])
    if rate_count and rate_count < len(series):
        pass
    else:
        for item in series:
            item["axis"] = "y"
    if series:
        localized["series"] = series
        localized.setdefault("unit", " / ".join(dict.fromkeys(
            item["unit"] for item in series if item["unit"])))
    # 长表（一行一个指标、共用一个数值列）没法按列拆轴：同一条柱子里
    # 「营业收入 218.84 亿」和「资产负债率 25.56 %」量纲差 10^9，
    # 画出来比率贴地、柱子高度也毫无意义 ⇒ 打上冲突标记，由出口决定不画。
    label_column = ""
    for candidate in (x_raw, x_label):
        if candidate and candidate in keys:
            label_column = str(candidate)
            break
    axis_kinds: dict[str, set[str]] = {}
    for item in series:
        column = str(item.get("key") or "")
        kinds: set[str] = set()
        if label_column and column in CHART_LONG_VALUE_KEYS:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw = row.get(column)
                number: float | None = None
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    number = float(raw)
                elif raw is not None:
                    try:
                        number = float(re.sub(r"[^\d.\-+eE]", "", str(raw)) or "")
                    except (TypeError, ValueError):
                        number = None
                if number is None:
                    continue
                kind = chart_unit_kind(chart_series_unit(
                    str(row.get(label_column) or ""), [number])[0])
                if kind:
                    kinds.add(kind)
        else:
            kind = chart_unit_kind(str(item.get("unit") or ""))
            if kind:
                kinds.add(kind)
        axis_kinds.setdefault(str(item.get("axis") or "y"), set()).update(kinds)
    if any(len(group) > 1 for group in axis_kinds.values()):
        localized["unit_conflict"] = True
        localized["gate_note"] = (
            "这些指标单位不一样（金额和百分比），画在一张图里看不清，已按表格给出。"
        )
    elif len(series) > CHART_MAX_SERIES:
        # 系列太多 = 马赛克：一张图塞 16 个指标，柱子细到看不出谁高谁低。
        localized["unit_conflict"] = True
        localized["gate_note"] = (
            f"这张图要塞下 {len(series)} 个指标，柱子挤在一起谁也看不清，已按表格给出。"
            "只想看其中几个的话，把指标名直接写进问题里再问一次。"
        )
    return localized

def localize_financial_chart_with_meta(chart: Any) -> Any:
    """先按**原始英文字段名**定单位，再本地化列名，最后同步 series 的 key。

    单位判据认的是 `revenue_yoy / debt_ratio` 这类字段名；`localize_financial_chart`
    会把列名换成中文，先本地化再定单位就只能靠中文猜了。顺序不能反。
    """
    if not isinstance(chart, dict):
        return chart
    with_meta = attach_chart_series_meta(pivot_long_chart(chart))
    if isinstance(with_meta, dict) and with_meta.pop("unit_conflict", False):
        # 金额和比率混在同一根柱子/同一条轴上，或系列多到挤成一团 ⇒ 这图没有可读性，
        # 宁可不画：表格照给，出口补一句为什么没图（gate_note 留在入参上给调用方读，
        # 见 assistant 的 chart_note）。
        chart["gate_note"] = str(with_meta.get("gate_note") or "")
        return None
    localized = localize_financial_chart(with_meta)
    if isinstance(localized, dict) and isinstance(localized.get("series"), list):
        localized["series"] = [
            dict(item, key=COLUMN_LABELS.get(str(item.get("key")), str(item.get("key"))))
            for item in localized["series"]
        ]
    return localized

def localize_financial_chart(chart: Any) -> Any:
    """将图表坐标轴和数据的英文字段名转换为中文。"""
    if not isinstance(chart, dict):
        return chart
    localized = dict(chart)
    for axis in ("x", "y"):
        value = localized.get(axis)
        if isinstance(value, list):
            localized[axis] = [
                COLUMN_LABELS.get(str(item), str(item))
                for item in value
            ]
        elif value:
            localized[axis] = COLUMN_LABELS.get(str(value), str(value))
    data = localized.get("data")
    if isinstance(data, list):
        localized["data"] = [
            {
                COLUMN_LABELS.get(str(key), str(key)): value
                for key, value in row.items()
            }
            if isinstance(row, dict)
            else row
            for row in data
        ]
    return localized

_MYSQL_SSL_STATE: dict[str, Any] = {"probed": False, "context": None}
_MYSQL_SSL_LOCK = threading.Lock()

def _mysql_base_config(database: bool = True) -> dict[str, Any]:
    """构造不含 SSL 的 MySQL 连接参数。"""
    config = {
        "host": core.CONFIG["mysql_host"],
        "port": core.CONFIG["mysql_port"],
        "user": core.CONFIG["mysql_user"],
        "password": core.CONFIG["mysql_password"],
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
    }
    if database:
        config["database"] = core.CONFIG["mysql_database"]
    return config

def _shared_mysql_ssl_context() -> Any:
    """返回全进程复用的 SSL 上下文；服务器不支持 TLS 时返回 None。

    PyMySQL 没显式传 ssl 时走 PREFERRED 模式，每条连接都要
    ssl.create_default_context()，Windows 上它会枚举系统证书库：
    实测单次 pymysql.connect 314ms，换成共享上下文后 24ms（本机 have_ssl=YES）。
    是不是真支持 TLS 只有问服务器才知道，所以第一次探一下并记下结论。
    """
    state = _MYSQL_SSL_STATE
    if state["probed"]:
        return state["context"]
    with _MYSQL_SSL_LOCK:
        if state["probed"]:
            return state["context"]
        context = None
        try:
            connection = pymysql.connect(**_mysql_base_config())
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SHOW VARIABLES LIKE 'have_ssl'")
                    row = cursor.fetchone()
                    if row and str(row.get("Value") or "").upper() == "YES":
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
            finally:
                connection.close()
        except Exception:
            context = None
        state["context"] = context
        state["probed"] = True
    return state["context"]

def db_config(database: bool = True) -> dict[str, Any]:
    """构造 MySQL 连接参数（带上复用的 SSL 上下文）。"""
    config = _mysql_base_config(database)
    context = _shared_mysql_ssl_context()
    if context is not None:
        config["ssl"] = context
    return config

def query_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """执行只读查询并返回字典列表。"""
    config = db_config()
    read_user = os.getenv("MYSQL_READONLY_USER", "").strip()
    if read_user:
        config["user"] = read_user
        config["password"] = os.getenv("MYSQL_READONLY_PASSWORD", "")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME=10000")
            except Exception:
                pass
            cursor.execute("START TRANSACTION READ ONLY")
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return list(cursor.fetchall())
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()

def format_amount(value: Any, unit_mode: str = "自动") -> str:
    """按用户选择的单位格式化金额。"""
    try:
        number = float(value)
    except Exception:
        return "暂无数据"
    if unit_mode == "元":
        return f"{number:,.2f} 元"
    if unit_mode == "万元":
        return f"{number / 10_000:,.2f} 万元"
    if unit_mode == "亿元":
        return f"{number / 100_000_000:,.2f} 亿元"
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:,.2f} 亿元"
    if abs(number) >= 10_000:
        return f"{number / 10_000:,.2f} 万元"
    return f"{number:,.2f} 元"

def extract_stock_code(question: str) -> str | None:
    """从自然语言问题中提取证券代码。"""
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", question)
    if match:
        return match.group(1)
    seven = re.search(r"(?<!\d)(\d{7})(?!\d)", question)
    if seven:
        digits = seven.group(1)
        return digits[:3] + digits[-3:]
    try:
        matches = []
        for company in get_company_list():
            company_name = company.get("stock_name") or ""
            short_name = re.sub(r"(股份有限公司|有限公司)$", "", company_name)
            if company_name and (
                company_name in question
                or (len(short_name) >= 3 and short_name in question)
            ):
                code = str(company.get("stock_code") or "")
                matches.append((len(short_name), not code.startswith("FILE_"), code))
        if matches:
            matches.sort(reverse=True)
            return matches[0][2]
        # 用户写的是公告里登记的「证券简称」（豪迈科技、中国国贸、首创环保）。
        # 上面那种判据要求「公司名出现在问句里」，简称比全称短，方向反过来就不成立，
        # 于是简称只能掉进 RAG 兜底、答不出数——这里用登记的简称补上这座桥。
        # 全称没命中时才走到这里，所以不会盖掉全称匹配的优先级。
        short_matches = []
        for code, short in company_short_names().items():
            if not code.startswith("FILE_") and short and short in question:
                short_matches.append((len(short), code))
        if short_matches:
            short_matches.sort(reverse=True)
            return short_matches[0][1]
    except Exception:
        pass
    return None

_COMPANY_NAME_MAP_STATE: dict[str, Any] = {"at": 0.0, "map": {}}
_COMPANY_NAME_MAP_TTL = 30.0

# 权威公司主数据（代码 → 规范全称）。只收「有把握、且当前卡成 FILE_ 假主体的
# 知名上市公司」，用来给代码↔名称提供不依赖 OCR 自证的外部参照：
#   · 券商/集团报表正文抓到的持仓股代码串号，能被这道参照挡住或纠正；
#   · 招商/五粮液这类本来连真码都没入库的公司，重导入时能直接归到真实代码，
#     而不是永远停在 FILE_ 碎片上。
# 只补不覆盖（INSERT IGNORE），已入库且名字正确的行不会被改写；对没有把握的代码
# （北交所 8xxxxx、新三板、非公众公司）一律不猜，宁可留 FILE_ 也不塞错码。
# 已经在库里正确入库的公司（达安 002030、云南白药 000538、四川九洲 000801 等）
# 不放进来——它们不缺这份参照，塞进来只会多一处可能记错全名的风险。
COMPANY_MASTER: dict[str, str] = {
    "600999": "招商证券股份有限公司",
    "000858": "宜宾五粮液股份有限公司",
    "000568": "泸州老窖股份有限公司",
    "000625": "重庆长安汽车股份有限公司",
    "002384": "苏州东山精密制造股份有限公司",
    "000100": "TCL科技集团股份有限公司",
    "002508": "杭州老板电器股份有限公司",
    "600252": "广西梧州中恒集团股份有限公司",
    # 300006 曾被历史解析错标成「广西梧州中恒集团股份有限公司」，一度被当成空壳幽灵删掉，
    # 结果「对比中恒集团和莱美药业」从两家塌成一家——真实代码即使没有文档，也是名称反查
    # 的公司全集，只能改名不能删行。名字依据是中恒集团 2026 年半年度报告税收优惠附注原文
    # 「重庆莱美药业股份有限公司（简称"莱美药业"）」，其上市地即创业板 300006。
    "300006": "重庆莱美药业股份有限公司",
}

# 证券简称 → 代码的显式别名（同样只在归一化全等时桥接，绝不子串）。库里有些文档
# 的 company_name 只有证券简称（老板电器、长安Ｂ），对不上全称、又确是有把握的
# 知名公众公司，用这张表精确搭桥归真码；北交所/非公众公司（鑫宇、金鼎）不在此列，
# 保持 FILE_ 诚实待补，不猜。
COMPANY_NAME_ALIASES: dict[str, str] = {
    "老板电器": "002508",
    "长安Ｂ": "000625",
    "长安B": "000625",
}


def seed_company_master(cursor) -> int:
    """把权威主数据幂等写进 1_stock_basic（只补不覆盖），让代码↔名有外部参照。"""
    added = 0
    for code, name in COMPANY_MASTER.items():
        affected = cursor.execute(
            "INSERT IGNORE INTO 1_stock_basic (stock_code, stock_name, industry, list_date) "
            "VALUES (%s, %s, NULL, NULL)",
            (code, name),
        )
        added += int(affected or 0)
    return added


def authoritative_stock_name(stock_code: str, raw_name: str) -> str:
    """写 1_stock_basic 时该用的公司全称：主数据优先，其余一律先清洗 OCR 残留。

    解析不出名字时返回空串（原来会把 "CompanynameinChinese…" 这种带英文模板标签的
    原始 OCR 串直接写进主表），空名会被 company_name_map 过滤掉，不影响代码检索。
    """
    code = str(stock_code or "").strip()
    master_name = COMPANY_MASTER.get(code, "")
    if master_name:
        return master_name
    raw = str(raw_name or "").strip()
    return clean_company_name(raw, code) or raw


def prune_orphan_validation_results(stock_code: str = "", report_period: str = "") -> int:
    """删掉「描述的数字已经不在库里」的校验结论行，返回删除行数。

    校验结论是按 (代码, 报告期) 存的，但**重导只清事实表、不清 `9_validation_results`**，
    于是留下两类陈旧行（实测抓到）：
    - `002030 2030年半年报` ×3 —— 那是解析层年份上限修复**之前**造出来的错期间，事实行早没了；
    - `FILE_4080B9E7 / FILE_E664CE98 2026年半年报` ×3 @2026-09-13 —— 身份已归并/改名后的旧结论。
    它们会让 /api/validation 显示与当前数据矛盾的「异常」，是「看着像 bug」的主要来源。
    判据：四张事实表里一张都没有这个 (代码, 报告期) ⇒ 这条结论描述的数字不存在 ⇒ 陈旧。

    传 (stock_code, report_period) 就把清扫**限定在这一期**：删除单个文档之后用它
    （用户要求「删了文件，对应的数据校验内容也要删」），免得删一份文件顺手把
    别家公司几十周前留下的陈旧结论也清了——那种全库清扫归导入批末负责。
    """
    scope = ""
    params: tuple[Any, ...] = ()
    if stock_code and report_period:
        scope = " AND v.stock_code = %s AND v.report_period = %s"
        params = (stock_code, report_period)
    connection = pymysql.connect(**db_config())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE v FROM `9_validation_results` v
                LEFT JOIN (
                    SELECT stock_code, report_period FROM `2_performance_report`
                    UNION SELECT stock_code, report_period FROM `3_income_statement`
                    UNION SELECT stock_code, report_period FROM `4_balance_sheet`
                    UNION SELECT stock_code, report_period FROM `5_cash_flow_statement`
                ) f ON f.stock_code = v.stock_code AND f.report_period = v.report_period
                WHERE f.stock_code IS NULL
                """ + scope,
                params,
            )
            deleted = int(cursor.rowcount or 0)
        connection.commit()
        return deleted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def prune_orphan_entities() -> list[str]:
    """清掉不再拥有任何文档/事实/切片的 FILE_ 身份行，返回清理的代码。

    一份文件换身份重新入库（FILE_ → 真实代码，或归并到别的主体）之后，旧身份会
    留在 1_stock_basic 里，公司列表凭空多出一个没人认领的鬼影主体。实测抓到
    FILE_F886028B「CompanynameinChinese荣盛石化股份有限公司」：0 文档、0 事实、
    0 切片，只剩 8 行校验结果，却照样显示在 /api/companies。
    只清 FILE_ 开头的代码：真实代码即使暂时空着也合法（权威主数据刚播种、文档还
    没导入），自动删它们会误杀，那类空主体交给审计 SQL 人工过目。
    """
    orphan_codes: list[str] = []
    connection = pymysql.connect(**db_config())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT stock_code FROM `1_stock_basic`
                """
            )
            candidates = [str(row["stock_code"]) for row in cursor.fetchall()]
            for code in [item for item in candidates if item.startswith("FILE_")]:
                cursor.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM `7_document_index` WHERE stock_code=%s) AS docs,
                      (SELECT COUNT(*) FROM `2_performance_report` WHERE stock_code=%s) AS perf,
                      (SELECT COUNT(*) FROM `3_income_statement` WHERE stock_code=%s) AS income,
                      (SELECT COUNT(*) FROM `4_balance_sheet` WHERE stock_code=%s) AS balance,
                      (SELECT COUNT(*) FROM `5_cash_flow_statement` WHERE stock_code=%s) AS cash,
                      (SELECT COUNT(*) FROM `6_rag_chunks` WHERE stock_code=%s) AS chunks
                    """,
                    (code, code, code, code, code, code),
                )
                row = cursor.fetchone() or {}
                if any(int(row.get(key) or 0) for key in ("docs", "perf", "income", "balance", "cash", "chunks")):
                    continue
                for table in ("9_validation_results", "1_stock_basic"):
                    cursor.execute(f"DELETE FROM `{table}` WHERE stock_code=%s", (code,))
                orphan_codes.append(code)
        connection.commit()
    finally:
        connection.close()
    if orphan_codes:
        _COMPANY_NAME_MAP_STATE["map"] = {}
        _COMPANY_NAME_MAP_STATE["at"] = 0.0
        for code in orphan_codes:
            print(f"🧹 清理鬼影主体 {code}（已无文档/事实/切片）")
    return orphan_codes


def company_name_map() -> dict[str, str]:
    """代码 → 公司名，整表一次取回，30 秒内复用。"""
    now = time.time()
    if _COMPANY_NAME_MAP_STATE["map"] and now - _COMPANY_NAME_MAP_STATE["at"] < _COMPANY_NAME_MAP_TTL:
        return _COMPANY_NAME_MAP_STATE["map"]
    rows = query_all(
        "SELECT stock_code, stock_name FROM 1_stock_basic "
        "WHERE stock_name IS NOT NULL AND stock_name <> ''"
    )
    mapping = {str(row["stock_code"]): str(row["stock_name"]) for row in rows if row.get("stock_code")}
    _COMPANY_NAME_MAP_STATE["map"] = mapping
    _COMPANY_NAME_MAP_STATE["at"] = now
    return mapping

def load_company_name(stock_code: str) -> str:
    """根据证券代码查询公司名称。

    原来每条记录各开一条 MySQL 连接（query_all 不做池化），文件库要对 138 份
    文档逐个解析，实测 /api/documents 花 80.7 秒才返回，前端文件面板等于卡死。
    缓存里没有的代码仍会单查一次，所以刚导入的新公司不会被 30 秒缓存挡住。
    """
    code = str(stock_code or "").strip()
    if not code:
        return ""
    name = company_name_map().get(code)
    if name:
        return name
    rows = query_all(
        "SELECT stock_name FROM 1_stock_basic WHERE stock_code=%s LIMIT 1",
        (code,),
    )
    if rows and rows[0].get("stock_name"):
        return rows[0]["stock_name"]
    return code

def resolve_company_name(company_name: str, stock_code: str) -> str:
    """结合证券代码和提取结果确定标准公司名。"""
    if stock_code and not stock_code.startswith("FILE_"):
        try:
            canonical = clean_company_name(load_company_name(stock_code), stock_code)
            if canonical and canonical != stock_code:
                return canonical
        except Exception:
            pass
    return clean_company_name(company_name, stock_code)

def company_names_match(left: str, right: str) -> bool:
    """两个公司名是否指同一家：归一化后相同或互为子串即算一致。

    任一方为空时返回 True（无法判断就不算冲突），避免把代码误退回 FILE_。
    """
    left_clean = clean_company_name(left or "", "")
    right_clean = clean_company_name(right or "", "")
    if not left_clean or not right_clean:
        return True
    if left_clean == right_clean or left_clean in right_clean or right_clean in left_clean:
        return True
    return False

def resolve_stock_code_by_name(company_name: str) -> str:
    """用公司名在已入库的真实主体里唯一反查代码，查不到或歧义返回空。

    只在规范化后全等时命中，绝不做子串包含：被 OCR 砍头的残缺名（如「科技集团
    股份有限公司」）恰好是别家全称的后缀，子串匹配会把不相干公司并进错代码。
    简称对不上全称就返回空，让上层退回 FILE_ 待人工，诚实且不污染。
    例外是显式登记的证券简称别名（见 COMPANY_NAME_ALIASES），同样只在归一化全等
    且目标代码确已入库时生效。
    """
    target = clean_company_name(company_name or "", "")
    if not target:
        return ""
    hits: set[str] = set()
    for row in get_company_list():
        code = str(row.get("stock_code") or "")
        if not code or code.startswith("FILE_"):
            continue
        name = clean_company_name(str(row.get("stock_name") or ""), "")
        if name and name == target:
            hits.add(code)
    if len(hits) == 1:
        return next(iter(hits))
    if not hits:
        alias_code = COMPANY_NAME_ALIASES.get(target)
        if alias_code:
            known = {
                str(row.get("stock_code") or "")
                for row in get_company_list()
                if not str(row.get("stock_code") or "").startswith("FILE_")
            }
            if alias_code in known:
                return alias_code
    return ""

def extract_metrics(question: str) -> list[tuple[str, str, str]]:
    """从问题中提取多个财务指标。"""
    matches: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for keyword, target in sorted(
        METRIC_QUERY_MAP.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if keyword in question and target not in seen:
            matches.append(target)
            seen.add(target)
    return matches

def extract_metric(question: str) -> tuple[str, str, str] | None:
    """从问题中提取单个财务指标。"""
    metrics = extract_metrics(question)
    return metrics[0] if metrics else None

def get_latest_report_period(stock_code: str, table: str) -> str | None:
    """查询指定表的最新报告期。"""
    rows = query_all(
        f"SELECT report_period FROM `{table}` WHERE stock_code=%s ORDER BY report_period DESC LIMIT 1",
        (stock_code,),
    )
    return rows[0]["report_period"] if rows else None

def query_metric_series(
    stock_code: str,
    table: str,
    column: str,
) -> list[dict[str, Any]]:
    """查询某个财务指标的时间序列。"""
    if column == "__debt_ratio__":
        rows = query_all(
            """
            SELECT report_period, total_assets, total_liability
            FROM 4_balance_sheet
            WHERE stock_code=%s
            ORDER BY report_period DESC
            LIMIT 8
            """,
            (stock_code,),
        )
        series = []
        for row in rows:
            assets = row.get("total_assets")
            liabilities = row.get("total_liability")
            if assets in (None, 0) or liabilities is None:
                continue
            series.append(
                {
                    "report_period": row.get("report_period"),
                    "value": float(liabilities) / float(assets) * 100,
                }
            )
        return series
    return query_all(
        f"SELECT report_period, `{column}` AS value FROM `{table}` "
        "WHERE stock_code=%s AND "
        f"`{column}` IS NOT NULL ORDER BY report_period DESC LIMIT 8",
        (stock_code,),
    )

def format_financial_metric(label: str, value: Any, unit_mode: str) -> str:
    """格式化单个指标值。"""
    if value is None:
        return "暂无数据"
    if label in {"资产负债率"} or "率" in label:
        return f"{float(value):.2f}%"
    # 轮廿二：同比也走这条格式化——库里存的数就是百分数（单位:%），
    # 但 label 叫「营收同比」不带「率」字，曾被当成金额拼出「2.95 元」（服务器外验 V4 实测）。
    if "同比" in label or "环比" in label:
        return f"{float(value):.2f}%"
    return format_amount(value, unit_mode)

def format_metric_change(series: list[dict[str, Any]], label: str, unit_mode: str) -> str:
    """生成指标同比或环比变化说明。"""
    if len(series) < 2:
        return ""
    current = float(series[0]["value"])
    previous = float(series[1]["value"])
    if previous == 0:
        return ""
    delta = current - previous
    percentage = delta / abs(previous) * 100
    direction = "增长" if delta > 0 else "下降" if delta < 0 else "持平"
    if label in {"资产负债率"} or "率" in label or "同比" in label or "环比" in label:
        return f"较上一期{direction} {abs(delta):.2f} 个百分点"
    return f"较上一期{direction} {abs(percentage):.2f}%"

def available_metric_labels(stock_code: str) -> tuple[str, list[str]]:
    """返回该公司最新一期实际入库了哪些指标。

    用来把「查不到」说清楚：原来只要问的指标是空值，就回一句
    「当前没有可用于回答的已入库财务数据」，可库里其实有营业收入和利润总额，
    这句话既不准确也不 actionable。
    """
    skip = {"stock_code", "report_period", "id", "created_at", "updated_at"}
    period = ""
    labels: list[str] = []
    for table in (
        "2_performance_report",
        "3_income_statement",
        "4_balance_sheet",
        "5_cash_flow_statement",
    ):
        try:
            rows = query_all(
                f"SELECT * FROM `{table}` WHERE stock_code=%s "
                "ORDER BY report_period DESC LIMIT 1",
                (stock_code,),
            )
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0]
        if not period:
            period = str(row.get("report_period") or "")
        for key, value in row.items():
            if str(key) in skip or value in (None, ""):
                continue
            label = COLUMN_LABELS.get(str(key))
            if label and label not in labels:
                labels.append(label)
    return period, labels

# 「年报」二字是「半年报」的子串 ⇒ 必须用否定后视，否则「2026年半年报」会被当成也要年报，
# 筛出来空集再回退到最新一期，等于把修好的行为又弄反（实测踩过）。
PERIOD_ANNUAL_RE = re.compile(r"(?<!半)年报|年度报告|全年")
PERIOD_SEMI_RE = re.compile(r"半年报|中期报告|中报")
PERIOD_MULTI_RE = re.compile(r"(?:近|最近|过去|前)\s*([一二三四五六七八九十]|\d{1,2})\s*年|历年|各年|多年")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 轮廿二（用户 m02721）：相对时间说法以前整条链路都不认（只认 20\d{2}），
# 「那去年呢」会被当成没点期间、错落最新一期。先折算成具体年份再走原有解析。
RELATIVE_TIME_WORDS = {
    "今年": 0, "本年": 0,
    "去年": -1, "上年": -1, "上一年": -1, "上年度": -1,
    "前年": -2,
}

def expand_relative_time_words(text: str) -> str:
    """把「去年/今年/前年/上一年/上年度」折算成「2025年」这类具体年份。"""
    out = str(text or "")
    now_year = datetime.now().year
    for word in sorted(RELATIVE_TIME_WORDS, key=len, reverse=True):
        out = out.replace(word, f"{now_year + RELATIVE_TIME_WORDS[word]}年")
    return out

def period_phrase_from_text(text: str) -> str:
    """抽出文本里点明的期间字面（「2025年年报」/「2024年」/「近三年」），没点明回空串。"""
    body = re.sub(r"\s+", "", expand_relative_time_words(text))
    year_match = re.search(r"(20\d{2})年?", body)
    if year_match:
        year = year_match.group(1)
        if re.search(r"半年报|中期|中报", body):
            return f"{year}年半年报"
        if PERIOD_ANNUAL_RE.search(body):
            return f"{year}年年报"
        return f"{year}年"
    multi = PERIOD_MULTI_RE.search(body)
    if multi:
        return multi.group(0)
    return ""

def metric_label_from_text(text: str) -> str:
    """抽文本里第一个命中的指标标签，没有回空串（供追问继承拼句用）。"""
    hit = extract_metric(str(text or ""))
    return str(hit[2]) if hit else ""

def report_period_hint(question: str) -> dict[str, Any]:
    """解析题目里的报告期意图：年份、年报/半年报、要不要多年跨度。

    实测（轮八取证）：修复前 `answer_with_structured_data` **完全不看这些**，
    只做 `latest = series[0]` ⇒ 「广州发展最近三年年报的营业收入」回 1 行 2026年半年报；
    「四川九洲2024年年报的净利润」也回 **2026年半年报**（答错期间还不说，比不支持跨度更严重）。
    """
    text = expand_relative_time_words(str(question or ""))
    years = [int(item) for item in re.findall(r"(20\d{2})", text)]
    kinds: set[str] = set()
    if PERIOD_ANNUAL_RE.search(text):
        kinds.add("年报")
    if PERIOD_SEMI_RE.search(text):
        kinds.add("半年报")
    match = PERIOD_MULTI_RE.search(text)
    limit = 0
    if match:
        raw = match.group(1)
        limit = CN_NUM.get(raw, 0) or (int(raw) if raw.isdigit() else 0)
    return {"years": years, "kinds": kinds, "span": bool(match), "limit": limit,
            "explicit": bool(years or kinds)}


def period_kind(label: Any) -> str:
    """把库里的 `report_period` 字符串归到 年报/半年报（「半年报」含「年报」二字，必须先判半年报）。"""
    text = str(label or "")
    if PERIOD_SEMI_RE.search(text):
        return "半年报"
    if PERIOD_ANNUAL_RE.search(text):
        return "年报"
    return ""


def filter_series_by_period(series: list[dict[str, Any]], hint: dict[str, Any]) -> list[dict[str, Any]]:
    """按题目点名的年份/口径筛时间序列；没点名就原样返回。"""
    rows = list(series or [])
    if hint.get("kinds"):
        rows = [row for row in rows if period_kind(row.get("report_period")) in hint["kinds"]]
    if hint.get("years"):
        rows = [row for row in rows
                if any(str(year) in str(row.get("report_period") or "") for year in hint["years"])]
    if hint.get("limit"):
        rows = rows[: hint["limit"]]
    return rows


def answer_with_structured_data(
    question: str,
    unit_mode: str,
    audience: str = "综合分析师",
) -> dict[str, Any] | None:
    """使用本地结构化数据回答问题。"""
    stock_code = extract_stock_code(question)
    metrics = extract_metrics(question)
    if not stock_code or not metrics:
        return None
    company_name = load_company_name(stock_code)
    period_hint = report_period_hint(question)
    metric_results: list[dict[str, Any]] = []
    missing_labels: list[str] = []
    seen_periods: list[str] = []
    for table, column, label in metrics:
        series = query_metric_series(stock_code, table, column)
        if not series:
            continue
        for row in series:
            text = str(row.get("report_period") or "")
            if text and text not in seen_periods:
                seen_periods.append(text)
        # 题目点名的期间必须**尊重**：库里 `report_period` 只有 2026年半年报/2025年年报/2024年年报
        # 三种字符串，`query_metric_series` 的 `ORDER BY report_period DESC` 会把半年报顶到第一，
        # 修复前无脑取 series[0] ⇒ 「2024年年报的净利润」照样回 2026年半年报（答错期间还不说）。
        picked = filter_series_by_period(series, period_hint)
        if not picked:
            missing_labels.append(label)
            continue
        latest = picked[0]
        metric_results.append(
            {
                "label": label,
                "value": latest.get("value"),
                "formatted": format_financial_metric(
                    label,
                    latest.get("value"),
                    unit_mode,
                ),
                "report_period": latest.get("report_period"),
                "series": picked,
                "change": format_metric_change(picked, label, unit_mode),
            }
        )
    if missing_labels and not metric_results:
        asked = "、".join(missing_labels)
        tail = "、".join(sorted(seen_periods, reverse=True)) or "没有任何一期"
        answer = (
            f"{company_name}这一期还没有入库——你要的「{asked}」现在查不到；"
            f"目前能查的是：{tail}。要不要我按这些期次给你算一遍？"
        )
        return {
            "answer": answer,
            "summary": answer,
            "table": [],
            "chart": None,
            "sources": [],
            "metrics": [],
            "key_points": [],
            "warnings": [f"「{asked}」这一期未入库，已列出可查期次"],
            "mode": "local",
            "company_name": company_name,
        }
    if not metric_results:
        period, available = available_metric_labels(stock_code)
        asked = "、".join(label for _, _, label in metrics)
        if available:
            answer = (
                f"{company_name}的「{asked}」尚未入库；"
                f"{period} 已入库的指标有：{'、'.join(available)}。"
            )
        elif period:
            answer = f"{company_name}的「{asked}」尚未入库（{period} 该字段为空）。"
        else:
            answer = f"{company_name}还没有任何已入库的财务数据。"
        return {
            "answer": answer,
            "summary": answer,
            "table": [],
            "chart": None,
            "sources": [],
            "metrics": [],
            "key_points": [],
            "warnings": [f"缺少指标：{asked}"],
            "mode": "local",
            "company_name": company_name,
        }

    latest_period = metric_results[0]["report_period"]
    summary_parts = [
        f"{item['label']}{item['formatted']}"
        for item in metric_results
    ]
    summary = f"{company_name} {latest_period}：" + "，".join(summary_parts) + "。"
    key_points = []
    for item in metric_results:
        if item["change"]:
            key_points.append(f"{item['label']}{item['change']}。")
        else:
            key_points.append(f"当前数据库中只有本期{item['label']}数据，暂无可比期间。")
        if item["label"] == "资产负债率":
            ratio = float(item["value"])
            if ratio < 40:
                key_points.append("负债水平相对稳健，但仍需结合现金流与偿债期限判断。")
            elif ratio < 70:
                key_points.append("负债水平处于中等区间，应关注负债结构和偿债压力。")
            else:
                key_points.append("负债水平较高，偿债风险和融资压力需要重点关注。")
        elif item["label"] in {"净利润", "归母净利润"} and item["value"] is not None:
            if float(item["value"]) < 0:
                key_points.append("当前净利润为负，说明报告期存在经营亏损。")
            else:
                key_points.append("当前净利润为正，具备一定盈利能力。")
    key_points.append(AUDIENCE_FOCUS.get(audience, AUDIENCE_FOCUS["综合分析师"]))

    table: list[dict[str, Any]] = []
    if len(metric_results) == 1:
        item = metric_results[0]
        table = [
            {
                "报告期": row["report_period"],
                item["label"]: format_financial_metric(
                    item["label"],
                    row["value"],
                    unit_mode,
                ),
            }
            for row in item["series"]
        ]
    else:
        table = [
            {
                "指标": item["label"],
                "报告期": item["report_period"],
                "本期数值": item["formatted"],
                "变化说明": item["change"] or "暂无可比期间",
            }
            for item in metric_results
        ]

    chart = None
    chart_note = ""
    wants_chart = any(word in question for word in CHART_WORDS)
    if wants_chart and len(metric_results) == 1:
        series = list(reversed(metric_results[0]["series"]))
        if len(series) >= 2:
            chart = {
                "type": "line" if any(word in question for word in ("趋势", "走势")) else "bar",
                "title": f"{company_name}{metric_results[0]['label']}变化",
                "data": series,
                "x": "report_period",
                "y": "value",
                "colors": ["#2dd4bf", "#60a5fa", "#f0b35c", "#f472b6", "#a78bfa"],
            }
        else:
            chart_note = "当前可比期间不足，暂不生成趋势图。"
    elif wants_chart and len(metric_results) >= 2:
        chart_data = [
            {"指标": item["label"], "数值": item["value"]}
            for item in metric_results
            if item["value"] is not None
        ]
        if len(chart_data) >= 2:
            # 金额（亿/万元）和比率（%）不能塞进同一条系列同一根轴：218 亿和 25% 画一起
            # 就是「数值 vs 数值」那种看不出东西的图。单位不一致就不画，改用一句话说明。
            unit_kinds = {chart_series_unit(str(row.get("指标") or ""), [row.get("数值")])[0] for row in chart_data}
            if len(unit_kinds) > 1:
                chart_note = "这些指标单位不一样（金额和百分比），画在一张图里看不清，已按表格给出。"
            else:
                chart = {
                    "type": "pie" if any(word in question for word in ("占比", "结构")) else "bar",
                    "title": f"{company_name}关键指标对比",
                    "data": chart_data,
                    "x": "指标",
                    "y": "数值",
                    "colors": ["#2dd4bf", "#f0b35c", "#60a5fa", "#f472b6", "#a78bfa"],
                }

    return {
        "answer": summary,
        "summary": summary,
        "table": table,
        "chart": chart,
        "chart_note": chart_note,
        "sources": [],
        "metrics": [
            {
                "label": item["label"],
                "value": item["formatted"],
                "change": item["change"],
            }
            for item in metric_results
        ],
        "key_points": key_points,
        "warnings": [],
        "mode": "local",
        "company_name": company_name,
        "audience": audience,
    }

def get_database_summary() -> dict[str, Any]:
    """返回首页统计卡片和数据库概览。"""
    rows = query_all(
        """
        SELECT
          (SELECT COUNT(*) FROM 1_stock_basic) AS companies,
          (SELECT COUNT(*) FROM 2_performance_report) AS performance,
          (SELECT COUNT(*) FROM 3_income_statement) AS income,
          (SELECT COUNT(*) FROM 4_balance_sheet) AS balance,
          (SELECT COUNT(*) FROM 5_cash_flow_statement) AS cashflow
        """
    )
    result = rows[0] if rows else {}
    result["cards"] = [
        {
            "key": "companies",
            "title": "公司基础信息",
            "value": int(result.get("companies") or 0),
            "unit": "家公司",
            "description": "包含股票代码、公司名称、所属行业和上市日期",
            "usage": "用于选择分析对象并关联财报数据",
        },
        {
            "key": "performance",
            "title": "业绩概览",
            "value": int(result.get("performance") or 0),
            "unit": "条业绩记录",
            "description": "包含营业收入、净利润、每股收益和同比变化",
            "usage": "用于判断盈利水平和增长趋势",
        },
        {
            "key": "income",
            "title": "利润表",
            "value": int(result.get("income") or 0),
            "unit": "条记录",
            "description": "包含收入、成本、利润和每股收益明细",
            "usage": "用于分析盈利能力、成本结构和利润来源",
        },
        {
            "key": "balance",
            "title": "资产负债表",
            "value": int(result.get("balance") or 0),
            "unit": "条记录",
            "description": "包含资产、负债、股东权益和流动性数据",
            "usage": "用于判断偿债压力、资产结构和财务风险",
        },
        {
            "key": "cashflow",
            "title": "现金流量表",
            "value": int(result.get("cashflow") or 0),
            "unit": "条记录",
            "description": "包含经营、投资、筹资现金流和现金净增加额",
            "usage": "用于分析现金创造能力、资金去向和经营质量",
        },
    ]
    result["rag_chunks"] = len(RAG_CHUNKS)
    # 概览条要用的两个真数字：入库文件份数、有数据的报告期（跨表去重取前 3）
    try:
        result["documents"] = len(load_document_index())
    except Exception as error:  # noqa: BLE001 - 概览不许把接口拖崩
        print(f"[概览] 文件份数读取失败: {error}")
        result["documents"] = 0
    try:
        period_rows = query_all(
            """
            SELECT report_period FROM (
              SELECT report_period FROM 2_performance_report
              UNION SELECT report_period FROM 3_income_statement
              UNION SELECT report_period FROM 4_balance_sheet
            ) t WHERE report_period IS NOT NULL AND report_period <> ''
            GROUP BY report_period ORDER BY report_period DESC LIMIT 3
            """
        )
        result["periods"] = [row["report_period"] for row in period_rows]
    except Exception as error:  # noqa: BLE001
        print(f"[概览] 报告期读取失败: {error}")
        result["periods"] = []
    return result

def get_data_quality_report() -> dict[str, Any]:
    """返回结构化财报数据完整率和基础一致性报告。"""
    companies = query_all(
        """
        SELECT stock_code, stock_name
        FROM 1_stock_basic
        WHERE stock_code IS NOT NULL
          AND stock_code <> ''
          AND stock_code NOT LIKE 'FILE_%'
        ORDER BY stock_code
        """
    )
    company_map = {
        str(item.get("stock_code") or ""): display_company_name(
            str(item.get("stock_code") or ""),
            str(item.get("stock_name") or ""),
        )
        for item in companies
    }
    table_specs = (
        ("2_performance_report", "业绩概览"),
        ("3_income_statement", "利润表"),
        ("4_balance_sheet", "资产负债表"),
        ("5_cash_flow_statement", "现金流量表"),
    )
    coverage: dict[str, set[str]] = {}
    for table, _ in table_specs:
        rows = query_all(
            f"SELECT DISTINCT stock_code FROM `{table}` "
            "WHERE stock_code IS NOT NULL AND stock_code <> ''"
        )
        coverage[table] = {
            str(item.get("stock_code") or "") for item in rows
        }
    all_codes = set(company_map)
    missing_by_table = {
        title: sorted(all_codes - coverage[table])
        for table, title in table_specs
    }
    incomplete = []
    for code in sorted(all_codes):
        missing = [
            title
            for table, title in table_specs
            if code not in coverage[table]
        ]
        if missing:
            incomplete.append(
                {
                    "stock_code": code,
                    "company_name": company_map.get(code, code),
                    "missing": missing,
                }
            )
    orphan_records = {}
    for table, title in table_specs:
        orphan_records[title] = len(coverage[table] - all_codes)
    document_rows = load_document_index()
    missing_documents = 0
    for item in document_rows:
        file_path = Path(str(item.get("file_path") or ""))
        if not file_path.exists():
            missing_documents += 1
    return {
        "company_count": len(all_codes),
        "report_coverage": {
            title: {
                "covered": len(coverage[table] & all_codes),
                "total": len(all_codes),
                "percent": round(
                    len(coverage[table] & all_codes)
                    / max(1, len(all_codes))
                    * 100,
                    2,
                ),
            }
            for table, title in table_specs
        },
        "missing_by_table": missing_by_table,
        "incomplete_companies": incomplete,
        "orphan_records": orphan_records,
        "documents": {
            "total": len(document_rows),
            "missing_files": missing_documents,
        },
        "vector_index": load_vector_index_status(),
    }

def company_coverage_report() -> dict[str, Any]:
    """数据覆盖体检：按公司摊开「几份原文、哪些报告期、四张表各几行、有没有缺口」。

    为什么需要这一页：库是用户自己边删边导的，「某家只导进了摘要级小文件」和
    「有原文却没解析出数」这两种缺口在文件库里看不出来，等提问答不上来才暴露
    （实测：600310 曾只有 0.15MB 摘要，其余 53 家都是全文）。这里一次算完，
    前端只管摆，不再让「有没有数据」变成口头猜。
    """
    companies = {
        str(item.get("stock_code") or ""): display_company_name(
            str(item.get("stock_code") or ""),
            str(item.get("stock_name") or ""),
        )
        for item in query_all(
            """
            SELECT stock_code, stock_name
            FROM 1_stock_basic
            WHERE stock_code <> '' AND stock_code NOT LIKE 'FILE_%'
            """
        )
    }
    table_specs = (
        ("2_performance_report", "业绩概览"),
        ("3_income_statement", "利润表"),
        ("4_balance_sheet", "资产负债表"),
        ("5_cash_flow_statement", "现金流量表"),
    )
    periods: dict[str, set[str]] = {}
    table_rows: dict[str, dict[str, int]] = {}
    for table, title in table_specs:
        for item in query_all(
            f"SELECT stock_code, report_period, COUNT(*) AS n FROM `{table}` "
            "WHERE stock_code <> '' GROUP BY stock_code, report_period"
        ):
            code = str(item.get("stock_code") or "")
            periods.setdefault(code, set()).add(str(item.get("report_period") or ""))
            bucket = table_rows.setdefault(code, {})
            bucket[title] = bucket.get(title, 0) + int(item.get("n") or 0)
    documents: dict[str, list[dict[str, Any]]] = {}
    for item in load_document_index():
        code = str(item.get("stock_code") or "").strip()
        if not code or code.startswith("FILE_"):
            continue
        documents.setdefault(code, []).append(item)

    rows: list[dict[str, Any]] = []
    registry_only = 0
    for code in sorted(set(companies) | set(documents) | set(periods)):
        docs = documents.get(code, [])
        own_periods = sorted(p for p in periods.get(code, set()) if p)
        if not docs and not own_periods:
            # 档案里挂着名字、库里既没原文也没数据 = 压根没导入过。
            # 用户明确点过这条（000568 泸州老窖）：这种公司摆在体检页里
            # 只会把真缺口淹掉，「没有财报原文」对它们也毫无意义。
            registry_only += 1
            continue
        largest_mb = round(
            max((float(item.get("file_size") or 0) for item in docs), default=0.0)
            / 1024
            / 1024,
            2,
        )
        flags: list[dict[str, str]] = []
        if code not in companies:
            flags.append({"level": "bad", "text": "股票档案缺失（有数据或文件，但档案里没有这家）"})
        if not docs:
            flags.append({"level": "bad", "text": "库里已没有它的原文（财务数据还在，但引用原文、答风险的题做不了）"})
        elif not own_periods:
            flags.append({"level": "bad", "text": "有原文但没解析出财务数据（要重导或查解析）"})
        if docs and largest_mb < 1.0:
            flags.append({"level": "warn", "text": f"原文只有 {largest_mb}MB，疑似摘要级文件而非年报全文"})
        if len(own_periods) == 1:
            flags.append({"level": "warn", "text": "只有一个报告期，趋势/同比类问题答不了"})
        rows.append(
            {
                "stock_code": code,
                "company_name": companies.get(code, code),
                "document_count": len(docs),
                "largest_document_mb": largest_mb,
                "periods": own_periods,
                "tables": table_rows.get(code, {}),
                "flags": flags,
            }
        )
    all_periods = sorted({p for item in rows for p in item["periods"]})
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": {
            "companies": len(companies),
            "in_library": len(rows),
            "registry_only": registry_only,
            "documents": sum(len(item) for item in documents.values()),
            "with_data": sum(1 for item in rows if item["periods"]),
            "with_gaps": sum(1 for item in rows if item["flags"]),
            "periods": all_periods,
        },
        "rows": sorted(rows, key=lambda item: (not item["flags"], item["company_name"])),
    }


def get_validation_report() -> list[dict[str, Any]]:
    """返回财务校验报告。"""
    rows = query_all(
        """
        SELECT v.stock_code, s.stock_name, v.report_period,
               v.rule_name, v.status, v.message, v.created_at
        FROM 9_validation_results v
        LEFT JOIN 1_stock_basic s ON s.stock_code=v.stock_code
        ORDER BY v.created_at DESC, v.id DESC
        LIMIT 300
        """
    )
    status_labels = {
        "通过": "通过",
        "异常": "异常",
        "待补充": "待补充",
        "pass": "通过",
        "failed": "异常",
        "pending": "待补充",
    }
    return [
        {
            "公司": display_company_name(
                str(item.get("stock_code") or ""),
                str(item.get("stock_name") or ""),
            ),
            "报告期": item.get("report_period") or "",
            "校验项": item.get("rule_name") or "",
            "结果": status_labels.get(str(item.get("status")), str(item.get("status") or "")),
            "说明": item.get("message") or "",
        }
        for item in rows
    ]

def get_company_list() -> list[dict[str, Any]]:
    """返回公司基础信息列表。"""
    return query_all(
        """
        SELECT stock_code, stock_name
        FROM 1_stock_basic
        WHERE stock_name IS NOT NULL AND stock_name <> ''
        ORDER BY stock_code
        """
    )

def load_document_index() -> list[dict[str, Any]]:
    """从数据库或 JSON 加载文件库索引（进程内缓存，写入时失效）。

    界面上一轮解析公司名会调它几十次，每次都新开一条 MySQL 连接全表查：
    数据质量报告实测 11.6 秒。所有写路径都会走 save_document_index，
    所以缓存只在那里失效。
    """
    cached = _DOCUMENT_INDEX_CACHE["rows"]
    if cached is not None:
        return list(cached)
    try:
        connection = pymysql.connect(**db_config())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, stock_code, company_name, report_period,
                           file_name, file_path, file_type, file_size,
                           file_hash, managed_copy, imported_at
                    FROM 7_document_index
                    ORDER BY imported_at DESC
                    """
                )
                rows = list(cursor.fetchall())
                if rows:
                    for item in rows:
                        item["managed_copy"] = bool(item.get("managed_copy"))
                    _DOCUMENT_INDEX_CACHE["rows"] = rows
                    return list(rows)
        finally:
            connection.close()
    except Exception:
        pass
    if not DOCUMENT_INDEX_PATH.exists():
        return []
    try:
        entries = json.loads(DOCUMENT_INDEX_PATH.read_text(encoding="utf-8"))
        changed = False
        for item in entries:
            path = Path(item.get("file_path", ""))
            if not item.get("file_hash") and path.exists():
                item["file_hash"] = sha256_file(path)
                changed = True
            if "managed_copy" not in item:
                item["managed_copy"] = is_managed_copy(path)
                changed = True
        if changed:
            save_document_index(entries)
        else:
            _DOCUMENT_INDEX_CACHE["rows"] = entries
        return list(entries)
    except Exception:
        return []

def save_document_index(entries: list[dict[str, Any]]) -> None:
    """将文件库索引保存到数据库和 JSON。"""
    _DOCUMENT_INDEX_CACHE["rows"] = None  # 写入即失效，界面导入后立刻能看到新文件
    try:
        core.MySQL().init_database()
        connection = pymysql.connect(**db_config())
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM 7_document_index")
                if entries:
                    cursor.executemany(
                        """
                        INSERT INTO 7_document_index (
                          id, stock_code, company_name, report_period,
                          file_name, file_path, file_type, file_size,
                          file_hash, managed_copy, imported_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                item.get("id"),
                                item.get("stock_code"),
                                item.get("company_name"),
                                item.get("report_period"),
                                item.get("file_name"),
                                item.get("file_path"),
                                item.get("file_type"),
                                item.get("file_size", 0),
                                item.get("file_hash"),
                                1 if item.get("managed_copy") else 0,
                                item.get("imported_at"),
                            )
                            for item in entries
                        ],
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception as error:
        # 2026-10-28 静态审计（类别2）：文档索引写库失败原本静默吞掉 ⇒ 只加日志，行为不变。
        print(f"[文档索引] 写库失败（不影响主流程）：{type(error).__name__}: {str(error)[:160]}")
    try:
        write_text_atomically(
            DOCUMENT_INDEX_PATH,
            json.dumps(entries, ensure_ascii=False, indent=2),
        )
    except Exception as error:
        print(
            f"[文档索引] 写文件失败 {DOCUMENT_INDEX_PATH}（不影响主流程）："
            f"{type(error).__name__}: {str(error)[:160]}"
        )

def sha256_file(path: Path) -> str:
    """计算文件的 SHA256 摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def is_managed_copy(path: Path) -> bool:
    """判断文件是否位于系统管理目录。"""
    try:
        path.resolve().relative_to((BASE_DIR / "storage/uploads/web").resolve())
        return True
    except ValueError:
        return False

def path_referenced_by_document(path: Path) -> bool:
    """盘上这个文件是不是「某条文档记录的原文」。

    实测事故（2026-10-27）：一次 `POST /api/import/start {}` 之后，`7_document_index`
    里 94 行有 **25 行的 `file_path` 指向盘上已经不存在的文件**。原因就在
    `is_managed_copy()` 只看路径不看库行：
      · 空 `local_dir` 经 `Path("").expanduser()` 变成服务进程自己的当前目录（=项目根），
        `rglob` 于是把 `storage/uploads/web/<ts>/*.pdf` 里**已经在库里**的原文全扫成待导入；
      · `process_one()` 对每个文件 `find_duplicate(file_hash)` 必然命中（它就是自己那条记录），
        走「重复」分支 ⇒ `if is_managed_copy(path): path.unlink()` ⇒ **把文档行指向的原文删了**；
      · 而这 25 行在库里的 `managed_copy` 全是 0 —— 也就是说「系统认不认它是临时副本」
        跟「库里有没有人在引用它」本来就是两件事。
    后果边界：事实表、切片、向量都还在（`2_performance_report=52`、`6_rag_chunks=22431`），
    坏掉的是 `/api/documents/open`（点开读不到）和「以后重导/重 OCR 这些文档的能力」。
    所以删盘上文件之前必须问一次库里：有任何一行引用它，就不是可删的临时副本。
    """
    resolved = str(path.resolve())
    rows = query_all(
        "SELECT 1 AS hit FROM 7_document_index WHERE file_path=%s LIMIT 1",
        (resolved,),
    )
    if rows:
        return True
    # 登记时写的不一定是 resolve() 后的形态，再用原始拼法查一次，宁可多认不可漏认。
    return bool(
        query_all(
            "SELECT 1 AS hit FROM 7_document_index WHERE file_path=%s LIMIT 1",
            (str(path),),
        )
    )

def find_duplicate(file_hash: str) -> dict[str, Any] | None:
    """根据文件摘要查找重复文档。"""
    return next(
        (
            item
            for item in load_document_index()
            if item.get("file_hash") == file_hash
        ),
        None,
    )

def register_document(
    path: Path,
    *,
    file_hash: str | None = None,
    managed_copy: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """将文件登记到文件库索引。"""
    metadata = metadata or {}
    stock_code = (
        str(metadata.get("stock_code") or "").strip()
        or core.extract_stock_code_from_name(path)
    )
    company_name = resolve_company_name(
        str(metadata.get("company_name") or ""),
        stock_code,
    )
    if not company_name:
        company_name = resolve_company_name(load_company_name(stock_code), stock_code)
    if not company_name:
        company_name = path.stem if not is_opaque_file_stem(path.stem) else "未识别公司"
    report_period = (
        str(metadata.get("report_period") or "").strip()
        or report_period_from_metadata(metadata)
        or (
            get_latest_report_period(stock_code, "2_performance_report")
            if not stock_code.startswith("FILE_")
            else ""
        )
        or ""
    )
    report_label = str(metadata.get("report_label") or "").strip()
    entry = {
        "id": uuid.uuid4().hex,
        "stock_code": stock_code,
        "company_name": company_name,
        "report_period": report_period,
        "file_name": build_document_display_name(
            path,
            stock_code,
            company_name,
            report_period,
            report_label,
        ),
        "file_path": str(path.resolve()),
        "file_type": path.suffix.lower().lstrip("."),
        "file_size": path.stat().st_size if path.exists() else 0,
        "file_hash": file_hash or sha256_file(path),
        "managed_copy": is_managed_copy(path)
        if managed_copy is None
        else managed_copy,
        "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with DOCUMENT_INDEX_LOCK:
        entries = load_document_index()
        entries = [
            item
            for item in entries
            if Path(item.get("file_path", "")).resolve() != path.resolve()
        ]
        entries.append(entry)
        save_document_index(entries)

def search_documents(query: str = "") -> list[dict[str, Any]]:
    """返回按公司分组的文件库数据。"""
    query = query.strip().lower()
    grouped: dict[str, dict[str, Any]] = {}
    for item in load_document_index():
        haystack = (
            f"{item.get('company_name', '')} {item.get('stock_code', '')} "
            f"{item.get('file_name', '')}"
        ).lower()
        if query and query not in haystack:
            continue
        stock_code = str(item.get("stock_code") or "").strip()
        company_name = resolve_company_name(
            str(item.get("company_name") or ""),
            stock_code,
        ) or str(item.get("company_name") or "").strip() or "未知公司"
        key = company_name.casefold()
        group = grouped.setdefault(
            key,
            {
                "stock_code": stock_code,
                "company_name": company_name,
                "documents": [],
            },
        )
        if (
            str(group.get("stock_code") or "").startswith("FILE_")
            and stock_code
            and not stock_code.startswith("FILE_")
        ):
            group["stock_code"] = stock_code
        group["documents"].append(item)
    return list(grouped.values())

def reimport_report_period_documents(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """把同一 (公司, 报告期) 剩下的文档重新解析回填一次（delete_document 出锁后调用）。

    为什么需要：四张事实表是按 `(stock_code, report_period)` 整行删的，而库里实测有
    36 组「同一公司同一期存在多份文件」（摘要 + 全文、或两个版本）。删掉其中一份，
    另一份贡献的数值也被一起带走，可它的切片还在 ⇒ Text-to-SQL 查不到数、RAG 却能
    引原文，表现就是「同一个问题一次有一次没有」。补法只有一条：让留下来的那份
    重新走一遍入库链，库里剩下的就是它该贡献的东西。

    必须在 `DOCUMENT_INDEX_LOCK` **外面**调用：`register_document`（:4792）与
    `build_rag_chunks_from_file`（:5436）都要拿同一批锁，而 `config.py:61/:63` 是
    非重入的 `threading.Lock()`，锁内调用会当场死锁。
    """
    rebuilt: list[str] = []
    failed: list[str] = []
    if not entries:
        return {"rebuilt": rebuilt, "failed": failed}
    database: MySQL | None = None
    # 小的先、大的后：同批比较会保留信息更多的一方，让最完整那份最后写，
    # 最终留下的数值就跟「界面上还剩哪几份」一致。
    for item in sorted(entries, key=lambda one: int(one.get("file_size") or 0)):
        path = Path(str(item.get("file_path") or ""))
        if not path.exists():
            print(f"[删除重建] {path.name} 原文已不在磁盘，这一期只能少一份")
            failed.append(f"{path.name}（原文已不在磁盘）")
            continue
        if database is None:
            # 只有真的要重灌时才建连接、过一遍幂等 DDL：全是缺文件的入参不该碰库。
            database = MySQL()
            database.init_database()
        try:
            metadata = process_financial_document(path, database) or {}
            if metadata.get("quarantined"):
                # 与 run_import_job 同口径：闸门拦下的文档不登记、不做切片。
                print(f"[删除重建] {path.name} 被入库闸门拦下，未回填")
                failed.append(f"{path.name}（入库闸门拦下）")
                continue
            register_document(
                path,
                file_hash=str(item.get("file_hash") or "") or sha256_file(path),
                managed_copy=bool(item.get("managed_copy")),
                metadata=metadata,
            )
            build_rag_chunks_from_file(path)
            rebuilt.append(path.name)
        except Exception as exc:
            print(f"[删除重建] {path.name} 重解析失败：{type(exc).__name__}: {str(exc)[:160]}")
            failed.append(f"{path.name}（{type(exc).__name__}）")
    return {"rebuilt": rebuilt, "failed": failed}

def delete_all_documents() -> dict[str, Any]:
    """清空文件库：文档索引、四张事实表、切片、校验结论、向量一次清干净。

    刻意**不删磁盘上的原文**：`storage/uploads/web` 里躺着的可能正是某条记录唯一的
    原文（实测被误删过 25 份，见 :5598 注释）。原文留着，用户之后指同一目录重新导入
    时 `find_duplicate` 已查不到记录，会正常重新入库。
    """
    with DOCUMENT_INDEX_LOCK:
        entries = load_document_index()
        save_document_index([])
    with RAG_CHUNKS_LOCK:
        RAG_CHUNKS[:] = []
        save_rag_chunks()
    rows: dict[str, int] = {}
    try:
        connection = pymysql.connect(**db_config())
        try:
            with connection.cursor() as cursor:
                for table in (
                    "2_performance_report",
                    "3_income_statement",
                    "4_balance_sheet",
                    "5_cash_flow_statement",
                    "6_rag_chunks",
                    "9_validation_results",
                ):
                    cursor.execute(f"DELETE FROM `{table}`")
                    rows[table] = int(cursor.rowcount or 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception as exc:
        print(f"[全部删除] 数据库清理失败（文档索引与切片已清）：{type(exc).__name__}: {str(exc)[:160]}")
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:160]}", "documents": len(entries)}
    with _VECTOR_INDEX_LOCK:
        try:
            vector_store.reset_collection()
        except Exception as exc:
            print(f"[全部删除] 向量集合清空失败（其余已清）：{type(exc).__name__}: {str(exc)[:160]}")
            rows["qdrant_error"] = -1
    print(f"[全部删除] 已清 {len(entries)} 份文档，删表行数 {rows}")
    return {"status": "cleared", "documents": len(entries), "rows": rows}

def delete_document(document_id: str) -> dict[str, str]:
    """删除文件及其关联索引和 RAG 数据；这一期若还剩别的文件，用留下来的重新回填。"""
    survivors: list[dict[str, Any]] = []
    with DOCUMENT_INDEX_LOCK:
        entries = load_document_index()
        target = next((item for item in entries if item["id"] == document_id), None)
        if target is None:
            raise KeyError("文档不存在")
        path = Path(target.get("file_path", ""))
        if target.get("managed_copy") and path.exists():
            path.unlink()
        stock_code = target.get("stock_code")
        report_period = target.get("report_period")
        # 同一 (公司, 报告期) 还剩下几份文件？库里实测 36 组这种情况（摘要 + 全文、多版本）。
        # 事实表是按 (代码, 期间) 整行删的，剩下那几份贡献的数值也会被一起带走，
        # 所以出锁后要拿它们重新回填一次。
        if stock_code and report_period:
            survivors = [
                item
                for item in entries
                if item is not target
                and str(item.get("stock_code") or "") == str(stock_code)
                and str(item.get("report_period") or "") == str(report_period)
            ]
        if stock_code and report_period:
            connection = pymysql.connect(**db_config())
            try:
                with connection.cursor() as cursor:
                    for table in (
                        "2_performance_report",
                        "3_income_statement",
                        "4_balance_sheet",
                        "5_cash_flow_statement",
                    ):
                        cursor.execute(
                            f"DELETE FROM `{table}` WHERE stock_code=%s AND report_period=%s",
                            (stock_code, report_period),
                        )
                    if not survivors:
                        # 这一期在库里已经一份不剩 ⇒ 描述这些数字的校验结论必须跟着作废。
                        # 还有剩就绝不能删：重灌那份自己会 DELETE+INSERT，而且它「保留原行」的
                        # 分支是故意留着旧结论的（实测先删会造出「有数字没校验」的空档）。
                        cursor.execute(
                            "DELETE FROM 9_validation_results "
                            "WHERE stock_code=%s AND report_period=%s",
                            (stock_code, report_period),
                        )
                connection.commit()
            finally:
                connection.close()
        save_document_index([item for item in entries if item["id"] != document_id])
        target_path = str(path.resolve()) if str(path) else ""
        # 只按路径删：同名文件可以来自不同目录（实测 920833_重庆美心翼申… 一名挂 22 条路径 970 块），
        # 之前带 file_name 条件会让「删一份」连坐删掉其他文件的向量与块。
        chunk_paths = {item for item in (target_path, str(target.get("file_path") or "")) if item}
        with RAG_CHUNKS_LOCK:
            RAG_CHUNKS[:] = [
                chunk for chunk in RAG_CHUNKS if chunk.get("file_path") not in chunk_paths
            ]
            save_rag_chunks()
        try:
            vector_store.delete_file_chunks(target_path)
        except Exception as exc:
            print(f"[删除] 向量点未清理 {target_path}：{type(exc).__name__}: {str(exc)[:160]}")
        try:
            connection = pymysql.connect(**db_config())
            try:
                with connection.cursor() as cursor:
                    placeholders = ", ".join(["%s"] * len(chunk_paths)) or "%s"
                    cursor.execute(
                        f"DELETE FROM 6_rag_chunks WHERE file_path IN ({placeholders})",
                        tuple(chunk_paths) or (target_path,),
                    )
                connection.commit()
            finally:
                connection.close()
        except Exception as exc:
            print(f"[删除] 6_rag_chunks 未清理：{type(exc).__name__}: {str(exc)[:160]}")
    # 出锁后重建：register_document(:4792) 与 build_rag_chunks_from_file(:5436) 都要拿
    # config.py:61/:63 那两把锁，而它们是**非重入** threading.Lock，锁内调用会当场死锁。
    result: dict[str, str] = {"status": "deleted"}
    if survivors:
        stats = reimport_report_period_documents(survivors)
        if stats["rebuilt"]:
            result["rebuilt"] = "、".join(stats["rebuilt"])
        if stats["failed"]:
            result["rebuild_failed"] = "、".join(stats["failed"])
    # 用户要求（m02806）：删了文件，这一期对应的「数据校验」内容也要跟着走。
    # 判据用数字还在不在，不用文件在不在：重灌后数字没了 ⇒ 结论陈旧 ⇒ 删掉；
    # 数字还在 ⇒ 那条结论描述的就是当前数据 ⇒ 留着（否则校验页会凭空少一排绿灯）。
    if stock_code and report_period:
        try:
            stale = prune_orphan_validation_results(str(stock_code), str(report_period))
            if stale:
                result["validation_dropped"] = str(stale)
                print(f"[删除] 同步清掉 {stock_code} {report_period} 的陈旧校验结论 {stale} 行")
        except Exception as exc:
            print(f"[删除] 校验结论未清理 {stock_code} {report_period}：{type(exc).__name__}: {str(exc)[:160]}")
    return result

_SHORT_NAME_ALIAS_CACHE: dict[str, Any] = {}


def company_short_names() -> dict[str, str]:
    """股票代码 -> 证券简称（公告里登记的叫法，如「豪迈科技」「首创环保」）。

    用户只写简称时以前必须写全称或六位代码才查得到数——1_stock_basic 只有工商全称，
    简称只散落在公告正文与文件名里。这里一次性扫出来并缓存（签名 = 有简称的码数 + 码数，
    新公司入库后签名变化会自动重建）。
    """
    try:
        sig = query_all(
            "SELECT COUNT(*) AS c, "
            "(SELECT COUNT(*) FROM `1_stock_basic`) AS t "
            "FROM `1_stock_basic` WHERE alias IS NOT NULL AND alias <> ''"
        )[0]
        signature = (int(sig["c"]), int(sig["t"]))
    except Exception:
        return {}
    if _SHORT_NAME_ALIAS_CACHE.get("signature") == signature:
        return _SHORT_NAME_ALIAS_CACHE["value"]
    rows = query_all("SELECT stock_code, alias FROM `1_stock_basic` WHERE alias <> ''")
    table = {str(r["stock_code"]): str(r["alias"]) for r in rows if r["alias"]}
    _SHORT_NAME_ALIAS_CACHE["signature"] = signature
    _SHORT_NAME_ALIAS_CACHE["value"] = table
    return table


def registered_company_aliases(company_name: str) -> list[str]:
    """按工商全称反查它登记过的证券简称（供 assistant 的别名候选池使用）。"""
    name = re.sub(r"\s+", "", str(company_name or ""))
    if not name:
        return []
    codes = {
        str(r["stock_code"])
        for r in query_all("SELECT stock_code FROM `1_stock_basic` WHERE stock_name = %s", (name,))
    }
    table = company_short_names()
    return sorted({table[c] for c in codes if table.get(c)})


def extract_company_short_name(text: str, full_name: str) -> str:
    """从公告文本/文件名里抽出属于这家公司的证券简称。

    只认三种「股票/证券/公司简称」标签后的值，以及文件名前缀 `简称：正文`；
    并且要求抽出来的字按顺序能在本公司全称里读到（子序列），否则多半是切片里
    顺带提到的别家公司（实测 600999 招商证券 会被抽成「美心翼申」）。
    标签前 14 字里出现「变更」的要跳过——那是「变更前的股票简称（如有）」。
    """
    full = re.sub(r"\s+", "", str(full_name or ""))
    compact = re.sub(r"[\s\u3000]+", "", str(text or ""))
    boundary = (
        "股票代码", "证券代码", "公司代码", "代码", "公告编号", "公告", "披露", "上市",
        "变更", "注册", "办公", "住所", "序号", "项目", "如有", "简称", "交易所", "报告", "日期",
        "：", ":", "、", "。", "（",
    )
    metadata = re.compile(
        r"股票|证券|简称|代码|公告|披露|变更|注册|办公|住所|序号|项目|如有|交易所|报告|日期|累计|公允|价值|审计|律师|监事|董事"
    )
    votes: dict[str, int] = {}

    def accept(candidate: str, weight: int) -> None:
        cand = re.sub(r"\s+", "", candidate or "").strip("：:、。（ ")
        if not (2 <= len(cand) <= 8) or metadata.search(cand):
            return
        core = re.sub(r"^\*?ST", "", cand, flags=re.I)
        if not core:
            return
        it = iter(full)
        if not all(ch in it for ch in core):
            return
        votes[cand] = votes.get(cand, 0) + weight

    for m in re.finditer(r"(?:股票|证券|公司)简称", compact):
        if "变更" in compact[max(0, m.start() - 14):m.start()]:
            continue
        rm = re.compile(r"[：:\s]*([一-龥A-Za-z0-9*]{1,20})").match(compact, m.end())
        if not rm:
            continue
        text_after = rm.group(1)
        # 简称后面直接粘着全称或下一个字段名（实测版式：「公司简称：三一重工三一重工股份有限公司
        # 2026年半年度报告」「股票简称红太阳股票代码000525」）。以前按「尾部是全称前缀」裁，
        # 撞上全称后面还跟年份数字就永远裁不干净（实测 600031/600098/600085… 全部抽成空串），
        # 改成找**最短的前缀**，其剩余部分以全称或某个字段名开头。
        picked = ""
        for size in range(2, len(text_after) + 1):
            rest = text_after[size:]
            if not rest or rest.startswith(full) or any(rest.startswith(t) for t in boundary):
                picked = text_after[:size]
                break
        if not picked:
            continue
        accept(picked, 1)

    fm = re.match(r"^\s*([一-龥A-Za-z0-9*]{2,8})\s*[：:]", compact)
    if fm:
        accept(fm.group(1), 2)  # 文件名前缀是人为登记的叫法，权重更高

    if not votes:
        return ""
    return min(votes, key=lambda s: (-votes[s], len(s)))


def backfill_company_aliases(limit: int = 0) -> dict[str, Any]:
    """把抽到的证券简称写回 1_stock_basic.alias（只补空，不覆盖人工已填的）。

    写路径不能走 query_all——那个函数把事务设成 READ ONLY。这里和 save_document_index
    一样自己开连接。
    """
    result: dict[str, Any] = {"companies": 0, "filled": 0, "codes": []}
    connection = pymysql.connect(**db_config(database=True))
    by_code: dict[str, list[str]] = {}
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT stock_code, stock_name FROM `1_stock_basic`")
            companies = list(cursor.fetchall())
            cursor.execute("SELECT stock_code, file_name FROM `7_document_index`")
            for r in cursor.fetchall():
                by_code.setdefault(str(r["stock_code"] or ""), []).append(str(r["file_name"] or ""))
            for item in companies:
                code = str(item["stock_code"])
                full = re.sub(r"\s+", "", str(item["stock_name"] or ""))
                if not code or not full:
                    continue
                result["companies"] += 1
                found = ""
                for file_name in by_code.get(code, []):
                    found = extract_company_short_name(file_name, full)
                    if found:
                        break
                if not found:
                    try:
                        cursor.execute(
                            "SELECT content FROM `6_rag_chunks` WHERE stock_code = %s AND content LIKE %s LIMIT 40",
                            (code, "%简称%"),
                        )
                        chunk_rows = list(cursor.fetchall())
                    except Exception:
                        chunk_rows = []
                    for r in chunk_rows:
                        found = extract_company_short_name(r["content"], full)
                        if found:
                            break
                if found:
                    cursor.execute(
                        "UPDATE `1_stock_basic` SET alias = %s "
                        "WHERE stock_code = %s AND (alias IS NULL OR alias = '')",
                        (found, code),
                    )
                    result["filled"] += cursor.rowcount
                    result["codes"].append(f"{code}={found}")
                if limit and result["filled"] >= limit:
                    break
        connection.commit()
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        connection.close()
    return result


def display_company_name(stock_code: str, stock_name: str = "") -> str:
    """解析适合界面展示的公司名称。"""
    name = (stock_name or "").strip()
    invalid = (
        not name
        or name == stock_code
        or re.fullmatch(rf"{re.escape(stock_code)}上市公司", name)
    )
    if not invalid:
        return name
    for item in load_document_index():
        if item.get("stock_code") == stock_code:
            candidate = str(item.get("company_name") or "").strip()
            if candidate and candidate != stock_code and "上市公司" not in candidate:
                return candidate
    candidate = chunk_company_name_index().get(stock_code)
    if candidate:
        return candidate
    return name or stock_code

def chunk_company_name_index() -> dict[str, str]:
    """证券代码 → 文本分块里记的公司名，整份索引只建一次。

    原来每解析不到一个代码，就把 24455 条分块从头扫一遍找名字：
    数据质量报告要过 77 家公司，等于近两百万次遍历，接口实测 11.6 秒。
    分块数量一变就重建，语义和原来的顺序扫描一致（取第一条命中的）。
    """
    if _CHUNK_NAME_INDEX["count"] != len(RAG_CHUNKS):
        mapping: dict[str, str] = {}
        for chunk in RAG_CHUNKS:
            code = str(chunk.get("stock_code") or "")
            candidate = str(chunk.get("company_name") or "").strip()
            if code and candidate and candidate != code and code not in mapping:
                mapping[code] = candidate
        _CHUNK_NAME_INDEX["map"] = mapping
        _CHUNK_NAME_INDEX["count"] = len(RAG_CHUNKS)
    return _CHUNK_NAME_INDEX["map"]

RECOMMENDATION_INTENTS: dict[str, tuple[str, ...]] = {
    "营收利润": ("营业收入", "营收", "净利润", "利润"),
    "同比增速": ("同比", "增速", "增长", "下滑", "变化"),
    "资产负债": ("资产负债率", "负债", "资产结构", "偿债", "资产"),
    "现金流": ("现金流", "经营现金"),
    "原文风险": ("风险", "原文", "披露", "管理层", "经营情况", "主营业务", "竞争力"),
    "对比": ("对比", "比较", "哪家", "差异", "vs"),
    "排行": ("排名", "排行", "前5", "前 5", "前十", "最高", "最低", "前几"),
    "图表趋势": ("柱状图", "趋势图", "画出", "生成图", "图表", "趋势"),
}

_RECOMMENDATION_POOL_LIMIT = 10
_RECOMMENDATION_RETURN = 6


def recent_question_preferences(limit_sessions: int = 30) -> dict[str, Any]:
    """从最近的会话里提「用户偏好」：常问哪家、常问什么、常问哪一期。

    只读内存里的 CHAT_SESSIONS（不查库、不发请求），所以每次点「换一批」都能重算。
    历史太少时返回空信号，调用方必须能接受空偏好并退回纯随机 —— 宁可不偏，不许瞎推。
    """
    with CHAT_SESSIONS_LOCK:
        recent = [item for item in CHAT_SESSIONS[-max(1, int(limit_sessions)) :] if isinstance(item, dict)]
    texts: list[str] = []
    for session in recent:
        for message in session.get("messages") or []:
            if str(message.get("role") or "") == "user":
                content = str(message.get("content") or "").strip()
                if content:
                    texts.append(content)
        context = session.get("analysis_context") or {}
        last_question = str(context.get("last_question") or "").strip()
        if last_question:
            texts.append(last_question)
    if not texts:
        return {"codes": {}, "intents": {}, "periods": {}, "samples": 0}

    short_names = company_short_names()
    names_by_code: dict[str, set[str]] = {}
    for item in get_company_list():
        code = str(item.get("stock_code") or "")
        if not code or code.startswith("FILE_"):
            continue
        raw_name = str(item.get("stock_name") or "").strip()
        candidates = {raw_name, display_company_name(code, raw_name), short_names.get(code, "")}
        names_by_code[code] = {name for name in candidates if len(name) >= 2}

    codes: dict[str, int] = {}
    intents: dict[str, int] = {}
    periods: dict[str, int] = {}
    for text in texts:
        for code in set(re.findall(r"(?<!\d)(\d{6})(?!\d)", text)):
            if code in names_by_code:
                codes[code] = codes.get(code, 0) + 3
        for name, owners in ((n, c) for c, names in names_by_code.items() for n in names):
            if name and name in text:
                codes[owners] = codes.get(owners, 0) + 2
        for intent, keywords in RECOMMENDATION_INTENTS.items():
            if any(keyword in text for keyword in keywords):
                intents[intent] = intents.get(intent, 0) + 1
        for period in re.findall(r"(?:19|20)\d{2}年(?:半年报|年报|一季报|三季报)", text):
            periods[period] = periods.get(period, 0) + 1
    return {
        "codes": codes,
        "intents": intents,
        "periods": periods,
        "samples": len(texts),
        "names_by_code": names_by_code,
    }


def rank_recommended_questions(pool: list[str], preferences: dict[str, Any] | None) -> list[str]:
    """按偏好打分排序，同分之间随机抖 —— 既偏向用户常问的方向，又有真随机性。

    底线不变：候选池已经是「验证过答得到数」的那批，这里只改顺序，不往里塞新问题。
    """
    if not pool:
        return []
    preferences = preferences or {}
    codes = preferences.get("codes") or {}
    intents = preferences.get("intents") or {}
    periods = preferences.get("periods") or {}
    names_by_code = preferences.get("names_by_code") or {}
    top_codes = {code for code, _ in sorted(codes.items(), key=lambda kv: -kv[1])[:6]}
    top_intents = {name for name, _ in sorted(intents.items(), key=lambda kv: -kv[1])[:3]}
    top_periods = {name for name, _ in sorted(periods.items(), key=lambda kv: -kv[1])[:2]}
    scored: list[tuple[float, str]] = []
    for text in pool:
        score = random.random()
        for code in top_codes:
            if code in text or any(name and name in text for name in names_by_code.get(code, set())):
                score += 4.0
        for intent in top_intents:
            if any(keyword in text for keyword in RECOMMENDATION_INTENTS.get(intent, ())):
                score += 2.0
        for period in top_periods:
            if period in text:
                score += 1.0
        scored.append((score, text))
    scored.sort(key=lambda item: -item[0])
    return [text for _, text in scored]


def recommend_questions_from_pool(pool: list[str]) -> list[str]:
    """现算偏好 → 排序 → 只回前几条给前端挑（前端还会再随机取 3 个）。"""
    return rank_recommended_questions(list(pool), recent_question_preferences())[:_RECOMMENDATION_RETURN]


def get_recommended_questions() -> list[str]:
    """根据已入库数据生成推荐问题。"""
    global _RECOMMENDATION_CACHE, _RECOMMENDATION_CACHE_AT
    if (
        _RECOMMENDATION_CACHE
        and time.time() - _RECOMMENDATION_CACHE_AT < _RECOMMENDATION_CACHE_SECONDS
    ):
        return recommend_questions_from_pool(_RECOMMENDATION_CACHE)
    with _RECOMMENDATION_LOCK:
        if (
            _RECOMMENDATION_CACHE
            and time.time() - _RECOMMENDATION_CACHE_AT < _RECOMMENDATION_CACHE_SECONDS
        ):
            return recommend_questions_from_pool(_RECOMMENDATION_CACHE)
        companies = get_company_list()
        performance_rows = query_all(
            """
            SELECT stock_code, report_period, revenue, net_profit, net_profit_parent,
                   revenue_yoy, profit_yoy
            FROM 2_performance_report
            WHERE revenue IS NOT NULL OR net_profit IS NOT NULL
            ORDER BY stock_code, report_period DESC
            """
        )
        balance_rows = query_all(
            """
            SELECT stock_code, report_period, total_assets, total_liability
            FROM 4_balance_sheet
            WHERE total_assets IS NOT NULL OR total_liability IS NOT NULL
            ORDER BY stock_code, report_period DESC
            """
        )
        cash_rows = query_all(
            """
            SELECT stock_code, report_period, operate_cash
            FROM 5_cash_flow_statement
            WHERE operate_cash IS NOT NULL
            ORDER BY stock_code, report_period DESC
            """
        )
        performance_by_code: dict[str, list[dict[str, Any]]] = {}
        for row in performance_rows:
            performance_by_code.setdefault(str(row.get("stock_code") or ""), []).append(row)
        balance_by_code: dict[str, dict[str, Any]] = {}
        for row in balance_rows:
            balance_by_code.setdefault(str(row.get("stock_code") or ""), row)
        cash_by_code: dict[str, list[dict[str, Any]]] = {}
        for row in cash_rows:
            cash_by_code.setdefault(str(row.get("stock_code") or ""), []).append(row)
        rag_by_code: dict[str, list[dict[str, Any]]] = {}
        for chunk in RAG_CHUNKS:
            code = str(chunk.get("stock_code") or "")
            if code:
                rag_by_code.setdefault(code, []).append(chunk)

        candidates: list[str] = []
        for company in companies:
            stock_code = str(company.get("stock_code") or "")
            company_name = clean_company_name(
                str(company.get("stock_name") or ""),
                stock_code,
            ) or stock_code
            company_recommendations: list[str] = []
            rows = performance_by_code.get(stock_code, [])
            if rows:
                latest = rows[0]
                latest_period = latest.get("report_period") or ""
                if latest.get("revenue") is not None and latest.get("net_profit") is not None:
                    company_recommendations.append(
                        f"{company_name} {latest_period}营业收入和净利润是多少？"
                    )
                elif latest.get("revenue") is not None:
                    company_recommendations.append(
                        f"{company_name} {latest_period}营业收入是多少？"
                    )
                elif latest.get("net_profit") is not None:
                    company_recommendations.append(
                        f"{company_name} {latest_period}净利润是多少？"
                    )
                if latest.get("net_profit_parent") is not None:
                    company_recommendations.append(
                        f"{company_name} {latest_period}归母净利润是多少？"
                    )
                if sum(1 for row in rows if row.get("revenue") is not None) >= 2:
                    company_recommendations.append(
                        f"画出{company_name}近几期营业收入趋势图"
                    )

            balance = balance_by_code.get(stock_code)
            if (
                balance
                and balance.get("total_assets") not in (None, 0)
                and balance.get("total_liability") is not None
            ):
                company_recommendations.append(
                    f"{company_name}的资产负债率是多少？"
                )
            if len(cash_by_code.get(stock_code, [])) >= 2:
                company_recommendations.append(
                    f"分析{company_name}经营活动现金流变化"
                )

            rag_for_company = rag_by_code.get(stock_code, [])
            risk_chunks = [
                chunk
                for chunk in rag_for_company
                if any(
                    phrase in str(chunk.get("content") or "")
                    for phrase in (
                        "风险提示",
                        "主要风险",
                        "重大风险",
                        "可能面临",
                        "面临的风险",
                        "风险应对",
                    )
                )
            ]
            if len(risk_chunks) >= 2 and sum(
                len(str(chunk.get("content") or "")) for chunk in risk_chunks
            ) >= 400:
                company_recommendations.append(
                    f"{company_name}有哪些主要经营风险？"
                )
            business_chunks = [
                chunk
                for chunk in rag_for_company
                if any(
                    phrase in str(chunk.get("content") or "")
                    for phrase in (
                        "主营业务",
                        "主要产品",
                        "经营模式",
                        "行业地位",
                        "市场竞争",
                        "业务构成",
                        "经营情况",
                    )
                )
            ]
            if len(business_chunks) >= 2 and sum(
                len(str(chunk.get("content") or "")) for chunk in business_chunks
            ) >= 400:
                company_recommendations.append(
                    f"{company_name}的主要经营情况如何？"
                )
            candidates.extend(dict.fromkeys(company_recommendations[:3]))

        company_name_by_code = {
            str(item.get("stock_code") or ""): clean_company_name(
                str(item.get("stock_name") or ""),
                str(item.get("stock_code") or ""),
            )
            for item in companies
            if not str(item.get("stock_code") or "").startswith("FILE_")
        }
        full_metric_codes = []
        for stock_code, rows in performance_by_code.items():
            if stock_code.startswith("FILE_") or not rows:
                continue
            latest = rows[0]
            balance = balance_by_code.get(stock_code) or {}
            if (
                latest.get("revenue") is not None
                and latest.get("net_profit") is not None
                and latest.get("revenue_yoy") is not None
                and latest.get("profit_yoy") is not None
                and balance.get("total_assets") not in (None, 0)
                and balance.get("total_liability") is not None
                and company_name_by_code.get(stock_code)
            ):
                full_metric_codes.append(stock_code)
        preferred_codes = recent_question_preferences().get("codes") or {}
        full_metric_codes.sort(key=lambda code: -int(preferred_codes.get(code, 0)))
        cross_company_questions = []
        for left_index, left_code in enumerate(full_metric_codes):
            for right_code in full_metric_codes[left_index + 1 :]:
                left_name = company_name_by_code[left_code]
                right_name = company_name_by_code[right_code]
                cross_company_questions.append(
                    f"对比{left_name}和{right_name}最近一期营业收入、净利润、"
                    "资产负债率及同比变化，分析两家公司资产结构和利润差异，并生成柱状图。"
                )
                cross_company_questions.append(
                    f"比较{left_name}和{right_name}最近一期营业收入、净利润和资产负债率，"
                    "分析两家公司资产结构与利润变化，并生成柱状图。"
                )
                break
            if len(cross_company_questions) >= 4:
                break
        cross_company_questions = list(dict.fromkeys(cross_company_questions))
        if cross_company_questions:
            remaining = [
                item
                for item in candidates
                if item not in set(cross_company_questions)
            ]
            random.shuffle(remaining)
            candidates = cross_company_questions[:1] + remaining
        else:
            random.shuffle(candidates)
        validated: list[str] = []
        for item in candidates:
            if len(validated) >= _RECOMMENDATION_POOL_LIMIT:
                break
            code = extract_stock_code(item)
            metrics = extract_metrics(item)
            if code and metrics:
                preview = answer_with_structured_data(item, "自动")
                if not preview or not preview.get("metrics"):
                    continue
            validated.append(item)
        _RECOMMENDATION_CACHE = list(validated)
        _RECOMMENDATION_CACHE_AT = time.time()
        return rank_recommended_questions(
            list(validated), recent_question_preferences()
        )[:_RECOMMENDATION_RETURN]

def build_rag_chunks_from_file(
    path: Path,
    pages: list[dict[str, Any]] | None = None,
    defer_vector_save: bool = False,
) -> int:
    """将文档切分为 RAG 文本块并写入存储。

    - `pages`：调用方已解析好的页面文本；传入可省掉本函数内的二次全文解析。
    - `defer_vector_save`：批量导入时置 True，镜像的 rag_chunks.json 由批次收尾
      统一落盘，不再逐份全量写两遍。
    - 向量嵌入失败会以 RuntimeError 抛出（文字切片照常已入库），调用方负责记账，
      自动补向量兜底照旧。
    """
    if pages is None:
        if path.suffix.lower() == ".pdf":
            pages, _ = core.read_pdf(path)
        elif path.suffix.lower() == ".docx":
            pages, _ = core.read_docx(path)
        else:
            pages = []
    combined_text = "\n".join(page.get("text") or "" for page in pages)
    # 身份只认一个来源：文档索引里那份已经过「代码↔名」调和的记录。切片步骤再自己
    # 抽一遍就会造出第二份真相——实测 TCL 英文全文入库是 000100，切片却按文件名哈希
    # 记成 FILE_2A8E1549，结果按公司筛选 RAG 时这家公司搜不到自己的正文。
    indexed = next(
        (
            item
            for item in load_document_index()
            if str(item.get("file_path") or "") == str(path.resolve())
        ),
        None,
    ) or {}
    stock_code = (
        str(indexed.get("stock_code") or "").strip()
        or core.extract_stock_code_from_text(combined_text)
        or core.extract_stock_code_from_name(path)
    )
    company_name = (
        str(indexed.get("company_name") or "").strip()
        or core.resolve_company_name(
            core.extract_company_name(combined_text, stock_code)
            if combined_text
            else "",
            stock_code,
        )
    ) or (path.stem if not core.is_opaque_file_stem(path.stem) else "未识别公司")
    report_period = (
        str(indexed.get("report_period") or "").strip()
        or (
            core.get_latest_report_period(stock_code, "2_performance_report")
            if stock_code and not stock_code.startswith("FILE_")
            else ""
        )
        or ""
    )
    report_label = (
        "摘要"
        if "半年度报告摘要" in combined_text[:6000]
        or "年度报告摘要" in combined_text[:6000]
        else "全文"
        if "半年度报告全文" in combined_text[:6000]
        or "年度报告全文" in combined_text[:6000]
        else ""
    )
    display_name = core.build_document_display_name(
        path,
        stock_code,
        company_name,
        report_period,
        report_label,
    )
    resolved_path = str(path.resolve())
    chunks: list[dict[str, Any]] = []
    chunk_index = 0
    for page in pages:
        raw_text = page.get("text") or ""
        text = re.sub(r"(?m)^\s*(?:第\s*)?\d+\s*页\s*$", "", raw_text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if not text:
            continue
        step = 650
        window = 900
        for start in range(0, len(text), step):
            content = text[start : start + window].strip()
            if len(content) < 30:
                continue
            chunks.append(
                {
                    "file_name": display_name,
                    "file_path": resolved_path,
                    "stock_code": stock_code,
                    "company_name": company_name,
                    "page_no": page.get("page_no", 0),
                    "chunk_index": chunk_index,
                    "content": content,
                }
            )
            chunk_index += 1
    try:
        core.MySQL().init_database()
        connection = pymysql.connect(**db_config())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM 6_rag_chunks WHERE file_path=%s",
                    (resolved_path,),
                )
                if chunks:
                    cursor.executemany(
                        """
                        INSERT INTO 6_rag_chunks (
                          file_name, file_path, stock_code, company_name,
                          page_no, chunk_index, content, embedding_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                item["file_name"],
                                item["file_path"],
                                item["stock_code"],
                                item["company_name"],
                                item["page_no"],
                                item["chunk_index"],
                                item["content"],
                                item.get("embedding_json"),
                            )
                            for item in chunks
                        ],
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception as exc:
        print(f"⚠️ RAG 数据库写入失败，保留 JSON 索引：{exc}")
    with RAG_CHUNKS_LOCK:
        RAG_CHUNKS[:] = [
            chunk
            for chunk in RAG_CHUNKS
            if chunk.get("file_path") != resolved_path
        ]
        RAG_CHUNKS.extend(chunks)
        save_rag_chunks()
    vector_error = index_new_rag_chunks(chunks, save=not defer_vector_save)
    if vector_error:
        raise RuntimeError(f"向量嵌入失败：{vector_error}")
    return len(chunks)

def handle_file_upload(
    files: list[tuple[str, bytes]],
    local_dir: str,
    session_id: str = "",
) -> dict[str, Any]:
    """处理文件上传和本地目录导入。"""
    paths: list[Path] = []
    target_root = BASE_DIR / "storage/uploads/web"
    target_root.mkdir(parents=True, exist_ok=True)
    for filename, content in files:
        directory = target_root / str(time.time_ns())
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / Path(filename or "report.pdf").name
        target.write_bytes(content)
        paths.append(target)
    if local_dir.strip():
        source = Path(local_dir.strip()).expanduser()
        if source.is_dir():
            paths.extend(
                item
                for item in source.rglob("*")
                if item.is_file() and item.suffix.lower() in {".pdf", ".docx"}
            )
        elif source.is_file():
            paths.append(source)
    if not paths:
        raise ValueError("请选择文件或输入有效目录")
    return start_import_task_for_paths(paths, session_id)

IMPORT_CONTROL_ACTIONS = {"pause", "resume", "stop"}
IMPORT_DONE_STATUSES = {"completed", "stopped", "failed"}
IMPORT_WINDOW = 2  # 同时在飞的份数上限，与 config.py:56 的 IMPORT_EXECUTOR(max_workers=2) 对齐


def set_import_job_control(job_id: str, action: str) -> dict[str, Any]:
    """暂停 / 继续 / 结束一个后台导入任务。

    - `pause`：停止投递新文件，**手上那份跑完就停**（线程池 2 个工人，
      所以最多再等 2 份）；此时 `status` 仍是 running，直到队列排空由
      工作线程改成 `paused`，界面靠 `control` 显示「正在等手上那份」。
    - `resume`：从断点继续投剩下的文件，已导入的不重跑（本来也命中去重）。
    - `stop`：结束任务，没投的份数记进 `cancelled`；**已经入库的文件不回滚**
      —— 每份都是完整入库的（文档索引+事实表+切片+向量），半路回滚只会
      造出「有切片没数字」的第三种真相。
    跟 `pause_chat_task`（assistant.py:5259）同一个讲究：已结束的任务
    不再改写状态，否则进度条永远转圈、前端等不到结果。
    """
    with IMPORT_JOBS_LOCK:
        job = IMPORT_JOBS.get(job_id)
        if job is None:
            raise KeyError("导入任务不存在或已被清理")
        if action not in IMPORT_CONTROL_ACTIONS:
            raise ValueError(f"未知的导入操作：{action}")
        if str(job.get("status") or "") in IMPORT_DONE_STATUSES:
            return dict(job)
        job["control"] = {"pause": "pause", "resume": "running", "stop": "stop"}[action]
        if action == "resume":
            job["status"] = "running"
        return dict(job)


IMPORT_JOBS_KEEP = 20  # 已结束任务最多留几个（长跑的 Web 服务里任务表以前只增不减）
IMPORT_JOBS_MIN_AGE = timedelta(hours=1)  # 刚结束的任务先留着，前端可能还在轮询


def prune_import_jobs(keep_id: str = "") -> list[str]:
    """丢掉「结束超过 1 小时、且超出最近 20 个名额」的旧导入任务。

    以前 IMPORT_JOBS 只插不删：服务连续开几天、每天导入几十次，
    每个任务（连它的 errors 列表）会一直占着内存。
    运行中的任务、刚建的任务、刚结束不到 1 小时的任务一律不动，
    所以 pollImportJob 不可能读到被裁掉的 id。
    调用方必须已持有 IMPORT_JOBS_LOCK。
    """
    if len(IMPORT_JOBS) <= IMPORT_JOBS_KEEP:
        return []
    deadline = datetime.now() - IMPORT_JOBS_MIN_AGE
    finished: list[tuple[datetime, str]] = []
    for job_id, job in IMPORT_JOBS.items():
        # 暂停中的任务也保住：它随时可能被「继续」，裁掉之后 resume 就 404 了
        if job_id == keep_id or job.get("status") in {"running", "paused"}:
            continue
        stamp = str(job.get("finished_at") or "")
        if not stamp:
            continue
        try:
            ended = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ended < deadline:
            finished.append((ended, job_id))
    room = max(len(IMPORT_JOBS) - IMPORT_JOBS_KEEP, 0)
    removed = [job_id for _ended, job_id in sorted(finished)[:room]]
    for job_id in removed:
        IMPORT_JOBS.pop(job_id, None)
    return removed


def start_import_task_for_paths(
    paths: list[Path],
    session_id: str = "",
) -> dict[str, Any]:
    """为一组文件创建后台导入任务。"""
    session_id = ensure_import_chat_session(session_id)
    normalized_paths = [Path(item) for item in paths if Path(item).is_file()]
    if not normalized_paths:
        raise ValueError("没有可导入的 PDF 或 DOCX 文件")
    job_id = uuid.uuid4().hex
    with IMPORT_JOBS_LOCK:
        IMPORT_JOBS[job_id] = {
            "id": job_id,
            "status": "running",
            "control": "running",  # running | pause | stop，由 set_import_job_control 写
            "total": len(normalized_paths),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,  # 「结束」时还没投出去的份数
            "active": [],  # 正在处理中的文件名（滑窗最多 2 个），给界面显示「正在处理谁」
            "errors": [],
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "locked_company": None,
            "session_id": session_id,
        }
        prune_import_jobs(keep_id=job_id)
    threading.Thread(
        target=run_import_job,
        args=(job_id, normalized_paths, session_id),
        daemon=True,
    ).start()
    with IMPORT_JOBS_LOCK:
        return dict(IMPORT_JOBS[job_id])

def run_import_job(
    job_id: str,
    paths: list[Path],
    session_id: str = "",
) -> None:
    """后台执行批量导入任务。"""
    with IMPORT_JOBS_LOCK:
        job = IMPORT_JOBS[job_id]
        # 老任务字典可能没有 active（回归件的自制 job 就是），缺了补上，
        # 别让「正在处理谁」这个纯展示字段把真导入带崩。
        job.setdefault("active", [])
    database = core.MySQL()
    database.init_database()
    seen_hashes: set[str] = set()
    hash_lock = threading.Lock()

    imported_companies: set[tuple[str, str]] = set()

    def process_one(path: Path) -> str:
        """处理批量导入中的单个文件。"""
        with IMPORT_JOBS_LOCK:
            job["active"].append(path.name)
        try:
            file_hash = sha256_file(path)
            with hash_lock:
                if file_hash in seen_hashes or find_duplicate(file_hash):
                    # 「重复」不等于「可以删」：托管目录里躺着的可能正是某条文档记录
                    # 唯一的原文（实测被这样删掉 25 份），所以删之前必须问一次库里。
                    if is_managed_copy(path) and path.exists() and not path_referenced_by_document(path):
                        path.unlink()
                    return "duplicate"
                seen_hashes.add(file_hash)
            # 每份 PDF 原先「解析入库」和「切片」各用 pdfplumber 完整读一遍（扫描件
            # 还要两遍逐页 OCR），2 核机器上这是最大的一笔重复开销。这里读一次，两边共用。
            pages: list[dict[str, Any]] | None = None
            tables: list[dict[str, Any]] | None = None
            try:
                if path.suffix.lower() == ".pdf":
                    pages, tables = core.read_pdf(path)
                elif path.suffix.lower() == ".docx":
                    pages, tables = core.read_docx(path)
            except Exception:
                pages = tables = None  # 预读失败退回原路：下游各自再读，错误在自己函数里处理
            metadata = core.process_financial_document(
                path,
                database,
                pages_override=(pages, tables) if pages is not None else None,
            ) or {}
            if metadata.get("quarantined"):
                # 被入库闸门拦下：既不进 7_document_index，也不进 6_rag_chunks/Qdrant，
                # 否则会出现「数据库没有、RAG 能搜到」的第三种真相。
                return "quarantined"
            register_document(
                path,
                file_hash=file_hash,
                managed_copy=False,
                metadata=metadata,
            )
            imported_ticker = str(metadata.get("stock_code") or "").strip()
            imported_name = str(metadata.get("company_name") or "").strip()
            if imported_ticker and not imported_ticker.startswith("FILE_"):
                with hash_lock:
                    imported_companies.add((imported_ticker, imported_name))
            try:
                # defer_vector_save：导入批次里逐份全量 dump rag_chunks.json 太贵
                # （每份两遍、库越大越慢），镜像留到批尾统一落一次。
                build_rag_chunks_from_file(
                    path, pages=pages, defer_vector_save=True
                )
            except Exception as exc:
                # 以前这里 `except Exception: pass` 把切片/嵌入失败整个咽掉、文件照样
                # 计 success——用户看到的是「导入成功」，实际语义检索搜不到这份文件。
                # 现在把失败透到 errors，收尾的自动补向量照旧兜底。
                with IMPORT_JOBS_LOCK:
                    if len(job["errors"]) < 50:
                        job["errors"].append(
                            f"{path.name}: 切片或向量嵌入失败（{str(exc)[:120]}）；"
                            "文字已入库不受影响，向量会在导入收尾时自动补"
                        )
            return "success"
        except Exception:
            if is_managed_copy(path) and path.exists() and not path_referenced_by_document(path):
                path.unlink()
            raise
        finally:
            with IMPORT_JOBS_LOCK:
                if path.name in job["active"]:
                    job["active"].remove(path.name)

    try:
        queue = list(paths)
        futures: dict[Any, Path] = {}
        stopped = False

        def harvest(future: Any) -> None:
            """把一份跑完的结果计进任务进度。"""
            path = futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                with IMPORT_JOBS_LOCK:
                    job["processed"] += 1
                    job["failed"] += 1
                    if len(job["errors"]) < 50:
                        job["errors"].append(f"{path.name}: {exc}")
                return
            with IMPORT_JOBS_LOCK:
                job["processed"] += 1
                if result == "duplicate":
                    job["skipped"] += 1
                elif result == "quarantined":
                    # 不是异常，但也没进库：计入 failed 并把原因透出来，界面才会看见
                    job["failed"] += 1
                    if len(job["errors"]) < 50:
                        job["errors"].append(f"{path.name}: 入库闸门已拦下（待人工确认），未登记文档、未做切片")
                else:
                    job["success"] += 1

        while True:
            with IMPORT_JOBS_LOCK:
                control = str(job.get("control") or "running")
            if control == "stop":
                stopped = True
                break
            # 一次只投够窗口（=线程池那两个工人）那么多份。以前是整批预投：
            # 几万个文件全压在池子自己的内部队列里，撤不回来，「暂停/结束」
            # 就没法兑现成「手上这份跑完就停」。
            while control == "running" and queue and len(futures) < IMPORT_WINDOW:
                item = queue.pop(0)
                futures[IMPORT_EXECUTOR.submit(process_one, item)] = item
                with IMPORT_JOBS_LOCK:
                    control = str(job.get("control") or "running")
            if not futures:
                if not queue:
                    break
                # 手上没活儿了但还有存货 ⇒ 就是暂停到位了，等「继续」或「结束」
                with IMPORT_JOBS_LOCK:
                    job["status"] = "paused" if control == "pause" else "running"
                time.sleep(0.4)
                continue
            done, _pending = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                harvest(future)
        if stopped and futures:
            # 「结束」也不掐断正在写库的那份：半途断会留下「文档登记了、
            # 数字没入库」的半行数据，比多花几秒难查得多。
            for future in list(futures):
                try:
                    future.result()
                except Exception:
                    pass
                harvest(future)
        try:
            # 整批导入结束后再收鬼影主体：单个文件处理中途删身份行会误伤同批还在写的主体
            pruned = prune_orphan_entities()
            stale = prune_orphan_validation_results()
            if pruned:
                print(f"🧹 已清理鬼影主体 {len(pruned)} 个：{'、'.join(pruned[:10])}")
            if stale:
                print(f"🧹 已清理陈旧校验结论 {stale} 行（描述的数字已不在库）")
        except Exception as exc:
            print(f"⚠️ 鬼影主体清理跳过：{exc}")
        with IMPORT_JOBS_LOCK:
            job["status"] = "stopped" if stopped else "completed"
            if stopped:
                job["cancelled"] = len(queue)
        if len(imported_companies) == 1:
            ticker, company_name = next(iter(imported_companies))
            locked_company = lock_chat_session_company(
                session_id,
                ticker,
                company_name,
            )
            with IMPORT_JOBS_LOCK:
                job["locked_company"] = locked_company
    except Exception as exc:
        with IMPORT_JOBS_LOCK:
            job["status"] = "failed"
            job["errors"].append(str(exc))
    finally:
        with IMPORT_JOBS_LOCK:
            job["active"] = []
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 批尾把 rag_chunks 镜像统一落一次盘（导入期间逐份全量 dump 的开销已省掉；
        # 中途被打断留下的缺口由下面的自动补向量兜底，它跑完自己也会 save）。
        try:
            with RAG_CHUNKS_LOCK:
                save_rag_chunks()
        except Exception as exc:
            print(f"⚠️ 导入收尾写 rag_chunks 镜像失败：{exc}")
        # 导入收尾（含被「结束/暂停后退出」打断的情况）最容易留下「切片进了库、
        # 向量还没嵌」的洞 ⇒ 自己补上，别让数据半可用（RAG 只走词面、搜不到语义）。
        auto_backfill_pending_vectors("导入任务收尾")
    # 导入进度不再回写会话标题（历史会话列表里不该出现「导入中…」这种非聊天条目），
    # 界面改读 /api/import/status/<id> 并在「数据导入」页显示进度卡。

def start_bulk_import_task(local_dir: str, session_id: str = "") -> dict[str, Any]:
    """创建并启动批量导入任务。"""
    # 空串在这里是**炸点**：`Path("").expanduser()` 会落到服务进程自己的当前目录
    # （= 项目根），`rglob` 于是把 `storage/uploads/web` 里已经在库里的原文
    # 全当成「待导入目录里的新文件」。实测一次 `POST /api/import/start {}`
    # 就这样把 25 份文档记录的原文删掉了（删除侧已加引用校验，见
    # `path_referenced_by_document`），所以这一头也必须拦住：
    # 目录要写明白，而且不许把系统的托管上传目录圈进来。
    raw = str(local_dir or "").strip()
    if not raw:
        raise ValueError("请写明要导入的本地目录（不能用空目录，空目录会被当成服务自己的工程目录）")
    source = Path(raw).expanduser()
    if not source.is_absolute():
        raise ValueError(f"导入目录请写绝对路径：{raw}")
    if not source.is_dir():
        raise ValueError("本地目录不存在")
    managed_root = (BASE_DIR / "storage" / "uploads").resolve()
    try:
        managed_root.relative_to(source.resolve())
    except ValueError:
        pass
    else:
        raise ValueError(
            f"导入目录不能包含 FinSight 的托管上传目录（{managed_root}）："
            "那里放的是已入库文档的原文，不是待导入的素材"
        )
    paths = sorted(
        item
        for item in source.rglob("*")
        if item.is_file() and item.suffix.lower() in {".pdf", ".docx"}
    )
    if not paths:
        raise ValueError("目录中没有 PDF 或 DOCX 文件")
    return start_import_task_for_paths(paths, session_id)

RAG_CHUNKS.extend(load_rag_chunks())
CHAT_SESSIONS.extend(load_chat_sessions())
