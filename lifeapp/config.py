"""应用配置与路径管理。"""
from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    """是否运行于 PyInstaller 打包后的环境。"""
    return getattr(sys, "frozen", False)


def base_dir() -> str:
    """项目根目录。

    - 打包后：exe 所在目录（便携式，数据随 exe 存放）。
    - 源码运行：life_system 目录。
    """
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_data_dir_cache: tuple[str, str] | None = None


def data_dir() -> str:
    """数据目录（数据库等结构化数据）。笔记不在这里，存在 Obsidian 库里。

    统一存放到用户主目录 ``~/.life_system``，与 exe / 源码位置解耦：
    - 打包产物目录（dist）随时可能被清理重编译，数据放那里会被误删；
    - 源码运行与打包运行共用同一份数据，避免两份库互相割裂。

    结果按主目录缓存：``db.connect()`` 每次都调它，而 ``os.makedirs`` 是真的
    系统调用 —— 一次 reload 里 62 次 connect 就要多跑 62 次建目录。
    """
    global _data_dir_cache
    home = os.path.expanduser("~")
    if _data_dir_cache is None or _data_dir_cache[0] != home:
        path = os.path.join(home, ".life_system")
        os.makedirs(path, exist_ok=True)
        _data_dir_cache = (home, path)
    return _data_dir_cache[1]


def legacy_data_dirs() -> list[str]:
    """返回可能存在的旧数据目录（迁移源），去重并排除当前数据目录。

    旧版本把数据放在可执行/源码目录下的 ``data`` 子目录，这里列出两个候选：
    - ``base_dir()/data``：打包时是 exe 所在目录下的 data，源码时是项目目录下的 data；
    - 项目源码目录下的 data（无论打包与否都可能残留）。
    """
    current = os.path.abspath(data_dir())
    src_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base_dir(), "data"),
        os.path.join(src_base, "data"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for d in candidates:
        d = os.path.abspath(d)
        if d == current or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def migrate_legacy_data() -> bool:
    """若新数据目录尚无数据库，则从旧位置把数据迁移（复制）过来。

    迁移策略：只做增量复制（目标已存在则跳过），绝不覆盖新位置已有文件。
    返回是否发生了迁移。
    """
    import shutil

    if os.path.exists(db_path()):
        return False
    for old_dir in legacy_data_dirs():
        old_db = os.path.join(old_dir, "life.db")
        if not os.path.exists(old_db):
            continue
        os.makedirs(data_dir(), exist_ok=True)
        for name in os.listdir(old_dir):
            src = os.path.join(old_dir, name)
            dst = os.path.join(data_dir(), name)
            try:
                if os.path.isdir(src):
                    if not os.path.exists(dst):
                        shutil.copytree(src, dst)
                else:
                    if not os.path.exists(dst):
                        shutil.copy2(src, dst)
            except OSError:
                continue
        return True
    return False


def db_path() -> str:
    """SQLite 数据库路径。"""
    return os.path.join(data_dir(), "life.db")


def asset_path(relative: str) -> str:
    """资源文件路径。

    兼容 PyInstaller onefile 模式（资源解压在 sys._MEIPASS 临时目录）。
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", base_dir())
        return os.path.join(base, relative)
    return os.path.join(base_dir(), relative)


def tools_root() -> str:
    """集成工具（api_cluster / rag / arxiver）所在的目录。

    优先从环境变量 LIFE_TOOLS_ROOT 读取；否则从 base_dir 向上查找
    包含这些工具目录的父目录。源码运行和打包运行都能正确定位，
    因此工具代码改动无需重新打包 LifeSystem 即可生效。
    """
    env = os.environ.get("LIFE_TOOLS_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)

    base = os.path.abspath(base_dir())
    for _ in range(4):
        if any(os.path.isdir(os.path.join(base, d))
               for d in ("api_cluster", "rag", "arxiver")):
            return base
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    return os.path.abspath(os.path.join(base_dir(), ".."))


def tool_dir(name: str) -> str:
    """某个集成工具的目录（api_cluster / rag / arxiver）。"""
    return os.path.join(tools_root(), name)


APP_NAME = "Life System"
APP_VERSION = "1.0.0"
