"""应用入口。"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar,
)

from . import config, db, services, sounds, theme
from .main_window import MainWindow


class SplashScreen(QWidget):
    """启动画面：标题 + 滚动进度条 + 状态文字。"""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(340, 132)

        card = QVBoxLayout(self)
        card.setContentsMargins(1, 1, 1, 1)
        inner = QWidget()
        inner.setObjectName("SplashInner")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(10)

        title = QLabel("Life System")
        title.setObjectName("AppTitle")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        self.msg = QLabel("正在加载…")
        self.msg.setObjectName("Meta")
        self.msg.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.msg)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # 不确定进度（滚动）
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)

        card.addWidget(inner)
        self.setStyleSheet(
            "QWidget#SplashInner { background: #ffffff;"
            " border: 1px solid #e6e8f0; border-radius: 14px; }"
        )

    def set_message(self, text: str) -> None:
        self.msg.setText(text)


def main() -> int:
    # 确保工作目录指向 exe / 脚本所在目录，否则从注册表 Run 键
    # 开机自启时工作目录为 C:\Windows\System32，会导致找不到依赖。
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))
    else:
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 数据目录已迁到 ~/.life_system：首次启动自动从旧位置迁移数据
    config.migrate_legacy_data()
    # 主库损坏检测：损坏则从最新备份自动恢复（无备份则隔离重建，避免崩溃）
    db.ensure_healthy()
    db.init_db()
    # 复习对账：把每道在刷题缺的复习待办补进待办的「算法复习」「八股复习」清单。
    # 每天首次启动都会跑到，因为待办是活的（可能被删/被直接勾掉）。
    try:
        services.algo_sync_reviews()
        services.interview_sync_reviews()
    except Exception:  # noqa: BLE001
        pass
    # 每天首次启动做一次热备份（保留最近 7 份）
    try:
        db.backup_db()
    except Exception:  # noqa: BLE001
        pass

    # 高分屏支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("Life System")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # 单实例：已有实例在运行时，唤起它并让本实例退出。
    # LifeSystem 是托盘常驻应用，点 × 只缩到托盘，用户再次双击 exe 很容易误开
    # 第二个实例。这里用 QLocalServer 占坑，已运行的实例收到「activate」信号后
    # 唤起自己的窗口。
    _single_name = "LifeSystem.SingleInstance.v1"
    _probe = QLocalSocket()
    _probe.connectToServer(_single_name)
    if _probe.waitForConnected(400):
        _probe.write(b"activate")
        _probe.flush()
        _probe.waitForBytesWritten(400)
        _probe.disconnectFromServer()
        return 0  # 已有实例，已唤起，本实例静默退出
    QLocalServer.removeServer(_single_name)
    _single_server = QLocalServer()
    if not _single_server.listen(_single_name):
        return 0  # 并发启动的极端情况：本实例抢不到坑，直接退出

    # Windows 任务栏图标分组（否则显示 Python 图标）
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "LifeSystem.App.1.0")
        except Exception:
            pass

    # 主题管理器（日间为默认）
    theme.manager = theme.ThemeManager(app)
    saved_dark = db.get_setting("theme", "light") == "dark"
    theme.manager.apply_immediate(saved_dark)

    def _persist_theme() -> None:
        db.set_setting("theme", "dark" if theme.manager.is_dark else "light")

    theme.manager.changed.connect(_persist_theme)

    # 应用图标（窗口 + 任务栏）
    icon_path = config.asset_path(os.path.join("assets", "icon.ico"))
    if not os.path.exists(icon_path):
        icon_path = os.path.join(config.base_dir(), "assets", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 启动画面：覆盖窗口构建 + 工具预热
    splash = SplashScreen()
    splash.show()
    app.processEvents()

    window = MainWindow()
    window.setWindowIcon(app.windowIcon())

    # 第二个实例发来的「activate」→ 唤起本实例窗口
    def _activate_instance() -> None:
        conn = _single_server.nextPendingConnection()
        if conn:
            conn.disconnectFromServer()
        if window.isVisible() and not window.isMinimized():
            window.raise_()
            window.activateWindow()
        else:
            window.show_from_tray()

    _single_server.newConnection.connect(_activate_instance)

    # 后台预启动三个集成工具
    splash.set_message("正在启动集成工具…")
    app.processEvents()
    window.prewarm_tools()

    splash.close()
    window.showMaximized()

    # 窗口出来之后再解码音效，别和启动流程抢 CPU
    QTimer.singleShot(1500, sounds.prewarm)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
