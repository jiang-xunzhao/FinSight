# FinSight 财报智析

本地部署的上市公司财报分析系统，支持结构化财报问数、Text-to-SQL、财报文本 RAG、股东表解析、图表展示、多轮会话和本地向量检索。

## 主要功能

- 财报文件解析并写入 MySQL 结构化数据库。
- 自然语言查询营业收入、净利润、资产负债率等指标。
- 智能财报分析模式执行 SQL 或混合 SQL + RAG。
- 支持一句话同时比较多家公司，自动拆解 SQL 子任务并并发调度。
- 多公司结果统一报告期、统一指标列并合并为一张比较表。
- 规划、执行、结果和回答校验失败时自动进入纠错或重规划流程。
- 股东持股表专用解析、柱形图、饼图和图表导出。
- 经营概览、风险、审批和交易性质等专用文本处理。
- 多轮会话、公司自动切换和图表换型。
- 本地 `BAAI/bge-small-zh-v1.5` 向量与 Qdrant 向量库。
- BM25 + Qdrant 向量召回 + 章节重排。
- 数据完整率、校验报告和回答证据链。

## 目录说明

- `01_基础配置/config.py`：配置和共享状态。
- `02_向量存储/vector_store.py`：Qdrant 向量库适配。
- `03_数据层/data.py`：文档解析、MySQL、RAG 和向量索引。
- `04_智能分析/assistant.py`：意图路由、检索、SQL、答案生成和校验。
- `05_Web服务/web.py`：HTTP 服务、接口和前端页面。
- `06_维护工具/vector_index.py`：向量索引维护命令。
- `06_维护工具/acceptance_check.py`：核心能力验收命令。
- `99_启动入口/my_open.py`：依赖检查和启动入口。
- `storage/`：文件索引、聊天历史、RAG 内容和本地模型。

## 环境要求

- Windows 10/11
- Python 3.13
- MySQL 8.x
- 至少 4GB 可用磁盘空间

## 安装

```powershell
cd "C:\Users\35999\PycharmProjects\FinSight"
python -m venv ..\.venv
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

数据库使用 `01_基础配置/config.py` 中的连接配置。正式部署时应把密码改为环境变量，并创建只读业务账号。
可参考 `create_readonly_user.sql.example` 创建只读账号，再设置 `MYSQL_READONLY_USER` 和 `MYSQL_READONLY_PASSWORD`。

## 启动

```powershell
..\.venv\Scripts\python.exe "99_启动入口\my_open.py"
```

浏览器打开：

```text
http://127.0.0.1:8501
```

## 智能模式说明

“智能财报分析”是混合模式：

- 营业收入、利润、现金流等确定性数值走结构化数据工具。
- 多公司比较由自主规划器拆成独立子任务，调度器并行执行后统一汇总。
- 股东表、经营概览和风险信息走专用处理器。
- 原因分析、文本归纳和跨章节综合使用 DeepSeek。
- 云端不可用时，系统会明确使用本地数据结果。

密钥由环境变量提供：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
```

不要把密钥写入源码或提交到 Git。

## 向量索引

首次安装后检查索引状态：

```powershell
..\.venv\Scripts\python.exe "06_维护工具\vector_index.py"
```

重建全部向量：

```powershell
..\.venv\Scripts\python.exe "06_维护工具\vector_index.py" --rebuild
```

新导入的文件会自动生成增量向量并写入 Qdrant。向量模型缓存在 `storage/models/`，Qdrant 本地数据保存在 `storage/qdrant/`。

首次或重建后同步已有向量：

```powershell
..\.venv\Scripts\python.exe "06_维护工具\vector_index.py" --sync-qdrant
```

需要清空并重建 Qdrant 时：

```powershell
..\.venv\Scripts\python.exe "06_维护工具\vector_index.py" --reset-qdrant
```

默认使用嵌入式本地 Qdrant，不需要 Docker。设置 `QDRANT_URL` 后可切换到远程 Qdrant 服务。
嵌入式 Qdrant 会独占本地文件锁，执行 `06_维护工具\vector_index.py`、备份或恢复前应先停止 Web 服务。

## 备份

```powershell
.\backup.ps1 -BackupRoot "D:\FinSight_Backup"
```

备份内容包括：

- MySQL 数据库
- `storage/` 文件、索引、聊天历史和本地模型
- 项目源代码

恢复时先还原代码，再导入 MySQL SQL 文件，最后恢复 `storage/`。

## 验收

```powershell
..\.venv\Scripts\python.exe "06_维护工具\vector_index.py"
..\.venv\Scripts\python.exe "06_维护工具\acceptance_check.py"
..\.venv\Scripts\python.exe "06_维护工具\correction_benchmark.py"
Invoke-RestMethod http://127.0.0.1:8501/api/summary
Invoke-RestMethod http://127.0.0.1:8501/api/data-quality
Invoke-RestMethod http://127.0.0.1:8501/api/vector-index
```

SQL 纠错评测会生成 `storage/correction_report.json`，其中包含纠错成功率、修复成功数、检出异常数、修复样本和降级样本。

默认评测使用本地修复规则。需要测试 DeepSeek 的 SQL 修复能力时，先设置密钥，再增加 `--online`：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
..\.venv\Scripts\python.exe "06_维护工具\correction_benchmark.py" --online
```

详细功能状态参见 `README` 的验收输出和运行日志。
