"""回归件：报表跨页时口径必须「按文档顺序延续」（600208 的固化用例）。

历史 bug（P0）：中国财报每张报表都**跨页**，续页没有表标题。旧写法按「页」判口径，
于是 600208（衢州信安发展）2026 半年报：
  · p37「合并利润表」开头、**p38 无标题**却是合并利润表续页，`五、净利润 32,234,254.86`
    —— 整页被判「没有标题」丢掉，合并真值进不来；
  · p39「母公司利润表」、**p40 无标题**是母公司续页，`四、净利润 382,273,538.06`
    —— p40 页底有下一张表的标题「合并现金流量表」，旧写法拿它当整页口径，
    于是**母公司的净利润被当成合并口径入库**（库里 net 382,273,538.06 vs 归母 169,143,269.77，2.3 倍）。
修法：`extract_values(..., tables=...)` 里维护 `running`（上一张表的口径），
续页无标题就继承，进「附注」区就作废；`running` 为空时**跳过而不是猜**。

文档名：`600208_20260831_O1MM.pdf`（157 页 / 261 张表），另有摘要版 `…_摘要.pdf`（4 页）。
预期/实际（A=按页旧行为，B=按文档顺序延续）：
  net_profit     A=382273538.06(p40 母公司)  B=32234254.86(p38 合并) ✅
  total_profit   A=382273538.06(p40)          B=51304200.95(p38)   ✅
  total_assets   A=None                       B=94571189946.97(p34) ✅
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))

import data  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'} {name}{'' if condition else ' -> ' + detail}")
    if not condition:
        FAILURES.append(name)


def page(page_no: int, text: str) -> dict:
    return {"page_no": page_no, "text": text, "rows": [], "is_scanned": False}


def table(page_no: int, index: int, rows: list[list[str]]) -> dict:
    return {"page_no": page_no, "table_index": index, "headers": rows[0], "rows": rows[1:]}


CONSOLIDATED_INCOME_FIRST = [
    ["项目", "附注", "2026年半年度", "2025年半年度"],
    ["一、营业总收入", "", "384,509,845.65", "324,046,501.79"],
]
CONSOLIDATED_INCOME_CONT = [
    ["项目", "附注", "2026年半年度", "2025年半年度"],
    ["五、净利润（净亏损以“-”号填列）", "", "32,234,254.86", "164,087,683.70"],
    ["三、利润总额（亏损总额以“-”号填列）", "", "51,304,200.95", "199,321,905.62"],
]
PARENT_INCOME_FIRST = [
    ["项目", "附注", "2026年半年度", "2025年半年度"],
    ["一、营业收入", "", "384,509,845.65", "324,046,501.79"],
]
PARENT_INCOME_CONT = [
    ["项目", "附注", "2026年半年度", "2025年半年度"],
    ["四、净利润（净亏损以“-”号填列）", "", "382,273,538.06", "382,273,538.07"],
]
BALANCE_CONT = [
    ["项目", "附注", "期末余额", "期初余额"],
    ["资产总计", "", "94,571,189,946.97", "90,111,222,333.11"],
]
NOTES_TABLE = [
    ["项目", "2026年半年度"],
    ["货币资金", "1,234,567.89"],
]


def run_case(pages, tables):
    values, _previous = data.extract_values(pages, "", tables)
    return values


def main() -> int:
    # 1) 跨页续表继承上一张表的口径（合并利润表跨 p37+p38）
    pages = [
        page(37, "合并利润表\n" + "\n".join(" | ".join(row) for row in CONSOLIDATED_INCOME_FIRST)),
        page(38, "\n".join(" | ".join(row) for row in CONSOLIDATED_INCOME_CONT)),
    ]
    tables = [table(37, 1, CONSOLIDATED_INCOME_FIRST), table(38, 1, CONSOLIDATED_INCOME_CONT)]
    values = run_case(pages, tables)
    revenue = values.get("operating_revenue")
    net_profit = values.get("net_profit")
    total_profit = values.get("total_profit")
    check(
        "合并利润表跨页：营业收入取到",
        revenue is not None and str(revenue.current) == "384509845.65",
        str(revenue and revenue.current),
    )
    check(
        "合并利润表跨页：续页的净利润入库（600208 真值 32,234,254.86）",
        net_profit is not None and str(net_profit.current) == "32234254.86",
        str(net_profit and net_profit.current),
    )
    check(
        "合并利润表跨页：续页的利润总额入库",
        total_profit is not None and str(total_profit.current) == "51304200.95",
        str(total_profit and total_profit.current),
    )

    # 2) 母公司续页必须整张跳过，且不能被页底的下一张表标题「救」回来
    pages = [
        page(37, "合并利润表\n" + "\n".join(" | ".join(row) for row in CONSOLIDATED_INCOME_FIRST)),
        page(38, "\n".join(" | ".join(row) for row in CONSOLIDATED_INCOME_CONT)),
        page(39, "母公司利润表\n" + "\n".join(" | ".join(row) for row in PARENT_INCOME_FIRST)),
        page(
            40,
            "\n".join(" | ".join(row) for row in PARENT_INCOME_CONT)
            + "\n\n合并现金流量表\n经营活动产生的现金流量净额",
        ),
    ]
    tables = [
        table(37, 1, CONSOLIDATED_INCOME_FIRST),
        table(38, 1, CONSOLIDATED_INCOME_CONT),
        table(39, 1, PARENT_INCOME_FIRST),
        table(40, 1, PARENT_INCOME_CONT),
    ]
    values = run_case(pages, tables)
    net_profit = values.get("net_profit")
    check(
        "母公司续页的 382,273,538.06 不得入库（旧写法就是把它当合并口径）",
        net_profit is not None and str(net_profit.current) == "32234254.86",
        str(net_profit and net_profit.current),
    )

    # 3) 600208 的真实页序：p40 上半页是母公司利润表的续页（标题之前），下半页才是
    #    「合并现金流量表」的开头 ⇒ 标题之前的表继承母公司被跳过，标题之后的表按合并现金流入库。
    cash_first = [
        ["项目", "附注", "2026年半年度", "2025年半年度"],
        ["经营活动产生的现金流量净额", "", "1,234,567,890.12", "987,654,321.00"],
    ]
    pages = [
        page(37, "合并利润表\n" + "\n".join(" | ".join(row) for row in CONSOLIDATED_INCOME_FIRST)),
        page(38, "\n".join(" | ".join(row) for row in CONSOLIDATED_INCOME_CONT)),
        page(39, "母公司利润表\n" + "\n".join(" | ".join(row) for row in PARENT_INCOME_FIRST)),
        page(
            40,
            "\n".join(" | ".join(row) for row in PARENT_INCOME_CONT)
            + "\n\n合并现金流量表\n"
            + "\n".join(" | ".join(row) for row in cash_first),
        ),
    ]
    tables = [
        table(37, 1, CONSOLIDATED_INCOME_FIRST),
        table(38, 1, CONSOLIDATED_INCOME_CONT),
        table(39, 1, PARENT_INCOME_FIRST),
        table(40, 1, PARENT_INCOME_CONT),
        table(40, 2, cash_first),
    ]
    values = run_case(pages, tables)
    net_profit = values.get("net_profit")
    check(
        "一页两张表：标题之前的母公司续表不得入库",
        net_profit is not None and str(net_profit.current) == "32234254.86",
        str(net_profit and net_profit.current),
    )
    cash_values = {code: item for code, item in values.items() if str(item.current) == "1234567890.12"}
    check(
        "一页两张表：标题之后的合并现金流量表入库",
        bool(cash_values),
        "没取到经营活动现金流量净额",
    )
    check(
        "合并现金流量表的行归到 cash_flow_statement",
        all(item.statement_type == "cash_flow_statement" for item in cash_values.values()),
        str({code: item.statement_type for code, item in cash_values.items()}),
    )

    # 4) 资产负债表续页（无标题）继承合并口径 ⇒ 资产总计入库
    pages = [
        page(34, "合并资产负债表\n" + "\n".join(" | ".join(row) for row in [["项目", "期末余额"], ["货币资金", "1,000.00"]])),
        page(35, "\n".join(" | ".join(row) for row in BALANCE_CONT)),
    ]
    tables = [table(34, 1, [["项目", "期末余额"], ["货币资金", "1,000.00"]]), table(35, 1, BALANCE_CONT)]
    values = run_case(pages, tables)
    total_assets = values.get("total_assets")
    check(
        "资产负债表续页：资产总计入库",
        total_assets is not None and str(total_assets.current) == "94571189946.97",
        str(total_assets and total_assets.current),
    )

    # 5) 进附注区 ⇒ 上一张报表的口径作废，附注表不许被当报表数字灌进来
    pages = [
        page(35, "\n".join(" | ".join(row) for row in BALANCE_CONT)),
        page(36, "五、合并财务报表项目附注\n" + "\n".join(" | ".join(row) for row in NOTES_TABLE)),
        page(37, "货币资金\n" + "\n".join(" | ".join(row) for row in [["项目", "期末余额"], ["资产总计", "9,999,999.00"]])),
    ]
    tables = [
        table(35, 1, BALANCE_CONT),
        table(36, 1, NOTES_TABLE),
        table(37, 1, [["项目", "期末余额"], ["资产总计", "9,999,999.00"]]),
    ]
    values = run_case(pages, tables)
    total_assets = values.get("total_assets")
    check(
        "附注标题页作废口径：附注区之后的表不被继承灌入",
        total_assets is None or str(total_assets.current) == "94571189946.97",
        str(total_assets and total_assets.current),
    )

    # 6) 000525（红太阳）2026 半年报的真实版式：扫描件里**报表标题常落在表格片段之后**，
    #    同一页还有「上一页报表的尾巴表」。位置规则单独用会失手 ⇒ 必须配合「这张表自己的科目投票」。
    #    库里实测（合并）：资产总计 6,553,879,521.62 / 负债合计 3,841,929,227.38 / 所有者权益合计 2,711,950,294.24
    #    母公司实测（p47/p48 续页，整张**不许入库**）：资产总计 5,125,068,513.64 / 负债合计 2,196,195,636.00
    #    / 所有者权益合计 2,928,872,877.64
    bs_assets_first = [
        ["项目", "期末余额", "期初余额"],
        ["流动资产：", "", ""],
        ["货币资金", "1,940,094,645.70", "1,594,569,256.93"],
        ["流动资产合计", "1,940,094,645.70", "1,594,569,256.93"],
    ]
    bs_assets_cont = [
        ["项目", "期末余额", "期初余额"],
        ["非流动资产合计", "4,613,784,875.92", "4,761,583,249.04"],
        ["资产总计", "6,553,879,521.62", "6,356,152,505.97"],
    ]
    bs_liab_consolidated = [
        ["项目", "期末余额", "期初余额"],
        ["应付手续费及佣金", "1,000.00", "1,000.00"],
        ["流动负债合计", "3,170,437,218.81", "3,149,506,725.47"],
        ["非流动负债合计", "671,492,008.57", "651,535,132.03"],
        ["负债合计", "3,841,929,227.38", "3,801,041,857.50"],
        ["所有者权益合计", "2,711,950,294.24", "2,555,110,648.47"],
    ]
    bs_parent_start = [
        ["项目", "期末余额", "期初余额"],
        ["流动资产：", "", ""],
        ["结算备付金", "237,761,523.89", "190,701,205.38"],
    ]
    bs_parent_cont = [
        ["项目", "期末余额", "期初余额"],
        ["衍生金融资产", "10,000.00", "10,000.00"],
        ["资产总计", "5,125,068,513.64", "5,067,296,073.84"],
        ["负债合计", "2,196,195,636.00", "2,095,863,450.14"],
        ["所有者权益合计", "2,928,872,877.64", "2,971,432,623.70"],
    ]
    p46_text = "\n".join(" | ".join(row) for row in bs_liab_consolidated) + "\n母公司资产负债表\n" + "\n".join(
        " | ".join(row) for row in bs_parent_start
    )
    pages = [
        page(44, "合并资产负债表\n" + "\n".join(" | ".join(row) for row in bs_assets_first)),
        page(45, "\n".join(" | ".join(row) for row in bs_assets_cont)),
        page(46, p46_text),
        page(47, "\n".join(" | ".join(row) for row in bs_parent_cont)),
    ]
    tables = [
        table(44, 1, bs_assets_first),
        table(45, 1, bs_assets_cont),
        table(46, 1, bs_liab_consolidated),
        table(46, 2, bs_parent_start),
        table(47, 1, bs_parent_cont),
    ]
    values = run_case(pages, tables)
    for code, expected, forbidden, label in (
        ("total_assets", "6553879521.62", "5125068513.64", "资产总计"),
        ("total_liabilities", "3841929227.38", "2196195636.00", "负债合计"),
        ("total_equity", "2711950294.24", "2928872877.64", "所有者权益合计"),
    ):
        item = values.get(code)
        check(
            f"000525 版式：合并{label}取合并数、母公司数不得串入",
            item is not None and str(item.current) == expected,
            f"取到 {item and item.current}（母公司数 {forbidden} 串号）",
        )

    # 7) 附注条目「N、<已知科目>」也是附注起点：起点之后的表一律不收，
    #    而且附注区里的“伪报表标题”（实测 000525 p67 一页命中 8 个标题）不许把口径重新点亮。
    pages = [
        page(45, "合并资产负债表\n" + "\n".join(" | ".join(row) for row in bs_assets_cont)),
        page(72, "13、应收账款\n" + "\n".join(" | ".join(row) for row in [["项目", "期末余额"], ["应收账款", "637,502.92"]])),
        page(144, "合并资产负债表\n" + "\n".join(" | ".join(row) for row in [["项目", "期末余额"], ["资产总计", "9,999,999.00"]])),
    ]
    tables = [
        table(45, 1, bs_assets_cont),
        table(72, 1, [["项目", "期末余额"], ["应收账款", "637,502.92"]]),
        table(144, 1, [["项目", "期末余额"], ["资产总计", "9,999,999.00"]]),
    ]
    values = run_case(pages, tables)
    total_assets = values.get("total_assets")
    receivable = values.get("accounts_receivable")
    check(
        "附注条目页停收：之后的伪标题表不许覆盖资产总计",
        total_assets is not None and str(total_assets.current) == "6553879521.62",
        str(total_assets and total_assets.current),
    )
    check(
        "附注条目页停收：附注里的 637,502.92 不入库",
        receivable is None,
        str(receivable and receivable.current),
    )

    print()
    if FAILURES:
        print(f"结论：{len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("结论：跨页口径延续全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
