# FinSight 回归测试集

这个目录是**项目内的常驻回归集**，不是临时脚本堆。原则（用户 2026-10-26 定的方法论）：

1. **先全量冒烟导入**，再按 **核心报表 / 格式 / 异常 / 边界** 四层深测，重点核对**字段**与**金额勾稽**。
2. 每个 bug 必须给：**文档名、复现步骤、预期/实际、日志、严重级别**。
3. 每轮输出**覆盖矩阵**。
4. 历史 bug 涉及的文档与典型样本**固化成回归集**，放在项目里（就是本目录）。

## 目录

| 路径 | 作用 |
|---|---|
| `layers.py` | 四层深测的检查项实现（全部**只读**，不写库、不改文件） |
| `run_layers.py` | 跑层 → 产出 `报告/缺陷清单_<日期>_<时间>.md` + `覆盖矩阵_*.md`（历史不覆盖） |
| `samples.py` | 分层样本登记表 + 已核验 oracle（逐值断言的唯一真值来源） |
| `常驻件/` | 逐主题回归件（闸门、身份、单位、口径、路由、HTTP、端到端…） |
| `报告/` | 每轮深测的缺陷清单与覆盖矩阵（带时间戳，只增不删） |

## 怎么跑

用项目 venv 的 python（系统 python 没有 pandas）：

```powershell
$py = "C:\Users\35999\PyCharmMiscProject\.venv\Scripts\python.exe"

# ① 四层深测（只读，服务可不停）
& $py "06_维护工具\回归测试\run_layers.py"
& $py "06_维护工具\回归测试\run_layers.py" --only L1 L3   # 只跑某几层

# ② 常驻件全量回归（约 7 分钟，需要服务在跑；**唯一入口 = 本目录 `run_all.py`**）
& $py "06_维护工具\回归测试\run_all.py"
& $py "06_维护工具\回归测试\run_all.py" --quick          # 跳过建临时库/端到端等重件
& $py "06_维护工具\回归测试\run_all.py" --only gate      # 只跑名字含 gate 的件
& $py "06_维护工具\回归测试\run_all.py" --layers         # 常驻件跑完再跑四层深测
& $py "06_维护工具\回归测试\常驻件\<某件>.py"              # 单件

# ③ 全量冒烟导入（≈55-65 分钟，**必须先停服务**：内嵌 Qdrant 独占文件锁）
python finsight_launch.py --stop
& $py "C:\Users\35999\Desktop\FinSight财报智析\_tools\reimport_all_documents.py"
python finsight_launch.py --restart
```

退出码约定：`0` 全通过；`1` 有 FAIL/有 P0-P1 缺陷；`2` 件自身跑崩。

## 严重级别定义

| 级别 | 含义 | 判定动作 |
|---|---|---|
| **P0** | 数据丢失、身份挂错主体、**口径串号**（母公司/合并/附注的数混进同一行）、整表为空 | 必须修，修完重导入 + 重跑本层 |
| **P1** | 金额或字段值错（取错列、单位倍率错、报告期错、路由错科目） | 必须修，同上 |
| **P2** | 量级/勾稽可疑但未定性（需回原文才能判） | 先取证（只读探针打原文行），定性后升级为 P0/P1 或写清不修理由 |
| **P3** | 展示/文案/统计口径问题 | 记录，可延后 |

「未定性」不等于「不报告」：P2 项一律进缺陷清单，附文档名与原文行号。

## 四层各查什么

- **L1 核心报表勾稽**：`资产 = 负债 + 权益`、`净利润 = 归母 + 少数股东损益`、利润表列齐套、逐值 oracle（对文档原文真值）。
- **L2 格式**：报告期标签正则、坏公司名（OCR 粘数字/砍头）、表格展示、列名与建表一致、SQL 与键。
- **L3 异常**：未来报告期、FILE_ 终态、改名不删行、切片身份 vs 文档索引（第三真相）、向量条数漂移、非定期报告写事实、定期报告零事实、**校验结论覆盖（有事实必须有结论）**、**陈旧校验结论（无事实不得留结论）**、**同 (代码,期间,文件名) 重复导入**。
- **L4 边界**：单位声明 vs 实测量级、恒等式反解残留、极小值/越界（>2 万亿）、现金流三列和与净额差。

## 已固化的历史 bug（回归集必须一直拦住它们）

| 样本 | 曾经的症状 | 现在拦它的件 |
|---|---|---|
| `002030 达安基因` | 正文「2030年半年度」把报告期写在未来 | `test_report_period_label.py` + L3 未来报告期 |
| `920833 美心翼申`（3 份招商证券文档） | 正文抽到的代码盖过文件名 → 错挂到招商证券 | `test_f1_stock_code.py`、`test_identity_hygiene.py`、L3 |
| `600208 衢州信安` | 报表跨页：合并利润表续页无标题被整页丢弃，母公司续页因页底下一张表标题被判成合并 → 净利润取母公司数 | `test_running_scope.py` + `test_percent_and_scope_guards.py` + L1 oracle |
| `002195 禾元` | 归母 440,102,807.82 其实是「其中：营业成本」；净利润取了「六、综合收益总额」 | `test_percent_and_scope_guards.py`（折行接续看下一行）+ L1 |
| `600999 招商证券` | 14 个碎片主体 | `test_company_aliases.py`、L3 |
| `300006 莱美药业` | 按「0 文档 0 事实=噪音」删掉真实代码 → 简称对比从 2 家塌成 1 家 | `test_identity_hygiene.py`、L3 改名不删行 |
| `001227 / 600085 / 600115` | 单位声明与金额行不同行 → 倍数全丢（4,179,417 应为 41.79 亿） | `test_data_period_and_cost.py`、L4 |
| 英文版 6 家（000100/000568/000858/002384/002493/002508） | 只有英文摘要表 → 库里零事实 | `test_english_labels.py`、`test_balance_unit_recovery.py` |
| `920833 / 泰州两家`（重组件、审计报告） | 临时公告的数被当定期财务事实 | `test_nonperiodic_no_overwrite.py`、L3 判型 |
| 入库闸门 | 不合格文档静默挂错键 | `test_ingest_gate.py`（一次性库真跑 save） |
| 别名缓存 | 先写凭证后写载荷 → 永不自愈的错配对 | `test_alias_cache_race.py` |
| `IMPORT_JOBS` | 只插不删 | `test_import_jobs_pruning.py` |
| 对比混合口径 | 无共同期时最后一个指标的报告期定义整行 | `test_comparison_mixed_period.py` |
| `000932 华菱钢铁` | 合并净利润远大于归母（2.53×）被当成解析串号 | 已取证为**真实少数股东损益**（子公司亏损）⇒ L1 倍率判据只查「合并 ≫ 归母」，`samples.py` 留 note |
| `600085 同仁堂 / 600007 中国国贸` | 「归属于母公司所有者权益（或股东权益）合计」被短别名 `股东权益合计` 抢走；权益变动表里的「净利润」行两列取数 → `profit_yoy` 187200.4271% | `test_cash_forex_and_truncated_label.py`、`test_equity_statement_scope.py` + L1 oracle |
| `600031 三一重工` | 现金流配不平，差额 `-768,603,000` = 库里**没有列存汇率变动项**（不是解析错） | `fx_cash` 八触点 + `test_cash_forex_and_truncated_label.py`、L4 四段配平 |
| 校验结论生命周期 | 摘要盖完整版那次 `kept` ⇒ 整组结论被删光不回填（18 个事实行无结论）；重导不清 `9_validation_results` ⇒ 56 行陈旧「异常」 | `test_validation_rows_lifecycle.py`（一次性库真跑 save）、L3 |
| 重复导入 | `920833` 27 份、`600999` 15 份同名不同字节的截断件全在库（44 行、2059 切片） | `_tools\dedupe_documents.py`（判据=同 `(代码,期间,文件名)` 保留切片最多者）、L3 |
| **空 `local_dir` 误删原文** | `POST /api/import/start {}` 把**服务进程当前目录**当导入目录扫，重复分支按「路径像托管副本」就 `unlink` ⇒ 实测删掉 **25 份已入库文档的原文**（库行/切片/向量都在，坏的是「点开文档」和「以后重导」） | `常驻件\test_import_delete_safety.py`（删前必须查引用 + 入参必须绝对路径且不含托管根）、`常驻件\test_http_guards.py`、L3「文档原文丢失」判据、恢复件 `_tools\restore_missing_uploads.py` |

## 写新回归件的规矩

- 放 `常驻件/`，文件名 `test_*.py`，**只读**或只写一次性库（跑完必须 DROP）。
- 断言只能钉**真值**：真值来自 `samples.py` 的 oracle（已核验）或文档原文，**不许在两个文件各记一遍同一个数**。
- 合成用例的页内文本顺序必须**复刻真实版式**（否则测的是构造而不是机制）。
- 取证走 `6_rag_chunks` 已存文本 + 纯函数，别逐文档重跑 OCR（138 份全量 OCR 是一小时的活）。
- 建/DROP 临时库的件**绝不并发**跑（串台会产生假 FAIL）。
- 改 Python 一律用编辑器/`edit`，不要用 PowerShell `.Replace()+Set-Content`（中文会变 GBK 乱码）。
- **路径层数**：`常驻件/` 下的件取项目根要用 `Path(__file__).resolve().parents[3]`（0=常驻件 1=回归测试 2=06_维护工具 3=项目根）；`回归测试/` 下的 `layers.py`/`run_layers.py`/`samples.py`/`run_all.py` 用 `parents[2]`。写错会 `ModuleNotFoundError: No module named 'data'`。
- **别写 `hasattr(模块, "不存在的名字")` 当兜底**：恒 False ⇒ 检查静默跳过 ⇒ 覆盖矩阵里显示成假的 ✅（真实事故：`layers.py` 曾调不存在的 `data.vector_store_status()`，导致「向量点数漂移」这项一直没跑）。要么调真实存在的 API，要么读不到就**记一条 P3 并写明原因**，绝不静默通过。这条由 `test_module_refs.py` 静态守住（AST 扫全项目 `data.xxx` / `vector_store.xxx` 引用是否真的存在，196 处引用 0 问题）。
- 读 .py 源码做静态分析时一律 `encoding="utf-8-sig"`（项目里有些件带 BOM，用 utf-8 读会 `invalid non-printable character U+FEFF`）；判 `data.append(...)` 这类引用要先排**函数内局部变量遮蔽**（`assistant.py:3540` 的 `data = []` 不是模块）。
- 深测件跑之前必须确认全量冒烟导入已结束：`load_vector_index_status()` 会载入 `storage/rag_chunks.json` 镜像并**打开内嵌 Qdrant 目录**，与导入抢锁。
- **回归集唯一入口 = 本目录 `run_all.py`**。桌面 `_tools\run_regression.py` 只是转发壳；曾经桌面与项目内各有一份同名件（MD5 相同）⇒ 已把桌面那份移入 `_tools\_migrated\`，不再双份维护。**件数不写死**：`run_all.py` 启动时会比对 `SUITES` 与 `常驻件\*.py`，**有未登记的件就打印并 `return 1`**（实测曾漏 2 件从未被跑而矩阵仍显全绿）⇒ 新增件必须同步登记 `SUITES`。

