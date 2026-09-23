"""滴答清单式的「添加清单 / 添加标签 / 添加过滤器」对话框。

对齐参考图的两处关键做法：
- 清单对话框左边是表单、右边是一张实时预览卡片；
- 过滤器分「普通 / 高级」两个 tab，普通 tab 用一行行「字段名 + 控件」排下来。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QRectF, Signal
from PySide6.QtGui import QConicalGradient, QColor, QPainter
from PySide6.QtWidgets import (
    QDialog, QWidget, QFrame, QLabel, QLineEdit, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QCheckBox, QRadioButton, QButtonGroup,
    QSizePolicy,
)

from . import services, theme, widgets
from .todo_icons import TickIcon, ColorDot

# 清单可选图标（都是 todo_icons 里已有的线性图标名）
LIST_ICONS = ["list", "folder", "tag", "flag", "calendar", "inbox", "archive",
              "habit", "note", "clock", "filter", "pin"]
# 调色板：第 0 个是「无颜色」，最后一个是滴答的「彩色」渐变项
SWATCHES = ["", "#f0435f", "#ed9a12", "#e8c33a", "#0db987",
            "#3d8bff", "#8b5cf6", "gradient"]
DATE_FILTERS = [("all", "所有"), ("today", "今天"), ("overdue", "已过期"),
                ("next7", "最近7天"), ("none", "无日期")]


def _c(key: str) -> str:
    return theme.get(key)


class SwatchRow(QWidget):
    """一行颜色圆点，单选。"""

    picked = Signal(str)

    def __init__(self, current: str = "", parent=None):
        super().__init__(parent)
        self._value = current
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._btns: list[QFrame] = []
        for hex_value in SWATCHES:
            btn = QFrame()
            btn.setObjectName("Swatch")
            btn.setFixedSize(21, 21)
            btn.setCursor(Qt.PointingHandCursor)
            inner = QHBoxLayout(btn)
            inner.setContentsMargins(3, 3, 3, 3)
            if not hex_value:
                inner.addWidget(TickIcon("none", 14, "muted"))
            elif hex_value == "gradient":
                inner.addWidget(_GradientDot(15))
            else:
                inner.addWidget(ColorDot(hex_value, 15))
            btn.clicked_value = hex_value          # noqa: SLF001 仅作数据挂载
            btn.mouseReleaseEvent = lambda _e, b=btn: self._pick(b)  # noqa: E731
            lay.addWidget(btn)
            self._btns.append(btn)
        lay.addStretch(1)
        self._sync()

    @property
    def value(self) -> str:
        return "" if self._value == "gradient" else self._value

    def _pick(self, btn: QFrame) -> None:
        self._value = btn.clicked_value            # noqa: SLF001
        self._sync()
        self.picked.emit(self.value)

    def _sync(self) -> None:
        for b in self._btns:
            widgets._apply_property(b, "checked",
                                    "true" if b.clicked_value == self._value else "false")  # noqa: SLF001


class _GradientDot(QWidget):
    def __init__(self, size: int = 14, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QConicalGradient(self.width() / 2, self.height() / 2, 90)
        for i, h in enumerate(("#f0435f", "#ed9a12", "#0db987",
                               "#3d8bff", "#8b5cf6", "#f0435f")):
            g.setColorAt(i / 5, QColor(h))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))


class MultiPickButton(QPushButton):
    """「所有 / 已选 N 项」按钮 + 可多选的弹层。"""

    changed = Signal()

    def __init__(self, options: list[tuple[str, str]], parent=None):
        super().__init__("所有", parent)
        self.setObjectName("Ghost")
        self._options = options
        self._selected: list[str] = []
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.clicked.connect(self._open)

    @property
    def values(self) -> list[str]:
        return list(self._selected)

    def set_values(self, values: list[str]) -> None:
        self._selected = [v for v, _ in self._options if v in values]
        self._sync_text()

    def _sync_text(self) -> None:
        if not self._selected:
            self.setText("所有")
            return
        labels = [lab for v, lab in self._options if v in self._selected]
        self.setText("、".join(labels[:2]) + ("…" if len(labels) > 2 else ""))

    def _open(self) -> None:
        pop = _CheckPopup(self._options, self._selected, self)
        pop.picked.connect(self._on_picked)
        pop.adjustSize()
        pop.move(self.mapToGlobal(QPoint(0, self.height())))
        pop.show()

    def _on_picked(self, values: list) -> None:
        self._selected = values
        self._sync_text()
        self.changed.emit()


class _CheckPopup(QFrame):
    picked = Signal(list)

    def __init__(self, options: list[tuple[str, str]], selected: list[str],
                 parent=None):
        # Frameless + NoDropShadow：否则 Windows 按矩形给 Popup 加原生投影，
        # 圆角外面糊出一圈 ~#b5b5b5 的深灰描边（看起来就是"黑边"）。
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        card = QFrame(self)
        card.setObjectName("TickMenuCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)
        self._checks: dict[str, QCheckBox] = {}
        for value, label in options:
            cb = QCheckBox(label)
            cb.setObjectName("CheckRow")
            cb.setChecked(value in selected)
            self._checks[value] = cb
            lay.addWidget(cb)
        row = QHBoxLayout()
        ok = QPushButton("确定")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._accept)
        none = QPushButton("清空")
        none.setObjectName("Ghost")
        none.clicked.connect(lambda: [c.setChecked(False) for c in self._checks.values()])
        row.addWidget(ok)
        row.addWidget(none)
        lay.addLayout(row)
        self.setFixedWidth(max(180, card.sizeHint().width()))

    def _accept(self) -> None:
        self.picked.emit([v for v, c in self._checks.items() if c.isChecked()])
        self.close()


def _field_row(label: str, w: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    lbl = QLabel(label)
    lbl.setObjectName("DlgField")
    lbl.setFixedWidth(64)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(lbl)
    row.addWidget(w, 1)
    return row


class _BaseDialog(QDialog):
    """统一的标题 + 底部按钮骨架。"""

    def __init__(self, title: str, parent=None, width: int = 380):
        super().__init__(parent)
        self.setObjectName("TickDialog")
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(20, 16, 20, 16)
        self._root.setSpacing(12)
        head = QLabel(title)
        head.setObjectName("DlgTitle")
        head.setAlignment(Qt.AlignCenter)
        self._root.addWidget(head)

    def _footer(self, ok_text: str = "保存", on_ok=None, extra=None) -> None:
        row = QHBoxLayout()
        if extra is not None:
            row.addWidget(extra)
        row.addStretch(1)
        ok = QPushButton(ok_text)
        ok.setObjectName("Primary")
        ok.setEnabled(False)
        ok.clicked.connect(on_ok or self.accept)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        self._root.addLayout(row)
        self._ok_btn = ok

    def _set_valid(self, on: bool) -> None:
        self._ok_btn.setEnabled(on)


class ListDialog(_BaseDialog):
    """添加 / 编辑清单：名称 + 图标 + 颜色 + 视图 + 文件夹 + 类型 + 预览。"""

    def __init__(self, parent=None, list_row: dict | None = None,
                 folder_id: int = 0):
        self._row = list_row
        title = "编辑清单" if list_row else "添加清单"
        super().__init__(title, parent, width=660)
        body = QHBoxLayout()
        body.setSpacing(16)
        self._root.addLayout(body)

        form = QWidget()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(12)
        body.addWidget(form, 1)

        # 名称 + 图标选择
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.icon_btn = QPushButton()
        self.icon_btn.setObjectName("ToolBtn")
        self.icon_btn.setFixedSize(30, 30)
        self.icon_btn.setCursor(Qt.PointingHandCursor)
        self._icon_kind = (list_row or {}).get("icon") or "list"
        self._icon_color = (list_row or {}).get("color") or ""
        self._repaint_icon_btn()
        self.icon_btn.clicked.connect(self._pick_icon)
        name_row.addWidget(self.icon_btn)
        self.name_edit = QLineEdit((list_row or {}).get("name", ""))
        self.name_edit.setPlaceholderText("名称")
        self.name_edit.textChanged.connect(lambda t: self._set_valid(bool(t.strip())))
        name_row.addWidget(self.name_edit, 1)
        fl.addLayout(name_row)

        self.swatches = SwatchRow((list_row or {}).get("color", ""))
        self.swatches.picked.connect(self._on_color)
        fl.addLayout(_field_row("颜色", self.swatches))

        self.view_group = QButtonGroup(self)
        vw = QFrame()
        vw.setObjectName("SegTrack")
        vl = QHBoxLayout(vw)
        vl.setContentsMargins(3, 3, 3, 3)
        vl.setSpacing(3)
        self._view_btns: dict[str, QPushButton] = {}
        for kind, icon, lab in (("list", "list", "列表视图"),
                                ("kanban", "board", "看板视图"),
                                ("timeline", "timeline", "时间线视图（暂未支持）")):
            b = QPushButton()
            b.setObjectName("SegTab")
            b.setCheckable(True)
            b.setFixedSize(46, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(lab)
            TickIcon(icon, 15, "text", b).move(16, 8)
            if kind == "timeline":
                # 时间线整个应用里都没有，就让它一直是灰的；以前连看板一起灰着，
                # 结果这排按钮看着像三个都不能点
                b.setEnabled(False)
            else:
                b.clicked.connect(lambda _c=False, k=kind: self._pick_view(k))
            self.view_group.addButton(b)
            self._view_btns[kind] = b
            vl.addWidget(b)
        self._init_view = (list_row or {}).get("view_kind") or "list"
        self._view_btns[self._init_view].setChecked(True)
        vl.addStretch(1)
        fl.addLayout(_field_row("视图", vw))

        self.folder_combo = QComboBox()
        self.folder_combo.addItem("无", 0)
        for f in services.list_all():
            if f.get("kind") == "folder":
                self.folder_combo.addItem(f["name"], f["id"])
        if folder_id:
            idx = self.folder_combo.findData(folder_id)
            if idx >= 0:
                self.folder_combo.setCurrentIndex(idx)
        if list_row:
            idx = self.folder_combo.findData(list_row.get("folder_id") or 0)
            if idx >= 0:
                self.folder_combo.setCurrentIndex(idx)
        fl.addLayout(_field_row("文件夹", self.folder_combo))

        self.type_combo = QComboBox()
        self.type_combo.addItem("任务清单", "list")
        self.type_combo.addItem("文件夹", "folder")
        if list_row and list_row.get("kind") == "folder":
            self.type_combo.setCurrentIndex(1)
        fl.addLayout(_field_row("类型", self.type_combo))

        self.hide_check = QCheckBox("不在智能清单中显示")
        self.hide_check.setObjectName("CheckRow")
        self.hide_check.setChecked(bool((list_row or {}).get("hide_in_smart")))
        fl.addWidget(self.hide_check)
        fl.addStretch(1)

        # 右侧预览：跟着「视图」那一档换样子（滴答就是列表 / 看板两张预览）
        self.preview = QFrame()
        self.preview.setObjectName("PreviewCard")
        self.preview.setFixedWidth(250)
        pv = QVBoxLayout(self.preview)
        pv.setContentsMargins(14, 14, 14, 14)
        pv.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        self._pv_icon = TickIcon("list", 15, "muted")
        head.addWidget(self._pv_icon)
        self._pv_name = QLabel("清单名称")
        self._pv_name.setObjectName("Strong")
        head.addWidget(self._pv_name)
        head.addStretch(1)
        pv.addLayout(head)
        self._pv_list = self._pv_rows()
        self._pv_board = self._pv_columns()
        pv.addWidget(self._pv_list)
        pv.addWidget(self._pv_board)
        pv.addStretch(1)
        body.addWidget(self.preview)

        self.name_edit.textChanged.connect(self._sync_preview)
        self.swatches.picked.connect(lambda _v: self._sync_preview())
        self._pv_show(self._init_view)
        self._sync_preview()
        self._footer("添加" if not list_row else "保存", self._save)
        self._set_valid(bool(self.name_edit.text().strip()))

    def _pv_bar(self, w: int, h: int = 8) -> QFrame:
        line = QFrame()
        line.setFixedSize(w, h)
        line.setStyleSheet(f"background: {_c('surface_hi')}; border: none; border-radius: {h // 2}px;")
        return line

    def _pv_rows(self) -> QWidget:
        """列表视图的样子：一列「方框 + 一行字」。"""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        for w in (150, 96, 170, 120, 80):
            r = QHBoxLayout()
            r.setContentsMargins(0, 0, 0, 0)
            r.setSpacing(7)
            cb = QFrame()
            cb.setFixedSize(11, 11)
            cb.setStyleSheet(
                "background: transparent; border: 1.5px solid "
                f"{_c('border_strong')}; border-radius: 3px;")
            r.addWidget(cb)
            r.addWidget(self._pv_bar(w))
            r.addStretch(1)
            v.addLayout(r)
        return box

    def _pv_columns(self) -> QWidget:
        """看板视图的样子：三列小卡片。"""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        for n, w in ((2, 52), (3, 40), (1, 60)):
            col = QVBoxLayout()
            col.setSpacing(6)
            col.addWidget(self._pv_bar(46, 7))
            for _ in range(n):
                card = QFrame()
                card.setStyleSheet(
                    "background: transparent; border: 1px solid "
                    f"{_c('border')}; border-radius: 6px;")
                cv = QVBoxLayout(card)
                cv.setContentsMargins(6, 6, 6, 6)
                cv.setSpacing(5)
                cv.addWidget(self._pv_bar(w, 6))
                cv.addWidget(self._pv_bar(int(w * 0.62), 6))
                cv.addStretch(1)
                col.addWidget(card)
            col.addStretch(1)
            h.addLayout(col)
        box.hide()
        return box

    def _pv_show(self, kind: str) -> None:
        self._pv_list.setVisible(kind != "kanban")
        self._pv_board.setVisible(kind == "kanban")

    def _pick_view(self, kind: str) -> None:
        self._pv_show(kind)

    def _repaint_icon_btn(self) -> None:
        for ch in self.icon_btn.findChildren(TickIcon):
            ch.deleteLater()
        ic = TickIcon(self._icon_kind, 16, "muted", self.icon_btn)
        if self._icon_color:
            ic.set_color_hex(self._icon_color)
        ic.move(7, 7)
        ic.show()

    def _pick_icon(self) -> None:
        from .pages.todo import TickMenu
        items = [(k, k, k) for k in LIST_ICONS]
        menu = TickMenu(items, self, checked=self._icon_kind)
        menu.picked.connect(self._on_icon)
        menu.exec_at(self.icon_btn.mapToGlobal(QPoint(0, self.icon_btn.height())))

    def _on_icon(self, kind: object) -> None:
        self._icon_kind = str(kind)
        self._repaint_icon_btn()
        self._sync_preview()

    def _on_color(self, hex_value: str) -> None:
        self._icon_color = hex_value

    def _sync_preview(self) -> None:
        self._pv_name.setText(self.name_edit.text().strip() or "清单名称")
        self._pv_icon.set_kind(self._icon_kind)
        if self._icon_color:
            self._pv_icon.set_color_hex(self._icon_color)
        else:
            self._pv_icon.set_color_key("muted")

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            return
        view = next((k for k, b in self._view_btns.items() if b.isChecked()), "list")
        fields = dict(name=name, icon=self._icon_kind, color=self._icon_color,
                      view_kind=view,
                      folder_id=self.folder_combo.currentData() or 0,
                      kind=self.type_combo.currentData(),
                      hide_in_smart=int(self.hide_check.isChecked()))
        old = self._row["name"] if self._row else ""
        if self._row:
            services.list_update(self._row["id"], **fields)
            if old and old != name:
                services.list_rename(self._row["id"], name, old)
        else:
            services.list_add(name, icon=fields["icon"], color=fields["color"],
                              folder_id=fields["folder_id"], view_kind=view,
                              kind=fields["kind"],
                              hide_in_smart=fields["hide_in_smart"])
        self.accept()


class TagDialog(_BaseDialog):
    """添加 / 编辑标签：名称 + 颜色 + 父标签。"""

    def __init__(self, parent=None, tag_row: dict | None = None):
        self._row = tag_row
        super().__init__("编辑标签" if tag_row else "添加标签", parent, width=400)
        self.name_edit = QLineEdit((tag_row or {}).get("name", ""))
        self.name_edit.setPlaceholderText("标签名称")
        self.name_edit.textChanged.connect(lambda t: self._set_valid(bool(t.strip())))
        self._root.addWidget(self.name_edit)

        self.swatches = SwatchRow((tag_row or {}).get("hex", "")
                                  or (tag_row or {}).get("color", ""))
        self._root.addLayout(_field_row("颜色", self.swatches))

        self.parent_combo = QComboBox()
        self.parent_combo.addItem("无", 0)
        for t in services.tag_all():
            if not tag_row or t["id"] != tag_row["id"]:
                self.parent_combo.addItem(t["name"], t["id"])
        if tag_row:
            idx = self.parent_combo.findData(tag_row.get("parent_id") or 0)
            if idx >= 0:
                self.parent_combo.setCurrentIndex(idx)
        self._root.addLayout(_field_row("父标签", self.parent_combo))
        self._footer()
        self._set_valid(bool(self.name_edit.text().strip()))

    def accept(self) -> None:  # noqa: D102
        name = self.name_edit.text().strip()
        if not name:
            return
        hex_value = self.swatches.value
        key = next((k for k, v in
                    (("red", "#f0435f"), ("amber", "#ed9a12"), ("green", "#0db987"),
                     ("blue", "#3d8bff")) if v == hex_value), "blue")
        parent_id = self.parent_combo.currentData() or 0
        if self._row:
            services.tag_update(self._row["id"], name=name, color=key,
                                hex=hex_value or self._row.get("hex", ""),
                                parent_id=parent_id)
        else:
            services.tag_add(name, color=key, hex_value=hex_value,
                             parent_id=parent_id)
        super().accept()


class FilterDialog(_BaseDialog):
    """添加 / 编辑过滤器：普通 tab 是滴答那六行条件，高级 tab 补三个可求值的字段。"""

    def __init__(self, parent=None, filter_row: dict | None = None):
        self._row = filter_row
        self._cond = dict((filter_row or {}).get("cond") or {})
        super().__init__("编辑过滤器" if filter_row else "添加过滤器",
                         parent, width=460)

        tabs = QWidget()
        tabs.setObjectName("SegTrack")
        tl = QHBoxLayout(tabs)
        tl.setContentsMargins(3, 3, 3, 3)
        tl.setSpacing(3)
        self._tab_btns: dict[str, QPushButton] = {}
        for kind, lab in (("normal", "普通"), ("advanced", "高级")):
            b = QPushButton(lab)
            b.setObjectName("SegTab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=kind: self._switch(k))
            tl.addWidget(b)
            self._tab_btns[kind] = b
        tl.addStretch(1)
        self._root.addWidget(tabs, 0)
        self._tab_btns["normal"].setChecked(True)

        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self._root.addLayout(grid)
        self._grid = grid
        self._pages: dict[str, QWidget] = {}
        self._build_normal()
        self._build_advanced()
        self._switch("normal")

        preview = QPushButton("预览")
        preview.setObjectName("LinkBtn")
        preview.setCursor(Qt.PointingHandCursor)
        preview.clicked.connect(lambda: (self._cond.update(self._cond_of()),
                                         self.parent().reload()))
        self._footer(extra=preview)
        self._set_valid(bool(self.name_edit.text().strip()))

    # ---- 普通 tab ----
    def _build_normal(self) -> None:
        w = QWidget()
        lay = QGridLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(10)
        names = [l["name"] for l in services.list_all()
                 if l.get("kind", "list") == "list"]
        tag_names = [t["name"] for t in services.tag_all()]
        self.name_edit = QLineEdit((self._row or {}).get("name", ""))
        self.name_edit.setPlaceholderText("名称")
        self.name_edit.textChanged.connect(
            lambda t: self._set_valid(bool(t.strip())))
        self.list_pick = MultiPickButton([(n, n) for n in names])
        self.list_pick.set_values(self._cond.get("lists") or [])
        self.tag_pick = MultiPickButton([(n, n) for n in tag_names])
        self.tag_pick.set_values(self._cond.get("tags") or [])
        self.date_combo = QComboBox()
        for v, lab in DATE_FILTERS:
            self.date_combo.addItem(lab, v)
        idx = self.date_combo.findData(self._cond.get("date") or "all")
        self.date_combo.setCurrentIndex(max(idx, 0))
        self.prio_box = _PrioChecks(self._cond.get("priority") or [])
        self.keyword_edit = QLineEdit(self._cond.get("keyword") or "")
        self.keyword_edit.setPlaceholderText("输入任务关键词")
        self.type_box = _TypeChecks(self._cond.get("type") or "")
        rows = [("名称", self.name_edit), ("清单", self.list_pick),
                ("标签", self.tag_pick), ("日期", self.date_combo),
                ("优先级", self.prio_box), ("内容包含", self.keyword_edit),
                ("任务类型", self.type_box)]
        for i, (label, wgt) in enumerate(rows):
            lbl = QLabel(label)
            lbl.setObjectName("DlgField")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lay.addWidget(lbl, i, 0)
            lay.addWidget(wgt, i, 1)
        self._pages["normal"] = w

    # ---- 高级 tab ----
    def _build_advanced(self) -> None:
        w = QWidget()
        lay = QGridLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(10)
        self.repeat_combo = QComboBox()
        self.repeat_combo.addItem("所有", "")
        for v, lab in [("set", "有重复"), ("none", "无重复")]:
            self.repeat_combo.addItem(lab, v)
        self.remind_combo = QComboBox()
        self.remind_combo.addItem("所有", "")
        for v, lab in [("set", "有提醒"), ("none", "无提醒")]:
            self.remind_combo.addItem(lab, v)
        self.sub_combo = QComboBox()
        self.sub_combo.addItem("所有", "")
        for v, lab in [("any", "有子任务"), ("none", "无子任务"),
                       ("undone", "子任务未完成")]:
            self.sub_combo.addItem(lab, v)
        for i, (label, wgt) in enumerate([("重复", self.repeat_combo),
                                          ("提醒", self.remind_combo),
                                          ("子任务", self.sub_combo)]):
            lbl = QLabel(label)
            lbl.setObjectName("DlgField")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lay.addWidget(lbl, i, 0)
            lay.addWidget(wgt, i, 1)
        note = QLabel("高级条件与「普通」同时生效（取交集）")
        note.setObjectName("EmptyHint")
        lay.addWidget(note, 3, 0, 1, 2)
        self._pages["advanced"] = w

    def _switch(self, kind: str) -> None:
        for k, b in self._tab_btns.items():
            b.setChecked(k == kind)
        while self._grid.count():
            self._grid.takeAt(0)
        self._grid.addWidget(self._pages[kind], 0, 0)
        self._kind = kind

    def _cond_of(self) -> dict:
        c = {
            "lists": self.list_pick.values,
            "tags": self.tag_pick.values,
            "date": self.date_combo.currentData() or "all",
            "priority": self.prio_box.values,
            "keyword": self.keyword_edit.text().strip(),
            "type": self.type_box.values,
            "repeat": self.repeat_combo.currentData() or "",
            "reminder": self.remind_combo.currentData() or "",
            "subtask": self.sub_combo.currentData() or "",
        }
        return {k: v for k, v in c.items() if v not in ("", "all", [], None)}

    def accept(self) -> None:  # noqa: D102
        name = self.name_edit.text().strip()
        if not name:
            return
        cond = self._cond_of()
        if self._row:
            services.filter_update(self._row["id"], name=name, cond=cond,
                                   kind=self._kind)
        else:
            services.filter_add(name, cond, kind=self._kind)
        super().accept()


class _PrioChecks(QWidget):
    """优先级：所有 / 高 / 中 / 低 / 无（「所有」是单选，其余可多选）。"""

    def __init__(self, selected: list[str], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._all = QRadioButton("所有")
        self._all.setChecked(not selected)
        lay.addWidget(self._all)
        self._boxes: dict[str, QCheckBox] = {}
        for value, label in (("3", "高"), ("2", "中"), ("1", "低"), ("0", "无")):
            cb = QCheckBox(label)
            cb.setChecked(value in selected)
            cb.toggled.connect(lambda _=False: self._all.setChecked(False))
            lay.addWidget(cb)
            self._boxes[value] = cb
        lay.addStretch(1)

    @property
    def values(self) -> list[str]:
        if self._all.isChecked():
            return []
        return [v for v, cb in self._boxes.items() if cb.isChecked()]


class _TypeChecks(QWidget):
    def __init__(self, selected: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._group = QButtonGroup(self)
        self._rb_all = QRadioButton("所有")
        self._rb_task = QRadioButton("任务")
        self._rb_note = QRadioButton("笔记")
        for i, (key, b) in enumerate((("", self._rb_all),
                                      ("task", self._rb_task),
                                      ("note", self._rb_note))):
            b.setProperty("val", key)
            self._group.addButton(b, i)
            lay.addWidget(b)
            if key == (selected or ""):
                b.setChecked(True)
        if not selected:
            self._rb_all.setChecked(True)
        lay.addStretch(1)

    @property
    def values(self) -> str:
        btn = self._group.checkedButton()
        return btn.property("val") if btn else ""
