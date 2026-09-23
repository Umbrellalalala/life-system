"""工具内嵌页：作为侧边栏的一个独立导航项，把外部工具窗口抓进内容区。"""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from .tools import ToolEmbedHost


class ToolPage(QWidget):
    def __init__(self, tool: dict):
        super().__init__()
        self.setObjectName("Root")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.host = ToolEmbedHost(tool)
        lay.addWidget(self.host, 1)

    def detach(self) -> None:
        """应用退出时还原内嵌窗口。"""
        self.host.detach()
