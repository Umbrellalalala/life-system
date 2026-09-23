"""笔记记录：Obsidian 库的只读前端。

分工是刻意的 —— Obsidian 负责「写和存」，这里负责「看、搜、跳」：
- 左侧读 vault 的目录树（每层带笔记数），支持全文搜索；
- 中间只读渲染，双击或点按钮跳进 Obsidian 编辑；
- 右侧大纲 + 反向链接，点大纲能带着标题定位过去；
- 「同步到 Obsidian」把科研进展 / 专注记录 / 待办快照写成 md 落进
  ``<vault>/LifeSystem/``，Obsidian 那边用 dataview 聚合。

这里不提供编辑器：两边都能写，早晚会分裂成两套互相过时的笔记。
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import unquote

from PySide6.QtCore import (
    Qt, QEvent, QObject, QItemSelectionModel, QUrl, QTimer, Signal)
from PySide6.QtGui import (
    QColor, QFont, QImage, QKeySequence, QShortcut, QTextBlockFormat,
    QTextCursor, QTextFormat)
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkReply, QNetworkRequest)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QTextBrowser, QSplitter, QHeaderView,
    QFrame, QScrollArea, QAbstractItemView, QSizePolicy, QApplication,
    QButtonGroup, QSlider,
)

from .. import db, popups, theme, vault
from .base import Page

_ROLE_REL = Qt.UserRole
_ROLE_FOLDER = Qt.UserRole + 1
_ROLE_LINE = Qt.UserRole + 2
_ROLE_HEADING = Qt.UserRole + 3
_ROLE_LEVEL = Qt.UserRole + 4
_ROLE_NAME = Qt.UserRole + 5      # 不带图标的显示名，读拖拽后的顺序用

# Obsidian 的资源管理器没有「手动拖拽」这一档，所以手动顺序只能存在本应用自己的
# 设置里 —— 不往库里写，也不改文件名，免得悄悄动了用户没打算动的东西。
SORTS = [
    ("manual", "手动（拖拽排序）"),
    ("name-asc", "文件名 A-Z"),
    ("name-desc", "文件名 Z-A"),
    ("mtime-desc", "修改时间 新→旧"),
    ("mtime-asc", "修改时间 旧→新"),
    ("ctime-desc", "创建时间 新→旧"),
    ("ctime-asc", "创建时间 旧→新"),
]
DEFAULT_SORT = "name-asc"

# 顶层文件夹的配色，模仿 Obsidian folder-icon 那种彩色列表。
# 只用主题里已有的色相，夜间模式才会跟着一起翻。
_FOLDER_TINTS = ["red", "amber", "green", "blue", "accent", "nlp_fg"]

_RECENT_COL = 64           # 「最近」视图第二列要放得下「3 个月前」
_COUNT_COL = 34
_MAX_BACKLINKS = 12        # 反向链接面板条数上限，超出会在面板里说明
_HEADING_TOP_PAD = 6       # 跳标题时留一点上边距，别贴着卡片边缘
_IMAGE_OBJECT = int(QTextFormat.ImageObject)


def _ago(ts: float) -> str:
    """给「最近」列表用的相对时间。精确到日期的话一眼扫不出远近。"""
    d = time.time() - ts
    if d < 120:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 31:
        return f"{int(d // 86400)} 天前"
    if d < 86400 * 365:
        return f"{int(d // 86400 // 30)} 个月前"
    return f"{int(d // 86400 // 365)} 年前"


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class SidePanel(QFrame):
    """右侧一块带标题的面板（大纲 / 反向链接共用外壳）。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)
        head = QLabel(title)
        head.setObjectName("CardTitle")
        lay.addWidget(head)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(2)
        lay.addLayout(self.rows)
        lay.addStretch(1)

    def clear_rows(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def set_hint(self, text: str) -> None:
        self.clear_rows()
        lbl = QLabel(text)
        lbl.setObjectName("Muted")
        self.rows.addWidget(lbl)

    def add_row(self, text: str, on_click=None, indent: int = 0,
                font_px: int = 13, tip: str = "") -> None:
        """on_click 收到按下时的修饰键，用来区分「本应用内跳转」和「跳 Obsidian」。"""
        btn = QPushButton(text)
        btn.setObjectName("OutlineBtn")
        btn.setCursor(Qt.PointingHandCursor if on_click else Qt.ArrowCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                          QSizePolicy.Policy.Fixed)
        btn.setStyleSheet(
            f"padding: 3px 8px 3px {8 + indent}px; font-size: {font_px}px;")
        btn.setMinimumWidth(0)
        if tip:
            btn.setToolTip(tip)
        if on_click:
            # 修饰键必须在点击那一刻取：写成默认参数会在建行时就求值，永远是无修饰
            btn.clicked.connect(
                lambda checked=False: on_click(QApplication.keyboardModifiers()))
        self.rows.addWidget(btn)

    def add_note(self, text: str) -> None:
        """跟在 add_row 下面的一行小灰字，用来放命中上下文。"""
        lbl = QLabel(text)
        lbl.setObjectName("Muted")
        lbl.setStyleSheet("padding: 0 8px 4px 10px; font-size: 11px;")
        lbl.setWordWrap(True)
        self.rows.addWidget(lbl)


class ImageLoader(QObject):
    """把图片按视口宽度缩放后塞进 QTextDocument 的资源表。

    两个非做不可的理由：
    - QTextBrowser 不会自己拉网络图，`<img src="https://…">` 放上去就是破图占位
      （用户的图全在 gitee 图床上，是 image-auto-upload 插件重写过的链接）；
    - 本地图它会按原始尺寸排版，一张超宽的图就把预览栏撑出横向滚动条。
    本地图同步读、网络图异步下，都走同一个 _store 做夹宽。
    """

    imageReady = Signal(str)
    MAX_CACHED = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._images: dict[str, QImage] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()

    def get(self, src: str) -> QImage | None:
        return self._images.get(src)

    def load(self, src: str, hint: int, limit: int) -> QImage | None:
        """能立刻给的就给（缓存/本地图），需要下载的先发起、返回 None。"""
        if src in self._images:
            return self._images[src]
        if src.startswith(("http://", "https://")):
            self._fetch(src, hint, limit)
            return None
        if src in self._failed:
            return None
        img = QImage()
        path = QUrl(src).toLocalFile() if src.startswith("file:") else src
        if img.load(path) or img.load(unquote(path)):
            return self._store(src, img, hint, limit)
        self._failed.add(src)
        return None

    def _store(self, src: str, img: QImage, hint: int, limit: int) -> QImage:
        cap = min(hint, limit) if hint else limit
        if 0 < cap < img.width():
            img = img.scaledToWidth(cap, Qt.TransformationMode.SmoothTransformation)
        if len(self._images) >= self.MAX_CACHED:
            self._images.clear()               # 翻很久之后别无限攒
        self._images[src] = img
        return img

    def _fetch(self, src: str, hint: int, limit: int) -> None:
        if src in self._pending or src in self._failed:
            return
        self._pending.add(src)
        req = QNetworkRequest(QUrl(src))
        req.setTransferTimeout(10000)
        reply = self._nam.get(req)
        reply.setProperty("src", src)
        reply.setProperty("hint", hint)
        reply.setProperty("limit", limit)
        reply.finished.connect(self._on_finished)

    def _on_finished(self) -> None:
        reply = self.sender()
        src = reply.property("src")
        self._pending.discard(src)
        img = QImage()
        if (reply.error() == QNetworkReply.NetworkError.NoError
                and img.loadFromData(reply.readAll())):
            self._store(src, img, int(reply.property("hint") or 0),
                        int(reply.property("limit") or 0))
            self.imageReady.emit(src)
        else:
            self._failed.add(src)              # 离线/挂了：别每次渲染都重试
        reply.deleteLater()


class OutlineTree(QFrame):
    """层级大纲：每个节点能单独折叠，顶部滑杆批量展开到某一级。

    照 Obsidian quiet-outline 的交互：滑杆停在 Hn 就把 n 级以内的节点全展开，
    标签实时显示「H2: 133」这种「当前层级: 可见条数」。
    """

    jumpRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 8)
        lay.setSpacing(6)

        title = QLabel("大纲")
        title.setObjectName("CardTitle")
        lay.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, 4)
        self.slider.setValue(2)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.valueChanged.connect(self._apply_level)
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("Muted")
        row.addWidget(self.slider, 1)
        row.addWidget(self.count_lbl)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(13)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.itemClicked.connect(self._on_click)
        lay.addWidget(self.tree, 1)

    def set_hint(self, text: str) -> None:
        self.tree.clear()
        it = QTreeWidgetItem([text])
        it.setDisabled(True)
        it.setForeground(0, QColor(theme.get("muted")))
        self.tree.addTopLevelItem(it)
        self.count_lbl.setText("")
        self.slider.setEnabled(False)

    def show_headings(self, headings: list[tuple[int, str, int]]) -> None:
        self.slider.setEnabled(bool(headings))
        self.tree.blockSignals(True)
        self.tree.clear()
        stack: list[tuple[int, QTreeWidgetItem]] = []
        for level, text, _line in headings:
            it = QTreeWidgetItem([text])
            it.setData(0, _ROLE_HEADING, vault.plain_text(text))
            it.setData(0, _ROLE_LEVEL, level)
            it.setToolTip(0, vault.plain_text(text))
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(it)
            else:
                self.tree.addTopLevelItem(it)
            stack.append((level, it))
        self.tree.blockSignals(False)
        self._apply_level(self.slider.value())

    def _items(self):
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop(0)
            yield it
            stack[:0] = [it.child(i) for i in range(it.childCount())]

    def _apply_level(self, n: int) -> None:
        visible = 0
        self.tree.blockSignals(True)
        for it in self._items():
            level = it.data(0, _ROLE_LEVEL)
            it.setExpanded(level < n)
            if level <= n:
                visible += 1
        self.tree.blockSignals(False)
        self.count_lbl.setText("H%d: %d" % (n, visible))

    def _on_click(self, item: QTreeWidgetItem, _col: int) -> None:
        heading = item.data(0, _ROLE_HEADING)
        if heading:
            self.jumpRequested.emit(heading)

    def marks(self, doc) -> list[tuple[float, QTreeWidgetItem]]:
        """按出现顺序把树节点和文档里的标题块配对，供滚动时高亮当前小节。

        必须和 vault.headings() 用同一套层级过滤（1~4 级）：库里只要有 h5/h6，
        不过滤就会整体错位，高亮跳到隔壁小节去。
        """
        blocks = []
        blk = doc.begin()
        while blk.isValid():
            level = blk.blockFormat().headingLevel()
            if 0 < level <= 4 and blk.layout():
                blocks.append(blk.layout().position().y())
            blk = blk.next()
        items = list(self._items())
        return list(zip(blocks[:len(items)], items))

    def set_current(self, item: QTreeWidgetItem | None) -> None:
        if item is None or self.tree.hasFocus():
            return                          # 用户正在翻大纲，别抢他的选中项
        if self.tree.currentItem() is not item:
            self.tree.setCurrentItem(item,
                                     QItemSelectionModel.SelectionFlag.NoUpdate)
            self.tree.scrollToItem(item)


class FolderTree(QTreeWidget):
    """目录树：拖拽只允许「同一个文件夹里的兄弟」互相换位。

    拖进别的文件夹在磁盘上等于移动文件，那是 Obsidian 的活。Qt 的 InternalMove
    在拖拽过程中没法可靠地拒绝这种落点，所以先让它落下、事后核对父级：
    父级变了就发 dropRejected，由页面整棵重建（等于撤销）。
    """

    orderChanged = Signal(str, list)
    dropRejected = Signal()

    def folder_of(self, item: QTreeWidgetItem) -> str:
        parent = item.parent()
        if parent is None:
            return ""
        folder = parent.data(0, _ROLE_FOLDER)
        return folder if folder is not None else ""

    def item_for_folder(self, folder: str) -> QTreeWidgetItem | None:
        if not folder:
            return None
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it.data(0, _ROLE_FOLDER) == folder:
                return it
            stack.extend(it.child(i) for i in range(it.childCount()))
        return None

    def names_under(self, folder: str) -> list[str]:
        node = self.item_for_folder(folder)
        parent = node if node is not None else self.invisibleRootItem()
        return [parent.child(i).data(0, _ROLE_NAME) or ""
                for i in range(parent.childCount())]

    def dropEvent(self, event) -> None:  # noqa: N802
        moved = self.currentItem()
        if moved is None or not self.dragEnabled():
            event.ignore()
            return
        before = self.folder_of(moved)
        super().dropEvent(event)
        if self.folder_of(moved) != before:
            self.dropRejected.emit()
            return
        self.orderChanged.emit(before, self.names_under(before))


class NotePage(Page):
    def __init__(self):
        super().__init__("笔记记录", "Obsidian 负责写，这里负责看、搜、跳")
        self._notes: list[dict] = []
        self._current_rel = ""
        self._tint_cache: dict[str, str] = {}
        self._view = "tree"              # tree | recent
        self._outline_pinned: bool | None = None   # None = 跟随窗口宽度自动
        # 用户手动展开过的文件夹。None 表示还没动过，此时按「顶层全展开」给默认值；
        # 一旦动过就完全照它恢复 —— 否则每次 ↻/同步/换主题都把嵌套层塌回去。
        self._expanded: set[str] | None = None
        self._heading_marks: list[tuple[float, QTreeWidgetItem]] = []
        self._sort = DEFAULT_SORT
        self._manual: dict[str, list[str]] = {}
        self._loader = ImageLoader(self)
        self._loader.imageReady.connect(self._on_image_ready)
        self._load_prefs()

        self._build_toolbar()

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self._build_left())
        self.splitter.addWidget(self._build_center())
        self.splitter.addWidget(self._build_right())
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setStretchFactor(2, 2)
        self.splitter.setSizes([260, 620, 220])
        self.splitter.setCollapsible(1, False)
        self.body().addWidget(self.splitter, 1)

        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(
            lambda: (self.search_input.setFocus(), self.search_input.selectAll()))
        # 大纲：点一下在本应用内滚过去，Ctrl+点交给 Obsidian 定位
        self.outline.jumpRequested.connect(self._on_outline_jump)
        self.preview.verticalScrollBar().valueChanged.connect(
            self._sync_outline_current)
        if theme.manager is not None:
            theme.manager.changed.connect(self._on_theme_changed)

        self._apply_tree_style()
        self._apply_preview_style()
        self._apply_sort_ui()
        self.reload()
        # 后台先热一遍全库文本缓存，否则「一打开应用就搜索」还要现场读 1 秒
        vault.warm_cache_async()

    # ---------------------------------------------------------------- 工具栏
    # 窄窗口下这三个长文案按钮 + 定宽搜索框一共要 925px，比窗口还能缩的下限宽得多，
    # 于是整页被撑住、右边的分割线永远看不见。窄到 NARROW_AT 以下就换成短标签，
    # 意思仍然在 tooltip 里。
    _SHORT_LABELS = {
        "vault_btn": "📚 库",
        "os_btn": "🔍 Obsidian",
        "sync_btn": "⇪ 同步",
    }
    NARROW_AT = 900        # 低于此宽度：工具栏换短标签
    HIDE_OUTLINE_AT = 780  # 低于此宽度：右栏整块收起
    _narrow_loaded: bool | None = None
    _long_labels: dict = {}

    def _build_toolbar(self) -> None:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.vault_btn = QPushButton("选择 Obsidian 库")
        self.vault_btn.setObjectName("Ghost")
        self.vault_btn.setCursor(Qt.PointingHandCursor)
        self.vault_btn.setToolTip("换一个 Obsidian 库目录")
        self.vault_btn.clicked.connect(self._switch_vault)
        bar.addWidget(self.vault_btn)

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("Ghost")
        self.refresh_btn.setFixedWidth(34)
        self.refresh_btn.setToolTip("重新扫描库目录")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.reload)
        bar.addWidget(self.refresh_btn)

        # 窄窗口时右栏会被自动收起（见 resizeEvent），留个开关让人拽回来。
        # 只在「自动规则会藏掉它」的时候出现，宽窗口不占工具栏位置。
        self.outline_btn = QPushButton("☰")
        self.outline_btn.setObjectName("Ghost")
        self.outline_btn.setCheckable(True)
        self.outline_btn.setFixedWidth(34)
        self.outline_btn.setToolTip("显示 / 隐藏右侧大纲与反向链接")
        self.outline_btn.setCursor(Qt.PointingHandCursor)
        self.outline_btn.setVisible(False)
        self.outline_btn.clicked.connect(self._toggle_outline)
        bar.addWidget(self.outline_btn)

        bar.addStretch(1)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 全文搜索（Ctrl+O）")
        self.search_input.setMinimumWidth(120)
        self.search_input.setMaximumWidth(240)
        self.search_input.setClearButtonEnabled(True)
        # 全库扫一次约 0.12s：逐字符触发的话打字会一顿一顿，所以防抖，
        # 停 220ms 再搜；回车立刻搜。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_input.textChanged.connect(self._on_search)
        self.search_input.returnPressed.connect(self._enter_in_search)
        # Esc 得用事件过滤器：QLineEdit 自己的 keyPressEvent 会先吃掉它，
        # 挂 QShortcut（哪怕是 WidgetShortcut）抢不过。
        self.search_input.installEventFilter(self)
        bar.addWidget(self.search_input, 1)

        self.os_btn = QPushButton("在 Obsidian 中搜索")
        self.os_btn.setObjectName("Ghost")
        self.os_btn.setCursor(Qt.PointingHandCursor)
        self.os_btn.setToolTip("把当前关键词丢给 Obsidian 的搜索")
        self.os_btn.clicked.connect(self._search_in_obsidian)
        bar.addWidget(self.os_btn)

        self.sync_btn = QPushButton("⇪ 同步到 Obsidian")
        self.sync_btn.setObjectName("Primary")
        self.sync_btn.setCursor(Qt.PointingHandCursor)
        self.sync_btn.setToolTip(
            "把科研进展 / 专注记录 / 待办快照写成 md，放进库的 LifeSystem 目录")
        self.sync_btn.clicked.connect(self._sync)
        bar.addWidget(self.sync_btn)

        self._long_labels = {
            name: getattr(self, name).text() for name in self._SHORT_LABELS}
        self._narrow_loaded = None
        self.body().addLayout(bar)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        wide = self.width() >= self.NARROW_AT
        if wide != self._narrow_loaded:
            self._narrow_loaded = wide
            for name, short in self._SHORT_LABELS.items():
                btn = getattr(self, name)
                btn.setText(self._long_labels[name] if wide else short)
        self._apply_outline_visibility()

    def _apply_outline_visibility(self) -> None:
        """右栏（大纲 / 反向链接）：窄窗口默认收起，但允许用户手动拽回来。

        它 170px 的下限会把整页钉在缩不动的位置上，所以窄的时候默认让位；
        可「默认收起」不等于「不许看」——那时工具栏露出 ☰ 开关。
        窗口重新宽过阈值就把控制权交回自动规则，否则手动状态会一直赖着。
        """
        sp = getattr(self, "splitter", None)
        if sp is None:
            return
        panel = sp.widget(2)
        if panel is None:               # 构造中途也会走这里，右栏还没挂上来
            return
        auto = self.width() >= self.HIDE_OUTLINE_AT
        if auto:
            self._outline_pinned = None
        show = auto if self._outline_pinned is None else self._outline_pinned
        panel.setVisible(show)
        self.outline_btn.setVisible(not auto)
        self.outline_btn.setChecked(show)

    def _toggle_outline(self, checked: bool) -> None:
        self._outline_pinned = checked
        self._apply_outline_visibility()

    # ---------------------------------------------------------------- 左：树
    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(6)

        # 目录 / 最近：进页面只会看到目录树，但「我刚才在写哪篇」是更高频的问题
        track = QFrame()
        track.setObjectName("SegTrack")
        tl = QHBoxLayout(track)
        tl.setContentsMargins(3, 3, 3, 3)
        tl.setSpacing(3)
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        self._view_btns: dict[str, QPushButton] = {}
        for key, label in (("tree", "目录"), ("recent", "最近")):
            b = QPushButton(label)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(key == self._view)
            b.clicked.connect(lambda checked=False, k=key: self._set_view(k))
            self._view_group.addButton(b)
            self._view_btns[key] = b
            tl.addWidget(b)
        tl.addStretch(1)

        self.sort_btn = QPushButton()
        self.sort_btn.setObjectName("Ghost")
        self.sort_btn.setCursor(Qt.PointingHandCursor)
        self.sort_btn.setToolTip("目录树的排序方式（手动顺序只影响本应用，Obsidian 没有这一档）")
        self.sort_btn.clicked.connect(self._pick_sort)
        tl.addWidget(self.sort_btn)

        # 批量折叠：一层层点折叠太累，尤其库里有十几层目录的时候
        self.expand_btn = QPushButton("⊞")
        self.collapse_btn = QPushButton("⊟")
        for btn, tip, slot in ((self.expand_btn, "展开全部文件夹", self._expand_all),
                               (self.collapse_btn, "折叠全部文件夹", self._collapse_all)):
            btn.setObjectName("Ghost")
            btn.setFixedWidth(28)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda checked=False, s=slot: s())
            tl.addWidget(btn)
        lay.addWidget(track)

        self.tree = FolderTree()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        # 名称列必须自己撑开：默认它会停在「建控件那一刻」的 sizeHint 上（那时还没
        # 塞条目），结果就是一百多像素就截断，右边大片空白留给计数列用不上。
        head = self.tree.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        head.setSectionResizeMode(1, QHeaderView.Fixed)
        self.tree.setColumnWidth(1, 34)
        self.tree.setIndentation(16)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.itemClicked.connect(self._on_click)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.itemExpanded.connect(lambda it: self._remember_expand(it, True))
        self.tree.itemCollapsed.connect(lambda it: self._remember_expand(it, False))
        self.tree.orderChanged.connect(self._on_order_changed)
        self.tree.dropRejected.connect(self._on_drop_rejected)
        lay.addWidget(self.tree, 1)

        self.hint_lbl = QLabel("")
        self.hint_lbl.setObjectName("Muted")
        self.hint_lbl.setWordWrap(True)
        self.hint_lbl.setStyleSheet("padding: 0 4px;")
        lay.addWidget(self.hint_lbl)
        return w

    def _apply_tree_style(self) -> None:
        self.tree.setStyleSheet(f"""
            QTreeWidget {{ background: {theme.get('surface')};
                border: 1px solid {theme.get('border')}; border-radius: 10px;
                outline: none; }}
            QTreeWidget::item {{ padding: 3px 0; border-radius: 6px; }}
            QTreeWidget::item:hover {{ background: {theme.get('surface_hi')}; }}
            QTreeWidget::item:selected {{ background: {theme.get('accent_soft')};
                color: {theme.get('text')}; }}
        """)

    # ---------------------------------------------------------------- 中：预览
    def _build_center(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(8)

        head = QFrame()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(2)
        self.doc_title = QLabel("未选择笔记")
        self.doc_title.setObjectName("Strong")
        font = QFont()
        font.setPointSizeF(13)
        font.setBold(True)
        self.doc_title.setFont(font)
        hl.addWidget(self.doc_title)
        self.doc_path = QLabel("从左边挑一篇，或按 Ctrl+O 搜索")
        self.doc_path.setObjectName("Muted")
        hl.addWidget(self.doc_path)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.reveal_btn = QPushButton("在文件夹中显示")
        self.reveal_btn.setObjectName("Ghost")
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.clicked.connect(self._reveal)
        actions.addWidget(self.reveal_btn)
        self.edit_btn = QPushButton("✎ 去 Obsidian 编辑")
        self.edit_btn.setObjectName("Primary")
        self.edit_btn.setCursor(Qt.PointingHandCursor)
        self.edit_btn.clicked.connect(lambda: self._open_in_obsidian())
        actions.addWidget(self.edit_btn)
        hl.addLayout(actions)
        lay.addWidget(head)

        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.anchorClicked.connect(self._on_anchor)
        lay.addWidget(self.preview, 1)

        self.doc_stat = QLabel("")
        self.doc_stat.setObjectName("Muted")
        self.doc_stat.setStyleSheet("padding: 0 6px;")
        lay.addWidget(self.doc_stat)
        return w

    def _apply_preview_style(self) -> None:
        accent, text = theme.get("accent"), theme.get("text")
        muted, border = theme.get("muted"), theme.get("border")
        surface, surface_hi = theme.get("surface"), theme.get("surface_hi")
        hi = theme.get("text_hi")
        self.preview.setStyleSheet(
            f"background: {surface}; border: 1px solid {border};"
            f" border-radius: 10px;")
        # QTextBrowser 默认 9pt、正文贴边，读起来像日志不像笔记。
        # 注意：Qt 的样式表**不认 line-height**（写了也不生效），行距和代码块
        # 换行要在 setHtml 之后用 QTextBlockFormat 设，见 _apply_doc_format()。
        # 内边距交给 documentMargin，别在这里再叠一层 padding，两层会累加。
        self.preview.document().setDefaultStyleSheet(f"""
            body {{ color: {text}; font-size: 10.5pt; }}
            p, li, td, th {{ color: {text}; font-size: 10.5pt; }}
            h1 {{ color: {hi}; font-size: 16pt; }}
            h2 {{ color: {hi}; font-size: 13.5pt; }}
            h3 {{ color: {hi}; font-size: 11.5pt; }}
            h4 {{ color: {text}; font-size: 10.5pt; }}
            a {{ color: {accent}; text-decoration: none; font-weight: 600; }}
            a.tag {{ background: {accent}22; border-radius: 4px; padding: 0 4px; }}
            code {{ font-family: Consolas, 'Courier New', monospace;
                    background: {surface_hi}; border-radius: 3px;
                    padding: 1px 4px; font-size: 10pt; }}
            pre {{ font-family: Consolas, 'Courier New', monospace;
                   background: {surface_hi}; padding: 10px; border-radius: 8px;
                   font-size: 10pt; }}
            table {{ border-collapse: collapse; }}
            td, th {{ border: 1px solid {border}; padding: 5px 10px; }}
            blockquote {{ color: {muted}; border-left: 3px solid {border};
                          margin: 0; padding-left: 12px; }}
            hr {{ border: none; border-top: 1px solid {border}; }}
            span.imgmiss {{ color: {muted}; font-style: italic; }}
        """)
        self.preview.document().setDocumentMargin(18)

    def _apply_doc_format(self) -> None:
        """整篇设行距 + 允许代码块换行。setHtml 之后必须重做一次：它会重建文档块。

        Qt 把 <pre> 导成「不可断行」的块，长代码行会把文档撑宽、底部冒横向滚动条；
        这两件事在同一次 mergeBlockFormat 里做掉，比分开走两遍便宜。
        """
        cur = QTextCursor(self.preview.document())
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(1.45, QTextBlockFormat.LineDistanceHeight.value)
        fmt.setNonBreakableLines(False)
        cur.mergeBlockFormat(fmt)

    # ---------------------------------------------------------------- 右：面板
    def _build_right(self) -> QWidget:
        w = QFrame()
        w.setMinimumWidth(190)
        w.setMaximumWidth(320)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.setSpacing(8)

        # 大纲自己就是一个带折叠节点的树，得占住剩余高度；
        # 外面再套一层 QScrollArea 会让它拿不到视口、滚不动。
        self.outline = OutlineTree()
        lay.addWidget(self.outline, 3)

        self.backlinks = SidePanel("反向链接")
        bl = QScrollArea()
        bl.setWidgetResizable(True)
        bl.setFrameShape(QFrame.Shape.NoFrame)
        bl.setMaximumHeight(240)
        bl.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                         " QScrollArea > QWidget > QWidget{background:transparent;}")
        bl.setWidget(self.backlinks)
        lay.addWidget(bl, 1)
        return w

    # ---------------------------------------------------------------- 数据
    def reload(self) -> None:
        root = vault.vault_path()
        name = os.path.basename(os.path.normpath(root)) if root else ""
        self.vault_btn.setText(f"📚 {name}" if name else "未选择 Obsidian 库")
        # 窄窗口的「长标签」是在 _build_toolbar 里快照的，那时库名还没填进去；
        # 不跟着刷新，窗口一宽按钮就永远退回占位文字。
        self._long_labels["vault_btn"] = self.vault_btn.text()
        self._notes = vault.iter_notes(root) if root else []
        self._tint_cache = {}
        self._build_tree()
        last = vault.last_sync_at()
        self.hint_lbl.setText(
            f"{len(self._notes)} 篇笔记" + (f" · 上次同步 {last}" if last else ""))
        if self._current_rel and any(n["rel"] == self._current_rel for n in self._notes):
            self._select_in_tree(self._current_rel)
            self._render_preview()      # 重新扫描后正文要跟着回来，不然切库回来是一片空白
            self._refresh_side()
        else:
            self._show_empty()

    def _tint_for(self, folder: str) -> str:
        top = folder.split("/", 1)[0] or "库根目录"
        if top not in self._tint_cache:
            key = _FOLDER_TINTS[len(self._tint_cache) % len(_FOLDER_TINTS)]
            self._tint_cache[top] = theme.get(key)
        return self._tint_cache[top]

    def _build_tree(self) -> None:
        self.tree.blockSignals(True)      # 建表期间的 setExpanded 不该被当成用户行为
        self.tree.clear()
        kw = self.search_input.text().strip()
        recent = bool(self._notes) and not kw and self._view == "recent"
        self.tree.setColumnWidth(1, _RECENT_COL if recent else _COUNT_COL)
        if self._notes:
            if kw:
                self._build_search_tree(kw)
            elif recent:
                self._build_recent_tree()
            else:
                self._build_folder_tree()
        self.tree.blockSignals(False)

    def _remember_expand(self, item: QTreeWidgetItem, open_: bool) -> None:
        folder = item.data(0, _ROLE_FOLDER)
        if folder is None:
            return
        if self._expanded is None:
            # 信号只在「状态变了」时才发：初始 expandToDepth(0) 已经把顶层展开了，
            # 用户再点一次顶层文件夹不会触发任何东西。所以第一次变化时要先按
            # 当前实际展开状态打个底，否则恢复出来的集合会缺一大半。
            self._expanded = self._expanded_folders()
        (self._expanded.add if open_ else self._expanded.discard)(folder)

    def _expanded_folders(self) -> set[str]:
        out = set()
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            folder = it.data(0, _ROLE_FOLDER)
            if folder is not None and it.isExpanded():
                out.add(folder)
            stack.extend(it.child(i) for i in range(it.childCount()))
        return out

    # ---------------------------------------------------------- 排序与手动顺序
    def _load_prefs(self) -> None:
        self._sort = db.get_setting("note_sort") or DEFAULT_SORT
        if not any(k == self._sort for k, _l in SORTS):
            self._sort = DEFAULT_SORT
        try:
            blob = json.loads(db.get_setting("note_order") or "{}")
        except ValueError:
            blob = {}
        # 手动顺序按「文件夹相对路径」记，换库之后对不上号，直接丢掉
        self._manual = (blob.get("order") or {}) if blob.get("vault") == vault.vault_path() else {}

    def _save_order(self) -> None:
        db.set_setting("note_order", json.dumps(
            {"vault": vault.vault_path(), "order": self._manual},
            ensure_ascii=False))

    def _apply_sort_ui(self) -> None:
        manual = self._sort == "manual"
        self.sort_btn.setText("↕ " + dict(SORTS)[self._sort].split("（")[0])
        mode = (QAbstractItemView.DragDropMode.InternalMove if manual
                else QAbstractItemView.DragDropMode.NoDragDrop)
        self.tree.setDragDropMode(mode)
        self.tree.setDragEnabled(manual)
        self.tree.setAcceptDrops(manual)
        self.tree.viewport().setAcceptDrops(manual)
        self.tree.setDropIndicatorShown(manual)
        if manual:
            self.tree.setDefaultDropAction(Qt.MoveAction)

    def _pick_sort(self) -> None:
        labels = [lab for _k, lab in SORTS]
        idx = next((i for i, (k, _l) in enumerate(SORTS) if k == self._sort), 1)
        choice, ok = popups.get_item(self, "排序方式", "目录树的排序", labels, idx)
        if not ok:
            return
        self._sort = SORTS[labels.index(choice)][0]
        db.set_setting("note_sort", self._sort)
        self._apply_sort_ui()
        self._build_tree()
        if self._current_rel:
            self._select_in_tree(self._current_rel)

    def _sort_children(self, entries: list[tuple], folder: str) -> list[tuple]:
        """同一父级下的兄弟排序；文件夹永远排在文件前面，免得树跳得难看。

        entries 是 (kind, name, stamp)：kind 是 "dir"/"file"，stamp 是时间戳
        （文件夹取它含子目录里最新的那篇）。
        """
        base = self._sort.split("-")[0]
        reverse = self._sort.endswith("-desc")
        if base == "manual":
            order = self._manual.get(folder, [])
            rank = lambda e: order.index(e[1]) if e[1] in order else len(order)
        elif base == "name":
            rank = lambda e: e[1].lower()
        else:
            rank = lambda e: e[2]
        dirs = sorted([e for e in entries if e[0] == "dir"], key=rank, reverse=reverse)
        files = sorted([e for e in entries if e[0] == "file"], key=rank, reverse=reverse)
        return dirs + files

    def _expand_all(self) -> None:
        self.tree.expandAll()

    def _collapse_all(self) -> None:
        self.tree.collapseAll()

    def _on_order_changed(self, folder: str, names: list[str]) -> None:
        self._manual[folder] = [n for n in names if n]
        self._save_order()

    def _on_drop_rejected(self) -> None:
        self._build_tree()              # 重建等于撤销那次跨文件夹的拖动
        if self._current_rel:
            self._select_in_tree(self._current_rel)
        self.hint_lbl.setText("只能调整同一文件夹内的先后顺序；要移动文件请在 Obsidian 里做")

    def _set_view(self, key: str) -> None:
        self._view = key
        self._build_tree()
        if self._current_rel:
            self._select_in_tree(self._current_rel)

    def _build_recent_tree(self) -> None:
        """按修改时间倒序的平铺列表 —— 回答「我刚才在写哪篇」。

        排除 LifeSystem/ 子树：那是本应用自己写出去的快照，同步一次就全体刷新，
        会把用户真正在写的笔记挤出列表。目录树里照旧能看到它。
        """
        mine = vault.EXPORT_DIR + "/"
        recent = [n for n in self._notes if not n["rel"].startswith(mine)]
        recent = sorted(recent, key=lambda n: n["mtime"], reverse=True)[:80]
        for n in recent:
            it = QTreeWidgetItem([f"📄 {n['name']}", _ago(n["mtime"])])
            it.setData(0, _ROLE_REL, n["rel"])
            it.setToolTip(0, f"{n['rel']}\n{_when(n['mtime'])} 改过")
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            it.setForeground(1, QColor(theme.get("muted")))
            self.tree.addTopLevelItem(it)

    def _build_folder_tree(self) -> None:
        """按当前排序方式递归搭出目录树。

        排序只作用在「同一父级下的兄弟」，并且文件夹恒在文件前面 ——
        跨层混排会让树看起来像散架。
        """
        field = "ctime" if self._sort.startswith("ctime") else "mtime"
        subdirs: dict[str, set[str]] = {}
        files: dict[str, list[dict]] = {}
        stamp: dict[str, float] = {}
        for n in self._notes:
            files.setdefault(n["folder"], []).append(n)
            parent, _, _ = n["folder"].rpartition("/")
            subdirs.setdefault(parent, set()).add(n["folder"])
            cur = n["folder"]
            while True:                     # 时间戳往上冒泡，父文件夹取子树里最新的
                stamp[cur] = max(stamp.get(cur, 0.0), n[field])
                if not cur:
                    break
                cur = cur.rpartition("/")[0]
        for folder in subdirs:
            subdirs.setdefault(folder, set())

        dirs: dict[str, QTreeWidgetItem] = {}
        bold = QFont()
        bold.setBold(True)

        def build(folder: str, parent_item) -> None:
            parent = parent_item if parent_item is not None \
                else self.tree.invisibleRootItem()
            entries = [("dir", f.rpartition("/")[2], stamp.get(f, 0.0), f)
                       for f in subdirs.get(folder, ())]
            entries += [("file", n["name"], n[field], n["rel"])
                        for n in files.get(folder, ())]
            for kind, name, _ts, payload in self._sort_children(entries, folder):
                if kind == "dir":
                    it = QTreeWidgetItem([f"📁 {name}", ""])
                    it.setData(0, _ROLE_FOLDER, payload)
                    it.setForeground(0, QColor(self._tint_for(payload)))
                    it.setFont(0, bold)
                    it.setToolTip(0, payload)
                    parent.addChild(it)
                    dirs[payload] = it
                    build(payload, it)
                else:
                    it = QTreeWidgetItem([f"📄 {name}", ""])
                    it.setData(0, _ROLE_REL, payload)
                    it.setToolTip(0, payload)
                    parent.addChild(it)
                it.setData(0, _ROLE_NAME, name)

        if not subdirs.get("") and not files.get(""):
            it = QTreeWidgetItem(["库里没有 Markdown 笔记", ""])
            it.setDisabled(True)
            self.tree.addTopLevelItem(it)
        build("", None)

        for folder, it in dirs.items():     # 计数含子目录，和 Obsidian note-count 一致
            prefix = folder + "/"
            it.setText(1, str(sum(
                1 for n in self._notes
                if n["folder"] == folder or n["folder"].startswith(prefix))))
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)

        if self._expanded is None:
            self.tree.expandToDepth(0)
        else:
            for folder in self._expanded:
                item = dirs.get(folder)
                if item is not None:
                    item.setExpanded(True)

    def _build_search_tree(self, kw: str) -> None:
        hits = vault.search(kw, notes=self._notes)
        if not hits:
            it = QTreeWidgetItem([f"没有匹配「{kw}」的笔记", ""])
            it.setDisabled(True)
            self.tree.addTopLevelItem(it)
            self.hint_lbl.setText(f"0 篇命中 · {kw}")
            return
        muted = QColor(theme.get("muted"))
        for h in hits:
            n = h["note"]
            where = n["folder"] or "库根目录"
            line = f"第 {h['line']} 行" if h["line"] else "标题匹配"
            it = QTreeWidgetItem([f"📄 {n['name']}", ""])
            it.setData(0, _ROLE_REL, n["rel"])
            it.setData(0, _ROLE_LINE, h["line"])
            it.setToolTip(0, f"{where} · {line}\n{h['snippet']}")
            sub = QTreeWidgetItem([f"　{h['snippet']}", ""])
            sub.setData(0, _ROLE_REL, n["rel"])
            sub.setData(0, _ROLE_LINE, h["line"])
            sub.setForeground(0, muted)
            it.addChild(sub)
            self.tree.addTopLevelItem(it)
        self.hint_lbl.setText(f"命中 {len(hits)} 篇 · {kw}")

    # ---------------------------------------------------------------- 交互
    def _on_search(self, _text: str) -> None:
        self._search_timer.start()

    def _apply_search(self) -> None:
        self._search_timer.stop()
        kw = self.search_input.text().strip()
        self._build_tree()
        if not kw:
            self.hint_lbl.setText(f"{len(self._notes)} 篇笔记")
        if self._current_rel:
            self._select_in_tree(self._current_rel)

    def _enter_in_search(self) -> None:
        """回车 = Obsidian 快速切换的手感：立刻搜，并打开排最前的那条。"""
        self._apply_search()
        first = self.tree.topLevelItem(0)
        rel = first.data(0, _ROLE_REL) if first else None
        if rel:
            self._open(rel, line=first.data(0, _ROLE_LINE) or 0)
            self._select_in_tree(rel)

    def eventFilter(self, obj, event):  # noqa: N802, ANN001
        if (obj is self.search_input
                and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            self._escape_search()
            return True
        return super().eventFilter(obj, event)

    def _escape_search(self) -> None:
        if self.search_input.text():
            self.search_input.clear()
            # 清空要立刻重建：还等 220ms 防抖的话，列表停在旧结果上，
            # 看起来就像 Esc 按了没反应。
            self._apply_search()
        else:
            self.search_input.clearFocus()
            self.tree.setFocus()

    def _on_click(self, item: QTreeWidgetItem, _col: int) -> None:
        rel = item.data(0, _ROLE_REL)
        if rel:
            self._open(rel, line=item.data(0, _ROLE_LINE) or 0)

    def _on_double_click(self, item: QTreeWidgetItem, col: int) -> None:
        rel = item.data(0, _ROLE_REL)
        if not rel:
            return                      # 双击文件夹只该展开/收起，别把上一篇丢给 Obsidian
        # Qt 会先派发 clicked 再派发 doubleClicked，这篇通常已经打开了；
        # 无条件再 _open 一次就是白做一遍渲染 + 全库反向链接扫描。
        if rel != self._current_rel:
            self._open(rel, line=item.data(0, _ROLE_LINE) or 0)
        self._open_in_obsidian(rel)

    def _select_in_tree(self, rel: str) -> None:
        it = self._find_item(rel)
        if it is None:
            return
        parent = it.parent()
        while parent is not None:        # 折叠着的文件夹，不展开就滚不到、看不见
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(it)
        self.tree.scrollToItem(it)

    def _find_item(self, rel: str) -> QTreeWidgetItem | None:
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it is None:
                continue
            if it.data(0, _ROLE_REL) == rel:
                return it
            stack.extend(it.child(i) for i in range(it.childCount()))
        return None

    def _open(self, rel: str, line: int = 0) -> None:
        self._current_rel = rel
        self.doc_title.setText(os.path.splitext(os.path.basename(rel))[0])
        self.doc_path.setText(rel)
        self.doc_stat.setText(vault.stats(rel))
        self._render_preview()
        self._refresh_side()
        if line:
            kw = self.search_input.text().strip()
            if kw:
                QTimer.singleShot(0, lambda: self.preview.find(kw))

    def _show_empty(self) -> None:
        self._current_rel = ""
        if self._notes:
            self.doc_title.setText("未选择笔记")
            self.doc_path.setText("从左边挑一篇，或按 Ctrl+O 搜索")
        else:
            self.doc_title.setText("没有可看的笔记")
            self.doc_path.setText("点左上「选择 Obsidian 库」，指到你的库目录")
        self.doc_stat.setText("")
        self.preview.clear()
        self.outline.set_hint("—")
        self.backlinks.set_hint("—")

    # ---------------------------------------------------------------- 预览
    def _render_preview(self) -> None:
        if not self._current_rel:
            return
        html = vault.render_html(self._current_rel)
        self.preview.setHtml(html)
        self._apply_doc_format()
        self._load_images(html)

    def _load_images(self, html: str) -> None:
        """文档里每张图都按视口夹宽后塞进资源表；网络图异步补。"""
        doc = self.preview.document()
        limit = max(200, self.preview.viewport().width() - 44)
        for src, hint in vault.image_refs(html):
            img = self._loader.load(src, hint, limit)
            if img is not None:
                doc.addResource(_IMAGE_OBJECT, src, img)

    def _on_image_ready(self, src: str) -> None:
        img = self._loader.get(src)
        if img is not None:
            # 图可能属于上一篇笔记，addResource 只是塞进资源表，不引用就没影响
            self.preview.document().addResource(_IMAGE_OBJECT, src, img)

    def _refresh_side(self) -> None:
        heads = vault.headings(self._current_rel)
        if heads:
            self.outline.show_headings(heads)
            self._heading_marks = self.outline.marks(self.preview.document())
        else:
            self.outline.set_hint("这篇没有标题")
            self._heading_marks = []

        self.backlinks.clear_rows()
        links = vault.backlinks(self._current_rel, notes=self._notes)
        if links:
            for b in links[:_MAX_BACKLINKS]:
                n = b["note"]
                self.backlinks.add_row(
                    f"{n['name']}  ({b['count']})",
                    tip=f"{n['rel']} · 第 {b['line']} 行",
                    on_click=lambda mods, r=n["rel"]: self._jump(r))
                if b.get("snippet"):
                    self.backlinks.add_note(b["snippet"])
            if len(links) > _MAX_BACKLINKS:
                self.backlinks.add_note(
                    f"还有 {len(links) - _MAX_BACKLINKS} 条，去 Obsidian 的"
                    "反向链接面板看全")
        else:
            self.backlinks.set_hint("链接当前文件 0")

    def _on_outline_jump(self, heading: str) -> None:
        """大纲点击默认在本应用里滚过去；Ctrl+点击才交给 Obsidian 定位。

        两处都要传去掉行内标记后的文本：Obsidian 的 &heading= 也是拿渲染后的
        标题去匹配的，带 ** 过去同样定位不了。
        """
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            self._open_in_obsidian(heading=heading)
        else:
            self._scroll_to_heading(heading)

    def _sync_outline_current(self) -> None:
        """正文滚到哪一节，右侧大纲就高亮哪一条。"""
        if not self._heading_marks:
            return
        y = self.preview.verticalScrollBar().value()
        cur = None
        for mark_y, item in self._heading_marks:
            if mark_y > y + 10:
                break
            cur = item
        self.outline.set_current(cur)

    def _scroll_to_heading(self, text: str) -> None:
        """在渲染出的文档里找这个标题所在的块，把它摆到视口顶部。

        不能用 `while not cursor.atEnd()` + NextBlock 遍历：游标走到最后一个块就停在
        块首，atEnd() 永远等不到，标题找不到时就直接把界面钉死。QTextBlock 迭代器
        走到末尾会返回 invalid，循环自然收敛。
        """
        want = vault.plain_text(text)
        block = self.preview.document().begin()
        fallback = None
        while block.isValid():
            if vault.plain_text(block.text()) == want:
                if block.blockFormat().headingLevel() > 0:
                    self._place_cursor(block)
                    return
                if fallback is None:
                    fallback = block
            block = block.next()
        # 标题里带行内标记（**加粗**、`代码`）时纯文本对不上，退化成搜索
        if fallback is not None:
            self._place_cursor(fallback)
        else:
            self.preview.find(want)

    def _place_cursor(self, block) -> None:  # noqa: ANN001
        cur = QTextCursor(block)
        self.preview.setTextCursor(cur)
        lay = block.layout()
        if lay is None:
            self.preview.ensureCursorVisible()
            return
        # ensureCursorVisible 只保证「露个头」，实测会把目标停在视口最下沿，
        # 差整整一屏，看着就像点了没反应。按块在文档里的 y 直接摆到顶部。
        bar = self.preview.verticalScrollBar()
        bar.setValue(max(0, int(lay.position().y()) - _HEADING_TOP_PAD))

    def _jump(self, rel: str) -> None:
        self._open(rel)
        self._select_in_tree(rel)

    def _on_anchor(self, url) -> None:  # noqa: ANN001
        if url.scheme() != "lifeapp":
            return
        path = url.path().lstrip("/")
        if path.startswith("note/"):
            target = path[5:]
            rel = vault.resolve_link(target, notes=self._notes)
            if rel:
                self._jump(rel)
            else:
                popups.notify(self, "没有这篇笔记",
                              f"库里找不到 [[{target}]]。\n要新建请在 Obsidian 里建。")
        elif path.startswith("tag/"):
            # 标签不做跨库索引，直接把它填进搜索框，走同一套全文搜索
            self.search_input.setText(f"#{path[4:]}")

    def _on_theme_changed(self) -> None:
        self._apply_preview_style()
        self._apply_tree_style()
        self.reload()
        self._render_preview()

    # ---------------------------------------------------------------- Obsidian
    def _open_in_obsidian(self, rel: str = "", heading: str = "") -> None:
        rel = rel or self._current_rel
        if not rel:
            popups.notify(self, "还没选笔记", "左边先挑一篇。")
            return
        if not vault.open_note(rel, heading):
            popups.notify(
                self, "打不开 Obsidian",
                "系统里没有能处理 obsidian:// 的程序。\n"
                "确认 Obsidian 已安装，或在「设置」里重新指一次库目录。",
                danger=True)

    def _reveal(self) -> None:
        root = vault.vault_path()
        if not self._current_rel or not root:
            return
        full = os.path.join(root, self._current_rel.replace("/", os.sep))
        if not vault.reveal_in_folder(full):
            popups.notify(self, "找不到文件", full, danger=True)

    def _search_in_obsidian(self) -> None:
        kw = self.search_input.text().strip()
        if not kw:
            popups.notify(self, "先输入关键词",
                          "搜索框里写点什么，再跳 Obsidian 搜。")
            return
        vault.open_search(kw)

    def _switch_vault(self) -> None:
        if vault.pick_vault(self):
            self.reload()

    # ---------------------------------------------------------------- 同步
    def _sync(self) -> None:
        if vault.sync_from_ui(self):
            self.reload()
