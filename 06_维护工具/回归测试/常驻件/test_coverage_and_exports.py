r"""钉住轮廿五「⑦ 功能」那一批的实测结论（引用点原文 / 导出 / SQL 复制 / 数据体检页）。

① 数据覆盖体检（`company_coverage_report` + `/api/coverage` + 「数据体检」页）：
   库是用户自己边删边导的，「某家只导进摘要级小文件」「有原文却没解析出数」这两类
   缺口在文件库里看不出来，等提问答不上来才暴露（实测：600310 曾只有 0.15MB 摘要，
   其余 53 家都是全文）。⇒ 体检必须**自洽**：summary 的数必须能从 rows 实算出来，
   旗子只允许 `bad`/`warn` 两级，有旗子的排前面。

② 导出与引用（全在 `05_Web服务\\web.py`）：回答可导出 MD / Word、表格可导出 Excel、
   SQL 日志一键复制、证据里的「打开原文」必须带 `#page=N`（`/api/documents/open/<id>`
   是 inline 返回 PDF 字节，浏览器阅读器认这个片段）。

③ 结构性守卫（本轮真踩过）：web.py 里 `</body>`/`</html>` **只允许出现在完整页面文档的
   末尾**（轮卅八起恰两处：主页 + LOGIN_HTML 登录页）。导出用的 HTML 字符串必须写成
   `<\/body>` —— 否则快照/注入类工具
   在 `</body>` 处插脚本时会插进 JS 字符串内部，把整段 `<script>` 截断，
   表现是页面全瘫：`Uncaught SyntaxError: Unexpected end of input` +
   `renderResult is not defined`（2026-10-29 实测）。
"""
from __future__ import annotations

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

import data  # noqa: E402

WEB_TEXT = (ROOT / "05_Web服务" / "web.py").read_text(encoding="utf-8")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" —— {detail}" if detail else ""))
        FAILURES.append(name)


print("[1] 体检报告自洽（summary 必须能从 rows 实算出来）")
try:
    report = data.company_coverage_report()
    error = ""
except Exception as exc:  # 建库/查询失败要看得见，不能装作体检通过
    report = {}
    error = f"{type(exc).__name__}: {exc}"
check("company_coverage_report 能跑完", not error, error)
rows = report.get("rows") or []
summary = report.get("summary") or {}
check("返回三段结构 generated_at/summary/rows",
      bool(report.get("generated_at")) and isinstance(summary, dict) and isinstance(rows, list))
if rows:
    check("summary.with_data == 实算有报告期的公司数",
          int(summary.get("with_data") or 0) == sum(1 for r in rows if r.get("periods")),
          f"summary={summary.get('with_data')} 实算={sum(1 for r in rows if r.get('periods'))}")
    check("summary.with_gaps == 实算有旗子的公司数",
          int(summary.get("with_gaps") or 0) == sum(1 for r in rows if r.get("flags")),
          f"summary={summary.get('with_gaps')} 实算={sum(1 for r in rows if r.get('flags'))}")
    check("summary.documents == 各家公司原文数之和",
          int(summary.get("documents") or 0) == sum(int(r.get("document_count") or 0) for r in rows),
          f"summary={summary.get('documents')} 实算={sum(int(r.get('document_count') or 0) for r in rows)}")
    check("summary.periods == 各行报告期的并集",
          sorted(summary.get("periods") or [])
          == sorted({p for r in rows for p in (r.get("periods") or [])}))
    check("有旗子的行排在前面（缺口要第一眼看见）",
          all((not r.get("flags")) or True for r in rows)
          and [bool(r.get("flags")) for r in rows]
          == sorted([bool(r.get("flags")) for r in rows], reverse=True))
    bad_level = [f for r in rows for f in (r.get("flags") or [])
                 if str(f.get("level")) not in {"bad", "warn"}]
    check("旗子只有 bad/warn 两级", not bad_level, f"越界旗子={bad_level[:2]}")
    miss_key = [r.get("stock_code") for r in rows
                if not {"stock_code", "company_name", "document_count",
                        "largest_document_mb", "periods", "tables", "flags"} <= set(r)]
    check("每行字段齐全", not miss_key, f"缺字段={miss_key[:3]}")
    # 用户点过名的坑（000568 泸州老窖）：档案里有名字、库里啥都没有的公司
    # 不许出现在体检名单里 —— 「没有财报原文」对它们毫无意义，只会把真缺口淹掉。
    empty = [r for r in rows if int(r.get("document_count") or 0) == 0
             and not (r.get("periods") or [])]
    check("库里既没原文又没数据的公司不进体检名单", not empty,
          f"混进名单={[(r['stock_code'], r['company_name']) for r in empty[:2]]}")
    check("summary.in_library == 名单行数",
          int(summary.get("in_library") or 0) == len(rows),
          f"summary={summary.get('in_library')} rows={len(rows)}")
    check("summary.registry_only 是个非负数（没导入过的家数，只计数不摆行）",
          int(summary.get("registry_only") or 0) >= 0,
          f"registry_only={summary.get('registry_only')}")
    lost_doc = [r for r in rows if int(r.get("document_count") or 0) == 0 and (r.get("periods") or [])]
    check("有数据但原文已被删光的单独标出来（引用/风险题会做不了）",
          all(any("库里已没有它的原文" in str(f.get("text")) for f in (r.get("flags") or [])) for r in lost_doc),
          f"命中 {len(lost_doc)} 家")
    thin = [r for r in rows if int(r.get("document_count") or 0) > 0
            and float(r.get("largest_document_mb") or 0) < 1.0]
    check("最大原文不足 1MB 的被判成摘要级（600310 那类）",
          all(any("疑似摘要级" in str(f.get("text")) for f in (r.get("flags") or [])) for r in thin),
          f"命中 {len(thin)} 家，抽样={[(r['stock_code'], r['largest_document_mb']) for r in thin[:2]]}")
    alone = [r for r in rows if len(r.get("periods") or []) == 1]
    check("只有一个报告期的公司被提示做不了趋势/同比",
          all(any("只有一个报告期" in str(f.get("text")) for f in (r.get("flags") or [])) for r in alone),
          f"命中 {len(alone)} 家")
    parsed = [r for r in rows if int(r.get("document_count") or 0) > 0 and not (r.get("periods") or [])]
    check("有原文但没解析出数据的被单独标出来",
          all(any("没解析出" in str(f.get("text")) for f in (r.get("flags") or [])) for r in parsed),
          f"命中 {len(parsed)} 家")
else:
    print("  SKIP 库里一家公司都没有（数据面缺席），只验结构不验内容")

print()
print("[2] 体检页前后端接线")
check("后端函数名在 data.py 里", "def company_coverage_report" in
      (ROOT / "03_数据层" / "data.py").read_text(encoding="utf-8"))
check("路由 /api/coverage 已挂上", '"/api/coverage"' in WEB_TEXT)
check("导航按钮「数据体检」", 'data-page="coverage"' in WEB_TEXT)
check("页面容器 page-coverage", 'id="page-coverage"' in WEB_TEXT)
check("切页时自动体检", 'if (pageName === "coverage") loadCoverage();' in WEB_TEXT)
check("体检标题进 pageTitles", re.search(r'coverage:\s*"数据体检"', WEB_TEXT) is not None)
check("行样式 cov-row + 旗子分级 cov-flag.bad",
      ".cov-row {" in WEB_TEXT and ".cov-flag.bad" in WEB_TEXT)

print()
print("[3] 引用点原文 / 导出 / SQL 复制")
check("证据里有「打开原文」按钮", 'class="ev-open"' in WEB_TEXT)
check("数字来源也能「看这一页」", "看这一页" in WEB_TEXT)
check("打开时带 #page= 片段（PDF 阅读器直接翻到那页）", "#page=${page}" in WEB_TEXT)
check("按钮回退文案按原始文本恢复（不写死「打开原文」）", "const original = button.textContent;" in WEB_TEXT)
check("回答导出 MD", 'data-action="md"' in WEB_TEXT and "function answerMarkdown" in WEB_TEXT)
check("回答导出 Word", 'data-action="word"' in WEB_TEXT and "function answerWordHtml" in WEB_TEXT)
check("Word 导出后端化：真 .docx + A4 纵向 + 三线表 + 表头跨页",
      '"/api/export/docx"' in WEB_TEXT and "def build_answer_docx" in WEB_TEXT
      and "WD_ORIENT.PORTRAIT" in WEB_TEXT and '"w:tblHeader"' in WEB_TEXT)
check("MD/Word 共用同一份内容结构", "function answerSections" in WEB_TEXT)
check("表格导出 Excel", "function tableExcelHtml" in WEB_TEXT and "下载表格 Excel" in WEB_TEXT)
check("CSV 文件名跟着问题走（不再固定 financial_result.csv）",
      'link.download = exportFileName(result, "csv");' in WEB_TEXT)
check("SQL 日志复制按钮", 'id="sqlLogCopy"' in WEB_TEXT and "lastSqlLogs" in WEB_TEXT)
check("Word/Excel 导出带 BOM（老 Office 认 UTF-8）", WEB_TEXT.count('"\\ufeff"') >= 2)

print()
print("[4] 结构守卫：JS 里不许出现裸 </body>、</html>")
# 轮卅八起 web.py 内有两份完整页面文档：主页 + LOGIN_HTML，各含一套闭合标签。
check("web.py 全文 </body> 恰两处（主页+登录页各一）",
      WEB_TEXT.count("</body>") == 2, f"实际 {WEB_TEXT.count('</body>')} 次")
check("web.py 全文 </html> 恰两处（主页+登录页各一）",
      WEB_TEXT.count("</html>") == 2, f"实际 {WEB_TEXT.count('</html>')} 次")
check("导出用的闭合标签写成 <\\/body> 转义",
      WEB_TEXT.count("<\\/body>") >= 2, f"实际 {WEB_TEXT.count(chr(60) + chr(92) + '/body>')} 次")

print()
print("[5] 推荐题跟着历史偏好（只改顺序、不塞新问题）")
POOL = [
    "三一重工 2026年半年报 营业收入和净利润",
    "云南白药 2026年半年报 营业收入和净利润",
    "对比 云南白药 和 三一重工 的毛利率",
    "库里营收最高的前 5 家是谁",
    "云南白药 2026年半年报里提到的主要经营风险",
]
check("空池子返回空列表", data.rank_recommended_questions([], None) == [])
BIASED = {
    "codes": {"000538": 9},
    "intents": {"原文风险": 4},
    "periods": {"2026年半年报": 3},
    "names_by_code": {"000538": {"云南白药", "云南白药集团股份有限公司"}},
}
runs = [data.rank_recommended_questions(POOL, BIASED) for _ in range(20)]
check("排序前后题目一模一样（一题不丢、一题不加）",
      all(sorted(out) == sorted(POOL) for out in runs))
check("没有偏好也不崩、题目一条不少",
      all(sorted(data.rank_recommended_questions(POOL, None)) == sorted(POOL) for _ in range(5)))
# 云南白药 +4、风险意图 +2、2026年半年报 +1 ⇒ 那条题至少 +7，而随机抖只有 0~1
check("偏好指向的题稳定排在最前（不是纯随机）",
      all(out[0] == "云南白药 2026年半年报里提到的主要经营风险" for out in runs),
      f"实测首位分布={sorted({out[0] for out in runs})}")
check("同分之间是真随机（两次排序结果不完全一样）",
      len({"|".join(data.rank_recommended_questions(POOL, None)) for _ in range(12)}) > 1)

saved_sessions = list(data.CHAT_SESSIONS)
try:
    data.CHAT_SESSIONS[:] = [{
        "id": "probe-session",
        "title": "探针",
        "messages": [
            {"role": "user", "content": "云南白药 2026年半年报 营业收入和净利润同比"},
            {"role": "assistant", "content": "这段不该进偏好"},
        ],
        "analysis_context": {"last_question": "云南白药 2026年半年报 经营风险", "company_ticker": "000538"},
    }]
    prefs = data.recent_question_preferences()
    check("能从历史里认出提问条数", int(prefs.get("samples") or 0) >= 2, f"samples={prefs.get('samples')}")
    intents = prefs.get("intents") or {}
    check("意图识别命中营收利润/同比增速/原文风险",
          {"营收利润", "同比增速", "原文风险"} <= set(intents), f"intents={sorted(intents)}")
    check("assistant 的话术不算偏好（只认 user + last_question）",
          "这段不该进偏好" not in str(prefs), "prefs 里混进了回答文本")
    periods = set(prefs.get("periods") or {})
    # 钉死那个正则坑：后缀必须长优先，否则「2026年半年报」被截成「2026年报」
    check("报告期正则不被截断（2026年半年报 完整识别）",
          "2026年半年报" in periods and "2026年报" not in periods, f"periods={sorted(periods)}")
    codes = prefs.get("codes") or {}
    if "000538" in (prefs.get("names_by_code") or {}):
        check("常问的公司被认出来（代码或名称任一命中）", "000538" in codes, f"codes={sorted(codes)[:6]}")
    else:
        print("  SKIP 000538 不在库里（数据面缺席），代码偏好这条不检查")
finally:
    data.CHAT_SESSIONS[:] = saved_sessions
check("历史为空时返回空信号（退回纯随机，不报错）",
      (lambda: (data.CHAT_SESSIONS.clear(), data.recent_question_preferences().get("samples") == 0))())

print()
print("[6] 布局移动 + Word 版式 + 体检只列库内有的")
DATA_TEXT = (ROOT / "03_数据层" / "data.py").read_text(encoding="utf-8")
check("底部标题改成「试试问」", 'class="suggest-title">试试问<' in WEB_TEXT)
check("顶部那张可点问题区已撤掉（不再和底部重复）",
      '<div class="ov-asks">' not in WEB_TEXT and "OVERVIEW_ASKS" not in WEB_TEXT)
check("换一批的随机取法没动（仍是 3 个 chip）", "pickSuggestions" in WEB_TEXT)
check("Word 版式常量在位（页边距/行距/段距）",
      "const WORD_STYLE" in WEB_TEXT and "@page WordSection1" in WEB_TEXT
      and "line-height: 1.75" in WEB_TEXT and "margin: 2cm" in WEB_TEXT)
check("长段落会被切开再写进 Word", "function wordParagraphs" in WEB_TEXT)
check("Word 正文包在 WordSection1 里（否则 @page 不生效）",
      'class="WordSection1"' in WEB_TEXT)
check("Word 不再用老式 <font size> 排版",
      "<font size=" not in WEB_TEXT.split("function answerWordHtml")[1].split("function tableExcelHtml")[0])
check("体检页把「没导入过」和「有缺口」分开说", "registry_only" in WEB_TEXT and "不算缺口" in WEB_TEXT)
check("体检代码里确实是按 in_library/registry_only 分流",
      DATA_TEXT.count("registry_only") >= 4)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: " + " | ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
