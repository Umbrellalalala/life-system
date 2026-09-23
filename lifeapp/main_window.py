"""主窗口：侧边导航 + 主题切换 + 内容区。"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut, QAction
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QStackedWidget, QButtonGroup, QPushButton, QSystemTrayIcon, QMenu,
)

from . import sounds, theme
from .reminders import ReminderService
from .pages.todo import TodoPage
from .pages.pomodoro import PomodoroPage
from .pages.weight import WeightPage
from .pages.finance import FinancePage
from .pages.note import NotePage
from .pages.research import ResearchPage
from .pages.algo import AlgoPage
from .pages.interview import InterviewPage
from .pages.calendar import CalendarPage
from .pages.habits import HabitPage
from .pages.settings import SettingsPage
from .pages import tools as tools_mod
from .pages.tool_page import ToolPage


# 侧边栏导航项：
#   ("icon", "label", PageClass, "module")  → 功能模块
#   ("—", "分组名", None, "section")         → 分组标题
#   ("icon", "label", tool_dict, "tool")     → 内嵌的外部工具
# 工具项会把对应工具的窗口直接「抓」进内容区（见 ToolPage）。
def _tool_page(tool: dict):
    class _ToolPage(ToolPage):
        def __init__(self):
            super().__init__(tool)
    return _ToolPage


NAV_ITEMS = [
    ("📋", "待办清单", TodoPage, "module"),
    ("🔔", "习惯打卡", HabitPage, "module"),
    ("📅", "日历", CalendarPage, "module"),
    ("🍅", "番茄钟", PomodoroPage, "module"),
    ("⚖️", "体重管理", WeightPage, "module"),
    ("💰", "理财管理", FinancePage, "module"),
    ("📝", "笔记记录", NotePage, "module"),
    ("🔬", "科研管理", ResearchPage, "module"),
    ("🧩", "算法刷题", AlgoPage, "module"),
    ("🗣", "八股刷题", InterviewPage, "module"),
    ("—", "工具", None, "section"),
    ("🔀", "ApiCluster", tools_mod.TOOLS[0], "tool"),
    ("📚", "RAG 助手", tools_mod.TOOLS[1], "tool"),
    ("📡", "Arxiver", tools_mod.TOOLS[2], "tool"),
    ("—", "系统", None, "section"),
    ("⚙", "设置", SettingsPage, "module"),
]

# 侧边栏两种宽度：展开显示图标+文字，收起只显示图标（窄栏，参考 api_cluster）
SIDEBAR_WIDTH = 214
SIDEBAR_COLLAPSED_WIDTH = 60
# 窗口窄到这个宽度就自动把侧栏收成图标条，宽回来再展开。
# 两个值之间留一段迟滞，否则在边界上拖窗口会来回跳。
NAV_NARROW = 880
NAV_WIDE = 960


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Life System · 个人管理系统")
        self.resize(1140, 740)
        self.setMinimumSize(620, 480)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = self._build_sidebar()
        root.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # 悬浮手柄：贴在侧边栏右缘（收起后贴窗口左缘），上下居中，点击收起/展开
        self._sidebar_collapsed = False
        self.sidebar_btn = QPushButton("‹", self)
        self.sidebar_btn.setObjectName("SidebarHandle")
        self.sidebar_btn.setCursor(Qt.PointingHandCursor)
        self.sidebar_btn.setFixedSize(14, 60)
        self.sidebar_btn.clicked.connect(self.toggle_sidebar)

        self._pages = []
        self._tool_index: dict[str, int] = {}
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        # 依次插入导航项（模块 + 工具分组），分组标题用 SideSection 标签
        insert_at = 3  # 标题(0) / 副标题(1) / 间距(2) 之后
        page_index = 0
        self._section_labels: list[QLabel] = []
        self._nav_buttons: list[tuple[QPushButton, str, str]] = []
        for icon, label, payload, kind in NAV_ITEMS:
            if kind == "section":
                sec = QLabel(label)
                sec.setObjectName("SideSection")
                self._nav_layout.insertWidget(insert_at, sec)
                self._section_labels.append(sec)
                insert_at += 1
                continue
            btn = QPushButton(f"  {icon}   {label}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            self._nav_group.addButton(btn, page_index)
            self._nav_layout.insertWidget(insert_at, btn)
            self._nav_buttons.append((btn, icon, label))
            insert_at += 1
            page = payload() if kind == "module" else ToolPage(payload)
            self._pages.append(page)
            self.stack.addWidget(page)
            if kind == "tool" and isinstance(payload, dict):
                # 供「科研管理 → 打开 Arxiver」这类跨页跳转按 key 找页面
                self._tool_index[payload.get("key", "")] = page_index
            page_index += 1

        self._page_count = page_index
        self._nav_group.idClicked.connect(self.switch_page)
        self._nav_group.button(0).setChecked(True)
        self.stack.setCurrentIndex(0)

        # 番茄钟里完成任务 → 待办清单同步刷新
        pomodoro_page = next(
            (p for p in self._pages if isinstance(p, PomodoroPage)), None)
        todo_page = next((p for p in self._pages if isinstance(p, TodoPage)), None)
        if pomodoro_page is not None and todo_page is not None:
            pomodoro_page.todo_changed.connect(todo_page.reload)

        # 待办页里点「今日打卡」行 → 跳到习惯页
        habit_idx = next(
            (i for i, p in enumerate(self._pages) if isinstance(p, HabitPage)), None)
        self._habit_page_index = habit_idx     # 习惯打卡提醒的气泡要点得开这一页
        if todo_page is not None and habit_idx is not None:
            todo_page.openHabits.connect(
                lambda: (self._nav_group.button(habit_idx).setChecked(True),
                         self.switch_page(habit_idx)))

        # 待办页点「复习：xxx」这类合成待办 → 跳到刷题页对应那道题。
        # 它们不是能编辑的任务：改标题、改备注都没意义，做完的动作在刷题页做，
        # 那边完成后会把这条自动勾掉（见 services._review_close）。
        self._review_pages = {
            "algo": next((i for i, p in enumerate(self._pages)
                          if isinstance(p, AlgoPage)), None),
            "interview": next((i for i, p in enumerate(self._pages)
                               if isinstance(p, InterviewPage)), None),
        }
        if todo_page is not None:
            todo_page.openReview.connect(self._open_review_item)

        # 习惯页 / 日历页的「开始专注」→ 跳到番茄钟页，带着条目名直接开计时
        habit_page = next(
            (p for p in self._pages if isinstance(p, HabitPage)), None)
        cal_page = next(
            (p for p in self._pages if isinstance(p, CalendarPage)), None)
        if pomodoro_page is not None:
            pomo_idx = self._pages.index(pomodoro_page)

            def start_focus(title: str, mode: str) -> None:
                self._nav_group.button(pomo_idx).setChecked(True)
                self.switch_page(pomo_idx)
                pomodoro_page.start_focus_for(title, mode)
            for src in (habit_page, cal_page):
                if src is not None:
                    src.focusRequested.connect(start_focus)

        self._position_sidebar_btn()

        self._quitting = False      # 托盘「关闭」时置 True，真正退出
        self._max_before_hide = False
        self._setup_tray()
        self._setup_reminders()

        self._setup_shortcuts()

        # 最后一道保险：无论从哪条路径退出（aboutToQuit / 异常），
        # 都先把内嵌的工具窗口还给它们自己，避免被本进程销毁连带带走。
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._detach_tools)

        # 主题切换联动
        self._update_theme_btn()
        theme.manager.changed.connect(self._on_theme_changed)

    def _setup_shortcuts(self) -> None:
        """Ctrl+1~N 快速切换模块 / 工具页；Ctrl+B 收起/展开侧边栏。"""
        for i in range(self._page_count):
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.activated.connect(lambda idx=i: (self._nav_group.button(idx).setChecked(True),
                                                self.switch_page(idx)))
        sc_sidebar = QShortcut(QKeySequence("Ctrl+B"), self)
        sc_sidebar.activated.connect(self.toggle_sidebar)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(214)
        self._nav_layout = QVBoxLayout(sidebar)
        self._nav_layout.setContentsMargins(14, 20, 14, 16)
        self._nav_layout.setSpacing(5)

        title = QLabel("Life System")
        title.setObjectName("AppTitle")
        self._sidebar_title = title
        self._nav_layout.addWidget(title)

        sub = QLabel("个人管理系统")
        sub.setObjectName("AppSubtitle")
        self._sidebar_subtitle = sub
        self._nav_layout.addWidget(sub)
        self._nav_layout.addSpacing(16)

        # 导航项由 __init__ 通过 insertWidget 插入在 index 3（标题/副标题/间距之后）

        self._nav_layout.addStretch(1)

        # 主题切换按钮（快捷；完整配置在「设置」页）
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("ThemeToggle")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self._toggle_theme)
        self._nav_layout.addWidget(self.theme_btn)

        version = QLabel("Life System v1.1.0")
        version.setObjectName("AppSubtitle")
        version.setAlignment(Qt.AlignCenter)
        self._sidebar_version = version
        self._nav_layout.addWidget(version)
        return sidebar

    def collapse_sidebar(self) -> None:
        """收起侧边栏：从宽栏收窄为图标栏（只显示图标，参考 api_cluster）。"""
        self._set_sidebar_collapsed(True)

    def expand_sidebar(self) -> None:
        """展开侧边栏：恢复图标 + 文字。"""
        self._set_sidebar_collapsed(False)

    def _set_sidebar_collapsed(self, collapsed: bool) -> None:
        """统一处理侧边栏收起/展开时的宽度与各控件显示状态。"""
        self._sidebar_collapsed = collapsed
        self.sidebar.setFixedWidth(
            SIDEBAR_COLLAPSED_WIDTH if collapsed else SIDEBAR_WIDTH)
        if collapsed:
            self._nav_layout.setContentsMargins(8, 20, 8, 16)
        else:
            self._nav_layout.setContentsMargins(14, 20, 14, 16)

        # 标题 / 副标题 / 版本号 / 分组标题：收起后隐藏
        for w in (self._sidebar_title, self._sidebar_subtitle,
                  self._sidebar_version):
            w.setVisible(not collapsed)
        for sec in self._section_labels:
            sec.setVisible(not collapsed)

        # 导航按钮：收起后只显示图标（悬停提示显示完整名称）
        for btn, icon, label in self._nav_buttons:
            btn.setProperty("collapsed", "true" if collapsed else "false")
            if collapsed:
                btn.setText(icon)
                btn.setToolTip(label)
            else:
                btn.setText(f"  {icon}   {label}")
                btn.setToolTip("")
            self._repolish(btn)

        # 主题切换按钮：收起后只显示图标
        self.theme_btn.setProperty("collapsed", "true" if collapsed else "false")
        self._update_theme_btn()
        self._repolish(self.theme_btn)

        self._position_sidebar_btn()

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        """QSS 动态属性变化后刷新样式，使 [collapsed="true"] 规则生效。"""
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def toggle_sidebar(self) -> None:
        """切换侧边栏显示/收起（Ctrl+B / 手柄）。手动操作后暂停自动收起。"""
        self._sidebar_auto = False
        if self._sidebar_collapsed:
            self.expand_sidebar()
        else:
            self.collapse_sidebar()

    def _position_sidebar_btn(self) -> None:
        """手柄吸附在侧边栏右缘（收起后侧边栏变窄，手柄随之移动）。"""
        w, h = self.sidebar_btn.width(), self.sidebar_btn.height()
        y = (self.height() - h) // 2
        self.sidebar_btn.move(self.sidebar.width() - w // 2, y)
        if self._sidebar_collapsed:
            self.sidebar_btn.setText("›")
            self.sidebar_btn.setToolTip("展开侧边栏 (Ctrl+B)")
        else:
            self.sidebar_btn.setText("‹")
            self.sidebar_btn.setToolTip("收起侧边栏 (Ctrl+B)")
        self.sidebar_btn.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "sidebar_btn", None) is not None:
            self._position_sidebar_btn()
        self._auto_narrow_sidebar()

    def _auto_narrow_sidebar(self) -> None:
        """窄窗口自动收起侧栏，宽回来再展开。

        用户手动 Ctrl+B / 点手柄之后先听用户的，直到窗口宽度再次跨过阈值
        才把控制权还给自动逻辑 —— 否则手刚收起就被 resize 又弹开。
        """
        w = self.width()
        prev = getattr(self, "_last_width", w)
        self._last_width = w
        if (prev > NAV_NARROW >= w) or (prev < NAV_WIDE <= w):
            self._sidebar_auto = True
        if not getattr(self, "_sidebar_auto", True):
            return
        if w < NAV_NARROW and not self._sidebar_collapsed:
            self.collapse_sidebar()
        elif w > NAV_WIDE and self._sidebar_collapsed:
            self.expand_sidebar()

    def prewarm_tools(self) -> None:
        """启动时后台预启动三个集成工具进程（不内嵌），切过去即内嵌。"""
        for page in self._pages:
            if isinstance(page, ToolPage):
                page.host.start_embed()

    def closeEvent(self, event):  # noqa: ANN001
        """点 × 不退出，缩小到系统托盘；托盘菜单「关闭」才真正退出。"""
        if not self._quitting:
            event.ignore()
            self._max_before_hide = self.isMaximized()
            self.hide()
            return
        # 真正退出：先还原内嵌工具窗口（越早越好，见 tools.detach_all），
        # 再保存各页面未落盘内容（如笔记）
        self._detach_tools()
        self._save_pages()
        self._tray.hide()
        # 清除主题同步文件，退出后内嵌工具恢复各自独立主题
        theme.clear_sync()
        super().closeEvent(event)

    def _setup_tray(self) -> None:
        """系统托盘图标：左键单击打开主窗口，右键菜单（打开 / 关闭）。"""
        self._tray = QSystemTrayIcon(self.windowIcon(), self)
        self._tray.setToolTip("Life System · 个人管理系统")

        menu = QMenu()
        act_open = QAction("打开", menu)
        act_open.triggered.connect(self.show_from_tray)
        act_quit = QAction("关闭", menu)
        act_quit.triggered.connect(self.really_quit)
        menu.addAction(act_open)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _setup_reminders(self) -> None:
        """到点提醒。

        以前 todos.reminder / habits.reminder 只是写进库、在行尾画一枚时钟图标，
        设了提醒的人永远等不到任何东西 —— 现在走托盘通知，窗口收进托盘也能响。
        """
        self._reminders = ReminderService(self)
        self._reminders.fired.connect(self._on_reminder)
        self._reminders.start()
        self._reminder_target = ("", 0)
        self._tray.messageClicked.connect(self._open_reminder_target)

    def _open_review_item(self, kind: str, item_id: int) -> None:
        """切到对应的刷题页并选中那道题（待办页点复习条目时过来）。

        点一下标题行会发两次 titleClicked（标签和整行各办各的），开详情时
        无所谓，但这里每次都要清筛选 + 重刷列表，不去重就是白跑两遍。
        """
        idx = getattr(self, "_review_pages", {}).get(kind)
        if idx is None:
            return
        if self.stack.currentIndex() == idx and \
                getattr(self.stack.widget(idx), "_focused_review", None) == item_id:
            return
        self._nav_group.button(idx).setChecked(True)
        self.switch_page(idx)
        page = self.stack.widget(idx)
        if hasattr(page, "focus_item"):
            page.focus_item(item_id)
            page._focused_review = item_id

    def _on_reminder(self, kind: str, ref_id: int, title: str, body: str) -> None:
        self._reminder_target = (kind, ref_id)
        sounds.play("reminder")
        self._tray.showMessage(title, body,
                               QSystemTrayIcon.MessageIcon.Information, 6000)

    def _open_reminder_target(self) -> None:
        """点通知气泡 → 打开对应页面；待办顺带把那条选出来。"""
        kind, ref_id = getattr(self, "_reminder_target", ("", 0))
        if not kind or not ref_id:
            self.show_from_tray()
            return
        if kind == "todo":
            self._nav_group.button(0).setChecked(True)
            self.switch_page(0)
            page = self.stack.widget(0)
            if hasattr(page, "_show_detail"):
                page._show_detail(ref_id)
        else:
            idx = getattr(self, "_habit_page_index", None)
            if idx is not None:
                self._nav_group.button(idx).setChecked(True)
                self.switch_page(idx)
        self.show_from_tray()

    def _on_tray_activated(self, reason) -> None:  # noqa: ANN001
        """左键单击托盘图标 → 打开主窗口。"""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_from_tray()

    def show_from_tray(self) -> None:
        """从托盘恢复主窗口（保持之前的最大化状态）。"""
        if self._max_before_hide and not self.isVisible():
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def really_quit(self) -> None:
        """托盘菜单「关闭」：真正退出应用。"""
        self._quitting = True
        self.close()

    def _save_pages(self) -> None:
        """退出前保存各页面未落盘的内容（如笔记编辑区）。"""
        for page in self._pages:
            save = getattr(page, "save_on_exit", None)
            if save is not None:
                try:
                    save()
                except Exception:  # noqa: BLE001
                    pass

    def _detach_tools(self) -> None:
        """解绑所有内嵌工具窗口（幂等）。

        用模块级 detach_all 而不是只遍历页面：即使某个页面状态异常，
        也会按底层记录的窗口句柄兜底还原。
        """
        try:
            tools_mod.detach_all()
        except Exception:  # noqa: BLE001
            pass
        for page in self._pages:
            detach = getattr(page, "detach", None)
            if detach is not None:
                try:
                    detach()
                except Exception:  # noqa: BLE001
                    pass

    def hideEvent(self, event):  # noqa: ANN001
        """缩到系统托盘时立刻解绑工具。

        内嵌窗口平时是 LifeSystem 的子窗口，只要 LifeSystem 消失（崩溃/强杀/关机）
        就会被系统连带销毁。缩到托盘期间没人会看内嵌内容，这里提前解绑，
        把「窗口挂在 LifeSystem 下」的时间缩到最短，工具随时可从自己托盘打开。
        """
        super().hideEvent(event)
        self._detach_tools()

    def nativeEvent(self, eventType, message):  # noqa: ANN001, N802
        """捕获 Windows 关机/注销：这条路径不会走 closeEvent，必须在这里解绑。"""
        try:
            if sys.platform == "win32" and eventType in (
                    "windows_generic_MSG", "windows_dispatcher_MSG"):
                import ctypes
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message in (0x0011, 0x0016):  # WM_QUERYENDSESSION / WM_ENDSESSION
                    self._detach_tools()
        except Exception:  # noqa: BLE001
            pass
        return super().nativeEvent(eventType, message)

    def _toggle_theme(self) -> None:
        theme.manager.toggle()

    def _update_theme_btn(self) -> None:
        dark = theme.manager.is_dark
        if self._sidebar_collapsed:
            self.theme_btn.setText("☀️" if dark else "🌙")
            self.theme_btn.setToolTip("切换主题")
        else:
            self.theme_btn.setText("☀️  日间模式" if dark else "🌙  夜间模式")
            self.theme_btn.setToolTip("")

    def _on_theme_changed(self) -> None:
        self._update_theme_btn()
        self._refresh_all(self)

    @staticmethod
    def _refresh_all(widget: QWidget) -> None:
        widget.update()
        for child in widget.findChildren(QWidget):
            child.update()

    def switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)

    def open_tool(self, key: str) -> bool:
        """按工具 key 跳到对应的内嵌工具页（科研管理里「打开 Arxiver」用）。

        返回是否找到；找不到时调用方自己决定兜底（比如打开工具的库目录）。
        """
        idx = self._tool_index.get(key)
        if idx is None:
            return False
        btn = self._nav_group.button(idx)
        if btn is not None:
            btn.setChecked(True)
        self.switch_page(idx)
        return True
