"""Web 服务模块。负责 HTTP 路由、静态资源、前端页面和 API 请求处理。"""
from __future__ import annotations  # 启用延迟类型注解，兼容较早的 Python 版本

# Python 标准库：HTTP 服务、网络请求、文件类型和路由解析。
import errno  # 识别端口占用等系统错误码
import hashlib  # 计算文件和文本的 SHA256 摘要
import hmac  # 登录会话令牌的签名与恒时比较
import io  # 导出 docx：把 python-docx 文档存进内存字节流
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

# 第三方依赖：数据库和接口依赖。
import pandas as pd  # 生成和展示财务数据表
import pdfplumber  # 提取 PDF 文本和表格
import pymysql  # 连接和操作 MySQL 数据库
import pypdfium2 as pdfium  # 渲染扫描版 PDF 页面
from docx import Document  # 读取 Word 文档内容
from docx.enum.section import WD_ORIENT  # 导出 docx：页面方向（A4 纵向总闸）
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT  # 导出 docx：表格单元格垂直对齐
from docx.enum.text import WD_ALIGN_PARAGRAPH  # 导出 docx：段落对齐
from docx.oxml import OxmlElement  # 导出 docx：三线表边框/页码域等 OOXML 细节
from docx.oxml.ns import qn as docx_qn  # 导出 docx：OOXML 属性名限定
from docx.shared import Cm as docx_cm  # 导出 docx：厘米
from docx.shared import Mm as docx_mm  # 导出 docx：毫米（A4 页面尺寸）
from docx.shared import Pt as docx_pt  # 导出 docx：磅（字号/线宽）
from docx.shared import RGBColor as docx_rgb  # 导出 docx：字体颜色
from openai import OpenAI  # 调用云端大模型和嵌入模型
from pymysql.cursors import DictCursor  # 使用字典游标读取数据库结果

# 项目内部模块：读取配置和共享运行状态。
from config import *  # 导入项目配置、路径和共享状态

# 数据层接口：处理文件、数据库和财务数据请求。
from data import *  # 导入数据层函数和共享数据

# 智能问答接口：处理聊天、图表和历史记录请求。
from assistant import *  # 导入智能问答和图表函数

SERVER_INSTANCE: ThreadingHTTPServer | None = None

# —————— 登录闸门 ——————
# 仅当 .env 同时设置 LOGIN_USER 与 LOGIN_PASSWORD 时启用；两者缺一 = 全站照旧开放
# （回归测试与未配置环境零影响）。口令校验用 compare_digest，会话是 HMAC 签名的
# 过期时间戳放进 HttpOnly cookie，改密码即让所有旧会话作废（密钥派生自口令）。
SESSION_COOKIE = "fsession"
SESSION_TTL_SECONDS = 30 * 24 * 3600
LOGIN_PUBLIC_PATHS = {"/login", "/api/login", "/api/logout"}

def auth_enabled() -> bool:
    # FINSIGHT_DISABLE_LOGIN=1：开发/截图用的临时总闸（只影响进程环境，
    # 本机进程本来就能直调 data/web 函数，没引入新攻击面）。
    if os.getenv("FINSIGHT_DISABLE_LOGIN") == "1":
        return False
    return bool(os.getenv("LOGIN_USER")) and bool(os.getenv("LOGIN_PASSWORD"))

def _session_secret() -> bytes:
    seed = "|".join(
        [os.getenv("LOGIN_USER", ""), os.getenv("LOGIN_PASSWORD", ""), "FinSight-session-salt-v1"]
    )
    return hashlib.sha256(seed.encode("utf-8")).digest()

def make_session_token() -> str:
    expiry = str(int(time.time()) + SESSION_TTL_SECONDS)
    digest = hmac.new(_session_secret(), expiry.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expiry}.{digest}"

def verify_session_token(token: str) -> bool:
    expiry, _, digest = (token or "").partition(".")
    if not expiry.isdigit():
        return False
    expect = hmac.new(_session_secret(), expiry.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expect) and int(expiry) > time.time()

def import_job_elapsed(payload: dict) -> float | None:
    """给界面算「已用时」：started_at 到 finished_at，未结束就用当前时间。"""
    try:
        started = datetime.strptime(
            str(payload.get("started_at") or ""), "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return None
    raw_end = payload.get("finished_at")
    try:
        ended = datetime.strptime(str(raw_end), "%Y-%m-%d %H:%M:%S") if raw_end else datetime.now()
    except Exception:
        ended = datetime.now()
    return max(0.0, (ended - started).total_seconds())

# —————— 导出：规范 A4 纵向的真 Word 文档（.docx，python-docx 服务端生成） ——————
# 版式按财务报表惯例：黑体标题/宋体正文、标题块（公司+报告期+单位与口径）、
# 三线表（上下粗线+表头细线、无竖线、跨页重复表头）、数字列右对齐并加千分位。
DOCX_INK = (0x1A, 0x1A, 0x1A)  # 正文墨色（近黑）
DOCX_MUTED = (0x59, 0x59, 0x59)  # 次要信息灰
DOCX_LINE = "2F5496"  # 三线表横线颜色（深蓝，与深蓝表头底色呼应）
DOCX_CELL_RE = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\s*(亿|万)?\s*(元|%)?$")
DOCX_PURE_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def docx_run_style(run, *, east="宋体", latin="Times New Roman", size=10.5, bold=False, color=DOCX_INK):
    """统一设置一个 run 的中西文字体（eastAsia 不设中文会掉回首退字体）。"""
    run.font.name = latin
    run.font.size = docx_pt(size)
    run.font.bold = bold
    run.font.color.rgb = docx_rgb(*color)
    run._element.get_or_add_rPr().get_or_add_rFonts().set(docx_qn("w:eastAsia"), east)


def docx_cell_text(cell, text, *, east="宋体", size=10.5, bold=False, align="left", color=DOCX_INK):
    """写单元格：清默认空段、行距固定、垂直居中、指定对齐。"""
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_before = docx_pt(1)
    paragraph.paragraph_format.space_after = docx_pt(1)
    paragraph.paragraph_format.line_spacing = 1.15
    paragraph.alignment = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }[align]
    docx_run_style(paragraph.add_run(str(text)), east=east, size=size, bold=bold, color=color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def docx_table_plain(table):
    """三线表：顶/底 1.5pt 粗线，表头下 0.75pt 细线，无竖线无内横线（直写 OOXML）。"""
    borders = OxmlElement("w:tblBorders")
    for tag, edge in (
        ("top", ("single", "12", DOCX_LINE)),
        ("bottom", ("single", "12", DOCX_LINE)),
        ("left", ("none", "0", "auto")),
        ("right", ("none", "0", "auto")),
        ("insideH", ("none", "0", "auto")),
        ("insideV", ("none", "0", "auto")),
    ):
        element = OxmlElement(f"w:{tag}")
        element.set(docx_qn("w:val"), edge[0])
        element.set(docx_qn("w:sz"), edge[1])
        element.set(docx_qn("w:space"), "0")
        element.set(docx_qn("w:color"), edge[2])
        borders.append(element)
    # OOXML 对 tblPr 子元素顺序有硬性 schema 规定：tblBorders 必须在 shd/tblLayout/tblLook 之前，
    # 直接 append 会让 Word 报「内容有错」，所以按后继标签表插位。
    table._tbl.tblPr.insert_element_before(
        borders, "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
        "w:tblCaption", "w:tblDescription", "w:tblPrChange",
    )
    table.autofit = True


def docx_cell_bottom_border(cell, sz="6"):
    """表头行的下边线（三线表的中线）——只有这一行有横线。"""
    borders = OxmlElement("w:tcBorders")
    element = OxmlElement("w:bottom")
    element.set(docx_qn("w:val"), "single")
    element.set(docx_qn("w:sz"), sz)
    element.set(docx_qn("w:space"), "0")
    element.set(docx_qn("w:color"), DOCX_LINE)
    borders.append(element)
    cell._tc.get_or_add_tcPr().insert_element_before(
        borders, "w:shd", "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText",
        "w:vAlign", "w:hideMark", "w:headers", "w:cellIns", "w:cellDel", "w:cellMerge", "w:tcPrChange",
    )


def docx_row_repeat_header(row):
    """跨页时重复这一行（财务报表表格惯例，长表翻页不丢列名）。"""
    element = OxmlElement("w:tblHeader")
    element.set(docx_qn("w:val"), "true")
    row._tr.get_or_add_trPr().append(element)


def docx_add_page_field(paragraph, instruction):
    """页脚里的「第 X 页 共 Y 页」——PAGE/NUMPAGES 是域，Word 打开时才现算。"""
    begin = OxmlElement("w:fldChar")
    begin.set(docx_qn("w:fldCharType"), "begin")
    body = OxmlElement("w:instrText")
    body.set(docx_qn("xml:space"), "preserve")
    body.text = f" {instruction} "
    end = OxmlElement("w:fldChar")
    end.set(docx_qn("w:fldCharType"), "end")
    run = paragraph.add_run()
    run._r.append(begin)
    run._r.append(body)
    run._r.append(end)
    docx_run_style(run, size=9, color=DOCX_MUTED)


def docx_is_numeric_cell(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    matched = DOCX_CELL_RE.fullmatch(text)
    return bool(matched)


def docx_thousands(value: str) -> str:
    """纯数字（整数部分 ≥5 位）加千分位；带单位后缀的原样——那是界面口径。"""
    text = str(value).strip()
    if not DOCX_PURE_NUM_RE.match(text):
        return text
    sign = text[0] if text[0] in "+-" else ""
    head, dot, tail = text.lstrip("+-").partition(".")
    if len(head) < 5:
        return text
    reversed_head = head[::-1]
    grouped = ",".join(reversed_head[i:i + 3] for i in range(0, len(reversed_head), 3))[::-1]
    return sign + grouped + (dot + tail if dot else "")


def docx_answer_sentence(result: dict) -> str:
    text = str(result.get("summary") or result.get("answer") or "").strip()
    if text and text[-1] not in "。！？!?.":
        text += "。"
    return text


def docx_meta_lines(result: dict) -> tuple[str, str, str]:
    """标题块三要素：公司 / 报告期 / 金额单位口径——优先取回答里已核对过的信息。"""
    company = str(result.get("company_name") or "").strip()
    period = ""
    unit_parts: list[str] = []
    for item in result.get("evidence") or []:
        info = item.get("metric_source") or {} if isinstance(item, dict) else {}
        company = company or str(info.get("company_name") or "").strip()
        period = period or str(info.get("report_period") or "").strip()
        for metric in info.get("metrics") or []:
            value = str((metric or {}).get("value") or "")
            for token in ("亿元", "万元", "元"):
                if value.endswith(token) and token not in unit_parts:
                    unit_parts.append(token)
    rows = result.get("table") or []
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    company = company or str(first.get("公司名称") or "").strip()
    period = period or str(first.get("报告期") or "").strip()
    if not period:
        try:
            period = period_phrase_from_text(str(result.get("question") or ""))
        except Exception:
            period = ""
    if not unit_parts:
        for row in rows:
            for value in (row or {}).values():
                text = str(value)
                for token in ("亿元", "万元"):
                    if text.endswith(token) and token not in unit_parts:
                        unit_parts.append(token)
    unit = "金额单位：" + "、".join(unit_parts) if unit_parts else ""
    return company, period, unit


def build_answer_docx(result: dict) -> tuple[bytes, str]:
    """把一条问答结果排成规范 A4 纵向 .docx，返回 (字节, 建议文件名)。"""
    result = result if isinstance(result, dict) else {}
    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT  # 强制纵向：即使模板是横向也掰回来
    section.page_width, section.page_height = docx_mm(210), docx_mm(297)
    section.left_margin = section.right_margin = docx_cm(2.5)
    section.top_margin = docx_cm(2.5)
    section.bottom_margin = docx_cm(2.2)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = docx_pt(10.5)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(docx_qn("w:eastAsia"), "宋体")

    company, period, unit = docx_meta_lines(result)
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = docx_pt(2)
    docx_run_style(title.add_run(company or "上市公司财务数据报表"), east="黑体", size=16, bold=True)
    if company:
        subtitle = document.add_paragraph()
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        subtitle.paragraph_format.space_after = docx_pt(2)
        docx_run_style(subtitle.add_run("财务数据分析报表"), east="黑体", size=13, bold=True)
    meta_bits = [bit for bit in (period, unit, "数据来源：FinSight 财报智析") if bit]
    meta = document.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.paragraph_format.space_after = docx_pt(6)
    docx_run_style(meta.add_run("　｜　".join(meta_bits)), size=9, color=DOCX_MUTED)
    rule = document.add_paragraph()
    rule.paragraph_format.space_after = docx_pt(8)
    rule_borders = OxmlElement("w:pBdr")
    bottom_line = OxmlElement("w:bottom")
    bottom_line.set(docx_qn("w:val"), "single")
    bottom_line.set(docx_qn("w:sz"), "6")
    bottom_line.set(docx_qn("w:space"), "1")
    bottom_line.set(docx_qn("w:color"), DOCX_LINE)
    rule_borders.append(bottom_line)
    # pPr 同理：pBdr 必须排在 spacing/ind/jc 等后继元素之前。
    rule._p.get_or_add_pPr().insert_element_before(
        rule_borders, "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
        "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi",
        "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
        "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
        "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
    )

    question = str(result.get("question") or "").strip()
    if question:
        topic = document.add_paragraph()
        topic.paragraph_format.space_after = docx_pt(6)
        docx_run_style(topic.add_run("分析主题："), east="黑体", size=11, bold=True)
        docx_run_style(topic.add_run(question), size=11)

    def heading(text):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = docx_pt(8)
        paragraph.paragraph_format.space_after = docx_pt(3)
        docx_run_style(paragraph.add_run(text), east="黑体", size=12, bold=True)

    def body(text, *, indent=True):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = docx_pt(3)
        paragraph.paragraph_format.line_spacing = 1.3
        if indent:
            paragraph.paragraph_format.first_line_indent = docx_pt(21)
        docx_run_style(paragraph.add_run(text), size=11)

    section_num = iter(("一", "二", "三", "四", "五", "六"))
    heading("{}、分析结论".format(next(section_num)))
    body(docx_answer_sentence(result) or "（本次回答没有可导出的结论正文）")
    key_points = [str(item).strip() for item in (result.get("key_points") or []) if str(item).strip()]
    if key_points:
        heading("{}、要点".format(next(section_num)))
        for item in key_points:
            body("· " + item, indent=False)
    rows = [row for row in (result.get("table") or []) if isinstance(row, dict)]
    if rows:
        heading("{}、数据表".format(next(section_num)))
        columns = list(rows[0].keys())
        numeric_column = []
        for column in columns:
            # 代码/编号列长得像数字但是身份列：不右对齐、不加千分位（600422≠600,422）
            if "代码" in column or "编号" in column:
                numeric_column.append(False)
                continue
            values = [str(row.get(column) if row.get(column) is not None else "") for row in rows]
            numeric_column.append(
                bool(values) and sum(docx_is_numeric_cell(v) for v in values) >= max(1, int(len(values) * 0.7))
            )
        table = document.add_table(rows=len(rows) + 1, cols=len(columns))
        docx_table_plain(table)
        for index, column in enumerate(columns):
            cell = table.rows[0].cells[index]
            docx_cell_text(cell, column, east="黑体", size=10, bold=False, align="center")
            docx_cell_bottom_border(cell)
        docx_row_repeat_header(table.rows[0])
        for row_index, row in enumerate(rows, start=1):
            for column_index, column in enumerate(columns):
                raw = str(row.get(column) if row.get(column) is not None else "")
                align = "right" if numeric_column[column_index] and raw.strip() else "left"
                docx_cell_text(
                    table.rows[row_index].cells[column_index],
                    docx_thousands(raw),
                    size=10,
                    align=align,
                )
        for note in (str(result.get("chart_note") or "").strip(),):
            if note:
                spare = document.add_paragraph()
                spare.paragraph_format.space_before = docx_pt(3)
                docx_run_style(spare.add_run(note), size=9, color=DOCX_MUTED)
    evidence_items = []
    for item in (result.get("evidence") or []):
        if isinstance(item, str):
            if item.strip():
                evidence_items.append({"type": "_text", "text": item.strip()})
        elif isinstance(item, dict) and item:
            evidence_items.append(item)
    if evidence_items:
        heading("{}、数据依据".format(next(section_num)))
        for item in evidence_items:
            etype = str(item.get("type") or "")
            if etype == "_text":
                body("· " + item.get("text", ""), indent=False)
                continue
            if etype == "report_text" or (etype == "metric_source" and str(item.get("file_name") or "").strip()):
                page = "第 {} 页".format(item.get("page_no")) if item.get("page_no") else ""
                excerpt = str(item.get("excerpt") or "")[:180]
                line = "· 财报原文｜{company}《{file}》{page}".format(
                    company=item.get("company_name") or "—",
                    file=item.get("file_name") or "—",
                    page=page,
                )
                if excerpt:
                    line += "：{}".format(excerpt)
                body(line, indent=False)
            if etype != "metric_source":
                if etype == "data_limit":
                    for text in item.get("items") or []:
                        if str(text or "").strip():
                            body("· 数据说明｜" + str(text).strip(), indent=False)
                continue
            # 服务器实测形状：metric_source 一条里同时带 file_name/page_no（原文出处）与 metrics/missing/calculation
            # （数据库口径）——两行都要出，之前 elif 把出处行整个吃掉，这就是「数据依据」一直空的真因。
            detail = "；".join(
                "{label}={value}".format(label=m.get("label"), value=m.get("value"))
                for m in (item.get("metrics") or [])
                if isinstance(m, dict)
            )
            missing = [str(name) for name in (item.get("missing") or []) if str(name).strip()]
            if missing:
                detail = (detail + "；" if detail else "") + "缺数：" + "、".join(missing)
            if str(item.get("calculation") or "").strip():
                detail = (detail + "（" if detail else "（") + str(item["calculation"]).strip() + "）"
            body(
                "· 数据库｜{company} {period}：{detail}".format(
                    company=item.get("company_name") or "—",
                    period=item.get("report_period") or "",
                    detail=detail or "—",
                ),
                indent=False,
            )
    warnings = [str(item).strip() for item in (result.get("warnings") or []) if str(item).strip()]
    if warnings:
        heading("{}、注意事项".format(next(section_num)))
        for item in warnings:
            body("· " + item, indent=False)

    footer = section.footer
    footer.is_linked_to_previous = False
    foot = footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    docx_run_style(foot.add_run("FinSight 财报智析 · 生成于 {time} · 第 ".format(time=datetime.now().strftime("%Y-%m-%d %H:%M"))), size=9, color=DOCX_MUTED)
    docx_add_page_field(foot, "PAGE")
    docx_run_style(foot.add_run(" 页，共 "), size=9, color=DOCX_MUTED)
    docx_add_page_field(foot, "NUMPAGES")
    docx_run_style(foot.add_run(" 页"), size=9, color=DOCX_MUTED)

    buffer = io.BytesIO()
    document.save(buffer)
    stem = "_".join(bit for bit in (company or "上市公司", period or "财务数据") if bit)
    stem = re.sub(r'[\\/:*?"<>|\s]+', "", stem)[:60]
    name = "{stem}_财务数据报表_{stamp}.docx".format(stem=stem, stamp=datetime.now().strftime("%Y%m%d"))
    return buffer.getvalue(), name


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · FinSight 财报智析</title>
<style>
  :root {
    --pure: #eaf7ff; --text: #d6e5f2; --muted: #7e93a8; --accent: #35d6f5;
    --ease: cubic-bezier(.32,.72,0,1); --soft: cubic-bezier(.22,1,.36,1);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    background: #03070f; color: var(--text); overflow: hidden;
  }
  /* 背景与主站完全一致：同一张科技底图 + 平移网格 + 漂移极光 + 暗角（纯 CSS，图走 /assets/） */
  .fx-bg { position: fixed; inset: -8%; z-index: 0; pointer-events: none;
    background:
      linear-gradient(100deg, rgba(3,7,15,.90) 0%, rgba(3,8,17,.80) 38%, rgba(4,10,20,.66) 68%, rgba(3,7,15,.84) 100%),
      radial-gradient(1200px 620px at 82% 18%, rgba(42,124,240,.30), transparent 62%),
      radial-gradient(900px 520px at 12% 88%, rgba(53,214,245,.22), transparent 64%),
      url("/assets/bg-tech.jpg") center / cover no-repeat;
    will-change: transform;
    animation: ambient-kenburns 17s cubic-bezier(.4,0,.2,1) infinite alternate;
  }
  @keyframes ambient-kenburns {
    0% { transform: scale(1.04) rotate(-1.1deg); }
    50% { transform: scale(1.12) rotate(0.6deg); }
    100% { transform: scale(1.06) rotate(1.5deg); }
  }
  .fx-bg::before { content: ""; position: absolute; inset: -70px;
    background-image:
      linear-gradient(rgba(120,200,255,.055) 1px, transparent 1px),
      linear-gradient(90deg, rgba(120,200,255,.055) 1px, transparent 1px);
    background-size: 64px 64px, 64px 64px;
    mask-image: radial-gradient(115% 90% at 50% 34%, rgba(0,0,0,.85) 18%, transparent 88%);
    -webkit-mask-image: radial-gradient(115% 90% at 50% 34%, rgba(0,0,0,.85) 18%, transparent 88%);
    will-change: transform;
    animation: ambient-grid 12s linear infinite;
  }
  @keyframes ambient-grid {
    from { transform: translate3d(0, 0, 0); }
    to { transform: translate3d(64px, 64px, 0); }
  }
  .fx-bg::after { content: ""; position: absolute; inset: -60px;
    background:
      radial-gradient(720px 540px at 8% 6%, rgba(42,124,240,.34), transparent 66%),
      radial-gradient(620px 500px at 92% 14%, rgba(53,214,245,.26), transparent 68%),
      radial-gradient(560px 420px at 44% 96%, rgba(79,240,208,.16), transparent 70%);
    filter: blur(18px);
    will-change: transform;
    animation: ambient-drift 16s cubic-bezier(.4,0,.2,1) infinite alternate;
  }
  @keyframes ambient-drift {
    from { transform: translate3d(0,0,0); }
    to { transform: translate3d(46px,34px,0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .fx-bg, .fx-bg::before, .fx-bg::after { animation: none; }
  }
  body::after { content: ""; position: fixed; inset: 0; z-index: 5; pointer-events: none;
    background: radial-gradient(120% 95% at 50% 46%, transparent 58%, rgba(2,5,10,.55) 100%); }
  .wrap { position: relative; z-index: 10; height: 100%;
    display: flex; align-items: center; justify-content: center; }
  .login-card { width: 372px; padding: 38px 36px 30px; border-radius: 20px; text-align: center;
    background: linear-gradient(180deg, rgba(12,22,36,0.62), rgba(6,13,23,0.55));
    border: 1px solid rgba(160,205,240,.13);
    box-shadow: 0 40px 90px -40px rgba(0,0,0,.9), inset 0 1px 0 rgba(255,255,255,.05);
    backdrop-filter: blur(6px); }
  .login-card h3 { color: var(--pure); font-size: 21px; font-weight: 300; letter-spacing: .34em; text-indent: .34em; }
  .login-card .sub { color: #64788d; font-size: 11px; letter-spacing: .18em; margin: 6px 0 26px; }
  .field { margin-bottom: 14px; text-align: left; }
  .field label { display: block; color: #5f7590; font-size: 10.5px; letter-spacing: .14em; margin-bottom: 5px; text-transform: uppercase; }
  .field input { width: 100%; padding: 10px 2px; border-radius: 0; border: none; border-bottom: 1px solid rgba(160,205,240,.20);
    background: transparent; color: var(--pure); font: inherit; font-size: 14px; outline: none;
    transition: border-color .3s var(--ease); }
  .field input:focus { border-bottom-color: rgba(53,214,245,.65); }
  /* 浏览器自动填充会强涂一层白底盖掉 transparent——用整格内阴影压回卡片深色，字色/光标一并锁死 */
  .field input:-webkit-autofill,
  .field input:-webkit-autofill:hover,
  .field input:-webkit-autofill:focus {
    -webkit-text-fill-color: var(--pure);
    caret-color: var(--pure);
    -webkit-box-shadow: 0 0 0 1000px #101c2c inset;
    transition: background-color 99999s ease-out;
  }
  .login-btn { width: 100%; margin-top: 18px; padding: 12px; border: 1px solid rgba(53,214,245,.40); border-radius: 12px;
    font-size: 13px; letter-spacing: .3em; text-indent: .3em; font-weight: 500;
    background: rgba(53,214,245,.07); color: #c9f2fc; cursor: pointer; transition: all .4s var(--ease); }
  .login-btn:hover { background: rgba(53,214,245,.14); border-color: rgba(53,214,245,.7); box-shadow: 0 0 26px -6px rgba(53,214,245,.35); }
  .login-btn[disabled] { opacity: .6; cursor: default; }
  .login-hint { min-height: 16px; color: #e0836f; font-size: 11.5px; letter-spacing: .06em; margin-top: 14px; }
  .shake { animation: card-shake .5s var(--soft); }
  @keyframes card-shake { 0%,100%{transform:translateX(0)} 22%{transform:translateX(-7px)} 46%{transform:translateX(6px)} 72%{transform:translateX(-3px)} }
  .bloom { position: fixed; left: 50%; top: 50%; width: 420px; height: 420px; margin: -210px 0 0 -210px; border-radius: 50%;
    background: radial-gradient(circle, rgba(90,200,240,.30) 0%, rgba(60,160,220,.10) 45%, transparent 70%);
    opacity: 0; pointer-events: none; z-index: 30; }
  .playing .bloom { animation: bloomout .9s var(--soft) forwards; }
  @keyframes bloomout { 0%{opacity:.85; transform:scale(.25)} 100%{opacity:0; transform:scale(1.6)} }
  .playing .login-card { animation: cardout .55s var(--soft) forwards; }
  @keyframes cardout { 0%{opacity:1; filter:blur(0)} 100%{opacity:0; transform:scale(1.04); filter: blur(10px);} }
</style>
</head>
<body>
  <div class="fx-bg"></div>
  <div class="bloom"></div>
  <div class="wrap">
    <form class="login-card" id="card" autocomplete="off">
      <h3>FIN SIGHT</h3>
      <div class="sub">上市公司财报分析平台</div>
      <div class="field"><label for="u">账号</label><input id="u" name="username" autocomplete="username"></div>
      <div class="field"><label for="p">密码</label><input id="p" name="password" type="password" autocomplete="current-password"></div>
      <button class="login-btn" id="go" type="submit">进入</button>
      <div class="login-hint" id="hint"></div>
    </form>
  </div>
<script>
const card = document.getElementById("card");
const hint = document.getElementById("hint");
const go = document.getElementById("go");
card.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (go.disabled) return;
  go.disabled = true;
  go.textContent = "验证中";
  hint.textContent = "";
  let payload = null;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("u").value.trim(),
        password: document.getElementById("p").value,
      }),
    });
    payload = await response.json();
  } catch (error) {
    payload = { detail: "服务连不上，稍后再试" };
  }
  if (!payload || payload.ok !== true) {
    go.disabled = false;
    go.textContent = "进入";
    hint.textContent = (payload && payload.detail) || "登录失败";
    card.classList.remove("shake");
    void card.offsetWidth;
    card.classList.add("shake");
    return;
  }
  // 成功：整个登录界面消散——卡片轻微放大+模糊淡出，底下一团光晕散开 → 跳转主界面。整段约 1.4s。
  document.body.classList.add("playing");
  setTimeout(() => { window.location.href = "/"; }, 950);
});
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    """处理 FinSight 的 HTTP、API、文件上传和静态资源请求。"""
    server_version = "FinSight/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        """关闭默认 HTTP 访问日志。"""
        return

    def send_json(self, payload: Any, status: int = 200) -> None:
        """发送 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    ALLOWED_ORIGINS = {
        f"http://127.0.0.1:{APP_PORT}",
        f"http://localhost:{APP_PORT}",
        f"http://[::1]:{APP_PORT}",
    }

    def _same_origin(self, origin: str) -> bool:
        """Origin 是不是「就是本服务这个地址」。

        坑（对外访问必须改这里）：原来只认 127.0.0.1 / localhost / [::1] 三个写死的地址
        ⇒ 一旦 `APP_HOST=0.0.0.0`、用户从 `http://<服务器IP>:8501` 打开页面，浏览器带的
        Origin 就是那个 IP ⇒ 不在白名单 ⇒ POST /api/chat 直接 403「跨站请求已被拒绝」，
        页面能打开却一句话都问不了。
        改成按请求头比对：Origin 的 host:port 等于本次请求的 Host 即同源（第三方网页发
        过来的 Origin 是它自己的域名、Host 是我们 ⇒ 不相等，照旧挡掉）。
        """
        if origin in self.ALLOWED_ORIGINS:
            return True
        host = str(self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        try:
            netloc = (urlparse(origin).netloc or "").lower()
        except Exception:
            return False
        if not netloc:
            return False
        if netloc == host:
            return True
        # localhost 与 127.0.0.1 在浏览器眼里是两个 origin，实际都是本机，等价放行
        return netloc.replace("localhost", "127.0.0.1") == host.replace("localhost", "127.0.0.1")

    def reject_cross_site_write(self) -> bool:
        """挡掉第三方网页对本机的跨站写请求。

        服务只听 127.0.0.1，但浏览器里任意网页都能向本机发 POST：
        /api/import/start 能用任意本地目录把用户磁盘文档拉进文件库并经
        /api/documents/open 读出，/api/system/shutdown 能停服，
        /api/chat/start 能耗用户的模型配额。命令行/本机脚本不带 Origin，照常放行。
        """
        site = str(self.headers.get("Sec-Fetch-Site") or "").lower()
        origin = str(self.headers.get("Origin") or "").rstrip("/")
        if site == "cross-site" or (origin and not self._same_origin(origin)):
            self.send_json({"detail": "跨站请求已被拒绝"}, 403)
            return True
        return False

    def key_detail(self, exc: Exception) -> str:
        """把 `KeyError` 里那句中文说明取出来，不带 `str(KeyError)` 附赠的引号。

        坑：`str(KeyError("聊天任务不存在"))` 得到的是 `'聊天任务不存在'`（带一对单引号），
        以前 `do_GET`/`do_DELETE` 直接把 `str(exc)` 回给前端 ⇒ 界面上出现过
        `"'导入任务不存在'"` 这种东西。`args[0]` 才是原文。
        """
        first = exc.args[0] if exc.args else ""
        return first if isinstance(first, str) and first.strip() else "资源不存在"

    def log_server_error(self, where: str, exc: Exception) -> None:
        """把异常细节留在服务端日志，只回客户端一句通用错误。"""
        print(f"[ERROR] {where} -> {type(exc).__name__}: {exc}")

    def _cookie_value(self, name: str) -> str:
        for part in str(self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def auth_ok(self) -> bool:
        if not auth_enabled():
            return True
        if urlparse(self.path).path in LOGIN_PUBLIC_PATHS:
            return True
        peer = self.client_address[0] if self.client_address else ""
        # 回环 + 显式测试标记：常驻回归用裸 urllib 打本机 HTTP，不带登录 cookie。
        # 只对 127.0.0.1/::1 且带 X-FinSight-Test: 1 的请求放行——本机进程本来就能
        # 直接 import data/web 调同一套函数（登录未启用时全站也是开放的），没引入新面。
        if peer in ("127.0.0.1", "::1") and self.headers.get("X-FinSight-Test") == "1":
            return True
        return verify_session_token(self._cookie_value(SESSION_COOKIE))

    def require_login(self) -> bool:
        """未登录时把响应发掉并返回 True（调用方直接 return）。"""
        if self.auth_ok():
            return False
        if urlparse(self.path).path.startswith("/api/"):
            self.send_json({"detail": "请先登录"}, HTTPStatus.UNAUTHORIZED)
        else:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/login")
            self.end_headers()
        return True

    def handle_login(self) -> None:
        """POST /api/login：校验账号口令，成功发 30 天 HttpOnly cookie。"""
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            request = {}
        user = str(request.get("username") or "")
        password = str(request.get("password") or "")
        ok_user = hmac.compare_digest(user, os.getenv("LOGIN_USER", ""))
        ok_pass = hmac.compare_digest(password, os.getenv("LOGIN_PASSWORD", ""))
        if not (auth_enabled() and ok_user and ok_pass):
            time.sleep(0.5)  # 拖慢口令爆破，不透露是账号还是口令错了
            self.send_json({"detail": "账号或密码不正确"}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={make_session_token()}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}",
        )
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """处理 GET 请求和静态资源。"""
        path = urlparse(self.path).path
        if self.require_login():
            return
        try:
            if path == "/":
                body = INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/login":
                if not auth_enabled():
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                body = LOGIN_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/assets/"):
                asset_root = (BASE_DIR / "storage/assets").resolve()
                asset_path = (asset_root / path.removeprefix("/assets/")).resolve()
                try:
                    asset_path.relative_to(asset_root)
                except ValueError:
                    raise KeyError("资源不存在")
                if not asset_path.is_file():
                    raise KeyError("资源不存在")
                body = asset_path.read_bytes()
                content_type = {
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".json": "application/json; charset=utf-8",
                }.get(
                    asset_path.suffix.lower(),
                    mimetypes.guess_type(asset_path.name)[0]
                    or "application/octet-stream",
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/system/version":
                self.send_json({"app": "FinSight", "version": APP_VERSION})
            elif path == "/api/summary":
                self.send_json(get_database_summary())
            elif path == "/api/companies":
                self.send_json(get_company_list())
            elif path == "/api/recommendations":
                self.send_json(get_recommended_questions())
            elif path == "/api/validation":
                self.send_json(get_validation_report())
            elif path == "/api/data-quality":
                self.send_json(get_data_quality_report())
            elif path == "/api/coverage":
                self.send_json(company_coverage_report())
            elif path == "/api/vector-index":
                self.send_json(get_vector_index_status_api())
            elif path == "/api/documents":
                parsed = parse_qs(urlparse(self.path).query)
                self.send_json(search_documents(parsed.get("query", [""])[0]))
            elif path.startswith("/api/documents/open/"):
                document_id = path.rsplit("/", 1)[1]
                target = next(
                    (
                        item
                        for item in load_document_index()
                        if item.get("id") == document_id
                    ),
                    None,
                )
                if target is None:
                    raise KeyError("文档不存在")
                file_path = Path(target.get("file_path", ""))
                if not file_path.exists():
                    raise KeyError("文件不存在")
                body = file_path.read_bytes()
                content_type = (
                    mimetypes.guess_type(file_path.name)[0]
                    or "application/octet-stream"
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Content-Disposition",
                    "inline; filename*=UTF-8''"
                    f"{quote(str(target.get('file_name') or file_path.name))}",
                )
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/history":
                self.send_json(get_chat_history())
            elif path.startswith("/api/chat/status/"):
                self.send_json(load_chat_task_status(path.rsplit("/", 1)[1]))
            elif path.startswith("/api/history/"):
                detail = get_chat_session_detail(path.rsplit("/", 1)[1])
                last_question = ""
                for message in detail.get("messages") or []:
                    if not isinstance(message, dict):
                        continue
                    if message.get("role") == "user":
                        last_question = str(message.get("content") or "")
                        continue
                    result = message.get("result")
                    if not isinstance(result, dict):
                        continue
                    question = str(result.get("question") or last_question)
                    evidence = result.get("evidence") or []
                    summary = result.get("evidence_summary") or {}
                    if not evidence or not any(summary.values()):
                        evidence = build_evidence_chain(question, result, {})
                        result["evidence"] = evidence
                        result["evidence_summary"] = build_evidence_summary(
                            evidence
                        )
                self.send_json(detail)
            elif path.startswith("/api/import/status/"):
                job_id = path.rsplit("/", 1)[1]
                with IMPORT_JOBS_LOCK:
                    if job_id not in IMPORT_JOBS:
                        raise KeyError("导入任务不存在")
                    payload = dict(IMPORT_JOBS[job_id])
                    payload["errors"] = list(payload.get("errors") or [])
                    payload["logs"] = list(payload.get("logs") or [])
                    payload["active"] = list(payload.get("active") or [])
                elapsed = import_job_elapsed(payload)
                if elapsed is not None:
                    payload["elapsed_seconds"] = elapsed
                # 锁内只取快照，向 socket 写数据放到锁外：慢客户端不能冻住导入进度更新。
                self.send_json(payload)
            else:
                self.send_json({"detail": "接口不存在"}, 404)
        except KeyError as exc:
            self.send_json({"detail": self.key_detail(exc)}, 404)
        except Exception as exc:
            self.log_server_error(f"{self.command} {path}", exc)
            self.send_json({"detail": "服务内部错误，详情见服务端日志"}, 500)

    def do_DELETE(self) -> None:
        """处理 DELETE 请求。"""
        if self.reject_cross_site_write():
            return
        path = urlparse(self.path).path
        if self.require_login():
            return
        if path == "/api/history":
            self.send_json(delete_all_chat_sessions())
        elif path.startswith("/api/history/"):
            self.send_json(delete_chat_session(path.rsplit("/", 1)[1]))
        elif path == "/api/documents":
            # 文件库「全部删除」：只清库与索引，磁盘上的原文不动。
            self.send_json(delete_all_documents())
        elif path.startswith("/api/documents/"):
            try:
                self.send_json(delete_document(path.rsplit("/", 1)[1]))
            except KeyError as exc:
                self.send_json({"detail": str(exc)}, 404)
        else:
            self.send_json({"detail": "接口不存在"}, 404)

    def do_POST(self) -> None:
        """处理 POST 请求、文件上传和聊天。"""
        if self.reject_cross_site_write():
            return
        path = urlparse(self.path).path
        if self.require_login():
            return
        try:
            if path == "/api/login":
                self.handle_login()
            elif path == "/api/logout":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
                )
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/chat/start":
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(start_chat_task(request))
            elif path.startswith("/api/chat/pause/"):
                self.send_json(pause_chat_task(path.rsplit("/", 1)[1]))
            elif path.startswith("/api/chat/resume/"):
                self.send_json(resume_chat_task(path.rsplit("/", 1)[1]))
            elif path == "/api/chat":
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(handle_chat_request(request))
            elif path == "/api/export/docx":
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                data, filename = build_answer_docx(
                    payload.get("result") if isinstance(payload.get("result"), dict) else payload
                )
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                # 中文文件名走 RFC 5987；另留一份 ASCII 兜底给不认 filename* 的老客户端
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename=FinSight_report.docx; filename*=UTF-8''{quoted}".format(
                        quoted=quote(filename)
                    ),
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/system/shutdown":
                self.send_json({"status": "shutting_down"})
                if SERVER_INSTANCE is not None:
                    threading.Thread(
                        target=SERVER_INSTANCE.shutdown,
                        daemon=True,
                    ).start()
            elif path == "/api/vector-index/rebuild":
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(
                    start_vector_index_build(
                        bool(request.get("force", False))
                    )
                )
            elif path == "/api/import/start":
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(
                    start_bulk_import_task(
                        str(request.get("local_dir", "")),
                        str(request.get("session_id", "")),
                    )
                )
            elif path.startswith("/api/import/pause/"):
                self.send_json(set_import_job_control(path.rsplit("/", 1)[1], "pause"))
            elif path.startswith("/api/import/resume/"):
                self.send_json(set_import_job_control(path.rsplit("/", 1)[1], "resume"))
            elif path.startswith("/api/import/stop/"):
                self.send_json(set_import_job_control(path.rsplit("/", 1)[1], "stop"))
            elif path == "/api/upload":
                content_type = self.headers.get("Content-Type", "")
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                message = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                    + body
                )
                files: list[tuple[str, bytes]] = []
                local_dir = ""
                session_id = ""
                for part in message.iter_parts():
                    if part.get_filename():
                        files.append(
                            (
                                part.get_filename() or "",
                                part.get_payload(decode=True) or b"",
                            )
                        )
                    elif part.get_param("name", header="content-disposition") == "local_dir":
                        local_dir = (part.get_payload(decode=True) or b"").decode("utf-8")
                    elif part.get_param("name", header="content-disposition") == "session_id":
                        session_id = (
                            part.get_payload(decode=True) or b""
                        ).decode("utf-8")
                self.send_json(handle_file_upload(files, local_dir, session_id))
            else:
                self.send_json({"detail": "接口不存在"}, 404)
        except ValueError as exc:
            self.send_json({"detail": str(exc)}, 400)
        except KeyError as exc:
            # 与 do_GET 对齐：「资源不存在」这个语义统一回 404。以前 do_POST 没有
            # 这一支，`pause/resume_chat_task` 里那句 `raise KeyError("聊天任务不存在")`
            # 就掉进下面的 `except Exception` 变成 500（实测 `POST /api/chat/pause/
            # does-not-exist` ⇒ 500），客户端拿到 500 会以为是服务端坏了、去重试。
            self.send_json({"detail": self.key_detail(exc)}, 404)
        except Exception as exc:
            self.log_server_error(f"{self.command} {path}", exc)
            self.send_json({"detail": "服务内部错误，详情见服务端日志"}, 500)

INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FinSight 财报智析</title>
  <style>
    :root {
      /* 主题色从科技底图取：钴蓝 / 青 / 冰蓝 / 薄荷 */
      --bg: #03070f;
      --panel: rgba(10, 22, 38, .72);
      --panel-2: rgba(14, 28, 48, .78);
      --line: rgba(120,200,255,.13);
      --text: #c3d4e3;
      --pure: #eaf3fb;
      --muted: #8ea6bd;
      --accent: #35d6f5;
      --accent-2: #ffd479;
      --cobalt: #2a7cf0;
      --ice: #9fe8ff;
      --mint: #4ff0d0;
      --danger: #ff7189;
      --ease: cubic-bezier(.32,.72,0,1);
      --composer-space: 190px;
    }
    * { box-sizing: border-box; }
    html { background: #03070f; }
    body {
      margin: 0;
      min-height: 100vh;
      background: #03070f;
      overflow-x: hidden;
      color: var(--text);
      font: 14px/1.6 "Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
      letter-spacing: -.011em;
      -webkit-font-smoothing: antialiased;
    }
    /* 动态背景：底图整块缓慢推拉+旋转，叠一层平移网格和一层漂移极光。**页面上已经不再挂毛玻璃**
       ——以前有二十处 backdrop-filter 压在这块会动的底图上面，底图每动一帧它们就要把身后的整块画面
       重新模糊一遍，太吃性能；现在全部摘掉，改成给每层自身补一点不透明度来保住原来的分层观感。 */
    .fx-bg {
      position: fixed; inset: -8%; z-index: 0; pointer-events: none;
      background:
        linear-gradient(100deg, rgba(3,7,15,.90) 0%, rgba(3,8,17,.80) 38%, rgba(4,10,20,.66) 68%, rgba(3,7,15,.84) 100%),
        radial-gradient(1200px 620px at 82% 18%, rgba(42,124,240,.30), transparent 62%),
        radial-gradient(900px 520px at 12% 88%, rgba(53,214,245,.22), transparent 64%),
        url("/assets/bg-tech.jpg") center / cover no-repeat;
      will-change: transform;
      animation: ambient-kenburns 17s cubic-bezier(.4,0,.2,1) infinite alternate;
    }
    /* 底图推拉 + 轻微旋转（只动 transform）*/
    @keyframes ambient-kenburns {
      0% { transform: scale(1.04) rotate(-1.1deg); }
      50% { transform: scale(1.12) rotate(0.6deg); }
      100% { transform: scale(1.06) rotate(1.5deg); }
    }
    /* 网格：以前 animate 的是 background-position（等于每帧重绘整屏网格线），
       改成整块平移一个格子宽 64px ⇒ 位移量刚好是一个周期，无缝，而且能单独提合成层。 */
    .fx-bg::before {
      content: ""; position: absolute; inset: -70px;
      background-image:
        linear-gradient(rgba(120,200,255,.055) 1px, transparent 1px),
        linear-gradient(90deg, rgba(120,200,255,.055) 1px, transparent 1px);
      background-size: 64px 64px, 64px 64px;
      mask-image: radial-gradient(115% 90% at 50% 34%, rgba(0,0,0,.85) 18%, transparent 88%);
      -webkit-mask-image: radial-gradient(115% 90% at 50% 34%, rgba(0,0,0,.85) 18%, transparent 88%);
      will-change: transform;
      animation: ambient-grid 12s linear infinite;
    }
    /* 极光漂移：模糊从 30px 降到 18px（本来就是一团团软渐变，肉眼看不出差别），
       加了 will-change 后模糊只在生成纹理时算一次，不再每帧重算。 */
    .fx-bg::after {
      content: ""; position: absolute; inset: -60px;
      background:
        radial-gradient(720px 540px at 8% 6%, rgba(42,124,240,.34), transparent 66%),
        radial-gradient(620px 500px at 92% 14%, rgba(53,214,245,.26), transparent 68%),
        radial-gradient(560px 420px at 44% 96%, rgba(79,240,208,.16), transparent 70%);
      filter: blur(18px);
      will-change: transform;
      animation: ambient-drift 16s cubic-bezier(.4,0,.2,1) infinite alternate;
    }
    @keyframes ambient-drift {
      from { transform: translate3d(0,0,0); }
      to { transform: translate3d(46px,34px,0); }
    }
    @media (prefers-reduced-motion: reduce) {
      .fx-bg, .fx-bg::before, .fx-bg::after { animation: none; }
    }
    @keyframes ambient-grid {
      from { transform: translate3d(0, 0, 0); }
      to { transform: translate3d(64px, 64px, 0); }
    }
    .app { position: relative; z-index: 1; }
    button, input, select, textarea { font: inherit; }
    button { cursor: pointer; }
    .app { display: flex; min-height: 100vh; }
    .sidebar {
      width: 230px;
      background: rgba(9,20,34,0.32);
      color: #e8edf2;
      border-right: 1px solid rgba(255,255,255,.09);
      padding: 18px 14px;
      position: fixed;
      inset: 0 auto 0 0;
      overflow-y: auto;
    }
    .brand { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
    .brand span { color: var(--accent); }
    /* 第 2 款：反色贴深色侧栏，高度 44px */
    .brand-logo {
      display: block; height: 44px; width: auto;
      filter: invert(1) hue-rotate(180deg) saturate(1.05) brightness(1.05);
    }
    .caption { color: #9ba8b4; font-size: 12px; margin-bottom: 18px; }
    .nav { display: grid; gap: 6px; margin-bottom: 18px; }
    .nav-btn {
      text-align: left; border: 1px solid transparent; border-radius: 6px;
      background: transparent; color: #aeb9c2; padding: 8px 10px;
    }
    .nav-btn:hover { color: #ffffff; background: rgba(255,255,255,.05); }
    .nav-btn.active {
      color: #ffffff; background: rgba(255,255,255,.08);
      border-color: rgba(255,255,255,.12);
    }
    .page { display: none; }
    .page.active { display: block; }
    .page-panel {
      max-width: 1100px; margin: 24px; padding: 24px;
      background: linear-gradient(180deg, rgba(9,20,34,0.32), rgba(5,12,22,0.32));
      border: 1px solid rgba(120,200,255,.13);
      border-radius: 16px;
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.075),
        0 16px 36px -22px rgba(0,0,0,.85),
        0 40px 80px -46px rgba(8,40,80,.9);
      color: var(--text);
    }
    .page-panel h2 { margin: 0 0 8px; font-size: 20px; letter-spacing: -.024em; color: var(--pure); }
    .side-title { color: var(--muted); font-size: 12px; margin: 18px 0 8px; }
    .history-actions {
      display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px;
    }
    .history-actions .ghost { padding: 6px 8px; font-size: 11px; }
    .history-actions .ghost {
      color: #ffffff; border-color: rgba(255,255,255,.24);
      background: rgba(255,255,255,.035);
    }
    .history-actions .ghost:hover {
      color: #ffffff; border-color: rgba(255,255,255,.44);
      background: rgba(255,255,255,.08);
    }
    .history-item {
      padding: 9px 10px; border-radius: 8px; margin-bottom: 6px;
      background: rgba(255,255,255,.035); cursor: pointer;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .history-item:hover { background: rgba(45,212,191,.1); }
    .history-row {
      display: flex; align-items: center; gap: 6px; margin-bottom: 6px;
      background: rgba(255,255,255,.055); border-radius: 6px;
      padding: 7px 8px;
    }
    .history-open {
      flex: 1; min-width: 0; cursor: pointer; color: #e8edf2;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      border: 0; background: transparent; text-align: left; padding: 0;
      font-size: 12px;
    }
    .history-open:hover { color: var(--accent); }
    .history-delete {
      border: 0; background: transparent; color: #81909d;
      padding: 0 3px; font-size: 15px; line-height: 1;
      opacity: 0; pointer-events: none; transition: opacity .15s ease, color .15s ease;
    }
    .history-row:hover .history-delete,
    .history-delete:focus { opacity: 1; pointer-events: auto; color: var(--danger); }
    .file-group { margin-bottom: 10px; }
    .file-company { color: var(--accent); font-size: 12px; margin: 8px 0 4px; }
    .file-item {
      display: flex; align-items: center; justify-content: space-between;
      gap: 8px; padding: 7px 8px; border-radius: 6px;
      background: rgba(255,255,255,.03); margin-bottom: 4px;
      font-size: 12px;
    }
    .file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .file-actions { display: flex; gap: 5px; flex-shrink: 0; }
    .file-open {
      border: 0; background: transparent; color: var(--accent);
      padding: 0 3px; font-size: 12px;
    }
    .file-delete {
      border: 0; background: transparent; color: var(--danger);
      padding: 0 3px; font-size: 12px;
    }
    .file-search-bar { display: flex; gap: 8px; margin-bottom: 8px; }
    .cov-row {
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px;
      padding: 8px 10px; border-radius: 8px; margin-bottom: 5px;
      background: rgba(255,255,255,.03); font-size: 12.5px;
    }
    .cov-row b { font-size: 13.5px; font-weight: 650; letter-spacing: -.01em; color: var(--pure); }
    .cov-code { color: #7f8f9e; font-variant-numeric: tabular-nums; }
    .cov-ok { color: #3fd2c4; }
    .cov-flag { color: #f0c078; }
    .cov-flag.bad { color: #f28aa6; }
    #fileSearch {
      flex: 1; min-width: 0; padding: 8px 9px;
      background: var(--panel); color: var(--text);
      border: 1px solid var(--line); border-radius: 6px;
    }
    #fileSearchBtn {
      padding: 8px 14px; border: 1px solid var(--accent);
      border-radius: 6px; background: transparent; color: var(--accent);
      cursor: pointer;
    }
    #fileSearchBtn:hover { background: rgba(45,212,191,.08); }
    .main {
      margin-left: 230px; margin-right: var(--rail-w, 44px);
      width: calc(100% - 230px - var(--rail-w, 44px));
      min-height: 100vh; display: flex; flex-direction: column;
      transition: width .5s var(--ease), margin-right .5s var(--ease);
    }
    .topbar {
      height: 56px; border-bottom: 1px solid var(--line);
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 24px;
      background: rgba(6,16,28,0.70);
      border-bottom-color: rgba(255,255,255,.09);
      position: sticky; top: 0; z-index: 2;
    }
    .topbar h1 { font-size: 18px; margin: 0; font-weight: 650; color: #f7fbfd; }
    .mode-control {
      display: inline-flex; align-items: center; gap: 8px;
      margin-left: auto; flex: 0 0 auto;
    }
    .status {
      display: inline-flex; align-items: center; gap: 7px;
      flex: 0 0 auto; padding: 4px 10px;
      color: #b8c7d3; font-size: 11px;
      border: 1px solid rgba(255,255,255,.12); border-radius: 999px;
      background: rgba(255,255,255,.045);
    }
    .status::before {
      content: ""; width: 6px; height: 6px; border-radius: 50%;
      background: #42d392; box-shadow: 0 0 0 3px rgba(66,211,146,.12);
    }
    .mode-select {
      appearance: none; padding: 4px 28px 4px 10px;
      border: 1px solid rgba(57,196,178,.45); border-radius: 999px;
      background:
        linear-gradient(45deg, transparent 50%, #7de3d4 50%) calc(100% - 13px) 50% / 5px 5px no-repeat,
        linear-gradient(135deg, #7de3d4 50%, transparent 50%) calc(100% - 9px) 50% / 5px 5px no-repeat,
        rgba(57,196,178,.1);
      color: #e8fffb; font-size: 11px; outline: none; cursor: pointer;
    }
    .mode-select:disabled { opacity: .62; cursor: wait; }
    .mode-select option { background: #102333; color: #e8eef4; }
    .metrics {
      display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px; padding: 12px 24px 10px;
      width: 100%; max-width: none; margin: 0;
    }
    .metric {
      background: linear-gradient(180deg, rgba(9,20,34,0.32), rgba(5,12,22,0.32));
      border: 1px solid rgba(120,200,255,.13);
      border-radius: 14px; padding: 16px 16px;
      min-height: 130px;
      color: var(--text);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.07),
        0 14px 32px -20px rgba(0,0,0,.85);
      transition: transform .5s var(--ease), box-shadow .5s var(--ease);
    }
    .metric:hover {
      transform: translateY(-3px);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.09),
        0 20px 40px -22px rgba(0,0,0,.9),
        0 0 54px -26px rgba(53,214,245,.42);
    }
    .metric-head {
      display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
    }
    .metric label { color: var(--text); font-size: 12px; font-weight: 600; }
    .metric strong { font-size: 18px; color: var(--accent); white-space: nowrap; }
    .metric-unit { color: var(--accent); font-size: 11px; margin-top: 1px; }
    .metric-description { color: #c5d1db; font-size: 11px; line-height: 1.4; margin-top: 7px; }
    .metric-usage { color: #93a3b2; font-size: 11px; line-height: 1.4; margin-top: 4px; }
    /* 主界面顶部：一行真数字 + 可点示例问题（原来的 5 张表结构卡是讲稿，不是功能） */
    .overview { display: grid; gap: 8px; margin-top: 4px; }
    .ov-strip {
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px 12px;
      padding: 10px 14px; border: 1px solid rgba(120,200,255,.14); border-radius: 12px;
      background: linear-gradient(180deg, rgba(12,26,42,0.46), rgba(6,14,25,0.32));
    }
    .ov-fact { display: inline-flex; align-items: baseline; gap: 4px; }
    .ov-fact b { font-size: 19px; font-weight: 700; letter-spacing: -.02em; color: var(--pure); font-variant-numeric: tabular-nums; }
    .ov-fact em { font-style: normal; font-size: 11px; color: var(--accent); }
    .ov-fact i { font-style: normal; font-size: 11.5px; color: #93a3b2; margin-left: 2px; }
    .ov-sep { width: 1px; height: 14px; background: rgba(255,255,255,.12); }
    .ov-periods { margin-left: auto; font-size: 11.5px; color: #93a3b2; }
    .ov-asks { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .ov-asks-label { font-size: 11.5px; color: var(--muted); letter-spacing: .04em; }
    .ov-ask {
      font: inherit; font-size: 12px; color: #cfe2f0; cursor: pointer;
      padding: 6px 12px; border-radius: 999px;
      border: 1px solid rgba(120,200,255,.20);
      background: rgba(120,200,255,.06);
      transition: transform .45s var(--ease), background .3s var(--ease), border-color .3s var(--ease), box-shadow .45s var(--ease);
    }
    .ov-ask:hover {
      color: var(--pure); background: rgba(120,200,255,.13); border-color: rgba(120,200,255,.42);
      transform: translateY(-2px); box-shadow: 0 10px 26px -16px rgba(63,210,190,.55);
    }
    .sql-console {
      margin: 0 24px 10px;
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 8px;
      background: rgba(10, 22, 34, .42);
      color: var(--text);
    }
    .sql-console-head {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; padding: 9px 11px;
    }
    .sql-console-title { font-size: 12px; font-weight: 650; color: #e8f1f6; }
    .sql-log-status { color: var(--muted); font-size: 11px; margin-left: 8px; }
    .sql-log-toggle {
      border: 1px solid var(--line); border-radius: 6px;
      background: transparent; color: var(--accent);
      padding: 4px 9px; font-size: 11px;
    }
    .sql-log-list {
      display: grid; gap: 6px; max-height: 300px; overflow-y: auto;
      padding: 8px 11px 11px; border-top: 1px solid rgba(255,255,255,.08);
    }
    .sql-log-list[hidden] { display: none; }
    .sql-log-item {
      padding: 7px 8px; border-radius: 6px;
      background: rgba(255,255,255,.035);
      font-size: 11px; line-height: 1.45;
    }
    .sql-log-item.success { border-left: 3px solid #42d392; }
    .sql-log-item.empty { border-left: 3px solid #8a96a3; }
    .sql-log-item.failed { border-left: 3px solid #ef6b73; }
    .sql-log-item.running { border-left: 3px solid #f0b35c; }
    .sql-log-meta { color: var(--muted); margin-top: 3px; }
    .sql-code {
      margin: 5px 0 0; padding: 7px; border-radius: 5px;
      background: rgba(0,0,0,.25); color: #c7d7e2;
      white-space: pre-wrap; overflow-wrap: anywhere;
      font-family: Consolas, "Courier New", monospace; font-size: 10px;
    }
    /* SQL 日志默认展示「查了什么、查到没有」，语句原文收进这里，想看再点开 */
    .sql-fold { margin-top: 5px; }
    .sql-fold summary { cursor: pointer; color: var(--muted); font-size: 10.5px; }
    .sql-fold summary:hover { color: var(--accent); }
    /* 右侧 SQL 日志栏：默认收起成一条边，展开时主区让位 */
    body { --rail-w: 44px; }
    body.sql-rail-open { --rail-w: 250px; }
    /* SQL 执行日志只在「智能问数」主界面出现：别的页签里整条收起、主区自己吃回宽度 */
    body:not(.page-chat) { --rail-w: 0px; }
    body:not(.page-chat) .sql-rail { display: none; }
    body:not(.page-chat) .sql-rail-tab { display: none; }
    .sql-rail {
      position: fixed; top: 0; right: 0; bottom: 0; width: var(--rail-w);
      display: flex; z-index: 5; overflow: hidden;
      border-left: 1px solid rgba(120,200,255,.14);
      background: linear-gradient(180deg, rgba(8,18,30,0.28), rgba(4,11,20,0.18));
      transition: width .5s var(--ease), background .5s var(--ease);
    }
    .sql-rail-tab {
      flex: 0 0 44px; width: 44px; border: 0; border-radius: 0;
      background: transparent; color: var(--muted);
      display: flex; align-items: center; justify-content: center; gap: 8px;
      writing-mode: vertical-rl; letter-spacing: .06em; font-size: 11px;
      transition: color .35s var(--ease), background .35s var(--ease);
    }
    .sql-rail-tab:hover { color: var(--pure); background: rgba(120,200,255,.07); }
    /* 箭头本身指左（←），在竖排里会被转 90°，所以这里转回来 = 永远水平指向 */
    .sql-rail-tab-icon { display: block; line-height: 0; color: var(--accent); transform: rotate(0deg); transition: transform .45s var(--ease); }
    .sql-rail-tab-icon svg { display: block; }
    /* 收起时指向左（← 往外拉），展开时指向右（→ 收回去） */
    body.sql-rail-open .sql-rail-tab-icon { transform: rotate(180deg); }
    .sql-rail-body {
      flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column;
      padding: 14px 12px; gap: 10px;
      opacity: 0; pointer-events: none; transition: opacity .3s var(--ease);
    }
    body.sql-rail-open .sql-rail-body { opacity: 1; pointer-events: auto; }
    body.sql-rail-open .sql-rail {
      background: linear-gradient(180deg, rgba(9,20,34,.55), rgba(5,12,22,.42));
    }
    .sql-rail-head {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }
    .sql-rail-head strong { font-size: 12.5px; color: var(--pure); letter-spacing: -.01em; }
    .sql-rail .sql-console {
      margin: 0; border-radius: 12px; background: rgba(120,200,255,.045);
      border-color: rgba(120,200,255,.13);
    }
    .sql-rail .sql-console-head { padding: 0; }
    .sql-rail .sql-log-status { margin-left: 0; font-size: 10.5px; }
    .sql-rail .sql-log-toggle { padding: 2px 9px; font-size: 10.5px; border-radius: 999px; }
    .sql-rail .sql-log-list {
      flex: 1 1 auto; max-height: none; gap: 6px; padding: 10px 10px 12px;
      border-top-color: rgba(120,200,255,.12); overflow-y: auto;
    }
    .sql-rail .sql-log-item { font-size: 10.5px; padding: 7px 8px; border-radius: 8px;
      background: rgba(120,200,255,.05); }
    .sql-rail .sql-code { font-size: 9.5px; padding: 6px; }
    .chat-wrap {
      flex: 1; padding: 0 0 var(--composer-space, 190px); overflow: visible;
      max-width: none; width: 100%; margin: 0;
    }
    .workspace {
      display: block; width: 100%; max-width: none;
      margin: 0; padding: 0 24px;
    }
    .conversation-column { min-width: 0; max-width: none; width: 100%; margin: 0; }
    .context-panel { display: none !important; }
    .context-card {
      padding: 11px; border: 1px solid var(--line); border-radius: 8px;
      background: rgba(12,25,38,0.94);
      box-shadow: 0 12px 30px rgba(0,0,0,.15);
      color: var(--text);
      animation: panel-in .28s ease both;
    }
    .context-label { color: var(--muted); font-size: 11px; letter-spacing: .02em; }
    .context-title { margin-top: 7px; font-weight: 700; line-height: 1.4; }
    .context-meta { margin-top: 5px; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .context-stage { margin-top: 8px; color: var(--accent); font-weight: 700; }
    .context-progress {
      height: 3px; margin-top: 12px; overflow: hidden;
      background: rgba(255,255,255,.1); border-radius: 999px;
    }
    .context-progress span {
      display: block; width: 0; height: 100%;
      background: var(--accent); transition: width .35s ease;
    }
    .context-lines { display: grid; grid-template-columns: 1fr auto; gap: 7px 10px; margin-top: 9px; font-size: 12px; }
    .context-lines span { color: var(--muted); }
    @keyframes panel-in {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .message { margin: 22px 0; }
    .message.user { text-align: right; }
    .bubble {
      display: inline-block; max-width: 72%; text-align: left;
      padding: 11px 14px; border-radius: 10px;
      background: var(--panel); border: 1px solid var(--line);
    }
    /* 用户气泡：与系统回复同一块底色，只稍微提亮一档，不再做成蓝渐变 */
    .message.user .bubble {
      background: linear-gradient(180deg, rgba(12,26,42,0.54), rgba(6,14,25,0.42));
      border-color: rgba(120,200,255,.20);
      color: var(--text);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.075), 0 8px 22px -18px rgba(0,0,0,.85);
    }
    .answer {
      position: relative;
      background: linear-gradient(180deg, rgba(9,20,34,0.32), rgba(5,12,22,0.32));
      border: 1px solid rgba(120,200,255,.14);
      border-radius: 16px; padding: 24px 24px 24px; margin-top: 8px;
      height: auto;
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.075),
        0 1px 2px rgba(0,0,0,.5),
        0 16px 36px -22px rgba(0,0,0,.85),
        0 40px 80px -46px rgba(8,40,80,.9);
      color: var(--ink, var(--text));
      animation: answer-in .45s var(--ease) both;
      width: 100%;
    }
    .answer::after {
      content: ""; position: absolute; inset: 0; border-radius: 16px; pointer-events: none;
      background: linear-gradient(180deg, rgba(159,232,255,.06), transparent 40%);
    }
    @keyframes answer-in {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .answer-head {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--line);
      color: var(--muted); font-size: 11px; font-weight: 600;
      letter-spacing: .12em; text-transform: uppercase;
    }
    .answer-action {
      border: 1px solid rgba(120,200,255,.30); border-radius: 999px;
      background: rgba(120,200,255,.05); color: var(--muted);
      padding: 3px 12px; font-size: 12px; cursor: pointer;
      transition: color .2s var(--ease), border-color .2s var(--ease);
    }
    .answer-action:hover { color: var(--accent); border-color: rgba(57,196,178,.65); }
    /* 动作按钮整组靠右（回答/证据标题不跟着散开），组内留固定间距 */
    .answer-actions { display: flex; gap: 10px; margin-left: auto; }
    /* 结果底部右对齐的思考时长（后端 thinking_seconds，两位小数） */
    .think-time { margin-top: 12px; text-align: right; color: #64788d; font-size: 11px; letter-spacing: .06em; }
    .thinking-overlay {
      position: fixed; inset: 0; display: flex;
      align-items: center; justify-content: center;
      pointer-events: none; z-index: 20;
    }
    .thinking-card {
      padding: 12px 18px; border-radius: 8px;
      background: rgba(24,35,45,.96); border: 1px solid var(--line);
      box-shadow: 0 12px 30px rgba(0,0,0,.28);
      color: var(--text);
    }
    .answer pre { white-space: pre-wrap; font-family: inherit; margin: 0; }
    /* ── 数据表：单位上表头、数字右对齐等宽、排名徽章、数值条 ── */
    table { width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 16px; font-size: 13.5px; border: 1.5px solid rgba(140,210,255,.62); border-radius: 12px; }
    th, td { padding: 12px 14px; border-bottom: 1.5px solid rgba(140,210,255,.62); border-right: 1px solid rgba(140,210,255,.30); text-align: left; }
    th:last-child, td:last-child { border-right: none; }
    thead th:first-child { border-top-left-radius: 12px; }
    thead th:last-child { border-top-right-radius: 12px; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:last-child td:first-child { border-bottom-left-radius: 12px; }
    tbody tr:last-child td:last-child { border-bottom-right-radius: 12px; }
    tbody tr { transition: background .35s var(--ease); }
    tbody tr:hover { background: rgba(53,214,245,.06); }
    thead th {
      color: var(--muted); font-weight: 500; font-size: 11px; letter-spacing: .05em;
      border-bottom: 2.5px solid rgba(140,210,255,.90); padding-bottom: 11px;
      position: sticky; top: 0; z-index: 3;
      background: linear-gradient(180deg, rgba(14,28,46,0.98), rgba(10,21,36,0.96));
    }
    thead th .unit {
      display: block; font-size: 10px; color: rgba(140,170,200,.75);
      letter-spacing: 0; margin-top: 2px;
    }
    th.num, td.num { text-align: left; }
    td.num { font-variant-numeric: tabular-nums; letter-spacing: -.02em; color: var(--pure); }
    td.num .cellnum { display: block; line-height: 1.5; }
    td.code { color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; letter-spacing: 0; }
    .rk {
      display: inline-flex; align-items: center; justify-content: center;
      width: 22px; height: 22px; border-radius: 7px;
      font-size: 11.5px; font-weight: 700; color: #08131f; letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }
    .rk.g1 { background: linear-gradient(180deg,#ffe6a3,#f0b73f); box-shadow: 0 0 16px -4px rgba(240,183,63,.85); }
    .rk.g2 { background: linear-gradient(180deg,#eef4fa,#b9c6d4); box-shadow: 0 0 14px -4px rgba(185,198,212,.7); }
    .rk.g3 { background: linear-gradient(180deg,#f6cfae,#cf8f5e); box-shadow: 0 0 14px -4px rgba(207,143,94,.7); }
    .rk.g0 { background: transparent; color: var(--muted); font-weight: 500; }
    .bar { position: relative; display: block; height: 3px; margin-top: 7px; border-radius: 2px;
      background: rgba(120,200,255,.10); overflow: hidden; }
    .bar i { position: absolute; inset: 0; width: var(--w, 50%);
      background: linear-gradient(90deg, rgba(53,214,245,.9), rgba(42,124,240,.35)); }
    .bar.neg i { background: linear-gradient(90deg, rgba(255,113,137,.9), rgba(255,113,137,.25)); }
    .up { color: var(--mint); font-variant-numeric: tabular-nums; }
    .down { color: var(--danger); font-variant-numeric: tabular-nums; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; letter-spacing: 0; }
    .pill.ok { background: rgba(79,240,208,.12); border: 1px solid rgba(79,240,208,.3); color: var(--mint); }
    .pill.neg { background: rgba(255,113,137,.13); border: 1px solid rgba(255,113,137,.32); color: var(--danger); }
    .pill.warn { background: rgba(255,194,75,.12); border: 1px solid rgba(255,194,75,.3); color: var(--accent-2); }
    .chart { margin-top: 12px; min-height: 280px; }
    /* 图表外壳：标题/单位/来源/图例是真实 DOM（字体由浏览器渲染，不发虚），画布只画图形 */
    .chart-shell {
      margin-top: 16px; border: 1px solid rgba(120,200,255,.14); border-radius: 14px;
      background: linear-gradient(180deg, rgba(15,29,45,.55), rgba(8,17,28,.34));
      box-shadow: 0 18px 42px -26px rgba(0,0,0,.75);
      overflow: hidden;
    }
    .chart-head {
      display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap;
      padding: 13px 16px 3px;
    }
    .chart-head .ct { color: var(--pure); font-size: 14.5px; font-weight: 700; letter-spacing: -.015em; }
    .chart-head .cu {
      color: #8ea1b4; font-size: 11px; padding: 1px 7px; border-radius: 999px;
      background: rgba(120,200,255,.09); border: 1px solid rgba(120,200,255,.14);
    }
    .chart-head .csrc { margin-left: auto; color: #6d8194; font-size: 10.5px; letter-spacing: .02em; }
    .chart-body { position: relative; padding: 6px 12px 10px; }
    .chart-legend {
      display: flex; flex-wrap: wrap; gap: 7px; padding: 0 16px 13px;
    }
    .chart-legend button {
      display: inline-flex; align-items: center; gap: 6px;
      border: 1px solid rgba(120,200,255,.14); border-radius: 999px;
      background: rgba(120,200,255,.05); color: #b6c6d5;
      padding: 3px 10px; font-size: 11px; cursor: pointer;
      transition: color .25s var(--ease), background .25s var(--ease), opacity .25s var(--ease);
    }
    .chart-legend button:hover { color: var(--pure); background: rgba(120,200,255,.1); }
    .chart-legend button.off { opacity: .38; }
    .chart-legend button i { width: 8px; height: 8px; border-radius: 50%; display: block; }
    .chart-tip {
      position: absolute; left: 0; top: 0; z-index: 6; pointer-events: none; opacity: 0;
      min-width: 128px; padding: 9px 11px; border-radius: 11px;
      background: rgba(9,17,26,0.94); border: 1px solid rgba(120,200,255,.22);
      box-shadow: 0 18px 38px -20px rgba(0,0,0,.9);
      transition: opacity .16s var(--ease), transform .16s var(--ease);
      font-size: 11.5px; color: #dce7f0;
    }
    .chart-tip .h { color: #8ea1b4; font-size: 10.5px; letter-spacing: .03em; margin-bottom: 5px; }
    .chart-tip .r { display: flex; align-items: center; gap: 7px; line-height: 1.7; white-space: nowrap; }
    .chart-tip .r i { width: 3px; height: 12px; border-radius: 2px; flex: 0 0 3px; }
    .chart-tip .r b { margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums; color: #f2f7fa; }
    .chart-canvas {
      display: block; width: 100%; max-width: none;
      height: 400px; cursor: zoom-in;
    }
    .download-row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
    .download-btn {
      border: 1px solid var(--line); background: transparent; color: var(--text);
      border-radius: 6px; padding: 5px 9px; font-size: 12px;
    }
    .download-btn:hover { border-color: var(--accent); color: var(--accent); }
    .chart-modal {
      position: fixed; inset: 0; z-index: 40;
      display: none; align-items: center; justify-content: center;
      padding: 18px;
    }
    .chart-modal.open { display: flex; }
    .chart-modal-backdrop {
      position: absolute; inset: 0;
      background: rgba(2,8,14,0.94);
    }
    .chart-modal-dialog {
      position: relative; z-index: 1;
      width: min(96vw, 1800px); height: min(91vh, 1080px);
      display: flex; flex-direction: column; overflow: hidden;
      border: 1px solid rgba(125, 211, 252, .24); border-radius: 14px;
      background:
        radial-gradient(circle at 12% 0%, rgba(45,212,191,.1), transparent 32%),
        linear-gradient(145deg, rgba(17,31,44,.99), rgba(8,18,28,.99));
      box-shadow: 0 30px 100px rgba(0,0,0,.62);
      animation: chart-modal-in .22s cubic-bezier(.2,.8,.2,1) both;
      will-change: transform, opacity;
    }
    .chart-modal-header {
      min-height: 58px; display: flex; align-items: center; justify-content: space-between;
      gap: 16px; padding: 10px 14px 10px 18px;
      border-bottom: 1px solid rgba(255,255,255,.1);
    }
    .chart-modal-title { min-width: 0; }
    .chart-modal-title strong {
      display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      color: #f3f8fc; font-size: 15px;
    }
    .chart-modal-title span { color: var(--muted); font-size: 11px; }
    .chart-modal-tools { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
    .chart-tool {
      min-width: 34px; height: 32px; padding: 0 10px;
      border: 1px solid rgba(255,255,255,.14); border-radius: 7px;
      background: rgba(255,255,255,.045); color: #dce7ef; font-size: 12px;
    }
    .chart-tool:hover { border-color: rgba(57,196,178,.65); color: #ffffff; }
    .chart-tool.close { border-color: rgba(217,107,117,.3); color: #f0a3aa; }
    .chart-zoom-value {
      min-width: 48px; color: #b9c7d3; font-size: 12px; text-align: center;
    }
    .chart-preview-legend { padding: 0 18px 4px; }
    .chart-preview-legend:empty { display: none; }
    .chart-preview-layout {
      flex: 1; min-height: 0; display: grid;
      grid-template-columns: minmax(0, 1fr) 290px;
    }
    .chart-preview-viewport {
      position: relative; min-width: 0; min-height: 0; overflow: hidden;
      background:
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px),
        rgba(7,16,25,.72);
      background-size: 32px 32px;
      cursor: grab; touch-action: none;
    }
    .chart-preview-viewport.dragging { cursor: grabbing; }
    .chart-preview-canvas-wrap {
      width: 100%; height: 100%; padding: 18px;
      transform-origin: center center; will-change: transform;
    }
    #chartPreviewCanvas { width: 100% !important; height: 100% !important; }
    .chart-preview-data {
      min-height: 0; overflow: auto; padding: 14px;
      border-left: 1px solid rgba(255,255,255,.1);
      background: rgba(9,20,31,.76);
    }
    .chart-preview-data h4 { margin: 0 0 10px; color: #edf4f8; font-size: 13px; }
    .chart-preview-data .data-note { margin: -4px 0 10px; color: var(--muted); font-size: 11px; }
    .chart-data-row {
      display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 8px;
      padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,.07);
      font-size: 12px;
    }
    .chart-data-row span:first-child {
      color: #c7d3dd; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .chart-data-row span:last-child { color: #f2f7fa; font-variant-numeric: tabular-nums; }
    .chart-data-row em { display: block; color: var(--accent); font-style: normal; font-size: 11px; }
    @keyframes chart-modal-in {
      from { opacity: 0; transform: translateY(8px) scale(.992); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    .sources { margin-top: 12px; color: var(--muted); font-size: 12px; }
    .evidence-block {
      margin-top: 12px; border-top: 1px solid var(--line);
      padding-top: 10px; color: var(--muted); font-size: 12px;
    }
    .evidence-block summary { cursor: pointer; color: var(--accent); }
    .evidence-list { display: grid; gap: 8px; margin-top: 10px; }
    .evidence-item {
      padding: 9px 10px; border: 1px solid var(--line); border-radius: 7px;
      background: rgba(255,255,255,.025); line-height: 1.55;
    }
    .evidence-item strong { display: block; color: #dce7ef; font-weight: 600; }
    .ev-open {
      margin-top: 6px; padding: 3px 10px; border-radius: 999px; cursor: pointer;
      border: 1px solid rgba(120, 200, 255, .28); background: rgba(12, 26, 42, .5);
      color: #b9e2ff; font-size: 11.5px; letter-spacing: -.01em;
      transition: border-color .3s cubic-bezier(.32,.72,0,1), color .3s cubic-bezier(.32,.72,0,1);
    }
    .ev-open:hover { border-color: rgba(120, 200, 255, .6); color: #eaf7ff; }
    .composer {
      position: fixed; left: 230px; right: var(--rail-w, 44px); bottom: 0;
      background: linear-gradient(180deg, rgba(7,20,31,0.20), rgba(7,20,31,0.94) 34%);
      border-top: 1px solid rgba(255,255,255,.08);
      padding: 10px 24px 14px; z-index: 3;
    }
    /* 新会话态（hero）：输入框搬进 empty-state 的正文流里，居中、只留输入行。
       触发条件由 JS 控制：#page-chat 上有 hero 类 == 会话区还是空状态。 */
    #page-chat.hero .composer {
      position: static; z-index: auto;
      width: var(--hero-input-w, 460px); max-width: 92%;
      margin: 1.6em auto 0; padding: 0;
      background: none; border-top: none; box-shadow: none;
    }
    #page-chat.hero .composer .suggestions,
    #page-chat.hero .composer .composer-meta { display: none; }
    #page-chat.hero .empty-state { padding-bottom: 8px; }
    /* 新会话态：视角（受众）胶囊整排放到聊天框**下方**，顶边抵住框的左下角（用户 m01869） */
    #page-chat.hero .composer .suggest-title,
    #page-chat.hero .composer #refreshSuggestions,
    #page-chat.hero .composer #refreshNote { display: none; }
    #page-chat.hero .composer-inner { display: flex; flex-direction: column; }
    #page-chat.hero .suggest-head { order: 2; margin: 0; }
    #page-chat.hero .audience-options { margin-left: 0; flex-wrap: wrap; }
    /* 聊天态：推荐行换成「接着问」（与会话上下文相关的追问） */
    .followup-label {
      color: var(--muted); font-size: 11px; letter-spacing: .04em; margin: 4px 0 2px;
    }
    /* 聊天态只留受众选择，通用「试试问/换一批」已被会话追问取代 */
    #page-chat:not(.hero) .suggest-title,
    #page-chat:not(.hero) #refreshSuggestions,
    #page-chat:not(.hero) #refreshNote { display: none; }
    .composer-inner {
      width: 100%; max-width: none;
      margin-left: 0;
      margin-right: 0;
    }
    .audience-options {
      display: flex; gap: 6px; align-items: center;
      margin-left: auto; margin-top: 0; margin-bottom: 0; overflow: visible;
    }
    .audience-chip {
      border: 1px solid var(--line); border-radius: 999px;
      background: transparent; color: var(--muted);
      padding: 4px 9px; font-size: 11px; white-space: nowrap;
      transition: transform .35s var(--ease), border-color .35s var(--ease),
                  background .35s var(--ease), color .35s var(--ease), box-shadow .35s var(--ease);
    }
    /* 与卡片同一套悬浮手感：上浮 2px + 冷光 + 描边提亮 */
    .audience-chip:hover {
      transform: translateY(-2px); color: var(--pure);
      border-color: rgba(120,200,255,.42); background: rgba(120,200,255,.10);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 10px 22px -18px rgba(0,0,0,.9),
                  0 0 34px -16px rgba(63,210,190,.55);
    }
    .audience-chip.active {
      color: var(--accent); border-color: rgba(45,212,191,.45);
      background: rgba(15,107,95,.08);
    }
    .composer .audience-chip {
      color: #c7d5df; border-color: rgba(255,255,255,.18);
    }
    .composer .audience-chip.active {
      color: #ffffff; border-color: rgba(94,234,212,.58);
      background: rgba(45,212,191,.14);
    }
    .suggest-head {
      display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
    }
    .suggest-title {
      font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
      color: var(--muted);
    }
    .suggest-head .ghost {
      padding: 3px 10px; font-size: 11px; border-radius: 999px;
      color: var(--ice); border-color: rgba(53,214,245,.22);
      background: rgba(53,214,245,.06);
      transition: border-color .5s var(--ease), background .5s var(--ease), color .5s var(--ease);
    }
    .suggest-head .ghost:hover {
      color: var(--pure); border-color: rgba(53,214,245,.5); background: rgba(53,214,245,.12);
    }
    #refreshNote { font-size: 11px; }
    .suggestions {
      display: flex; flex-wrap: nowrap; gap: 8px; margin-bottom: 8px;
      max-width: none;
    }
    .suggestions .chip {
      flex: 1 1 0; min-width: 0; max-width: none;
    }
    .chip {
      background: rgba(53,214,245,.06); color: var(--text);
      border: 1px solid rgba(53,214,245,.16);
      border-radius: 999px; padding: 4px 11px; font-size: 11.5px;
      max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      transition: border-color .5s var(--ease), color .5s var(--ease), background .5s var(--ease);
    }
    .chip:hover {
      border-color: rgba(53,214,245,.5); color: var(--pure);
      background: rgba(53,214,245,.13);
    }
    .answer-summary {
      margin: 0 0 16px; font-size: 15.5px; line-height: 1.72; color: var(--pure);
      letter-spacing: -.014em; font-weight: 450;
    }
    /* 结论句里的关键数字提亮 */
    .answer-summary .num-hl { color: var(--accent); font-weight: 650; font-variant-numeric: tabular-nums; }
    .answer-summary .num-neg { color: var(--danger); font-weight: 650; font-variant-numeric: tabular-nums; }
    .empty-state {
      padding: 72px 20px 24px; text-align: center; color: var(--muted);
    }
    .empty-state .empty-kicker {
      color: var(--accent); font-size: 11px; letter-spacing: .08em;
      text-transform: uppercase;
    }
    .empty-state h3 { margin: 9px 0 6px; color: var(--text); font-size: 20px; }
    .empty-state p { margin: 0; font-size: 13px; }
    .thinking-message .bubble {
      position: relative; overflow: hidden;
      display: inline-flex; align-items: center; gap: 11px;
      min-width: 0; max-width: 72%; padding: 9px 15px 9px 13px;
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(14,30,50,.52), rgba(6,15,28,.38));
      border: 1px solid rgba(120,200,255,.22);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 0 26px -14px rgba(53,214,245,.35);
      color: rgba(226,240,250,.9); font-size: 12.5px; letter-spacing: .01em;
    }
    /* 底部一条来回扫的冷光：表示"在跑"，不是卡住。扫光走 transform（合成层），
       不 animate background-position —— 后者每帧重绘整块玻璃底，在慢机器上会拖成一顿一顿。 */
    .thinking-message .bubble::after {
      content: ""; position: absolute; left: 0; bottom: 0; width: 46%; height: 2px;
      background: linear-gradient(90deg, transparent, rgba(53,214,245,.95), transparent);
      will-change: transform;
      animation: thinking-sweep 1.8s var(--ease) infinite;
    }
    @keyframes thinking-sweep {
      0% { transform: translate3d(-100%, 0, 0); opacity: .3; }
      45% { opacity: 1; }
      100% { transform: translate3d(218%, 0, 0); opacity: .3; }
    }
    .thinking-message { scroll-margin-bottom: calc(var(--composer-space, 190px) + 12px); }
    /* 左侧亮弧绕圈。**弧画在伪元素上、动画只作用在这个伪元素自身**：
       Chromium 对"自身同时有 conic-gradient 和 mask 的元素"做 transform 动画时不允许走合成层，
       会被强制每帧重绘（看着就是卡顿）；挪到子元素上就能单独提层、GPU 转。
       冷光也拆开了：用一个静止的径向渐变，不再让 drop-shadow 跟着每帧重算模糊。 */
    .thinking-dots {
      position: relative; flex: 0 0 15px; width: 15px; height: 15px; margin: 0;
      isolation: isolate;
    }
    .thinking-dots::before {
      content: ""; position: absolute; inset: 0; border-radius: 50%;
      background: conic-gradient(from -90deg, rgba(53,214,245,0) 0 46%, rgba(53,214,245,.35) 72%, var(--accent) 96%, var(--pure) 100%);
      -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px));
      mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px));
      will-change: transform;
      animation: thinking-spin 1.1s linear infinite;
    }
    .thinking-dots::after {
      content: ""; position: absolute; inset: -5px; border-radius: 50%; z-index: -1;
      background: radial-gradient(circle, rgba(53,214,245,.30), rgba(53,214,245,0) 68%);
    }
    @keyframes thinking-spin { to { transform: rotate(360deg); } }
    .thinking-dots.paused::before {
      animation: none;
      background: conic-gradient(from -90deg, rgba(255,212,121,.22) 0 70%, var(--accent-2) 100%);
    }
    .thinking-dots.paused::after {
      background: radial-gradient(circle, rgba(255,212,121,.24), rgba(255,212,121,0) 68%);
    }
    /* 暂停 = 整块静止、退掉冷光（:has 不支持时最坏只是扫光还在动，不影响功能） */
    .thinking-message .bubble:has(.thinking-dots.paused) {
      border-color: rgba(255,212,121,.26); color: rgba(233,214,170,.9);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
    }
    .thinking-message .bubble:has(.thinking-dots.paused)::after {
      animation: none; background: rgba(255,212,121,.22); opacity: .5;
    }
    .answer-metrics {
      display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px;
    }
    .answer-metric {
      padding: 8px 12px; border: 1px solid rgba(53,214,245,.18); border-radius: 11px;
      background: rgba(53,214,245,.07);
      display: inline-flex; align-items: baseline; gap: 8px;
      transition: transform .5s var(--ease), border-color .5s var(--ease);
    }
    .answer-metric:hover { transform: translateY(-2px); border-color: rgba(53,214,245,.42); }
    .answer-metric label { color: var(--muted); font-size: 11.5px; letter-spacing: 0; }
    .answer-metric strong {
      font-size: 15px; color: var(--pure); font-variant-numeric: tabular-nums; letter-spacing: -.02em;
    }
    .key-points { margin: 0 0 8px; padding-left: 18px; color: var(--ink, var(--text)); font-size: 13.5px; }
    .key-points li { margin: 6px 0; }
    /* 口径 / 样本 / 期次这类前提信息：小字脚注，不挤在结论里 */
    .caliber-note {
      margin-top: 14px; padding-top: 12px; border-top: 1px dashed rgba(120,200,255,.14);
      color: var(--muted); font-size: 11.5px; line-height: 1.75; letter-spacing: 0;
    }
    .warning-list {
      color: var(--accent-2); font-size: 12px; margin-top: 14px;
      padding: 8px 12px 8px 26px; position: relative; line-height: 1.7;
      background: rgba(255,194,75,.06); border: 1px solid rgba(255,194,75,.18);
      border-radius: 9px; list-style: none;
    }
    .warning-list::before {
      content: ""; position: absolute; left: 12px; top: 14px;
      width: 6px; height: 6px; border-radius: 50%; background: var(--accent-2);
      box-shadow: 0 0 12px 2px rgba(255,194,75,.55);
    }
    .clarification-options { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
    .clarify-chip {
      border: 1px solid var(--line); background: transparent; color: var(--accent);
      border-radius: 999px; padding: 4px 9px; font-size: 11px;
    }
    .input-row { display: flex; gap: 10px; }
    .composer-meta {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; margin-top: 7px; color: #8f9fac; font-size: 11px;
    }
    .composer-meta span:last-child { white-space: nowrap; }
    #page-chat:not(.hero) .composer textarea { min-height: 18px; padding: 4px 10px; }
    textarea {
      flex: 1; resize: none; min-height: 34px; max-height: 72px;
      background: var(--panel); color: var(--text);
      border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px;
      outline: none;
      font-size: 13px;
    }
    textarea:focus { border-color: var(--accent); }
    .send {
      width: 76px; border: 1px solid rgba(120,200,255,.26); border-radius: 10px;
      background: linear-gradient(180deg, rgba(23,45,71,0.94) 0%, rgba(11,23,39,0.90) 100%);
      color: #cfeaf5; font-weight: 650; letter-spacing: .02em;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 6px 18px -14px rgba(0,0,0,.9);
      transition: transform .35s var(--ease), box-shadow .35s var(--ease), border-color .35s var(--ease);
    }
    .send:hover {
      transform: translateY(-1px); filter: brightness(1.06);
      box-shadow: 0 10px 26px -10px rgba(53,214,245,.95), inset 0 1px 0 rgba(255,255,255,.45);
    }
    .send[data-state="running"] { background: linear-gradient(135deg, #b9741d, #ffd479); color: #2a1a02; }
    .send[data-state="paused"] { background: linear-gradient(135deg, #245a8c, #4f9fd8); color: #04121c; }
    .ghost {
      background: transparent; color: var(--text);
      border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px;
    }
    .ghost:hover { border-color: var(--accent); color: var(--accent); }
    .settings { margin-top: 12px; }
    .settings input, .settings select {
      width: 100%; padding: 8px 9px; margin: 4px 0 10px;
      background: var(--panel); color: var(--text);
      border: 1px solid var(--line); border-radius: 7px;
    }
    .upload { margin-top: 14px; padding: 12px; border: 1px dashed var(--line); border-radius: 10px; }
    .upload input { width: 100%; color: var(--muted); }
    .muted { color: var(--muted); }
    .error { color: var(--danger); }
    @media (max-width: 900px) {
      .sidebar { position: relative; width: 100%; inset: auto; }
      .app { display: block; }
      .main { margin-left: 0; width: 100%; }
      .composer { left: 0; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .workspace { display: block; padding: 0 14px; }
      .context-panel { display: none; }
      .chart-modal { padding: 8px; }
      .chart-modal-dialog { width: 98vw; height: 94vh; }
      .chart-modal-header { align-items: flex-start; flex-direction: column; }
      .chart-modal-tools { width: 100%; overflow-x: auto; }
      .chart-preview-layout {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(0, 1fr) 210px;
      }
      .chart-preview-data { border-left: 0; border-top: 1px solid rgba(255,255,255,.1); }
    }
    @media (max-width: 1360px) {
      .context-panel { display: none; }
      .metrics, .workspace, .conversation-column, .chat-wrap, .composer-inner {
        max-width: none;
      }
    }
    @media (min-width: 1361px) {
      .composer-inner { max-width: none; }
    }
  </style>
</head>
<body class="page-chat">
<div class="fx-bg" aria-hidden="true"></div>
<script src="/assets/chart.umd.min.js?v=20260914"></script>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><img class="brand-logo" src="/assets/logo_main.png" alt="FinSight 财报智析"></div>
    <div class="caption">上市公司财报分析平台</div>

    <nav class="nav">
      <button class="nav-btn active" data-page="chat">智能问数</button>
      <button class="nav-btn" data-page="files">文件库</button>
      <button class="nav-btn" data-page="validation">数据校验</button>
      <button class="nav-btn" data-page="coverage">数据体检</button>
      <button class="nav-btn" data-page="import">数据导入</button>
      <button class="nav-btn" data-page="settings">设置</button>
    </nav>

    <div class="side-title">历史会话</div>
    <div class="history-actions">
      <button class="ghost" id="newSession">新建会话</button>
      <button class="ghost" id="clearHistory">清空历史</button>
    </div>
    <div id="historyList"></div>
    <button class="ghost" id="logoutBtn" style="margin-top:10px">退出登录</button>
  </aside>

  <aside class="sql-rail" id="sqlRail" aria-label="SQL 执行日志">
    <button class="sql-rail-tab" id="sqlRailTab" title="展开 SQL 执行日志">
      <span class="sql-rail-tab-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></span>
      <span class="sql-rail-tab-text">SQL 执行日志</span>
    </button>
    <div class="sql-rail-body">
      <div class="sql-rail-head">
        <strong>SQL 执行日志</strong>
        <button class="sql-log-toggle" id="sqlLogToggle">收起</button>
      </div>
      <section class="sql-console" id="sqlConsole">
        <div class="sql-console-head">
          <span class="sql-log-status" id="sqlLogSummary">等待任务</span>
          <button class="sql-log-toggle" id="sqlLogCopy" title="把最近一次任务的 SQL 语句复制成文本">复制</button>
        </div>
        <div class="sql-log-list" id="sqlLogList" hidden></div>
      </section>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <h1 id="pageTitle">智能问数</h1>
      <div class="mode-control">
        <span id="status" class="status">就绪</span>
        <select id="analysisMode" class="mode-select" aria-label="分析模式">
          <option value="deepseek">智能财报分析</option>
          <option value="local">本地数据模式</option>
        </select>
      </div>
    </div>

    <section class="page active hero" id="page-chat">
      <div class="overview" id="metrics"></div>
      <div class="workspace">
        <div class="conversation-column">
          <div class="chat-wrap" id="messages">
            <div class="empty-state">
              <div class="empty-kicker">Financial Analysis Workspace</div>
              <h3>输入公司名称和财务指标开始分析</h3>
              <p>支持财务问数、股东结构、趋势图和财报文本检索。</p>
            </div>
          </div>
        </div>
        <aside class="context-panel">
          <section class="context-card">
            <div class="context-label">当前会话</div>
            <div class="context-title" id="contextSessionTitle">新会话</div>
            <div class="context-meta" id="contextSessionMeta">尚未开始分析</div>
          </section>
          <section class="context-card">
            <div class="context-label">分析状态</div>
            <div class="context-stage" id="contextStage">等待提问</div>
            <div class="context-progress"><span id="contextProgress"></span></div>
          </section>
          <section class="context-card">
            <div class="context-label">数据范围</div>
            <div class="context-lines">
              <span>结构化财报</span><strong>5 张业务表</strong>
              <span>文本证据</span><strong>财报 RAG</strong>
              <span>图表输出</span><strong>高清导出</strong>
            </div>
          </section>
        </aside>
      </div>
      <div class="composer">
        <div class="composer-inner">
          <div class="suggest-head">
            <span class="suggest-title">试试问</span>
            <button class="ghost" id="refreshSuggestions">换一批</button>
            <span class="muted" id="refreshNote"></span>
            <div class="audience-options" id="audienceOptions">
              <button class="audience-chip active" data-audience="综合分析师">综合分析师</button>
              <button class="audience-chip" data-audience="投行/卖方分析师">投行/卖方分析师</button>
              <button class="audience-chip" data-audience="企业管理层">企业管理层</button>
              <button class="audience-chip" data-audience="风险控制/审计">风险控制/审计</button>
              <button class="audience-chip" data-audience="个人投资者">个人投资者</button>
            </div>
          </div>
          <div class="suggestions" id="suggestions"></div>
          <div class="input-row">
            <textarea id="question" placeholder="给FinSight发消息"></textarea>
            <button class="send" id="sendBtn">发送</button>
          </div>
          <div class="composer-meta">
            <span>未填写 API 密钥时，系统支持核心财务数据的基础查询。</span>
            <span>Enter 发送 · Shift+Enter 换行</span>
          </div>
        </div>
      </div>
    </section>

    <section class="page" id="page-files">
      <div class="page-panel">
        <h2>文件库</h2>
        <div class="file-search-bar">
          <input id="fileSearch" placeholder="搜索公司或文件名">
          <button id="fileSearchBtn">查询</button>
          <button class="ghost" id="deleteAllFilesBtn" title="清空文件库的入库记录（磁盘原文不动）">全部删除</button>
        </div>
        <div id="fileList"></div>
      </div>
    </section>

    <section class="page" id="page-import">
      <div class="page-panel">
        <h2>数据导入</h2>
        <p class="muted">少量文件可直接上传；几万个文件请填写本地目录，系统会在后台排队处理。</p>
        <div class="upload">
          <input id="files" type="file" accept=".pdf,.docx" multiple>
          <input id="localDir" placeholder="本地目录路径">
          <button class="ghost" id="uploadBtn">开始导入</button>
          <div id="uploadState" class="muted"></div>
        </div>
        <div class="upload" id="importProgress" style="display:none">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <strong id="importPhase">导入中</strong>
            <span class="muted" id="importCounts">0 / 0</span>
            <button class="ghost" id="importPauseBtn">暂停</button>
            <button class="ghost" id="importResumeBtn" disabled>继续</button>
            <button class="ghost" id="importStopBtn">结束</button>
          </div>
          <p class="muted" id="importRunning" style="margin:8px 0 0"></p>
          <div class="context-progress" style="height:6px;margin-top:10px"><span id="importBar"></span></div>
          <details id="importErrors" style="display:none;margin-top:8px">
            <summary class="muted">失败与警告明细</summary>
            <ul id="importErrorList" class="muted" style="margin:6px 0 0;padding-left:18px"></ul>
          </details>
          <p class="muted" id="importNote" style="margin:9px 0 0"></p>
        </div>
      </div>
    </section>

    <section class="page" id="page-validation">
      <div class="page-panel">
        <h2>数据校验报告</h2>
        <div id="validationSummary" class="muted"></div>
        <div id="validationList"></div>
      </div>
    </section>

    <section class="page" id="page-coverage">
      <div class="page-panel">
        <h2>数据覆盖体检</h2>
        <div class="file-search-bar">
          <button class="ghost" id="refreshCoverage">重新体检</button>
          <span class="muted" id="coverageSummary">还没体检。</span>
        </div>
        <div id="coverageList"></div>
      </div>
    </section>

    <section class="page" id="page-settings">
      <div class="page-panel">
        <h2>接口设置</h2>
        <p class="muted">DeepSeek 密钥由服务器读取 DEEPSEEK_API_KEY，不在浏览器中保存或显示。</p>
        <div class="settings">
          <label>API 地址</label>
          <input id="baseUrl" value="https://api.deepseek.com" readonly>
          <label>对话模型</label>
          <input id="llmModel" value="deepseek-flash" readonly>
          <label>金额单位</label>
          <select id="unitMode">
            <option>自动</option><option>元</option><option>万元</option><option>亿元</option>
          </select>
          <label>回答风格</label>
          <select id="answerStyle">
            <option>分析师视角</option><option>简洁结论</option><option>详细讲解</option>
          </select>
          <button class="ghost" id="saveSettings">保存设置</button>
          <div id="settingsState" class="muted"></div>
        </div>
      </div>
    </section>
  </main>
</div>

<div class="chart-modal" id="chartModal" aria-hidden="true">
  <div class="chart-modal-backdrop" data-close-chart-modal></div>
  <section class="chart-modal-dialog" role="dialog" aria-modal="true" aria-label="图表高清预览">
    <header class="chart-modal-header">
      <div class="chart-modal-title">
        <strong id="chartPreviewTitle">图表高清预览</strong>
        <span>滚轮缩放 · 拖拽移动 · 双击重置</span>
      </div>
      <div class="chart-modal-tools">
        <button class="chart-tool" id="chartZoomOut" title="缩小">−</button>
        <span class="chart-zoom-value" id="chartZoomValue">100%</span>
        <button class="chart-tool" id="chartZoomIn" title="放大">＋</button>
        <button class="chart-tool" id="chartZoomReset">重置</button>
        <button class="chart-tool" id="chartPreviewDownload">下载 PNG</button>
        <button class="chart-tool close" id="chartPreviewClose" title="关闭">关闭</button>
      </div>
    </header>
    <div class="chart-preview-legend" id="chartPreviewLegend"></div>
    <div class="chart-preview-layout">
      <div class="chart-preview-viewport" id="chartPreviewViewport">
        <div class="chart-preview-canvas-wrap" id="chartPreviewCanvasWrap">
          <canvas id="chartPreviewCanvas"></canvas>
        </div>
      </div>
      <aside class="chart-preview-data" id="chartPreviewData"></aside>
    </div>
  </section>
</div>

<script>
let recommendations = [];
let loadedHistoryId = null;
let selectedAudience = "综合分析师";
let currentSessionId = null;
let currentTaskId = null;
let chatStarting = false;
let currentTaskState = null;
let taskPollTimer = null;
let sqlLogExpanded = false;
// 复制按钮要能拿到最近一次的日志：renderSqlLogs 每次把裁剪后的条目留一份在这。
let lastSqlLogs = [];
let previewChartInstance = null;
let previewChartData = null;
let previewTransform = { scale: 1, x: 0, y: 0 };
let previewFrame = 0;
let previewDragging = false;
let previewPointer = { x: 0, y: 0 };

const pageTitles = {
  chat: "智能问数",
  files: "文件库",
  validation: "数据校验",
  coverage: "数据体检",
  import: "数据导入",
  settings: "设置"
};
const SETTINGS_KEY = "finsight_settings";

function collectSettings() {
  return {
    baseUrl: "https://api.deepseek.com",
    llmModel: "deepseek-flash",
    analysisMode: document.getElementById("analysisMode").value,
    unitMode: document.getElementById("unitMode").value,
    answerStyle: document.getElementById("answerStyle").value
  };
}

function applySettings(settings) {
  if (!settings) return;
  document.getElementById("baseUrl").value = "https://api.deepseek.com";
  document.getElementById("llmModel").value = "deepseek-flash";
  document.getElementById("analysisMode").value =
    settings.analysisMode === "local" ? "local" : "deepseek";
  document.getElementById("unitMode").value = settings.unitMode || "自动";
  document.getElementById("answerStyle").value = settings.answerStyle || "分析师视角";
}

function syncModeStatus() {
  const mode = document.getElementById("analysisMode").value;
  document.getElementById("status").textContent =
    mode === "deepseek" ? "智能分析就绪" : "本地数据就绪";
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(collectSettings()));
    document.getElementById("settingsState").textContent = "设置已保存";
  } catch (error) {
    document.getElementById("settingsState").textContent = "保存失败";
  }
}

function loadSavedSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return false;
    applySettings(JSON.parse(raw));
    return true;
  } catch {
    return false;
  }
}

function switchPage(pageName) {
  document.querySelectorAll(".page").forEach(page => {
    page.classList.toggle("active", page.id === `page-${pageName}`);
  });
  document.querySelectorAll(".nav-btn").forEach(button => {
    button.classList.toggle("active", button.dataset.page === pageName);
  });
  document.getElementById("pageTitle").textContent = pageTitles[pageName] || "";
  // SQL 执行日志右栏只在主界面（智能问数）出现
  document.body.classList.toggle("page-chat", pageName === "chat");
  if (pageName === "files") loadDocuments();
  if (pageName === "validation") loadValidation();
  if (pageName === "coverage") loadCoverage();
}

document.querySelectorAll(".nav-btn").forEach(button => {
  button.onclick = () => switchPage(button.dataset.page);
});
document.querySelectorAll(".audience-chip").forEach(button => {
  button.onclick = () => {
    selectedAudience = button.dataset.audience || "综合分析师";
    document.querySelectorAll(".audience-chip").forEach(item => {
      item.classList.toggle("active", item === button);
    });
  };
});

async function loadRecommendations() {
  try {
    const payload = await readJson(await fetch("/api/recommendations"), "推荐问题");
    recommendations = Array.isArray(payload) ? payload : [];
  } catch {
    recommendations = [];
  }
}

document.getElementById("refreshSuggestions").onclick = async () => {
  const button = document.getElementById("refreshSuggestions");
  const note = document.getElementById("refreshNote");
  button.disabled = true;
  note.textContent = "正在换一批…";
  try {
    await loadRecommendations();
    pickSuggestions();
    // 换一批不需要回执，只在失败时留字
    note.textContent = "";
  } catch (error) {
    note.textContent = error.message || "刷新失败";
  } finally {
    button.disabled = false;
  }
};

// —— 新会话 hero 态：输入框搬进 empty-state 正文流居中（宽=说明文字实测×2），
//    发出第一条消息后回到页面底部坞位；回坞就是把 composer 放回 #page-chat 末尾。
const chatPageEl = document.getElementById("page-chat");
const composerEl = chatPageEl.querySelector(".composer");
let lastUserQuestion = "";

function followupQuestions(question) {
  // 纯前端规则：从上一问里认公司名和关心的指标，生成三条贴着当前会话的追问。
  const q = String(question || "");
  if (!q) return [];
  const company = (q.match(/[\u4e00-\u9fa5A-Za-z0-9]{2,10}?(?:公司|集团|股份|科技|技术|银行|证券|保险|能源|汽车|药业|控)/) || [""])[0];
  const has = words => words.some(w => q.includes(w));
  if (company) {
    return [
      `${company}近四年营收和净利润趋势图`,
      has(["股东"]) ? `${company}的十大流通股东明细` : `${company}的股东结构`,
      has(["毛利", "净利", "ROE", "roe", "净资产收益"]) ? `${company}毛利率和净利率的逐年变化` : `${company}和同行业龙头对比怎么样`,
    ];
  }
  return [
    "把刚才的指标画成趋势图",
    "对比上一期的变化",
    "列出支撑刚才结论的财报原文",
  ];
}

function fitHeroComposer() {
  const cap = document.querySelector("#messages .empty-state p");
  if (!cap) return;
  const wrap = document.getElementById("messages");
  // p 是块级元素、宽度=整列，所以这里量的是"列宽的一半"（用户要求：比原来短一半）
  const w = Math.min(
    Math.max(cap.getBoundingClientRect().width * 0.5, 320),
    Math.max((wrap.clientWidth || 640) - 32, 320)
  );
  chatPageEl.style.setProperty("--hero-input-w", `${Math.round(w)}px`);
}

function setHeroMode(on) {
  chatPageEl.classList.toggle("hero", !!on);
  const empty = document.querySelector("#messages .empty-state");
  if (on && empty) {
    if (composerEl.parentElement !== empty) empty.appendChild(composerEl);
    fitHeroComposer();
  } else if (composerEl.parentElement !== chatPageEl) {
    chatPageEl.appendChild(composerEl);
  }
  pickSuggestions();
  syncComposerSpace();
}

function pickSuggestions() {
  const box = document.getElementById("suggestions");
  if (!box) return;
  box.innerHTML = "";
  // 新会话（hero）态不展示推荐；聊天态展示贴着当前会话的追问。
  if (chatPageEl.classList.contains("hero") || !lastUserQuestion) return;
  const label = document.createElement("div");
  label.className = "followup-label";
  label.textContent = "接着问";
  box.appendChild(label);
  followupQuestions(lastUserQuestion).forEach(text => {
    const button = document.createElement("button");
    button.className = "chip";
    button.textContent = text;
    button.onclick = () => {
      document.getElementById("question").value = text;
      sendQuestion();
    };
    box.appendChild(button);
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;"
  })[char]);
}

async function readJson(response, label) {
  // 后端出错时也是 JSON（{"detail": "..."}）或干脆一段 HTML：
  // 不先验 response.ok，调用方就把错误对象当列表用，面板安静地变空白。
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error((payload && payload.detail) || `${label || "请求"}失败（HTTP ${response.status}）`);
  }
  return payload;
}

async function loadSummary() {
  const box = document.getElementById("metrics");
  let data;
  try {
    data = await readJson(await fetch("/api/summary"), "概览指标");
  } catch (error) {
    box.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    return;
  }
  const count = key => Number(data[key] || 0).toLocaleString("zh-CN");
  const facts = [
    { label: "公司已入库", value: count("companies"), unit: "家" },
    { label: "财报文件", value: count("documents"), unit: "份" },
    { label: "可检索切片", value: count("rag_chunks"), unit: "条" },
    { label: "业绩记录", value: count("performance"), unit: "条" },
  ];
  const periods = Array.isArray(data.periods) ? data.periods.slice(0, 3) : [];
  box.className = "overview";
  box.innerHTML = `
    <div class="ov-strip">
      ${facts.map(item => `<span class="ov-fact"><b>${item.value}</b><em>${item.unit}</em><i>${escapeHtml(item.label)}</i></span>`).join('<span class="ov-sep"></span>')}
      ${periods.length ? `<span class="ov-periods">已覆盖报告期 ${periods.map(escapeHtml).join(" · ")}</span>` : ""}
    </div>
    </div>`;
}

async function loadHistory() {
  const list = document.getElementById("historyList");
  let items;
  try {
    items = await readJson(await fetch("/api/history"), "历史会话");
  } catch (error) {
    list.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    return;
  }
  items = Array.isArray(items) ? items : [];
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<div class="muted">暂无历史会话</div>';
    return;
  }
  items.forEach(item => {
    const row = document.createElement("div");
    row.className = "history-row";
    const openButton = document.createElement("button");
    openButton.className = "history-open";
    openButton.textContent = item.title;
    openButton.onclick = async () => {
      // 切进历史会话前先终止在跑的轮询链，否则旧任务完成时会把答案插进这份历史会话、还会改走 currentSessionId。
      if (taskPollTimer) {
        clearTimeout(taskPollTimer);
        taskPollTimer = null;
      }
      currentTaskId = null;
      currentTaskState = null;
      chatStarting = false;
      let detail;
      try {
        detail = await readJson(await fetch(`/api/history/${item.id}`), "历史会话详情");
      } catch (error) {
        // 读失败就留在列表里报错：切进一个空白聊天页比报错更难懂
        updateTaskContext(error.message, 0);
        return;
      }
      switchPage("chat");
      renderSession(detail);
      loadedHistoryId = item.id;
      currentSessionId = detail.id;
      updateSessionContext(detail.title, `${detail.messages?.length || 0} 条消息`);
      updateTaskContext("已载入历史会话", 100);
    };
    const deleteButton = document.createElement("button");
    deleteButton.className = "history-delete";
    deleteButton.textContent = "×";
    deleteButton.title = "删除会话";
    deleteButton.setAttribute("aria-label", "删除会话");
    deleteButton.onclick = async () => {
      const deletingCurrent =
        currentSessionId === item.id || loadedHistoryId === item.id;
      await fetch(`/api/history/${item.id}`, { method: "DELETE" });
      if (deletingCurrent) {
        resetChatSession();
      }
      loadHistory();
    };
    row.appendChild(openButton);
    row.appendChild(deleteButton);
    list.appendChild(row);
  });
}

function renderSession(session) {
  const box = document.getElementById("messages");
  box.innerHTML = "";
  if (!session.messages?.length) {
    box.innerHTML = emptyStateHtml();
    lastUserQuestion = "";
    setHeroMode(true);
    return;
  }
  (session.messages || []).forEach(message => {
    if (message.role === "user") {
      const userMessage = document.createElement("div");
      userMessage.className = "message user";
      userMessage.innerHTML = `<div class="bubble">${escapeHtml(message.content || "")}</div>`;
      box.appendChild(userMessage);
    } else if (message.role === "assistant" && message.result) {
      renderResult("", message.result, false, false);
    }
  });
  const lastResult = [...(session.messages || [])]
    .reverse()
    .find(message => message.role === "assistant" && message.result)
    ?.result;
  const lastUser = [...(session.messages || [])]
    .reverse()
    .find(message => message.role === "user");
  lastUserQuestion = (lastUser && lastUser.content) || "";
  setHeroMode(false);
  renderSqlLogs(lastResult?.execution_logs || [], "completed");
  setSqlLogExpanded(false);
}

function emptyStateHtml() {
  return `
    <div class="empty-state">
      <div class="empty-kicker">Financial Analysis Workspace</div>
      <h3>输入公司名称和财务指标开始分析</h3>
      <p>支持财务问数、股东结构、趋势图和财报文本检索。</p>
    </div>`;
}

function resetChatSession() {
  document.querySelectorAll("#messages .chart-canvas").forEach(canvas => {
    if (canvas._chartInstance) {
      canvas._chartInstance.destroy();
      canvas._chartInstance = null;
    }
  });
  document.getElementById("messages").innerHTML = emptyStateHtml();
  // 关键顺序：hero 态下 composer 就住在 #messages 里，上面这句会把它连同旧空状态一起摘出 DOM。
  // 必须先 setHeroMode(true) 把它挂回新空状态，再碰 #question——否则 getElementById 拿到 null、
  // 整函数抛在半路，输入框凭空消失（轮廿「删除会话后新建会话没用」实锤根因）。
  setHeroMode(true);
  document.getElementById("question").value = "";
  syncModeStatus();
  loadedHistoryId = null;
  currentSessionId = null;
  currentTaskId = null;
  currentTaskState = null;
  chatStarting = false;
  if (taskPollTimer) {
    clearTimeout(taskPollTimer);
    taskPollTimer = null;
  }
  setComposerState("idle");
  renderSqlLogs([], "");
  setSqlLogExpanded(false);
  updateSessionContext("新会话", "尚未开始分析");
  updateTaskContext("等待提问", 0);
  lastUserQuestion = "";
  setHeroMode(true);
  document.getElementById("question").focus();
}

async function loadDocuments() {
  const query = document.getElementById("fileSearch")?.value?.trim() || "";
  const list = document.getElementById("fileList");
  let groups;
  try {
    groups = await readJson(await fetch(`/api/documents?query=${encodeURIComponent(query)}`), "文件列表");
  } catch (error) {
    // 接口报错绝不能显示成「暂无已导入文件」，那会让人以为文件丢了
    list.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    return;
  }
  groups = Array.isArray(groups) ? groups : [];
  list.innerHTML = "";
  if (!groups.length) {
    list.innerHTML = '<div class="muted">暂无已导入文件</div>';
    return;
  }
  groups.forEach(group => {
    const wrapper = document.createElement("div");
    wrapper.className = "file-group";
    wrapper.innerHTML = `<div class="file-company">${escapeHtml(group.company_name)}</div>`;
    group.documents.forEach(documentItem => {
      const row = document.createElement("div");
      row.className = "file-item";
      row.innerHTML = `
        <span class="file-name" title="${escapeHtml(documentItem.file_name)}">
          ${escapeHtml(documentItem.file_name)}
        </span>
        <span class="file-actions">
          <button class="file-open" title="打开">打开</button>
          <button class="file-delete" title="删除">删除</button>
        </span>`;
      row.querySelector(".file-open").onclick = () => {
        window.open(`/api/documents/open/${documentItem.id}`, "_blank");
      };
      row.querySelector(".file-delete").onclick = async () => {
        try {
          await readJson(await fetch(`/api/documents/${documentItem.id}`, { method: "DELETE" }), "删除文件");
        } catch (error) {
          // 删除失败要说出来：原来直接刷列表，文件还在，看上去像界面抽风
          list.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
          return;
        }
        loadDocuments();
        loadSummary();
        loadRecommendations().then(pickSuggestions);
      };
      wrapper.appendChild(row);
    });
    list.appendChild(wrapper);
  });
}

// 数据覆盖体检：一次问清「每家几份原文、哪些报告期、还缺什么」。
// 有了它，某家只导进摘要级小文件这种事当场就能看见，不用等提问答不上来才发现。
async function loadCoverage() {
  const summary = document.getElementById("coverageSummary");
  const list = document.getElementById("coverageList");
  if (!summary || !list) return;
  summary.textContent = "正在体检…";
  try {
    const payload = await readJson(await fetch("/api/coverage"), "数据体检");
    const stat = payload.summary || {};
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    const registryOnly = Number(stat.registry_only || 0);
    summary.textContent =
      `库里体检 ${Number(stat.in_library || 0)} 家 · ${Number(stat.documents || 0)} 份原文 · ` +
      `有数据 ${Number(stat.with_data || 0)} 家 · 有缺口 ${Number(stat.with_gaps || 0)} 家 · ` +
      `报告期 ${(stat.periods || []).join("、") || "无"}` +
      `（体检于 ${escapeHtml(payload.generated_at || "")}）` +
      (registryOnly
        ? `　另有 ${registryOnly} 家只在证券档案里、库里没有任何原文和数据 —— 没导入过，不算缺口，不计入体检。`
        : "");
    list.innerHTML = rows.map(row => {
      const flags = row.flags || [];
      const tables = row.tables || {};
      const detail = [
        `原文 ${Number(row.document_count || 0)} 份`,
        `最大 ${Number(row.largest_document_mb || 0)}MB`,
        `报告期 ${(row.periods || []).join("、") || "无"}`,
        `业绩概览 ${Number(tables["业绩概览"] || 0)} 行`
      ].join(" · ");
      const verdict = flags.length
        ? flags.map(item =>
            `<span class="cov-flag${item.level === "bad" ? " bad" : ""}">${escapeHtml(item.text)}</span>`
          ).join(" · ")
        : '<span class="cov-ok">覆盖正常</span>';
      return `<div class="cov-row"><b>${escapeHtml(row.company_name || "")}</b>` +
        `<span class="cov-code">${escapeHtml(row.stock_code || "")}</span>` +
        `<span class="muted">${escapeHtml(detail)}</span>${verdict}</div>`;
    }).join("") || '<p class="muted">库里还没有公司数据。</p>';
  } catch (error) {
    summary.textContent = `体检失败：${error.message}`;
    list.innerHTML = "";
  }
}

document.getElementById("refreshCoverage").onclick = loadCoverage;

async function loadValidation() {
  const summary = document.getElementById("validationSummary");
  const list = document.getElementById("validationList");
  let rows, quality, vector;
  try {
    [rows, quality, vector] = await Promise.all([
      fetch("/api/validation").then(response => readJson(response, "校验结果")),
      fetch("/api/data-quality").then(response => readJson(response, "数据覆盖")),
      fetch("/api/vector-index").then(response => readJson(response, "向量索引状态"))
    ]);
  } catch (error) {
    summary.textContent = error.message;
    list.innerHTML = "";
    return;
  }
  rows = Array.isArray(rows) ? rows : [];
  quality = quality || {};
  vector = vector || {};
  const indexText = `向量索引 ${vector.current_model}/${vector.total}，${vector.ready ? "已就绪" : "构建中"}`;
  const coverageText = `公司 ${quality.company_count} 家，缺失文件 ${quality.documents?.missing_files || 0} 个`;
  if (!rows.length) {
    summary.textContent = `暂无校验结果，导入财报后自动生成。${indexText}；${coverageText}。`;
    list.innerHTML = "";
    return;
  }
  const passed = rows.filter(item => item["结果"] === "通过").length;
  const failed = rows.filter(item => item["结果"] === "异常").length;
  const pending = rows.filter(item => item["结果"] === "待补充").length;
  summary.textContent =
    `共 ${rows.length} 项：通过 ${passed}，异常 ${failed}，待补充 ${pending}；` +
    `${indexText}；${coverageText}`;
  list.innerHTML = renderTable(rows);
}

function parseCellNumber(text) {
  const raw = String(text ?? "").trim();
  if (!raw) return null;
  const compact = raw.replace(/\s+/g, "").replace(/,/g, "").replace(/[（(]/g, "-").replace(/[）)]/g, "");
  const matched = compact.match(/^([+-]?\d+(?:\.\d+)?)(亿元|万元|亿|万|元|%|倍|股)?$/);
  if (!matched) return null;
  const num = Number(matched[1]);
  if (!Number.isFinite(num)) return null;
  return { num, unit: matched[2] || "" };
}

const RANK_COLUMNS = new Set(["排名", "排行", "序号", "名次"]);
const STATUS_COLUMNS = new Set(["结果", "状态", "结论"]);
const STATUS_TONE = { 通过: "ok", 正常: "ok", 异常: "neg", 失败: "neg", 缺失: "neg", 待补充: "warn", 提示: "warn" };
const BAR_COLUMNS = ["净利润", "归母净利润", "数值", "金额", "营业收入", "营收", "本期数值"];

function pickBarColumn(meta, rowCount) {
  if (rowCount < 2 || rowCount > 12) return "";
  const hit = meta.find(item => item.numeric && BAR_COLUMNS.some(name => item.column.includes(name)));
  if (!hit || hit.unit === "%" || hit.absMax <= 0) return "";
  return hit.column;
}

function renderTable(rows) {
  if (!rows || !rows.length) return "";
  const columns = Object.keys(rows[0]);
  const meta = columns.map(column => {
    const parsed = rows.map(row => parseCellNumber(row[column])).filter(Boolean);
    const numeric = parsed.length >= Math.max(1, Math.ceil(rows.length * 0.7));
    const units = [...new Set(parsed.map(item => item.unit).filter(Boolean))];
    return {
      column,
      numeric,
      unit: numeric && units.length === 1 ? units[0] : "",
      absMax: numeric && parsed.length ? Math.max(...parsed.map(item => Math.abs(item.num))) : 0
    };
  });
  const barColumn = pickBarColumn(meta, rows.length);
  const head = meta.map(item => {
    const classes = [];
    if (item.numeric) classes.push("num");
    if (item.column.includes("代码")) classes.push("code");
    const unit = item.unit ? `<span class="unit">单位 ${escapeHtml(item.unit)}</span>` : "";
    const label = escapeHtml(item.column) + unit;
    return `<th${classes.length ? ` class="${classes.join(" ")}"` : ""}>${label}</th>`;
  }).join("");
  const body = rows.map(row => {
    const cells = meta.map(item => {
      const value = row[item.column];
      const text = String(value ?? "");
      if (RANK_COLUMNS.has(item.column)) {
        const order = Number(parseCellNumber(text)?.num ?? NaN);
        const tone = order === 1 ? "g1" : order === 2 ? "g2" : order === 3 ? "g3" : "g0";
        return `<td><span class="rk ${tone}">${escapeHtml(text)}</span></td>`;
      }
      if (STATUS_COLUMNS.has(item.column) && STATUS_TONE[text.trim()]) {
        return `<td><span class="pill ${STATUS_TONE[text.trim()]}">${escapeHtml(text)}</span></td>`;
      }
      const parsed = item.numeric ? parseCellNumber(text) : null;
      if (!parsed) {
        const extra = item.column.includes("代码") ? ` class="code"` : "";
        return `<td${extra}>${escapeHtml(text)}</td>`;
      }
      const shown = item.unit ? text.replace(/\s*(亿元|万元|亿|万|元|%|倍|股)\s*$/, "") : text;
      const negative = parsed.num < 0;
      if (item.column === barColumn) {
        const width = Math.max(4, Math.round((Math.abs(parsed.num) / item.absMax) * 100));
        return `<td class="num"><span class="cellnum">${escapeHtml(shown)}</span><span class="bar${negative ? " neg" : ""}" style="--w:${width}%"><i></i></span></td>`;
      }
      if (item.unit === "%" || /同比|增长|占比|变动/.test(item.column)) {
        const tone = negative ? "down" : "up";
        return `<td class="num"><span class="${tone}">${escapeHtml(text)}</span></td>`;
      }
      return `<td class="num">${escapeHtml(shown)}</td>`;
    }).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/* 结论句里的关键金额/百分比提亮，负数标红 */
function highlightNumbers(text) {
  return String(text || "").replace(
    /([+-]?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|亿|万|元|%|倍)/g,
    (whole, number) => {
      const negative = String(number).trim().startsWith("-");
      return `<span class="${negative ? "num-neg" : "num-hl"}">${whole}</span>`;
    }
  );
}

function cleanAnswer(text) {
  return String(text || "")
    .replace(/```[\s\S]*?```/g, "")
    .replace(/^\s*SQL\s*[:：].*$/gim, "")
    .replace(/\\\|/g, "|")
    .replace(/#{1,6}\s*/g, "")
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/__(.*?)__/g, "$1")
    .replace(/:?-{3,}:?/g, "")
    .replace(/\|/g, " ")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function ensureSentenceEnd(text) {
  const value = String(text || "").trim();
  if (!value) return value;
  if (/[。！？!?…]$/.test(value)) return value;
  if (/[；;]$/.test(value)) return value.slice(0, -1).trimEnd() + "。";
  return value + "。";
}

/* 把结论句里夹着的前提说明（口径/样本/报告期）拆出来，放进小字脚注 */
function splitCaliberNote(text) {
  const lines = String(text || "").split(/\n+/).map(line => line.trim()).filter(Boolean);
  const bodyLines = [];
  const caliberLines = [];
  lines.forEach(line => {
    if (/^(口径|样本|说明|统计范围)[:：]/.test(line)) {
      caliberLines.push(line);
    } else {
      bodyLines.push(line);
    }
  });
  return { body: bodyLines.join("\n"), caliber: caliberLines.join("　") };
}

function normalizeForDedupe(text) {
  return String(text || "").replace(/[\s，,。.、；;：:（）()「」“”"'%％．·\-—_~!！?？\/|｜《》<>]/g, "");
}

/* 比"重复"之前先摘掉公司全称：同一家公司的全称在句首反复出现并不算新信息 */
function stripCompanyNames(text) {
  return String(text || "")
    .replace(/[\u4e00-\u9fa5A-Za-z]{2,14}?(集团)?股份?有限(责任)?公司/g, "")
    .replace(/公司/g, "");
}

/* 这一句是否已经被正文说过（按重叠切片命中率判断，避免正文与要点重复一遍） */
function mostlyContainedIn(text, containerKey, threshold = 0.35) {
  const key = normalizeForDedupe(stripCompanyNames(text));
  if (!key) return true;
  if (!containerKey) return false;
  const size = 8;
  const chunks = [];
  for (let index = 0; index + size <= key.length; index += 4) {
    chunks.push(key.slice(index, index + size));
  }
  if (!chunks.length) return containerKey.includes(key);
  const hit = chunks.filter(chunk => containerKey.includes(chunk)).length;
  return hit / chunks.length >= threshold;
}

function chartNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  const absolute = Math.abs(number);
  if (absolute >= 100000000) return (number / 100000000).toFixed(2) + "亿";
  if (absolute >= 10000) return (number / 10000).toFixed(2) + "万";
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

const CHART_PALETTES = {
  bar: ["#5bc8f5", "#f0c078", "#3fd2c4", "#f28aa6", "#9d9bf0", "#7fd6a8"],
  horizontalBar: ["#5bc8f5", "#3fd2c4", "#f0c078", "#f28aa6", "#9d9bf0", "#7fd6a8"],
  stackedBar: ["#3fb8e6", "#3fd2c4", "#e6b063", "#8f8ce8", "#e87f9c", "#6fc998"],
  histogram: ["#5bc8f5", "#9d9bf0", "#f28aa6", "#3fd2c4", "#f0c078", "#7fd6a8"],
  line: ["#3fd2c4", "#9d9bf0", "#5bc8f5", "#f0c078", "#f28aa6", "#7fd6a8"],
  area: ["#3fd2c4", "#5bc8f5", "#9d9bf0", "#f0c078", "#f28aa6", "#7fd6a8"],
  pie: ["#5bc8f5", "#3fd2c4", "#f0c078", "#9d9bf0", "#f28aa6", "#7fd6a8", "#e08a8a", "#8fb3d9"],
  radar: ["#3fd2c4", "#5bc8f5", "#f0c078", "#f28aa6", "#9d9bf0", "#7fd6a8"],
  scatter: ["#5bc8f5", "#f28aa6", "#3fd2c4", "#f0c078", "#9d9bf0", "#7fd6a8"],
  dualAxis: ["#5bc8f5", "#f28aa6", "#3fd2c4", "#f0c078"],
  default: ["#5bc8f5", "#3fd2c4", "#f0c078", "#9d9bf0", "#f28aa6", "#7fd6a8"]
};

function chartPalette(type) {
  return CHART_PALETTES[type] || CHART_PALETTES.default;
}

function hexToRgba(hex, alpha = 0.26) {
  const value = String(hex || "").replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(value)) return `rgba(45,212,191,${alpha})`;
  const number = parseInt(value, 16);
  return `rgba(${(number >> 16) & 255},${(number >> 8) & 255},${number & 255},${alpha})`;
}

function prepareChartData(chart) {
  const data = Array.isArray(chart.data) ? chart.data : [];
  if (!data.length) return null;
  const keys = Object.keys(data[0] || {});
  const xKey = keys.includes(chart.x) ? chart.x : keys[0];
  if (!xKey) return null;
  const type = chart.type || "bar";
  const requestedY = type === "stackedBar" || Array.isArray(chart.y)
    ? (Array.isArray(chart.y) ? chart.y : [chart.y])
    : [chart.y];
  let yKeys = [...new Set(requestedY.map(item => String(item || "")).filter(key => key && keys.includes(key)))];
  if (!yKeys.length) {
    const fallback = keys.find(key => data.some(row => Number.isFinite(Number(row[key]))));
    if (fallback && fallback !== xKey) yKeys = [fallback];
  }
  if (!yKeys.length) return null;
  const metaList = Array.isArray(chart.series) ? chart.series : [];
  const metaFor = key => metaList.find(item => String(item.key || item.label || "") === key) || null;
  const magnitudes = value => data
    .map(row => Math.abs(Number(row && row[value])))
    .filter(value => Number.isFinite(value));
  const isRateKey = key => {
    const meta = metaFor(key);
    if (meta && meta.unit) return /%|百分|比率/.test(String(meta.unit));
    return /率|同比|环比|增长|毛利|净利率|占比|百分/.test(String(key));
  };
  const labels = data.map(row => String(row[xKey] ?? ""));
  const datasets = yKeys.map(key => {
    const meta = metaFor(key);
    const values = data.map(row => {
      if (row[key] === null || row[key] === undefined || row[key] === "") return null;
      const value = Number(row[key]);
      return Number.isFinite(value) ? value : null;
    });
    const raw = magnitudes(key);
    const peak = raw.length ? Math.max(...raw) : 0;
    let unit = meta ? String(meta.unit || "") : "";
    let scale = Number(meta && meta.scale);
    if (!Number.isFinite(scale) || Math.abs(scale) < 1e-9) scale = 1;
    if (!unit) {
      if (isRateKey(key)) { unit = "%"; scale = 1; }
      else if (/股|股本|股数/.test(String(key))) unit = "股";
      else if (peak >= 1e8) unit = "亿元";
      else if (peak >= 1e4) unit = "万元";
      else if (peak > 0) unit = "元";
    }
    return {
      label: (meta && meta.label) || key,
      key,
      unit,
      scale,
      rate: isRateKey(key),
      axis: meta && meta.axis ? String(meta.axis) : "",
      peak,
      values
    };
  }).filter(dataset => dataset.values.some(value => Number.isFinite(value)));
  const validCount = datasets.reduce(
    (total, dataset) => total + dataset.values.filter(value => Number.isFinite(value)).length,
    0
  );
  if (labels.length < 2 || !datasets.length || validCount < 2) return null;
  let dual = false;
  if (!datasets.some(dataset => dataset.axis)) {
    const amountPeak = Math.max(...datasets.filter(d => !d.rate).map(d => d.peak), 0);
    const ratePeak = Math.max(...datasets.filter(d => d.rate).map(d => d.peak), 0);
    dual = datasets.length >= 2 && amountPeak > 0 && ratePeak > 0 && ratePeak <= 100 &&
      (amountPeak / Math.max(ratePeak, 1e-9) >= 20 || type === "dualAxis");
  }
  datasets.forEach(dataset => {
    if (!dataset.axis) dataset.axis = dual && dataset.rate ? "y1" : "y";
  });
  const units = [...new Set(datasets.map(dataset => dataset.unit).filter(Boolean))];
  return {
    labels,
    datasets,
    yLabel: datasets.map(dataset => dataset.label).join(" · "),
    unitLabel: units.join(" / "),
    xTitle: chart.x_title || "",
    source: chart.source || "",
    subtitle: chart.subtitle || "",
    dual,
    type
  };
}

function chartUnitSuffix(unit) {
  const text = String(unit || "").trim();
  if (!text || text === "%") return "";
  return " " + text;
}

function formatChartValue(value, dataset) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const scale = Math.abs(Number(dataset && dataset.scale)) >= 1e-9 ? Number(dataset.scale) : 1;
  const shown = number / scale;
  if (dataset && dataset.rate) {
    const abs = Math.abs(shown);
    return (abs >= 100 ? shown.toFixed(0) : abs >= 10 ? shown.toFixed(1) : shown.toFixed(2)).replace(/\.?0+$/, "") + "%";
  }
  if (Math.abs(shown) >= 10000) return chartNumber(shown) + chartUnitSuffix(dataset && dataset.unit);
  const text = shown.toLocaleString("zh-CN", { maximumFractionDigits: Math.abs(shown) >= 1000 ? 1 : 2 });
  return text + chartUnitSuffix(dataset && dataset.unit);
}

function formatChartPercent(value) {
  const number = Number(value) || 0;
  if (number > 0 && number < 1) return number.toFixed(2);
  if (number < 10) return number.toFixed(1).replace(/\.0$/, "");
  return number.toFixed(1).replace(/\.0$/, "");
}

const pieCalloutPlugin = {
  id: "pieCalloutLabels",
  afterDatasetsDraw(chart) {
    if (chart.config.type !== "pie") return;
    const meta = chart.getDatasetMeta(0);
    const values = chart.data.datasets[0]?.data || [];
    const total = values.reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0);
    if (!total || !meta?.data?.length) return;
    const ctx = chart.ctx;
    const area = chart.chartArea;
    const expanded = Boolean(chart.options.plugins?.pieCallout?.expanded);
    const outsideThreshold = expanded ? 10 : 6;
    const callouts = [];

    meta.data.forEach((arc, index) => {
      const value = Math.max(0, Number(values[index]) || 0);
      const percent = value / total * 100;
      const angle = (arc.startAngle + arc.endAngle) / 2;
      const centerX = (area.left + area.right) / 2;
      const centerY = (area.top + area.bottom) / 2;
      const edgeX = centerX + Math.cos(angle) * arc.outerRadius;
      const edgeY = centerY + Math.sin(angle) * arc.outerRadius;

      if (percent >= outsideThreshold) {
        if (percent < 3.5) return;
        const radius = arc.outerRadius * 0.62;
        const x = centerX + Math.cos(angle) * radius;
        const y = centerY + Math.sin(angle) * radius;
        const label = `${formatChartPercent(percent)}%`;
        ctx.save();
        ctx.font = `700 ${expanded ? 13 : 12}px Microsoft YaHei`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.lineWidth = 3;
        ctx.strokeStyle = "rgba(8,16,24,.74)";
        ctx.strokeText(label, x, y);
        ctx.fillStyle = "#ffffff";
        ctx.fillText(label, x, y);
        ctx.restore();
        return;
      }

      const side = Math.cos(angle) >= 0 ? 1 : -1;
      callouts.push({
        index,
        label: String(chart.data.labels[index] ?? ""),
        percent,
        edgeX,
        edgeY,
        side,
        endX: side > 0 ? area.right - 6 : area.left + 6,
        y: edgeY
      });
    });

    callouts.sort((left, right) => left.y - right.y);
    const gap = expanded ? 20 : 17;
    callouts.forEach((item, index) => {
      if (index > 0 && item.y < callouts[index - 1].y + gap) {
        item.y = callouts[index - 1].y + gap;
      }
      item.y = Math.max(area.top + 10, Math.min(area.bottom - 10, item.y));
    });
    for (let index = callouts.length - 2; index >= 0; index -= 1) {
      if (callouts[index].y > callouts[index + 1].y - gap) {
        callouts[index].y = callouts[index + 1].y - gap;
      }
    }

    callouts.forEach(item => {
      const elbowX = item.endX - item.side * (expanded ? 34 : 28);
      const textX = item.endX - item.side * 7;
      ctx.save();
      ctx.strokeStyle = "rgba(178,198,214,.7)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(item.edgeX, item.edgeY);
      ctx.lineTo(elbowX, item.y);
      ctx.lineTo(textX, item.y);
      ctx.stroke();
      const text = `${item.label.slice(0, 11)} ${formatChartPercent(item.percent)}%`;
      ctx.font = `600 ${expanded ? 13 : 11}px Microsoft YaHei`;
      ctx.textAlign = item.side > 0 ? "right" : "left";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(8,16,24,.9)";
      ctx.strokeText(text, textX, item.y);
      ctx.fillStyle = "#dce7ef";
      ctx.fillText(text, textX, item.y);
      ctx.restore();
    });
  }
};

/* 柱顶数值：只在柱类图上把数值标在柱头，负值写到零轴下方，点太多时自动降级只标最高的几个 */
const barValueLabelsPlugin = {
  id: "finsightBarValues",
  afterDatasetsDraw(chart) {
    const cfg = chart.options.plugins.finsightBarValues;
    if (!cfg || !cfg.enabled) return;
    const horizontal = cfg.horizontal;
    const stacked = Boolean(chart.options.scales?.x?.stacked && horizontal) ||
      Boolean(chart.options.scales?.y?.stacked && !horizontal);
    const metas = chart.data.datasets
      .map((dataset, index) => ({ dataset, index, meta: chart.getDatasetMeta(index) }))
      .filter(entry => entry.meta.type === "bar" && !entry.meta.hidden);
    const cells = [];
    metas.forEach(entry => {
      entry.meta.data.forEach((element, dataIndex) => {
        const value = Number(entry.dataset.data[dataIndex]);
        if (!Number.isFinite(value)) return;
        cells.push({ element, value, dataset: entry.dataset, dataIndex });
      });
    });
    if (!cells.length) return;
    let show = cells;
    const cap = stacked ? 24 : 34;
    if (cells.length > cap) {
      const top = Math.ceil(cap / Math.max(metas.length, 1));
      show = [];
      metas.forEach(entry => {
        const picked = entry.meta.data
          .map((element, dataIndex) => ({ element, dataIndex, value: Number(entry.dataset.data[dataIndex]) }))
          .filter(item => Number.isFinite(item.value))
          .sort((left, right) => Math.abs(right.value) - Math.abs(left.value))
          .slice(0, top);
        picked.forEach(item => show.push({ element: item.element, value: item.value, dataset: entry.dataset, dataIndex: item.dataIndex }));
      });
    }
    if (!show.length) return;
    const ctx = chart.ctx;
    const area = chart.chartArea;
    const fontPx = chart.$chartPreview ? 13 : 11;
    ctx.save();
    ctx.font = `600 ${fontPx}px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif`;
    ctx.textBaseline = "middle";
    show.forEach(item => {
      const el = item.element;
      const negative = item.value < 0;
      let x;
      let y;
      let align;
      let text;
      if (horizontal) {
        text = formatChartValue(item.value, item.dataset);
        if (negative) {
          x = el.base + 6; y = el.y; align = "right";
        } else {
          x = el.x + 6; y = el.y; align = "left";
        }
        if (x > area.right - 4) { x = el.x - 6; align = "right"; }
        if (x < area.left + 4) { x = el.base - 6; align = "left"; }
      } else {
        text = formatChartValue(item.value, item.dataset);
        x = el.x;
        y = negative ? el.base + fontPx * 0.85 : el.y - fontPx * 0.85;
        align = "center";
      }
      ctx.textAlign = align;
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(6,13,21,.82)";
      ctx.strokeText(text, x, y);
      ctx.fillStyle = negative ? "rgba(255,150,168,.95)" : "rgba(232,242,250,.96)";
      ctx.fillText(text, x, y);
    });
    ctx.restore();
  }
};

/* 圆环中心：合计 */
const centerTotalPlugin = {
  id: "finsightCenterTotal",
  afterDraw(chart) {
    const cfg = chart.options.plugins.finsightCenterTotal;
    if (!cfg || !cfg.enabled) return;
    const values = chart.data.datasets[0]?.data || [];
    const total = values.reduce((sum, value) => sum + Math.abs(Number(value) || 0), 0);
    if (!total) return;
    const ctx = chart.ctx;
    const area = chart.chartArea;
    const cx = (area.left + area.right) / 2;
    const cy = (area.top + area.bottom) / 2;
    const first = chart.data.datasets[0] || {};
    const dataset = cfg.dataset || {};
    const main = (Number(first.rawScale) || 1) === 100 ? `${total.toFixed(0)}%` : formatChartValue(total, dataset);
    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = `700 ${chart.$chartPreview ? 22 : 17}px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif`;
    ctx.fillStyle = "#eef4f9";
    ctx.fillText(main, cx, cy - 7);
    ctx.font = `500 ${chart.$chartPreview ? 12 : 10.5}px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif`;
    ctx.fillStyle = "rgba(150,172,192,.9)";
    ctx.fillText(cfg.label || "合计", cx, cy + 14);
    ctx.restore();
  }
};

function chartTipElement(chart) {
  const parent = chart.canvas.parentNode;
  if (!parent) return null;
  let tip = parent.querySelector(".chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    parent.appendChild(tip);
  }
  return tip;
}

/* 自定义玻璃 tooltip：鼠标停在图上才出现
   Chart.js 的 external 回调只传**一个** context 对象 {chart, tooltip}，
   但回归件里是按 (chart, tooltip) 两参直接调的 ⇒ 两种调用形态都要认。 */
function renderGlassTooltip(chartOrContext, maybeTooltip) {
  const isContext = Boolean(chartOrContext && chartOrContext.tooltip);
  const chart = isContext ? chartOrContext.chart : chartOrContext;
  const tooltip = isContext ? chartOrContext.tooltip : maybeTooltip;
  if (!chart || !chart.canvas) return;
  const tip = chartTipElement(chart);
  if (!tip) return;
  if (!tooltip || tooltip.opacity === 0) {
    tip.style.opacity = "0";
    tip.style.transform = "translate3d(0,0,0)";
    return;
  }
  const prepared = chart.$prepared || null;
  const title = (tooltip.title || []).join(" ") || "—";
  const rows = tooltip.dataPoints.map(point => {
    const dataset = (prepared && prepared.datasets && prepared.datasets[point.datasetIndex]) || chart.data.datasets[point.datasetIndex] || {};
    const color = Array.isArray(chart.data.datasets[point.datasetIndex]?.backgroundColor)
      ? chart.data.datasets[point.datasetIndex].backgroundColor[point.dataIndex]
      : (chart.data.datasets[point.datasetIndex]?.borderColor || "#5bc8f5");
    const label = dataset.label || point.label || "数值";
    const raw = Number(point.raw);
    let value;
    if (Number.isFinite(raw)) {
      value = formatChartValue(raw, dataset);
      if (chart.config.type === "pie" && prepared) {
        const total = chart.data.datasets[point.datasetIndex].data.reduce(
          (sum, item) => sum + Math.abs(Number(item) || 0), 0);
        if (total) value += ` (${formatChartPercent(raw / total * 100)}%)`;
      }
    } else {
      value = String(point.formattedValue || "—");
    }
    return `<div class="r"><i style="background:${color}"></i>${escapeHtml(String(label))}<b>${escapeHtml(value)}</b></div>`;
  }).join("");
  tip.innerHTML = `<div class="h">${escapeHtml(title)}</div>${rows}`;
  tip.style.opacity = "1";
  const area = chart.chartArea || { left: 0, right: 1, top: 0, bottom: 1 };
  const width = tip.offsetWidth || 140;
  const height = tip.offsetHeight || 46;
  let left = tooltip.caretX + 14;
  if (left + width > area.right + 6) left = tooltip.caretX - width - 14;
  if (left < area.left) left = area.left;
  let top = tooltip.caretY - height / 2;
  top = Math.max(area.top, Math.min(area.bottom - height, top));
  tip.style.transform = `translate3d(${Math.round(left)}px, ${Math.round(top)}px, 0)`;
}

function chartGradientFill(ctx, color, kind) {
  const area = ctx.chart.chartArea;
  const chartType = ctx.chart.config?.type;
  const horizontal = chartType === "bar" && ctx.chart.options?.indexAxis === "y";
  const alphaTop = kind === "line" ? 0.40 : kind === "barHover" ? 1.0 : 0.96;
  const alphaBottom = kind === "line" ? 0.02 : kind === "barHover" ? 0.52 : 0.34;
  if (!area) return hexToRgba(color, alphaTop);
  const from = horizontal ? area.left : area.top;
  const to = horizontal ? area.right : area.bottom;
  const gradient = ctx.chart.ctx.createLinearGradient(from, 0, to, 0);
  if (horizontal && kind !== "line") {
    gradient.addColorStop(0, hexToRgba(color, alphaBottom));
    gradient.addColorStop(1, hexToRgba(color, alphaTop));
  } else {
    gradient.addColorStop(0, hexToRgba(color, alphaTop));
    gradient.addColorStop(1, hexToRgba(color, alphaBottom));
  }
  return gradient;
}

/* 图表外壳：标题 / 单位 / 来源 / 图例都是真实 DOM，画布只画图形 */
function chartCardHtml(chartId, chart) {
  const meta = chart && typeof chart === "object" ? chart : {};
  const title = meta.title || "数据图表";
  const subtitle = meta.subtitle ? `<span class="cu">${escapeHtml(meta.subtitle)}</span>` : "";
  const unit = meta.unit || (Array.isArray(meta.series) ? meta.series.map(item => item.unit).filter(Boolean).join(" / ") : "");
  const unitHtml = unit ? `<span class="cu">单位 ${escapeHtml(unit)}</span>` : "";
  const source = meta.source ? `<span class="csrc">${escapeHtml(meta.source)}</span>` : "";
  return `<div class="chart-shell">` +
    `<div class="chart-head"><span class="ct">${escapeHtml(title)}</span>${unitHtml}${subtitle}${source}</div>` +
    `<div class="chart-body"><canvas class="chart-canvas" id="${chartId}"></canvas></div>` +
    `<div class="chart-legend" id="${chartId}_legend"></div>` +
    `</div>`;
}

function hydrateChartShell(chart, options = {}) {
  const prepared = chart?.$prepared;
  const legendHost = document.getElementById(options.legendId || "chartCardLegend");
  if (!legendHost) return;
  legendHost.innerHTML = "";
  if (!prepared || prepared.type === "pie") return;
  if (prepared.datasets.length < 2 && prepared.type !== "scatter") return;
  prepared.datasets.forEach((dataset, index) => {
    const button = document.createElement("button");
    const color = chartPalette(prepared.type)[index % chartPalette(prepared.type).length];
    button.innerHTML = `<i style="background:${color}"></i>${escapeHtml(dataset.label)}` +
      (dataset.axis === "y1" ? `<em>右轴</em>` : "");
    button.onclick = () => {
      const instance = options.instance ? options.instance() : null;
      if (!instance) return;
      const next = !instance.isDatasetVisible(index);
      instance.setDatasetVisibility(index, next);
      button.classList.toggle("off", !next);
      instance.update();
    };
    legendHost.appendChild(button);
  });
}

function drawChartOnCanvas(canvas, chart, renderOptions = {}) {
  const prepared = prepareChartData(chart);
  if (!prepared) return false;
  if (canvas._chartInstance) {
    canvas._chartInstance.destroy();
    canvas._chartInstance = null;
  }
  const preview = Boolean(renderOptions.preview);
  if (preview) canvas.style.height = "100%";
  canvas._chartData = chart;
  canvas.$chartPreview = preview;

  if (window.Chart) {
    const type = prepared.type;
    const isHorizontal = type === "horizontalBar";
    const isStacked = type === "stackedBar";
    const isArea = type === "area";
    const isPie = type === "pie";
    const isRadar = type === "radar";
    const isHistogram = type === "histogram";
    const isDualAxis = type === "dualAxis";
    // 散点只有在 x 全是数值时才是真散点，否则退回分类轴，别画成一竖列。
    const scatterNumeric = type === "scatter" &&
      prepared.labels.every(label => label !== "" && Number.isFinite(Number(label)));
    const chartType = isPie || isRadar
      ? type
      : scatterNumeric
        ? "scatter"
        : (type === "line" || isArea || isDualAxis || type === "scatter") ? "line" : "bar";
    const colors = chartPalette(type);
    const niceStep = (span, count) => {
      if (!(span > 0) || !(count > 0)) return 1;
      const rough = span / count;
      const power = Math.pow(10, Math.floor(Math.log10(rough)));
      const base = rough / power;
      // 往下取整到 1 / 2 / 2.5 / 5 / 10，宁可多给刻度，也不要「只有两三个数」
      const nice = base >= 7.5 ? 10 : base >= 3.75 ? 5 : base >= 2.25 ? 2.5 : base >= 1.5 ? 2 : 1;
      return nice * power;
    };
    const axisTicks = axisKey => ({
      // 刻度走整数步长，避免出现 75.4 这种读数。
      afterBuildTicks: scale => {
        const min = Number(scale.min);
        const max = Number(scale.max);
        if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return;
        const step = niceStep(max - min, preview ? 9 : 8);
        const low = Math.floor(min / step) * step;
        const high = Math.ceil(max / step) * step;
        const ticks = [];
        for (let value = low; value <= high + step * 1e-6; value += step) {
          ticks.push({ value: Math.abs(value) < step * 1e-6 ? 0 : Number(value.toFixed(10)) });
        }
        scale.ticks = ticks;
      },
      callback: value => formatChartValue(value, prepared.datasets.find(dataset => dataset.axis === axisKey) || prepared.datasets[0])
    });
    const datasets = prepared.datasets.map((dataset, index) => {
      const color = colors[index % colors.length];
      const axis = dataset.axis === "y1" ? "y1" : "y";
      if (chartType === "pie") {
        return {
          label: dataset.label,
          data: dataset.values.map(value => (Number.isFinite(value) ? Math.abs(value) : 0)),
          backgroundColor: prepared.labels.map((_, slot) => colors[slot % colors.length]),
          borderColor: "rgba(7,15,25,.85)",
          borderWidth: 2,
          hoverOffset: 10,
          rawScale: dataset.scale,
          unit: dataset.unit
        };
      }
      if (chartType === "scatter") {
        return {
          label: dataset.label,
          yAxisID: axis,
          data: prepared.labels.map((label, slot) => ({
            x: Number(label),
            y: dataset.values[slot] ?? null
          })).filter(point => Number.isFinite(point.y)),
          backgroundColor: hexToRgba(color, 0.82),
          borderColor: "rgba(7,15,25,.6)",
          borderWidth: 1,
          pointRadius: preview ? 6 : 5,
          pointHoverRadius: preview ? 9 : 8
        };
      }
      if (chartType === "line") {
        const showFill = isArea || (isDualAxis && index === 0) || (prepared.datasets.length === 1 && !isRadar);
        return {
          type: isDualAxis && index > 0 ? "line" : (isRadar ? "radar" : "line"),
          label: dataset.label,
          yAxisID: axis,
          data: dataset.values,
          borderColor: color,
          backgroundColor: showFill ? (context => chartGradientFill(context, color, "line")) : color,
          fill: isRadar ? true : showFill,
          borderWidth: preview ? 2.6 : 2.2,
          tension: 0.34,
          cubicInterpolationMode: "monotone",
          pointRadius: 0,
          pointHoverRadius: preview ? 6 : 5,
          pointBackgroundColor: color,
          pointBorderColor: "rgba(7,15,25,.9)",
          pointBorderWidth: 2,
          pointHitRadius: 18,
          ...dataset
        };
      }
      if (chartType === "bar") {
        return {
          label: dataset.label,
          yAxisID: axis,
          data: dataset.values,
          backgroundColor: context => chartGradientFill(context, color, "bar"),
          hoverBackgroundColor: context => chartGradientFill(context, color, "barHover"),
          hoverBorderColor: "rgba(236,246,255,.9)",
          hoverBorderWidth: { top: 2, right: 1.2, bottom: 1.2, left: 1.2 },
          borderColor: hexToRgba(color, 0.95),
          borderWidth: { top: 1.4, right: 0, bottom: 0, left: 0 },
          borderRadius: isHorizontal
            ? { topLeft: 0, topRight: 6, bottomLeft: 0, bottomRight: 6 }
            : { topLeft: 6, topRight: 6, bottomLeft: 2, bottomRight: 2 },
          borderSkipped: false,
          categoryPercentage: 0.82,
          barPercentage: isHistogram ? 0.98 : 0.78,
          maxBarThickness: isHistogram ? 56 : 30,
          ...dataset
        };
      }
      return {
        label: dataset.label,
        yAxisID: axis,
        data: dataset.values,
        borderColor: color,
        backgroundColor: hexToRgba(color, 0.24),
        borderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 6,
        ...dataset
      };
    });
    const gridColor = "rgba(255,255,255,.055)";
    const tickColor = "rgba(196,214,230,.62)";
    const axisFont = { size: preview ? 12 : 11, family: '-apple-system, "Segoe UI", "Microsoft YaHei", sans-serif' };
    const categoryScale = {
      type: "category",
      stacked: isStacked,
      grid: { display: false, drawTicks: false },
      border: { display: false },
      offset: chartType === "bar",
      ticks: {
        color: tickColor,
        font: axisFont,
        maxRotation: chartType === "bar" && prepared.labels.length > 6 ? 32 : 0,
        autoSkip: true,
        autoSkipPadding: 14,
        padding: 8
      },
      title: prepared.xTitle ? {
        display: true,
        text: prepared.xTitle,
        color: "rgba(150,172,192,.85)",
        font: { size: preview ? 12 : 11, weight: "500", family: axisFont.family }
      } : { display: false }
    };
    const linearCategory = scatterNumeric ? { type: "linear", ticks: { callback: value => chartNumber(value), color: tickColor, font: axisFont } } : {};
    const valueScale = extra => Object.assign({
      beginAtZero: true,
      stacked: isStacked,
      grid: { color: gridColor, drawTicks: false, lineWidth: 1 },
      border: { display: false, dash: [3, 5] },
      ticks: Object.assign({ color: tickColor, font: axisFont, padding: 10, maxTicksLimit: 12, autoSkip: false }, extra || {}),
    }, axisTicks("y"));
    const rightScale = Object.assign({
      position: "right",
      beginAtZero: true,
      grid: { drawOnChartArea: false, drawTicks: false },
      border: { display: false },
      ticks: Object.assign({ color: tickColor, font: axisFont, padding: 10, maxTicksLimit: 6 }, axisTicks("y1")),
    }, {});
    const scales = {};
    if (chartType === "pie" || chartType === "radar") {
      scales.r = {
        grid: { color: "rgba(255,255,255,.07)" },
        angleLines: { color: "rgba(255,255,255,.07)" },
        pointLabels: { color: "rgba(214,228,240,.85)", font: { size: preview ? 13 : 11.5, family: axisFont.family } },
        ticks: { display: false, showLabelBackdrop: false }
      };
    } else {
      scales.x = Object.assign({}, categoryScale, chartType === "bar" ? {} : linearCategory);
      scales.y = valueScale();
      if (prepared.dual) scales.y1 = rightScale;
    }
    const instance = new Chart(canvas, {
      type: chartType,
      data: { labels: prepared.labels, datasets },
      options: {
        responsive: true,
        // 位图必须吃满 devicePixelRatio，CSS 拉伸是「模糊劣质感」的真凶。
        devicePixelRatio: Math.max(window.devicePixelRatio || 1, preview ? 2 : 1.5),
        maintainAspectRatio: false,
        animation: { duration: preview ? 700 : 520, easing: "easeOutQuart" },
        layout: { padding: { top: 26, right: preview ? 18 : 10, bottom: 4, left: 2 } },
        interaction: { mode: isPie ? "nearest" : "index", intersect: false },
        plugins: {
          title: preview ? {
            display: Boolean(chart.title),
            text: chart.title || "",
            color: "#eaf2f8",
            padding: { top: 4, bottom: 14 },
            font: { size: 15, weight: "600", family: axisFont.family }
          } : { display: false },
          legend: { display: false },
          tooltip: { enabled: false, external: renderGlassTooltip },
          finsightBarValues: {
            enabled: chartType === "bar" && !isStacked,
            horizontal: isHorizontal
          },
          finsightCenterTotal: {
            enabled: isPie,
            label: prepared.unitLabel ? `合计（${prepared.unitLabel}）` : "合计",
            dataset: prepared.datasets[0]
          },
          pieCallout: { expanded: preview }
        },
        scales
      }
    });
    instance.$prepared = prepared;
    canvas._chartInstance = instance;
    canvas.$chartInstance = instance;
    return true;
  }
  const width = 1200;
  const height = 480;
  const ratio = renderOptions.preview
    ? Math.max(4, window.devicePixelRatio || 1)
    : Math.max(3, window.devicePixelRatio || 1);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.width = "100%";
  canvas.style.height = renderOptions.preview ? "100%" : "420px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.fillStyle = "#151e27";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#e6edf3";
  ctx.font = "600 18px Microsoft YaHei";
  ctx.fillText(chart.title || "数据图表", 48, 34);

  const labels = prepared.labels;
  const datasets = prepared.datasets;
  const colors = chartPalette(prepared.type);
  const finite = datasets.flatMap(dataset => dataset.values).filter(value => Number.isFinite(value));
  if (!finite.length) return false;

  // 饼图：arc 扇区 + 图例
  if (prepared.type === "pie") {
    const values = datasets[0].values.map(value => Number.isFinite(value) ? Math.abs(value) : 0);
    const total = values.reduce((sum, value) => sum + value, 0);
    if (!total) return false;
    let angle = -Math.PI / 2;
    const cx = 250;
    const cy = 195;
    const radius = 112;
    values.forEach((value, index) => {
      const slice = value / total * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, angle, angle + slice);
      ctx.closePath();
      ctx.fillStyle = colors[index % colors.length];
      ctx.fill();
      ctx.strokeStyle = "#151e27";
      ctx.lineWidth = 2;
      ctx.stroke();
      angle += slice;
    });
    ctx.font = "13px Microsoft YaHei";
    labels.forEach((label, index) => {
      const y = 108 + index * 26;
      ctx.fillStyle = colors[index % colors.length];
      ctx.fillRect(445, y - 11, 12, 12);
      ctx.fillStyle = "#e6edf3";
      ctx.fillText(`${label}  ${chartNumber(values[index])} (${(values[index] / total * 100).toFixed(1)}%)`, 468, y);
    });
    return true;
  }

  // 雷达图：多边形网格 + 顶点连线 + 半透明填充
  if (prepared.type === "radar") {
    if (labels.length < 3) return drawChartOnCanvas(canvas, { ...chart, type: "bar" });
    const cx = 450;
    const cy = 198;
    const radius = 118;
    const maxValue = Math.max(...finite.map(value => Math.abs(value)), 1);
    const angleAt = index => -Math.PI / 2 + index * Math.PI * 2 / labels.length;
    for (let level = 1; level <= 4; level++) {
      ctx.beginPath();
      labels.forEach((_, index) => {
        const pointRadius = radius * level / 4;
        const x = cx + Math.cos(angleAt(index)) * pointRadius;
        const y = cy + Math.sin(angleAt(index)) * pointRadius;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.strokeStyle = "#293744";
      ctx.stroke();
    }
    labels.forEach((label, index) => {
      const x = cx + Math.cos(angleAt(index)) * (radius + 24);
      const y = cy + Math.sin(angleAt(index)) * (radius + 24);
      ctx.fillStyle = "#98a7b5";
      ctx.font = "11px Microsoft YaHei";
      ctx.textAlign = Math.abs(x - cx) < 5 ? "center" : x > cx ? "left" : "right";
      ctx.fillText(label, x, y + 4);
    });
    datasets.forEach((dataset, datasetIndex) => {
      ctx.beginPath();
      labels.forEach((_, index) => {
        const value = Number.isFinite(dataset.values[index]) ? Math.abs(dataset.values[index]) : 0;
        const pointRadius = radius * value / maxValue;
        const x = cx + Math.cos(angleAt(index)) * pointRadius;
        const y = cy + Math.sin(angleAt(index)) * pointRadius;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      const color = colors[datasetIndex % colors.length];
      ctx.fillStyle = hexToRgba(color, 0.22);
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.fill();
      ctx.stroke();
    });
    return true;
  }

  // 水平柱状图：x 轴为数值，y 轴为类别
  if (prepared.type === "horizontalBar") {
    const left = 104;
    const right = 34;
    const top = 66;
    const bottom = 42;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    let maxValue = Math.max(...finite, 0);
    let minValue = Math.min(...finite, 0);
    if (maxValue === minValue) maxValue = maxValue >= 0 ? maxValue * 1.2 + 1 : 1;
    else {
      const padding = (maxValue - minValue) * 0.1;
      maxValue += padding;
      minValue -= padding;
    }
    const xFor = value => left + (value - minValue) / (maxValue - minValue) * plotWidth;
    const baseline = xFor(0);
    ctx.font = "11px Microsoft YaHei";
    for (let i = 0; i <= 4; i++) {
      const value = minValue + (maxValue - minValue) * i / 4;
      const x = xFor(value);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, height - bottom);
      ctx.strokeStyle = "#293744";
      ctx.stroke();
      ctx.fillStyle = "#98a7b5";
      ctx.textAlign = "center";
      ctx.fillText(chartNumber(value), x, height - 18);
    }
    const slot = plotHeight / labels.length;
    const groupHeight = Math.min(46, slot * 0.65);
    const barHeight = groupHeight / datasets.length;
    labels.forEach((label, labelIndex) => {
      const centerY = top + slot * labelIndex + slot / 2;
      datasets.forEach((dataset, datasetIndex) => {
        const value = dataset.values[labelIndex];
        if (!Number.isFinite(value)) return;
        const y = centerY - groupHeight / 2 + datasetIndex * barHeight;
        const x = xFor(value);
        ctx.fillStyle = colors[datasetIndex % colors.length];
        ctx.fillRect(Math.min(x, baseline), y + 2, Math.max(2, Math.abs(x - baseline)), Math.max(2, barHeight - 4));
      });
      ctx.fillStyle = "#b8c4cf";
      ctx.textAlign = "right";
      ctx.fillText(label, left - 10, centerY + 4);
    });
    return true;
  }

  const left = 72;
  const right = 34;
  const top = 66;
  const bottom = 58;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;

  // 堆叠柱状图：同一 x 位置按多个 y 字段分段堆叠
  if (prepared.type === "stackedBar") {
    const totals = labels.map((_, labelIndex) => {
      let positive = 0;
      let negative = 0;
      datasets.forEach(dataset => {
        const value = dataset.values[labelIndex];
        if (!Number.isFinite(value)) return;
        if (value >= 0) positive += value;
        else negative += value;
      });
      return { positive, negative };
    });
    let maxValue = Math.max(0, ...totals.map(item => item.positive));
    let minValue = Math.min(0, ...totals.map(item => item.negative));
    if (maxValue === minValue) maxValue = maxValue >= 0 ? maxValue * 1.2 + 1 : 1;
    else {
      const padding = (maxValue - minValue) * 0.1;
      maxValue += padding;
      minValue -= padding;
    }
    const yFor = value => top + (maxValue - value) / (maxValue - minValue) * plotHeight;
    drawCanvasGrid(ctx, left, top, plotWidth, plotHeight, minValue, maxValue, yFor);
    const slot = plotWidth / labels.length;
    const barWidth = Math.min(42, slot * 0.5);
    labels.forEach((label, labelIndex) => {
      const x = left + slot * labelIndex + slot / 2 - barWidth / 2;
      let positive = 0;
      let negative = 0;
      datasets.forEach((dataset, datasetIndex) => {
        const value = dataset.values[labelIndex];
        if (!Number.isFinite(value)) return;
        const start = value >= 0 ? positive : negative;
        const end = start + value;
        if (value >= 0) positive = end;
        else negative = end;
        const y1 = yFor(start);
        const y2 = yFor(end);
        ctx.fillStyle = colors[datasetIndex % colors.length];
        ctx.fillRect(x, Math.min(y1, y2), barWidth, Math.max(2, Math.abs(y2 - y1)));
      });
      ctx.fillStyle = "#b8c4cf";
      ctx.font = "11px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText(label, x + barWidth / 2, height - 28);
    });
    return true;
  }

  // 折线图与面积图：折线 + 数据点，面积图额外填充折线下方
  if (prepared.type === "line" || prepared.type === "area") {
    let maxValue = Math.max(...finite, 0);
    let minValue = Math.min(...finite, 0);
    if (maxValue === minValue) maxValue = maxValue >= 0 ? maxValue * 1.2 + 1 : 1;
    else {
      const padding = (maxValue - minValue) * 0.12;
      maxValue += padding;
      minValue -= padding;
    }
    const yFor = value => top + (maxValue - value) / (maxValue - minValue) * plotHeight;
    const baseline = yFor(0);
    drawCanvasGrid(ctx, left, top, plotWidth, plotHeight, minValue, maxValue, yFor);
    datasets.forEach((dataset, datasetIndex) => {
      const color = colors[datasetIndex % colors.length];
      const points = dataset.values
        .map((value, index) => ({ value, index }))
        .filter(item => Number.isFinite(item.value));
      if (!points.length) return;
      if (prepared.type === "area") {
        ctx.beginPath();
        ctx.moveTo(left + plotWidth * points[0].index / Math.max(1, labels.length - 1), baseline);
        points.forEach(item => {
          const x = left + plotWidth * item.index / Math.max(1, labels.length - 1);
          ctx.lineTo(x, yFor(item.value));
        });
        ctx.lineTo(left + plotWidth * points[points.length - 1].index / Math.max(1, labels.length - 1), baseline);
        ctx.closePath();
        ctx.fillStyle = hexToRgba(color, 0.2);
        ctx.fill();
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      points.forEach((item, index) => {
        const x = left + plotWidth * item.index / Math.max(1, labels.length - 1);
        const y = yFor(item.value);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      points.forEach(item => {
        const x = left + plotWidth * item.index / Math.max(1, labels.length - 1);
        const y = yFor(item.value);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      });
    });
    labels.forEach((label, index) => {
      const x = left + plotWidth * index / Math.max(1, labels.length - 1);
      ctx.fillStyle = "#b8c4cf";
      ctx.font = "11px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText(label, x, height - 28);
    });
    drawCanvasLegend(ctx, datasets, colors, width);
    return true;
  }

  // 柱状图与直方图：直方图让柱体紧密排列
  let maxValue = Math.max(...finite, 0);
  let minValue = Math.min(...finite, 0);
  if (maxValue === minValue) maxValue = maxValue >= 0 ? maxValue * 1.2 + 1 : 1;
  else {
    const padding = (maxValue - minValue) * 0.12;
    maxValue += padding;
    minValue -= padding;
  }
  const yFor = value => top + (maxValue - value) / (maxValue - minValue) * plotHeight;
  const baseline = yFor(0);
  drawCanvasGrid(ctx, left, top, plotWidth, plotHeight, minValue, maxValue, yFor);
  const slot = plotWidth / labels.length;
  const histogram = prepared.type === "histogram";
  const barWidth = histogram ? slot : Math.min(42, slot * 0.66 / datasets.length);
  labels.forEach((label, labelIndex) => {
    datasets.forEach((dataset, datasetIndex) => {
      const value = dataset.values[labelIndex];
      if (!Number.isFinite(value)) return;
      const center = left + slot * labelIndex + slot / 2;
      const x = histogram
        ? center - slot / 2
        : center - datasets.length * barWidth / 2 + datasetIndex * barWidth;
      const y = yFor(value);
      ctx.fillStyle = colors[datasetIndex % colors.length];
      ctx.fillRect(x, Math.min(y, baseline), Math.max(1, barWidth - (histogram ? 0 : 2)), Math.max(2, Math.abs(baseline - y)));
    });
    ctx.fillStyle = "#b8c4cf";
    ctx.font = "11px Microsoft YaHei";
    ctx.textAlign = "center";
    ctx.fillText(label, left + slot * labelIndex + slot / 2, height - 28);
  });
  drawCanvasLegend(ctx, datasets, colors, width);
  return true;
}

function chartPreviewDetails(chart) {
  const prepared = prepareChartData(chart);
  if (!prepared) return '<div class="data-note">暂无可预览数据</div>';
  if (prepared.type === "pie") {
    const values = prepared.datasets[0]?.values || [];
    const total = values.reduce(
      (sum, value) => sum + (Number.isFinite(value) ? Math.abs(value) : 0),
      0
    );
    return `
      <h4>占比明细</h4>
      <div class="data-note">按原始数据展示，百分比保留实际精度</div>
      ${prepared.labels.map((label, index) => {
        const value = Number.isFinite(values[index]) ? values[index] : 0;
        const percent = total ? Math.abs(value) / total * 100 : 0;
        return `
          <div class="chart-data-row">
            <span title="${escapeHtml(label)}">${escapeHtml(label)}</span>
            <span>${escapeHtml(chartNumber(value))}<em>${formatChartPercent(percent)}%</em></span>
          </div>`;
      }).join("")}`;
  }
  return `
    <h4>数据明细</h4>
    <div class="data-note">${escapeHtml(prepared.type)} · ${prepared.datasets.length} 组数据</div>
    ${prepared.labels.map((label, index) => `
      <div class="chart-data-row">
        <span title="${escapeHtml(label)}">${escapeHtml(label)}</span>
        <span>${prepared.datasets.map(dataset => `
          ${escapeHtml(dataset.label)}：${Number.isFinite(dataset.values[index])
            ? escapeHtml(chartNumber(dataset.values[index]))
            : "无数据"}
        `).join("<br>")}</span>
      </div>
    `).join("")}`;
}

function applyPreviewTransform() {
  previewFrame = 0;
  const wrap = document.getElementById("chartPreviewCanvasWrap");
  wrap.style.transform =
    `translate3d(${previewTransform.x}px, ${previewTransform.y}px, 0) ` +
    `scale(${previewTransform.scale})`;
  document.getElementById("chartZoomValue").textContent =
    `${Math.round(previewTransform.scale * 100)}%`;
}

function schedulePreviewTransform() {
  if (previewFrame) cancelAnimationFrame(previewFrame);
  previewFrame = requestAnimationFrame(applyPreviewTransform);
}

function clampPreviewPan() {
  const viewport = document.getElementById("chartPreviewViewport");
  const maxX = Math.max(0, viewport.clientWidth * (previewTransform.scale - 1) / 2);
  const maxY = Math.max(0, viewport.clientHeight * (previewTransform.scale - 1) / 2);
  previewTransform.x = Math.max(-maxX, Math.min(maxX, previewTransform.x));
  previewTransform.y = Math.max(-maxY, Math.min(maxY, previewTransform.y));
}

function resetPreviewTransform() {
  previewTransform = { scale: 1, x: 0, y: 0 };
  schedulePreviewTransform();
}

function zoomPreview(factor, clientX, clientY) {
  const viewport = document.getElementById("chartPreviewViewport");
  const rect = viewport.getBoundingClientRect();
  const oldScale = previewTransform.scale;
  const newScale = Math.max(1, Math.min(5, oldScale * factor));
  if (newScale === oldScale) return;
  const originX = (clientX ?? rect.left + rect.width / 2) - rect.left - rect.width / 2;
  const originY = (clientY ?? rect.top + rect.height / 2) - rect.top - rect.height / 2;
  const ratio = newScale / oldScale;
  previewTransform.x = originX - (originX - previewTransform.x) * ratio;
  previewTransform.y = originY - (originY - previewTransform.y) * ratio;
  previewTransform.scale = newScale;
  if (newScale === 1) {
    previewTransform.x = 0;
    previewTransform.y = 0;
  }
  clampPreviewPan();
  schedulePreviewTransform();
}

function openChartPreview(chart) {
  if (!chart) return;
  previewChartData = chart;
  const modal = document.getElementById("chartModal");
  document.getElementById("chartPreviewTitle").textContent = chart.title || "图表高清预览";
  document.getElementById("chartPreviewData").innerHTML = chartPreviewDetails(chart);
  resetPreviewTransform();
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => {
    const canvas = document.getElementById("chartPreviewCanvas");
    if (previewChartInstance) {
      previewChartInstance.destroy();
      previewChartInstance = null;
    }
    if (drawChartOnCanvas(canvas, chart, { preview: true })) {
      previewChartInstance = canvas._chartInstance;
      hydrateChartShell(previewChartInstance, {
        legendId: "chartPreviewLegend",
        instance: () => previewChartInstance
      });
    }
  });
}

function closeChartPreview() {
  const modal = document.getElementById("chartModal");
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  if (previewChartInstance) {
    previewChartInstance.destroy();
    previewChartInstance = null;
  }
  previewChartData = null;
  resetPreviewTransform();
}

function drawCanvasGrid(ctx, left, top, plotWidth, plotHeight, minValue, maxValue, yFor) {
  ctx.strokeStyle = "#293744";
  ctx.fillStyle = "#98a7b5";
  ctx.font = "11px Microsoft YaHei";
  for (let i = 0; i <= 4; i++) {
    const value = minValue + (maxValue - minValue) * i / 4;
    const y = yFor(value);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + plotWidth, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(chartNumber(value), left - 9, y + 4);
  }
  ctx.textAlign = "left";
}

function drawCanvasLegend(ctx, datasets, colors, width) {
  ctx.font = "11px Microsoft YaHei";
  datasets.forEach((dataset, index) => {
    const x = width - 210 + (index % 2) * 105;
    const y = 50 + Math.floor(index / 2) * 16;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x, y - 9, 10, 10);
    ctx.fillStyle = "#c7d2dc";
    ctx.fillText(dataset.label, x + 14, y);
  });
}

function setComposerState(state) {
  const button = document.getElementById("sendBtn");
  document.getElementById("analysisMode").disabled = state !== "idle";
  button.dataset.state = state;
  if (state === "running") {
    button.textContent = "暂停";
    button.disabled = false;
  } else if (state === "paused") {
    button.textContent = "继续";
    button.disabled = false;
  } else {
    button.textContent = "发送";
    button.disabled = false;
  }
}

function updateSessionContext(title, meta) {
  document.getElementById("contextSessionTitle").textContent = title || "新会话";
  document.getElementById("contextSessionMeta").textContent = meta || "";
}

function updateTaskContext(stage, progress) {
  document.getElementById("contextStage").textContent = stage || "等待提问";
  document.getElementById("contextProgress").style.width = `${Math.max(0, Math.min(100, progress || 0))}%`;
}

function syncComposerSpace() {
  const composer = document.querySelector(".composer");
  if (!composer) return;
  if (composer.closest(".empty-state")) {
    // hero 态输入框在正文流里，底部不需要预留坞位
    document.documentElement.style.setProperty("--composer-space", "40px");
    return;
  }
  const height = Math.ceil(composer.getBoundingClientRect().height + 28);
  document.documentElement.style.setProperty("--composer-space", `${height}px`);
}

// 导出公共件：一份回答统一拆成「问题 / 结论 / 要点 / 表格 / 依据 / 提醒」，
// Markdown 和 Word 共用同一份结构，不会出现两种导出内容不一致。
function answerSections(result) {
  const table = Array.isArray(result.table) && typeof result.table[0] === "object" ? result.table : [];
  return {
    question: String(result.question || "").trim(),
    summary: ensureSentenceEnd(cleanAnswer(result.summary || result.answer || "")),
    points: (result.key_points || []).map(item => String(item || "").trim()).filter(Boolean),
    columns: table.length ? Object.keys(table[0]) : [],
    rows: table,
    evidence: (result.evidence || []).map(item => {
      if (item.type === "report_text") {
        const src = [item.company_name, item.file_name, item.page_no ? `第${item.page_no}页` : ""]
          .filter(Boolean).join(" · ");
        return `财报原文${src ? `（${src}）` : ""}：${item.excerpt || ""}`;
      }
      if (item.type === "metric_source") {
        const metrics = (item.metrics || []).map(m => `${m.label}=${m.value}`).join("，");
        return `${item.company_name || "公司数据"} ${item.report_period || ""}：${metrics}`.trim();
      }
      if (item.type === "data_limit") {
        return `数据说明：${(item.items || []).join("；")}`;
      }
      return "";
    }).filter(Boolean),
    warnings: (result.warnings || []).map(item => (typeof item === "string" ? item : JSON.stringify(item))).filter(Boolean),
    note: String(result.chart_note || "").trim(),
  };
}

function exportFileName(result, extension) {
  const base = (String(result.question || "").replace(/[\\/:*?"<>|]/g, "").trim() || "FinSight 回答").slice(0, 40);
  return `${base}.${extension}`;
}

function downloadText(content, mimeType, fileName) {
  const blob = new Blob([content], { type: mimeType });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(link.href);
}

function answerMarkdown(result) {
  const data = answerSections(result);
  const lines = [`# ${data.question || "FinSight 分析结果"}`, ""];
  if (data.summary) lines.push(data.summary, "");
  if (data.points.length) lines.push("## 要点", ...data.points.map(item => `- ${item}`), "");
  if (data.columns.length) {
    lines.push(
      "## 数据表",
      `| ${data.columns.join(" | ")} |`,
      `| ${data.columns.map(() => "---").join(" | ")} |`
    );
    data.rows.forEach(row => {
      lines.push(`| ${data.columns.map(c => String(row[c] ?? "").replace(/\|/g, "/")).join(" | ")} |`);
    });
    lines.push("");
  }
  if (data.note) lines.push(`> 说明：${data.note}`, "");
  if (data.evidence.length) lines.push("## 数据依据", ...data.evidence.map(item => `- ${item}`), "");
  if (data.warnings.length) lines.push("## 提醒", ...data.warnings.map(item => `- ${item}`), "");
  lines.push("---", "", `导出时间：${new Date().toLocaleString("zh-CN")} · FinSight`);
  return lines.join("\n");
}

const WORD_STYLE = [
  "@page WordSection1 { size: 595.3pt 841.9pt; margin: 2cm 2cm 1.8cm 2cm; }",
  "div.WordSection1 { page: WordSection1; }",
  "body { font-family: '等线','微软雅黑','Microsoft YaHei',sans-serif; font-size: 11pt; line-height: 1.75; color: #1f2733; }",
  "h1 { font-size: 19pt; line-height: 1.4; margin: 0 0 14pt; padding-bottom: 8pt; border-bottom: 2.25pt solid #2b6cb0; color: #14345c; }",
  "h2 { font-size: 13.5pt; line-height: 1.4; margin: 18pt 0 8pt; padding-left: 8pt; border-left: 4pt solid #2b6cb0; color: #14345c; }",
  "p { margin: 0 0 10pt; text-align: justify; }",
  "ul { margin: 0 0 10pt; padding-left: 20pt; }",
  "li { margin: 0 0 7pt; line-height: 1.7; }",
  "table { border-collapse: collapse; width: 100%; margin: 0 0 12pt; font-size: 10.5pt; }",
  "th { background: #eaf1f9; border: 0.75pt solid #9fb3cc; padding: 6pt 8pt; text-align: left; line-height: 1.5; }",
  "td { border: 0.75pt solid #c3d0e0; padding: 5pt 8pt; text-align: left; line-height: 1.5; }",
  "p.note { color: #4a5568; font-size: 10.5pt; margin: 0 0 12pt; }",
  "p.foot { color: #8a94a6; font-size: 9pt; margin: 16pt 0 0; text-align: right; border-top: 0.75pt solid #d7dee8; padding-top: 6pt; }",
].join("\n");

function wordParagraphs(text) {
  // Word 里最怕一整坨：先按换行分，再对超长段按句号/分号切到 ~180 字一段。
  const chunks = [];
  String(text || "").split(/\n+/).forEach(block => {
    const value = block.trim();
    if (!value) return;
    if (value.length <= 220) { chunks.push(value); return; }
    let current = "";
    value.split(/(?<=[。；;!?！？])/).forEach(sentence => {
      if ((current + sentence).length > 180 && current) { chunks.push(current); current = sentence; }
      else { current += sentence; }
    });
    if (current.trim()) chunks.push(current.trim());
  });
  return chunks;
}

function answerWordHtml(result) {
  const data = answerSections(result);
  const parts = [`<h1>${escapeHtml(data.question || "FinSight 分析结果")}</h1>`];
  wordParagraphs(data.summary).forEach(block => {
    parts.push(`<p>${escapeHtml(block)}</p>`);
  });
  if (data.points.length) parts.push("<h2>要点</h2><ul>" + data.points.map(i => `<li>${escapeHtml(i)}</li>`).join("") + "</ul>");
  if (data.columns.length) {
    parts.push(
      "<h2>数据表</h2><table>" +
      `<tr>${data.columns.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr>` +
      data.rows.map(row => `<tr>${data.columns.map(c => `<td>${escapeHtml(row[c] ?? "")}</td>`).join("")}</tr>`).join("") +
      "</table>"
    );
  }
  if (data.note) parts.push(`<p class='note'>说明：${escapeHtml(data.note)}</p>`);
  if (data.evidence.length) parts.push("<h2>数据依据</h2><ul>" + data.evidence.map(i => `<li>${escapeHtml(i)}</li>`).join("") + "</ul>");
  if (data.warnings.length) parts.push("<h2>提醒</h2><ul>" + data.warnings.map(i => `<li>${escapeHtml(i)}</li>`).join("") + "</ul>");
  parts.push(`<p class='foot'>导出时间：${escapeHtml(new Date().toLocaleString("zh-CN"))} · FinSight</p>`);
  return `<html xmlns:w="urn:schemas-microsoft-com:office:word"><head><meta charset="utf-8">` +
    `<title>${escapeHtml(data.question || "FinSight")}<\/title>` +
    `<style type="text/css">${WORD_STYLE}<\/style><\/head>` +
    `<body><div class="WordSection1">${parts.join("")}<\/div><\/body><\/html>`;
}

function tableExcelHtml(result) {
  const rows = Array.isArray(result.table) ? result.table : [];
  if (!rows.length || typeof rows[0] !== "object") return "";
  const columns = Object.keys(rows[0]);
  return `<html xmlns:x="urn:schemas-microsoft-com:office:excel"><head><meta charset="utf-8"><\/head><body>` +
    `<table border="1"><tr>${columns.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr>` +
    rows.map(row => `<tr>${columns.map(c => `<td>${escapeHtml(row[c] ?? "")}</td>`).join("")}</tr>`).join("") +
    "<\/table><\/body><\/html>";
}

function renderResult(question, result, replace = false, includeQuestion = true) {
  const box = document.getElementById("messages");
  if (replace) box.innerHTML = "";
  const chartId = "chart_" + Date.now();
  const answerId = "answer_" + Date.now();
  const metrics = (result.metrics || []).map(item => `
    <div class="answer-metric">
      <label>${escapeHtml(item.label || "")}</label>
      <strong>${escapeHtml(item.value || "暂无数据")}</strong>
      ${item.change ? `<div class="muted">${escapeHtml(item.change)}</div>` : ""}
    </div>`).join("");
  const summarySplit = splitCaliberNote(
    ensureSentenceEnd(cleanAnswer(result.summary || result.answer || ""))
  );
  const summaryDedupeKey = normalizeForDedupe(stripCompanyNames(
    summarySplit.body + "\n" + summarySplit.caliber
  ));
  const points = (result.key_points || [])
    .filter(item => !mostlyContainedIn(item, summaryDedupeKey))
    .map(item => `<li>${escapeHtml(item)}</li>`).join("");
  const warnings = (result.warnings || []).map(
    item => `<div>${escapeHtml(item)}</div>`
  ).join("");
  const evidence = result.evidence || [];
  const tableCompanyCount = new Set(
    (result.table || [])
      .map(row => row["公司名称"] || row["stock_name"] || "")
      .filter(Boolean)
  ).size;
  const evidenceCompanyCount =
    evidence.filter(item => item.type === "metric_source").length || tableCompanyCount;
  const evidenceSourceCount = new Set(
    evidence
      .filter(item => item.type === "report_text")
      .map(item => `${item.file_name || ""}:${item.page_no || 0}`)
  ).size;
  const evidenceLimitCount = evidence
    .filter(item => item.type === "data_limit")
    .reduce((count, item) => count + (item.items || []).length, 0);
  const evidenceSummary = result.evidence_summary || {
    company_count: evidenceCompanyCount,
    source_count: evidenceSourceCount,
    metric_count: evidence
      .filter(item => item.type === "metric_source")
      .reduce((count, item) => count + (item.metrics || []).length, 0),
    limit_count: evidenceLimitCount
  };
  const evidenceSummaryText = [
    evidenceSummary.company_count ? `${evidenceSummary.company_count}家公司` : "",
    evidenceSummary.source_count ? `${evidenceSummary.source_count}份资料` : "",
    evidenceSummary.metric_count ? `${evidenceSummary.metric_count}项指标` : "",
    evidenceSummary.limit_count ? `${evidenceSummary.limit_count}项说明` : ""
  ].filter(Boolean).join(" · ") || "暂无依据";
  const evidenceItems = evidence.map(item => {
    if (item.type === "report_text") {
      const source = [
        item.company_name,
        item.file_name,
        item.page_no ? `第${item.page_no}页` : ""
      ]
        .filter(Boolean)
        .join(" · ");
      return `
        <div class="evidence-item">
          <strong>财报原文</strong>
          <div class="muted">${escapeHtml(source || "已上传财报")}</div>
          ${escapeHtml(item.excerpt || "")}
          ${item.file_name ? `<div><button class="ev-open" data-file="${escapeHtml(item.file_name)}" data-page="${Number(item.page_no || 0)}">打开原文${item.page_no ? ` · 第${Number(item.page_no)}页` : ""}</button></div>` : ""}
        </div>`;
    }
    if (item.type === "metric_source") {
      const metrics = (item.metrics || []).map(metric => `
        <div>${escapeHtml(metric.label || "")}：${escapeHtml(metric.value ?? "暂无数据")}</div>
      `).join("");
      const missing = (item.missing || []).length
        ? `<div class="muted">暂缺：${escapeHtml((item.missing || []).join("、"))}</div>`
        : "";
      const source = [
        item.file_name,
        item.page_no ? `第${item.page_no}页` : ""
      ].filter(Boolean).join(" · ");
      return `
        <div class="evidence-item">
          <strong>${escapeHtml(item.company_name || "公司数据")}</strong>
          <div class="muted">${escapeHtml(item.report_period || "报告期未标注")}</div>
          ${metrics}
          ${item.calculation ? `<div class="muted">${escapeHtml(item.calculation)}</div>` : ""}
          ${missing}
          ${source ? `<div class="muted">来源：${escapeHtml(source)}</div>` : ""}
          ${item.file_name ? `<div><button class="ev-open" data-file="${escapeHtml(item.file_name)}" data-page="${Number(item.page_no || 0)}">看这一页${item.page_no ? ` · 第${Number(item.page_no)}页` : ""}</button></div>` : ""}
        </div>`;
    }
    if (item.type === "structured_table") {
      return `
        <div class="evidence-item">
          <strong>${escapeHtml(item.company_name || "结构化财报数据")}</strong>
          ${item.report_period ? `<div class="muted">${escapeHtml(item.report_period)}</div>` : ""}
          <div>已用于生成上方数据表，共 ${Number(item.rows || 0)} 行。</div>
        </div>`;
    }
    if (item.type === "data_limit") {
      const limits = (item.items || []).map(text => `<li>${escapeHtml(text)}</li>`).join("");
      return `
        <div class="evidence-item">
          <strong>数据限制</strong>
          <ul>${limits}</ul>
        </div>`;
    }
    return "";
  }).join("");
  const clarificationOptions = (result.clarification_options || []).map(
    item => `<button class="clarify-chip" data-question="${escapeHtml(item)}">${escapeHtml(item)}</button>`
  ).join("");
  const summaryText = ensureSentenceEnd(cleanAnswer(result.summary || result.answer || ""));
  const summaryParts = splitCaliberNote(summaryText);
  const summaryHtml = highlightNumbers(escapeHtml(summaryParts.body));
  const caliberHtml = summaryParts.caliber
    ? `<div class="caliber-note">${escapeHtml(summaryParts.caliber)}</div>` : "";
  const item = document.createElement("div");
  item.className = "message";
  item.innerHTML = `
    ${includeQuestion ? `<div class="message user"><div class="bubble">${escapeHtml(question)}</div></div>` : ""}
    <div class="answer" id="${answerId}">
      <div class="answer-head">
        <span>分析结果</span>
        <div class="answer-actions">
          <button class="answer-action" data-action="copy">复制</button>
          <button class="answer-action" data-action="md" title="存成 .md 文件">导出 MD</button>
          <button class="answer-action" data-action="word" title="存成规范 A4 版式的 Word 文档（.docx）">导出 Word</button>
        </div>
      </div>
      <div class="answer-summary">${summaryHtml}</div>
      ${metrics ? `<div class="answer-metrics">${metrics}</div>` : ""}
      ${points ? `<ul class="key-points">${points}</ul>` : ""}
      ${renderTable(result.table || [])}
      ${result.chart_note ? `<div class="sources">${escapeHtml(result.chart_note)}</div>` : ""}
      ${caliberHtml}
      ${warnings ? `<div class="warning-list">${warnings}</div>` : ""}
      ${evidenceItems ? `<details class="evidence-block"><summary>数据依据 · ${escapeHtml(evidenceSummaryText)}</summary><div class="evidence-list">${evidenceItems}</div></details>` : ""}
      ${clarificationOptions ? `<div class="clarification-options">${clarificationOptions}</div>` : ""}
      ${result.chart ? chartCardHtml(chartId, result.chart) : ""}
      ${(result.table && result.table.length) || result.chart ? `
        <div class="download-row" id="download_${chartId}"></div>` : ""}
      ${Number.isFinite(Number(result.thinking_seconds)) ? `<div class="think-time">已思考 ${Number(result.thinking_seconds).toFixed(2)} 秒</div>` : ""}
    </div>`;
  box.appendChild(item);
  item.querySelector('[data-action="copy"]').onclick = async event => {
    await navigator.clipboard.writeText(
      ensureSentenceEnd(cleanAnswer(result.summary || result.answer || ""))
    );
    event.currentTarget.textContent = "已复制";
    setTimeout(() => { event.currentTarget.textContent = "复制"; }, 1200);
  };
  const flashAction = (button, original) => {
    button.textContent = "已导出";
    setTimeout(() => { button.textContent = original; }, 1200);
  };
  const mdButton = item.querySelector('[data-action="md"]');
  if (mdButton) {
    mdButton.onclick = () => {
      downloadText(answerMarkdown(result), "text/markdown;charset=utf-8", exportFileName(result, "md"));
      flashAction(mdButton, "导出 MD");
    };
  }
  const wordButton = item.querySelector('[data-action="word"]');
  if (wordButton) {
    // 正路：让后端用 python-docx 排一份规范 A4 纵向的真 .docx（三线表/千分位/页码域）。
    wordButton.onclick = async () => {
      const original = "导出 Word";
      wordButton.textContent = "生成中…";
      wordButton.disabled = true;
      try {
        const response = await fetch("/api/export/docx", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            question: result.question,
            summary: result.summary || result.answer,
            key_points: result.key_points,
            table: result.table,
            evidence: result.evidence,
            warnings: result.warnings,
            chart_note: result.chart_note,
            company_name: result.company_name
          })
        });
        if (!response.ok) throw new Error(String(response.status));
        const disposition = response.headers.get("Content-Disposition") || "";
        const matched = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        const name = matched ? decodeURIComponent(matched[1]) : exportFileName(result, "docx");
        const blob = await response.blob();
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = name;
        link.click();
        URL.revokeObjectURL(link.href);
        flashAction(wordButton, original);
      } catch (error) {
        // 兜底：老的前端拼 .doc 路（万一打到没升级的服务器也不至于导出无门）。
        // BOM 是给老 Word 认 UTF-8 用的，少了它中文会花。
        downloadText("\ufeff" + answerWordHtml(result), "application/msword;charset=utf-8", exportFileName(result, "doc"));
        flashAction(wordButton, original);
      } finally {
        wordButton.disabled = false;
      }
    };
  }
  item.querySelectorAll(".clarify-chip").forEach(button => {
    button.onclick = () => {
      document.getElementById("question").value = button.dataset.question || "";
      sendQuestion();
    };
  });
  // 「打开原文」：证据里只带文件名和页码，先用文件库接口把文件名换成文档 id，
  // 再走现成的 /api/documents/open/<id>（系统默认程序打开那份 PDF）。
  item.querySelectorAll(".ev-open").forEach(button => {
    button.onclick = async () => {
      const fileName = button.dataset.file || "";
      const page = Number(button.dataset.page || 0);
      const original = button.textContent;
      button.textContent = "正在打开…";
      try {
        const groups = await readJson(
          await fetch(`/api/documents?query=${encodeURIComponent(fileName)}`), "文件定位"
        );
        let hit = null;
        (groups || []).forEach(group => {
          (group.documents || []).forEach(doc => {
            if (!hit && doc.file_name === fileName) hit = doc;
          });
        });
        if (!hit) {
          button.textContent = "库里找不到这份文件";
        } else {
          // open 接口是 inline 返回 PDF 字节，浏览器自带的阅读器认 `#page=N` ⇒ 直接翻到引用的那一页。
          window.open(
            `/api/documents/open/${hit.id}` + (page ? `#page=${page}` : ""),
            "_blank"
          );
          button.textContent = page ? `已打开 · 第${page}页` : "已打开";
        }
      } catch (_) {
        button.textContent = "打开失败";
      }
      setTimeout(() => { button.textContent = original; }, 2500);
    };
  });
  const downloadRow = item.querySelector(`#download_${chartId}`);
  if (result.table && result.table.length && downloadRow) {
    const csvButton = document.createElement("button");
    csvButton.className = "download-btn";
    csvButton.textContent = "下载表格 CSV";
    csvButton.onclick = () => {
      const columns = Object.keys(result.table[0]);
      const lines = [
        columns.join(","),
        ...result.table.map(row => columns.map(column => `"${String(row[column] ?? "").replace(/"/g, '""')}"`).join(","))
      ];
      const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = exportFileName(result, "csv");
      link.click();
      URL.revokeObjectURL(link.href);
    };
    downloadRow.appendChild(csvButton);
    const excelButton = document.createElement("button");
    excelButton.className = "download-btn";
    excelButton.textContent = "下载表格 Excel";
    excelButton.onclick = () => {
      const html = tableExcelHtml(result);
      if (html) downloadText("\ufeff" + html, "application/vnd.ms-excel;charset=utf-8", exportFileName(result, "xls"));
    };
    downloadRow.appendChild(excelButton);
  }
  if (result.chart) {
    const canvas = item.querySelector(`#${chartId}`);
    const rendered = canvas ? drawChartOnCanvas(canvas, result.chart) : false;
    if (rendered) {
      hydrateChartShell(canvas._chartInstance, {
        legendId: `${chartId}_legend`,
        instance: () => canvas._chartInstance
      });
    }
    if (rendered && downloadRow) {
      const previewButton = document.createElement("button");
      previewButton.className = "download-btn";
      previewButton.textContent = "高清预览";
      previewButton.onclick = () => openChartPreview(result.chart);
      downloadRow.appendChild(previewButton);
      canvas.addEventListener("click", () => openChartPreview(result.chart));
      const pngButton = document.createElement("button");
      pngButton.className = "download-btn";
      pngButton.textContent = "下载图表 PNG";
      pngButton.onclick = () => {
        const link = document.createElement("a");
        link.href = canvas.toDataURL("image/png");
        link.download = `${result.chart.title || "financial_chart"}.png`;
        link.click();
      };
      downloadRow.appendChild(pngButton);
    }
  }
  item.scrollIntoView({ behavior: "smooth", block: "end" });
}

function setSqlLogExpanded(expanded, remember = true) {
  sqlLogExpanded = Boolean(expanded);
  document.body.classList.toggle("sql-rail-open", sqlLogExpanded);
  const list = document.getElementById("sqlLogList");
  const toggle = document.getElementById("sqlLogToggle");
  const tab = document.getElementById("sqlRailTab");
  if (list) list.hidden = !sqlLogExpanded;
  if (toggle) toggle.textContent = sqlLogExpanded ? "收起" : "展开";
  if (tab) {
    tab.title = sqlLogExpanded ? "收起 SQL 执行日志" : "展开 SQL 执行日志";
  }
  if (remember) {
    try { localStorage.setItem("finsight.sqlRailOpen", sqlLogExpanded ? "1" : "0"); } catch (_) {}
  }
}

function renderSqlLogs(logs, status = "") {
  const list = document.getElementById("sqlLogList");
  const summary = document.getElementById("sqlLogSummary");
  if (!list || !summary) return;
  const entries = Array.isArray(logs) ? logs.slice(-80) : [];
  lastSqlLogs = entries;
  const completed = entries.filter(item => item.type === "subtask_done");
  const success = completed.filter(item => item.status === "completed").length;
  const empty = completed.filter(item => item.status === "empty").length;
  const failed = completed.filter(
    item => item.status !== "completed" && item.status !== "empty"
  ).length;
  const lastStage = [...entries].reverse().find(item => item.type === "stage");
  if (status === "running" || status === "paused") {
    summary.textContent =
      `${lastStage?.stage || (status === "paused" ? "已暂停" : "处理中")}` +
      ` · 成功${success} · 无数据${empty} · 失败${failed}`;
  } else if (entries.length) {
    summary.textContent =
      `最近一次任务 · 成功${success} · 无数据${empty} · 失败${failed}`;
  } else {
    summary.textContent = "等待任务";
  }
  list.innerHTML = entries.map(item => {
    if (item.type === "stage") {
      return `<div class="sql-log-item running"><strong>阶段</strong> ${escapeHtml(item.stage || "")}<div class="sql-log-meta">${escapeHtml(item.time || "")}</div></div>`;
    }
    if (item.type === "plan") {
      const companies = (item.companies || []).filter(Boolean).join("、");
      return `<div class="sql-log-item success"><strong>规划完成</strong> ${Number(item.subtasks || 0)} 个子任务${companies ? ` · ${escapeHtml(companies)}` : ""}<div class="sql-log-meta">${escapeHtml(item.time || "")}</div></div>`;
    }
    if (item.type === "subtask_start") {
      return `<div class="sql-log-item running"><strong>开始${item.kind === "rag" ? "资料检索" : "SQL查询"}</strong> ${escapeHtml(item.company || "")}<div class="sql-log-meta">${escapeHtml(item.time || "")}</div></div>`;
    }
    if (item.type === "subtask_done") {
      const ok = item.status === "completed";
      const empty = item.status === "empty";
      const statusClass = ok ? "success" : empty ? "empty" : "failed";
      const attempts = Math.max(1, Number(item.attempts || 1));
      const purpose = String(item.purpose || "").trim();
      const statusText = ok ? "已取到" : empty ? "没有匹配数据" : "查询失败";
      const got = item.kind === "rag"
        ? `检索到资料 ${Number(item.sources || 0)} 条`
        : `读取 ${Number(item.rows || 0)} 行数据`;
      const bits = [];
      if (purpose) bits.push(purpose);
      bits.push(`用时 ${Number(item.duration || 0).toFixed(2)} 秒`);
      if (attempts > 1) bits.push(ok ? `自动修正后第 ${attempts} 次才成功` : `重试了 ${attempts} 次`);
      if (item.time) bits.push(item.time);
      const error = item.error ? `<div class="sql-log-meta">${escapeHtml(item.error)}</div>` : "";
      const sql = item.sql ? `<details class="sql-fold"><summary>查看 SQL 语句</summary><pre class="sql-code">${escapeHtml(item.sql)}</pre></details>` : "";
      return `<div class="sql-log-item ${statusClass}"><strong>${escapeHtml(item.company || "")} · ${statusText}</strong> ${escapeHtml(got)}<div class="sql-log-meta">${bits.map(escapeHtml).join(" · ")}</div>${error}${sql}</div>`;
    }
    if (item.type === "error") {
      return `<div class="sql-log-item failed"><strong>执行错误</strong> ${escapeHtml(item.error || "")}<div class="sql-log-meta">${escapeHtml(item.time || "")}</div></div>`;
    }
    return "";
  }).join("") || '<div class="sql-log-item">等待任务运行。</div>';
}

document.getElementById("sqlLogToggle").onclick = () => {
  setSqlLogExpanded(!sqlLogExpanded);
};

// SQL 日志一键复制：把条目还原成「注释 + 语句」的纯文本，贴给同学/老师就能直接看。
const sqlLogCopyButton = document.getElementById("sqlLogCopy");
if (sqlLogCopyButton) {
  sqlLogCopyButton.onclick = async () => {
    const lines = [];
    lastSqlLogs.forEach(item => {
      if (item.type === "stage") {
        lines.push(`-- 阶段：${item.stage || ""}  ${item.time || ""}`);
      } else if (item.type === "plan") {
        lines.push(`-- 规划完成：${Number(item.subtasks || 0)} 个子任务`);
      } else if (item.type === "subtask_start") {
        lines.push(`-- 开始${item.kind === "rag" ? "资料检索" : "SQL查询"}：${item.company || ""}`);
      } else if (item.type === "subtask_done") {
        const meta = item.kind === "rag"
          ? `资料 ${Number(item.sources || 0)} 条`
          : `${Number(item.rows || 0)} 行`;
        lines.push(
          `-- ${item.company || ""}${item.purpose ? "：" + String(item.purpose).trim() : ""}` +
          ` · ${item.status || ""} · ${meta} · ` +
          `${Number(item.duration || 0).toFixed(2)}秒 · 尝试${Number(item.attempts || 1)}次`
        );
        if (item.sql) lines.push(String(item.sql).trim(), ";");
        if (item.error) lines.push(`-- 错误：${item.error}`);
      } else if (item.type === "error") {
        lines.push(`-- 执行错误：${item.error || ""}`);
      }
    });
    if (!lines.length) {
      sqlLogCopyButton.textContent = "暂无日志";
    } else {
      try {
        await navigator.clipboard.writeText(lines.join("\n"));
        sqlLogCopyButton.textContent = "已复制";
      } catch (_) {
        sqlLogCopyButton.textContent = "复制失败";
      }
    }
    setTimeout(() => { sqlLogCopyButton.textContent = "复制"; }, 1500);
  };
}

// 右栏竖条：点一下展开/收起；启动时按上次状态恢复（默认收起）
const sqlRailTab = document.getElementById("sqlRailTab");
if (sqlRailTab) sqlRailTab.onclick = () => setSqlLogExpanded(!sqlLogExpanded);
try {
  setSqlLogExpanded(localStorage.getItem("finsight.sqlRailOpen") === "1", false);
} catch (_) {
  setSqlLogExpanded(false, false);
}

async function sendQuestion() {
  const status = document.getElementById("status");
  if (currentTaskId) {
    const action = currentTaskState?.paused ? "resume" : "pause";
    const response = await fetch(`/api/chat/${action}/${currentTaskId}`, { method: "POST" });
    const task = await response.json();
    if (!response.ok) {
      status.textContent = task.detail || "任务状态更新失败";
      return;
    }
    currentTaskState = task;
    setComposerState(task.paused ? "paused" : "running");
    status.textContent = task.stage || (task.paused ? "已暂停" : "处理中");
    updateTaskContext(task.stage || (task.paused ? "已暂停" : "处理中"), task.paused ? 55 : 45);
    return;
  }
  const question = document.getElementById("question").value.trim();
  if (!question || chatStarting) return;
  chatStarting = true;
  document.getElementById("question").value = "";
  status.textContent = "正在提交问题";
  updateSessionContext(question, "分析进行中");
  updateTaskContext("准备问题", 10);
  renderSqlLogs([], "running");
  const messages = document.getElementById("messages");
  messages.querySelector(".empty-state")?.remove();
  lastUserQuestion = question;
  setHeroMode(false);
  const userMessage = document.createElement("div");
  userMessage.className = "message user";
  userMessage.innerHTML = `<div class="bubble">${escapeHtml(question)}</div>`;
  messages.appendChild(userMessage);
  const thinking = document.createElement("div");
  thinking.className = "message assistant thinking-message";
  thinking.innerHTML = '<div class="bubble"><span class="thinking-dots"></span>准备问题</div>';
  messages.appendChild(thinking);
  syncComposerSpace();
  thinking.scrollIntoView({ behavior: "smooth", block: "center" });
  setComposerState("running");
  try {
    const response = await fetch("/api/chat/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        session_id: currentSessionId,
        analysis_mode: document.getElementById("analysisMode").value,
        base_url: "https://api.deepseek.com",
        llm_model: "deepseek-flash",
        embedding_model: "BAAI/bge-small-zh-v1.5",
        answer_style: document.getElementById("answerStyle").value,
        unit_mode: document.getElementById("unitMode").value,
        audience: selectedAudience
      })
    });
    const task = await readJson(response, "提问");
    currentTaskId = task.task_id;
    currentTaskState = task;
    pollChatTask(task.task_id, thinking);
  } catch (error) {
    thinking.remove();
    status.textContent = "请求失败";
    renderResult("", { answer: error.message }, false, false);
    currentTaskId = null;
    currentTaskState = null;
    chatStarting = false;
    setComposerState("idle");
  }
}

async function pollChatTask(taskId, thinking) {
  // 只跟当前任务：新建会话/换问题后旧轮询链必须自己退出，否则同一答案渲染两遍、会话还会被改回去。
  if (!taskId || taskId !== currentTaskId) return;
  try {
    const response = await fetch(`/api/chat/status/${taskId}`);
    const task = await readJson(response, "任务状态");
    if (taskId !== currentTaskId) {
      thinking.remove();
      return;
    }
    currentTaskState = task;
    renderSqlLogs(task.logs || [], task.status);
    const bubble = thinking.querySelector(".bubble");
    // ★ 环常驻，只改文字：轮询是 600ms 一次，以前这里每次都用 innerHTML 重写整块气泡，
    //   等于每 600ms 把 .thinking-dots 拆掉重建 ⇒ CSS 动画每次都从 0° 重新开始，
    //   圈转到一半就跳回原点 —— 这才是“转圈卡顿”的真根因（跟显卡没关系）。
    let ring = bubble.querySelector(".thinking-dots");
    if (!ring) {
      ring = document.createElement("span");
      ring.className = "thinking-dots";
      bubble.textContent = "";
      bubble.appendChild(ring);
      bubble.appendChild(document.createTextNode("准备问题"));
    }
    const ringLabel = ring.nextSibling;
    if (task.status === "paused") {
      ring.classList.add("paused");
      if (ringLabel && ringLabel.nodeValue !== "已暂停") ringLabel.nodeValue = "已暂停";
      setComposerState("paused");
      document.getElementById("status").textContent = "已暂停，点击继续";
      updateTaskContext("已暂停", 55);
    } else if (task.status === "running") {
      ring.classList.remove("paused");
      const stageText = task.stage || "处理中";
      if (ringLabel && ringLabel.nodeValue !== stageText) ringLabel.nodeValue = stageText;
      setComposerState("running");
      document.getElementById("status").textContent = task.stage || "处理中";
      updateTaskContext(task.stage || "处理中", 58);
    } else if (task.status === "completed") {
      thinking.remove();
      const result = task.result || {};
      currentSessionId = result.session_id || currentSessionId;
      renderSqlLogs(result.execution_logs || task.logs || [], "completed");
      renderResult("", result, false, false);
      document.getElementById("status").textContent = "分析完成";
      currentTaskId = null;
      currentTaskState = null;
      chatStarting = false;
      setComposerState("idle");
      updateSessionContext(currentSessionId ? "当前会话" : "新会话", "分析已完成");
      updateTaskContext("分析完成", 100);
      loadHistory();
      return;
    } else {
      thinking.remove();
      document.getElementById("status").textContent = "处理失败";
      renderSqlLogs(task.logs || [], "failed");
      renderResult("", { answer: task.error || "任务处理失败" }, false, false);
      currentTaskId = null;
      currentTaskState = null;
      chatStarting = false;
      setComposerState("idle");
      updateTaskContext("处理失败", 100);
      return;
    }
    taskPollTimer = setTimeout(() => pollChatTask(taskId, thinking), 600);
  } catch (error) {
    if (taskId !== currentTaskId) {
      thinking.remove();
      return;
    }
    thinking.remove();
    document.getElementById("status").textContent = "请求失败";
    renderResult("", { answer: error.message }, false, false);
    currentTaskId = null;
    currentTaskState = null;
    chatStarting = false;
    setComposerState("idle");
    updateTaskContext("请求失败", 100);
  }
}

document.getElementById("sendBtn").onclick = sendQuestion;
document.getElementById("question").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendQuestion();
  }
});
document.getElementById("question").addEventListener("input", event => {
  const target = event.target;
  target.style.height = "auto";
  target.style.height = `${Math.min(target.scrollHeight, 72)}px`;
});

let importJobId = "";
const IMPORT_PHASE = {
  running: "导入中", paused: "已暂停", completed: "已完成",
  stopped: "已结束", failed: "出错"
};

async function importControl(action) {
  const note = document.getElementById("importNote");
  if (!importJobId) return;
  note.textContent = action === "pause"
    ? "正在暂停：手上这一份做完就停（大文件可能要几十秒）…"
    : (action === "stop" ? "正在结束：手上这一份做完就收工，队列里剩下的不再处理…" : "");
  try {
    const response = await fetch(`/api/import/${action}/${importJobId}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "操作失败");
  } catch (error) {
    note.textContent = error.message;
  }
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
}

function renderImportStatus(status) {
  const active = status.status === "running" || status.status === "paused";
  const pct = status.total ? Math.round((status.processed / status.total) * 100) : 0;
  document.getElementById("importBar").style.width = `${pct}%`;
  const waiting = status.status === "running" && status.control === "pause";
  document.getElementById("importPhase").textContent = waiting
    ? "暂停中（手上那份完成就停）"
    : (IMPORT_PHASE[status.status] || status.status || "导入中");
  document.getElementById("importCounts").textContent =
    `${status.processed} / ${status.total} · 成功 ${status.success}` +
    ` · 重复 ${status.skipped} · 失败 ${status.failed}`;
  document.getElementById("importRunning").textContent = active
    ? `正在处理：${(status.active || []).join("、") || "（等下一份）"} · 已用时 ${fmtDuration(status.elapsed_seconds)}`
    : "";
  document.getElementById("importPauseBtn").disabled = status.status !== "running";
  document.getElementById("importResumeBtn").disabled = status.status !== "paused";
  document.getElementById("importStopBtn").disabled = !active;
  const errors = status.errors || [];
  const details = document.getElementById("importErrors");
  if (errors.length) {
    details.style.display = "block";
    details.open = status.failed > 0 || status.status === "failed";
    const list = document.getElementById("importErrorList");
    list.innerHTML = "";
    for (const item of errors) {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    }
  } else {
    details.style.display = "none";
  }
  document.getElementById("importNote").textContent =
    status.status === "paused"
      ? "已暂停：手上那份跑完了，剩下的没开始。点「继续」接着排队。"
      : (active ? "「暂停」= 手上这一份做完就停；「结束」= 队列里剩下的不再处理。" : "");
  if (!active) {
    const dur = `，用时 ${fmtDuration(status.elapsed_seconds)}`;
    const state = document.getElementById("uploadState");
    if (status.status === "completed") {
      if (status.failed > 0) {
        state.textContent =
          `⚠️ 导入完成：成功 ${status.success} 件、失败 ${status.failed} 件${dur}。` +
          "失败原因见「失败与警告明细」，修正后可直接重新导入（重复的会自动跳过）。";
        state.style.color = "#e07b39";
      } else {
        state.textContent =
          `✅ 导入完成：成功 ${status.success} 件` +
          (status.skipped ? `、重复跳过 ${status.skipped} 件` : "") +
          (errors.length ? `；另有 ${errors.length} 条切片/向量警告（会自动补，见明细）` : "") +
          `${dur}。`;
        state.style.color = "";
      }
    } else if (status.status === "stopped") {
      state.textContent =
        `⏹ 已结束：已处理 ${status.processed} 件、取消 ${status.cancelled || 0} 件未开始${dur}。` +
        "已入库的文件都是完整的，下次导入会自动跳过。";
      state.style.color = "";
    } else {
      state.textContent = `❌ 导入出错：${errors[0] || "详情见服务端日志"}${dur}。`;
      state.style.color = "#d0453e";
    }
  }
}

async function pollImportJob(job) {
  importJobId = job.id;
  const state = document.getElementById("uploadState");
  state.textContent = "";
  state.style.color = "";
  document.getElementById("importProgress").style.display = "block";
  let fails = 0;
  const refresh = async () => {
    try {
      const response = await fetch(`/api/import/status/${job.id}`);
      const status = await response.json();
      if (!response.ok) {
        if (response.status === 404) {
          // 服务重启会把内存里的任务台账清掉（数据本身不丢）：说清楚，别让进度条悬着
          state.textContent =
            "找不到这个导入任务（多半是服务中途重启了）。已入库的文件不受影响，" +
            "没嵌完的向量会在服务启动时自动补齐；要接着导剩余文件，直接重新点「开始导入」即可（重复的会自动跳过）。";
          state.style.color = "#e07b39";
          document.getElementById("importProgress").style.display = "none";
          loadSummary();
          loadDocuments();
          return;
        }
        throw new Error(status.detail || "无法读取导入状态");
      }
      fails = 0;
      renderImportStatus(status);
      if (status.status === "running" || status.status === "paused") {
        setTimeout(refresh, 1000);
        return;
      }
      loadSummary();
      loadDocuments();
      loadRecommendations().then(pickSuggestions);
    } catch (error) {
      // 网络抖一下不该整个停掉轮询：连错 4 次才认输
      if (++fails < 4) {
        setTimeout(refresh, 1500);
        return;
      }
      state.textContent = error.message;
      state.style.color = "#d0453e";
    }
  };
  refresh();
}
document.getElementById("importPauseBtn").onclick = () => importControl("pause");
document.getElementById("importResumeBtn").onclick = () => importControl("resume");
document.getElementById("importStopBtn").onclick = () => importControl("stop");

document.getElementById("uploadBtn").onclick = async () => {
  const files = document.getElementById("files").files;
  const localDir = document.getElementById("localDir").value.trim();
  const state = document.getElementById("uploadState");
  if (!files.length && !localDir) {
    state.textContent = "请选择文件或输入目录";
    return;
  }
  try {
    let response;
    if (!files.length && localDir) {
      state.textContent = "正在创建导入任务...";
      response = await fetch("/api/import/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          local_dir: localDir,
          session_id: currentSessionId || ""
        })
      });
    } else {
      state.textContent = "正在上传文件...";
      const form = new FormData();
      for (const file of files) form.append("files", file);
      form.append("local_dir", localDir);
      form.append("session_id", currentSessionId || "");
      response = await fetch("/api/upload", { method: "POST", body: form });
    }
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || "任务创建失败");
    currentSessionId = job.session_id || currentSessionId;
    // 导入进度只写在「数据导入」页的进度卡上，不再往历史会话里塞「导入任务」条目
    document.getElementById("uploadState").textContent = "";
    pollImportJob(job);
  } catch (error) {
    state.textContent = error.message;
  }
};

document.getElementById("clearHistory").onclick = async () => {
  await fetch("/api/history", { method: "DELETE" });
  resetChatSession();
  loadHistory();
};
document.getElementById("newSession").onclick = resetChatSession;
document.getElementById("logoutBtn").onclick = async () => { await fetch("/api/logout", { method: "POST" }); location.href = "/"; };

document.querySelectorAll("[data-close-chart-modal]").forEach(element => {
  element.addEventListener("click", closeChartPreview);
});
document.getElementById("chartPreviewClose").onclick = closeChartPreview;
document.getElementById("chartZoomIn").onclick = () => zoomPreview(1.25);
document.getElementById("chartZoomOut").onclick = () => zoomPreview(0.8);
document.getElementById("chartZoomReset").onclick = resetPreviewTransform;
document.getElementById("chartPreviewDownload").onclick = () => {
  if (!previewChartInstance) return;
  const link = document.createElement("a");
  link.href = previewChartInstance.toBase64Image("image/png", 1);
  link.download = `${previewChartData?.title || "financial_chart"}_highres.png`;
  link.click();
};
const previewViewport = document.getElementById("chartPreviewViewport");
previewViewport.addEventListener("wheel", event => {
  event.preventDefault();
  zoomPreview(Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
}, { passive: false });
previewViewport.addEventListener("pointerdown", event => {
  if (event.button !== 0 || previewTransform.scale <= 1) return;
  previewDragging = true;
  previewPointer = {
    x: event.clientX,
    y: event.clientY,
    startX: previewTransform.x,
    startY: previewTransform.y
  };
  previewViewport.classList.add("dragging");
  previewViewport.setPointerCapture(event.pointerId);
});
previewViewport.addEventListener("pointermove", event => {
  if (!previewDragging) return;
  previewTransform.x = previewPointer.startX + event.clientX - previewPointer.x;
  previewTransform.y = previewPointer.startY + event.clientY - previewPointer.y;
  clampPreviewPan();
  schedulePreviewTransform();
});
const stopPreviewDrag = event => {
  if (!previewDragging) return;
  previewDragging = false;
  previewViewport.classList.remove("dragging");
  if (previewViewport.hasPointerCapture(event.pointerId)) {
    previewViewport.releasePointerCapture(event.pointerId);
  }
};
previewViewport.addEventListener("pointerup", stopPreviewDrag);
previewViewport.addEventListener("pointercancel", stopPreviewDrag);
previewViewport.addEventListener("dblclick", resetPreviewTransform);
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && document.getElementById("chartModal").classList.contains("open")) {
    closeChartPreview();
  }
});

document.getElementById("analysisMode").addEventListener("change", () => {
  saveSettings();
  syncModeStatus();
});
loadSavedSettings();
syncModeStatus();
setComposerState("idle");
syncComposerSpace();
if (window.ResizeObserver) {
  new ResizeObserver(syncComposerSpace).observe(
    document.querySelector(".composer")
  );
}
window.addEventListener("resize", syncComposerSpace);
window.addEventListener("resize", () => {
  if (chatPageEl.classList.contains("hero")) fitHeroComposer();
});
// 首帧：页面初始就是新会话（empty-state 在、hero 类已挂在 #page-chat），
// 把静态坞位里的 composer 一次性搬进 hero 正文流。
setHeroMode(!!document.querySelector("#messages .empty-state"));
document.getElementById("saveSettings").onclick = saveSettings;
loadRecommendations().then(pickSuggestions);
loadSummary();
loadHistory();
loadDocuments();
document.getElementById("fileSearchBtn").onclick = loadDocuments;
document.getElementById("fileSearch").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    loadDocuments();
  }
});
document.getElementById("deleteAllFilesBtn").onclick = async () => {
  // 全部删除：清的是库（入库记录、财务数字、切片、向量），磁盘上的原文不动，
  // 所以删完还能在「数据导入」里指同一目录重新灌回来。
  const list = document.getElementById("fileList");
  const button = document.getElementById("deleteAllFilesBtn");
  let groups;
  try {
    groups = await readJson(await fetch("/api/documents"), "文件列表");
  } catch (error) {
    list.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    return;
  }
  const count = (Array.isArray(groups) ? groups : []).reduce(
    (total, group) => total + (Array.isArray(group.documents) ? group.documents.length : 0),
    0,
  );
  if (!count) {
    list.innerHTML = '<div class="muted">文件库已经是空的。</div>';
    return;
  }
  if (!window.confirm(`确定清空文件库？会删掉 ${count} 份文件的入库记录、财务数字、切片和向量（磁盘上的原文不动）。之后可以重新导入。`)) {
    return;
  }
  button.disabled = true;
  button.textContent = "正在清空…";
  let result;
  let failure = "";
  try {
    result = await readJson(await fetch("/api/documents", { method: "DELETE" }), "全部删除");
  } catch (error) {
    failure = error.message;
  }
  button.disabled = false;
  button.textContent = "全部删除";
  if (failure) {
    list.innerHTML = `<div class="muted">${escapeHtml(failure)}</div>`;
    return;
  }
  if (!result || result.status !== "cleared") {
    list.innerHTML = `<div class="muted">全部删除失败：${escapeHtml((result && (result.error || result.status)) || "未知原因")}（库里可能只清了一部分，建议刷新看一下）</div>`;
    return;
  }
  loadDocuments();
  loadSummary();
  loadRecommendations().then(pickSuggestions);
  list.innerHTML = `<div class="muted">已清空 ${result.documents} 份文件的库内数据，原文仍在磁盘上，可在「数据导入」重新导入。</div>`;
};
</script>
</body>
</html>
"""

class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """提供独占端口且支持多线程的 HTTP 服务。"""
    allow_reuse_address = False

# APP_HOST 允许写成 0.0.0.0（对外访问，见 config.py 那条注释）。但 0.0.0.0 不能用来
# 「访问」自己：自检/停服/打印都要有个真能连的地址 ⇒ 统一换算成回环。
LOCAL_PROBE_HOST = "127.0.0.1" if APP_HOST in {"0.0.0.0", "", "::", "[::]"} else APP_HOST

def local_site_url(path: str = "") -> str:
    """服务对外监听 0.0.0.0 时，探测/自检要用 127.0.0.1 拼 URL，不能用监听地址本身。

    真根因：`urllib` 把 URL 里的 host 直接当连接目标 ⇒ 拿 `http://0.0.0.0:8501/...`
    请求 = 去连 `0.0.0.0` 这个地址，这在 Windows 上根本连不上（不是本机回环）。
    所以只要 APP_HOST 是通配监听，本机 URL 一律回退 127.0.0.1；端口照 APP_PORT 带上。
    """
    host = LOCAL_PROBE_HOST or "127.0.0.1"
    if int(APP_PORT) == 80:
        base = "http://%s" % host
    else:
        base = "http://%s:%d" % (host, int(APP_PORT))
    tail = str(path or "")
    if tail and not tail.startswith("/"):
        tail = "/" + tail
    return base + tail


def stop_existing_finsight() -> bool:
    """停止占用端口的旧 FinSight 服务。"""
    try:
        with urllib.request.urlopen(
            local_site_url("/api/system/version"),
            timeout=2,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("app") != "FinSight":
            return False
        request = urllib.request.Request(
            local_site_url("/api/system/shutdown"),
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except Exception:
            # 服务正在关自己的端口，连接被掐断是常态。
            # 原来这里冒到外层 except 直接 return False，等待循环一次都没跑，
            # 新进程就报「端口被其他程序占用」然后退出。
            pass
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.25)
            try:
                with socket.create_connection((LOCAL_PROBE_HOST, APP_PORT), timeout=0.3):
                    continue
            except OSError:
                return True
        return False
    except Exception:
        return False

def run_server() -> None:
    """启动 FinSight HTTP 服务。"""
    global SERVER_INSTANCE
    for attempt in range(2):
        try:
            server = ExclusiveThreadingHTTPServer((APP_HOST, APP_PORT), Handler)
            SERVER_INSTANCE = server
            # 上次进程被打断（直接关窗口/崩溃/半路停服）会留下「切片在库、向量没嵌」的洞，
            # 那种切片 RAG 只能走词面 ⇒ 起服即自查自补。非阻塞后台线程，
            # pending=0 时一个请求都不发，不重复烧 embedding。
            auto_backfill_pending_vectors("服务启动")
            if APP_HOST in {"0.0.0.0", "::", "[::]"}:
                print(f"FinSight Web: {local_site_url()}（对外已开放 {APP_PORT}，需自行配置防火墙与安全组）")
            else:
                print(f"FinSight Web: http://{APP_HOST}:{APP_PORT}")
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return
        except OSError as exc:
            port_in_use = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048
            if not port_in_use:
                raise
            if attempt == 0 and stop_existing_finsight():
                continue
            print(
                f"端口 {APP_PORT} 已被其他程序占用。"
                "请关闭占用该端口的程序后重新运行比赛.py。"
            )
            return
