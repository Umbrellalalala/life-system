"""开机自启：通过 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 注册表项控制。

- 打包后：注册表值指向 LifeSystem.exe 本身。
- 源码运行：注册表值指向 `python run.py`（开发期可用）。
"""
from __future__ import annotations

import os
import sys

from . import config

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_KEY = "LifeSystem"


def _windowless_python() -> str:
    """当前解释器对应的无控制台版本（python.exe → pythonw.exe）。"""
    base, name = os.path.split(sys.executable)
    if name.lower() == "python.exe":
        candidate = os.path.join(base, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def _launch_command() -> str:
    """返回写入注册表的启动命令。"""
    if config.is_frozen():
        return f'"{sys.executable}"'
    # 源码运行：pythonw.exe run.py
    # 必须用 pythonw 而不是 python —— python.exe 是控制台子系统程序，从 Run 键
    # 启动时 Windows 会额外给它开一个控制台窗口，任务栏上就多出一个「python」。
    # 用户点掉那个控制台，系统会向进程发 CTRL_CLOSE_EVENT，整个 LifeSystem 跟着退出。
    run_py = os.path.join(config.base_dir(), "run.py")
    return f'"{_windowless_python()}" "{run_py}"'


def is_enabled() -> bool:
    """当前是否已注册开机自启。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_KEY)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable() -> bool:
    """写入开机自启注册表项（指向当前 exe / 脚本）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_KEY, 0, winreg.REG_SZ, _launch_command())
        return True
    except OSError:
        return False


def disable() -> bool:
    """删除开机自启注册表项。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, APP_KEY)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def sync(enabled: bool) -> bool:
    """按目标状态设置开机自启。"""
    return enable() if enabled else disable()
