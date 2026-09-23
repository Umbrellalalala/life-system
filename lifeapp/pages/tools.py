"""工具启动器：从侧边栏把本地 AI 工具（ApiCluster / RAG / Arxiver）内嵌进 Life System。

- 工具源码位于工作区根目录（与 life_system 同级），从外部路径启动，
  修改工具代码无需重新打包 LifeSystem 即可生效。
- 内嵌原理：用 Win32 SetParent 把工具自身的原生窗口「重定父」到 LifeSystem
  的内容区里（WebView2 / tkinter 窗口都可直接嵌入）。这样工具就相当于
  LifeSystem 里的一个页面，而不是另开一个独立窗口。
- 单实例：工具已在运行时点击会复用其窗口并嵌入，而不是再开一个。
- 兜底：若捕获窗口失败，则退回「在独立窗口打开」。

⚠️ 解绑（detach）是内嵌方案里最关键的一环：
Win32 规则是「父窗口被销毁 → 它的所有子窗口被自动销毁」，跨进程 SetParent
同样适用。也就是说 LifeSystem 一旦在还挂着工具窗口的情况下消失（崩溃、被强杀、
关机、解绑异常），三个工具的窗口会被系统连带销毁——进程还在、托盘图标还在，
但窗口没了，托盘就成了点不开的「幽灵图标」。
因此本模块在以下时机都会调用 detach_all() 把工具窗口还原为独立窗口：
  应用退出（closeEvent）/ 缩到托盘（hideEvent）/ 系统关机注销（WM_ENDSESSION）
  / aboutToQuit。且还原时会恢复窗口原来的样式、屏幕位置和可见性，
  让工具回到「从未被内嵌过」的状态，保证独立使用不受影响。
"""
from __future__ import annotations

import os
import subprocess
import sys
import weakref

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from .. import config, popups

TOOLS = [
    {
        "key": "api_cluster",
        "name": "ApiCluster",
        "icon": "🔀",
        "exe": "ApiCluster.exe",
        "script": None,
        # 内嵌时静默启动（窗口先隐藏，我们再把它抓进来显示），避免闪一下最大化
        "embed_args": ["-hidden"],
        "hint": "ApiCluster",
    },
    {
        "key": "rag",
        "name": "RAG 助手",
        "icon": "📚",
        "exe": None,
        "script": "main.py",
        # 用便携环境 python（含完整 CPU 依赖 customtkinter/fastembed 等），
        # 否则会 fallback 到 life_system 的 python（无这些依赖，import 失败）
        "python": r"D:\RAGAssistant\python\pythonw.exe",
        # --tray：启动即缩小到托盘，由 life_system 内嵌后再显示，避免弹出独立窗口
        "embed_args": ["--tray"],
        "hint": "RAG 文件助手",
    },
    {
        "key": "arxiver",
        "name": "Arxiver",
        "icon": "📡",
        "exe": None,
        "script": "run.py",
        # 启动即缩小到托盘（窗口隐藏）并被内嵌；保留托盘图标，
        # 点托盘「打开主窗口」时脱离内嵌变成独立窗口，保证同一时刻只有一个窗口
        "embed_args": ["--minimized"],
        "hint": "Arxiver",
    },
]

# 由本程序启动的进程句柄
_processes: dict[str, subprocess.Popen] = {}

# ---------------------------------------------------------------------------
# Win32 窗口重定父（嵌入）引擎
# ---------------------------------------------------------------------------
IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    # 用无符号 32 位处理窗口样式：WS_POPUP(0x80000000) 等高位若被解释为
    # 有符号负数，会让 GetWindowLongW 返回负值，进而在 reparent/restore 时
    # 使 SetWindowLongW 参数溢出，导致内嵌失败。
    _user32.GetWindowLongW.restype = ctypes.c_uint32
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint32]

    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_POPUP = 0x80000000
    WS_CHILD = 0x40000000
    WS_SYSMENU = 0x00080000
    WS_VISIBLE = 0x10000000
    WS_MINIMIZE = 0x20000000
    WS_MAXIMIZE = 0x01000000

    SW_HIDE = 0
    SW_SHOW = 5
    SW_RESTORE = 9

    SWP_FRAMECHANGED = 0x0020
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    _HWND_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    # 记录被我们内嵌过的窗口的原始状态，detach 时完整还原（样式 / 屏幕矩形 / 可见性）
    _orig_styles: dict[int, int] = {}
    _orig_geometry: dict[int, tuple[int, int, int, int]] = {}
    _orig_visible: dict[int, bool] = {}
    # 所有内嵌宿主的弱引用：退出 / 隐藏前统一解绑（见 detach_all）
    _hosts: list = []

    def _enum_windows(keyword: str) -> list[int]:
        found: list[int] = []
        kw = keyword.lower()

        def cb(hwnd, lparam):  # noqa: ANN001
            length = _user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                if kw in buf.value.lower():
                    found.append(int(hwnd))
            return True

        _user32.EnumWindows(_HWND_ENUMPROC(cb), 0)
        return found

    def find_window(keyword: str) -> int | None:
        """返回标题包含 keyword 的顶层窗口句柄。

        优先匹配可见窗口；若没有可见的（例如 ApiCluster 以 -hidden 静默启动时
        窗口是隐藏的），则退而匹配隐藏窗口，确保内嵌能抓到它。
        """
        wins = _enum_windows(keyword)
        if not wins:
            return None
        visible = [h for h in wins if _user32.IsWindowVisible(h)]
        return (visible[0] if visible else wins[0])

    def get_class_name(hwnd: int) -> str:
        """返回窗口类名（用于识别 Tk 的 wrapper/client 双窗口结构）。"""
        try:
            buf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, buf, 256)
            return buf.value
        except Exception:  # noqa: BLE001
            return ""

    def find_tk_client(hwnd: int) -> int | None:
        """Tk 窗口的特殊结构：wrapper(TkTopLevel, 带标题栏) 内含 client(TkChild) 才是
        真正渲染内容的窗口。内嵌必须操作 client 而非 wrapper——对 wrapper 做 SetParent
        会触发 Tk 重建窗口，导致「独立窗口 + 内嵌」两个窗口并存。返回 client 句柄；
        若 hwnd 不是 Tk wrapper 则返回 None。"""
        if get_class_name(hwnd) != "TkTopLevel":
            return None
        found: dict[str, int] = {"hwnd": 0}

        def cb(child, lparam):  # noqa: ANN001
            if get_class_name(child) == "TkChild":
                found["hwnd"] = int(child)
                return False  # 找到即停止
            return True

        _user32.EnumChildWindows(hwnd, _HWND_ENUMPROC(cb), 0)
        return found["hwnd"] or None

    def is_window(hwnd: int) -> bool:
        return bool(_user32.IsWindow(hwnd))

    def get_parent(hwnd: int) -> int:
        try:
            return int(_user32.GetParent(hwnd))
        except Exception:  # noqa: BLE001
            return 0

    def get_work_area() -> tuple[int, int, int, int]:
        """返回屏幕可用区域（排除任务栏），用于把还原后的窗口夹回可见范围。"""
        try:
            rect = wintypes.RECT()
            if _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                if rect.right > rect.left and rect.bottom > rect.top:
                    return rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001
            pass
        return 0, 0, max(800, _user32.GetSystemMetrics(0)), max(600, _user32.GetSystemMetrics(1))

    def reparent_window(hwnd: int, host_wid: int) -> bool:
        """把 hwnd 变成 host_wid 的子窗口（去掉标题栏/边框）。"""
        try:
            style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
            _orig_styles[hwnd] = style
            # 记录内嵌前的屏幕矩形与可见性：detach 时按原样还原，
            # 否则工具会以一个「缩到 host 大小、落在屏幕左上角」的怪窗口继续存活。
            rect = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                _orig_geometry[hwnd] = (rect.left, rect.top, rect.right, rect.bottom)
            _orig_visible[hwnd] = bool(_user32.IsWindowVisible(hwnd))
            # 去掉标题栏/边框/弹窗/系统菜单，同时清除最小化/最大化状态
            # （tkinter 的 iconify 启动会让窗口带 WS_MINIMIZE，子窗口不能最小化，
            #  不清理会导致内嵌后仍显示为最小化/空白）。
            new_style = (style & ~(WS_CAPTION | WS_THICKFRAME | WS_POPUP | WS_SYSMENU
                                   | WS_MINIMIZE | WS_MAXIMIZE)) | WS_CHILD
            _user32.SetWindowLongW(hwnd, GWL_STYLE, new_style)
            _user32.SetParent(hwnd, host_wid)
            _user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def resize_child(hwnd: int, w: int, h: int) -> None:
        try:
            _user32.SetWindowPos(
                hwnd, 0, 0, 0, int(w), int(h), SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        except Exception:  # noqa: BLE001
            pass

    def client_size(hwnd: int) -> tuple[int, int]:
        """获取窗口客户区物理像素尺寸（直接读 Win32，规避 DPI 换算误差）。"""
        rect = wintypes.RECT()
        try:
            if _user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return max(0, rect.right - rect.left), max(0, rect.bottom - rect.top)
        except Exception:  # noqa: BLE001
            pass
        return 0, 0

    def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
        """返回窗口的屏幕矩形 (left, top, right, bottom)，失败返回 None。"""
        try:
            rect = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001
            pass
        return None

    def show_child(hwnd: int) -> None:
        try:
            # 先 SW_RESTORE 清除最小化状态（tkinter iconify 启动的窗口），再 SW_SHOW
            _user32.ShowWindow(hwnd, SW_RESTORE)
            _user32.ShowWindow(hwnd, SW_SHOW)
        except Exception:  # noqa: BLE001
            pass

    def focus_child(hwnd: int) -> None:
        """把键盘焦点设给内嵌的子窗口。

        tkinter 窗口被 SetParent 成 Qt 子窗口后，鼠标点击时焦点不会自动传给
        子窗口，导致输入框无法打字。SetFocus 只能作用于同一线程的窗口，而
        内嵌工具是独立进程，因此需先用 AttachThreadInput 连接输入队列再设焦点。
        """
        try:
            kernel32 = ctypes.windll.kernel32
            pid = wintypes.DWORD()
            target_tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            current_tid = kernel32.GetCurrentThreadId()
            if target_tid and target_tid != current_tid:
                _user32.AttachThreadInput(current_tid, target_tid, True)
                try:
                    _user32.SetFocus(hwnd)
                finally:
                    _user32.AttachThreadInput(current_tid, target_tid, False)
            else:
                _user32.SetFocus(hwnd)
        except Exception:  # noqa: BLE001
            pass

    def restore_window(hwnd: int) -> None:
        """退出内嵌：还原标题栏/边框并挂回桌面，作为独立窗口继续存活。

        顺序非常关键：
        1) 先 SetParent(0) 脱离宿主，再改样式。窗口还挂着父窗口时无法可靠清除
           WS_CHILD；残留 WS_CHILD 的「顶层窗口」不进任务栏、托盘也唤不起来。
        2) 恢复内嵌前的屏幕矩形，否则窗口会停在宿主内容区的大小与 (0,0) 位置。
        3) 恢复内嵌前的可见性：托盘启动的工具（-hidden / --tray / --minimized）
           内嵌前是隐藏的，解绑后应继续留在托盘，而不是被强行弹出来——强行弹出
           会让工具内部的可见性状态与真实状态不一致，之后点托盘反而没反应。
        """
        try:
            if not is_window(hwnd):
                return
            _user32.SetParent(hwnd, 0)  # 0 = 桌面

            orig = _orig_styles.pop(hwnd, None)
            if orig is None:
                # 没有原始记录（重复解绑 / 宿主状态丢失）时兜底拼一个标准顶层样式
                orig = (_user32.GetWindowLongW(hwnd, GWL_STYLE)
                        & ~(WS_CHILD | WS_POPUP | WS_MINIMIZE | WS_MAXIMIZE)
                        | (WS_CAPTION | WS_SYSMENU | WS_THICKFRAME))
            _user32.SetWindowLongW(hwnd, GWL_STYLE, orig)
            _user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

            rect = _orig_geometry.pop(hwnd, None)
            if rect:
                left, top, right, bottom = rect
                w = max(480, right - left)
                h = max(360, bottom - top)
                al, at, ar, ab = get_work_area()
                if (left < al - 40 or left > ar - 120
                        or top < at - 40 or top > ab - 120):
                    left = al + max(0, (ar - al - w) // 2)
                    top = at + max(0, (ab - at - h) // 2)
                _user32.SetWindowPos(
                    hwnd, 0, left, top, w, h, SWP_NOZORDER | SWP_NOACTIVATE)

            if _orig_visible.pop(hwnd, True) is False:
                _user32.ShowWindow(hwnd, SW_HIDE)  # 原本藏在托盘 → 继续留在托盘
            else:
                _user32.ShowWindow(hwnd, SW_RESTORE)
        except Exception:  # noqa: BLE001
            pass

    def get_parent(hwnd: int) -> int:
        """返回窗口的父窗口句柄（0 = 无父窗口，即独立顶层窗口）。"""
        try:
            return int(_user32.GetParent(hwnd))
        except Exception:  # noqa: BLE001
            return 0

else:
    def find_window(keyword: str):  # noqa: ANN001
        return None

    def is_window(hwnd):  # noqa: ANN001
        return False

    def get_parent(hwnd):  # noqa: ANN001
        return 0

    def reparent_window(hwnd, host_wid):  # noqa: ANN001
        return False

    def resize_child(hwnd, w, h):  # noqa: ANN001
        pass

    def client_size(hwnd):  # noqa: ANN001
        return 0, 0

    def show_child(hwnd):  # noqa: ANN001
        pass

    def focus_child(hwnd):  # noqa: ANN001
        pass

    def restore_window(hwnd):  # noqa: ANN001
        pass


# ---------------------------------------------------------------------------
# 工具启动
# ---------------------------------------------------------------------------
def find_python(tool_dir: str, tool: dict | None = None) -> str:
    """优先使用工具指定的 python，其次工具自带 venv，其次系统 Python。"""
    # 工具显式指定的 python（如 RAG 助手用便携环境，含完整 CPU 依赖）
    if tool and tool.get("python"):
        p = tool["python"]
        if os.path.exists(p):
            return p
    for venv in (".venv", "venv"):
        for exe in ("pythonw.exe", "python.exe"):
            p = os.path.join(tool_dir, venv, "Scripts", exe)
            if os.path.exists(p):
                return p
    if not config.is_frozen():
        return sys.executable
    import shutil
    for exe in ("pythonw", "python"):
        found = shutil.which(exe)
        if found:
            return found
    return "python"


def _is_process_running(image_name: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
            capture_output=True, text=True, creationflags=0x08000000,
        ).stdout
        return image_name.lower() in (out or "").lower()
    except Exception:  # noqa: BLE001
        return False


def _kill_process(image_name: str) -> bool:
    """强制结束指定进程（用于清理「进程还在但窗口已销毁」的僵尸实例）。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", image_name],
            capture_output=True, creationflags=0x08000000,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def tool_installed(tool: dict) -> bool:
    """判断工具是否已安装（其 exe / 入口脚本文件是否存在）。"""
    tool_dir = config.tool_dir(tool["key"])
    if tool.get("exe"):
        return os.path.exists(os.path.join(tool_dir, tool["exe"]))
    if tool.get("script"):
        return os.path.exists(os.path.join(tool_dir, tool["script"]))
    return False


def launch_tool(tool: dict, parent: QWidget | None = None,
                extra_args: list[str] | None = None, quiet: bool = False) -> bool:
    """启动工具；已在运行则唤起其窗口而不是开新实例。返回是否成功启动/复用。

    quiet=True 时（内嵌场景）不弹任何提示框，静默返回，让调用方继续轮询捕获窗口。
    """
    key = tool["key"]
    hint = tool.get("hint") or tool["name"]

    def warn(msg: str) -> None:
        if not quiet:
            _warn(parent, msg)

    # 1) 窗口已存在（无论由谁启动）→ 唤起
    if IS_WIN and find_window(hint):
        return True
    # 2) exe 型工具：检测进程。
    #    关键：进程在但窗口找不到（find_window 已返回 None）→ 僵尸进程
    #    （窗口被销毁但进程还占着端口/单实例锁，如 LifeSystem 被强杀后残留），
    #    若不清理，新实例会因单实例保护直接退出、窗口永远出不来，内嵌卡死。
    if tool["exe"] and _is_process_running(tool["exe"]):
        _kill_process(tool["exe"])
        _processes.pop(key, None)
        warn(f"检测到 {tool['name']} 残留进程，已清理并重启…")
    # 3) 本程序启动的进程仍存活但窗口未找到 → 提示
    proc = _processes.get(key)
    if proc is not None and proc.poll() is None:
        warn(f"{tool['name']} 已在运行。")
        return True
    # 4) 启动新实例
    tool_dir = config.tool_dir(key)
    if tool["exe"]:
        exe = os.path.join(tool_dir, tool["exe"])
        if not os.path.exists(exe):
            warn(f"未找到：\n{exe}\n请确认工具目录完整。")
            return False
        cmd = [exe] + (extra_args or [])
    else:
        script = os.path.join(tool_dir, tool["script"])
        if not os.path.exists(script):
            warn(f"未找到：\n{script}\n请确认工具目录完整。")
            return False
        cmd = [find_python(tool_dir, tool), script] + (extra_args or [])

    try:
        CREATE_NO_WINDOW = 0x08000000
        _processes[key] = subprocess.Popen(
            cmd, cwd=tool_dir, creationflags=CREATE_NO_WINDOW, close_fds=True)
        return True
    except Exception as e:  # noqa: BLE001
        warn(f"启动失败：{e}")
        return False


def open_tool_dir(tool: dict, parent: QWidget | None = None) -> None:
    tool_dir = config.tool_dir(tool["key"])
    if os.path.isdir(tool_dir):
        QDesktopServices.openUrl(QUrl.fromLocalFile(tool_dir))
    else:
        _warn(parent, f"目录不存在：\n{tool_dir}")


def _warn(parent: QWidget | None, msg: str) -> None:
    if parent is not None:
        popups.notify(parent, "提示", msg)
    else:
        print(msg)


# ---------------------------------------------------------------------------
# 内嵌宿主：把工具窗口抓进 LifeSystem 内容区
# ---------------------------------------------------------------------------
class ToolEmbedHost(QWidget):
    """一个页面：头部放状态与操作按钮，主体是一个用来「装」工具窗口的容器。"""

    def __init__(self, tool: dict):
        super().__init__()
        self.tool = tool
        self._hwnd: int | None = None
        self._wrapper: int | None = None  # Tk 窗口的 wrapper 句柄（内嵌 client 时隐藏它）
        self._embedded = False
        self._external = False  # 是否处于「独立窗口」模式（点新窗口/托盘脱离）
        self._find_timer: QTimer | None = None
        self._watch_timer: QTimer | None = None  # 监视窗口是否被外部解绑/关闭
        self._attempts = 0
        self._max_attempts = 30  # 每次 350ms，最多约 10.5s
        self._wrapper_rect: tuple[int, int, int, int] | None = None  # Tk wrapper 原位
        self._zombie_restarted = False  # 僵尸实例自愈只尝试一次
        self._setup_ui()
        if IS_WIN:
            _hosts.append(weakref.ref(self))  # 供 detach_all 统一解绑

    def _setup_ui(self) -> None:
        from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("ToolHeader")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(16, 10, 16, 10)
        hb.setSpacing(8)

        self.status = QLabel(f"{self.tool['name']} · 准备中…")
        self.status.setObjectName("Meta")
        hb.addWidget(self.status)

        btn_ext = QPushButton("↗ 新窗口")
        btn_ext.setObjectName("Ghost")
        btn_ext.setCursor(Qt.PointingHandCursor)
        btn_ext.clicked.connect(self.open_external)
        btn_dir = QPushButton("📁 目录")
        btn_dir.setObjectName("Ghost")
        btn_dir.setCursor(Qt.PointingHandCursor)
        btn_dir.clicked.connect(lambda: open_tool_dir(self.tool, self))
        btn_restart = QPushButton("⟳ 重启")
        btn_restart.setObjectName("Ghost")
        btn_restart.setCursor(Qt.PointingHandCursor)
        btn_restart.clicked.connect(self.restart)
        btn_quit = QPushButton("⏹ 退出")
        btn_quit.setObjectName("Ghost")
        btn_quit.setCursor(Qt.PointingHandCursor)
        btn_quit.clicked.connect(self.quit_tool)

        hb.addStretch(1)
        hb.addWidget(btn_ext)
        hb.addWidget(btn_dir)
        hb.addWidget(btn_restart)
        hb.addWidget(btn_quit)
        root.addWidget(header)

        self.host = QWidget()
        self.host.setObjectName("EmbedHost")
        # 拦截 host 的鼠标事件，点击时把键盘焦点转给内嵌窗口（tkinter 输入框需要）
        self.host.installEventFilter(self)
        root.addWidget(self.host, 1)

    def eventFilter(self, obj, event):  # noqa: ANN001
        """host 区域被点击时，把键盘焦点转给内嵌窗口，确保 tkinter 输入框可打字。"""
        from PySide6.QtCore import QEvent
        if obj is self.host and event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if self._hwnd and is_window(self._hwnd):
                focus_child(self._hwnd)
        return super().eventFilter(obj, event)

    # ---- 生命周期 ----
    def showEvent(self, event):  # noqa: ANN001
        from PySide6.QtCore import QEvent
        super().showEvent(event)
        self.start_embed()

    def start_embed(self) -> None:
        if not tool_installed(self.tool):
            self.status.setText(f"{self.tool['name']} · 该功能暂未安装")
            return
        if not IS_WIN:
            self.status.setText("仅 Windows 支持内嵌，已尝试在独立窗口打开")
            launch_tool(self.tool, self, quiet=True)
            return
        # 预热场景：页面尚未显示，仅启动进程（不内嵌），等 showEvent 再真正内嵌
        if not self.isVisible():
            if not (self._hwnd and is_window(self._hwnd)):
                launch_tool(self.tool, self,
                            extra_args=self.tool.get("embed_args"), quiet=True)
            return
        # 已内嵌且窗口还在 → 直接显示回来
        if self._embedded and self._hwnd and is_window(self._hwnd):
            # 窗口若已被工具自己 detach（如 Arxiver 点托盘弹出独立窗口），进入独立窗口模式
            if get_parent(self._hwnd) != int(self.host.winId()):
                self._embedded = False
                self._external = True
                self.status.setText(f"{self.tool['name']} · 独立窗口")
                self._start_watch()
                return
            show_child(self._hwnd)
            self._resize_child()
            self.status.setText(f"{self.tool['name']} · 已内嵌")
            return
        # 窗口仍在但没内嵌（比如之前还原过）→ 重新抓
        if self._hwnd and is_window(self._hwnd) and not self._embedded:
            self._embed(self._hwnd)
            return
        self._hwnd = None
        self._embedded = False
        self._external = False
        self._attempts = 0
        launched = launch_tool(self.tool, self, extra_args=self.tool.get("embed_args"), quiet=True)
        if not launched:
            self.status.setText("启动失败")
            return
        self.status.setText("正在捕获窗口…")
        self._schedule_find()

    def _schedule_find(self) -> None:
        if self._find_timer is None:
            self._find_timer = QTimer(self)
            self._find_timer.timeout.connect(self._try_embed)
        self._find_timer.start(350)

    def _try_embed(self) -> None:
        if self._embedded:
            self._find_timer.stop()
            return
        self._attempts += 1
        hwnd = find_window(self.tool["hint"])
        if hwnd:
            self._embed(hwnd)
            self._find_timer.stop()
        elif self._attempts >= self._max_attempts:
            self._find_timer.stop()
            # 僵尸实例自愈：进程还在，却怎么都找不到窗口 → 窗口已被销毁。
            # 典型成因就是 LifeSystem 上次异常退出时被连带销毁（见 detach_all）。
            # 本程序启动的实例有 PID，直接杀掉重启，让工具恢复可用（只试一次）。
            proc = _processes.get(self.tool["key"])
            if proc is not None and proc.poll() is None and not self._zombie_restarted:
                self._zombie_restarted = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                self.status.setText(f"{self.tool['name']} · 窗口已失效，正在重启…")
                QTimer.singleShot(800, self.restart)
                return
            self.status.setText("未能捕获窗口，已退回独立窗口打开")
            # 兜底：确保窗口可见
            launch_tool(self.tool, self, quiet=True)

    def _embed(self, hwnd: int) -> None:
        try:
            host_wid = int(self.host.winId())
            self._host_wid = host_wid
            # Tk 窗口（wrapper + client 双窗口）：内嵌 client，隐藏 wrapper。
            # 若 SetParent wrapper，Tk 会重建窗口，导致「独立窗口 + 内嵌」并存。
            tk_client = find_tk_client(hwnd) if IS_WIN else None
            if tk_client:
                self._wrapper = hwnd  # 传入的是 wrapper
                client = tk_client
                # 记下 wrapper 原位，detach 时移回屏幕内该位置（而不是硬编码 100,100）
                self._wrapper_rect = _window_rect(hwnd)
            else:
                client = hwnd  # 传入的是 client（重新内嵌）或普通单窗口工具
            if reparent_window(client, host_wid):
                self._hwnd = client
                self._embedded = True
                self._external = False
                self._zombie_restarted = False
                if self._wrapper and is_window(self._wrapper):
                    # 把 wrapper 移到屏幕外而非 SW_HIDE：Tk 检测到 WS_VISIBLE 被清掉后，
                    # 会在下个事件循环重新显示 wrapper，导致独立窗口残留、空白且关不掉。
                    # 移屏幕外保持 WS_VISIBLE（Tk 不干预），Tk 内部坐标仍是正常值。
                    _user32.SetWindowPos(
                        self._wrapper, 0, -32000, -32000, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                show_child(client)
                self._resize_child()
                # 嵌入后把键盘焦点交给子窗口，解决 tkinter 输入框无法打字的问题
                QTimer.singleShot(120, lambda: focus_child(client))
                # 布局稳定后再补几次，确保完全填满
                QTimer.singleShot(80, self._resize_child)
                QTimer.singleShot(400, self._resize_child)
                self.status.setText(f"{self.tool['name']} · 已内嵌到 Life System")
                # 开始监视：若窗口被外部（如 ApiCluster 托盘）解绑，及时更新状态
                self._start_watch()
            else:
                self.status.setText("内嵌失败，已退回独立窗口打开")
                launch_tool(self.tool, self, quiet=True)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"内嵌异常：{e}")

    def _resize_child(self) -> None:
        """把内嵌窗口缩放到填满 host（按 host 客户区物理像素）。"""
        if not (self._hwnd and IS_WIN):
            return
        host_wid = getattr(self, "_host_wid", 0)
        w, h = client_size(host_wid)
        if w <= 0 or h <= 0:
            # 兜底：host 尚未布局完成，用逻辑尺寸 × DPI 比估算
            ratio = self.devicePixelRatioF()
            w = int(round(self.host.width() * ratio))
            h = int(round(self.host.height() * ratio))
        if w > 0 and h > 0:
            resize_child(self._hwnd, w, h)
            # Tk 窗口：同步 wrapper 尺寸，让 Tk 收到 WM_SIZE 后重算画布。
            # 否则 Tk 画布仍是启动时的尺寸（wrapper 在屏幕外尺寸没变），内容显示不完整。
            wrapper = getattr(self, "_wrapper", None)
            if wrapper and is_window(wrapper):
                try:
                    wr = wintypes.RECT()
                    cr = wintypes.RECT()
                    if (_user32.GetWindowRect(wrapper, ctypes.byref(wr))
                            and _user32.GetClientRect(wrapper, ctypes.byref(cr))):
                        bw = (wr.right - wr.left) - (cr.right - cr.left)  # 左右边框和
                        bh = (wr.bottom - wr.top) - (cr.bottom - cr.top)  # 上下边框和（含标题栏）
                        _user32.SetWindowPos(
                            wrapper, 0, 0, 0, int(w) + bw, int(h) + bh,
                            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
                except Exception:  # noqa: BLE001
                    pass

    def _start_watch(self) -> None:
        """开始监视内嵌窗口是否被外部解绑（如 ApiCluster 从托盘弹出独立窗口）。"""
        if self._watch_timer is None:
            self._watch_timer = QTimer(self)
            self._watch_timer.timeout.connect(self._check_detached)
        self._watch_timer.start(800)

    def _stop_watch(self) -> None:
        if self._watch_timer is not None:
            self._watch_timer.stop()

    def _check_detached(self) -> None:
        """持续监视窗口状态：
        - 内嵌时被外部（托盘）解绑 → 进入独立窗口模式；
        - 独立窗口被关闭/隐藏 → 自动重新抓回内嵌（无需切换 Tab）。
        """
        if not IS_WIN:
            self._stop_watch()
            return
        if not self.isVisible():
            return  # 页面不可见时不动作，交给 showEvent → start_embed
        hwnd = self._hwnd
        host_wid = getattr(self, "_host_wid", 0)

        if self._embedded:
            if not (hwnd and is_window(hwnd)):
                self._embedded = False
                self._hwnd = None
                self._external = False
                self._stop_watch()
                return
            if host_wid and get_parent(hwnd) != host_wid:
                self._embedded = False
                self._external = True
                self.status.setText(f"{self.tool['name']} · 独立窗口")
                self.host.update()
            return

        if self._external:
            if hwnd and is_window(hwnd):
                # 独立窗口被关闭：非 Tk 窗口走 SW_HIDE（IsWindowVisible=False）；
                # Tk 窗口走「移屏幕外」（保持 WS_VISIBLE，用 wrapper 位置判断）。
                if not _user32.IsWindowVisible(hwnd) or self._wrapper_offscreen():
                    self._external = False
                    self._embed(hwnd)
            else:
                # 独立窗口已销毁（进程退出）→ 重新启动并内嵌
                self._external = False
                self._hwnd = None
                self.start_embed()

    def _wrapper_offscreen(self) -> bool:
        """Tk 窗口的 wrapper 是否被移到屏幕外（RAG 关闭独立窗口时用移屏幕外而非 SW_HIDE）。"""
        wrapper = getattr(self, "_wrapper", None)
        if not (wrapper and is_window(wrapper)):
            return False
        try:
            rect = wintypes.RECT()
            if not _user32.GetWindowRect(wrapper, ctypes.byref(rect)):
                return False
            return rect.left <= -30000 or rect.top <= -30000
        except Exception:  # noqa: BLE001
            return False

    def resizeEvent(self, event):  # noqa: ANN001
        super().resizeEvent(event)
        if self._embedded and self._hwnd:
            QTimer.singleShot(0, self._resize_child)

    def _restore_tool_window(self) -> None:
        """把内嵌窗口还原为独立窗口。

        Tk 窗口需把 client 挂回 wrapper 并显示 wrapper（否则只剩隐藏的 wrapper 或
        无边框的 client）；普通单窗口工具走原 restore_window 逻辑。"""
        hwnd = self._hwnd
        if not (hwnd and is_window(hwnd)):
            return
        wrapper = getattr(self, "_wrapper", None)
        if wrapper and is_window(wrapper):
            _orig_styles.pop(hwnd, None)  # client 不改样式，丢弃样式记录
            _orig_geometry.pop(hwnd, None)
            _orig_visible.pop(hwnd, None)
            try:
                _user32.SetParent(hwnd, wrapper)
                # 内嵌时 wrapper 被移到屏幕外，detach 要移回内嵌前的位置，
                # 否则独立窗口停在屏幕外/左上角，用户会以为工具「打不开」。
                rect = getattr(self, "_wrapper_rect", None)
                if rect and rect[2] > rect[0] and rect[3] > rect[1]:
                    _user32.SetWindowPos(wrapper, 0, rect[0], rect[1], 0, 0,
                                         SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                else:
                    _user32.SetWindowPos(wrapper, 0, 100, 100, 0, 0,
                                         SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                # 移除工具窗口样式（内嵌时加的），恢复任务栏图标
                _user32.SetWindowLongW(wrapper, -20,  # GWL_EXSTYLE
                                       _user32.GetWindowLongW(wrapper, -20) & ~0x00000080)
                _user32.ShowWindow(wrapper, SW_SHOW)
                _user32.ShowWindow(hwnd, SW_SHOW)
            except Exception:  # noqa: BLE001
                pass
        else:
            restore_window(hwnd)

    def open_external(self) -> None:
        """退出内嵌，让工具回到独立窗口并置前；关闭独立窗口后自动重新内嵌。"""
        if not tool_installed(self.tool):
            self.status.setText(f"{self.tool['name']} · 该功能暂未安装")
            return
        if self._hwnd and is_window(self._hwnd):
            self._restore_tool_window()
            self._embedded = False
            self._external = True
        else:
            self._hwnd = None
            self._external = True
        launch_tool(self.tool, self)
        self.status.setText(f"{self.tool['name']} · 独立窗口")
        self._start_watch()  # 监视独立窗口：被关闭后自动重新内嵌

    def restart(self) -> None:
        if not tool_installed(self.tool):
            self.status.setText(f"{self.tool['name']} · 该功能暂未安装")
            return
        self._stop_watch()
        self.quit_tool(force=True)
        self._hwnd = None
        self._embedded = False
        self._external = False
        self._attempts = 0
        self.status.setText("正在重启…")
        if launch_tool(self.tool, self, extra_args=self.tool.get("embed_args"), quiet=True):
            self._schedule_find()

    def quit_tool(self, force: bool = False) -> None:
        """结束工具进程。先终止进程再清状态（不做还原，避免窗口闪一下再消失）。"""
        self._stop_watch()
        self._hwnd = None
        self._embedded = False
        self._external = False
        proc = _processes.get(self.tool["key"])
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        if not force:
            self.status.setText(f"{self.tool['name']} · 已退出")

    def detach(self) -> None:
        """应用退出/隐藏前调用：把内嵌窗口还原为独立窗口，让工具继续独立运行。

        退出 LifeSystem 不应影响集成工具的独立使用：这里只解绑窗口、不终止进程，
        ApiCluster / RAG / Arxiver 都会还原成独立窗口（或托盘）继续运行，
        用户可随时继续使用其各项功能。
        幂等：可被 closeEvent / hideEvent / WM_ENDSESSION / aboutToQuit 重复调用。
        """
        self._stop_watch()
        if self._hwnd and is_window(self._hwnd):
            self._restore_tool_window()  # 还原窗口（清理样式 / 恢复 Tk wrapper）
        # 保留 _hwnd：从托盘恢复显示时可直接重新内嵌，无需再轮询找窗口
        self._embedded = False
        self._external = False
        if self._find_timer is not None:
            self._find_timer.stop()


def detach_all() -> None:
    """把所有仍挂在本进程窗口下的工具窗口解绑，还原成独立窗口继续运行。

    必须尽早调用：Win32「父窗口销毁 → 其所有子窗口被自动销毁」的规则对跨进程
    SetParent 一样生效。一旦宿主 HWND 先没了解绑就来不及——工具窗口会被系统
    一起销毁，进程还在、托盘图标还在，但窗口没了，就成了点不开的幽灵图标。

    这里除遍历所有内嵌宿户外，还兜底遍历 _orig_styles，即使某个页面状态丢失
    （_hwnd 为空）也能把窗口救回来。幂等，可重复调用。
    """
    for ref in list(_hosts):
        host = ref()
        if host is None:
            try:
                _hosts.remove(ref)
            except ValueError:  # noqa: PERF203
                pass
            continue
        try:
            host.detach()
        except Exception:  # noqa: BLE001
            pass
    if not IS_WIN:
        return
    for hwnd in list(_orig_styles):
        try:
            restore_window(hwnd)
        except Exception:  # noqa: BLE001
            pass
