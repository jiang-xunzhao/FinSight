"""回归集样本清单：历史 bug 文档 + 分层典型样本 + 已核验的期望值（oracle）。

这里是**唯一一份**真值登记表：验证件一律 `from samples import ORACLE`，不许在自己文件里
再抄一遍数字（本会话我三次把 oracle 记错过，代价是三轮返工）。

期望值的来源必须是文档自身可核验的关系，不是我的印象：
- 同一行「比上年同期增减」反算：本期 / (1+yoy) 必须等于上期，单位不一致时这个等式差一万倍；
- 资产负债表恒等式：total_assets = total_liability + total_equity；
- 利润勾稽：net_profit = net_profit_parent + 少数股东损益（库里没有少数股东列，只能回原文）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------- 表列名（实盘 information_schema 校过）
PERF_COLUMNS = ["revenue", "net_profit", "net_profit_parent", "eps", "revenue_yoy", "profit_yoy"]
INCOME_COLUMNS = [
    "operating_revenue", "operating_cost", "operating_profit",
    "total_profit", "net_profit", "net_profit_parent", "basic_eps",
]
BALANCE_COLUMNS = ["total_assets", "total_liability", "total_equity", "current_assets", "current_liability"]
CASH_COLUMNS = ["operate_cash", "invest_cash", "finance_cash", "cash_increase"]

# ---------------------------------------------------------------- 历史 bug 文档（回归必须继续拦住）
# key = 7_document_index.file_name 的可用子串；每条都是**真出过事故**的样本。
HISTORIC_BUG_DOCS: list[dict[str, Any]] = [
    {
        "file_needle": "美心翼申",
        "stock_code": "920833",
        "bug": "扫描件重大资产购买重组件被当定期报告入库，且取到备考「交易后」列",
        "expect": "四张事实表 0 行（只登记文档与切片）",
        "check": "nonperiodic_no_fact",
        "severity": "P1",
    },
    {
        "file_needle": "招商证券",
        "stock_code": "600999",
        "bug": "持仓股代码串号：920833 的 3 份文档挂到招商证券名下",
        "expect": "600999 只有 1 个主体、切片身份与文档索引一致",
        "check": "code_name_match",
        "severity": "P0",
    },
    {
        "file_needle": "达安基因",
        "stock_code": "002030",
        "bug": "正文「2030年半年度」把报告期写在未来（文件名无日期时披露日退化成今天）",
        "expect": "报告期年不得大于文件名年份；未来报告期 0 行",
        "check": "no_future_period",
        "severity": "P1",
    },
    {
        "file_needle": "衢州信安发展股份有限公司_2026年半年报",
        "stock_code": "600208",
        "bug": "报表跨页：合并利润表 p37→p38 续页无标题被整页丢弃，母公司利润表 p39→p40 的续页却因 p40 页底有「合并现金流量表」标题被判成合并口径 ⇒ 净利润取了母公司数（口径串号）",
        "expect": "net_profit 取合并口径 32,234,254.86，不取母公司 382,273,538.06；total_assets 94,571,189,946.97 不再丢失",
        "check": "oracle_value",
        "severity": "P0",
    },
    {
        "file_needle": "岩山科技",
        "stock_code": "002195",
        "bug": "2024 年报归母 440,102,807.82 其实是「其中：营业成本」；净利润又取了「六、综合收益总额」",
        "expect": "2024年报 net_profit_parent = 32,461,729.28",
        "check": "oracle_value",
        "severity": "P0",
    },
    {
        "file_needle": "莱美药业",
        "stock_code": "300006",
        "bug": "按「0 文档 0 事实=噪音」删了真实代码，导致简称对比从 2 家塌成 1 家",
        "expect": "300006 在 1_stock_basic 存在且名为重庆莱美药业股份有限公司（改名不删行）",
        "check": "entity_exists_named",
        "severity": "P0",
    },
    {
        "file_needle": "红太阳：2026年半年度报告",
        "stock_code": "000525",
        "bug": "两处叠加：①附注区「13、应收账款」起的分部列（万元）被当合并营业收入灌入（637,502.92）；"
        "②合并利润表 p48 的标题落在页内表格片段**之后**，位置规则失手 ⇒ 继承上一页「母公司资产负债表」口径 ⇒ 整张合并利润表被跳过（net_profit 变 None）",
        "expect": "operating_revenue = 1,562,045,738.41（摘要元值），net_profit = 104,095,143.10，负债合计 = 3,841,929,227.38（p46 合并）",
        "check": "oracle_value",
        "severity": "P0",
    },
    {
        "file_needle": "融发核电：2026年半年度报告",
        "stock_code": "002366",
        "bug": "同一形状：附注 p96 的「营业收入 46,017.73」被当合并营业收入（库里就是这条错值）",
        "expect": "operating_revenue = 361,904,468.86（摘要元值），net_profit = -27,213,722.92",
        "check": "oracle_value",
        "severity": "P0",
    },
    {
        "file_needle": "三一重工股份有限公司_2026年半年报",
        "stock_code": "600031",
        "bug": "「单位：千元」只写在报表**首页**，续页没有声明 ⇒ 旧写法按页取倍数，同一张表里收入乘了 1000、净利润/总资产没乘（少 1000 倍）",
        "expect": "net_profit = 5,724,043,000、total_assets = 181,152,782,000（都是千元口径 ×1000）",
        "check": "oracle_value",
        "severity": "P0",
    },
    {
        "file_needle": "长安汽车",
        "stock_code": "000625",
        "bug": "「六、综合收益总额」被当成「五、净利润」入库 ⇒ 净利润 2,037,636,368.25（比归母还大 2.5 倍）",
        "expect": "net_profit = 180,714,267.62 = 归母 817,395,511.85 + 少数股东损益 -636,681,244.23（勾稽精确）",
        "check": "oracle_value",
        "severity": "P0",
    },
    {
        "file_needle": "同仁堂",
        "stock_code": "600085",
        "bug": "两处叠加：①权益合计写作「归属于母公司所有者权益（或股东权益）合计」⇒ 归一化去掉「（或股东权益）」后"
        "长子串失效、短子串「股东权益合计」抢走 ⇒ 所有者权益合计取成归母 13,623,250,166.47（恒等式差 75 亿）；"
        "②合并现金流量表的「四、汇率变动对现金及现金等价物的影响」被分页切成三行（标签行无金额、金额单独成行、"
        "标签残尾「影响」在第三行）⇒ 汇率项整行丢失 ⇒ 经营+投资+筹资比净增加额少 79,697,897.58",
        "expect": "total_equity = 21,158,327,481.42，且 8,787,685,375.30 + 21,158,327,481.42 = 29,946,012,856.72 = total_assets（恒等式精确）",
        "check": "oracle_value",
        "severity": "P1",
    },
    {
        # 库里登记名是「中国国际贸易中心股份有限公司_2026年半年报.pdf」（实测 query 7_document_index）
        # ⇒ needle 必须用「中国国际贸易中心」，写简称「中国国贸」会 0 命中、误报成「文档已不在库里」。
        "file_needle": "中国国际贸易中心",
        "stock_code": "600007",
        "bug": "①合并利润表那行 OCR 吃掉顿号 ⇒ 行首粘成「四净利润」，位置护栏认不出行首序号 ⇒ 真行被跳过；"
        "②「所有者权益变动表」里也有一行「净利润」（只有 136,256 / 185,213 千元口径的变动额），旧代码把标题当普通行首匹配、"
        "没有按报表块判 ⇒ 串号入库 ⇒ profit_yoy 算出 187200.4271%，而摘要写 0.17%",
        "expect": "net_profit = 633,844,810（上期 632,708,818），profit_yoy ≈ 0.18%（摘要 0.17%）",
        "check": "oracle_value",
        "severity": "P2",
    },
]

# ---------------------------------------------------------------- 已核验 oracle（逐值断言用）
# 全部来自英文版第 7 页摘要表（合并口径、单位=元）或恒等式反算，见各条 note。
ORACLE: dict[str, dict[str, Any]] = {
    "000100": {
        "note": "TCL科技：主表单位 RMB'0,000，摘要表元；负债/权益靠恒等式反解 ×1000",
        "revenue": Decimal("88648186929"),
        "total_assets": Decimal("365144968892"),
        "total_liability": Decimal("237525394000"),
        "total_equity": Decimal("127619575000"),
        "net_profit_parent": Decimal("3808272967"),
        # 利润列与归母量级差 >100× 且无法验恒等式 ⇒ 按设计丢弃，不是缺陷
        "expect_dropped": ["net_profit", "operating_profit"],
    },
    "000568": {
        "note": "泸州老窖：主表即元；18,442,336,591.97+45,882,913,659.10=64,325,250,251.07",
        "revenue": Decimal("10472224575.78"),
        "total_assets": Decimal("64325250251.07"),
        "net_profit": Decimal("4376950333.58"),
        "net_profit_parent": Decimal("4339264086.33"),
    },
    "000858": {
        "note": "五粮液：主表即元；恒等式精确成立",
        "revenue": Decimal("28416674541.77"),
        "total_assets": Decimal("184999362605.88"),
        "net_profit_parent": Decimal("8752942991.31"),
        "total_equity": Decimal("120944389687.82"),
    },
    "002384": {
        "note": "东山精密：主表即元",
        "revenue": Decimal("27797683972.88"),
        "total_assets": Decimal("69167411001.34"),
        "net_profit_parent": Decimal("2956898811.04"),
    },
    "002493": {
        "note": "荣盛石化：parent=1,294,767,235.20 由 2026H1 的 -64.20% 反算核实",
        "revenue": Decimal("129406713606.78"),
        "total_assets": Decimal("402822539719.62"),
        "net_profit_parent": Decimal("5110652552.96"),
    },
    "002508": {
        "note": "老板电器：归母原文折行三行，靠 pending 接续取到",
        "revenue": Decimal("3972387199.38"),
        "total_assets": Decimal("16551968719.72"),
        "net_profit": Decimal("575615406.22"),
        "net_profit_parent": Decimal("578235101.90"),
        "total_liability": Decimal("4869520524.97"),
        "total_equity": Decimal("11610674719.67"),
    },
    "600208": {
        "note": "衢州信安（600208）合并真值在 p38「五、净利润」，母公司数在 p40",
        "net_profit": Decimal("32234254.86"),
    },
    "002195": {
        "report_period": "2024年年报",
        "note": "2024年报归母原文 p16 + p7 摘要表两处一致",
        "net_profit_parent": Decimal("32461729.28"),
    },
    "000525": {
        "note": "红太阳：营业收入用摘要元值当 oracle；负债/权益来自 p46 合并资产负债表，恒等式精确成立"
        "（3,841,929,227.38 + 2,711,950,294.24 = 6,553,879,521.62 = 资产总计）",
        "operating_revenue": Decimal("1562045738.41"),
        "revenue": Decimal("1562045738.41"),
        "net_profit": Decimal("104095143.10"),
        "total_assets": Decimal("6553879521.62"),
        "total_liability": Decimal("3841929227.38"),
        "total_equity": Decimal("2711950294.24"),
    },
    "002366": {
        "note": "融发核电：营业收入用摘要元值当 oracle；恒等式 2,670,549,883.62 + 5,109,080,320.68 = 7,779,630,204.30 精确成立",
        "operating_revenue": Decimal("361904468.86"),
        "revenue": Decimal("361904468.86"),
        "net_profit": Decimal("-27213722.92"),
        "total_assets": Decimal("7779630204.30"),
        "total_liability": Decimal("2670549883.62"),
        "total_equity": Decimal("5109080320.68"),
    },
    "600031": {
        "note": "三一重工：主表声明「单位：千元」只写在首页 ⇒ 这三个数是 ×1000 后的千元口径真值（旧库里少乘 1000）。"
        "total_equity 必须是 **93,195,609 千元（股东权益合计，含少数股东）**，不是 92,084,227 千元 —— 后者是上一行"
        "「归属于母公司所有者权益（或股东权益）合计」；p42 实测两行都在（「（或股东权益）合计 92,084,227 88,331,244」"
        "与「股东权益合计 93,195,609 89,431,397」），取含少数那一行才与恒等式 负债+权益=资产总计 181,152,782 千元 一致。"
        "此前这条 oracle 误写成归母值 ⇒ run_layers 报 P0（缺陷在测试件、不在解析，已用只读探针 why_equity_changed.py 取证）",
        "operating_revenue": Decimal("53506463000"),
        "revenue": Decimal("53506463000"),
        "net_profit": Decimal("5724043000"),
        "total_assets": Decimal("181152782000"),
        "total_equity": Decimal("93195609000"),
    },
    "000625": {
        "note": "长安汽车：净利润必须等于归母 817,395,511.85 + 少数股东损益 -636,681,244.23（勾稽精确）；"
        "旧库里的 2,037,636,368.25 是「六、综合收益总额」串号",
        "net_profit": Decimal("180714267.62"),
        "net_profit_parent": Decimal("817395511.85"),
    },
    "600085": {
        "note": "同仁堂：权益合计原写成「归属于母公司所有者权益（或股东权益）合计」，归一化去掉「（或股东权益）」"
        "后长子串失效、短子串「股东权益合计」抢走 ⇒ 旧库取成归母 13,623,250,166.47；恒等式 "
        "8,787,685,375.30 + 21,158,327,481.42 = 29,946,012,856.72 = 资产总计（精确）",
        "total_assets": Decimal("29946012856.72"),
        "total_liability": Decimal("8787685375.30"),
        "total_equity": Decimal("21158327481.42"),
    },
    "600007": {
        "note": "中国国贸：合并利润表那行 OCR 吃掉顿号 ⇒ 行首粘成「四净利润」挡住真行；权益变动表里的"
        "「净利润」行（136,256 / 185,213）串号后算出 profit_yoy=187200.4271%，"
        "而摘要写 0.17%（633,844,810 vs 632,708,818 = +0.1794%）",
        "net_profit": Decimal("633844810"),
        "profit_yoy": Decimal("0.18"),
    },
}

# ---------------------------------------------------------------- 分层样本选择器
# 每层只测「选中的样本」，不做全库深度解析（全库只跑只读 SQL 不变量）。
LAYER_SAMPLES: dict[str, list[str]] = {
    # L1 核心报表勾稽：口径完整、数字能互相验证的公司
    # L1 核心报表勾稽：口径完整、数字能互相验证的公司（600031 也在此：它的逐值 oracle 是「单位声明只在首页」那条 P0）
    "core_reports": ["000100", "000568", "000858", "002384", "002493", "002508", "600208", "002195", "600085", "001227",
                     "000525", "002366", "000625", "600031", "600007"],
    # L2 格式：字段格式/清洗/精度类历史事故
    "format": ["002030", "600115", "003816", "002508"],
    # L3 异常：非定期、串号、隔离、孤儿、条数漂移
    "anomaly": ["920833", "600999", "FILE_4080B9E7", "FILE_E664CE98", "300006"],
    # L4 边界：单位声明错位、恒等式反解、极小/极大值、只有摘要版文档
    "boundary": ["000100", "002493", "600008", "002571", "600031", "000932"],
}
