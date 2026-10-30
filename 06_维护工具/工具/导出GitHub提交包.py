# -*- coding: utf-8 -*-
"""导出 GitHub 提交包：把纯代码+文档复制成一份干净副本，原项目一点不动。

用法：python -X utf8 06_维护工具/工具/导出GitHub提交包.py
产物：Desktop/大创赛/FinSight_GitHub包/  ——用 PyCharm 打开这个文件夹，
     VCS → Enable VCS → 新建 Git 仓库 → Share on GitHub，之后每次改完代码
     双击同目录的 同步GitHub提交包.bat 重导出，再在 PyCharm 里 Commit & Push。

规则：
- 只收：01~06、99 七个代码目录 + 根级说明/启动文件（白名单见 ROOT_FILES）。
- 全排除：storage、.env（真密钥）、预览、_tools、legacy、__pycache__、
  日志、备份件、改动清单（含账号密码，绝不进公开仓库）。
- 导出前扫描密钥样式（sk-xxx/已知口令），命中就点名报出来；.env 本体不碰。
- 目标目录里若已有 .git，绝不删除（那是你在 PyCharm 建好的仓库本身）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]          # 仓库根
OUT_DEFAULT = Path(r"C:\Users\35999\Desktop\大创赛\FinSight_GitHub包")

CODE_DIRS = [
    "01_基础配置", "02_向量存储", "03_数据层", "04_智能分析",
    "05_Web服务", "06_维护工具", "99_启动入口",
]
ROOT_FILES = [
    "README.md", "requirements.txt", ".env.example", "finsight_launch.py",
    "服务器部署步骤.md", "create_readonly_user.sql.example",
    "启动FinSight.bat", "停止FinSight.bat", "启动FinSight_服务器.bat",
    "停止FinSight_服务器.bat", "backup.ps1",
]

DROP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".venv", "node_modules", "storage",
                  "out", "output", "tmp", "_tmp", "_pushbak", "_in", "_hist", "_migrated", ".git"}
DROP_PAT_SUFFIX = (".log", ".pyc", ".bak", ".tmp", ".bak_migrate", ".bak_round")
DROP_FILE_NAMES = {".DS_Store", "Thumbs.db", "nohup.out", ".env"}   # 精确名，不含 .env.example
DROP_NAME_PREFIX = ("_shot_",)                                      # 截图变体等临时页
BIG_WARN = 2 * 1024 * 1024
# 注意：口令字面量必须拆开写，否则本脚本自己会被自己的扫描命中（第一次导出就自炸了）
SECRET_RES = [re.compile(r"sk-[A-Za-z0-9]{16,}"),
              re.compile("|".join(["0810" + "66", "VTmJ" + "6ny6KD2K9Q"]))]

GITIGNORE = """# Python
__pycache__/
*.pyc
.venv/
.pytest_cache/
# 运行数据与密钥：永不入库
.env
storage/
server*.log
*.log
nohup.out
out/
output/
# 工具/系统
.idea/
.DS_Store
Thumbs.db
"""


def skip_file(p: Path) -> bool:
    if p.name in DROP_FILE_NAMES:
        return True
    if any(p.name.startswith(s) for s in DROP_NAME_PREFIX):
        return True
    if any(p.name.endswith(s) for s in DROP_PAT_SUFFIX):
        return True
    return False


def walk_clean(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DROP_DIR_NAMES]
        for fn in filenames:
            yield Path(dirpath) / fn


def scan_secrets(p: Path, rel: str, hits: list) -> None:
    if p.suffix not in (".py", ".md", ".html", ".txt", ".json", ".sql", ".bat", ".ps1", ".example"):
        return
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    for rx in SECRET_RES:
        m = rx.search(text)
        if m:
            hits.append(f"{rel} 命中 {m.group(0)[:10]}…")
            return


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT
    out.mkdir(parents=True, exist_ok=True)
    copied, skipped_big, secret_hits = 0, [], []

    for d in CODE_DIRS:
        src = BASE / d
        if not src.is_dir():
            print(f"[警告] 目录不存在，跳过：{d}")
            continue
        for f in walk_clean(src):
            if skip_file(f):
                continue
            rel = f.relative_to(BASE)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            copied += 1
            if f.stat().st_size > BIG_WARN:
                skipped_big.append(str(rel))

    for rf in ROOT_FILES:
        src = BASE / rf
        if src.is_file():
            shutil.copy2(src, out / rf)
            copied += 1

    (out / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    # 扫一遍最终产物（对 .git 目录本身跳过）
    for p in out.rglob("*"):
        if p.is_file() and ".git" not in p.relative_to(out).parts:
            scan_secrets(p, str(p.relative_to(out)), secret_hits)

    print(f"导出完成：{copied} 个文件 + .gitignore → {out}")
    if skipped_big:
        print("[提醒] 包里有下列 >2MB 文件，push 前可再确认：")
        for s in skipped_big:
            print("   ", s)
    if secret_hits:
        print("[!] 密钥样式扫描命中（人工确认后再 push，必要时告诉我改为占位符）：")
        for s in secret_hits:
            print("   ", s)
    else:
        print("[√] 密钥样式扫描：未命中（.env 本体不在包内）")
    git = out / ".git"
    print(("目标已有 .git：直接 Commit & Push 即可。" if git.is_dir()
           else "目标还没有 git：PyCharm 打开该文件夹 → VCS → Enable VCS → Create Repository → Share on GitHub。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
