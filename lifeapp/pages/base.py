"""页面基类与通用头部。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QHBoxLayout, QScrollArea, QFrame,
)

from .. import widgets


class Page(QWidget):
    """所有功能页的基类。

    scrollable=True 时，内容区放入滚动区域，适合内容较多的页面。
    bare=True 时不自带标题/副标题（页面自行搭建整块布局，例如番茄钟）。
    """

    def __init__(self, title: str, subtitle: str = "", scrollable: bool = False,
                 bare: bool = False):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(16)

        # 头部：大标题独占一行，右边留位给页面挂东西（例如缩小的统计胶囊）
        self._header: QHBoxLayout | None = None
        if not bare:
            head = QHBoxLayout()
            head.setSpacing(10)
            title_lbl = QLabel(title)
            title_lbl.setObjectName("PageTitle")
            head.addWidget(title_lbl)
            head.addStretch(1)
            outer.addLayout(head)
            self._header = head
            if subtitle:
                sub = QLabel(subtitle)
                sub.setObjectName("PageSubtitle")
                outer.addWidget(sub)

        if scrollable:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            content = QWidget()
            self._layout = QVBoxLayout(content)
            self._layout.setContentsMargins(0, 0, 8, 0)
            self._layout.setSpacing(16)
            scroll.setWidget(content)
            outer.addWidget(scroll, 1)
            self.scroll_area = scroll
        else:
            self._layout = outer
            self.scroll_area: QScrollArea | None = None

    def body(self) -> QVBoxLayout:
        return self._layout

    def header(self) -> QHBoxLayout | None:
        """大标题所在那一行（bare 页没有）。末尾是 stretch，直接 addWidget 就贴右。"""
        return self._header


def stats_row(cards: list[widgets.StatCard]) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(12)
    for c in cards:
        row.addWidget(c)
    return row
