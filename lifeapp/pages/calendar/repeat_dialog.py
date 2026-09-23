"""「修改重复任务」确认框：拖拽 / 改期一个重复周期时问「改哪些周期」。

对齐滴答清单的原话：
    修改重复任务
    你正在修改重复任务的时间，请确认修改范围。
        ◉ 仅此周期
        ○ 所有未完成周期
                                 [确定] [取消]
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QButtonGroup,
)


class RepeatDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, desc: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        head = QLabel(title)
        head.setObjectName("DialogHead")
        lay.addWidget(head)
        body = QLabel(desc)
        body.setObjectName("DialogBody")
        body.setWordWrap(True)
        lay.addWidget(body)

        self.only_one = QRadioButton("仅此周期")
        self.all_open = QRadioButton("所有未完成周期")
        group = QButtonGroup(self)
        group.addButton(self.only_one)
        group.addButton(self.all_open)
        self.only_one.setChecked(True)
        for r in (self.only_one, self.all_open):
            r.setCursor(Qt.PointingHandCursor)
            lay.addWidget(r)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    @staticmethod
    def ask(parent: QWidget, title: str, desc: str) -> str | None:
        """返回 'one' / 'all'，取消返回 None。"""
        dlg = RepeatDialog(parent, title, desc)
        if dlg.exec() != QDialog.Accepted:
            return None
        return "one" if dlg.only_one.isChecked() else "all"
