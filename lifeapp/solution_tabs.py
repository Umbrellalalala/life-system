"""解法编辑区：一版解法 = 一个语言标签，标签排成一行，下面只有一个编辑区。

抄的是力扣题解页那种排布：[C++] [Python] [＋]  —— 加一版解法不会再往下堆一张
卡，只多一个标签。切换标签就是切换在编辑哪一版。

落库函数两个页面不一样（algo_* / interview_*），所以这里不碰 services，
全部通过回调注进来。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from . import popups, services, widgets
from .code_view import CodeEdit


def lang_label(key: str) -> str:
    return services.solution_language_label(key)


def summary(sols: list[dict]) -> str:
    """「共 3 版：C++×2、Python」—— 解法区标题用。"""
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


class SolutionTabs(QWidget):
    def __init__(self, *, caption: str = "解法", show_source: bool = False,
                 on_save, on_delete, on_add, on_touch, on_commit):
        super().__init__()
        self._caption = caption
        self._show_source = show_source
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_add = on_add
        self._on_touch = on_touch
        self._on_commit = on_commit
        self._sols: list[dict] = []
        self._idx = -1
        self._loading = False
        self.current_id = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.track = QWidget()
        self.track.setObjectName("SegTrack")
        tl = QHBoxLayout(self.track)
        tl.setContentsMargins(3, 3, 3, 3)
        tl.setSpacing(3)
        self._tab_btns: list[QPushButton] = []
        tl.addStretch(1)
        self.add_btn = QPushButton("＋")
        self.add_btn.setObjectName("SegTab")
        # 不压缩：＋ 一旦被省略号吃掉，就没人找得到「加一版」在哪了
        self.add_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setToolTip("再存一版解法（默认 C++）；同一语言也能存多版")
        self.add_btn.clicked.connect(self._add)
        tl.insertWidget(tl.count() - 1, self.add_btn)
        row.addWidget(self.track, 1)

        self.lang_box = widgets.ComboBox()
        for key, label in services.SOLUTION_LANGUAGES:
            self.lang_box.addItem(label, key)
        self.lang_box.setFixedWidth(124)
        self.lang_box.setToolTip("这一版用什么语言写的")
        self.lang_box.currentIndexChanged.connect(self._on_lang)
        row.addWidget(self.lang_box)

        self.del_btn = QPushButton("删除这版")
        self.del_btn.setObjectName("Ghost")
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.clicked.connect(self._delete)
        row.addWidget(self.del_btn)
        lay.addLayout(row)

        self.empty_hint = QLabel("还没写解法 —— 点右边的 ＋ 加一版")
        self.empty_hint.setObjectName("Muted")
        lay.addWidget(self.empty_hint)

        # 思路并入页面下方的「备注」，这里只留代码块，把它能让出来的高度全给代码
        self.code = CodeEdit(language=services.SOLUTION_DEFAULT_LANG)
        self.code.setPlaceholderText("代码")
        self.code.setMinimumHeight(260)
        self.code.textChanged.connect(self._touch)
        lay.addWidget(self.code)

        self.source = None
        if show_source:
            self.source = QLineEdit()
            self.source.setPlaceholderText("出处 / 参考链接（可留空）")
            self.source.editingFinished.connect(self._touch)
            lay.addWidget(self.source)

        self._sync_visible()

    # ------------------------------------------------------------------ 渲染
    def set_solutions(self, sols: list[dict], select_id: int = 0) -> None:
        self._sols = list(sols)
        if select_id:
            self._idx = next(
                (i for i, s in enumerate(self._sols) if int(s["id"]) == int(select_id)),
                max(len(self._sols) - 1, -1))
        elif self._idx >= len(self._sols):
            self._idx = len(self._sols) - 1
        elif not self._sols:
            self._idx = -1
        elif self._idx < 0:
            self._idx = 0 if self._sols else -1
        self._rebuild_tabs()
        self._load_current()

    def _rebuild_tabs(self) -> None:
        lay = self.track.layout()
        # deleteLater 要等事件循环才生效，光靠它的话旧标签会一直赖在布局里，
        # 每次重画都多出一排 —— 先 takeAt 摘掉再删。末尾两项是 ＋ 和 stretch。
        while lay.count() > 2:
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._tab_btns = []
        # 只剩最右边那个 stretch 和 ＋ 时，插到 stretch 之前
        at = lay.count() - 2
        for i, sol in enumerate(self._sols):
            b = QPushButton(self._label_at(i))
            b.setObjectName("SegTab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            # 宽度够就按文案排，不够就压到 40px 出省略号 —— 否则标签一多，
            # 整行会把面板顶出横向滚动条
            b.setMinimumWidth(40)
            b.clicked.connect(lambda _=False, k=i: self._select(k))
            self.track.layout().insertWidget(max(at, 0) + i, b)
            self._tab_btns.append(b)
        if self._sols and 0 <= self._idx < len(self._tab_btns):
            self._tab_btns[self._idx].setChecked(True)

    def _label_at(self, i: int) -> str:
        """同一种语言存了两版时，第二版标成「C++ 2」。"""
        lang = services.solution_language_label(self._sols[i].get("language"))
        same = [j for j, s in enumerate(self._sols)
                if (s.get("language") or "") == (self._sols[i].get("language") or "")]
        if len(same) <= 1:
            return lang
        return "%s %d" % (lang, same.index(i) + 1)

    def _load_current(self) -> None:
        self._loading = True
        has = bool(self._sols) and 0 <= self._idx < len(self._sols)
        self.current_id = int(self._sols[self._idx]["id"]) if has else 0
        sol = self._sols[self._idx] if has else {}
        self.code.setPlainText(sol.get("body") or "")
        self.code.set_language(sol.get("language") or services.SOLUTION_DEFAULT_LANG)
        if self.source is not None:
            self.source.setText(sol.get("source") or "")
        i = self.lang_box.findData(sol.get("language") or services.SOLUTION_DEFAULT_LANG)
        self.lang_box.setCurrentIndex(max(i, 0))
        self._loading = False
        self._sync_visible()

    def _sync_visible(self) -> None:
        has = bool(self._sols)
        self.empty_hint.setVisible(not has)
        for w in (self.code, self.lang_box, self.del_btn):
            w.setVisible(has)
        if self.source is not None:
            self.source.setVisible(has)

    # ------------------------------------------------------------------ 交互
    def _select(self, i: int) -> None:
        if self._idx == i:
            return
        self.flush()               # 先把上一版写掉，别让没落盘的字丢了
        self._idx = i
        self._rebuild_tabs()
        self._load_current()

    def _add(self) -> None:
        self.flush()
        self._on_add()

    def _on_lang(self) -> None:
        if self._loading or not self.current_id:
            return
        lang = self.lang_box.currentData() or services.SOLUTION_DEFAULT_LANG
        self._on_save(self.current_id, language=lang)
        self._on_commit()

    def _delete(self) -> None:
        if not self.current_id:
            return
        lang = services.solution_language_label(
            self._sols[self._idx].get("language"))
        if not popups.confirm(self, "删除这版%s" % self._caption,
                              "%s 的思路和代码都会删掉。" % lang):
            return
        self._on_delete(self.current_id)
        self._on_commit()

    def _touch(self) -> None:
        if not self._loading:
            self._on_touch()

    def flush(self) -> None:
        if not self.current_id:
            return
        # 只写 body / source：界面上没有思路框了，把 idea 一起发过去
        # 等于每次自动保存都把库里已有的思路清空
        fields = {"body": self.code.toPlainText()}
        if self.source is not None:
            fields["source"] = self.source.text().strip()
        self._on_save(self.current_id, **fields)

    def refresh_theme(self) -> None:
        self.code.refresh_theme()

    def tab_labels(self) -> list[str]:
        return [b.text() for b in self._tab_btns]
