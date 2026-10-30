#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FinSight 一键启动器（纯 ASCII 源码，避免 cmd 代码页把中文路径解坏）。

用法：
    finsight_launch.py            检测/启动服务并打开浏览器
    finsight_launch.py --stop     停止服务
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8501
BASE_URL = f"http://{HOST}:{PORT}"
ENTRY_REL = Path("99_" + "\u542f\u52a8\u5165\u53e3") / "my_open.py"   # 99_启动入口
LOG_PATH = Path(os.environ.get("TEMP", ".")) / "finsight-server.log"
SECRET_VARS = ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL")


def registry_env(names: tuple[str, ...]) -> dict[str, str]:
    """从注册表补环境变量：不管服务是谁拉起来的，都能拿到机器级密钥。"""
    import winreg

    found: dict[str, str] = {}
    hives = (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for hive, path in hives:
        try:
            with winreg.OpenKey(hive, path) as key:
                for name in names:
                    if name in found or os.environ.get(name):
                        continue
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        found[name] = value.strip()
        except OSError:
            continue
    return found


def find_project_root() -> Path | None:
    """定位要启动的项目根。

    ★ **先认自己**：优先用「本文件所在目录」（或上一级，取真正含启动入口的那个）。
    这条是 2026-10-29 的根治：机器上曾同时存在四份可运行代码根 + 七份本脚本副本，
    其中 `Desktop\\启动FinSight.bat` 调的是 10-26 那份旧副本（搜索列表里没有
    `PycharmProjects`），于是双击桌面图标起来的是**旧根那份滞后代码**，
    而 `/api/vector-index` 照样回 ready=true ⇒ 看起来重启成功、其实没换。
    只要本文件跟着项目走，它就只可能启动自己这份代码。

    扫盘枚举只留作兜底（万一脚本被单独拷走、旁边没有入口）。
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        if (candidate / ENTRY_REL).is_file():
            return candidate

    bases = [
        Path.home() / "PycharmProjects",
        Path.home() / "PyCharmMiscProject",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path("D:/"),
    ]
    for base in bases:
        if not base.exists():
            continue
        try:
            children = [base, *base.glob("*/"), *base.glob("*/*/")]
        except OSError:
            continue
        for candidate in children:
            if (candidate / ENTRY_REL).is_file():
                return candidate
    return None


def find_python(root: Path) -> Path | None:
    """优先用项目自带虚拟环境，其次当前解释器。"""
    for candidate in (root.parent / ".venv", root / ".venv", root / "venv"):
        exe = candidate / "Scripts" / "python.exe"
        if exe.is_file():
            return exe
    return Path(sys.executable) if sys.executable else None


def server_is_ours(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/system/version", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("app") == "FinSight"
    except Exception:
        return False


def port_busy() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, PORT)) == 0


def child_env() -> dict[str, str]:
    """当前环境变量 + 注册表补齐的密钥，返回给服务子进程。"""
    env = dict(os.environ)
    added = registry_env(SECRET_VARS)
    env.update(added)
    if added:
        print("已从注册表补进：" + "、".join(added))
    if not env.get("DEEPSEEK_API_KEY"):
        print("提示：没有 DEEPSEEK_API_KEY，智能财报分析不可用，本地数据模式照常能跑。")
    return env


def start_server(root: Path, python: Path) -> subprocess.Popen:
    log = LOG_PATH.open("ab", buffering=0)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        # -u：不缓冲 stdout，日志实时落到 LOG_PATH；否则排错时看不到任何输出。
        [str(python), "-u", str(ENTRY_REL)],
        cwd=str(root),
        stdout=log,
        stderr=log,
        env=child_env(),
        creationflags=flags,
    )


def do_stop() -> int:
    try:
        request = urllib.request.Request(
            f"{BASE_URL}/api/system/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5):
            pass
        for _ in range(20):
            time.sleep(0.3)
            if not port_busy():
                print("FinSight 已停止。")
                return 0
        print("已发送停止请求。")
        return 0
    except Exception:
        print("FinSight 没有在运行，无需停止。")
        return 0


def main() -> int:
    if "--stop" in sys.argv:
        return do_stop()

    restart = "--restart" in sys.argv
    if not restart and server_is_ours():
        print("FinSight 已经在运行，直接打开页面。")
        webbrowser.open(BASE_URL)
        return 0
    if restart:
        do_stop()

    root = find_project_root()
    if root is None:
        print("没找到项目目录（里面要有 99_启动入口/my_open.py）。")
        return 1
    python = find_python(root)
    if python is None:
        print("没找到可用的 Python。")
        return 1

    print(f"项目：{root}")
    print(f"解释器：{python}")
    print("正在启动 FinSight，首次加载本地向量模型可能要几十秒...")
    start_server(root, python)

    for _ in range(240):
        time.sleep(0.5)
        if server_is_ours(timeout=1.0):
            print(f"启动完成，已打开 {BASE_URL}")
            webbrowser.open(BASE_URL)
            return 0
    print(f"启动超时，日志见：{LOG_PATH}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
