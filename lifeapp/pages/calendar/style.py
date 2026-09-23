"""日历模块自己的 QSS。

为什么不放进全局 theme.py：
1. theme.py 是并行会话改动最频繁的文件之一，为单个模块去改它容易互相冲掉
   （本次改造就出过一次「整段替换把模板收尾引号吃掉」的事故）；
2. 滴答的尺寸要按实测来，改起来频繁，放模块内改一次就生效。

尺寸依据（参考图是 150% 缩放的物理像素，逻辑像素 = 物理 ÷ 1.5）：
    色条高 25px → 17；行距 27px → 18
    日期号、星期表头、标题按滴答图量出来分别取 15 / 14 / 24
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QLabel, QMenu, QPushButton, QWidget

from ... import theme


class _Reapply(QObject):
    """主题信号的一个挂点；parent 设成宿主，宿主销毁时它跟着销毁、连接自动断。"""

    def __init__(self, owner: QWidget, fn):
        super().__init__(owner)
        self._fn = fn
        theme.manager.changed.connect(self._rerun)

    def _rerun(self) -> None:
        self._fn()


# 滴答这几张卡里的「蓝」实测是 #4772fa，和全局色板的 accent（#00a5ff，偏青）
# 不是一回事。accent 换掉会影响全应用，所以这里按模块局部取色。
def tick_blue() -> str:
    return "#8ba3ff" if theme.is_dark() else "#4772fa"


def reapply(owner: QWidget, fn) -> None:
    """立刻跑一次 ``fn``，并在换肤之后再跑一次。

    图标是 grab 出来的位图，颜色烤死在图里：亮色下建好的图标切到夜间主题，
    就是一张灰底深字的图贴在深色卡上（反过来的方向几乎看不见）。页头那几个
    常驻按钮建一次用一整天，所以得跟着主题重画。
    """
    fn()
    if theme.manager is not None:
        _Reapply(owner, fn)


def grab(kind: str, color: str = "muted", size: int = 15, glyph: str = ""):
    """按当前主题画一枚图标；``color`` 给主题色键，也给十六进制色。

    ``glyph`` 是格子里那行小字（日历图标里的「7」「一」），滴答的快捷日期按钮
    就靠它区分「一周后」和「挑日期」。
    """
    from ... import todo_icons
    ic = todo_icons.TickIcon(kind, size, "muted")
    if color.startswith("#"):
        ic.set_color_hex(color)
    else:
        ic.set_color_key(color)
    if glyph:
        ic.set_glyph(glyph)
    return ic.grab()


def themed_icon(owner: QWidget, kind: str, color: str = "muted",
                size: int = 15) -> None:
    """``reapply`` 的图标版，给「图标不换、只换色」的按钮和标签用。"""
    def apply() -> None:
        pm = grab(kind, color, size)
        if isinstance(owner, QLabel):
            owner.setPixmap(pm)
        else:
            owner.setIcon(QIcon(pm))

    reapply(owner, apply)


def icon(kind: str, size: int = 15, color_key: str = "muted",
         hex_color: str = ""):
    """自绘图标转 QIcon。``hex_color`` 用来画色板里没有的颜色（比如滴答蓝）。"""
    from PySide6.QtGui import QIcon
    from ... import todo_icons
    ic = todo_icons.TickIcon(kind, size, color_key)
    if hex_color:
        ic.set_color_hex(hex_color)
    return QIcon(ic.grab())


def icon_btn(kind: str, tip: str, slot, size: int = 30) -> QPushButton:
    """卡片右上角那种「只有图标、悬停有浅底」的按钮。"""
    b = QPushButton()
    b.setObjectName("CardIconBtn")
    themed_icon(b, kind, "muted", 16)
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedSize(size, size)
    b.clicked.connect(slot)
    return b


def sep_line() -> QFrame:
    """#f1f1f1 的横向分隔线（滴答卡片头部下面那条）。"""
    line = QFrame()
    line.setObjectName("CardHairline")
    line.setFixedHeight(1)
    return line


def labeled_btn(icon: str, text: str, object_name: str, slot,
                icon_color: str = "muted") -> QPushButton:
    """图标 + 文字 + 悬停浅底 的胶囊按钮（日期、清单都用这套）。"""
    b = QPushButton(f"  {text}")
    b.setObjectName(object_name)
    themed_icon(b, icon, icon_color, 15)
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(slot)
    return b


def qss() -> str:
    """按当前主题现算一份 QSS（主题切换后页面要重新 apply）。"""
    g = theme.get
    dark = theme.is_dark()
    # 月视图的网格线、周末底色按滴答实测取「中性灰」。色板里的 border 是
    # #dcdfea，带蓝味且比滴答重一档，画成整页网格会明显更「花」。
    grid = "#2b2b3a" if dark else "#f1f1f1"
    wknd = "#24242f" if dark else "#fafafa"
    tick = tick_blue()
    return f"""
QPushButton#CalViewBtn {{
    background: {g('surface')}; border: 1px solid {g('border_strong')};
    border-radius: 10px; min-width: 54px; min-height: 36px; padding: 0 10px;
    color: {g('text')}; font-size: 14px; font-weight: 600;
}}
QPushButton#CalViewBtn:hover {{ border-color: {g('accent')}; color: {g('accent')}; }}
/* 右上角「收起侧栏」和「⋯」：滴答是**无边框的裸图标**，只有悬停才出一块浅灰。
   之前跟 CalViewBtn 共用一条规则，被画成了带边框的胶囊，用户一眼就看出来不对。 */
QPushButton#CalIconBtn {{
    background: transparent; border: none; border-radius: 9px;
    min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
    padding: 0; color: {g('muted')}; }}
QPushButton#CalIconBtn:hover {{ background: {g('surface_hi')}; }}
QPushButton#CalIconBtn:pressed {{ background: {g('border')}; }}
QFrame#CalNavGroup {{
    background: {g('surface')}; border: 1px solid {g('border_strong')};
    border-radius: 10px; }}
QPushButton#CalNavBtn {{
    background: transparent; border: none; border-radius: 9px;
    min-width: 38px; min-height: 34px; color: {g('muted')}; font-size: 15px; padding: 0; }}
QPushButton#CalNavBtn:hover {{ background: {g('accent_soft')}; color: {g('accent')}; }}
QPushButton#CalToday {{
    background: transparent; border: none; border-left: 1px solid {g('border')};
    border-right: 1px solid {g('border')}; border-radius: 0;
    min-width: 58px; min-height: 34px; color: {g('text')}; font-size: 14px; padding: 0; }}
QPushButton#CalToday:hover {{ background: {g('accent_soft')}; color: {g('accent')}; }}

QFrame#CalTitleBox {{ background: transparent; border: none; border-radius: 8px; }}
QFrame#CalTitleBox[hover="true"] {{ background: {g('surface_hi')}; }}
QLabel#CalTitleBig {{ font-size: 24px; font-weight: 800; color: {g('text_hi')};
    background: transparent; }}
QLabel#CalTitleSmall {{ font-size: 17px; font-weight: 600; color: {g('text')};
    background: transparent; padding-bottom: 4px; }}
QLabel#CalTitleCaret {{ font-size: 14px; color: {g('muted')}; background: transparent;
    padding-bottom: 6px; }}

/* ---------- 月视图 ---------- */
QFrame#CalWeekHead {{ background: {g('bg_alt')}; border-bottom: 1px solid {grid}; }}
QLabel#CalWd {{ color: {g('muted')}; font-size: 12.5px; padding: 11px 0; }}
/* 滴答实测：列之间有竖线、周与周之间有横线，都是 #f1f1f1；
   最左最外两侧没有线，表头那一行没有竖线。 */
QFrame#CalCell {{ background: {g('surface')}; border: none;
    border-right: 1px solid {grid}; }}
QFrame#CalCell[edge="last"] {{ border-right: none; }}
QFrame#CalCell[weekend="true"] {{ background: {wknd}; }}
QFrame#CalCell[sep="true"] {{ border-bottom: 1px solid {grid}; }}
QFrame#CalCell[dim="true"] QLabel#CalDayNum {{ color: {g('border_strong')}; }}
QFrame#CalCell[drop="true"] {{
    background: {g('accent_soft')}; border-bottom: 2px solid {g('accent')}; }}
QLabel#CalDayNum {{ font-size: 15px; color: {g('text')}; background: transparent; }}
QFrame#CalCell[today="true"] QLabel#CalDayNum {{
    background: {g('accent')}; color: #ffffff; border-radius: 12px; font-weight: 700; }}
QLabel#CalFlag {{
    font-size: 10.5px; color: #ffffff; border-radius: 8px;
    min-width: 16px; max-width: 16px; min-height: 16px; max-height: 16px;
    background: transparent; }}
QLabel#CalFlag[flagKind="off"] {{ background: {g('green')}; }}
QLabel#CalFlag[flagKind="work"] {{ background: {g('red')}; }}
QLabel#CalFestival {{ font-size: 12.5px; color: {g('green')}; background: transparent; }}
QLabel#CalFestival[major="true"] {{ color: {g('red')}; }}
QPushButton#CalCellPlus {{
    background: {g('surface')}; border: 1px solid {g('border_strong')};
    border-radius: 10px; color: {g('muted')}; font-size: 13px; padding: 0; }}
QPushButton#CalCellPlus:hover {{ border-color: {g('accent')}; color: {g('accent')}; }}
QPushButton#CalMore {{
    background: transparent; border: none; color: {g('muted')};
    font-size: 12px; text-align: left; padding: 1px 6px; }}
QPushButton#CalMore:hover {{ color: {g('accent')}; }}

/* ---------- 时间轴 ---------- */
QFrame#CalTimeHead {{ background: {g('bg_alt')}; border-bottom: 1px solid {g('border')}; }}
QFrame#CalTimeHeadCell {{ background: transparent; border-left: 1px solid {g('border')}; }}
QLabel#CalTimeWd {{ color: {g('muted')}; font-size: 12.5px; background: transparent; }}
QLabel#CalTimeNum {{ color: {g('text')}; font-size: 16px; font-weight: 700;
    background: transparent; }}
QFrame#CalTimeHeadCell[today="true"] QLabel#CalTimeNum {{
    background: {g('accent')}; color: #ffffff; border-radius: 13px;
    min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px;
    qproperty-alignment: center; }}
QFrame#CalTimeHeadCell[dim="true"] QLabel#CalTimeNum {{ color: {g('border_strong')}; }}

/* ---------- 任务卡片弹层 ---------- */
QFrame#TaskCard {{ background: {g('surface')}; border: 1px solid {g('border')};
    border-radius: 14px; }}
QFrame#TaskCard QLabel, QFrame#TaskCard QLineEdit, QFrame#TaskCard QTextEdit {{
    background: transparent; }}
QLabel#CardSep {{ color: {g('border_strong')}; font-size: 15px; }}
QPushButton#CardDateChip {{
    background: transparent; border: none; border-radius: 8px;
    padding: 4px 7px; font-size: 15px; font-weight: 600; text-align: left; }}
QPushButton#CardDateChip:hover {{ background: {g('surface_hi')}; }}
QPushButton#CardDateChip[chipColor="red"] {{ color: {g('red')}; }}
QPushButton#CardDateChip[chipColor="accent"] {{ color: {tick}; }}
QPushButton#CardDateChip[chipColor="tick"] {{ color: {tick}; }}
QPushButton#CardDateChip[chipColor="text"] {{ color: {g('text')}; }}
QPushButton#CardDateChip[chipColor="muted"] {{ color: {g('muted')}; }}
QFrame#CardHairline {{ background: {grid}; border: none; }}
QPushButton#CardIconBtn {{
    background: transparent; border: none; border-radius: 8px;
    min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
    padding: 0; color: {g('muted')}; }}
QPushButton#CardIconBtn:hover {{ background: {g('surface_hi')}; }}
QPushButton#CardIconBtn:checked {{ background: {g('accent_soft')}; }}
QLineEdit#CardTitle {{ border: none; font-size: 17px; font-weight: 700;
    color: {g('text_hi')}; padding: 3px 0; }}
QTextEdit#CardDesc {{ border: none; font-size: 13.5px; color: {g('text')}; padding: 0; }}
QFrame#CardDetail {{ background: transparent; border: none; }}
QListWidget#CardSubList {{ background: transparent; border: none; }}
QListWidget#CardSubList::item {{ min-height: 26px; border-radius: 6px; padding: 0 4px; }}
QListWidget#CardSubList::item:hover {{ background: {g('surface_hi')}; }}
QLineEdit#CardSubAdd {{ border: none; font-size: 13.5px; padding: 4px 0; color: {g('text')}; }}
QPushButton#CardListBtn {{
    background: transparent; border: none; border-radius: 8px;
    padding: 5px 7px; font-size: 13.5px; color: {g('muted')}; text-align: left; }}
QPushButton#CardListBtn:hover {{ background: {g('surface_hi')}; color: {g('text')}; }}
QLabel#TagChip, QPushButton#TagChip {{ font-size: 12.5px; }}

QMenu#CalCardMenu {{
    background: {g('surface')}; border: 1px solid {g('border')};
    border-radius: 10px; padding: 5px; }}
QMenu#CalCardMenu::item {{
    padding: 8px 24px 8px 12px; border-radius: 7px; color: {g('text')}; font-size: 14px; }}
QMenu#CalCardMenu::item:selected {{ background: {grid}; color: {tick}; }}
QMenu#CalCardMenu::indicator {{ width: 15px; height: 15px; }}

/* 右键菜单里嵌的「日期」「优先级」两行（滴答把那两排放在菜单顶上） */
QWidget#CalMenuRow {{ background: transparent; }}
QLabel#CalMenuCap {{ color: {g('muted')}; font-size: 12.5px; background: transparent; }}
QPushButton#CalMenuIcon {{
    background: transparent; border: none; border-radius: 8px; }}
QPushButton#CalMenuIcon:hover {{ background: {g('surface_hi')}; }}
QPushButton#CalMenuIcon[on="true"] {{ background: {grid}; }}

/* ---------- 新建任务卡（点空白格子弹出的那张） ---------- */
/* 尺寸按 new1_card.png 实测：卡宽 410、头部行高 52、左右内边距 23、
   标题 17px、描述 14px、日期胶囊 15px */
QFrame#NewTaskCard {{
    background: {g('surface')}; border: 1px solid {g('border')};
    border-radius: 14px; }}
QLineEdit#NewTitle {{
    border: none; background: transparent; font-size: 17px; font-weight: 700;
    color: {g('text_hi')}; padding: 0; }}
QTextEdit#NewDesc {{
    border: none; background: transparent; font-size: 14px;
    color: {g('text')}; padding: 0; }}
QPushButton#NewListBtn {{
    background: transparent; border: none; border-radius: 8px;
    padding: 5px 7px; font-size: 14.5px; color: {g('text')}; text-align: left; }}
QPushButton#NewListBtn:hover {{ background: {g('surface_hi')}; }}

/* ---------- 清单选择器（卡里点「收集箱」弹出的那块） ---------- */
QFrame#ListPicker {{
    background: {g('surface')}; border: 1px solid {g('border')};
    border-radius: 12px; }}
QLineEdit#PickerSearch {{
    background: transparent; border: none; border-bottom: 1px solid {grid};
    font-size: 14px; color: {g('text')}; padding: 9px 0; }}
QFrame#PickerRow {{ background: transparent; border: none; border-radius: 8px; }}
QFrame#PickerRow:hover {{ background: {g('surface_hi')}; }}
QLabel#PickerText {{ font-size: 14.5px; color: {g('text')}; background: transparent; }}
QLabel#PickerText[on="true"] {{ color: {tick}; }}
QLabel#PickerTick {{ color: {tick}; font-size: 15px; background: transparent; }}
QLabel#PickerHint {{ color: {g('muted')}; font-size: 13px; background: transparent; }}

/* ---------- 日历订阅管理弹层 ---------- */
QFrame#FeedCard {{ background: {g('surface')}; border: none; }}
QLabel#FeedName {{ font-size: 14px; font-weight: 600; color: {g('text')};
    background: transparent; }}
QLabel#FeedSub {{ font-size: 11.5px; color: {g('muted')}; background: transparent; }}
QLabel#FeedSub[bad="true"] {{ color: {g('red')}; }}
QLabel#FeedHint {{ font-size: 12.5px; color: {g('muted')}; background: transparent; }}
QFrame#FeedRow {{ background: transparent; border: none; border-radius: 8px; }}
QFrame#FeedRow:hover {{ background: {g('surface_hi')}; }}
QPushButton#FeedBtn {{
    background: transparent; border: none; border-radius: 7px;
    min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px;
    padding: 0; }}
QPushButton#FeedBtn:hover {{ background: {g('bg_alt')}; }}
QPushButton#FeedAdd {{
    background: transparent; border: none; border-radius: 8px; padding: 7px 2px;
    font-size: 13.5px; font-weight: 600; color: {tick}; text-align: left; }}
QPushButton#FeedAdd:hover {{ background: {g('surface_hi')}; }}
QLineEdit#FeedInput {{
    border: 1px solid {g('border_strong')}; border-radius: 8px;
    padding: 6px 8px; font-size: 13px; background: {g('surface')};
    color: {g('text')}; }}
QLineEdit#FeedInput:focus {{ border-color: {tick}; }}

/* 只给手柄上了色、没管 add-page/sub-page，Qt 就把轨道交回原生样式画，
   那是条半透明白的 Windows 玻璃槽 —— 亮色底上看不出来，暗色下就是一道白杠。 */
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 右侧面板 ---------- */
QScrollArea#CalSide {{
    background: {g('bg_alt')}; border: 1px solid {g('border')}; border-radius: 12px; }}
QScrollArea#CalSide > QWidget > QWidget {{ background: {g('bg_alt')}; }}
QFrame#CalMini {{ background: transparent; border: none; }}
QLabel#CalMiniTitle {{ font-size: 14.5px; font-weight: 700; color: {g('text_hi')}; }}
QPushButton#CalMiniNav {{
    background: transparent; border: none; border-radius: 7px;
    min-width: 26px; max-width: 26px; min-height: 26px;
    color: {g('muted')}; font-size: 13px; padding: 0; }}
QPushButton#CalMiniNav:hover {{ background: {g('surface_hi')}; color: {g('text')}; }}
QLabel#CalMiniWd {{ color: {g('muted')}; font-size: 12px; }}
QPushButton#CalMiniDay {{
    background: transparent; border: none; border-radius: 12px;
    color: {g('text')}; font-size: 13px; padding: 0; }}
QPushButton#CalMiniDay:hover {{ background: {g('surface_hi')}; }}
QPushButton#CalMiniDay[miniState="off"] {{ color: {g('border_strong')}; }}
QPushButton#CalMiniDay[miniState="today"] {{ color: {g('accent')}; font-weight: 700; }}
QPushButton#CalMiniDay[miniState="sel"] {{
    background: {g('accent')}; color: #ffffff; font-weight: 700; }}
QLabel#CalSideHead {{ font-size: 12.5px; color: {g('muted')}; font-weight: 700;
    padding-top: 6px; }}
QFrame#CalSideRow {{ background: transparent; border: none; border-radius: 9px; }}
QFrame#CalSideRow:hover {{ background: {g('surface_hi')}; }}
QLabel#CalSideRowText {{ font-size: 13.5px; color: {g('text')}; background: transparent; }}
QLabel#CalSideBox {{ background: transparent; }}

/* ---------- 安排任务抽屉 ---------- */
QFrame#CalDrawer {{
    background: {g('bg_alt')}; border: 1px solid {g('border')}; border-radius: 12px; }}
QLabel#CalDrawerTitle {{ font-size: 16px; font-weight: 800; color: {g('text_hi')}; }}
QLabel#CalDrawerGroup {{ font-size: 13px; color: {g('text')}; font-weight: 600;
    background: transparent; }}
QLabel#CalDrawerCount {{ font-size: 12.5px; color: {g('muted')}; background: transparent; }}
QFrame#CalDrawerGroupRow {{ background: transparent; border: none; border-radius: 8px; }}
QFrame#CalDrawerGroupRow:hover {{ background: {g('surface_hi')}; }}
QPushButton#CalCrumb {{
    background: transparent; border: none; border-radius: 7px; padding: 5px 7px;
    font-size: 13.5px; font-weight: 600; color: {g('accent')}; text-align: left; }}
QPushButton#CalCrumb:hover {{ background: {g('accent_soft')}; }}
QFrame#CalChip {{ border: none; }}
QLabel#CalChipText {{ font-size: 13px; background: transparent; }}
QLabel#CalFlagSm {{ background: transparent; }}
"""


def menu(parent=None) -> QMenu:
    """建一个已经贴上日历样式的 QMenu。"""
    m = QMenu(parent)
    m.setObjectName("CalCardMenu")
    m.setStyleSheet(qss())
    return m


def apply_to(widget) -> None:
    """把日历样式贴到某个顶层控件上。

    卡片、快速添加、菜单都是独立顶层窗口，父控件的样式表盖不住它们，
    所以每个都要单独 apply。
    """
    widget.setStyleSheet(qss())
