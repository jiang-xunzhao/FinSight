"""项目配置模块。统一保存运行路径、数据库连接、全局共享状态和默认设置。"""
from __future__ import annotations  # 启用延迟类型注解，兼容较早的 Python 版本

# Python 标准库：线程、线程池和路径处理。
import os  # 读取部署环境变量
import threading  # 提供线程锁和后台线程能力
from concurrent.futures import ThreadPoolExecutor  # 管理后台任务和并发导入
from pathlib import Path  # 使用跨平台路径对象定位项目文件
from typing import Any  # 提供类型注解，提高代码可读性

# 配置模块位于一级阶段目录中，项目根目录需要向上取一级。
BASE_DIR = Path(__file__).resolve().parents[1]

def load_env_file(path: Path) -> None:
    """加载项目根目录下简单的 KEY=VALUE 环境变量文件。"""
    if not path.exists():
        return
    # 用 utf-8-sig：PowerShell 的 Set-Content -Encoding UTF8 会在文件头写 EF BB BF，
    # 少了这道签名第一个键名会变成 "\ufeffDEEPSEEK_API_KEY"，环境变量永远读不到（实测踩中）。
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

load_env_file(BASE_DIR / ".env")

CONFIG: dict[str, Any] = {
    "pdf_path": os.getenv(
        "FINSIGHT_PDF_PATH",
        r"C:\MySql\年度报告项目文件\财务报告\600007_20260831_11QJ.pdf",
    ),
    "mysql_host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "mysql_port": int(os.getenv("MYSQL_PORT", "3306")),
    "mysql_user": os.getenv("MYSQL_USER", "root"),
    "mysql_password": os.getenv("MYSQL_PASSWORD", "123456"),
    "mysql_database": os.getenv("MYSQL_DATABASE", "finance_report"),
    "ocr_language": os.getenv("OCR_LANGUAGE", "zh-Hans"),
    "ocr_dpi": int(os.getenv("OCR_DPI", "300")),
}

# 以前这里写死 "127.0.0.1"（只听本机回环）⇒ 部署到服务器上时，服务起得好好的、
# netstat 也在 LISTENING，但从自己电脑敲服务器 IP 必然连不上，看上去像「打不开」。
# 现在默认仍然只听 127.0.0.1（本机用不变），要对外访问就在 .env 里写 APP_HOST=0.0.0.0
# 并自己配好防火墙 + 云安全组（本程序目前没有账号密码，别开 0.0.0.0/0）。
APP_HOST = os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
APP_PORT = int(os.getenv("APP_PORT", "8501"))
APP_VERSION = "FinSight-2026.09.15"
VECTOR_MODEL_CACHE = BASE_DIR / "storage/models"
QDRANT_PATH = BASE_DIR / "storage/qdrant"
QDRANT_COLLECTION = "finsight_rag"
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()

RAG_CHUNKS: list[dict[str, Any]] = []
CHAT_SESSIONS: list[dict[str, Any]] = []
CHAT_TASKS: dict[str, dict[str, Any]] = {}
CHAT_TASKS_LOCK = threading.Lock()
CHAT_SESSIONS_LOCK = threading.RLock()
IMPORT_JOBS: dict[str, dict[str, Any]] = {}
IMPORT_JOBS_LOCK = threading.RLock()
IMPORT_EXECUTOR = ThreadPoolExecutor(max_workers=2)
VECTOR_INDEX_JOB: dict[str, Any] = {}
VECTOR_INDEX_JOB_LOCK = threading.Lock()

RAG_CHUNKS_PATH = BASE_DIR / "storage/rag_chunks.json"
RAG_CHUNKS_LOCK = threading.Lock()
DOCUMENT_INDEX_PATH = BASE_DIR / "storage/document_index.json"
DOCUMENT_INDEX_LOCK = threading.Lock()
CHAT_HISTORY_PATH = BASE_DIR / "storage/chat_history.json"

SETTINGS: dict[str, Any] = {
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "llm_model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    "embedding_model": "BAAI/bge-small-zh-v1.5",
    "answer_style": "分析师视角",
    "unit_mode": "自动",
    "chart_theme": "dark",
}

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# 账号实际可用的模型名以 /models 为准，可用 DEEPSEEK_MODEL 环境变量覆盖。
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_EMBEDDING_DIM = 512
EMBEDDING_BATCH_SIZE = 64
VECTOR_INDEX_VERSION = 1
