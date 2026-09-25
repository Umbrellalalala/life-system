"""格子放不下时，点「+N」弹出的当天清单。

对齐滴答：标题是 ISO 日期，下面把当天所有条目按色条排出来，超出高度在里面滚。
点其中一条 = 点格子里的那根条，开的还是同一张编辑卡。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QDate, QPoint, Signal
from PySide6.QtWidgets import QWidget, QLabel, QScrollArea, QVBoxLayout

from ... import popups
from . import style
from .month_view import BAR_H, TaskBar

WIDTH = 236
MAX_H = 330          # 约十行高，再多在里面滚


class DayPopup(popups.PopupCard):
    """当天的完整条目列表（浮层）。"""

    bar_clicked = Signal(dict, QPoint)

    def __init__(self, date: QDate, rows: list[dict],
                 parent: QWidget | None = None):
        super().__init__(parent, width=WIDTH)
        style.apply_to(self)
        self.card.setObjectName("CalDayCard")
        cap = QLabel(date.toString("yyyy-MM-dd"))
        cap.setObjectName("CalDayPopDate")
        self.lay.addWidget(cap)

        scroll = QScrollArea()
        scroll.setObjectName("CalDayPopScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        col = QVBoxLayout(body)
        # 右边不再留 8px：Qt 自己会给竖向滚动条腾位子，再留就是让标题提前八个
        # 像素被截断
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        for row in rows:
            bar = TaskBar(row, draggable=False)
            bar.clicked_.connect(self._on_bar)
            col.addWidget(bar)
        col.addStretch(1)
        scroll.setWidget(body)
        scroll.verticalScrollBar().setObjectName("CalDayPopBar")
        # 条数少的时候别按上限撑开：下面空一片很难看
        scroll.setFixedHeight(min(MAX_H, len(rows) * (BAR_H + 2) + 6))
        self.lay.addWidget(scroll)
        self.adjustSize()

    def _on_bar(self, row: dict, pos: QPoint) -> None:
        self.bar_clicked.emit(row, pos)
        self.close()
