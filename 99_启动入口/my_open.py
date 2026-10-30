"""项目启动模块。负责检查依赖并启动 FinSight Web 服务。"""
from __future__ import annotations  # 启用延迟类型注解，兼容较早的 Python 版本

# Python 标准库：依赖检查、启动子进程和路径处理。
import importlib.util  # 检查 Python 依赖是否安装
import subprocess  # 调用 Windows OCR 和安装依赖
import sys  # 管理模块路径、标准输出和程序退出
from pathlib import Path  # 使用跨平台路径对象定位项目文件


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
MODULE_DIRS = (
    PROJECT_ROOT / "01_基础配置",
    PROJECT_ROOT / "02_向量存储",
    PROJECT_ROOT / "03_数据层",
    PROJECT_ROOT / "04_智能分析",
    PROJECT_ROOT / "05_Web服务",
)
for candidate in (ROOT, PROJECT_ROOT, *MODULE_DIRS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def ensure_dependencies() -> None:
    """检查并安装缺失的 Python 依赖。"""
    packages = {
        "pymysql": "PyMySQL",
        "pdfplumber": "pdfplumber",
        "pypdfium2": "pypdfium2",
        "pandas": "pandas",
        "docx": "python-docx",
        "cryptography": "cryptography",
        "plotly": "plotly",
        "openai": "openai",
        "numpy": "numpy",
        "certifi": "certifi",
        "fastembed": "fastembed",
        "onnxruntime": "onnxruntime",
        "qdrant_client": "qdrant-client",
    }
    missing = [
        package
        for module, package in packages.items()
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing]
        )


def main() -> None:
    """启动 FinSight 项目。"""
    ensure_dependencies()
    from web import run_server

    run_server()


if __name__ == "__main__":
    main()
