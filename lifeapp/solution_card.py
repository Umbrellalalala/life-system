"""题目解法卡：语言 + 思路 + 代码，算法页和八股页共用一套。

两个页面的落库函数不同（algo_* / interview_*），所以这里不直接调 services，
由页面把 on_save / on_delete 注进来。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from . import popups, services, widgets


def code_font() -> QFont:
    """代码用等宽字体；Consolas 不在就退回系统默认等宽。"""
    f = QFont("Consolas")
    f.setStyleHint(QFont.Monospace)
    f.setPixelSize(13)
    return f


def lang_label(key: str) -> str:
    return services.SOLUTION_LANG_LABEL.get(
        (key or "").strip(), (key or "").strip() or "未设语言")


def summary(sols: list[dict]) -> str:
    """「共 3 版：C++×2、Python」——解法区标题用。"""
    if not sols:
        return ""
    by_lang: dict[str, int] = {}
    for sol in sols:
        key = lang_label(sol.get("language"))
        by_lang[key] = by_lang.get(key, 0) + 1
    return "共 %d 版：%s" % (
        len(sols), "、".join("%s×%d" % (k, v) if v > 1 else k
                            for k, v in by_lang.items()))


def as_text(sols: list[dict], caption: str = "解法") -> str:
    """把存过的解法排成文字，复习时跟在标准答案后面。"""
    lines = []
    for idx, s in enumerate(sols, start=1):
        lines.append("【%s %d · %s】" % (caption, idx, lang_label(s.get("language"))))
        idea = (s.get("idea") or "").strip()
        if idea:
            lines.append("思路：" + idea)
        body = (s.get("body") or "").rstrip()
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines).strip()


class SolutionCard(QFrame):
    """一版解法：语言 + 思路 + 代码。同一题可以并存多语言、同语言也能多版。"""

    def __init__(self, sol: dict, *, caption: str = "解法",
                 show_source: bool = False,
                 on_save, on_delete, on_touch, on_commit):
        super().__init__()
        self.sol_id = int(sol["id"])
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_touch = on_touch
        self._on_commit = on_commit
        self._loading = True
        self.setObjectName("InnerCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        cap = QLabel(caption)
        cap.setObjectName("CardTitle")
        head.addWidget(cap)
        self.lang = widgets.ComboBox()
        for key, label in services.SOLUTION_LANGUAGES:
            self.lang.addItem(label, key)
        idx = self.lang.findData(sol.get("language") or services.SOLUTION_DEFAULT_LANG)
        self.lang.setCurrentIndex(max(idx, 0))
        self.lang.setFixedWidth(96)
        self.lang.currentIndexChanged.connect(self._on_lang)
        head.addWidget(self.lang)
        head.addStretch(1)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("Ghost")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._delete)
        head.addWidget(del_btn)
        lay.addLayout(head)

        self.idea = QPlainTextEdit()
        self.idea.setPlaceholderText("思路：怎么想到的、复杂度、有什么坑")
        self.idea.setPlainText(sol.get("idea") or "")
        self.idea.setMinimumHeight(56)
        self.idea.textChanged.connect(self._touch)
        lay.addWidget(self.idea)

        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("代码")
        self.body.setFont(code_font())
        self.body.setPlainText(sol.get("body") or "")
        self.body.setMinimumHeight(150)
        self.body.setTabChangesFocus(False)
        self.body.textChanged.connect(self._touch)
        lay.addWidget(self.body)

        self.source = None
        if show_source:
            self.source = QLineEdit()
            self.source.setPlaceholderText("出处 / 参考链接（可留空）")
            self.source.setText(sol.get("source") or "")
            self.source.editingFinished.connect(self._touch)
            lay.addWidget(self.source)

        self._loading = False

    def language(self) -> str:
        return self.lang.currentData() or services.SOLUTION_DEFAULT_LANG

    def _on_lang(self) -> None:
        if self._loading:
            return
        self._on_save(self.sol_id, language=self.language())
        self._on_commit()

    def _touch(self) -> None:
        self._on_touch()

    def _delete(self) -> None:
        label = services.solution_language_label(self.language())
        if not popups.confirm(self, "删除这版解法",
                              "%s 的思路和代码都会删掉。" % label):
            return
        self._on_delete(self.sol_id)
        self._on_commit()

    def flush(self) -> None:
        fields = {"body": self.body.toPlainText(),
                  "idea": self.idea.toPlainText()}
        if self.source is not None:
            fields["source"] = self.source.text()
        self._on_save(self.sol_id, **fields)
