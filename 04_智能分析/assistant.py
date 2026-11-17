"""智能问答模块。负责大模型规划、RAG 检索、图表数据构建和聊天结果整理。"""
from __future__ import annotations  # 启用延迟类型注解，兼容较早的 Python 版本

# Python 标准库：文本处理、并发、网络和日期时间工具。
import errno  # 识别端口占用等系统错误码
import hashlib  # 计算文件和文本的 SHA256 摘要
import json  # 读写 JSON 配置、索引和模型结果
import mimetypes  # 判断上传文件和静态资源的 MIME 类型
import os  # 处理路径和操作系统相关操作
import random  # 随机生成推荐问题顺序
import re  # 使用正则表达式解析财务文本和 Markdown
import socket  # 检测本地端口和网络连接
import subprocess  # 调用 Windows OCR 和安装依赖
import sys  # 管理模块路径、标准输出和程序退出
import tempfile  # 创建 PDF 渲染和 OCR 的临时文件
import threading  # 提供线程锁和后台线程能力
import time  # 生成时间戳并控制重试间隔
import unicodedata  # 规范化财务字段和公司名称
import uuid  # 生成文档和会话唯一标识
from concurrent.futures import ThreadPoolExecutor, as_completed  # 管理后台任务和并发导入
from dataclasses import dataclass  # 定义财务指标数据结构
from datetime import date, datetime  # 处理报告期、日期和导入时间
from decimal import Decimal, InvalidOperation  # 精确处理财务金额和比例
from email import policy  # 解析 HTTP multipart 上传数据
from email.parser import BytesParser  # 解析上传请求的邮件格式内容
from http import HTTPStatus  # 使用标准 HTTP 状态码
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # 提供基础 HTTP 服务和请求处理
from pathlib import Path  # 使用跨平台路径对象定位项目文件
from typing import Any  # 提供类型注解，提高代码可读性
from urllib.parse import parse_qs, quote, urlparse  # 解析 URL 和编码文件名
import urllib.request  # 访问本地服务并发送 HTTP 请求

# 第三方依赖：OpenAI 客户端、数据库和 PDF 相关处理。
import pandas as pd  # 生成和展示财务数据表
import pdfplumber  # 提取 PDF 文本和表格
import pymysql  # 连接和操作 MySQL 数据库
import pypdfium2 as pdfium  # 渲染扫描版 PDF 页面
import numpy as np  # 计算混合检索中的向量相似度
from docx import Document  # 读取 Word 文档内容
from openai import OpenAI  # 调用云端大模型和嵌入模型
from pymysql.cursors import DictCursor  # 使用字典游标读取数据库结果

# 项目内部模块：读取配置和共享状态。
from config import *  # 导入项目配置、路径和共享状态

# 数据层接口：复用文档解析、数据库查询和财务分析能力。
from data import *  # 导入数据层函数和共享数据
import vector_store  # 提供 Qdrant 向量召回

DEAL_REFERENCE_WORDS = (
    "本次交易",
    "本次重组",
    "该交易",
    "这笔交易",
    "本次收购",
    "上述交易",
    "标的公司",
    "标的资产",
)
COMPANY_SWITCH_WORDS = ("切换公司", "换成", "改查", "换一家", "改为查询")

# 不能当数值轴用的描述列（代码/名称/报告期，含中文表头）。
CHART_META_COLUMNS = {
    "stock_code",
    "stock_name",
    "report_period",
    "id",
    "company_ticker",
    "company_name",
    "股票代码",
    "股票简称",
    "证券代码",
    "公司名称",
    "公司简称",
    "报告期",
    "日期",
    "序号",
}
COMPARISON_INTENT_WORDS = (
    "对比",
    "比较",
    "分别",
    "差异",
    "差距",
    "谁更",
    "哪家",
    "相较",
    "相比",
    "横向",
    "多家",
)
MAX_COMPARISON_COMPANIES = 6

class SQLValidationError(ValueError):
    """表示模型生成的 SQL 未通过只读安全校验。"""

PAGE_REFERENCE_WORDS = (
    "第几页",
    "哪一页",
    "页码",
    "出处",
    "来源页",
    "来源文件",
    "文件地址",
    "哪份文件",
    "哪个文件",
)

def question_wants_page_reference(question: str) -> bool:
    """判断用户是否明确要求文件名或页码出处。"""
    return any(word in (question or "") for word in PAGE_REFERENCE_WORDS)

def strip_source_citations(text: str) -> str:
    """普通问答中移除文件名、路径和页码引用。"""
    cleaned = str(text or "")
    cleaned = re.sub(
        r"《[^》\n]{1,240}》\s*第\s*\d+\s*页\s*[：:]?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:来源文件|文件|出处)\s*[：:][^\n；;]{0,240}?"
        r"第\s*\d+\s*页\s*[：:]?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:(?:原文|报告|资料|文件)\s*)?第\s*\d+\s*页\s*[：:]?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:[A-Za-z]:\\|/)[^\n；;<>|]{1,300}?\.(?:pdf|docx)\s*",
        "",
        cleaned,
        flags=re.I,
    )
    return re.sub(r"\s{2,}", " ", cleaned).strip()

def classify_question_task(question: str) -> str:
    """将问题路由到稳定的专用处理器。"""
    compact = re.sub(r"\s+", "", question or "")
    if is_chart_style_followup(question):
        return "chart_restyle"
    if any(
        marker in compact
        for marker in (
            "前10名股东",
            "前十大股东",
            "股东持股",
            "持股情况表",
            "股东持股比例",
        )
    ):
        return "shareholding_table"
    if any(
        marker in compact
        for marker in ("风险", "诉讼", "担保", "处罚", "不确定性")
    ):
        return "risk_text"
    if any(
        marker in compact
        for marker in (
            "主要经营情况",
            "经营情况如何",
            "主营业务",
            "主要业务",
            "经营概述",
            "经营分析",
            "业务情况",
            "经营范围",
        )
    ):
        return "operating_overview"
    try:
        if extract_metrics(question):
            return "financial_metric"
    except Exception as error:
        # 2026-10-28 静态审计（类别2）：这里吞掉异常会让意图**静默降级**成 general，
        # 后续链路整个换掉却没有任何痕迹。按用户决定只加日志，行为不变（仍返回 general）。
        print(
            f"[意图识别] extract_metrics 异常，降级为 general："
            f"{type(error).__name__}: {str(error)[:160]}｜题目={str(question)[:40]}"
        )
    return "general"

COMPANY_SUFFIX_WORDS = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "集团股份",
    "集团控股",
    "控股集团",
    "集团",
    "控股",
    "股份",
    "公司",
)

def company_name_forms(name: str) -> list[str]:
    """从全称逐级剥后缀，拿到「广西梧州中恒集团 → 广西梧州中恒」这类中间形态。"""
    forms = [name]
    current = name
    while len(current) > 4:
        stripped = current
        for word in COMPANY_SUFFIX_WORDS:
            if current.endswith(word) and len(current) - len(word) >= 4:
                stripped = current[: -len(word)]
                break
        if stripped == current:
            break
        current = stripped
        forms.append(current)
    return forms

_ALIAS_OWNERS_CACHE: dict[str, Any] = {}
_COMPANY_ALIAS_MEMO: dict[tuple[str, bool], list[str]] = {}
_COMPANY_LIST_MEMO: dict[str, Any] = {}
_ALIAS_OWNERS_LOCK = threading.Lock()

def cached_company_list(ttl: float = 30.0) -> list[dict[str, Any]]:
    """短时间内复用公司列表，避免识别一次公司就打一次库。"""
    now = time.time()
    rows = _COMPANY_LIST_MEMO.get("rows") or []
    if not rows or now - float(_COMPANY_LIST_MEMO.get("ts") or 0) > ttl:
        try:
            rows = get_company_list()
        except Exception:
            rows = _COMPANY_LIST_MEMO.get("rows") or []
        _COMPANY_LIST_MEMO["rows"] = rows
        _COMPANY_LIST_MEMO["ts"] = now
    return rows

def derived_alias_owners() -> dict[str, set[str]]:
    """统计所有公司的「派生简称」分别属于哪几家，用于剔除通名歧义。

    「股份有限公司」「技股份有限公司」这种切片被几十家公司共用，
    当成别名会让随便一句提到公司类型的问话都锁定到某家公司。

    并发约束：Web 服务是 ThreadingHTTPServer，多个请求会同时进这里重建。
    旧写法把凭证（signature）和载荷（index）分两条语句发布，交叉发布能留下
    「凭证=新列表、载荷=旧列表」的组合，而读路径只比凭证，于是这份错配会一直
    留在缓存里——简称明明已经唯一了还被判定为歧义，公司识别不到、界面变成追问。
    现在凭证与载荷打包成一条赋值发布（CPython 单条 dict store 是原子的），
    读者只会拿到完整的一对；重建再叠一把锁去重，并在锁内重取公司列表，
    保证发布出去的一对确实对应「当下」的列表。
    """
    companies = cached_company_list()
    signature = tuple(sorted(str(item.get("stock_code") or "") for item in companies))
    cached = _ALIAS_OWNERS_CACHE.get("entry")
    if cached is not None and cached[0] == signature:
        return cached[1]
    with _ALIAS_OWNERS_LOCK:
        companies = cached_company_list()
        signature = tuple(
            sorted(str(item.get("stock_code") or "") for item in companies)
        )
        cached = _ALIAS_OWNERS_CACHE.get("entry")
        if cached is not None and cached[0] == signature:
            return cached[1]
        owners: dict[str, set[str]] = {}
        for item in companies:
            name = str(item.get("stock_name") or "").strip()
            for alias in company_alias_candidates(name, unique_only=False):
                # 按公司名而不是代码计：同一家公司被拆成多个主体（重复导入）不该被当成歧义。
                owners.setdefault(alias, set()).add(name)
        _COMPANY_ALIAS_MEMO.clear()
        _ALIAS_OWNERS_CACHE["entry"] = (signature, owners)
        return owners

def company_alias_candidates(company_name: str, unique_only: bool = True) -> list[str]:
    """全称 + 去后缀名（永远保留）+ 公告简称 + 首尾切片简称（默认可要求全库唯一）。"""
    memo_key = (str(company_name or ""), unique_only)
    cached = _COMPANY_ALIAS_MEMO.get(memo_key)
    if cached is not None:
        return cached
    name = re.sub(r"\s+", "", company_name or "")
    if not name:
        return []
    core = {name}
    core.update(company_name_forms(name))
    for prefix in ("关于", "证券简称"):
        if name.startswith(prefix):
            core.add(name[len(prefix) :])
    # 公告登记的证券简称（来自 1_stock_basic.alias，入库时从「股票简称」/文件名前缀解析）：
    # 「豪迈科技」「首创环保」「中国国贸」这类首尾切片推不出来的名字，以前只能靠用户写全称或六位代码。
    core.update(registered_company_aliases(name))
    derived = {name}
    for form in company_name_forms(name):
        for cut in range(4, min(7, len(form)) + 1):
            derived.add(form[:cut])
            derived.add(form[-cut:])
    derived -= core
    if unique_only:
        owners = derived_alias_owners()
        derived = {item for item in derived if len(owners.get(item, set())) <= 1}
    aliases = core | derived
    cleaned = sorted(
        (
            item
            for item in aliases
            if len(item) >= 4 and not item.isdigit() and not item.startswith("FILE_")
        ),
        key=len,
        reverse=True,
    )
    if len(_COMPANY_ALIAS_MEMO) > 4000:
        _COMPANY_ALIAS_MEMO.clear()
    _COMPANY_ALIAS_MEMO[memo_key] = cleaned
    return cleaned

def company_aliases(company_name: str) -> list[str]:
    """生成公司全称、去后缀名与常用证券简称，用于稳定识别会话实体。

    只砍后缀产不出「云南白药」「四川九洲」「中恒集团」这类简称，
    用户按简称提问就会识别不到公司、退化成追问。
    """
    return company_alias_candidates(company_name)

def identify_company_from_question(
    question: str,
) -> dict[str, str] | None:
    """从问题中识别公司，并优先使用数字证券代码。"""
    compact = re.sub(r"\s+", "", question or "")
    # 抓代码必须用「摘掉年份」的副本：`000538 2026年半年报` 去掉空格后是
    # `0005382026年半…`，6 位代码后面紧跟着数字 2 ⇒ `(?<!\d)(\d{6})(?!\d)`
    # 判它「不是独立代码」⇒ 这家公司完全认不出来（identify=None）⇒ 锁不上公司
    # ⇒ 风险分支 `if not ticker: return None` ⇒ 答案变成「没有检索到」。
    # 实测：「000538 2026年半年报里提到的主要经营风险有哪些」identify=None，
    # 而「云南白药 2026年…」和「000538 的经营风险」都能认出 ⇒ 差异只在这串年份。
    # 别名匹配仍用原 compact（只删字符可能把两段拼成假别名，不扩大行为面）。
    # 与 identify_comparison_companies 里的同款修复保持一致。
    code_text = re.sub(r"(?:19|20)\d{2}\s*年", "", compact)
    codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", code_text)
    companies = []
    try:
        companies = get_company_list()
    except Exception:
        companies = []
    by_ticker = {
        str(item.get("stock_code") or "").strip(): str(
            item.get("stock_name") or ""
        ).strip()
        for item in companies
        if item.get("stock_code")
    }
    for code in codes:
        if code in by_ticker:
            return {
                "ticker": code,
                "name": display_company_name(code, by_ticker[code]),
                "primary": not code.startswith("FILE_"),
            }
    matched: list[tuple[int, str, str]] = []
    for item in companies:
        ticker = str(item.get("stock_code") or "").strip()
        name = str(item.get("stock_name") or "").strip()
        if not ticker or not name:
            continue
        for alias in company_aliases(name):
            if alias in compact:
                matched.append((len(alias), ticker, name))
    if not matched:
        return None
    matched.sort(
        key=lambda item: (
            item[0],
            not item[1].startswith("FILE_"),
            item[1],
        ),
        reverse=True,
    )
    numeric_match = next(
        (item for item in matched if not item[1].startswith("FILE_")),
        None,
    )
    selected = numeric_match or matched[0]
    _, ticker, name = selected
    return {
        "ticker": ticker,
        "name": display_company_name(ticker, name),
        "primary": not ticker.startswith("FILE_"),
    }

def question_has_comparison_intent(question: str) -> bool:
    """判断问题是否明确要求横向比较或并列查询。"""
    compact = re.sub(r"\s+", "", question or "")
    if any(word in compact for word in COMPARISON_INTENT_WORDS):
        return True
    return bool(re.search(r"[、,，/]|(?:与|和|以及|及).{1,30}(?:与|和|以及|及)", compact))

def identify_comparison_companies(question: str) -> list[dict[str, str]]:
    """识别同一问题中的多家主要上市公司，并保持问题中的出现顺序。"""
    # 先把「2026年」这类年份摘掉再抓代码：否则 `6000072026年半年报` 里
    # 600007 后面紧跟着数字 2，`(?<!\d)(\d{6})(?!\d)` 判它不是独立代码 ⇒
    # 第二家认不出来 ⇒ comparison_mode 旗子不开 ⇒ 会话被锁成单公司，
    # 越界保护把「A 和 B 对比」的正确答案整份删成「未找到有效信息」。
    # 实测题：「对比 000538 和 600007 2026年半年报 营业收入 净利润 资产负债率」。
    compact = re.sub(r"(?:19|20)\d{2}\s*年|\s+", "", question or "")
    companies = []
    try:
        companies = get_company_list()
    except Exception:
        companies = []

    matches: list[tuple[int, int, dict[str, str]]] = []
    seen: set[str] = set()
    by_ticker = {
        str(item.get("stock_code") or "").strip(): str(
            item.get("stock_name") or ""
        ).strip()
        for item in companies
        if str(item.get("stock_code") or "").startswith(("0", "3", "6", "8", "9"))
    }
    for code in re.findall(r"(?<!\d)(\d{6})(?!\d)", compact):
        if code not in by_ticker or code in seen:
            continue
        seen.add(code)
        matches.append(
            (
                compact.find(code),
                code,
                {
                    "ticker": code,
                    "name": display_company_name(code, by_ticker[code]),
                    "primary": True,
                },
            )
        )

    for item in companies:
        ticker = str(item.get("stock_code") or "").strip()
        name = str(item.get("stock_name") or "").strip()
        if (
            not ticker
            or ticker.startswith("FILE_")
            or ticker in seen
            or not name
        ):
            continue
        positions = [
            compact.find(alias)
            for alias in company_aliases(name)
            if alias and compact.find(alias) >= 0
        ]
        if not positions:
            continue
        seen.add(ticker)
        matches.append(
            (
                min(positions),
                ticker,
                {
                    "ticker": ticker,
                    "name": display_company_name(ticker, name),
                    "primary": True,
                },
            )
        )

    matches.sort(key=lambda item: (item[0], item[1]))
    detected = [item[2] for item in matches]
    if len(detected) < 2:
        return []
    if not question_has_comparison_intent(compact) and len(
        re.findall(r"(?<!\d)(\d{6})(?!\d)", compact)
    ) < 2:
        return []
    return detected[:MAX_COMPARISON_COMPANIES]

def build_company_lock_clarification() -> dict[str, Any]:
    """在缺少会话公司锁定时返回追问。"""
    return {
        "answer": "请确认公司名称或股票代码，确认后我再基于该公司已上传文件检索。",
        "summary": "当前会话尚未锁定公司，需要先确认公司名称或股票代码。",
        "table": [],
        "chart": None,
        "sources": [],
        "metrics": [],
        "key_points": [],
        "clarification_options": ["重庆美心翼申机械股份有限公司", "920833"],
        "warnings": [],
        "mode": "clarify",
        "company_name": "",
    }

def build_switch_company_clarification(
    locked_company: dict[str, str],
    detected_company: dict[str, str],
) -> dict[str, Any]:
    """在用户未明确切换公司时阻止会话实体被替换。"""
    locked_name = locked_company.get("name") or locked_company.get("ticker") or ""
    detected_name = detected_company.get("name") or detected_company.get("ticker") or ""
    result = build_company_lock_clarification()
    result["answer"] = (
        f"当前会话已锁定为{locked_name}。检测到问题中提及其他公司；"
        "如需切换，请明确说明“切换公司”。"
    )
    result["summary"] = f"当前会话已锁定为{locked_name}，请明确确认是否切换公司。"
    result["clarification_options"] = [f"切换公司到{detected_name}"]
    return result

def build_mentioned_company_response(
    detected_company: dict[str, str],
) -> dict[str, Any]:
    """提示当前文件库中没有该公司的独立披露报告。"""
    name = detected_company.get("name") or detected_company.get("ticker") or "该公司"
    result = build_company_lock_clarification()
    result["answer"] = (
        f"当前已上传文件中未将“{name}”识别为独立披露主体，"
        "现有内容主要是该公司作为中介机构或其他文件中的引用。"
        "无法据此回答其自身的主要经营情况，请补充该公司的年报、半年报等独立文件。"
    )
    result["summary"] = f"{name}缺少独立披露文件，无法回答经营情况。"
    result["clarification_options"] = []
    result["company_name"] = ""
    return result

def lock_company_for_session(
    session: dict[str, Any],
    question: str,
) -> dict[str, Any] | None:
    """维护会话级公司锁定；用户明确提到新公司时自动切换。"""
    with CHAT_SESSIONS_LOCK:
        locked_company = session.get("locked_company")
        if not isinstance(locked_company, dict):
            locked_company = {}
        detected = identify_company_from_question(question)
        if detected:
            if not detected.get("primary", True):
                return build_mentioned_company_response(detected)
            locked_ticker = str(locked_company.get("ticker") or "")
            if not locked_ticker or locked_ticker != detected["ticker"]:
                locked_company = dict(detected)
                session["locked_company"] = locked_company
                session["locked_deal"] = {
                    "deal_id": f"{detected['ticker']}:current_transaction",
                    "label": "本次交易",
                }
                session["analysis_context"] = {}
        elif not locked_company and any(
            word in question for word in DEAL_REFERENCE_WORDS
        ):
            return build_company_lock_clarification()
        if locked_company:
            session.setdefault("locked_company", locked_company)
            deal_id = f"{locked_company['ticker']}:current_transaction"
            session.setdefault(
                "locked_deal",
                {"deal_id": deal_id, "label": "本次交易"},
            )
        return None

def locked_company_from_request(
    request: dict[str, Any] | None,
) -> dict[str, str]:
    """读取请求中的锁定公司。"""
    locked = (request or {}).get("locked_company")
    if not isinstance(locked, dict):
        return {}
    ticker = str(locked.get("ticker") or "").strip()
    name = str(locked.get("name") or "").strip()
    return {"ticker": ticker, "name": name} if ticker else {}

def comparison_companies_from_request(
    request: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """读取请求中允许参与同一轮比较的公司列表。"""
    companies = (request or {}).get("comparison_companies")
    if not isinstance(companies, list):
        return []
    normalized = []
    seen: set[str] = set()
    for item in companies:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or item.get("stock_code") or "").strip()
        name = str(item.get("name") or item.get("stock_name") or "").strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        normalized.append({"ticker": ticker, "name": name or ticker})
    return normalized

def split_subtask_companies(company: dict[str, Any]) -> list[dict[str, str]]:
    """把模型写成整串的多公司（ticker="000538,000932"）拆成逐公司，避免检索过滤不到。"""
    tickers = [item.strip() for item in str(company.get("ticker") or "").split(",") if item.strip()]
    if not tickers:
        return []
    names = [
        item.strip()
        for item in re.split(r"[、,;；/]", str(company.get("name") or ""))
        if item.strip()
    ]
    return [
        {"ticker": ticker, "name": names[index] if index < len(names) else ticker}
        for index, ticker in enumerate(tickers)
    ]

def rag_targets_for_request(
    request: dict[str, Any],
    company: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """RAG 该按哪些公司过滤：子任务指定 → 比较清单 → 会话锁定，逐层兜底。"""
    targets = (
        split_subtask_companies(company or {})
        or comparison_companies_from_request(request or {})
    )
    if targets:
        return targets
    locked = locked_company_from_request(request)
    return [locked] if locked else []

def chunk_company_ticker(chunk: dict[str, Any]) -> str:
    """兼容读取 RAG 片段的 company_ticker 和 stock_code。"""
    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(
        chunk.get("company_ticker")
        or chunk.get("stock_code")
        or metadata.get("company_ticker")
        or metadata.get("stock_code")
        or ""
    ).strip()

def chunk_deal_id(chunk: dict[str, Any]) -> str:
    """兼容读取 RAG 片段的 deal_id。"""
    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(
        chunk.get("deal_id")
        or metadata.get("deal_id")
        or ""
    ).strip()

def filter_sources_by_locked_company(
    sources: list[dict[str, Any]],
    request: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """丢弃不属于会话锁定公司的召回片段。"""
    locked = locked_company_from_request(request)
    ticker = locked.get("ticker", "")
    if not ticker:
        return []
    return [
        item
        for item in sources
        if chunk_company_ticker(item) == ticker
    ]

def prioritize_shareholding_sources(
    question: str,
    sources: list[dict[str, Any]],
    request: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """优先补充包含前十大股东表的检索证据。"""
    if not any(word in question for word in ("股东", "持股", "股权")):
        return sources
    locked = locked_company_from_request(request)
    stock_code = locked.get("ticker") or extract_stock_code(question)
    if not stock_code:
        return sources
    markers = ("前10名股东", "前十大股东", "公司股东数量及持股情况")
    page_keys: set[tuple[str, int]] = set()
    for chunk in RAG_CHUNKS:
        if stock_code and str(chunk.get("stock_code") or "") != stock_code:
            continue
        content = re.sub(r"\s+", "", str(chunk.get("content") or ""))
        if any(marker in content for marker in markers):
            page_keys.add(
                (
                    str(chunk.get("file_path") or ""),
                    int(chunk.get("page_no") or 0),
                )
            )
    direct = [
        chunk
        for chunk in RAG_CHUNKS
        if (
            str(chunk.get("file_path") or ""),
            int(chunk.get("page_no") or 0),
        )
        in page_keys
    ]
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for item in direct + sources:
        key = (
            str(item.get("file_path") or ""),
            int(item.get("page_no") or 0),
            int(item.get("chunk_index") or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:12]

def extract_shareholding_rows_from_pdf(
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    """从原始 PDF 页面表格提取股东持股比例。"""
    file_path = Path(str(item.get("file_path") or ""))
    page_no = int(item.get("page_no") or 0)
    if not file_path.exists() or page_no < 1:
        return []
    try:
        with pdfplumber.open(str(file_path)) as pdf:
            # page_no 是 1 基页码，取下标要先减 1，否则优先读到下一页的续表。
            page_indexes = [page_no - 1, page_no]
            for page_index in dict.fromkeys(page_indexes):
                if page_index < 0 or page_index >= len(pdf.pages):
                    continue
                page = pdf.pages[page_index]
                for table in page.extract_tables():
                    if not table:
                        continue
                    header_cells = [
                        re.sub(r"\s+", "", str(cell or ""))
                        for row in table[:3]
                        for cell in row
                    ]
                    name_index = next(
                        (
                            index
                            for index, value in enumerate(header_cells)
                            if "股东名称" in value
                        ),
                        -1,
                    )
                    percent_index = next(
                        (
                            index
                            for index, value in enumerate(header_cells)
                            if "持股比例" in value
                        ),
                        -1,
                    )
                    nature_index = next(
                        (
                            index
                            for index, value in enumerate(header_cells)
                            if "股东性质" in value
                        ),
                        -1,
                    )
                    if name_index < 0 or percent_index < 0:
                        continue
                    rows: list[dict[str, Any]] = []
                    seen_names: set[str] = set()
                    for row in table:
                        if (
                            not row
                            or name_index >= len(row)
                            or percent_index >= len(row)
                        ):
                            continue
                        name = re.sub(r"\s+", "", str(row[name_index] or ""))
                        nature = (
                            re.sub(r"\s+", "", str(row[nature_index] or ""))
                            if 0 <= nature_index < len(row)
                            else ""
                        )
                        percentage_text = re.sub(
                            r"\s+",
                            "",
                            str(row[percent_index] or ""),
                        )
                        if (
                            not name
                            or name in seen_names
                        ):
                            continue
                        try:
                            percentage = float(
                                percentage_text.rstrip("%（%）()")
                            )
                        except ValueError:
                            continue
                        if percentage <= 0 or percentage > 100:
                            continue
                        seen_names.add(name)
                        rows.append(
                            {
                                "股东名称": name,
                                "股东性质": nature,
                                "持股比例": percentage,
                            }
                        )
                        if len(rows) >= 10:
                            break
                    if len(rows) >= 6:
                        return rows
    except Exception:
        return []
    return []

def extract_shareholding_rows_from_text(
    segment: str,
) -> list[dict[str, Any]]:
    """解析 PDF 文本中列顺序错位的股东持股表格。"""
    compact = re.sub(r"\s+", "", segment or "")
    end_marker = compact.find("上述股东关联关系")
    if end_marker >= 0:
        compact = compact[:end_marker]
    matches = list(
        re.finditer(
            r"(?<!\d)(\d{1,2}\.\d{1,2})%?([\d,]{3,})",
            compact,
        )
    )
    rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    previous_end = 0
    for match in matches:
        percentage = float(match.group(1))
        if percentage <= 0 or percentage > 100:
            continue
        prefix = compact[previous_end : match.start()]
        previous_end = match.end()
        prefix = re.sub(r"^[.\d,]+", "", prefix)
        prefix = re.sub(r"^(?:无[\d,]*)+", "", prefix)
        if "股东名称" in prefix:
            prefix = prefix.split("股东名称", 1)[-1]
        if "股份数量" in prefix:
            prefix = prefix.rsplit("股份数量", 1)[-1]
        leading_match = re.match(
            r"^(?:不适用|质押|无)?[\d.,]*"
            r"(（有限合伙）|集合资金信托计划|心（有限合伙）法人|"
            r"限公司|司(?:法人|人)?|户)",
            prefix,
        )
        if leading_match and rows:
            marker = leading_match.group(1)
            appended = {
                "心（有限合伙）法人": "心（有限合伙）",
                "司法人": "司",
                "司人": "司",
                "司": "司",
            }.get(marker, marker)
            if not (
                appended == "限公司"
                and rows[-1]["股东名称"].endswith("有限公司")
            ):
                rows[-1]["股东名称"] += appended
            prefix = prefix[leading_match.end() :]
        prefix = re.sub(r"^(?:数量|状态|股份状态)+", "", prefix)
        candidates = re.findall(
            r"[\u4e00-\u9fffA-Za-z&（）()·、.,\-－—]{3,120}",
            prefix,
        )
        if not candidates:
            continue
        name = candidates[-1]
        name = re.sub(
            r"(?:境内非国有法人|国有法人|国有法|境内自然人|境内自|境外法人|境外法|其他)+$",
            "",
            name,
        )
        name = re.sub(
            r"^(?:限公司|公司|人|然人|自然人|境内非国有法人|国有法人|国有法|境内自然人|境内自|境外法人|境外法|其他)+",
            "",
            name,
        )
        wrapped_prefix = next(
            (
                marker
                for marker in (
                    "业交易型开放式指数证券投资基金",
                    "交易型开放式指数证券投资基金",
                    "证券投资基金",
                )
                if name.startswith(marker)
            ),
            "",
        )
        if wrapped_prefix and rows:
            rows[-1]["股东名称"] += wrapped_prefix
            name = name[len(wrapped_prefix) :]
        if "境内自" in name and rows:
            wrapped_name, natural_person = name.split("境内自", 1)
            if wrapped_name:
                rows[-1]["股东名称"] += wrapped_name
            name = natural_person
        if name.endswith("有"):
            name += "限公司"
        elif name.endswith("有限") or name.endswith("集团有限"):
            name += "公司"
        if (
            len(name) < 3
            or name in seen_names
            or not is_valid_shareholding_name(name)
            or name.startswith("股东")
            or name.startswith("报告期末")
        ):
            continue
        seen_names.add(name)
        rows.append(
            {
                "股东名称": name,
                "股东性质": "",
                "持股比例": percentage,
            }
        )
        if len(rows) >= 10:
            break
    return rows

def is_valid_shareholding_name(name: str) -> bool:
    """过滤 PDF 错列后产生的明显残缺股东名称。"""
    value = re.sub(r"\s+", "", name or "")
    if not value or value.count("（") != value.count("）"):
        return False
    if any(
        marker in value
        for marker in (
            "境内非国",
            "有法人",
            "数量状态",
            "质押有",
            "限合伙）",
        )
    ):
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", value):
        return True
    if re.fullmatch(r"[A-Za-z0-9&.,·\-—]{3,100}", value):
        return True
    return any(
        marker in value
        for marker in (
            "公司",
            "企业",
            "合伙",
            "基金",
            "账户",
            "计划",
            "银行",
            "集团",
            "信托",
            "资金",
            "证券",
        )
    )

def extract_shareholding_rows(
    question: str,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从检索证据中提取前十大股东持股比例。"""
    if not any(word in question for word in ("股东", "持股", "股权")):
        return []
    pattern = re.compile(
        r"([\u4e00-\u9fffA-Za-z（）()·、\-－]{2,60}?)"
        r"(境内非国有法人|国有法人|境外法人|境内自然人|其他)"
        r"(\d+(?:\.\d+)?)%"
    )
    markers = ("前10名股东", "前十大股东", "公司股东数量及持股情况")
    tried_pages: set[tuple[str, int]] = set()
    for item in sources:
        page_key = (
            str(item.get("file_path") or ""),
            int(item.get("page_no") or 0),
        )
        if page_key not in tried_pages:
            tried_pages.add(page_key)
            pdf_rows = extract_shareholding_rows_from_pdf(item)
            if len(pdf_rows) >= 5:
                return pdf_rows
        content = str(item.get("content") or "")
        compact = re.sub(r"\s+", "", content)
        if not any(marker in compact for marker in markers):
            continue
        starts = [compact.find(marker) for marker in markers if marker in compact]
        start = min(starts) if starts else 0
        segment = compact[start : start + 8000]
        rows = extract_shareholding_rows_from_text(segment)
        if len(rows) >= 6:
            return rows
        rows = []
        seen_names: set[str] = set()
        for match in pattern.finditer(segment):
            name = match.group(1).strip("，,、：:")
            nature = match.group(2)
            percentage = float(match.group(3))
            if not name or name in seen_names:
                continue
            if any(word in name for word in ("股东名称", "前10名股东", "前十大股东")):
                continue
            seen_names.add(name)
            rows.append(
                {
                    "股东名称": name,
                    "股东性质": nature,
                    "持股比例": percentage,
                }
            )
            if len(rows) >= 10:
                break
        if len(rows) < 2:
            simple_pattern = re.compile(
                r"\d{1,2}([\u4e00-\u9fffA-Za-z（）()·、\-－]{2,80}?)"
                r"([\d,]+)(\d+(?:\.\d+)?)"
            )
            for match in simple_pattern.finditer(segment):
                name = match.group(1).strip("，,、：:")
                shares_text = match.group(2).replace(",", "")
                percentage = float(match.group(3))
                if (
                    not name
                    or name in seen_names
                    or name.startswith("合计")
                    or not shares_text.isdigit()
                    or percentage <= 0
                    or percentage > 100
                ):
                    continue
                seen_names.add(name)
                rows.append(
                    {
                        "股东名称": name,
                        "股东性质": "",
                        "持股比例": percentage,
                    }
                )
                if len(rows) >= 10:
                    break
        if len(rows) >= 6:
            return rows
    return []

def answer_with_overview(question: str) -> dict[str, Any] | None:
    """使用数据库统计值回答数量和记录数问题。"""
    compact = re.sub(r"\s+", "", question)
    count_words = (
        "多少家", "几家", "公司数量", "企业数量", "上市公司数量",
        "多少条", "几条", "多少记录", "多少份", "几份", "总数", "数据量",
    )
    if not any(word in compact for word in count_words):
        return None
    specs = [
        ("公司基础信息", "1_stock_basic", "家", ("公司", "企业", "上市公司")),
        ("业绩概览", "2_performance_report", "条", ("业绩", "业绩报表")),
        ("利润表", "3_income_statement", "条", ("利润表",)),
        ("资产负债表", "4_balance_sheet", "条", ("资产负债表",)),
        ("现金流量表", "5_cash_flow_statement", "条", ("现金流量表",)),
        ("文件库", "7_document_index", "份", ("文件库", "文件")),
    ]
    if any(word in compact for word in ("五张表", "五个表", "基本表", "数据表")):
        selected = specs[:5]
    else:
        selected = [
            spec
            for spec in specs
            if any(term in compact for term in spec[3])
        ]
    if not selected and "多少家" in compact:
        selected = [specs[0]]
    if not selected:
        return None
    rows = []
    for title, table, unit, _ in selected:
        result = query_all(f"SELECT COUNT(*) AS n FROM `{table}`")
        rows.append(
            {
                "数据范围": title,
                "有效记录数": int(result[0]["n"] if result else 0),
                "单位": unit,
            }
        )
    answer = "；".join(
        f"{row['数据范围']} {row['有效记录数']} {row['单位']}"
        for row in rows
    )
    return {
        "answer": f"当前数据库统计结果：{answer}。",
        "summary": f"当前数据库统计结果：{answer}。",
        "table": rows,
        "chart": None,
        "chart_note": "",
        "sources": [],
        "metrics": [],
        "key_points": [],
        "warnings": [],
        "mode": "local",
        "company_name": "",
    }

RANK_ORDER_WORDS = ("最高", "最多", "最大", "最强", "最好")
RANK_REVERSE_WORDS = ("最低", "最少", "最小", "最差", "倒数", "垫底")

def rank_report_period(text: str) -> str:
    """把问法里的报告期折算成库里 report_period 的写法。

    实测库内只有三种：2026年半年报 / 2025年年报 / 2024年年报（2_performance_report）。
    """
    text = expand_relative_time_words(text)
    year = re.search(r"(20\d{2})", text)
    if not year:
        return ""
    digit = year.group(1)
    if "半年" in text:
        return f"{digit}年半年报"
    if re.search(r"三季度|三季|Q3", text, re.I):
        return f"{digit}年三季度"
    if re.search(r"一季度|一季|Q1", text, re.I):
        return f"{digit}年一季报"
    if re.search(r"年报|年度", text):
        return f"{digit}年年报"
    return digit

def answer_cross_company_ranking(question: str, unit_mode: str = "自动") -> dict[str, Any] | None:
    """不带公司主体、只问「某指标最高/最低的前 N 家」时，直接给全库排行。

    原来这类问法会掉进澄清分支反问「市场/板块范围/口径」（实测 2026-10-27 23:52
    「2026年半年报里净利润最高的前5家公司是哪些？」mode=smart rows=0）。
    这里只统计**已入库**的公司，并在回答里把范围、口径、缺失家数说清楚。
    """
    compact = re.sub(r"\s+", "", question)
    if extract_stock_code(question):
        return None
    if not re.search(r"(排行|排名|榜|前十|前\d+|top\s*\d+)", compact, re.I) and not any(
        word in compact for word in RANK_ORDER_WORDS + RANK_REVERSE_WORDS
    ):
        return None
    metric = extract_metric(question)
    if not metric:
        return None
    table, column, label = metric
    if str(column).startswith("__"):
        return None  # 派生指标（如资产负债率）不做跨表排行，口径不明
    reverse = any(word in compact for word in RANK_REVERSE_WORDS)
    tail = re.search(r"(?:前|top)\s*(\d{1,3})", compact, re.I)
    limit = min(max(int(tail.group(1)) if tail else 5, 1), 20)
    period = rank_report_period(compact)
    where = [f"`{column}` IS NOT NULL"]
    params: list[Any] = []
    if period:
        where.append("report_period LIKE %s")
        params.append(f"{period}%")
    scope = " AND ".join(where)
    rows = query_all(
        "SELECT t.stock_code, b.stock_name, t.report_period, "
        f"t.`{column}` AS metric_value "
        f"FROM `{table}` t LEFT JOIN `1_stock_basic` b ON b.stock_code = t.stock_code "
        f"WHERE {scope} ORDER BY t.`{column}` {'ASC' if reverse else 'DESC'} LIMIT {limit}",
        tuple(params),
    )
    if not rows:
        return None
    total = query_all(
        f"SELECT COUNT(DISTINCT stock_code) AS n FROM `{table}` WHERE " + scope,
        tuple(params),
    )
    covered = int(total[0]["n"] if total else 0)
    table_rows = [
        {
            "排名": index,
            "股票代码": row.get("stock_code") or "",
            "公司名称": display_company_name(str(row.get("stock_code") or ""), str(row.get("stock_name") or "")),
            "报告期": row.get("report_period") or "",
            label: format_financial_metric(label, row.get("metric_value"), unit_mode),
        }
        for index, row in enumerate(rows, 1)
    ]
    period_text = period or "最新一期"
    direction = "最低" if reverse else "最高"
    answer = (
        f"{period_text} {label}{direction}的前 {len(rows)} 家："
        + "；".join(
            f"{item['公司名称']} {item[label]}"
            for item in table_rows
        )
        + f"。统计范围是**已入库公司**（该报告期有「{label}」数据的共 {covered} 家），"
        "不是全市场排名；口径按库里该字段原样（净利润=合并口径，归母净利润另算）。"
    )
    ranking_chart = {
        "type": chart_type_from_question(question, "bar"),
        "title": f"{period_text} {label}{direction}前 {len(rows)} 家",
        "x": "公司名称",
        "y": label,
        "data": [
            {"公司名称": item["公司名称"], label: chart_number(row.get("metric_value"))}
            for item, row in zip(table_rows, rows)
            if chart_number(row.get("metric_value")) is not None
        ],
    }
    if len(ranking_chart["data"]) < 2:
        ranking_chart = None
    elif ranking_chart.get("series") is None:
        # 排行路径自己造的图表绕过了 validate_chat_response 的本地化钩子，
        # 这里就地把 series 元数据补上，省得前端再猜单位（列名已是中文，attach 认得出）。
        try:
            ranking_chart = attach_chart_series_meta(ranking_chart)
        except Exception as exc:
            print(f"[图表] 排行 series 元数据附加失败（不影响出图）：{type(exc).__name__}: {exc}")
    return {
        "answer": answer,
        "summary": f"{period_text} {label}{direction}前 {len(rows)} 家（仅已入库公司）。",
        "table": table_rows,
        "chart": ranking_chart,
        "chart_note": "",
        "sources": [],
        "metrics": [
            {
                "label": label,
                "value": row.get("metric_value"),
                "formatted": table_rows[index][label],
                "report_period": row.get("report_period"),
                "series": [],
                "change": "",
            }
            for index, row in enumerate(rows)
        ][:3],
        "key_points": [
            f"第 1 名：{table_rows[0]['公司名称']} {table_rows[0][label]}",
            f"已入库且有该指标数据的公司：{covered} 家",
            "范围说明：只对已导入的财报排序，未导入的公司不参与。",
        ],
        "mode": "local",
        "company_name": "",
    }

def answer_with_local_rag(
    question: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """仅使用本地 RAG 证据生成回答。"""
    sources = retrieve_rag_chunks(question, top_k=20, request=request)
    if not sources:
        return None
    sources = filter_sources_by_locked_company(sources, request)
    if not sources:
        return None
    sources = prioritize_shareholding_sources(question, sources, request)
    sources = filter_sources_by_locked_company(sources, request)
    if not sources:
        return None
    company_code, company_name = identify_company_filter(question)
    locked = locked_company_from_request(request)
    if locked:
        company_code = locked.get("ticker", "")
        company_name = locked.get("name", "")
    if (
        not company_code
        and not company_name
        and not extract_stock_code(question)
        and not any(word in question for word in ("行业", "市场", "政策", "宏观经济"))
    ):
        return None
    compact_question = re.sub(r"\s+", "", question)
    query_terms = {
        compact_question[index : index + 2]
        for index in range(max(0, len(compact_question) - 1))
    }
    focus_words = [
        word
        for word in (
            "营业收入",
            "净利润",
            "资产",
            "负债",
            "现金流",
            "风险",
            "行业",
            "市场",
            "经营",
            "主营业务",
            "产品",
            "研发",
            "前景",
            "估值",
            "评估",
            "本次交易",
            "重大资产重组",
            "关联交易",
            "重组上市",
            "已履行",
            "尚需",
            "董事会",
            "股东会",
            "审批",
        )
        if word in question
    ]
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    report_types: set[str] = set()
    for item in sources:
        content = re.sub(r"\s+", "", str(item.get("content", "")))
        dot_count = content.count(".")
        visible_count = len(re.sub(r"[.·…\s]", "", content))
        if dot_count >= 12 and visible_count < len(content) * 0.62:
            continue
        for report_type in ("资产评估报告", "年度报告", "半年度报告", "行业研究报告"):
            if report_type in content:
                report_types.add(report_type)
        clauses = [
            clause.strip("，,、：: ")
            for clause in re.split(r"[。；;]", content)
            if clause.strip("，,、：: ")
        ]
        if len(clauses) == 1 and len(clauses[0]) > 260:
            anchor = next(
                (content.find(word) for word in focus_words if word in content),
                0,
            )
            clauses = [
                content[max(0, anchor - 80) : anchor + 180]
            ]
        for sentence in clauses:
            if len(sentence) < 12:
                continue
            if sentence.count(".") >= 4:
                continue
            score = sum(2 for word in focus_words if word in sentence)
            score += sum(0.15 for term in query_terms if term in sentence)
            score += sum(
                3
                for phrase in (
                    "因此，本次交易",
                    "本次交易构成重大资产重组",
                    "不构成关联交易",
                    "不构成重组上市",
                    "已履行的决策及审批程序",
                    "尚需履行的决策及审批程序",
                )
                if phrase in sentence
            )
            if "目录" in sentence or "释义" in sentence:
                score -= 4
            if any(
                word in sentence
                for word in ("不确定", "减值", "诉讼", "担保", "压力", "可能面临")
            ):
                score += 0.8
            cleaned = re.sub(
                r"(报告编码|合同编号|报告文号|共壹册|第壹册)[：:].*",
                "",
                sentence,
            ).strip("，,、：: ")
            if len(cleaned) < 12:
                continue
            snippet = cleaned[:180]
            candidates.append((score, snippet, item))
    candidates.sort(key=lambda item: item[0], reverse=True)
    points: list[str] = []
    snippets: list[str] = []
    seen = set()
    selected_candidates: list[tuple[float, str, dict[str, Any]]] = []

    def add_candidate(candidate: tuple[float, str, dict[str, Any]]) -> None:
        """添加未重复的候选片段。"""
        _, snippet, _ = candidate
        duplicate_key = re.sub(r"\W+", "", snippet)[:48]
        if duplicate_key in seen:
            return
        seen.add(duplicate_key)
        selected_candidates.append(candidate)

    if "审批" in question:
        nature_markers = ("重大资产重组", "关联交易", "重组上市")
        for candidate in candidates:
            snippet = candidate[1]
            if sum(marker in snippet for marker in nature_markers) >= 2:
                add_candidate(candidate)
            if len(selected_candidates) >= 2:
                break
        approval_count = 0
        for candidate in candidates:
            snippet = candidate[1]
            if any(
                marker in snippet
                for marker in ("已履行", "尚需", "审批程序", "董事会", "股东会")
            ):
                before = len(selected_candidates)
                add_candidate(candidate)
                if len(selected_candidates) > before:
                    approval_count += 1
            if approval_count >= 8:
                break
    for candidate in candidates:
        add_candidate(candidate)
        if len(selected_candidates) >= 12:
            break

    for _, snippet, item in selected_candidates:
        snippets.append(snippet)
        point = (
            (
                f"《{item.get('file_name', '已导入资料')}》"
                f"第{item.get('page_no', 0)}页：{snippet}"
            )
            if question_wants_page_reference(question)
            else snippet
        )
        points.append(point)
        if len(points) >= 4:
            break
    if not snippets:
        return None
    summary = "；".join(snippets[:12])
    warnings: list[str] = []
    if report_types:
        summary = f"当前资料属于{'、'.join(sorted(report_types))}。{summary}"
    combined_content = "".join(
        str(item.get("content") or "") for item in sources
    )
    if "审批" in question:
        compact_content = re.sub(r"\s+", "", combined_content)

        def approval_items(
            start_markers: tuple[str, ...],
            end_markers: tuple[str, ...],
        ) -> list[str]:
            """提取审批章节中的编号事项。"""
            start = min(
                (
                    compact_content.find(marker)
                    for marker in start_markers
                    if compact_content.find(marker) >= 0
                ),
                default=-1,
            )
            if start < 0:
                return []
            end = min(
                (
                    compact_content.find(marker, start + 1)
                    for marker in end_markers
                    if compact_content.find(marker, start + 1) >= 0
                ),
                default=len(compact_content),
            )
            section = compact_content[start:end]
            chunks = re.split(r"(?=\d+、)", section)
            cleaned_items = []
            for chunk in chunks:
                match = re.match(r"\d+、(.+)", chunk)
                if not match:
                    continue
                value = re.sub(r"\d{1,3}$", "", match.group(1)).strip("；;。")
                if len(value) >= 8:
                    cleaned_items.append(value)
            return cleaned_items

        completed = approval_items(
            (
                "（一）本次交易已履行的决策及审批程序",
                "(一)本次交易已履行的决策及审批程序",
            ),
            (
                "（二）本次交易尚需履行的决策及审批程序",
                "(二)本次交易尚需履行的决策及审批程序",
                "六、",
            ),
        )
        pending = approval_items(
            (
                "（二）本次交易尚需履行的决策及审批程序",
                "(二)本次交易尚需履行的决策及审批程序",
            ),
            ("上述决策及审批程序", "六、"),
        )
        approval_summary = []
        if completed:
            approval_summary.append(
                "已履行：" + "；".join(completed[:4])
            )
        if pending:
            approval_summary.append(
                "尚需履行：" + "；".join(pending[:3])
            )
        if approval_summary:
            nature_sentence = next(
                (
                    snippet
                    for snippet in snippets
                    if sum(
                        marker in snippet
                        for marker in ("重大资产重组", "关联交易", "重组上市")
                    )
                    >= 2
                ),
                summary,
            )
            summary = (
                f"{nature_sentence.rstrip('；')}；审批情况："
                + "；".join(approval_summary)
            )
    risk_question = "风险" in question
    direct_risk_evidence = bool(
        re.search(
            r"风险提示|可能面临|面临的风险|主要风险|重大风险|风险应对",
            combined_content,
        )
    )
    if risk_question and not direct_risk_evidence:
        if "资产评估报告" in report_types:
            summary = (
                "当前资料属于资产评估报告，检索内容主要涉及评估方法、"
                "收益预测和参数假设，未发现独立的公司经营风险章节。"
                "该资料不足以直接判断公司主要经营风险，建议补充年度报告或风险专项研报。"
            )
        else:
            summary = (
                "当前已导入资料中未检索到明确的经营风险披露，"
                "现有内容不足以直接形成风险结论。"
            )
        points = []
        warnings.append("风险问题缺少直接证据，当前仅提示资料局限。")
    elif "风险" in question and not any(
        "年度报告" in item or "半年度报告" in item for item in report_types
    ):
        warnings.append(
            "当前资料并非正式年报或风险专项报告，相关结论仅作线索参考。"
        )
    company_name = locked.get("name", "")
    if not company_name:
        for company in get_company_list():
            name = str(company.get("stock_name") or "")
            code = str(company.get("stock_code") or "")
            if (name and name in question) or (code and code in question):
                company_name = display_company_name(code, name)
                break
    shareholding_rows = extract_shareholding_rows(question, sources)
    table: list[dict[str, Any]] = []
    chart: dict[str, Any] | None = None
    if shareholding_rows:
        company_label = company_name or "该公司"
        summary = f"{company_label}前十大股东持股比例如下：" + "；".join(
            f"{row['股东名称']}持股{row['持股比例']:.2f}%"
            for row in shareholding_rows[:6]
        )
        points = [
            f"{row['股东名称']}：{row['持股比例']:.2f}%"
            for row in shareholding_rows[:8]
        ]
        table = shareholding_rows
        chart = {
            "type": chart_type_from_question(question, "bar"),
            "title": f"{company_label}前十大股东持股比例",
            "x": "股东名称",
            "y": "持股比例",
            "data": [
                {
                    "股东名称": row["股东名称"],
                    "持股比例": row["持股比例"],
                }
                for row in shareholding_rows
            ],
            "colors": ["#39c4b2", "#5b9cf6", "#d6a64f", "#e06c75", "#8b7cf6"],
        }
        answer_text = (
            f"根据已导入报告，{company_label}前十大股东持股比例如下。"
        )
    else:
        answer_text = (
            "根据已导入资料，检索到以下相关信息："
            f"{summary.rstrip('。')}。"
        )
    return {
        "answer": answer_text,
        "summary": answer_text,
        "table": table,
        "chart": chart,
        "chart_note": "",
        "sources": sources,
        "metrics": [],
        "key_points": points,
        "warnings": warnings,
        "mode": "local_rag",
        "company_name": company_name,
    }

def answer_with_operating_overview(
    question: str,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """使用主营业务专用规则生成经营概览，避免混入无关 RAG 片段。"""
    locked = locked_company_from_request(request)
    ticker = locked.get("ticker", "")
    if not ticker:
        return None
    company_name = locked.get("name") or ticker
    expanded_query = (
        f"{company_name} 报告期内公司从事的主要业务 主营业务分析 "
        "主要产品及用途 核心产品 业务模式 行业地位 分行业 分产品 "
        "营业收入 净利润 研发投入 核心竞争力"
    )
    sources = retrieve_rag_chunks(expanded_query, top_k=80, request=request)
    sources = filter_sources_by_locked_company(sources, request)
    if not sources:
        return build_scoped_not_found_response(
            company_name,
            "未在已上传文件中找到该公司的主要经营情况。",
        )
    narrative_markers = {
        "报告期内公司从事的主要业务": 10,
        "主营业务分析": 8,
        "主要产品及用途": 8,
        "公司主要从事": 7,
        "公司主营": 6,
        "上市公司主营": 6,
        "主要产品": 5,
        "业务模式": 5,
        "行业地位": 4,
        "核心竞争力": 4,
        "空管业务": 3,
        "微波射频业务": 3,
        "智能终端业务": 3,
        "低空经济": 3,
        "汽车电子": 3,
    }
    financial_markers = {
        "分行业": 3,
        "分产品": 3,
        "市场占有率": 3,
        "营业收入": 2,
        "净利润": 2,
        "研发投入": 2,
    }
    negative_markers = (
        "接待调研",
        "咨询公司生产经营情况",
        "会计政策",
        "财务报表附注",
        "企业所得税",
        "股东大会",
        "董事会决议",
        "公告编号",
        "募集资金",
        "股份增持",
        "签字",
    )
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for item in sources:
        content = re.sub(r"\s+", " ", str(item.get("content") or ""))
        if not content:
            continue
        if (
            any(marker in content for marker in negative_markers)
            and not any(
                marker in content
                for marker in ("报告期内公司从事的主要业务", "主营业务分析", "主要产品")
            )
        ):
            continue
        clauses = [
            clause.strip("，,、：:。；; ")
            for clause in re.split(r"[。；;\n]", content)
            if clause.strip("，,、：:。；; ")
        ]
        for clause in clauses:
            if not 24 <= len(clause) <= 360:
                continue
            clause = re.sub(
                rf"^{re.escape(company_name)}.*?(?:报告全文|报告摘要)",
                "",
                clause,
            ).strip()
            if len(clause) < 24:
                continue
            if clause.count("、") >= 5 or clause.count("）") >= 4:
                continue
            score = sum(
                weight
                for marker, weight in narrative_markers.items()
                if marker in clause
            )
            if score <= 0:
                continue
            score += sum(
                weight
                for marker, weight in financial_markers.items()
                if marker in clause
            )
            score -= sum(
                4 for marker in negative_markers if marker in clause
            )
            if score > 0:
                candidates.append((score, clause, item))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    page_counts: dict[tuple[str, int], int] = {}
    for _, clause, item in candidates:
        key = re.sub(r"[的\W]+", "", clause)[:72]
        page_key = (
            str(item.get("file_path") or ""),
            int(item.get("page_no") or 0),
        )
        if key in seen or page_counts.get(page_key, 0) >= 2:
            continue
        seen.add(key)
        page_counts[page_key] = page_counts.get(page_key, 0) + 1
        selected.append((clause, item))
        if len(selected) >= 4:
            break
    structured = answer_with_structured_data(
        f"{company_name} 营业收入 净利润 经营活动现金流",
        "自动",
        str(request.get("audience") or "综合分析师"),
    )
    if not selected and structured is None:
        return build_scoped_not_found_response(
            company_name,
            "未检索到可核验的主要业务描述或经营数据。",
        )
    parts = []
    if selected:
        parts.append("；".join(clause for clause, _ in selected))
    if structured and structured.get("answer"):
        parts.append(f"财务表现：{structured['answer']}")
    answer = f"{company_name}主要经营情况如下：{' '.join(parts)}"
    selected_sources = []
    source_ids = set()
    for _, item in selected:
        source_id = (
            str(item.get("file_path") or ""),
            int(item.get("page_no") or 0),
            int(item.get("chunk_index") or 0),
        )
        if source_id not in source_ids:
            source_ids.add(source_id)
            selected_sources.append(item)
    return {
        "answer": answer,
        "summary": answer,
        "table": structured.get("table", []) if structured else [],
        "chart": None,
        "chart_note": "",
        "sources": selected_sources,
        "metrics": structured.get("metrics", []) if structured else [],
        "key_points": [clause for clause, _ in selected],
        "warnings": (
            structured.get("warnings", [])
            if structured
            else ["未获取到结构化经营指标，仅展示已披露业务描述。"]
        ),
        "mode": "operating_overview",
        "company_name": company_name,
    }

def extract_risk_sections(
    sources: list[dict[str, Any]],
    request: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    """从完整风险章节中提取政策、市场、行业等主要风险。"""
    ticker = str(locked_company_from_request(request).get("ticker") or "")
    if not ticker or not sources:
        return []
    company_chunks = [
        item
        for item in RAG_CHUNKS
        if chunk_company_ticker(item) == ticker
    ]
    anchor = next(
        (
            item
            for item in company_chunks
            if "1.政策风险" in str(item.get("content") or "")
        ),
        None,
    )
    if anchor is None:
        anchor = next(
            (
                item
                for item in company_chunks
                if any(
                    marker in str(item.get("content") or "")
                    for marker in (
                        "可能面对的风险",
                        "风险因素及对策",
                        "2.市场风险",
                    )
                )
            ),
            None,
        )
    if not anchor:
        return []
    file_path = str(anchor.get("file_path") or "")
    scoped = [
        item
        for item in RAG_CHUNKS
        if chunk_company_ticker(item) == ticker
        and (
            not file_path
            or str(item.get("file_path") or "") == file_path
        )
    ]
    scoped.sort(
        key=lambda item: (
            int(item.get("page_no") or 0),
            int(item.get("chunk_index") or 0),
        )
    )
    anchor_key = (
        int(anchor.get("page_no") or 0),
        int(anchor.get("chunk_index") or 0),
    )
    start_index = next(
        (
            index
            for index, item in enumerate(scoped)
            if (
                int(item.get("page_no") or 0),
                int(item.get("chunk_index") or 0),
            )
            == anchor_key
        ),
        0,
    )
    stop_markers = (
        "(二)其他披露事项",
        "（二）其他披露事项",
        "第四节",
    )
    risk_text = ""
    previous_page = -1

    def merge_risk_text(left: str, right: str) -> str:
        """拼接有重叠的相邻 RAG 文本块。"""
        if not left:
            return right
        max_overlap = min(260, len(left), len(right))
        for size in range(max_overlap, 19, -1):
            if left[-size:] == right[:size]:
                return left + right[size:]
        return f"{left} {right}"

    for item in scoped[start_index:]:
        page_no = int(item.get("page_no") or 0)
        if risk_text and page_no > previous_page + 1:
            break
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content:
            continue
        stop_positions = [
            content.find(marker)
            for marker in stop_markers
            if content.find(marker) >= 0
        ]
        if risk_text and stop_positions:
            risk_text = merge_risk_text(
                risk_text,
                content[: min(stop_positions)],
            )
            break
        risk_text = merge_risk_text(risk_text, content)
        previous_page = page_no
        if risk_text and any(marker in risk_text for marker in stop_markers):
            break
    if not risk_text:
        return []
    start_positions = [
        risk_text.find(marker)
        for marker in ("可能面对的风险", "风险因素及对策", "风险应对措施")
        if risk_text.find(marker) >= 0
    ]
    if start_positions:
        risk_text = risk_text[min(start_positions) :]
    end_positions = [
        risk_text.find(marker)
        for marker in stop_markers
        if risk_text.find(marker) >= 0
    ]
    if end_positions:
        risk_text = risk_text[: min(end_positions)]

    heading_pattern = re.compile(
        r"(?<!\d)([1-9]\.\s*(?:政策|市场|行业|财务|经营|管理|投资|"
        r"法律|合规|汇率|安全|项目|资金|其他)[^。；;\n]{0,8}风险)"
    )
    matches = list(heading_pattern.finditer(risk_text))
    sections: list[tuple[str, str, dict[str, Any]]] = []
    for index, match in enumerate(matches):
        label = re.sub(r"^[1-9]\.", "", re.sub(r"\s+", "", match.group(1)))
        body_start = match.end()
        body_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(risk_text)
        )
        body = risk_text[body_start:body_end].strip("，,、：:；;。 ")
        for marker in (
            "为应对",
            "对此",
            "公司将",
            "公司已",
            "公司未来将",
            "公司将持续",
            "公司将继续",
            "公司积极",
            "公司加强",
        ):
            position = body.find(marker)
            if position >= 40:
                body = body[:position].strip("，,、：:；;。 ")
                break
        body = re.sub(r"\s+", "", body)
        body = re.sub(r"(?:为此|对此)$", "", body).strip("，,、：:；;。 ")
        if len(body) < 24:
            continue
        if len(body) > 320:
            body = body[:320].rsplit("。", 1)[0] + "。"
        source = next(
            (
                item
                for item in sources
                if body[:24]
                and body[:24] in re.sub(r"\s+", "", str(item.get("content") or ""))
            ),
            sources[0],
        )
        sections.append((label, body, source))
    return sections[:6]

def answer_with_risk_analysis(
    question: str,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """使用风险章节专用规则生成风险回答。"""
    locked = locked_company_from_request(request)
    ticker = locked.get("ticker", "")
    if not ticker:
        return None
    company_name = locked.get("name") or ticker
    # 检索词里必须带上**披露章节的标题原词**。实测（云南白药 000538 2026 年半年报）：
    # 风险章节标题是「十、公司面临的风险和应对措施」，正文是「（一）政策变化风险 / （二）经营风险 /
    # （三）环保风险 / （四）安全风险 / （六）投资风险」这类小节。旧检索词只有「主要经营风险 风险提示
    # …不确定性」，除「可能面临」外**没有一个能在这页对上字面**，本地词面打分拿不到分，
    # 只能靠 512 维本地向量去捞，而风险章节的向量距离并不占优（实测第 68 名）⇒
    # top_k=80 的候选池里根本没有它 ⇒ `if score > 0`（retrieve_rag_chunks 末尾那条硬过滤）把它筛掉
    # ⇒ 前端「未检索到相关文本」。补上标题原词后，词面分把它抬进前 12（实测第 2 名）。
    sources = retrieve_rag_chunks(
        (
            f"{company_name} 公司面临的风险和应对措施 可能面对的风险 面临的风险 "
            "主要经营风险 风险应对措施 政策变化风险 经营风险 市场风险 行业风险 "
            "环保风险 安全风险 投资风险 汇率风险 管理风险 不确定性"
        ),
        top_k=80,
        request=request,
    )
    sources = filter_sources_by_locked_company(sources, request)
    if not sources:
        return build_scoped_not_found_response(
            company_name,
            "未在已上传文件中找到该公司的经营风险披露。",
        )
    risk_sections = extract_risk_sections(sources, request)
    if risk_sections:
        answer = f"{company_name}已披露的主要经营风险如下：" + "；".join(
            f"{label}：{body.rstrip('。；;')}。"
            for label, body, _ in risk_sections
        )
        selected_sources = []
        source_keys = set()
        for _, _, item in risk_sections:
            key = (
                str(item.get("file_path") or ""),
                int(item.get("page_no") or 0),
                int(item.get("chunk_index") or 0),
            )
            if key in source_keys:
                continue
            source_keys.add(key)
            selected_sources.append(item)
        return {
            "answer": answer,
            "summary": answer,
            "table": [],
            "chart": None,
            "chart_note": "",
            "sources": selected_sources,
            "metrics": [],
            "key_points": [
                f"{label}：{body.rstrip('。；;')}。"
                for label, body, _ in risk_sections
            ],
            "warnings": [],
            "mode": "risk_analysis",
            "company_name": company_name,
        }
    risk_markers = (
        "风险提示",
        "主要风险",
        "重大风险",
        "可能面临",
        "面临的风险",
        "风险应对",
        "风险因素",
        "不确定性",
        "不利影响",
        "应对措施",
    )
    negative_markers = (
        "接待调研",
        "咨询公司生产经营情况",
        "会计政策",
        "审计报告",
        "商品.*风险和报酬",
        "合同资产",
        "金融工具",
        "公允价值",
        "公司已在本报告中详细描述",
        "公司在本报告中详细阐述",
        "公司治理",
        "股东大会",
        "请各位股东",
        "请投资者查阅",
        "公司在本报告中详细阐述",
        "主要控股参股公司情况说明",
        "本公司已制定风险管理政策",
        "其他应付款",
        "所得税",
        "财务报表",
    )
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for item in sources:
        content = re.sub(r"\s+", " ", str(item.get("content") or ""))
        if any(re.search(marker, content) for marker in negative_markers):
            continue
        for clause in re.split(r"[。；;\n]", content):
            clause = clause.strip("，,、：:。；; ")
            if not 24 <= len(clause) <= 360:
                continue
            score = sum(
                3 if marker in clause else 0
                for marker in risk_markers
            )
            if score <= 0:
                continue
            if any(marker in clause for marker in ("可能", "预计", "存在", "面临")):
                score += 1
            candidates.append((score, clause, item))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for _, clause, item in candidates:
        key = re.sub(r"\W+", "", clause)[:72]
        if key in seen:
            continue
        seen.add(key)
        selected.append((clause, item))
        if len(selected) >= 5:
            break
    if not selected:
        return build_scoped_not_found_response(
            company_name,
            "未检索到可核验的经营风险披露。",
        )
    answer = (
        f"{company_name}已披露的主要经营风险如下："
        + "；".join(clause for clause, _ in selected)
    )
    selected_sources = []
    source_ids = set()
    for _, item in selected:
        source_id = (
            str(item.get("file_path") or ""),
            int(item.get("page_no") or 0),
            int(item.get("chunk_index") or 0),
        )
        if source_id not in source_ids:
            source_ids.add(source_id)
            selected_sources.append(item)
    return {
        "answer": answer,
        "summary": answer,
        "table": [],
        "chart": None,
        "chart_note": "",
        "sources": selected_sources,
        "metrics": [],
        "key_points": [clause for clause, _ in selected],
        "warnings": [],
        "mode": "risk_analysis",
        "company_name": company_name,
    }

def build_scoped_not_found_response(
    company_name: str,
    message: str,
) -> dict[str, Any]:
    """在专用处理器失败时返回明确的资料不足结果。"""
    answer = message or "未在已上传文件中找到可核验信息。"
    return {
        "answer": answer,
        "summary": answer,
        "table": [],
        "chart": None,
        "chart_note": "",
        "sources": [],
        "metrics": [],
        "key_points": [],
        "warnings": [],
        "mode": "guide",
        "company_name": company_name,
    }

def repair_sql_using_llm(
    question: str,
    failed_sql: str,
    error_message: str,
    request: dict[str, Any],
) -> str:
    """让模型修复执行失败的 SQL。"""
    # 离线模式绝不能构造云端 client：实测 `api_key=""` 时 OpenAI SDK 抛
    # `Missing credentials. Please pass an `api_key`…`，被 run_with_correction 记成
    # repair_failed，于是评测/日志里把这条第三方文案当成了「修复失败原因」；
    # 更糟的是若机器上恰好有 OPENAI_API_KEY，这条路径会静默打到 api.openai.com 花别人的钱。
    if not str(request.get("api_key") or "").strip():
        raise ValueError("本地规则未能修复，当前为离线模式，不进行云端修复")
    client = make_client(request.get("api_key", ""), request.get("base_url", ""))
    prompt = SQL_SELF_CORRECTION_PROMPT.format(
        question=question,
        wrong_sql=failed_sql,
        error_msg=error_message,
    )
    response = client.chat.completions.create(
        model=request.get("llm_model", DEEPSEEK_MODEL),
        messages=[
            {"role": "system", "content": prompt},
        ],
        temperature=0,
    )
    repaired = (response.choices[0].message.content or "").strip()
    repaired = re.sub(r"^```(?:sql)?|```$", "", repaired, flags=re.I | re.M).strip()
    repaired = re.sub(r"^\s*SQL\s*[:：]\s*", "", repaired, flags=re.I)
    repaired = repaired.rstrip()
    while repaired.endswith(";"):
        repaired = repaired[:-1].rstrip()
    return validate_sql_safety(repaired)

def canonical_report_period_from_question(question: str) -> str:
    """把问题中的常用报告期表达转换为数据库口径。"""
    compact = expand_relative_time_words(re.sub(r"\s+", "", question or ""))
    year_match = re.search(r"(20\d{2})年", compact)
    if not year_match:
        return ""
    year = year_match.group(1)
    if any(word in compact for word in ("半年报", "半年度", "中期")):
        return f"{year}年半年报"
    if any(word in compact for word in ("年报", "年度报告")):
        return f"{year}年年报"
    return ""

PERIOD_LITERAL_RE = re.compile(
    r"((?:[A-Za-z_][A-Za-z0-9_]*\.)?report_period)\s*(?:=|LIKE)\s*'([^']*)'",
    flags=re.I,
)

def canonical_period_from_literal(raw: str) -> str:
    """把 SQL 里各种写法的报告期字面量归一到库内口径（实测库里只有 2026年半年报 / 2025年年报 / 2024年年报）。

    只认「明确带月或带半年/全年字样」的写法；`2026%`、`2026` 这种**只到年**的不动，
    因为它在库里 LIKE 得到、改了反而缩小范围。
    """
    text = re.sub(r"\s+", "", str(raw or ""))
    year_match = re.search(r"(20\d{2})", text)
    if not year_match:
        return ""
    year = year_match.group(1)
    if re.search(r"半年报|半年度|中期|H1|Q2", text, flags=re.I):
        return f"{year}年半年报"
    if re.search(r"年报|年度报告|全年|H2|Q4", text, flags=re.I):
        return f"{year}年年报"
    digits = re.findall(r"\d{1,2}", text.split(year, 1)[1] if year in text else "")
    month = 0
    if re.search(r"[-/.]", text) or "%" in text or len(re.sub(r"\D", "", text)) == 8:
        for chunk in digits:
            if 1 <= int(chunk) <= 12:
                month = int(chunk)
                break
    if not month:
        return ""
    return f"{year}年半年报" if month <= 6 else f"{year}年年报"

def normalize_period_literals(sql: str, question: str = "") -> str:
    """执行前把 `report_period='2026-06-30'` 这类 ISO 写法改写成库内中文口径。

    实测（2026-10-28，_tools\\_hist\\trace_flaky_gm.log）：同一题连问 6 次，
    模型 5 次写出 `2026-06-30` / `LIKE '2026-06%'` / `LIKE '2026%06%'`，
    全部要靠 attempts=2 的事后纠错才救回来；纠错不成立时就退化成「未查询到匹配数据」，
    表现为**同一题一次有一次没有**的 flaky。归一放在执行前，一次命中。
    """
    statement = str(sql or "")
    if "report_period" not in statement.lower():
        return statement
    fallback = canonical_report_period_from_question(question)

    def _replace(match: re.Match[str]) -> str:
        column, raw = match.group(1), match.group(2)
        canonical = canonical_period_from_literal(raw)
        if not canonical and re.search(r"[-/.]\d{1,2}|%\d{1,2}%|\d{8}", raw):
            canonical = fallback
        if not canonical or canonical == raw:
            return match.group(0)
        return f"{column}='{canonical}'"

    return PERIOD_LITERAL_RE.sub(_replace, statement)

def make_client(api_key: str, base_url: str) -> OpenAI:
    """创建 OpenAI 客户端。"""
    return OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), timeout=120)

def configure_analysis_mode(request: dict[str, Any]) -> dict[str, Any]:
    """固定 DeepSeek 配置，并按模式从环境变量注入密钥。

    实测坑（2026-10-27）：响应里 `mode` 写的是 `"smart"`（见 handle_chat_request
    与 local_fallback 两处），请求侧过去只认 `"deepseek"`，客户端把响应里的 `mode`
    原样回填就会被**静默降级成本地模式**（实测 0.3s、`mode=local`、`warnings=[]`，
    而正确写法 9.4s、`mode=smart`）⇒ 这里把 smart/llm/api 与含 deepseek 的写法都
    归一到智能模式，别让用户以为自己在用智能分析。
    """
    raw_mode = str(request.get("analysis_mode") or "").strip().lower()
    collapsed = raw_mode.replace("-", "").replace("_", "").replace(" ", "")
    if collapsed in {"smart", "llm", "api", "deepseek", "intelligent"} or "deepseek" in collapsed:
        mode = "deepseek"
    else:
        mode = "local"
    request["analysis_mode"] = mode
    request["base_url"] = DEEPSEEK_BASE_URL
    request["llm_model"] = DEEPSEEK_MODEL
    request["embedding_model"] = LOCAL_EMBEDDING_MODEL
    if mode == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise ValueError("未检测到 DEEPSEEK_API_KEY 环境变量")
        request["api_key"] = api_key
    else:
        request["api_key"] = ""
    return request

def parse_json_from_text(text: str) -> dict[str, Any]:
    """从模型回复中解析 JSON。"""
    text = (text or "").strip()
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("模型没有返回有效 JSON")
    return json.loads(match.group(0))

def normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """规范化自主规划结果，兼容原有单任务格式。"""
    normalized = dict(plan or {})
    raw_subtasks = normalized.get("subtasks")
    subtasks = []
    if isinstance(raw_subtasks, list):
        for index, raw in enumerate(raw_subtasks, start=1):
            if not isinstance(raw, dict):
                continue
            company = raw.get("company")
            if not isinstance(company, dict):
                company = {}
            ticker = str(
                company.get("ticker")
                or company.get("stock_code")
                or raw.get("ticker")
                or raw.get("stock_code")
                or ""
            ).strip()
            name = str(
                company.get("name")
                or company.get("stock_name")
                or raw.get("company_name")
                or ""
            ).strip()
            kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
            if not kind:
                kind = "sql" if raw.get("sql") else "rag" if raw.get("rag_query") else ""
            task = {
                "id": str(raw.get("id") or f"task_{index}").strip() or f"task_{index}",
                "kind": kind,
                "purpose": str(raw.get("purpose") or "").strip(),
                "depends_on": [
                    str(item).strip()
                    for item in raw.get("depends_on") or []
                    if str(item).strip()
                ],
                "company": {"ticker": ticker, "name": name or ticker} if ticker else {},
                "sql": str(raw.get("sql") or "").strip(),
                "rag_query": str(raw.get("rag_query") or raw.get("query") or "").strip(),
                "expect_columns": [
                    str(item).strip()
                    for item in raw.get("expect_columns") or []
                    if str(item).strip()
                ],
            }
            subtasks.append(task)
    if subtasks:
        normalized["subtasks"] = subtasks
        if str(normalized.get("action") or "").strip().lower() not in {
            "sql",
            "rag",
            "hybrid",
            "answer",
            "plan",
        }:
            normalized["action"] = "plan"
    return normalized

def validate_plan_graph(subtasks: list[dict[str, Any]]) -> list[str]:
    """校验子任务依赖图，返回可修复的错误列表。"""
    errors: list[str] = []
    ids = [str(item.get("id") or "") for item in subtasks]
    if any(not item for item in ids):
        errors.append("每个子任务都必须有唯一 id")
    if len(ids) != len(set(ids)):
        errors.append("子任务 id 不能重复")
    id_set = set(ids)
    dependencies = {
        str(item.get("id")): [
            str(dep) for dep in item.get("depends_on") or [] if str(dep).strip()
        ]
        for item in subtasks
    }
    for task_id, deps in dependencies.items():
        missing = [dep for dep in deps if dep not in id_set]
        if missing:
            errors.append(f"{task_id} 依赖不存在的子任务：{', '.join(missing)}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.append(f"子任务依赖存在环：{task_id}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in dependencies.get(task_id, []):
            if dep in id_set:
                visit(dep)
        visiting.discard(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)
    return errors

def validate_plan(
    plan: dict[str, Any],
    request: dict[str, Any] | None = None,
) -> list[str]:
    """校验规划结果、SQL 安全边界和子任务依赖关系。"""
    normalized = normalize_plan(plan)
    action = str(normalized.get("action") or "").strip().lower()
    errors: list[str] = []
    subtasks = normalized.get("subtasks") or []
    if action == "plan" or subtasks:
        if not subtasks:
            return ["完整规划必须至少包含一个子任务"]
        errors.extend(validate_plan_graph(subtasks))
        if not any(str(task.get("kind") or "").lower() == "sql" for task in subtasks):
            errors.append("完整规划至少需要一个 SQL 子任务")
        for task in subtasks:
            kind = str(task.get("kind") or "").lower()
            task_id = task.get("id") or "未命名任务"
            if kind == "sql":
                if not task.get("sql"):
                    errors.append(f"{task_id} 缺少 SQL")
                    continue
                try:
                    validate_sql_safety(str(task["sql"]))
                except Exception as exc:
                    errors.append(f"{task_id} SQL 不安全：{exc}")
            elif kind == "rag":
                if not task.get("rag_query"):
                    errors.append(f"{task_id} 缺少检索问题")
            else:
                errors.append(f"{task_id} 的任务类型无效")
        comparison_companies = comparison_companies_from_request(request)
        if comparison_companies:
            expected = {
                str(item.get("ticker") or "")
                for item in comparison_companies
                if item.get("ticker")
            }
            covered = {
                str((task.get("company") or {}).get("ticker") or "")
                for task in subtasks
                if str(task.get("kind") or "").lower() == "sql"
            }
            missing = expected - covered
            extra = covered - expected - {""}
            if extra:
                errors.append(
                    f"比较计划包含未请求的公司：{', '.join(sorted(extra))}"
                )
            if missing:
                errors.append(
                    f"比较计划缺少公司子任务：{', '.join(sorted(missing))}"
                )
        return errors
    if action not in {"sql", "rag", "hybrid", "answer"}:
        errors.append("action 必须是 sql、rag、hybrid、answer 或 plan")
    if action in {"sql", "hybrid"} and not normalized.get("sql"):
        errors.append("SQL 类计划缺少 sql")
    if action in {"rag", "hybrid"} and not normalized.get("rag_query"):
        errors.append("检索类计划缺少 rag_query")
    return errors

def repair_plan_with_llm(
    question: str,
    invalid_plan: dict[str, Any],
    errors: list[str],
    request: dict[str, Any],
) -> dict[str, Any]:
    """让模型修复结构错误或不安全的规划结果。"""
    client = make_client(request.get("api_key", ""), request.get("base_url", ""))
    prompt = (
        "你是财报问数规划器的修复器。只输出合法 JSON，不要 Markdown。\n"
        "需要支持单任务和完整子任务计划。多公司比较必须为每家公司拆分 SQL 子任务。\n"
        f"【可用表结构】\n{FINANCIAL_SCHEMA_PROMPT}\n"
        f"【原始问题】\n{question}\n"
        f"【错误规划】\n{json.dumps(invalid_plan, ensure_ascii=False)}\n"
        f"【校验错误】\n{'; '.join(errors)}\n"
        "【输出格式】\n"
        '{"action":"plan","intent":"multi_company_comparison","companies":[...],'
        '"subtasks":[{"id":"sql_company","kind":"sql","company":{"ticker":"","name":""},'
        '"depends_on":[],"sql":"SELECT ...","purpose":""}],'
        '"combine":{"mode":"wide"},"chart":{"type":"bar","x":"公司名称","y":"指标","title":""}}'
    )
    response = client.chat.completions.create(
        model=request.get("llm_model", DEEPSEEK_MODEL),
        messages=[{"role": "system", "content": prompt}],
        temperature=0,
    )
    return parse_json_from_text(response.choices[0].message.content or "")

def run_with_correction(
    initial_value: Any,
    execute: Any,
    validate: Any,
    repair: Any,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """执行、验证、修复的通用闭环。"""
    value = initial_value
    log: list[dict[str, Any]] = []
    last_result: Any = None
    last_error = ""
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            last_result = execute(value)
            last_error = str(validate(value, last_result) or "").strip()
            if not last_error:
                return {
                    "ok": True,
                    "value": value,
                    "result": last_result,
                    "attempts": attempt,
                    "log": log,
                }
        except Exception as exc:
            last_error = str(exc).strip() or exc.__class__.__name__
        log.append(
            {
                "attempt": attempt,
                "value": value,
                "status": "failed",
                "error": last_error,
            }
        )
        if attempt >= max_attempts:
            break
        try:
            value = repair(value, last_error)
        except Exception as exc:
            log.append(
                {
                    "attempt": attempt,
                    "value": value,
                    "status": "repair_failed",
                    "error": str(exc),
                }
            )
            break
    return {
        "ok": False,
        "value": value,
        "result": last_result,
        "attempts": len(log),
        "error": last_error,
        "log": log,
    }

def repair_sql_for_missing_metrics(
    sql: str,
    question: str,
    error: str,
    request: dict[str, Any],
) -> str:
    """空结果优先尝试把可选指标 JOIN 放宽为 LEFT JOIN。"""
    statement = re.sub(r"\s+", " ", sql or "").strip()
    period = canonical_report_period_from_question(question)
    if period and "report_period" in statement.lower() and "空" in error:
        period_statement = re.sub(
            r"((?:[A-Za-z_][A-Za-z0-9_]*\.)?report_period)"
            r"\s*(?:=|LIKE)\s*'[^']*'",
            rf"\1='{period}'",
            statement,
            flags=re.I,
        )
        if period_statement != statement:
            return validate_sql_safety(period_statement)
    relaxed = re.sub(r"\bINNER\s+JOIN\b", "LEFT JOIN", statement, flags=re.I)
    relaxed = re.sub(
        r"(?<!LEFT )\bJOIN\b",
        "LEFT JOIN",
        relaxed,
        flags=re.I,
    )
    if re.sub(r"\s+", " ", relaxed).strip() != statement:
        return validate_sql_safety(relaxed)
    return repair_sql_using_llm(question, sql, error, request)

def filter_rows_for_company(
    rows: list[dict[str, Any]],
    ticker: str,
) -> list[dict[str, Any]]:
    """过滤多公司 SQL 中不属于当前子任务公司的行。"""
    if not ticker:
        return rows
    filtered = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_ticker = str(
            row.get("stock_code")
            or row.get("股票代码")
            or ""
        ).strip()
        if not row_ticker or row_ticker == ticker:
            filtered.append(row)
    return filtered

def execute_sql_subtask_with_correction(
    subtask: dict[str, Any],
    question: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """执行单个 SQL 子任务，失败时执行安全校验和模型修复。"""
    company = subtask.get("company") if isinstance(subtask.get("company"), dict) else {}
    ticker = str(company.get("ticker") or "").strip()
    expected_columns = {
        str(item).lower()
        for item in subtask.get("expect_columns") or []
        if str(item).strip()
    }

    def validate_sql_result(sql: str, rows: Any) -> str:
        if not isinstance(rows, list):
            return "SQL 未返回列表结果"
        if not rows:
            return "SQL 查询结果为空"
        missing = expected_columns - {
            str(key).lower() for row in rows if isinstance(row, dict) for key in row
        }
        if missing:
            return f"SQL 结果缺少字段：{', '.join(sorted(missing))}"
        if ticker:
            codes = {
                str(row.get("stock_code") or "").strip()
                for row in rows
                if isinstance(row, dict) and row.get("stock_code") is not None
            }
            foreign_codes = {code for code in codes if code and code != ticker}
            if foreign_codes:
                return f"SQL 结果混入了其他公司：{', '.join(sorted(foreign_codes))}"
        return ""

    corrected = run_with_correction(
        normalize_period_literals(str(subtask.get("sql") or ""), question),
        lambda sql: filter_rows_for_company(
            query_all(validate_sql_safety(sql)),
            ticker,
        ),
        validate_sql_result,
        lambda sql, error: repair_sql_for_missing_metrics(
            sql,
            question,
            error,
            request,
        ),
        max_attempts=(
            2
            if (
                re.search(
                    r"\bJOIN\b",
                    str(subtask.get("sql") or ""),
                    flags=re.I,
                )
                or "report_period"
                in str(subtask.get("sql") or "").lower()
            )
            else 1
        ),
    )
    corrected_rows = corrected.get("result") or []
    if corrected.get("ok"):
        result_status = "completed"
        result_error = ""
    elif isinstance(corrected_rows, list) and not corrected_rows:
        result_status = "empty"
        result_error = ""
    else:
        result_status = "failed"
        result_error = corrected.get("error") or "SQL 执行失败"
    return {
        "subtask_id": str(subtask.get("id") or ""),
        "kind": "sql",
        "company": company,
        "purpose": subtask.get("purpose") or "",
        "status": result_status,
        "attempts": corrected.get("attempts", 0),
        "sql": corrected.get("value") or subtask.get("sql") or "",
        "rows": corrected_rows,
        "sources": [],
        "error": result_error,
        "correction_log": corrected.get("log") or [],
    }

def execute_plan_subtasks(
    plan: dict[str, Any],
    question: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """按依赖关系调度子任务，并返回完整执行轨迹。"""
    normalized = normalize_plan(plan)
    subtasks = normalized.get("subtasks") or []
    if not subtasks:
        return {"results": [], "trace": [], "warnings": ["规划没有可执行子任务"]}
    pending = {str(item.get("id")): item for item in subtasks}
    completed: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    warnings: list[str] = []
    while pending:
        ready = [
            task
            for task in pending.values()
            if all(dep in completed for dep in task.get("depends_on") or [])
        ]
        if not ready:
            warnings.append("子任务依赖无法满足，已停止调度")
            break
        with ThreadPoolExecutor(max_workers=min(4, len(ready))) as executor:
            future_map = {}
            for task in ready:
                future_map[
                    executor.submit(
                        execute_single_subtask,
                        task,
                        question,
                        request,
                    )
                ] = task
            for future in as_completed(future_map):
                task = future_map[future]
                task_id = str(task.get("id"))
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "subtask_id": task_id,
                        "kind": task.get("kind") or "unknown",
                        "company": task.get("company") or {},
                        "status": "failed",
                        "attempts": 1,
                        "rows": [],
                        "sources": [],
                        "error": str(exc),
                        "correction_log": [],
                    }
                completed[task_id] = result
                pending.pop(task_id, None)
                trace.append(result)
                warnings.extend(
                    str(item)
                    for item in result.get("warnings") or []
                    if str(item).strip()
                )
                if result.get("status") == "empty":
                    warnings.append(f"{task_id} 无匹配数据")
                elif result.get("status") != "completed":
                    warnings.append(
                        f"{task_id} 执行失败：{result.get('error') or '未知错误'}"
                    )
    return {
        "results": [completed[str(item.get("id"))] for item in subtasks if str(item.get("id")) in completed],
        "trace": trace,
        "warnings": warnings,
    }

def execute_single_subtask(
    subtask: dict[str, Any],
    question: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """执行一个已就绪的结构化查询或 RAG 子任务。"""
    kind = str(subtask.get("kind") or "").lower()
    if kind == "sql":
        task_log(
            request,
            {
                "type": "subtask_start",
                "subtask_id": str(subtask.get("id") or ""),
                "kind": "sql",
                "company": (
                    subtask.get("company") or {}
                ).get("name")
                or (subtask.get("company") or {}).get("ticker")
                or "",
                "sql": str(subtask.get("sql") or ""),
            },
        )
        started = time.perf_counter()
        result = execute_sql_subtask_with_correction(subtask, question, request)
        result["duration"] = round(time.perf_counter() - started, 2)
        task_log(
            request,
            {
                "type": "subtask_done",
                "subtask_id": result.get("subtask_id", ""),
                "kind": "sql",
                "company": (
                    result.get("company") or {}
                ).get("name")
                or (result.get("company") or {}).get("ticker")
                or "",
                "status": result.get("status", ""),
                "rows": len(result.get("rows") or []),
                "attempts": result.get("attempts", 0),
                "duration": result.get("duration", 0),
                "sql": result.get("sql", ""),
                # 界面日志用这句「人话」当主文案；raw SQL 收进折叠区。
                "purpose": str(result.get("purpose") or subtask.get("purpose") or ""),
                "error": result.get("error", ""),
            },
        )
        return result
    if kind == "rag":
        task_log(
            request,
            {
                "type": "subtask_start",
                "subtask_id": str(subtask.get("id") or ""),
                "kind": "rag",
                "company": str(subtask.get("purpose") or "资料检索"),
            },
        )
        started = time.perf_counter()
        company = subtask.get("company") if isinstance(subtask.get("company"), dict) else {}
        companies = rag_targets_for_request(request, company)
        sources = []
        source_keys = set()
        for target in companies:
            scoped_request = dict(request)
            scoped_request["locked_company"] = dict(target)
            for item in retrieve_rag_chunks(
                str(subtask.get("rag_query") or question),
                top_k=12,
                request=scoped_request,
            ):
                key = (
                    str(item.get("file_path") or ""),
                    int(item.get("page_no") or 0),
                    int(item.get("chunk_index") or 0),
                )
                if key in source_keys:
                    continue
                source_keys.add(key)
                sources.append(item)
        warnings = [] if sources else ["未检索到相关资料，已继续使用结构化数据。"]
        result = {
            "subtask_id": str(subtask.get("id") or ""),
            "kind": "rag",
            "company": company,
            "purpose": subtask.get("purpose") or "",
            "status": "completed",
            "attempts": 1,
            "rows": [],
            "sources": sources,
            "error": "",
            "warnings": warnings,
            "correction_log": [],
        }
        result["duration"] = round(time.perf_counter() - started, 2)
        task_log(
            request,
            {
                "type": "subtask_done",
                "subtask_id": result["subtask_id"],
                "kind": "rag",
                "company": str(
                    company.get("name")
                    or company.get("ticker")
                    or "资料检索"
                ),
                "status": result["status"],
                "rows": 0,
                "sources": len(sources),
                "attempts": 1,
                "duration": result["duration"],
                "purpose": str(result.get("purpose") or ""),
                "error": result.get("error", ""),
            },
        )
        return result
    return {
        "subtask_id": str(subtask.get("id") or ""),
        "kind": kind or "unknown",
        "company": subtask.get("company") or {},
        "status": "failed",
        "attempts": 0,
        "rows": [],
        "sources": [],
        "error": "不支持的子任务类型",
        "correction_log": [],
    }

def unified_table_from_plan_results(
    execution: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """把多个公司或指标子任务的结果合并成同一口径表。"""
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    warnings = list(execution.get("warnings") or [])
    for item in execution.get("results") or []:
        if item.get("status") not in {"completed", "empty"}:
            continue
        sources.extend(item.get("sources") or [])
        company = item.get("company") if isinstance(item.get("company"), dict) else {}
        ticker = str(company.get("ticker") or "").strip()
        name = str(company.get("name") or "").strip()
        for raw_row in item.get("rows") or []:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            row_ticker = str(
                row.get("stock_code")
                or row.get("股票代码")
                or ticker
                or ""
            ).strip()
            row_name = str(
                row.get("stock_name")
                or row.get("公司名称")
                or name
                or ""
            ).strip()
            if row_ticker:
                row["stock_code"] = row_ticker
            if row_name:
                row["stock_name"] = row_name
            rows.append(row)
    if not rows:
        return [], sources, warnings

    metadata_keys = {
        "stock_code",
        "股票代码",
        "stock_name",
        "公司名称",
        "report_period",
        "报告期",
    }
    metric_keys = {"metric", "指标", "metric_name"}
    # 「本期数值」是 `localize_financial_table` 长表格式的实际列名（实测：报告期/指标/本期数值/变化说明）。
    # 少了它，长表行就走不进下面的 metric/value 合并分支，而落到「按列合并」的兜底：
    # 于是每一行的「指标」文本都被当成**同一个字段的多值**，
    # 问「豪迈科技的主营业务是什么？」会吐两条假警告
    # 「未知公司 2026年半年报 字段 指标 存在多个值，已保留首个有效值。」
    # （公司名也丢，因为长表行没有身份列，group_key 退化成 ('', 报告期)）。
    value_keys = {"value", "数值", "metric_value", "本期数值"}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    def is_missing(value: Any) -> bool:
        return value in (None, "", "暂无数据", "None", "null")

    for row in rows:
        ticker = str(
            row.get("stock_code")
            or row.get("股票代码")
            or ""
        ).strip()
        name = str(
            row.get("stock_name")
            or row.get("公司名称")
            or ""
        ).strip()
        period = str(
            row.get("report_period")
            or row.get("报告期")
            or ""
        ).strip()
        group_key = (ticker or name, period)
        target = grouped.setdefault(
            group_key,
            {
                "stock_code": ticker,
                "stock_name": name,
                "report_period": period,
            },
        )
        if ticker and not target.get("stock_code"):
            target["stock_code"] = ticker
        if name and not target.get("stock_name"):
            target["stock_name"] = name

        metric_key = next((key for key in metric_keys if key in row), "")
        value_key = next((key for key in value_keys if key in row), "")
        if metric_key and value_key:
            metric = str(row.get(metric_key) or "").strip()
            value = row.get(value_key)
            if metric and not is_missing(value):
                target[metric] = value
            continue

        for key, value in row.items():
            if key in metadata_keys or is_missing(value):
                continue
            existing = target.get(key)
            if is_missing(existing):
                target[key] = value
            elif existing != value:
                warnings.append(
                    f"{name or ticker or '未知公司'} {period} "
                    f"字段 {key} 存在多个值，已保留首个有效值"
                )

    ordered_rows = list(grouped.values())
    # `2_performance_report` / `3_income_statement` 这些事实表**根本没有 stock_name 列**，
    # LLM 也就 SELECT 不到它（实测 002595 那条 SQL：只选了 stock_code/revenue/net_profit/…），
    # 于是 :2677 建出来的 `stock_name` 一直是空串 ⇒ `localize_financial_table` 的
    # `format_value` 把空格渲染成「暂无数据」⇒ 用户看到的表格是「公司名称 暂无数据」，
    # 而顶层 `company_name` 明明是对的（证据链 :4497 还先读了表格里那个假名）。
    # 名称本来就有一等公民来源（主数据 + 代码↔名对账），按代码回填即可；查不到就留空，
    # 让下面「全表都没有身份信息就不输出这两列」的既有规则兜底。
    if any(
        not str(row.get("stock_name") or "").strip() and str(row.get("stock_code") or "").strip()
        for row in ordered_rows
    ):
        names_by_code: dict[str, str] = {}
        for company in get_company_list():
            code = str(company.get("stock_code") or "").strip()
            if code:
                names_by_code[code] = display_company_name(code, str(company.get("stock_name") or ""))
        for row in ordered_rows:
            if str(row.get("stock_name") or "").strip():
                continue
            backfill = names_by_code.get(str(row.get("stock_code") or "").strip(), "")
            if backfill:
                row["stock_name"] = backfill
    # 单公司本地回答里身份列经常是空的，留着只会渲染成两列「暂无数据」；
    # 全表都没有身份信息时干脆不输出这两列。
    has_identity = any(
        str(row.get("stock_code") or "").strip() or str(row.get("stock_name") or "").strip()
        for row in ordered_rows
    )
    if not has_identity:
        for row in ordered_rows:
            row.pop("stock_code", None)
            row.pop("stock_name", None)
    if len(ordered_rows) > 1:
        all_fields = {
            key
            for row in ordered_rows
            for key in row
        }
        preferred = [
            "stock_code",
            "stock_name",
            "report_period",
            "revenue",
            "net_profit",
            "net_profit_parent",
            "revenue_yoy",
            "profit_yoy",
            "total_assets",
            "total_liability",
            "debt_ratio",
        ]
        ordered_keys = [
            key for key in preferred if key in all_fields
        ] + sorted(all_fields - set(preferred))
        return (
            [
                {key: row.get(key) for key in ordered_keys}
                for row in ordered_rows
            ],
            sources,
            list(dict.fromkeys(warnings)),
        )
    return ordered_rows, sources, list(dict.fromkeys(warnings))

def format_conversation_history(request: dict[str, Any], limit: int = 5) -> str:
    """整理最近对话历史供模型使用。"""
    history = request.get("conversation_history") or []
    lines = []
    for item in history[-limit:]:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        company = str(item.get("company_name") or "").strip()
        if not question:
            continue
        lines.append(
            f"用户：{question}\n助手：{answer[:500]}"
            + (f"\n当前公司：{company}" if company else "")
        )
    return "\n\n".join(lines)

def plan_with_llm(question: str, request: dict[str, Any]) -> dict[str, Any]:
    """让模型规划数据查询、RAG 和图表。"""
    client = make_client(request.get("api_key", ""), request.get("base_url", ""))
    history = format_conversation_history(request)
    perspective = perspective_prompt_for(request.get("audience", "综合分析师"))
    locked = locked_company_from_request(request)
    comparison_companies = comparison_companies_from_request(request)
    if comparison_companies:
        allowed = "；".join(
            f"{item.get('name', '')}（{item.get('ticker', '')}）"
            for item in comparison_companies
        )
        scope_prompt = (
            f"本轮允许比较以下公司：{allowed}。"
            "必须为每家公司拆出独立 SQL 子任务，最终合并为统一口径表，不得混入其他公司。"
        )
    else:
        scope_prompt = (
            f"当前会话已锁定公司：{locked.get('name', '')} "
            f"（{locked.get('ticker', '')}）。不得规划或引用其他公司的资料。"
            if locked
            else "当前会话尚未锁定公司；涉及“本次交易”时必须先追问公司，不得全库检索。"
        )
    system = (
        "你是财报问数自主规划器。只输出 JSON，不要 Markdown。\n\n"
        f"【会话公司锁定】\n{scope_prompt}\n\n"
        f"【当前分析视角】\n{perspective}\n\n"
        f"【最近对话历史】\n{history or '无'}\n\n"
        f"【可用表结构】\n{FINANCIAL_SCHEMA_PROMPT}\n\n"
        "【单任务输出格式】\n"
        '{"action":"sql|rag|hybrid|answer","sql":"只读SELECT，可为空",'
        '"rag_query":"检索问题，可为空",'
        '"chart":{"type":"line|bar|pie|radar|scatter|dualAxis|none","x":"列名","y":"列名","title":"标题"}}\n\n'
        "【完整规划输出格式】\n"
        '{"action":"plan","intent":"multi_company_comparison|complex_analysis",'
        '"companies":[{"ticker":"股票代码","name":"公司名称"}],'
        '"subtasks":[{"id":"唯一任务ID","kind":"sql|rag",'
        '"company":{"ticker":"股票代码","name":"公司名称"},"depends_on":["前置任务ID"],'
        '"sql":"只读SELECT，kind为sql时填写","rag_query":"检索问题，kind为rag时填写",'
        '"purpose":"该子任务说明","expect_columns":["期望字段"]}],'
        '"combine":{"mode":"wide|append","group_by":["stock_code","report_period"]},'
        '"chart":{"type":"bar","x":"公司名称","y":"指标字段","title":"标题"}}\n\n'
        "【规则】\n"
        "1. 只能 SELECT，不得写数据库；\n"
        "2. 需要图表时明确 chart.type；\n"
        "3. 原因分析、风险问题使用 rag 或 hybrid；\n"
        "4. 如果用户使用了“它”“那家”“上述公司”等指代词，请结合对话历史推断出具体公司；\n"
        "5. 一句话比较多家公司时必须使用 plan，并为每家公司生成独立 SQL 子任务；\n"
        "6. 子任务只声明真实依赖，独立公司的 SQL 任务不得互相依赖；\n"
        "7. 所有 SQL 必须显式包含 stock_code、公司名称、report_period 和所需指标字段"
        "（多家公司对比时没有公司名称列就无法区分系列）；\n"
        "8. 多表查询必须从有数据的核心表出发，可选指标表使用 LEFT JOIN，"
        "不得因某张表暂无数据而返回空结果；缺字段保留 NULL 并标注资料不足；\n"
        "9. 需要多源归因时，可同时拆分 sql 和 rag 子任务，最后统一汇总。\n\n"
        f"{PLANNER_FEW_SHOT}"
    )
    response = client.chat.completions.create(
        model=request.get("llm_model", DEEPSEEK_MODEL),
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"当前问题：{question}",
            },
        ],
        temperature=0.1,
    )
    plan = normalize_plan(
        parse_json_from_text(response.choices[0].message.content or "")
    )
    errors = validate_plan(plan, request)
    if errors:
        try:
            plan = normalize_plan(
                repair_plan_with_llm(question, plan, errors, request)
            )
            errors = validate_plan(plan, request)
        except Exception as exc:
            errors.append(f"规划修复失败：{exc}")
    if errors:
        raise ValueError("；".join(errors))
    plan["_validation_passed"] = True
    task_log(
        request,
        {
            "type": "plan",
            "action": plan.get("action", ""),
            "intent": plan.get("intent", ""),
            "subtasks": len(plan.get("subtasks") or []),
            "companies": [
                str(item.get("name") or item.get("ticker") or "")
                for item in comparison_companies_from_request(request)
            ],
        },
    )
    return plan

SQL_COMMENT_MARKERS = (";", "--", "/*", "#")
SQL_FORBIDDEN_WORDS = (
    " insert ",
    " update ",
    " delete ",
    " drop ",
    " alter ",
    " create ",
    " truncate ",
    " into ",
    "outfile",
    "dumpfile",
    "load_file",
    "load data",
    "procedure analyse",
    "information_schema",
    "performance_schema",
    "mysql.",
    "sleep(",
    "benchmark(",
)

def referenced_tables_of(statement: str) -> set[str]:
    """取出语句引用到的所有表名（含 FROM 后的逗号多表），CTE 名不算表。"""
    names: set[str] = set()
    for segment in re.findall(
        r"\b(?:from|join)\s+(.*?)"
        r"(?=\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\blimit\b|\bhaving\b|\bunion\b"
        r"|\bselect\b|\)|$)",
        statement,
        flags=re.I,
    ):
        for piece in segment.split(","):
            piece = piece.strip().strip("()")
            if not piece or piece.startswith("("):
                continue
            names.add(piece.split()[0].strip("`").lower())
    cte_names = {
        match.group(1).lower()
        for match in re.finditer(r"\b([A-Za-z0-9_]+)\s+as\s*\(", statement, flags=re.I)
    }
    return names - cte_names

def validate_sql_safety(sql: str) -> str:
    """校验并限制 SQL 为只读查询。"""
    statement = re.sub(r"\s+", " ", sql or "").strip()
    statement = re.sub(r"`?finance_report`?\.", "", statement, flags=re.I)
    for alias, real_table in TABLE_ALIASES.items():
        statement = re.sub(
            rf"(?<![A-Za-z0-9_])`?{re.escape(alias)}`?(?![A-Za-z0-9_])",
            f"`{real_table}`",
            statement,
            flags=re.I,
        )
    lowered = statement.lower()
    for marker in SQL_COMMENT_MARKERS:
        if marker in statement:
            raise SQLValidationError("SQL 包含不安全字符")
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise SQLValidationError("只允许只读查询")
    # 字符串字面量不参与关键字判断，避免把正文里的英文词误判成写操作。
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"", " ", lowered)
    padded = f" {stripped} "
    for word in SQL_FORBIDDEN_WORDS:
        if word in padded:
            raise SQLValidationError(f"SQL 包含不允许的用法：{word.strip()}")
    invalid_tables = referenced_tables_of(statement) - {
        name.lower() for name in ALLOWED_TABLES
    }
    if invalid_tables:
        raise SQLValidationError(
            f"SQL 使用了不允许的表：{', '.join(sorted(invalid_tables))}"
        )
    if " limit " not in padded:
        statement += " LIMIT 200"
    return statement

def answer_with_multi_company_data(
    question: str,
    unit_mode: str,
    audience: str,
    companies: list[dict[str, str]],
) -> dict[str, Any] | None:
    """并行查询多家公司，并按相同报告期生成统一比较表。"""
    if len(companies) < 2:
        return None
    metrics = extract_metrics(question)
    if not metrics:
        metrics = [
            ("2_performance_report", "revenue", "营业收入"),
            ("2_performance_report", "net_profit", "净利润"),
            ("4_balance_sheet", "__debt_ratio__", "资产负债率"),
        ]

    tasks = [
        (company, table, column, label)
        for company in companies
        for table, column, label in metrics
    ]
    series_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
    warnings: list[str] = []

    def load_metric(
        company: dict[str, str],
        table: str,
        column: str,
        label: str,
    ) -> tuple[tuple[str, str], list[dict[str, Any]]]:
        ticker = str(company.get("ticker") or "")
        return (ticker, label), query_metric_series(ticker, table, column)

    with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as executor:
        future_map = {
            executor.submit(load_metric, company, table, column, label): (
                company,
                label,
            )
            for company, table, column, label in tasks
        }
        for future in as_completed(future_map):
            company, label = future_map[future]
            try:
                key, series = future.result()
                series_map[key] = series
            except Exception as exc:
                warnings.append(
                    f"{company.get('name') or company.get('ticker')}的{label}查询失败：{exc}"
                )
                series_map[
                    (str(company.get("ticker") or ""), label)
                ] = []

    period_sets = [
        {
            str(row.get("report_period") or "")
            for row in series
            if row.get("report_period")
        }
        for series in series_map.values()
        if series
    ]
    common_period = ""
    if period_sets and all(period_sets):
        common = set.intersection(*period_sets)
        if common:
            common_period = max(common)
    if not common_period:
        warnings.append("各公司暂无可完全对齐的同一报告期，已分别采用各自最新可用报告期。")

    table: list[dict[str, Any]] = []
    comparison_values: dict[str, list[tuple[str, float, str]]] = {
        label: [] for _, _, label in metrics
    }
    company_rows: dict[str, dict[str, Any]] = {}
    mixed_periods = False
    for company in companies:
        ticker = str(company.get("ticker") or "")
        name = str(company.get("name") or ticker)
        row: dict[str, Any] = {
            "公司名称": name,
            "stock_code": ticker,
            "stock_name": name,
            "report_period": common_period,
        }
        metric_periods: dict[str, str] = {}
        for _, _, label in metrics:
            series = series_map.get((ticker, label)) or []
            selected = next(
                (
                    item
                    for item in series
                    if common_period
                    and str(item.get("report_period") or "") == common_period
                ),
                series[0] if series else None,
            )
            if selected:
                metric_periods[label] = str(selected.get("report_period") or "")
            value = selected.get("value") if selected else None
            row[label] = format_financial_metric(label, value, unit_mode)
            if value is not None:
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                comparison_values[label].append((name, numeric, row[label]))
        if not common_period and metric_periods:
            # 没有共同期时不许让最后一个指标悄悄定义整行口径：取最新的一个期间做行期，
            # 来自其它期间的指标在格子里标出自己的期间。
            row_period = max(metric_periods.values())
            row["report_period"] = row_period
            distinct = {p for p in metric_periods.values() if p}
            if len(distinct) > 1:
                mixed_periods = True
                for label, period in metric_periods.items():
                    if period and period != row_period and row.get(label):
                        row[label] = f"{row[label]}（{period}）"
        company_rows[ticker] = row
        table.append(row)

    if mixed_periods:
        warnings.append(
            "同一家公司的不同指标来自不同报告期，已在对应数值后标注来源期间。"
        )

    summary_parts = []
    for company in companies:
        row = company_rows.get(str(company.get("ticker") or ""), {})
        metric_text = "，".join(
            f"{label}{row.get(label, '暂无数据')}" for _, _, label in metrics
        )
        summary_parts.append(
            f"{row.get('stock_name') or company.get('name')}：{metric_text}"
        )
    if common_period:
        summary = f"{common_period}，统一口径比较如下。" + "；".join(summary_parts) + "。"
    else:
        # 无共同期时不能说「统一口径」，那正是这条 bug 的表象。
        summary = (
            "各公司最新可用报告期（口径未完全对齐，不同期间已在数值后标注），比较如下。"
            + "；".join(summary_parts)
            + "。"
        )

    key_points = []
    for _, _, label in metrics:
        values = comparison_values.get(label) or []
        if len(values) < 2:
            continue
        highest = max(values, key=lambda item: item[1])
        lowest = min(values, key=lambda item: item[1])
        if highest[1] == lowest[1]:
            key_points.append(f"{label}在各公司间基本持平。")
            continue
        gap = highest[1] - lowest[1]
        gap_text = (
            f"{gap:.2f}个百分点"
            if label == "资产负债率" or "率" in label or "同比" in label or "环比" in label
            else format_amount(gap, unit_mode)
        )
        key_points.append(
            f"{label}最高的是{highest[0]}（{highest[2]}），"
            f"最低的是{lowest[0]}（{lowest[2]}），相差{gap_text}。"
        )
    key_points.append(AUDIENCE_FOCUS.get(audience, AUDIENCE_FOCUS["综合分析师"]))

    chart = None
    chart_metric = next(
        (
            label
            for _, _, label in metrics
            if len(comparison_values.get(label) or []) >= 2
        ),
        "",
    )
    chart_frames: list[dict[str, Any]] = []
    chart_names = list(dict.fromkeys(
        name for entries in comparison_values.values() for name, _, _ in entries
    ))
    for chart_name in chart_names:
        chart_row: dict[str, Any] = {"公司名称": chart_name}
        for label, entries in comparison_values.items():
            for name, numeric, _ in entries:
                if name == chart_name:
                    chart_row[label] = numeric
                    break
        chart_frames.append(chart_row)

    if chart_metric:
        # 优先用「一指标一系列、横轴是公司」的对比图（多个指标都能画，数值取原始值 ——
        # 表格里是 "218.84 亿元" 这种格式化文本，喂给图表会和 series.scale 双重折算）；
        # 条件不满足再退回原来那张单指标图。
        multi_metric_chart = build_comparison_chart(chart_frames, {"type": "bar"}, question)
        chart = multi_metric_chart or {
            "type": "bar",
            "title": f"多家公司{chart_metric}对比",
            "x": "stock_name",
            "y": chart_metric,
            "data": [
                {"stock_name": name, chart_metric: value}
                for name, value, _ in comparison_values[chart_metric]
            ],
            "colors": ["#2dd4bf", "#f0b35c", "#60a5fa", "#f472b6", "#a78bfa"],
        }

    execution_trace = [
        {
            "subtask_id": f"local_{company.get('ticker')}",
            "kind": "sql",
            "company": company,
            "purpose": "批量读取同口径财务指标",
            "status": "completed",
            "attempts": 1,
            "rows": [company_rows.get(str(company.get("ticker") or ""), {})],
            "sources": [],
            "error": "",
            "correction_log": [],
        }
        for company in companies
    ]
    plan = {
        "action": "plan",
        "intent": "multi_company_comparison",
        "companies": companies,
        "subtasks": [
            {
                "id": f"local_{company.get('ticker')}",
                "kind": "sql",
                "company": company,
                "depends_on": [],
                "purpose": "批量读取同口径财务指标",
            }
            for company in companies
        ],
        "combine": {"mode": "wide", "group_by": ["stock_code", "report_period"]},
        "chart": chart or {"type": "none"},
    }
    return {
        "answer": summary,
        "summary": summary,
        "table": table,
        "chart": chart,
        "chart_note": "" if chart else "可用比较数据不足，未生成图表。",
        "sources": [],
        "metrics": [],
        "key_points": key_points,
        "warnings": warnings,
        "mode": "local",
        "company_name": "",
        "audience": audience,
        "plan": plan,
        "subtasks": plan["subtasks"],
        "execution_trace": execution_trace,
        "correction_log": [],
    }

def embedding_vectors(texts: list[str], request: dict[str, Any]) -> list[list[float]]:
    """使用本地模型或兼容接口生成向量。"""
    model_name = str(
        request.get("embedding_model") or LOCAL_EMBEDDING_MODEL
    ).strip()
    if model_name == LOCAL_EMBEDDING_MODEL or model_name.startswith("BAAI/"):
        return local_embedding_vectors(texts)
    client = OpenAI(
        api_key=request.get("api_key", ""),
        base_url=request.get("base_url", "").rstrip("/"),
        timeout=120,
    )
    response = client.embeddings.create(
        model=model_name,
        input=texts,
    )
    ordered = sorted(response.data, key=lambda item: item.index)
    return [list(item.embedding) for item in ordered]

def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)

def identify_company_filter(question: str) -> tuple[str, str]:
    """从问题中识别公司过滤条件。

    只认全称会把「招商证券」「泸州老窖」这类仅以文件主体入库的公司挡在外面，
    明明库里有它的报告却回一句「请补充公司名称」。文件主体同样可以作为过滤条件。
    """
    identity = identify_company_from_question(question)
    if identity:
        return identity.get("ticker", ""), identity.get("name", "")
    return "", ""

def retrieve_rag_chunks(
    query: str,
    top_k: int = 6,
    request: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """搜索与问题最相关的 RAG 文本块。"""
    def terms(text: str) -> set[str]:
        """提取文本中的中文字符片段和英文词组。"""
        compact = re.sub(r"\s+", "", text or "")
        grams = {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}
        words = {
            word.lower()
            for word in re.findall(r"[A-Za-z0-9_]{2,}", text or "")
        }
        return grams | words

    query_terms = terms(query)
    if not query_terms:
        return []
    task_wait(request or {})
    locked = locked_company_from_request(request)
    stock_code = locked.get("ticker", "")
    if not stock_code:
        print("[RAG_FILTER] blocked missing company_ticker")
        return []
    locked_deal = (request or {}).get("locked_deal")
    if not isinstance(locked_deal, dict):
        locked_deal = {}
    deal_id = str(locked_deal.get("deal_id") or "").strip()
    company_scoped = [
        chunk
        for chunk in RAG_CHUNKS
        if chunk_company_ticker(chunk) == stock_code
    ]
    deal_scoped = (
        [
            chunk
            for chunk in company_scoped
            if chunk_deal_id(chunk) == deal_id
        ]
        if deal_id
        else []
    )
    candidates = deal_scoped or company_scoped
    filter_scope = "deal" if deal_scoped else "company"
    print(
        "[RAG_FILTER] "
        f"company_ticker={stock_code} "
        f"deal_id={deal_id or '*'} "
        f"scope={filter_scope} "
        f"chunks={len(candidates)}"
    )
    if not candidates:
        return []
    # 键算法必须与写入 Qdrant 时完全一致，否则向量分和词面分合不到一起（混合检索会静默退化成纯词面）。
    build_chunk_key = vector_store.build_chunk_key

    lexical_scores: dict[str, float] = {}
    query_compact = re.sub(r"\s+", "", query)
    for index, chunk in enumerate(candidates):
        if index % 500 == 0:
            task_wait(request or {})
        content = str(chunk.get("content", ""))
        content_terms = terms(content)
        overlap = len(query_terms & content_terms)
        if overlap == 0:
            continue
        coverage = overlap / max(1, len(query_terms))
        density = overlap / max(1, len(content_terms)) ** 0.5
        score = coverage * 0.75 + density * 0.25
        if len(query_compact) >= 3 and query_compact in re.sub(r"\s+", "", content):
            score += 0.35
        if chunk_company_ticker(chunk) == stock_code:
            score += 0.1
        if score > 0:
            lexical_scores[build_chunk_key(chunk)] = score

    vector_scores: dict[str, float] = {}
    embedding_model = str(
        (request or {}).get("embedding_model") or ""
    ).strip()
    local_embedding = (
        embedding_model == LOCAL_EMBEDDING_MODEL
        or embedding_model.startswith("BAAI/")
    )
    if embedding_model and (local_embedding or request.get("api_key")):
        try:
            query_vector = np.asarray(
                embedding_vectors([query], request or {})[0],
                dtype=np.float32,
            )
            query_norm = float(np.linalg.norm(query_vector))
            if query_norm > 0:
                try:
                    qdrant_hits = vector_store.search_vectors(
                        query_vector.tolist(),
                        stock_code,
                        deal_id,
                        limit=max(80, top_k * 4),
                    )
                    for hit in qdrant_hits:
                        payload = hit.get("payload") or {}
                        key = str(payload.get("build_chunk_key") or "")
                        if key:
                            vector_scores[key] = float(hit.get("score") or 0)
                except Exception as exc:
                    print(f"[QDRANT_SEARCH] fallback_to_memory={exc}")
            if query_norm > 0 and not vector_scores:
                workset = candidates[:2000]
                missing = [
                    chunk
                    for chunk in workset
                    if not chunk.get("embedding_present")
                    and not str(chunk.get("embedding_json") or "").strip()
                ]
                if missing:
                    index_new_rag_chunks(missing[:500])
                hydrate = globals().get("hydrate_embeddings")
                if hydrate:
                    hydrate([c for c in workset if c.get("embedding_present")])
                for chunk in workset:
                    try:
                        vector = np.asarray(
                            json.loads(chunk.get("embedding_json") or "[]"),
                            dtype=np.float32,
                        )
                    except Exception:
                        vector = np.asarray([], dtype=np.float32)
                    if vector.size != query_vector.size:
                        continue
                    denominator = query_norm * float(np.linalg.norm(vector))
                    if denominator <= 0:
                        continue
                    score = float(np.dot(query_vector, vector) / denominator)
                    if score > 0:
                        vector_scores[build_chunk_key(chunk)] = score
        except Exception as exc:
            print(f"[VECTOR_SEARCH] fallback_to_lexical={exc}")

    vector_max = max(vector_scores.values(), default=0.0)
    lexical_max = max(lexical_scores.values(), default=0.0)
    if vector_scores:
        print(
            "[VECTOR_SEARCH] "
            f"company_ticker={stock_code} scored={len(vector_scores)}"
        )

    def section_rerank_bonus(chunk: dict[str, Any]) -> float:
        """按章节语义对混合召回结果做轻量重排。"""
        content = re.sub(r"\s+", "", str(chunk.get("content") or ""))
        bonus = 0.0
        if any(
            word in query
            for word in ("经营", "业务", "主营", "产品", "赚钱", "收入", "产业", "板块")
        ):
            if any(
                marker in content
                for marker in (
                    "报告期内公司从事的主要业务",
                    "主营业务分析",
                    "主要产品及用途",
                    "公司主要从事",
                    "核心产品",
                    "业务模式",
                    "经营情况",
                )
            ):
                bonus += 0.45
            if any(
                marker in content
                for marker in (
                    "企业所得税",
                    "会计政策",
                    "财务报表附注",
                    "摊余成本",
                    "实际利率法",
                    "金融资产",
                )
            ):
                bonus -= 0.35
        if any(word in query for word in ("风险", "诉讼", "担保", "处罚")):
            if any(
                marker in content
                for marker in (
                    "风险提示",
                    "主要风险",
                    "重大风险",
                    "可能面临",
                    "风险应对",
                )
            ):
                bonus += 0.45
            if "接待调研" in content:
                bonus -= 0.5
        if any(word in query for word in ("股东", "持股")):
            if any(
                marker in content
                for marker in ("前10名股东", "前十大股东", "股东数量及持股情况")
            ):
                bonus += 0.5
        if any(
            word in query
            for word in ("营收", "收入", "利润", "资产", "负债", "现金流", "毛利率")
        ):
            if any(
                marker in content
                for marker in ("营业收入", "净利润", "资产负债率", "现金流")
            ):
                bonus += 0.2
        return bonus

    combined: list[tuple[float, dict[str, Any]]] = []
    for chunk in candidates:
        key = build_chunk_key(chunk)
        vector_score = (
            vector_scores.get(key, 0.0) / vector_max
            if vector_max > 0
            else 0.0
        )
        lexical_score = (
            lexical_scores.get(key, 0.0) / lexical_max
            if lexical_max > 0
            else 0.0
        )
        score = (
            vector_score * 0.55
            + lexical_score * 0.45
            + section_rerank_bonus(chunk)
        )
        if score > 0:
            combined.append((score, chunk))
    combined.sort(key=lambda item: item[0], reverse=True)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, chunk in combined:
        key = build_chunk_key(chunk)
        if key in seen:
            continue
        seen.add(key)
        results.append(chunk)
        if len(results) >= top_k:
            break
    print(
        "[RAG_FILTER] "
        f"company_ticker={stock_code} "
        f"returned={len(results)}"
    )
    return results

CHART_SKIP_COLUMNS_RE = re.compile(
    r"^\s*(?:[\w一-鿿]{1,10}[_\s-]?)?(排名|名次|序号|位次|编号|行号|榜位|rank|no\.?|idx|index)"
    r"(?:[_\s-]?(no|num|number|序|位|列))?\s*$",
    re.I,
)

def chart_number(value: Any) -> float | None:
    """将图表文本转换为数值。"""
    text = (
        unicodedata.normalize("NFKC", str(value)).strip()
        if value is not None
        else ""
    )
    if not text:
        return None
    negative = text.startswith(("(", "（")) and text.endswith((")", "）"))
    text = re.sub(r"[*_`]", "", text)
    text = text.replace(",", "").replace("，", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if negative and number > 0:
        number = -number
    if "亿" in text:
        number *= 100_000_000
    elif "万" in text:
        number *= 10_000
    return number

def chart_type_from_question(question: str, fallback: str = "bar") -> str:
    """根据问题判断图表类型。"""
    allowed = {"line", "bar", "pie", "radar", "scatter", "dualAxis"}
    preferred = fallback if fallback in allowed else "bar"
    if any(word in question for word in ("柱状", "条形", "柱形")):
        return "bar"
    if "饼" in question:
        return "pie"
    if any(word in question for word in ("趋势", "走势", "变化")):
        return "line"
    if "雷达" in question:
        return "radar"
    if "散点" in question:
        return "scatter"
    if any(word in question for word in ("占比", "比例", "结构", "构成", "分布")):
        return "pie"
    return preferred

def build_chart_from_rows(
    question: str,
    rows: list[dict[str, Any]],
    x_key: str | None = None,
    y_key: str | None = None,
    title: str = "",
    fallback_type: str = "bar",
) -> dict[str, Any] | None:
    """将表格行转换为图表数据结构。"""
    source = [row for row in (rows or []) if isinstance(row, dict)]
    if len(source) < 2:
        return None
    keys: list[str] = []
    for row in source:
        for key in row:
            key = str(key)
            if key not in keys:
                keys.append(key)
    if not keys:
        return None
    x_key = str(x_key or "")
    y_key = str(y_key or "")
    if x_key not in keys or y_key not in keys or x_key == y_key:
        numeric_keys = []
        for key in keys:
            # 代码/名称/报告期也能被解析成数（"000538"→538、"2026年半年报"→2026），
            # 一旦当数值列上 y 轴就会画出「以代码为金额」的错误图表。
            if key in CHART_META_COLUMNS or key == x_key:
                continue
            # 排行榜的「排名/序号」是表上自带的行号列，不是财务数值：
            # 之前它被当成 y 轴 ⇒ 画出一张「排名(1,2,3…) 单位元」的假柱状图，
            # 真正的金额列（格式化文本）反而没被选中。
            if CHART_SKIP_COLUMNS_RE.search(str(key)):
                continue
            if key in CHART_META_COLUMNS:
                continue
            count = sum(
                1 for row in source if chart_number(row.get(key)) is not None
            )
            if count >= 2:
                numeric_keys.append((count, key))
        if not numeric_keys:
            return None
        y_key = max(numeric_keys, key=lambda item: item[0])[1]
        x_key = next(
            (
                key
                for key in keys
                if key != y_key
                and key not in CHART_META_COLUMNS
                and not CHART_SKIP_COLUMNS_RE.search(str(key))
                and any(str(row.get(key) or "").strip() for row in source)
            ),
            "",
        )
        if not x_key:
            return None
    data = []
    for row in source:
        label = str(row.get(x_key) or "").strip()
        value = chart_number(row.get(y_key))
        if label and value is not None:
            data.append({x_key: label, y_key: value})
    if len(data) < 2:
        return None
    return {
        "type": chart_type_from_question(question, fallback_type),
        "title": title or f"{question[:24]}图表",
        "x": x_key,
        "y": y_key,
        "data": data,
        "colors": ["#2dd4bf", "#f0b35c", "#60a5fa", "#f472b6", "#a78bfa"],
    }

def parse_markdown_table_cells(line: str) -> list[str]:
    """拆分 Markdown 表格单元格。"""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]

def is_markdown_table_separator(line: str) -> bool:
    """判断 Markdown 表格分隔行。"""
    cells = parse_markdown_table_cells(line)
    return len(cells) >= 2 and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))
        for cell in cells
    )

def extract_markdown_tables(text: str) -> list[list[dict[str, Any]]]:
    """从回答中解析 Markdown 表格。"""
    source = text or ""
    lines = source.splitlines()
    tables: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(lines) - 1:
        if "|" not in lines[index] or not is_markdown_table_separator(lines[index + 1]):
            index += 1
            continue
        headers = parse_markdown_table_cells(lines[index])
        rows: list[dict[str, Any]] = []
        index += 2
        while index < len(lines) and "|" in lines[index]:
            cells = parse_markdown_table_cells(lines[index])
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
            index += 1
        if rows:
            tables.append(rows)
    separator_pattern = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|)+")
    for match in separator_pattern.finditer(source):
        separator = parse_markdown_table_cells(match.group(0))
        width = len(separator)
        if width < 2:
            continue
        prefix_parts = [part.strip() for part in source[: match.start()].split("|")]
        while prefix_parts and not prefix_parts[-1]:
            prefix_parts.pop()
        if len(prefix_parts) < width:
            continue
        headers = prefix_parts[-width:]
        values = [
            part.strip()
            for part in source[match.end() :].split("|")
            if part.strip()
        ]
        rows = []
        for start in range(0, len(values), width):
            cells = values[start : start + width]
            if len(cells) < width:
                break
            rows.append(dict(zip(headers, cells)))
        if rows:
            tables.append(rows)
    return tables

def strip_markdown_tables(text: str) -> str:
    """从正文中移除 Markdown 表格。"""
    lines = (text or "").splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if (
            "|" in lines[index]
            and index + 1 < len(lines)
            and is_markdown_table_separator(lines[index + 1])
        ):
            index += 2
            while index < len(lines) and "|" in lines[index]:
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

FIELD_NAME_LEAKS = (
    "net_profit_parent", "operating_revenue", "operating_cost", "operating_profit",
    "total_profit", "basic_eps", "total_assets", "total_liability", "total_equity",
    "current_assets", "current_liability", "operate_cash", "invest_cash", "finance_cash",
    "fx_cash", "cash_increase", "profit_yoy", "revenue_yoy", "net_profit",
    "report_period", "stock_code", "stock_name",
)
FIELD_LEAK_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(sorted(FIELD_NAME_LEAKS, key=len, reverse=True)) + r")(?![A-Za-z0-9_])"
)

def replace_field_names_with_labels(text: str) -> str:
    """把正文里漏出的数据库字段名换成中文标签。

    实测（2026-10-28，_tools/_hist/check_wording_round10.log）：岩山科技那题回答要求第 2 条
    已写「不要输出字段名」，正文仍出现「（profit_yoy为-98.4641%）」⇒ 光靠提示词不保险，
    展示前做一次确定性替换（用 COLUMN_LABELS，替换后自带口径信息）。
    """
    return FIELD_LEAK_RE.sub(lambda match: COLUMN_LABELS.get(match.group(1), match.group(1)), str(text or ""))

def clean_answer_text(text: str) -> str:
    """清理回答中的多余 Markdown 符号。"""
    cleaned = str(text or "")
    cleaned = replace_field_names_with_labels(cleaned)
    cleaned = cleaned.replace("\\|", "|")
    cleaned = re.sub(r"#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r":?-{3,}:?", "", cleaned)
    cleaned = cleaned.replace("|", " ")
    cleaned = re.sub(r"(?m)^\s*[-*+]\s+", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

def ensure_answer_ending(text: str) -> str:
    """确保用户可见回答带有明确结尾标点。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return cleaned
    if cleaned.endswith(("。", "！", "？", "!", "?", "…")):
        return cleaned
    if cleaned.endswith(("；", ";")):
        return cleaned[:-1].rstrip() + "。"
    return cleaned + "。"

ANSWER_STYLE_RULES = {
    "简洁结论": {
        "instruction": "只给结论和最关键的数值，控制在3句话以内，不展开背景和过程。",
        "max_sentences": 3,
        "max_chars": 260,
        "max_points": 3,
    },
    "分析师视角": {
        "instruction": "给出核心结论、关键数据、原因和风险，保持专业但不过度展开。",
        "max_sentences": 6,
        "max_chars": 680,
        "max_points": 5,
    },
    "详细讲解": {
        "instruction": "完整说明数据变化、原因、影响和后续关注点，可使用分点表达。",
        "max_sentences": 12,
        "max_chars": 1400,
        "max_points": 8,
    },
}

def answer_style_rule(style: str) -> dict[str, Any]:
    """读取回答风格规则。"""
    return ANSWER_STYLE_RULES.get(
        str(style or "").strip(),
        ANSWER_STYLE_RULES["分析师视角"],
    )

def trim_answer_text(
    text: str,
    max_sentences: int,
    max_chars: int,
) -> str:
    """按完整句子限制回答长度，避免从句子中间截断。"""
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return compact
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])", compact)
        if item.strip()
    ]
    selected = []
    length = 0
    for sentence in sentences:
        if not selected and len(sentence) > max_chars:
            return sentence[:max_chars].rstrip("，,；; ") + "……"
        if selected and (
            len(selected) >= max_sentences
            or length + len(sentence) > max_chars
        ):
            break
        selected.append(sentence)
        length += len(sentence)
    if not selected:
        return compact[:max_chars].rstrip("，,；; ") + "。"
    return "".join(selected)

def apply_answer_style(
    result: dict[str, Any],
    style: str,
) -> dict[str, Any]:
    """把回答风格应用到正文、摘要和关键点。"""
    rule = answer_style_rule(style)
    result["answer"] = trim_answer_text(
        str(result.get("answer") or ""),
        int(rule["max_sentences"]),
        int(rule["max_chars"]),
    )
    result["summary"] = trim_answer_text(
        str(result.get("summary") or result.get("answer") or ""),
        int(rule["max_sentences"]),
        int(rule["max_chars"]),
    )
    result["key_points"] = [
        str(item).strip()
        for item in (result.get("key_points") or [])[: int(rule["max_points"])]
        if str(item).strip()
    ]
    result["answer_style"] = str(style or "分析师视角")
    return result

def normalize_result_endings(result: dict[str, Any]) -> dict[str, Any]:
    """统一整理所有用户可见文本的结尾标点。"""
    for field in ("answer", "summary"):
        result[field] = ensure_answer_ending(str(result.get(field) or ""))
    for field in ("key_points", "warnings"):
        result[field] = [
            ensure_answer_ending(str(item))
            for item in result.get(field) or []
            if str(item).strip()
        ]
    if result.get("chart_note"):
        result["chart_note"] = ensure_answer_ending(str(result["chart_note"]))
    return result

def remove_chart_limitation_text(text: str) -> str:
    """图表生成后移除无法绘图提示。"""
    return re.sub(
        r"(?:无法直接生成图形|无法生成图表|暂不生成图表)[，,。；;\s]*",
        "",
        str(text or ""),
    ).strip()

def validate_chart_points(chart: Any) -> bool:
    """判断图表是否有足够数据点。"""
    if not isinstance(chart, dict):
        return False
    rows = chart.get("data") or []
    raw_y = chart.get("y")
    y_keys = (
        [str(item) for item in raw_y]
        if isinstance(raw_y, list)
        else [str(raw_y or "")]
    )
    return (
        sum(
            1
            for row in rows
            if isinstance(row, dict)
            and any(
                chart_number(row.get(y_key)) is not None
                for y_key in y_keys
                if y_key
            )
        )
        >= 2
    )

def build_comparison_chart(
    frame: list[dict[str, Any]],
    proposed: Any,
    question: str,
) -> dict[str, Any] | None:
    """为多公司结果构建以公司为横轴的有效对比图。"""
    if len(frame) < 2:
        return None
    chart = proposed if isinstance(proposed, dict) else {}
    company_keys = ("stock_name", "公司名称", "公司", "stock_code", "股票代码", "证券简称", "公司简称")
    x_key = next(
        (
            key
            for key in company_keys
            if all(str(row.get(key) or "").strip() for row in frame if isinstance(row, dict))
        ),
        "",
    )
    if not x_key:
        return None
    # y 轴 = 所有能解析成数值的列（一指标一系列），不再只挑一列；
    # 排除序号/排名这类列（实测排行榜的「排名」列曾被选成 y 轴，画出 1/2/3 的假图）。
    numeric_keys = [
        str(key)
        for key in frame[0].keys()
        if key != x_key
        and key not in CHART_META_COLUMNS
        and not CHART_SKIP_COLUMNS_RE.search(str(key))
        and sum(1 for row in frame if chart_number(row.get(key)) is not None) >= 2
    ]
    if not numeric_keys:
        return None
    raw_y = chart.get("y")
    proposed_y = [
        str(item).strip()
        for item in (raw_y if isinstance(raw_y, list) else re.split(r"[,，、]", str(raw_y or "")))
        if str(item).strip()
    ]
    y_keys = [item for item in proposed_y if item in numeric_keys] or numeric_keys
    if len(y_keys) < 2:
        # 只有一列可用时沿用原来的单系列写法，避免图例只剩一个还标「右轴」
        pass
    chart_type = str(chart.get("type") or "").lower()
    if chart_type not in {"line", "bar", "area", "radar"}:
        chart_type = (
            "line"
            if any(word in question for word in ("趋势", "走势", "折线"))
            else "bar"
        )
    names = [
        display_company_name(str(row.get("stock_code") or ""), str(row.get(x_key) or ""))
        if str(x_key).startswith("stock_code") or str(x_key) == "股票代码"
        else str(row.get(x_key) or "")
        for row in frame
        if isinstance(row, dict) and row.get(x_key)
    ]
    return {
        "type": chart_type,
        "title": "、".join(names[:3]) + ("等公司" if len(names) > 3 else "") + "经营指标对比",
        "x": x_key,
        "y": y_keys if len(y_keys) > 1 else y_keys[0],
        "data": frame,
        "colors": ["#2dd4bf", "#f0b35c", "#60a5fa", "#f472b6", "#a78bfa"],
    }

def extract_chart_data_with_llm(
    question: str,
    answer: str,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """让模型从回答中提取图表数据。"""
    if not request.get("api_key") or not answer.strip():
        return None
    try:
        client = make_client(
            request.get("api_key", ""),
            request.get("base_url", ""),
        )
        response = client.chat.completions.create(
            model=request.get("llm_model", DEEPSEEK_MODEL),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是图表数据提取器。只输出 JSON，不要解释或 Markdown。"
                        '格式：{"rows":[{"label":"项目","value":0}],'
                        '"type":"pie|line|bar|radar|scatter|dualAxis",'
                        '"title":"标题"}。'
                        "只提取回答中已有的数据，不得估算或补充；"
                        "少于两个数据点时返回 rows=[]。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"用户问题：{question}\n\n"
                        f"回答内容：\n{answer[:6000]}"
                    ),
                },
            ],
            temperature=0,
        )
        payload = parse_json_from_text(response.choices[0].message.content or "")
        rows = []
        for item in payload.get("rows") or payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            label = (
                item.get("label")
                or item.get("name")
                or item.get("x")
                or item.get("类别")
                or item.get("名称")
            )
            value = item.get("value")
            if value is None:
                value = item.get("y") or item.get("数值")
            number = chart_number(value)
            if label is not None and number is not None:
                rows.append({"label": str(label), "value": number})
        return build_chart_from_rows(
            question,
            rows,
            "label",
            "value",
            str(payload.get("title") or question[:24]),
            str(payload.get("type") or "bar"),
        )
    except Exception:
        return None

def ensure_chart_data(
    question: str,
    result: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    """为图表题补齐结构化图表数据。"""
    if not any(word in question for word in CHART_WORDS):
        return result
    chart = result.get("chart")
    if validate_chart_points(chart):
        return result
    x_key = chart.get("x") if isinstance(chart, dict) else None
    y_key = chart.get("y") if isinstance(chart, dict) else None
    title = chart.get("title") if isinstance(chart, dict) else ""
    fallback_type = chart.get("type") if isinstance(chart, dict) else "bar"
    candidate = build_chart_from_rows(
        question,
        result.get("table") or [],
        x_key,
        y_key,
        str(title or ""),
        str(fallback_type or "bar"),
    )
    if candidate is None:
        for table in extract_markdown_tables(str(result.get("answer") or "")):
            candidate = build_chart_from_rows(question, table)
            if candidate is not None:
                break
    if candidate is None:
        candidate = extract_chart_data_with_llm(
            question,
            str(result.get("answer") or ""),
            request,
        )
    if candidate is not None:
        result["chart"] = candidate
        result["chart_note"] = ""
        result["warnings"] = [
            warning
            for warning in result.get("warnings") or []
            if "数据点不足" not in str(warning)
        ]
    return result

def answer_with_llm(question: str, request: dict[str, Any]) -> dict[str, Any]:
    """使用云端模型生成数据问答和图表。"""
    task_stage(request, "规划问题")
    task_wait(request)
    plan = plan_with_llm(question, request)
    task_stage(request, "执行数据查询")
    task_wait(request)
    history = format_conversation_history(request)
    audience = request.get("audience", "综合分析师")
    action = plan.get("action", "answer")
    frame: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    source_keys: set[tuple[str, int, int]] = set()
    sql_used = ""
    execution: dict[str, Any] | None = None
    plan_warnings: list[str] = []
    # 「主营业务是什么」这类 action=rag 的问题不会进 sql 分支，但下面 rag 检索要用它，
    # 不在这里初始化就会 UnboundLocalError（实测：cannot access local variable 'legacy_company'）。
    legacy_company = locked_company_from_request(request)

    if action == "plan" or plan.get("subtasks"):
        task_stage(request, "调度子任务")
        task_wait(request)
        execution = execute_plan_subtasks(plan, question, request)
        frame, sources, plan_warnings = unified_table_from_plan_results(execution)
        sql_used = " | ".join(
            str(item.get("sql") or "")
            for item in execution.get("results") or []
            if item.get("status") == "completed" and item.get("sql")
        )
        comparison_companies = comparison_companies_from_request(request)
        expected_tickers = {
            str(item.get("ticker") or "")
            for item in comparison_companies
            if item.get("ticker")
        }
        covered_tickers = {
            str(row.get("stock_code") or "")
            for row in frame
            if str(row.get("stock_code") or "")
        }
        needs_local_fallback = (
            not frame
            or bool(expected_tickers and expected_tickers - covered_tickers)
        )
        if needs_local_fallback:
            local_fallback = answer_with_multi_company_data(
                question,
                request.get("unit_mode", "自动"),
                request.get("audience", "综合分析师"),
                comparison_companies,
            )
            if local_fallback is not None:
                local_fallback["mode"] = "smart"
                local_fallback["analysis_engine"] = "local_structured_fallback"
                local_fallback["plan"] = plan
                local_fallback["subtasks"] = plan.get("subtasks") or []
                local_fallback["execution_trace"] = execution.get("trace") or []
                local_fallback["correction_log"] = [
                    trace
                    for item in execution.get("results") or []
                    for trace in item.get("correction_log") or []
                ]
                local_fallback["warnings"] = list(
                    dict.fromkeys(
                        plan_warnings + list(local_fallback.get("warnings") or [])
                    )
                )
                return local_fallback
    elif action in {"sql", "hybrid"} and plan.get("sql"):
        legacy_company = locked_company_from_request(request)
        legacy_result = execute_single_subtask(
            {
                "id": "legacy_sql",
                "kind": "sql",
                "company": legacy_company,
                "depends_on": [],
                "sql": plan.get("sql") or "",
                "expect_columns": [],
            },
            question,
            request,
        )
        frame = legacy_result.get("rows") or []
        sql_used = str(legacy_result.get("sql") or "")
        if legacy_result.get("status") == "empty":
            plan_warnings.append("未查询到匹配数据，SQL执行正常。")
        elif legacy_result.get("status") == "failed":
            plan_warnings.append(
                "SQL查询失败："
                + str(legacy_result.get("error") or "未知错误")
            )
    if action in {"rag", "hybrid"}:
        task_stage(request, "检索财报资料")
        task_wait(request)
        # 比较模式会清空会话锁定，直接拿 request 检索会一条都取不到，逐公司过滤才有效。
        for target in rag_targets_for_request(request, legacy_company):
            scoped_request = dict(request)
            scoped_request["locked_company"] = dict(target)
            for item in retrieve_rag_chunks(
                plan.get("rag_query") or question,
                top_k=16,
                request=scoped_request,
            ):
                key = (
                    str(item.get("file_path") or ""),
                    int(item.get("page_no") or 0),
                    int(item.get("chunk_index") or 0),
                )
                if key in source_keys:
                    continue
                source_keys.add(key)
                sources.append(item)

    structured_data = (
        json.dumps(frame[:50], ensure_ascii=False, default=str)
        if frame
        else "没有查询到结构化财务数据。"
    )
    rag_evidence = (
        "\n\n".join(
            f"文件：{item.get('file_name', '')}，第{item.get('page_no', 0)}页："
            f"{str(item.get('content', ''))[:1600]}"
            for item in sources
        )
        if sources
        else "没有检索到非结构化研报证据。"
    )
    context = (
        f"结构化查询结果：\n{structured_data}\n\n"
        f"财报文本证据：\n{rag_evidence}"
    )
    perspective = perspective_prompt_for(audience)
    locked = locked_company_from_request(request)
    comparison_companies = comparison_companies_from_request(request)
    answer_style = str(request.get("answer_style") or "分析师视角")
    style_rule = answer_style_rule(answer_style)
    if comparison_companies:
        allowed = "、".join(
            f"{item.get('name', '')}（{item.get('ticker', '')}）"
            for item in comparison_companies
        )
        scope_prompt = (
            f"本轮只允许比较：{allowed}。"
            "回答必须使用统一报告期和统一指标口径进行逐项比较，不得混入其他公司。"
        )
    else:
        scope_prompt = (
            f"会话锁定公司：{locked.get('name', '')}（{locked.get('ticker', '')}）。"
            "回答中不得出现其他公司的名称、证券代码或资料。"
            if locked
            else "会话尚未锁定公司；不得输出全库检索结果。"
        )
    system_content = (
        f"{perspective}\n\n"
        f"【公司范围】\n{scope_prompt}\n\n"
        f"回答风格：{answer_style}\n"
        f"风格要求：{style_rule['instruction']}\n"
        f"金额单位偏好：{request.get('unit_mode', '自动')}\n\n"
        f"【最近对话上下文】\n{history or '无'}\n\n"
        "【回答要求】\n"
        "1. 只能依据提供的数据回答，不得编造任何数字或事实；\n"
        "2. 不要输出 SQL、字段名、JSON、代码或原始数据表；\n"
        "3. 请用投资人能看懂的语言说明结论、关键数据和可能原因；\n"
        "4. 结合你当前的分析视角，突出该视角最关心的分析要点；\n"
        "5. 正文使用纯文本，不要使用 Markdown 标题、加粗、分隔线、表情或多余符号；\n"
        "6. 只有用户明确要求图表或数据表时，才可提供简洁表格作为数据源；"
        "图表生成后正文只保留结论和关键说明；\n"
        "7. 除图表数据源表格外，不要输出 |、---、##、** 等排版符号；\n"
        "8. 每个数字必须能在结构化数据或财报文本证据中直接找到；"
        "找不到就直接说明未检索到，不得推算、补全或根据常识猜测；\n"
        "9. 默认不要输出文件名、文件路径或页码；"
        "只有用户明确询问第几页、页码、出处或来源文件时，才提供文件名和页码。\n"
        "10. 提到「净利润同比」时必须注明它是**合并口径**（含少数股东损益），"
        "不得把它说成归母净利润的同比；若同一行里净利润与归母净利润一正一负，"
        "要说清盈利或亏损分别归母公司与少数股东承担。\n"
        "11. 第一句直接给结论：谁、多少、和谁比（例如「中国船舶半年赚 106.69 亿元，居首」），"
        "不要复述问题、不要以「根据数据」开头；\n"
        "12. 结论之后最多补三条关键数据，并顺手翻成大白话（例如「每 100 元收入倒亏 17.7 元」"
        "「两个口径只差 38 万元」），让人一眼看出这个数字意味着什么；\n"
        "13. 口径、样本量、报告期这类前提信息统一放到最后一句，以「口径：」开头，"
        "不要插在结论句中间（例如「口径：净利润为合并口径，样本为库内已导入该期的 26 家」）。"
    )
    if action == "hybrid" or (frame and sources):
        user_content = MULTI_SOURCE_FUSION_PROMPT.format(
            structured_data=structured_data,
            rag_evidence=rag_evidence,
            question=question,
        )
    else:
        user_content = f"问题：{question}\n\n{context}"

    client = make_client(request.get("api_key", ""), request.get("base_url", ""))
    task_stage(request, "生成回答")
    task_wait(request)
    response = client.chat.completions.create(
        model=request.get("llm_model", DEEPSEEK_MODEL),
        messages=[
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        temperature=0.1,
    )
    answer = response.choices[0].message.content or ""
    task_stage(request, "整理回答")
    task_wait(request)
    stock_code = (
        None
        if comparison_companies_from_request(request)
        else extract_stock_code(question)
    )
    if stock_code:
        company_name = load_company_name(stock_code)
        if company_name not in answer:
            answer = f"{company_name}：{answer}"
    if frame:
        # SQL 经常不 SELECT 公司名称，于是多家公司被压成同一条「数值」系列，
        # 图上看不出哪条是谁。这里按股票代码确定性回填公司名称（带缓存，不重复查库）。
        name_by_code: dict[str, str] = {}
        for row in frame:
            if not isinstance(row, dict) or any(
                key in row for key in ("公司名称", "公司", "stock_name", "公司简称", "证券简称")
            ):
                continue
            code = str(row.get("stock_code") or row.get("股票代码") or "").strip()
            if not code:
                continue
            if code not in name_by_code:
                try:
                    name_by_code[code] = str(load_company_name(code) or "")
                except Exception as exc:
                    print(f"[图表] 公司名称回填失败 {code}：{type(exc).__name__}: {exc}")
                    name_by_code[code] = ""
            if name_by_code[code]:
                original = list(row.items())
                row.clear()
                row["公司名称"] = name_by_code[code]
                for key, value in original:
                    if key != "公司名称":
                        row[key] = value
    chart = plan.get("chart")
    if chart and chart.get("type") == "none":
        chart = None
    if chart is None and frame and any(word in question for word in CHART_WORDS):
        columns = list(frame[0].keys())
        x_column = next(
            (
                column
                for column in columns
                if column in ("公司名称", "公司", "stock_name", "公司简称", "证券简称", "stock_code")
            ),
            columns[0],
        )
        numeric_column = next(
            (
                column
                for column in columns
                if isinstance(frame[0].get(column), (int, float))
                and column != x_column
                and not CHART_SKIP_COLUMNS_RE.search(str(column))
            ),
            columns[-1],
        )
        chart = {
            "type": "bar",
            "title": f"{question[:20]}对比",
            "x": x_column,
            "y": numeric_column,
        }
    if comparison_companies_from_request(request) and frame:
        chart = build_comparison_chart(frame, chart, question) or chart
    chart_note = ""
    if chart and frame:
        chart["data"] = frame
        if not validate_chart_points(chart):
            chart = None
            chart_note = "可用数据点不足，未生成图表。"
    key_points = [
        item.strip()
        for item in re.split(r"[。\n]", answer)
        if len(item.strip()) >= 8
    ][:4]
    return {
        "answer": answer,
        "summary": answer,
        "raw_table": frame,
        "table": frame,
        "chart": chart,
        "chart_note": chart_note,
        "sources": sources,
        "metrics": [],
        "key_points": key_points,
        "sql": sql_used,
        "mode": "smart",
        "company_name": load_company_name(stock_code) if stock_code else "",
        "plan": plan,
        "subtasks": plan.get("subtasks") or [],
        "execution_trace": execution.get("trace") if execution else [],
        "correction_log": [
            trace
            for item in (execution or {}).get("results") or []
            for trace in item.get("correction_log") or []
        ],
        "warnings": plan_warnings,
    }

def is_chart_style_followup(question: str) -> bool:
    """判断用户是否只在追问图表样式。"""
    compact = re.sub(r"\s+", "", question or "")
    chart_words = (
        "柱形图",
        "柱状图",
        "条形图",
        "饼图",
        "折线图",
        "面积图",
        "雷达图",
        "散点图",
        "趋势图",
        "占比图",
        "比例图",
        "双轴图",
        "图表",
    )
    if not any(word in compact for word in chart_words):
        return False
    residual = compact
    for word in chart_words:
        residual = residual.replace(word, "")
    residual = re.sub(
        r"(请|帮我|给我|再|重新|生成|画|做|用|改成|换成|换成个|一个|一张|张|个|吧|呢|的)",
        "",
        residual,
    )
    return len(residual) <= 3

def resolve_followup_question(
    question: str,
    conversation_history: list[dict[str, str]] | None = None,
    session: dict[str, Any] | None = None,
) -> str:
    """结合会话锁定和上一轮分析对象补全后续问题。

    轮廿二（用户 m02721）：「去年/今年/前年」先折算成具体年份（E1）；
    省略式追问（「那去年呢」「那净利润呢」）除继承公司名外，再补上一轮的
    期间与指标说法、带上锁定股票代码（E2），本地结构化链路才认得动；
    「怎么样/如何」类概览意图只补公司名，保持概览行为。
    """
    question = expand_relative_time_words(question)
    active_context = (
        session.get("analysis_context")
        if isinstance(session, dict)
        and isinstance(session.get("analysis_context"), dict)
        else {}
    )
    if active_context and is_chart_style_followup(question):
        locked = (
            session.get("locked_company")
            if isinstance(session, dict)
            and isinstance(session.get("locked_company"), dict)
            else {}
        )
        company_name = str(locked.get("name") or "").strip()
        context_title = str(active_context.get("title") or "").strip()
        if context_title:
            prefix = (
                context_title
                if company_name and company_name in context_title
                else f"{company_name}{context_title}"
                if company_name
                else context_title
            )
            return f"{prefix} {question}"
    if extract_stock_code(question):
        return question
    if not re.search(r"(那|其|它|该|这家|上述|呢|怎么样|如何)", question):
        return question
    if conversation_history is None:
        conversation_history = extract_session_history(
            find_chat_session(last_chat_session_id())
        )
    previous_question = ""
    for item in reversed(conversation_history):
        previous_question = str(item.get("question") or "")
        if previous_question:
            break
    locked = (
        session.get("locked_company")
        if isinstance(session, dict)
        and isinstance(session.get("locked_company"), dict)
        else {}
    )
    ticker = str(locked.get("ticker") or "").strip()
    company_name = str(locked.get("name") or "").strip()
    if not company_name:
        for item in reversed(conversation_history):
            company_name = str(item.get("company_name") or "")
            if company_name:
                break
    result = question
    if company_name and company_name not in result:
        result = f"{company_name}{result}"
    if (
        previous_question
        and len(re.sub(r"\s+", "", question)) <= 12
        and not re.search(r"怎么样|如何|情况|表现|概览", result)
        and not identify_comparison_companies(result)
    ):
        if not period_phrase_from_text(result) and period_phrase_from_text(previous_question):
            result = f"{result}（{period_phrase_from_text(previous_question)}）"
        if (
            not metric_label_from_text(result)
            and not re.search(r"率|额|比|金|值|利润|收入|支出|资产|负债|现金|存货|折旧", result)
        ):
            # 指标继承向历史深处回溯：连着几句短追问（「换成前年呢」「那去年呢」）
            # 都不带指标，只看最近一句会把指标链打断（e2e B3/B4 实测）。
            inherited_metric = ""
            for item in reversed(conversation_history):
                inherited_metric = metric_label_from_text(str(item.get("question") or ""))
                if inherited_metric:
                    break
            if inherited_metric:
                result = f"{result}{inherited_metric}"
        if ticker and ticker not in result:
            result = f"{ticker}{result}"
    return result

def last_chat_session_id() -> str | None:
    """在锁内取最近一个会话 id。

    直接写 CHAT_SESSIONS[-1] 会和「新建会话时整表替换」的线程抢：
    刚判完非空、列表被清空，请求线程就 IndexError 变 500。
    """
    with CHAT_SESSIONS_LOCK:
        return str(CHAT_SESSIONS[-1].get("id")) if CHAT_SESSIONS else None

def update_session_analysis_context(
    session: dict[str, Any],
    question: str,
    result: dict[str, Any],
) -> None:
    """保存本轮分析对象，供后续图表样式追问复用。"""
    active_context = session.get("analysis_context")
    if not isinstance(active_context, dict):
        active_context = {}
    if active_context and is_chart_style_followup(question):
        chart = result.get("chart") if isinstance(result.get("chart"), dict) else {}
        active_context["last_chart_type"] = str(chart.get("type") or "")
        active_context["last_question"] = question
        session["analysis_context"] = active_context
        return
    compact = re.sub(r"\s+", "", question or "")
    chart = result.get("chart") if isinstance(result.get("chart"), dict) else {}
    locked = session.get("locked_company")
    if not isinstance(locked, dict):
        locked = {}
    if any(
        marker in compact
        for marker in (
            "前10名股东",
            "前十大股东",
            "股东持股",
            "持股情况表",
            "股东持股比例",
        )
    ):
        title = "前十大股东持股情况表"
    elif str(chart.get("title") or "").strip():
        title = str(chart.get("title") or "").strip()
    elif any(marker in compact for marker in ("表", "指标", "数据")):
        title = question
    else:
        return
    session["analysis_context"] = {
        "title": title,
        "kind": "shareholding" if "股东" in title else "data",
        "company_ticker": str(locked.get("ticker") or ""),
        "company_name": str(locked.get("name") or ""),
        "last_chart_type": str(chart.get("type") or ""),
        "last_question": question,
    }

def complete_evidence_excerpt(
    text: str,
    limit: int = 360,
) -> str:
    """按完整句子提取审计摘要，避免中途截断。"""
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    clipped = compact[:limit]
    sentence_end = max(
        clipped.rfind("。"),
        clipped.rfind("！"),
        clipped.rfind("？"),
        clipped.rfind("；"),
    )
    if sentence_end >= max(80, limit // 3):
        return clipped[: sentence_end + 1]
    clause_end = max(
        clipped.rfind("，"),
        clipped.rfind(","),
        clipped.rfind("、"),
    )
    if clause_end >= max(60, limit // 4):
        return clipped[:clause_end] + "。"
    return clipped.rstrip("，,、；; ") + "……"

def build_evidence_summary(
    evidence: list[dict[str, Any]],
) -> dict[str, int]:
    """按业务含义统计证据数量。"""
    company_keys = set()
    source_keys = set()
    metric_count = 0
    limit_count = 0
    for item in evidence:
        item_type = str(item.get("type") or "")
        if item_type in {"metric_source", "report_text"}:
            company_key = str(
                item.get("company_ticker")
                or item.get("company_name")
                or ""
            ).strip()
            if company_key:
                company_keys.add(company_key)
        if item_type == "report_text":
            source_key = (
                str(item.get("file_name") or ""),
                int(item.get("page_no") or 0),
            )
            if any(source_key):
                source_keys.add(source_key)
        if item_type == "metric_source":
            metric_count += len(item.get("metrics") or [])
        if item_type == "data_limit":
            limit_count += len(item.get("items") or [])
    return {
        "company_count": len(company_keys),
        "source_count": len(source_keys),
        "metric_count": metric_count,
        "limit_count": limit_count,
    }

def build_evidence_chain(
    question: str,
    result: dict[str, Any],
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    """构建可审计的回答证据链。"""
    metric_evidence: list[dict[str, Any]] = []
    report_evidence: list[dict[str, Any]] = []
    grouped_sources: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for item in result.get("sources") or []:
        if not isinstance(item, dict):
            continue
        ticker = chunk_company_ticker(item)
        company_name = str(item.get("company_name") or "").strip()
        if not company_name and ticker:
            try:
                company_name = load_company_name(ticker)
            except Exception:
                company_name = ticker
        file_name = str(item.get("file_name") or "").strip()
        page_no = int(item.get("page_no") or 0)
        key = (ticker, company_name, file_name, page_no)
        group = grouped_sources.setdefault(
            key,
            {
                "type": "report_text",
                "company_ticker": ticker,
                "company_name": company_name,
                "file_name": file_name,
                "page_no": page_no,
                "content": [],
            },
        )
        content = str(item.get("content") or "").strip()
        if content and content not in group["content"]:
            group["content"].append(content)
    for group in grouped_sources.values():
        group["excerpt"] = complete_evidence_excerpt(
            " ".join(group.pop("content")),
            360,
        )
        if group["excerpt"]:
            report_evidence.append(group)
    report_evidence = report_evidence[:6]

    table = result.get("table") or []
    if table:
        metadata_keys = {
            "股票代码",
            "公司名称",
            "报告期",
            "stock_code",
            "stock_name",
            "report_period",
        }
        for row in table[:6]:
            if not isinstance(row, dict):
                continue
            company_name = str(
                next(
                    (
                        candidate
                        for candidate in (
                            row.get("公司名称"),
                            row.get("stock_name"),
                            result.get("company_name"),
                        )
                        # 「暂无数据」是表格占位符不是名字：以前 `or` 短路让它优先于
                        # 顶层正确的 result["company_name"]，证据卡跟着被污染（002595 实测）。
                        if str(candidate or "").strip() not in {"", "暂无数据", "无", "None"}
                    ),
                    "",
                )
            )
            metrics = []
            missing = []
            for key, value in row.items():
                if str(key) in metadata_keys:
                    continue
                if value in (None, "", "暂无数据", "None"):
                    missing.append(str(key))
                    continue
                metrics.append({"label": str(key), "value": value})
            source = next(
                (
                    item
                    for item in result.get("sources") or []
                    if isinstance(item, dict)
                    and (
                        not company_name
                        or str(item.get("company_name") or "") == company_name
                    )
                ),
                {},
            )
            metric_evidence.append(
                {
                    "type": "metric_source",
                    "company_name": company_name,
                    "report_period": str(
                        row.get("报告期")
                        or row.get("report_period")
                        or ""
                    ),
                    "metrics": metrics[:10],
                    "missing": missing[:6],
                    "file_name": str(source.get("file_name") or ""),
                    "page_no": int(source.get("page_no") or 0),
                    "calculation": (
                        "资产负债率 = 总负债 ÷ 总资产"
                        if any(
                            str(item.get("label") or "") == "资产负债率"
                            for item in metrics
                        )
                        else ""
                    ),
                }
            )
    limits = []
    if result.get("chart_note"):
        limits.append(str(result["chart_note"]))
    for warning in result.get("warnings") or []:
        text = str(warning).strip()
        if text:
            limits.append(text)
    if limits:
        limit_evidence = [
            {
                "type": "data_limit",
                "items": list(dict.fromkeys(limits))[:6],
            }
        ]
    else:
        limit_evidence = []
    return metric_evidence + report_evidence + limit_evidence

def validate_chat_response(question: str, result: dict[str, Any], unit_mode: str = "自动") -> dict[str, Any]:
    """校验并规范化聊天结果。"""
    warnings: list[str] = []
    answer = re.sub(r"```[\s\S]*?```", "", str(result.get("answer") or ""))
    answer = re.sub(r"^\s*SQL\s*[:：].*$", "", answer, flags=re.I | re.M)
    answer = re.sub(r"\s+", " ", answer).strip()
    if not answer:
        answer = "当前没有生成有效分析结论，请补充公司名称、报告期或指标后重试。"
        warnings.append("回答内容为空，已提示用户补充条件。")
    result["answer"] = answer
    result["summary"] = str(result.get("summary") or answer)
    raw_table = result.get("raw_table") or result.get("table") or []
    if raw_table:
        merged_table, _, merge_warnings = unified_table_from_plan_results(
            {
                "results": [
                    {
                        "status": "completed",
                        "company": {},
                        "rows": raw_table,
                        "sources": [],
                    }
                ],
                "warnings": [],
            }
        )
        result["table"] = localize_financial_table(merged_table, unit_mode)
        warnings.extend(merge_warnings)
    else:
        result["table"] = []
    result["metrics"] = result.get("metrics") or []
    result["key_points"] = result.get("key_points") or []
    chart = result.get("chart")
    if chart:
        data = chart.get("data") or []
        raw_y = chart.get("y")
        y_keys = (
            [str(item) for item in raw_y]
            if isinstance(raw_y, list)
            else [str(raw_y or "")]
        )
        valid_points = sum(
            1
            for row in data
            if isinstance(row, dict)
            and any(
                chart_number(row.get(y_key)) is not None
                for y_key in y_keys
                if y_key
            )
        )
        if len(data) < 2 or valid_points < 2:
            result["chart"] = None
            warnings.append("图表数据点不足，已取消图表展示。")
    if result.get("chart"):
        original_chart = result["chart"]
        result["chart"] = localize_financial_chart_with_meta(result["chart"])
        if result["chart"] is None:
            # 闸门（单位混着 / 系列太多）把图拦下了 ⇒ 必须说一句为什么没图，
            # 否则用户只看到表格、以为程序坏了。拦下时闸门会把原因留在原图上。
            result["chart_note"] = str(
                original_chart.get("gate_note")
                or "这些指标不适合画在一张图里，已按表格给出。"
            )
        # 图表外壳的「来源」：只写我们真说得准的口径，别编。
        chart = result["chart"]
        if isinstance(chart, dict) and not chart.get("source"):
            period = str(result.get("report_period") or result.get("period") or "").strip()
            scope = "已入库财报数据"
            result["chart"]["source"] = f"FinSight · {scope}（{period}）" if period else f"FinSight · {scope}"
    result["warnings"] = list(dict.fromkeys(warnings + list(result.get("warnings") or [])))
    return result

def foreign_entities_in_answer(
    answer: str,
    locked_company: dict[str, str],
    scoped_text: str = "",
) -> list[str]:
    """检查回答中是否混入其他公司名称或股票代码。"""
    ticker = str(locked_company.get("ticker") or "").strip()
    name = str(locked_company.get("name") or "").strip()
    if not ticker:
        return []
    violations = []
    for code in re.findall(r"(?<!\d)(\d{6})(?!\d)", answer or ""):
        if code != ticker and code not in scoped_text:
            violations.append(code)
    locked_aliases = set(company_aliases(name))
    try:
        companies = get_company_list()
    except Exception:
        companies = []
    for company in companies:
        other_ticker = str(company.get("stock_code") or "").strip()
        other_name = str(company.get("stock_name") or "").strip()
        if not other_ticker or other_ticker == ticker or not other_name:
            continue
        aliases = set(company_aliases(other_name))
        if aliases & locked_aliases:
            continue
        for alias in aliases:
            if alias and alias in answer and alias not in scoped_text:
                violations.append(alias)
                break
    return list(dict.fromkeys(violations))

def question_names_multiple_companies(question: str) -> bool:
    """问题本身是不是点了两家及以上公司（按**字面**判，不依赖名称识别成功）。

    `identify_comparison_companies` 认不出来的代码（库里 `1_stock_basic` 缺行的，
    比如 600007 只有文档没有基础信息行）⇒ 对比旗子不会开 ⇒ 单公司越界保护会把
    「A 和 B 对比」里 B 的内容当越界删掉，正确答案整份替换成
    「未在已上传文件中找到与当前公司相关的有效信息。」。
    ⇒ 越界保护要多认一条：**用户自己就点了几家**。
    两个 6 位代码之间必须是「和/与/跟/、/vs/以及」这类并列连接词，
    否则 `对比 20250630 和 20260630` 这种日期会被当成两家公司。
    """
    text = question or ""
    codes = {match.group(0) for match in re.finditer(r"(?<!\d)\d{6}(?!\d)", text)}
    if len(codes) >= 2:
        return True
    if len(identify_comparison_companies(text)) >= 2:
        return True
    # 「对比云南白药和长城汽车」这种**名字型**对比：库里没有第二家 ⇒ 名称识别只认
    # 得到一家 ⇒ 对比旗子不开 ⇒ 正确答案被越界保护整份删掉（和 600007 同一类事故）。
    # 兜法：有对比词 + 问题里能读到两个像公司名的词尾 ⇒ 就当用户点了两家。
    # 误判的代价只是「少删一份答案」，漏判的代价是删掉正确结果，取轻。
    if not re.search(r"对比|比较|哪家|谁更|比起|vs", text, re.I):
        return False
    tails = (
        "汽车|科技|集团|股份|公司|电子|医药|药业|药|能源|电力|燃气|银行|证券|保险"
        "|地产|控股|化工|机械|装备|生物|通信|软件|食品|环保|建材|物流|纺织|传媒"
        "|军工|航空|船舶|重工|矿山|发展"
    )
    names = set(re.findall(rf"[\u4e00-\u9fa5]{{2,6}}(?:{tails})", text))
    return len(names) >= 2

def enforce_company_scope(
    result: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    """阻止跨公司内容进入最终答案。"""
    if request.get("comparison_mode"):
        return result
    # 问题里本来就点了两家 ⇒ 不能拿单公司口径杀答案（哪怕系统只认出一家）
    if question_names_multiple_companies(str(request.get("question") or "")):
        return result
    locked = locked_company_from_request(request)
    if not locked:
        return result
    answer_text = "\n".join(
        [
            str(result.get("answer") or ""),
            str(result.get("summary") or ""),
            *[str(item) for item in result.get("key_points") or []],
        ]
    )
    if isinstance(result.get("chart"), dict):
        answer_text += "\n" + json.dumps(
            result["chart"],
            ensure_ascii=False,
            default=str,
        )
    scoped_text = "\n".join(
        str(item.get("content") or "")
        for item in result.get("sources") or []
        if isinstance(item, dict)
    )
    violations = foreign_entities_in_answer(answer_text, locked, scoped_text)
    if not violations:
        return result
    print(
        "[ANSWER_SCOPE] blocked "
        f"company_ticker={locked.get('ticker', '')} "
        f"foreign_entities={'|'.join(violations[:5])}"
    )
    return {
        "answer": "未在已上传文件中找到与当前公司相关的有效信息。",
        "summary": "未在已上传文件中找到与当前公司相关的有效信息。",
        "table": [],
        "chart": None,
        "chart_note": "",
        "sources": [],
        "metrics": [],
        "key_points": [],
        "warnings": [],
        "mode": "guide",
        "company_name": locked.get("name", ""),
    }

def answer_with_llm_clarification(
    question: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """让模型生成追问提示。"""
    client = make_client(request.get("api_key", ""), request.get("base_url", ""))
    perspective = perspective_prompt_for(request.get("audience", "综合分析师"))
    response = client.chat.completions.create(
        model=request.get("llm_model", DEEPSEEK_MODEL),
        messages=[
            {
                "role": "system",
                "content": (
                    f"{perspective}\n\n{INTENT_CLARIFICATION_PROMPT}"
                ),
            },
            {"role": "user", "content": f"用户问题：{question}"},
        ],
        temperature=0.2,
    )
    answer = (response.choices[0].message.content or "").strip()
    if not answer:
        raise ValueError("模型未返回澄清问题")
    return {
        "answer": answer,
        "summary": answer,
        "table": [],
        "chart": None,
        "sources": [],
        "metrics": [],
        "key_points": [],
        "clarification_options": [
            "盈利情况",
            "成长趋势",
            "偿债能力",
            "现金流质量",
            "经营风险",
        ],
        "warnings": [],
        "mode": "clarify",
        "company_name": "",
    }

def answer_missing_comparison_targets(
    question: str,
    resolved: list[dict[str, str]],
) -> dict[str, Any] | None:
    """对比题里点了多家、但其中有公司没入库 ⇒ 直说数据不足，不要偷偷降级成单公司。"""
    compact = re.sub(
        r"(?:19|20)\d{2}\s*年|\s+",
        "",
        question or "",
    )  # 去掉「2026年」这类年份，否则 0000022026年半年报 里的代码没有数字边界、抓不到
    if len(resolved or []) >= 2 or not re.search(
        r"(对比|对照|比较|相比|比一下|vs|VS)", compact
    ):
        return None
    known_codes: set[str] = set()
    try:
        known_codes = {
            str(item.get("stock_code") or "").strip()
            for item in get_company_list()
            if str(item.get("stock_code") or "").strip()
        }
    except Exception as exc:
        print(f"[对比缺数] 取公司列表失败，跳过数据不足判定：{exc}")
        return None
    # 自己数问题里点名的 A 股代码：identify_comparison_companies 只返回「库里有的」，
    # 一家都没匹上时是空列表，不能拿它当「用户点了几家」的依据。
    asked = [
        code
        for code in dict.fromkeys(re.findall(r"(?<!\d)(\d{6})(?!\d)", compact))
        if re.match(r"^(0[0-3]|[1-6]\d)\d{4}$", code)
    ]
    if len(asked) < 2:
        return None
    missing = [code for code in asked if code not in known_codes]
    if not missing:
        return None
    have = "、".join(
        str(item.get("name") or item.get("ticker") or "").strip()
        for item in (resolved or [])
        if str(item.get("name") or item.get("ticker") or "").strip()
    )
    if not have:
        have = "、".join(code for code in asked if code in known_codes)
    if have:
        answer = (
            f"数据不足：{'、'.join(missing)} 还没有入库，暂时没法和{have}做对比。"
            f"请先上传{'、'.join(missing)} 的年报/半年报，入库后再问这道题。"
        )
    else:
        answer = (
            f"数据不足：{'、'.join(missing)} 都还没有入库，没法对比。"
            "请先上传这些公司的年报/半年报，入库后再问这道题。"
        )
    return {
        "answer": answer,
        "summary": "对比对象未全部入库，已如实返回数据不足。",
        "table": [],
        "chart": None,
        "sources": [],
        "metrics": [],
        "key_points": [f"缺少入库数据：{'、'.join(missing)}。"],
        "warnings": [f"数据不足：{'、'.join(missing)} 尚未入库"],
        "mode": "guide",
        "company_name": "",
    }

def build_clarification_response(question: str) -> dict[str, Any] | None:
    """在本地生成缺少条件时的追问。"""
    company_code, company_name = identify_company_filter(question)
    if extract_stock_code(question) or company_code or company_name:
        return None
    if not re.search(
        r"(分析|看看|怎么样|如何|情况|表现|风险|股东|持股|指标|财务|营业收入|净利润)",
        question,
    ):
        return None
    return {
        "answer": "请补充需要分析的公司名称或股票代码；如涉及具体指标，请同时说明报告期。",
        "summary": "需要先确认公司，再继续分析。",
        "table": [],
        "chart": None,
        "sources": [],
        "metrics": [],
        "key_points": ["请提供公司名称或股票代码。", "如需单点数据，请补充指标名称和报告期。"],
        "clarification_options": ["盈利情况", "成长趋势", "偿债能力", "现金流质量", "经营风险"],
        "warnings": [],
        "mode": "clarify",
        "company_name": "",
    }

def task_log(request: dict[str, Any], event: dict[str, Any]) -> None:
    """记录当前聊天任务的执行事件。"""
    logs = request.setdefault("_log_events", [])
    logs.append(
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            **event,
        }
    )
    if len(logs) > 200:
        del logs[:-200]

def task_stage(request: dict[str, Any], stage: str) -> None:
    """更新后台聊天任务的当前处理阶段。"""
    task_log(request, {"type": "stage", "stage": stage})
    callback = request.get("_set_stage")
    if callable(callback):
        callback(stage)

def task_wait(request: dict[str, Any]) -> None:
    """在任务暂停时阻塞后台线程，恢复后继续执行。"""
    callback = request.get("_pause_wait")
    if callable(callback):
        callback()

def find_chat_session(session_id: str | None) -> dict[str, Any] | None:
    """按会话编号查找当前聊天会话。"""
    if not session_id:
        return None
    with CHAT_SESSIONS_LOCK:
        return next(
            (item for item in CHAT_SESSIONS if item.get("id") == session_id),
            None,
        )

def extract_session_history(session: dict[str, Any] | None) -> list[dict[str, str]]:
    """提取当前会话最近几轮消息作为模型上下文。

    find_chat_session 交回来的是**活的**会话对象，锁只保护了查找；
    回答线程还在往 message["result"] 里补字段时这里遍历同一个 dict，
    就会 RuntimeError: dictionary changed size during iteration，
    被外层 except 吞掉后表现成「历史莫名丢了」。所以先在锁内拍快照。
    """
    if not session:
        return []
    with CHAT_SESSIONS_LOCK:
        snapshot = [
            (
                str(message.get("role") or ""),
                str(message.get("content") or ""),
                dict(message.get("result") or {}),
            )
            for message in (session.get("messages") or [])
        ]
    history = []
    pending_question = ""
    for role, content, result in snapshot:
        if role == "user":
            pending_question = content
        elif role == "assistant" and pending_question:
            history.append(
                {
                    "question": pending_question,
                    "answer": str(result.get("answer") or ""),
                    "company_name": str(result.get("company_name") or ""),
                }
            )
            pending_question = ""
    return history[-5:]

def append_session_exchange(
    session: dict[str, Any] | None,
    question: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """向当前会话追加用户消息和助手回答。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with CHAT_SESSIONS_LOCK:
        if session is not None and session.get("_deleted"):
            result["session_id"] = session.get("id", "")
            result["question"] = question
            return session
        if session is None:
            session = {
                "id": uuid.uuid4().hex,
                "title": question[:30],
                "updated_at": now,
                "messages": [],
                "locked_company": {},
                "locked_deal": {},
                "analysis_context": {},
            }
        if not any(item.get("id") == session.get("id") for item in CHAT_SESSIONS):
            CHAT_SESSIONS.append(session)
        result["session_id"] = session["id"]
        result["question"] = question
        session["updated_at"] = now
        session.setdefault("title", question[:30])
        session.setdefault("messages", []).extend(
            [
                {
                    "id": uuid.uuid4().hex,
                    "role": "user",
                    "content": question,
                    "created_at": now,
                },
                {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "result": result,
                    "created_at": now,
                },
            ]
        )
        save_chat_sessions()
        return session

CHAT_TASK_RETENTION_SECONDS = 30 * 60
CHAT_TASK_MAX_ENTRIES = 128

def prune_chat_tasks(tasks: dict[str, Any]) -> int:
    """清掉跑完不久的后台任务，返回删除条数。

    每个问题都会往 CHAT_TASKS 塞一条含完整 result 的记录，原来只有增没有删：
    连着问几十条就是一份常驻的会话数据副本。
    """
    now = time.time()
    finished = [
        key
        for key, item in tasks.items()
        if str(item.get("status") or "") in {"completed", "failed"}
    ]
    expired = [
        key
        for key in finished
        if now - float(tasks[key].get("created_at") or 0) > CHAT_TASK_RETENTION_SECONDS
    ]
    for key in expired:
        tasks.pop(key, None)
    overflow = len(tasks) - CHAT_TASK_MAX_ENTRIES
    if overflow > 0:
        remaining = sorted(
            (key for key in finished if key in tasks),
            key=lambda key: float(tasks[key].get("created_at") or 0),
        )
        for key in remaining[:overflow]:
            tasks.pop(key, None)
    return len(expired) + max(overflow, 0)

def start_chat_task(request: dict[str, Any]) -> dict[str, Any]:
    """创建后台聊天任务并立即返回任务状态。"""
    # 空问题以前会走通：`web.py` 的 `/api/chat/start` 直接建任务返回 200，
    # 真正的 `ValueError("问题不能为空")` 要到后台 worker 里的
    # `handle_chat_request` 才抛（本文件 5023-5025 行）⇒ 任务落地成 failed，
    # 还白占一个 `CHAT_TASKS` 槽位。好消息是校验在调用模型之前，
    # 所以不烧配额；坏消息是客户端拿到的是 200 + 一个注定失败的任务。
    # 入参校验属于「这一层就该拒」，所以在建任务之前先拦（`do_POST` 把
    # ValueError 映射成 400，和同步的 `/api/chat` 表现一致）。
    if not str(request.get("question") or "").strip():
        raise ValueError("问题不能为空")
    task_id = uuid.uuid4().hex
    pause_event = threading.Event()
    pause_event.set()
    state: dict[str, Any] = {
        "id": task_id,
        "status": "running",
        "stage": "准备问题",
        "paused": False,
        "event": pause_event,
        "result": None,
        "error": "",
        "logs": [],
        "created_at": time.time(),
    }
    with CHAT_TASKS_LOCK:
        CHAT_TASKS[task_id] = state
        prune_chat_tasks(CHAT_TASKS)
    task_request = dict(request)

    def set_stage(stage: str) -> None:
        """更新后台任务的当前处理阶段。"""
        state["stage"] = stage

    def pause_wait() -> None:
        """在任务暂停时阻塞线程，恢复后继续执行。"""
        while not pause_event.is_set():
            state["status"] = "paused"
            state["paused"] = True
            pause_event.wait(0.25)
        if state["status"] == "paused":
            state["status"] = "running"
            state["paused"] = False
            state["stage"] = "继续处理"

    task_request["_set_stage"] = set_stage
    task_request["_pause_wait"] = pause_wait
    task_request["_log_events"] = state["logs"]

    def worker() -> None:
        """后台执行一次聊天分析并保存最终状态。"""
        try:
            task_wait(task_request)
            result = handle_chat_request(task_request)
            # 终态一次写完并和暂停/恢复互斥：否则用户正好在结果落地那刻点了暂停，
            # 这边写 completed、那边写 paused，谁后写谁赢，界面就停在「已暂停」不动了。
            with CHAT_TASKS_LOCK:
                state["result"] = result
                state["status"] = "completed"
                state["stage"] = "完成"
                state["paused"] = False
        except Exception as exc:
            task_log(
                task_request,
                {
                    "type": "error",
                    "error": str(exc),
                },
            )
            with CHAT_TASKS_LOCK:
                state["status"] = "failed"
                state["stage"] = "处理失败"
                state["error"] = str(exc)
                state["paused"] = False

    threading.Thread(target=worker, daemon=True).start()
    return load_chat_task_status(task_id)

def load_chat_task_status(task_id: str) -> dict[str, Any]:
    """返回聊天任务的可序列化状态。"""
    with CHAT_TASKS_LOCK:
        state = CHAT_TASKS.get(task_id)
        if state is None:
            raise KeyError("聊天任务不存在")
        return {
            "task_id": task_id,
            "status": state.get("status", ""),
            "stage": state.get("stage", ""),
            "paused": bool(state.get("paused")),
            "result": state.get("result"),
            "error": state.get("error", ""),
            "logs": list(state.get("logs") or []),
        }

def pause_chat_task(task_id: str) -> dict[str, Any]:
    """暂停指定聊天任务。"""
    with CHAT_TASKS_LOCK:
        state = CHAT_TASKS.get(task_id)
        if state is None:
            raise KeyError("聊天任务不存在")
        # 已经跑完的任务不再改写状态：把 completed 覆盖成 paused，
        # 前端就再也等不到结果（工作线程早退了，没人会把它设回去）。
        if str(state.get("status") or "") not in {"completed", "failed"}:
            state["event"].clear()
            state["status"] = "paused"
            state["paused"] = True
            state["stage"] = "已暂停"
    return load_chat_task_status(task_id)

def resume_chat_task(task_id: str) -> dict[str, Any]:
    """恢复指定聊天任务。"""
    with CHAT_TASKS_LOCK:
        state = CHAT_TASKS.get(task_id)
        if state is None:
            raise KeyError("聊天任务不存在")
        # 同理：对已结束的任务写 running 会让进度条无限转圈。
        if str(state.get("status") or "") not in {"completed", "failed"}:
            state["event"].set()
            state["status"] = "running"
            state["paused"] = False
            state["stage"] = "继续处理"
    return load_chat_task_status(task_id)

def handle_chat_request(request: dict[str, Any]) -> dict[str, Any]:
    """处理聊天请求并保存会话。"""
    question = str(request.get("question", "")).strip()
    if not question:
        raise ValueError("问题不能为空")
    thinking_started = time.perf_counter()  # 「已思考 N 秒」= 从理解问题到结果成型
    request = dict(request)
    request = configure_analysis_mode(request)
    session = find_chat_session(str(request.get("session_id") or ""))
    if session is None:
        session = {
            "id": uuid.uuid4().hex,
            "title": question[:30],
            "updated_at": "",
            "messages": [],
            "locked_company": {},
            "locked_deal": {},
            "analysis_context": {},
        }
    previous_ticker = str(
        (session.get("locked_company") or {}).get("ticker") or ""
    )
    comparison_companies = identify_comparison_companies(question)
    company_context_changed = False
    if comparison_companies:
        request["comparison_companies"] = comparison_companies
        request["comparison_mode"] = True
        session["locked_company"] = {}
        session["locked_deal"] = {}
        session["analysis_context"] = {
            "kind": "comparison",
            "companies": comparison_companies,
            "last_question": question,
        }
        lock_result = None
        company_context_changed = True
    else:
        lock_result = lock_company_for_session(session, question)
        current_ticker = str(
            (session.get("locked_company") or {}).get("ticker") or ""
        )
        company_context_changed = bool(
            previous_ticker
            and current_ticker
            and previous_ticker != current_ticker
        )
    request["locked_company"] = dict(session.get("locked_company") or {})
    request["locked_deal"] = dict(session.get("locked_deal") or {})
    request["conversation_history"] = (
        []
        if company_context_changed
        else extract_session_history(session)
    )
    resolved_question = resolve_followup_question(
        question,
        request["conversation_history"],
        session,
    )
    task_type = classify_question_task(resolved_question)
    shortage = answer_missing_comparison_targets(resolved_question, comparison_companies)
    if shortage:
        return shortage
    task_result = None
    if (
        lock_result is None
        and not request.get("comparison_mode")
        and locked_company_from_request(request)
    ):
        if task_type == "operating_overview":
            task_result = answer_with_operating_overview(
                resolved_question,
                request,
            )
        elif task_type == "risk_text":
            task_result = answer_with_risk_analysis(
                resolved_question,
                request,
            )
        elif task_type in {"shareholding_table", "chart_restyle"}:
            task_result = answer_with_local_rag(resolved_question, request)
            if task_result is None:
                task_result = build_scoped_not_found_response(
                    request.get("locked_company", {}).get("name", ""),
                    "未在已上传文件中找到可用于当前图表的完整表格数据。",
                )
    task_stage(request, "理解问题")
    task_wait(request)

    overview = (
        None
        if lock_result is not None or request.get("comparison_mode")
        else answer_with_overview(resolved_question)
        or answer_cross_company_ranking(
            resolved_question,
            request.get("unit_mode", "自动"),
        )
    )
    if lock_result is not None:
        result = lock_result
    elif task_result is not None:
        result = task_result
        if request.get("analysis_mode") == "deepseek":
            result["mode"] = "smart"
            result["analysis_engine"] = "structured_evidence"
    elif overview is not None:
        task_stage(request, "统计数据库")
        task_wait(request)
        result = overview
    elif request.get("api_key"):
        task_stage(request, "规划分析路径")
        task_wait(request)
        local_clarification = (
            None
            if request.get("comparison_mode")
            or locked_company_from_request(request)
            else build_clarification_response(resolved_question)
        )
        if local_clarification is not None:
            try:
                result = answer_with_llm_clarification(resolved_question, request)
            except Exception:
                result = local_clarification
        else:
            try:
                task_stage(request, "生成云端回答")
                result = answer_with_llm(resolved_question, request)
            except Exception as exc:
                print(
                    "[LLM_FALLBACK] "
                    f"company_ticker={request.get('locked_company', {}).get('ticker', '')} "
                    f"error={exc}"
                )
                task_stage(request, "切换本地分析")
                result = answer_with_multi_company_data(
                    resolved_question,
                    request.get("unit_mode", "自动"),
                    request.get("audience", "综合分析师"),
                    comparison_companies_from_request(request),
                )
                if result is None:
                    result = answer_with_structured_data(
                        resolved_question,
                        request.get("unit_mode", "自动"),
                        request.get("audience", "综合分析师"),
                    )
                if result is None:
                    result = answer_with_local_rag(resolved_question, request)
                if result is None:
                    result = build_clarification_response(resolved_question)
                if result is None:
                    raw_error = str(exc).strip()
                    if any(
                        marker in raw_error.lower()
                        for marker in ("connection", "timed out", "timeout")
                    ):
                        friendly_error = (
                            "无法连接 DeepSeek，请检查服务器网络或代理配置"
                        )
                    else:
                        friendly_error = raw_error or "智能分析请求失败"
                    result = {
                        "answer": (
                            "智能分析暂时不可用，已使用本地数据结果。"
                            f"原因：{friendly_error}。"
                        ),
                        "table": [],
                        "chart": None,
                        "sources": [],
                        "mode": "guide",
                    }
    else:
        task_stage(request, "查询本地数据")
        task_wait(request)
        result = answer_with_multi_company_data(
            resolved_question,
            request.get("unit_mode", "自动"),
            request.get("audience", "综合分析师"),
            comparison_companies_from_request(request),
        )
        if result is None:
            result = answer_with_structured_data(
                resolved_question,
                request.get("unit_mode", "自动"),
                request.get("audience", "综合分析师"),
            )
        if result is None:
            result = answer_with_local_rag(resolved_question, request)
        if result is None:
            result = build_clarification_response(resolved_question)
        if result is None:
            result = {
                "answer": (
                    "本地模式只查已入库的财报：请给出「公司（股票代码、全称或证券简称）＋指标＋报告期」，"
                    "例如「002595 2026年半年报 净利润」或「豪迈科技 2026年半年报 营业收入」。"
                    "想按自然语言追问、做归因或对比分析，请切到「智能模式」。"
                ),
                "table": [],
                "chart": None,
                "sources": [],
                "mode": "guide",
            }
    locked_company = locked_company_from_request(request)
    if locked_company and not result.get("company_name"):
        result["company_name"] = locked_company.get("name") or locked_company.get(
            "ticker",
            "",
        )
    task_stage(request, "整理回答")
    task_wait(request)
    result = ensure_chart_data(resolved_question, result, request)
    answer_tables = extract_markdown_tables(str(result.get("answer") or ""))
    if answer_tables and not result.get("table"):
        result["table"] = answer_tables[0]
    result["answer"] = strip_markdown_tables(str(result.get("answer") or ""))
    result["summary"] = strip_markdown_tables(
        str(result.get("summary") or result.get("answer") or "")
    )
    result["answer"] = clean_answer_text(str(result.get("answer") or ""))
    result["summary"] = clean_answer_text(
        str(result.get("summary") or result.get("answer") or "")
    )
    result["key_points"] = [
        cleaned
        for item in result.get("key_points") or []
        if (cleaned := clean_answer_text(str(item)))
    ]
    if not question_wants_page_reference(question):
        result["answer"] = strip_source_citations(result["answer"])
        result["summary"] = strip_source_citations(result["summary"])
        result["key_points"] = [
            cleaned
            for item in result["key_points"]
            if (cleaned := strip_source_citations(str(item)))
        ]
    if result.get("chart"):
        result["answer"] = remove_chart_limitation_text(result["answer"])
        result["summary"] = remove_chart_limitation_text(result["summary"])
        result["key_points"] = [
            remove_chart_limitation_text(item) for item in result["key_points"]
        ]
    wants_table = (
        ("表" in resolved_question and "图表" not in resolved_question)
        or "明细" in resolved_question
    )
    multi_company_result = bool(
        request.get("comparison_mode")
        or len(result.get("table") or []) > 1
        or (isinstance(result.get("plan"), dict) and result["plan"].get("companies"))
    )
    if (
        result.get("chart")
        and not wants_table
        and not multi_company_result
    ):
        result["table"] = []
    result = apply_answer_style(
        result,
        str(request.get("answer_style") or "分析师视角"),
    )
    result.setdefault("raw_table", result.get("table") or [])
    result = validate_chat_response(
        question, result, str(request.get("unit_mode") or "自动")
    )
    result = enforce_company_scope(result, request)
    result["summary"] = str(result.get("summary") or result.get("answer") or "")
    result = normalize_result_endings(result)
    result["evidence"] = build_evidence_chain(
        question,
        result,
        request,
    )
    result["evidence_summary"] = build_evidence_summary(
        result.get("evidence") or []
    )
    # 执行日志补真：快速直查路径（锁定公司直答/任务直答/统计概览/本地模式）
    # 不走规划器，一条 subtask_done 都不落，右侧「执行日志」就永远显示
    # 成功0·无数据0·失败0——明明答对了却像没干活。这里补记一条真实执行事件，
    # 状态、行数、出处数、耗时都取自已定型的 result，不掺假。
    _log_events = request.get("_log_events")
    if isinstance(_log_events, list) and not any(
        isinstance(item, dict) and item.get("type") == "subtask_done"
        for item in _log_events
    ):
        task_log(
            request,
            {
                "type": "subtask_done",
                "subtask_id": "direct",
                "kind": "direct",
                "company": str(result.get("company_name") or ""),
                "status": "empty" if str(result.get("mode") or "") == "guide" else "completed",
                "rows": len(result.get("table") or []),
                "sources": len(result.get("evidence") or []),
                "attempts": 1,
                "duration": round(time.perf_counter() - thinking_started, 2),
                "purpose": "本地直查（未走任务规划）",
            },
        )
    result["execution_logs"] = list(request.get("_log_events") or [])
    result["sources"] = []
    # 先写思考时长再落会话：历史里重开这条回答也带着它
    result["thinking_seconds"] = round(time.perf_counter() - thinking_started, 2)
    update_session_analysis_context(session, question, result)
    session = append_session_exchange(session, question, result)
    result["session_id"] = session["id"]
    result["question"] = question
    return result

def get_chat_history() -> list[dict[str, Any]]:
    """返回聊天历史列表。"""
    with CHAT_SESSIONS_LOCK:
        sessions = list(CHAT_SESSIONS[-50:])
    return [
        {
            "id": item["id"],
            "title": item.get("title") or "未命名会话",
            "updated_at": item.get("updated_at", ""),
            "message_count": len(item.get("messages") or []),
        }
        for item in reversed(sessions)
    ]

def get_chat_session_detail(session_id: str) -> dict[str, Any]:
    """返回单条聊天历史详情（副本，避免调用方改写共享会话对象）。"""
    with CHAT_SESSIONS_LOCK:
        for item in CHAT_SESSIONS:
            if item["id"] == session_id:
                return json.loads(json.dumps(item, ensure_ascii=False, default=str))
        raise KeyError("历史记录不存在")

def delete_all_chat_sessions() -> dict[str, str]:
    """清空聊天历史。"""
    with CHAT_SESSIONS_LOCK:
        for item in CHAT_SESSIONS:
            item["_deleted"] = True
        CHAT_SESSIONS.clear()
        save_chat_sessions()
        return {"status": "deleted"}

def delete_chat_session(session_id: str) -> dict[str, str]:
    """删除指定聊天历史。"""
    with CHAT_SESSIONS_LOCK:
        for item in CHAT_SESSIONS:
            if item.get("id") == session_id:
                item["_deleted"] = True
        CHAT_SESSIONS[:] = [
            item for item in CHAT_SESSIONS if item.get("id") != session_id
        ]
        save_chat_sessions()
        return {"status": "deleted"}

EVALUATION_DATASET_SCHEMA = [
    {
        "id": "Q001",
        "question": "重庆美心翼申机械股份有限公司的资产负债率是多少？",
        "expected_sql": "SELECT total_assets,total_liability FROM 4_balance_sheet WHERE stock_code='FILE_8B82CC65'",
        "expected_result": [{"total_assets": "54274820000", "total_liability": "12105375689"}],
        "conversation_context": [],
        "expected_company": "重庆美心翼申机械股份有限公司",
        "expected_error_type": None,
        "requires_correction": False,
    }
]

def normalize_eval_value(value: Any) -> Any:
    """规范化评测值，避免字符串、Decimal 和数字格式造成误判。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    factor = Decimal(1)
    if text.endswith("亿元"):
        factor = Decimal("100000000")
        text = text[:-2]
    elif text.endswith("万元"):
        factor = Decimal("10000")
        text = text[:-2]
    elif text.endswith("元"):
        text = text[:-1]
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = Decimal(text) * factor
        return format(number, "f").rstrip("0").rstrip(".")
    except (InvalidOperation, ValueError):
        return re.sub(r"\s+", "", str(value))

def eval_rows_match(
    actual_rows: list[dict[str, Any]],
    expected_rows: list[dict[str, Any]],
) -> bool:
    """按预期字段子集比较查询结果。"""
    if not expected_rows:
        return not actual_rows

    def values_equal(left: Any, right: Any) -> bool:
        normalized_left = normalize_eval_value(left)
        normalized_right = normalize_eval_value(right)
        try:
            left_number = Decimal(str(normalized_left))
            right_number = Decimal(str(normalized_right))
            tolerance = max(
                Decimal("0.01"),
                abs(right_number) * Decimal("0.0005"),
            )
            return abs(left_number - right_number) <= tolerance
        except (InvalidOperation, ValueError):
            return normalized_left == normalized_right

    for expected in expected_rows:
        matched = False
        for actual in actual_rows:
            if all(
                (key in actual or COLUMN_LABELS.get(key) in actual)
                and values_equal(
                    actual.get(key, actual.get(COLUMN_LABELS.get(key))),
                    value,
                )
                for key, value in expected.items()
            ):
                matched = True
                break
        if not matched:
            return False
    return True

def percentile(sorted_values: list[float], ratio: float) -> float:
    """计算简单百分位，使用最近秩法。"""
    if not sorted_values:
        return 0.0
    index = min(
        len(sorted_values) - 1,
        max(0, int(round((len(sorted_values) - 1) * ratio))),
    )
    return sorted_values[index]

def evaluate_question_set(
    dataset: list[dict[str, Any]],
    request_base: dict[str, Any],
) -> dict[str, Any]:
    """Evaluation framework for Text-to-SQL, latency, context and correction."""
    details: list[dict[str, Any]] = []
    for item in dataset:
        started = time.perf_counter()
        request = dict(request_base)
        request["question"] = item.get("question", "")
        request["conversation_history"] = item.get("conversation_context", [])
        result = handle_chat_request(request)
        elapsed = time.perf_counter() - started
        actual_result = result.get("raw_table") or result.get("table") or []
        result_correct = eval_rows_match(
            actual_result,
            item.get("expected_result") or [],
        )
        context_correct = (
            not item.get("expected_company")
            or item["expected_company"] == result.get("company_name")
        )
        error_detected = bool(result.get("warnings")) or bool(
            result.get("correction_log")
        )
        correction_ok = result_correct or not result.get("warnings")
        details.append(
            {
                "id": item.get("id"),
                "sql_correct": result_correct,
                "result_correct": result_correct,
                "context_correct": context_correct,
                "latency_seconds": elapsed,
                "error_detected": error_detected,
                "correction_ok": correction_ok,
                "expected_error_type": item.get("expected_error_type"),
                "requires_correction": bool(item.get("requires_correction")),
                "expected_company": item.get("expected_company"),
            }
        )
    total = max(1, len(details))
    context_cases = [
        item for item in details if item.get("expected_company")
    ]
    error_cases = [
        item for item in details if item.get("expected_error_type")
    ]
    correction_cases = [
        item for item in details if item.get("requires_correction")
    ]
    latencies = sorted(item["latency_seconds"] for item in details)
    return {
        "text_to_sql_accuracy": sum(x["sql_correct"] for x in details) / total,
        "query_result_accuracy": sum(
            x["result_correct"] for x in details
        ) / total,
        "multi_turn_accuracy": (
            sum(x["context_correct"] for x in context_cases)
            / max(1, len(context_cases))
        ),
        "error_detection_rate": (
            sum(x["error_detected"] for x in error_cases)
            / max(1, len(error_cases))
        ),
        "correction_rate": (
            sum(x["correction_ok"] for x in correction_cases)
            / max(1, len(correction_cases))
        ),
        "average_latency_seconds": sum(x["latency_seconds"] for x in details) / total,
        "p95_latency_seconds": percentile(latencies, 0.95),
        "sample_count": len(details),
        "context_sample_count": len(context_cases),
        "error_sample_count": len(error_cases),
        "correction_sample_count": len(correction_cases),
        "details": details,
    }
