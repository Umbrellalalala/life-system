"""主题系统：日间/夜间双主题 + 丝滑切换动画。

设计要点：
- 简洁平整（flat）风格：无阴影，靠层次与细边框区分。
- 全局样式走 QSS；组件状态色用动态属性（tagColor / statColor / moneySign / modeColor），
  因此主题切换时全局 QSS 自动生效，无需逐个刷新。
- 切换动画：对每个颜色通道做 RGB 线性插值，逐帧重生成 QSS。
"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import (
    QObject, Signal, Qt, QPropertyAnimation, QEasingCurve, QAbstractAnimation,
    QPointF,
)
from PySide6.QtGui import QColor, QPolygonF, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QGraphicsOpacityEffect

# ---------- 复选框的对勾图 ----------
_CHECK_FILE = os.path.join(tempfile.gettempdir(), "life_system_check_mark.png")
_CHECK_IMG = ""


def check_icon_path() -> str:
    """勾选态那枚白色对勾。

    QSS 的 ``image:`` 只能引文件、不能内联画，所以用 QPainter 现生成一张 PNG
    缓存到临时目录。生成失败就退回「纯色块没勾」的老样子，不影响启动。
    """
    global _CHECK_IMG
    if _CHECK_IMG:
        return _CHECK_IMG
    try:
        pm = QPixmap(40, 40)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#ffffff"), 3.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(9, 21), QPointF(17, 29),
                                  QPointF(31.5, 11.5)]))
        p.end()
        if not pm.save(_CHECK_FILE, "PNG"):
            return ""
        _CHECK_IMG = _CHECK_FILE.replace("\\", "/")
    except Exception:  # noqa: BLE001
        return ""
    return _CHECK_IMG


# ---------- 两套配色（语义化键） ----------
LIGHT = {
    "bg": "#f4f5f9",
    "bg_alt": "#ffffff",
    "nav_bg": "#fafbfd",
    "surface": "#ffffff",
    "surface_hi": "#eef0f6",
    "border": "#dcdfea",
    "border_strong": "#d6d9e4",
    "text": "#1b1e2b",
    "text_hi": "#10121b",
    "muted": "#8b8fa3",
    # 滴答蓝：待办模块的主强调色（选中态 / 日期高亮 / 主按钮）
    "accent": "#00a5ff",
    "accent_hi": "#33b8ff",
    "accent_soft": "#e6f6ff",
    # 快速添加里「今天16:00 / #清单 / @标签」的内联识别色。
    # 直接从滴答截图取色（淡靛底 + 同色系蓝字）：这套色和 accent 不同相，
    # 用强调色淡化的话在灰白底上会发青，一眼就能看出不是滴答。
    "nlp_bg": "#c8d5fe",
    "nlp_fg": "#4772fa",
    # 勾选框选中态：滴答勾完就变中性灰白勾，不再跟着优先级走色
    # （优先级只作用在未勾选时的描边上）
    "check_done": "#abafb9",
    "green": "#0db987",
    "green_soft": "#e2f8f1",
    "red": "#f0435f",
    "red_soft": "#fde9ed",
    "amber": "#ed9a12",
    "amber_soft": "#fdf1dc",
    "blue": "#3f86e0",
    "blue_soft": "#e6f0fd",
    "hero_top": "#dff2e9",
    "hero_bottom": "#f2faf6",
    # 专注模块专用蓝（番茄钟 / 统计页）
    "focus": "#3f6ef5",
    "focus_hi": "#5480f7",
    "focus_soft": "#e9f0ff",
    # 番茄钟刻度环的未点亮刻度。原来的 #e8eaf2 对页面底 #f4f5f9 只差 30/765，
    # 刻度几乎糊在背景里；参考设计的刻度对白底差 60/765，按同等对比度换算到
    # 我们的底色上就是 #e0e2e8（落在 border #e6e8f0 与 border_strong #d6d9e4 之间）。
    "focus_track": "#e0e2e8",
}

DARK = {
    "bg": "#15151d",
    "bg_alt": "#1b1b26",
    "nav_bg": "#191922",
    "surface": "#20202d",
    "surface_hi": "#292939",
    "border": "#31313f",
    "border_strong": "#3d3d4d",
    "text": "#e7e7f0",
    "text_hi": "#ffffff",
    "muted": "#8e8ea8",
    "accent": "#35b6ff",
    "accent_hi": "#5cc6ff",
    "accent_soft": "#14313f",
    # 夜间用同一支靛蓝的暗版：底色压到表面色之下，字色提亮保持可读
    "nlp_bg": "#2e3a63",
    "nlp_fg": "#9db4ff",
    "check_done": "#5c6274",
    "green": "#3dd68c",
    "green_soft": "#16382c",
    "red": "#ff5d7a",
    "red_soft": "#3a1f27",
    "amber": "#ffb020",
    "amber_soft": "#3a2d18",
    "blue": "#5fa0ff",
    "blue_soft": "#1d2c45",
    "hero_top": "#1d3126",
    "hero_bottom": "#17211d",
    # 专注模块专用蓝（番茄钟 / 统计页）
    "focus": "#5b86ff",
    "focus_hi": "#6f96ff",
    "focus_soft": "#1c2740",
    "focus_track": "#2c2c3a",
}

# ---------- QSS 模板（@key@ 占位符，避免与 {} 冲突） ----------
_QSS_TEMPLATE = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    outline: none;
}

QMainWindow, QWidget#Root { background: @bg@; color: @text@; }

/* ---------- 侧边栏 ---------- */
QFrame#Sidebar {
    background: @bg_alt@;
    border-right: 1px solid @border@;
}
QLabel#AppTitle {
    font-size: 19px; font-weight: 800; color: @text_hi@;
    padding: 4px 6px;
}
QLabel#AppSubtitle { font-size: 11px; color: @muted@; }

QPushButton#NavButton {
    background: transparent; border: none; border-radius: 10px;
    padding: 10px 14px; text-align: left;
    font-size: 13.5px; color: @muted@;
}
QPushButton#NavButton:hover { background: @surface_hi@; color: @text@; }
QPushButton#NavButton:checked {
    background: @accent_soft@; color: @accent@; font-weight: 700;
}
QPushButton#NavButton[collapsed="true"] {
    text-align: center; padding: 10px 0px; font-size: 16px;
}

QPushButton#ThemeToggle {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 10px;
    padding: 9px 12px; text-align: left; font-size: 13px; color: @text@;
}
QPushButton#ThemeToggle:hover { border-color: @accent@; color: @accent@; }
QPushButton#ThemeToggle[collapsed="true"] {
    text-align: center; padding: 9px 0px;
}

/* 侧边栏收起/展开手柄（悬浮在侧边栏边缘、上下居中的窄条） */
QPushButton#SidebarHandle {
    background: @bg_alt@; border: 1px solid @border@; border-radius: 7px;
    color: @muted@; font-size: 12px; padding: 0;
}
QPushButton#SidebarHandle:hover {
    border-color: @accent@; color: @accent@; background: @accent_soft@;
}

/* ---------- 卡片 ---------- */
QFrame#Card {
    background: @surface@;
    border: 1px solid @border@;
    border-radius: 14px;
}
QLabel#CardTitle { font-size: 14px; font-weight: 700; color: @text_hi@; }
QLabel#Muted { color: @muted@; font-size: 12px; }

/* 待办条目 */
QFrame#Card[done="true"] { background: @bg@; }
QLabel#TodoTitle { font-size: 14px; font-weight: 600; color: @text@; }
QLabel#TodoTitle[done="true"] { color: @muted@; }
QLabel#TodoList { font-size: 12px; color: @muted@; }

/* ---------- 工具内嵌页（侧边栏独立项） ---------- */
QFrame#ToolHeader {
    background: @bg_alt@;
    border-bottom: 1px solid @border@;
}
QWidget#EmbedHost {
    background: @bg@;
    border: none;
}

/* ---------- 通用文字类（主题切换自动跟随） ---------- */
QLabel#H1 { font-size: 20px; font-weight: 800; color: @text_hi@; }
QLabel#Strong { font-weight: 700; color: @text_hi@; }
QLabel#Meta { color: @muted@; font-size: 12px; }
QLabel[cText="red"]      { color: @red@; }
QLabel[cText="amber"]    { color: @amber@; }
QLabel[cText="blue"]     { color: @blue@; }
QLabel[cText="green"]    { color: @green@; }
QLabel[cText="accent"]   { color: @accent@; }
QLabel[cText="muted"]    { color: @muted@; }
QLabel[cText="text"]     { color: @text@; }
QLabel[cText="text_hi"]  { color: @text_hi@; }
QLabel[strongColor="red"]    { color: @red@;    font-weight: 700; }
QLabel[strongColor="amber"]  { color: @amber@;  font-weight: 700; }
QLabel[strongColor="blue"]   { color: @blue@;   font-weight: 700; }
QLabel[strongColor="green"]  { color: @green@;  font-weight: 700; }
QLabel[strongColor="accent"] { color: @accent@; font-weight: 700; }

/* 大纲按钮（Obsidian 风格） */
QPushButton#OutlineBtn {
    text-align: left; border: none; border-radius: 6px;
    background: transparent; color: @muted@; padding: 3px 8px;
}
QPushButton#OutlineBtn:hover { background: @surface_hi@; color: @text@; }

/* ---------- 待办：滴答清单三栏 ---------- */
QFrame#TodoNav {
    background: @nav_bg@;
    border-right: 1px solid @border@;
}
QScrollArea#TodoNavScroll { background: transparent; border: none; }
QScrollArea#TodoNavScroll > QWidget > QWidget { background: transparent; }

/* 三栏分割条：6px 才拖得住，但视觉上只想要滴答那条发丝线。
   分割器本体涂成中栏的白，那 6px 就「消失」在中栏里，出线仍然靠左右两栏
   各自的 1px 边框；只有悬停时才亮一下强调色，提示这里能拖。
   （本体设 transparent 的话，这 6px 会露出页面灰底，量出来是一道 9 物理像素
     的灰条，比原来的发丝线粗得多。） */
QSplitter#TodoSplit { background: @bg_alt@; }
QSplitter#TodoSplit::handle:horizontal {
    width: 6px; background: transparent; border: none;
}
QSplitter#TodoSplit::handle:horizontal:hover { background: @accent_soft@; }

/* 导航行：图标 + 文字 + 右对齐计数（滴答式，非 QPushButton） */
QWidget#SideRow { background: transparent; border: none; border-radius: 8px; }
QWidget#SideRow:hover { background: @surface_hi@; }
QWidget#SideRow[checked="true"] { background: @accent_soft@; }
/* 拖任务悬停在左栏时的落点高亮（滴答会把可放置的目标整行点亮） */
QWidget#SideRow[dropHot="true"] {
    background: @accent_soft@; border: 1px dashed @accent@; border-radius: 8px;
}
QLabel#SideRowText { font-size: 13px; color: @text@; background: transparent; }
QLabel#SideRowText[tint="red"]    { color: @red@; }
QLabel#SideRowText[tint="amber"]  { color: @amber@; }
QLabel#SideRowText[tint="blue"]   { color: @blue@; }
QLabel#SideRowText[tint="green"]  { color: @green@; }
QLabel#SideRowText[tint="muted"]  { color: @muted@; }
/* 滴答的选中行只铺一层底色，文字仍是深色，只是加粗。
   这里不去改 color：后代选择器权重比上面的 [tint="red"] 之类更高，
   一旦写成强调色，四象限那些彩色清单名选中时就会被统一刷成蓝色。 */
QWidget#SideRow[checked="true"] QLabel#SideRowText { font-weight: 600; }
QLabel#SideRowCount { font-size: 12px; color: @muted@; background: transparent; }
QPushButton#RowBtn {
    background: transparent; border: none; border-radius: 5px;
    color: @muted@; font-size: 12px; padding: 0;
}
QPushButton#RowBtn:hover { background: @border@; color: @text@; }

QLabel#SideSection {
    font-size: 11px; color: @muted@; padding: 4px 2px 2px;
    font-weight: 600;
}

/* 中栏 */
QFrame#TodoCanvas { background: @bg_alt@; border: none; }
QLabel#ViewTitle { font-size: 19px; font-weight: 700; color: @text_hi@; }
QLineEdit#TodoSearch {
    background: @bg@; border: 1px solid @border@; border-radius: 8px;
    padding: 6px 10px; font-size: 13px;
}
QLineEdit#TodoSearch:focus { border-color: @accent@; background: @bg_alt@; }
QScrollArea#TodoListScroll { background: transparent; border: none; }
QScrollArea#TodoListScroll > QWidget > QWidget { background: transparent; }
QFrame#QuickAdd {
    background: @bg@;
    border: 1px solid transparent;
    border-radius: 10px;
}
QFrame#QuickAdd[focused="true"] {
    background: @bg_alt@;
    border: 1px solid @accent@;
}
QLineEdit#QuickAddInput {
    background: transparent; border: none; padding: 9px 10px;
    font-size: 13.5px; color: @text@;
}

/* 右栏详情 */
QFrame#DetailPane {
    background: @bg_alt@;
    border-left: 1px solid @border@;
}
/* 窄窗口下详情是浮在列表右侧的抽屉：四边都描一圈，才看得出是盖在上面的一层 */
QFrame#DetailPane[floating="true"] {
    background: @bg_alt@;
    border: 1px solid @border_strong@;
}
QLabel#DetailEmpty { color: @muted@; font-size: 13px; }
QLineEdit#DetailTitleInput, QTextEdit#DetailTitleInput {
    background: transparent; border: none; font-size: 15.5px; font-weight: 600;
    color: @text_hi@; padding: 2px 0;
}
QLabel#DetailDesc { font-size: 13px; color: @muted@; }
QTextEdit#DetailDescInput {
    background: transparent; border: none; font-size: 13px; color: @text@;
}
QFrame#DetailMetaRow { background: transparent; border: none; border-radius: 8px; }
QFrame#DetailMetaRow:hover { background: @surface_hi@; }
QLabel#DetailMetaText { font-size: 13px; color: @text@; background: transparent; }
QLabel#DetailMetaText[placeholder="true"] { color: @muted@; }

/* 详情面板：顶栏 / 分隔线 / 子任务行 / 底栏（对齐滴答的紧凑结构） */
QFrame#DetailHead { background: @bg_alt@; border: none; }
/* 窄屏整页化时的返回箭头 */
QPushButton#DetailBack {
    background: transparent; border: none; border-radius: 6px;
    color: @muted@; font-size: 20px; font-weight: 700; padding: 0 0 3px 0;
}
QPushButton#DetailBack:hover { background: @surface_hi@; color: @text@; }
QFrame#DetailFoot { background: @bg_alt@; border: none; }
QFrame#DetailVBar { background: @border@; border: none; }
QFrame#Hairline { background: @border@; border: none; }
QFrame#RowLine { background: @border@; border: none; }
QFrame#SubRow { background: transparent; border: none; }
QFrame#SubRow:hover { background: @surface_hi@; }
/* 子任务拖动时的落点指示：靠上/靠下各画一条 2px accent 线 */
QFrame#SubRow[drop="above"] { border-top: 2px solid @accent@; }
QFrame#SubRow[drop="below"] { border-bottom: 2px solid @accent@; }
QLineEdit#SubRename {
    background: transparent; border: none; padding: 0;
    font-size: 13px; color: @text@;
}
QLineEdit#SubAddInput { background: transparent; border: none; font-size: 13px; }
QLabel#SubTaskTitle { background: transparent; }

/* 行内中性图标按钮（IconBtn 的 hover 是删除红，这里要中性） */
QPushButton#ToolBtn {
    background: transparent; border: none; border-radius: 6px;
    padding: 2px 4px; color: @muted@;
}
QPushButton#ToolBtn:hover { background: @border@; color: @text@; }

/* 空状态 */
QLabel#EmptyHint { font-size: 12.5px; color: @muted@; }

/* 清单 / 标签 / 过滤器对话框 */
QDialog#TickDialog { background: @bg_alt@; }
QLabel#DlgTitle { font-size: 15px; font-weight: 700; color: @text_hi@; }
QLabel#DlgField { font-size: 13px; color: @text@; }
QFrame#Swatch { background: transparent; border: 1.5px solid transparent; border-radius: 11px; }
QFrame#Swatch[checked="true"] { border: 1.5px solid @accent@; }
QFrame#SegTrack, QWidget#SegTrack { background: @surface_hi@; border-radius: 9px; }
QPushButton#SegTab {
    background: transparent; border: none; border-radius: 7px;
    padding: 4px 14px; font-size: 12.5px; color: @muted@;
}
QPushButton#SegTab:checked { background: @bg_alt@; color: @accent@; font-weight: 600; }
QFrame#PreviewCard { background: @bg@; border: 1px solid @border@; border-radius: 10px; }
QCheckBox#CheckRow { font-size: 12.5px; color: @text@; }

/* 弹层菜单（清单 / 优先级 / 排序 / 右键） */
QFrame#TickMenu { background: transparent; }
QFrame#TickMenuCard {
    background: @surface@; border: 1px solid @border@; border-radius: 10px;
}
QFrame#TickMenuRow { background: transparent; border: none; border-radius: 7px; }
QFrame#TickMenuRow[hover="true"] { background: @surface_hi@; }
QLabel#TickMenuLabel { font-size: 13px; color: @text@; background: transparent; }
QLabel#MenuSection { font-size: 11px; color: @muted@; font-weight: 600;
    padding: 2px 8px 4px; background: transparent; }
QLabel#TickMenuLabel[danger="true"] { color: @red@; }
/* ⋯ 菜单「视图」那一格的三档 */
QPushButton#ViewBtn {
    background: transparent; border: 1px solid transparent; border-radius: 7px;
}
QPushButton#ViewBtn:hover { background: @surface_hi@; }
QPushButton#ViewBtn[active="true"] { background: @accent_soft@; }
QPushButton#ViewBtn:disabled { background: transparent; border: none; }
QFrame#InsertLine { background: @accent@; border: none; border-radius: 1px; }

QLabel#StatValue { font-size: 26px; font-weight: 800; color: @text_hi@; }
QLabel#StatLabel { font-size: 12px; color: @muted@; }
QLabel#PageTitle { font-size: 24px; font-weight: 800; color: @text_hi@; }
QLabel#PageSubtitle { font-size: 13px; color: @muted@; }

/* 强调色文字（动态属性） */
QLabel[statColor="accent"] { color: @accent@; }
QLabel[statColor="green"]  { color: @green@; }
QLabel[statColor="red"]    { color: @red@; }
QLabel[statColor="amber"]  { color: @amber@; }
QLabel[statColor="blue"]   { color: @blue@; }

/* 金额符号 */
QLabel[moneySign="income"]  { color: @green@; font-weight: 700; }
QLabel[moneySign="expense"] { color: @red@; font-weight: 700; }

/* 标签（tagColor 动态属性） */
QLabel#Tag {
    border-radius: 8px; padding: 2px 9px;
    font-size: 11px; font-weight: 600;
}
QLabel#Tag[tagColor="accent"] { background: @accent_soft@; color: @accent@; }
QLabel#Tag[tagColor="green"]  { background: @green_soft@;  color: @green@; }
QLabel#Tag[tagColor="red"]    { background: @red_soft@;    color: @red@; }
QLabel#Tag[tagColor="amber"]  { background: @amber_soft@;  color: @amber@; }
QLabel#Tag[tagColor="blue"]   { background: @blue_soft@;   color: @blue@; }
QLabel#Tag[tagColor="muted"]  { background: @surface_hi@;  color: @muted@; }

/* ---------- 输入控件 ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {
    background: @bg@; border: 1px solid @border@; border-radius: 9px;
    padding: 8px 10px; color: @text@;
    selection-background-color: @accent@;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid @accent@; }
QComboBox::drop-down {
    subcontrol-origin: border; subcontrol-position: center right;
    border: none; width: 24px; background: transparent;
}
QDateEdit::drop-down {
    subcontrol-origin: border; subcontrol-position: center right;
    border: none; width: 26px; background: transparent;
}
QComboBox QAbstractItemView {
    background: @surface@; border: 1px solid @border@; border-radius: 9px;
    selection-background-color: @accent_soft@; selection-color: @accent@;
    color: @text@;
}
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button {
    border: none; width: 20px; background: transparent;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QTimeEdit::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover, QTimeEdit::down-button:hover {
    background: @surface_hi@;
}

/* ---------- 按钮 ---------- */
QPushButton#Primary {
    background: @accent@; color: white; border: none; border-radius: 9px;
    padding: 9px 18px; font-weight: 600;
}
QPushButton#Primary:hover { background: @accent_hi@; }
QPushButton#Primary:disabled { background: @accent_soft@; color: @muted@; }
QPushButton#Primary:pressed { background: @accent@; }

QPushButton#Ghost {
    background: transparent; color: @text@;
    border: 1px solid @border_strong@; border-radius: 9px; padding: 9px 18px;
}
QPushButton#Ghost:hover { border-color: @accent@; color: @accent@; }

QPushButton#Danger {
    background: transparent; color: @red@;
    border: 1px solid @red@; border-radius: 9px; padding: 9px 18px;
}
QPushButton#Danger:hover { background: @red@; color: white; }

QPushButton#IconBtn {
    background: transparent; border: none; border-radius: 8px;
    padding: 5px 9px; color: @muted@;
}
QPushButton#IconBtn:hover { background: @red_soft@; color: @red@; }

/* ---------- 列表（滴答清单风格：行式 hover） ---------- */
QListWidget { background: transparent; border: none; }
QListWidget::item {
    background: transparent; border: none;
    border-radius: 8px; margin: 0; padding: 1px;
}
QListWidget::item:hover { background: @surface_hi@; }
QListWidget::item:selected { background: @accent_soft@; border: none; }

/* 待办任务行 */
QFrame#TodoRow { background: transparent; border: none; border-radius: 8px; }
QFrame#TodoRow:hover { background: @surface_hi@; }
QFrame#TodoRow[dragging="true"] { background: @accent_soft@; }
QFrame#TodoRow[selected="true"] { background: @accent_soft@; }
QLabel#TodoTitle { font-size: 13.5px; font-weight: 400; color: @text@; background: transparent; }
QLabel#TodoTitle[done="true"] { color: @muted@; }
/* 就地改名要和标签长得一模一样：有边框/内边距的话，点下去字会往右跳 7px，
   这一下跳动就是「不够无感」的来源。滴答也是无边框、只靠光标。 */
QLineEdit#TodoRename {
    background: transparent; border: none; padding: 0;
    font-size: 13.5px; font-weight: 400; color: @text@;
}
QLabel#TodoList { font-size: 12px; color: @muted@; background: transparent; }
QLabel#TodoDate { font-size: 12px; color: @muted@; background: transparent; }
QLabel#TodoDate[dateState="overdue"] { color: @red@; }
QLabel#TodoDate[dateState="today"] { color: @accent@; }
QLabel#TodoSub { font-size: 12px; color: @muted@; background: transparent; }
QLabel#TodoSub[subState="full"] { color: @green@; }
/* ⋯ 菜单「显示详细 / 显示检查事项」打开后，标题下面的附加行 */
QLabel#TodoNotePreview { font-size: 12px; color: @muted@; background: transparent; }
QLabel#TodoListLine { font-size: 12px; color: @muted@; background: transparent; }
QFrame#RowSub { background: transparent; border: none; }
QLabel#RowSubText { font-size: 12.5px; color: @text@; background: transparent; }
QLabel#RowSubText[done="true"] { color: @muted@; }
/* 看板视图：列只是排布的槽位（不画底），卡片才有边框 */
QScrollArea#BoardScroll { background: transparent; border: none; }
QScrollArea#BoardScroll > QWidget > QWidget { background: transparent; }
QFrame#BoardColumn { background: transparent; border: none; }
QFrame#TodoRow[card="true"] {
    background: @surface@; border: 1px solid @border@; border-radius: 10px;
}
QFrame#TodoRow[card="true"]:hover { background: @surface@; border-color: @border_strong@; }
QFrame#TodoRow[card="true"][selected="true"] { background: @accent_soft@; border-color: @accent@; }

/* 四象限页：2×2 四格，格子里再按日期分组 */
QScrollArea#QuadScroll { background: transparent; border: none; }
QScrollArea#QuadScroll > QWidget > QWidget { background: transparent; }
QFrame#QuadCard { background: @surface@; border: 1px solid @border@; border-radius: 12px; }
QLabel#QuadBadge { color: #ffffff; border: none; border-radius: 9px;
    font-size: 10px; font-weight: 700; }
QLabel#QuadBadge[tint="red"] { background: @red@; }
QLabel#QuadBadge[tint="amber"] { background: @amber@; }
QLabel#QuadBadge[tint="blue"] { background: @blue@; }
QLabel#QuadBadge[tint="green"] { background: @green@; }
QLabel#QuadTitle { font-size: 13px; font-weight: 700; background: transparent; }
QLabel#QuadTitle[tint="red"] { color: @red@; }
QLabel#QuadTitle[tint="amber"] { color: @amber@; }
QLabel#QuadTitle[tint="blue"] { color: @blue@; }
QLabel#QuadTitle[tint="green"] { color: @green@; }
QLabel#QuadEmpty { font-size: 13px; color: @muted@; background: transparent;
    min-height: 220px; }
/* 往别的象限上拖 = 改优先级，悬停的那格要点亮，不然不知道能往哪儿放 */
QFrame#QuadCard[drop="true"] { border: 2px dashed @accent@; background: @accent_soft@; }

/* 分组标题行 */
QFrame#GroupHeader { background: transparent; border: none; border-radius: 8px; }
QFrame#GroupHeader:hover { background: @surface_hi@; }
QLabel#GroupTitle { font-size: 12.5px; font-weight: 700; color: @text_hi@; }
QLabel#GroupCount { font-size: 12.5px; color: @muted@; font-weight: 600; }
QLabel#GroupWarn { font-size: 12.5px; color: @red@; font-weight: 700; }
QPushButton#LinkBtn {
    background: transparent; border: none; color: @accent@;
    font-size: 12px; padding: 2px 6px;
}
QPushButton#LinkBtn:hover { color: @accent_hi@; text-decoration: underline; }

/* 子任务行 */
QLabel#SubTaskTitle { font-size: 13px; color: @text@; background: transparent; }
QLabel#SubTaskTitle[done="true"] { color: @muted@; }

/* 标签 chip（详情面板） */
QPushButton#TagAddBtn {
    background: transparent; border: 1px dashed @border_strong@;
    border-radius: 11px; padding: 3px 10px; font-size: 12px; color: @muted@;
}
QPushButton#TagAddBtn:hover { border-color: @accent@; color: @accent@; }
QPushButton#TagChip {
    background: transparent; border: 1px solid @border_strong@;
    border-radius: 11px; padding: 3px 10px; font-size: 12px; color: @muted@;
}
QPushButton#TagChip:hover { border-color: @accent@; color: @accent@; background: @accent_soft@; }
QPushButton#TagChip:pressed { background: @accent@; color: #ffffff; border-color: @accent@; }
QPushButton#TagChip[tagColor="accent"]:checked { background: @accent_soft@; color: @accent@; border-color: @accent@; font-weight: 600; }
QPushButton#TagChip[tagColor="green"]:checked  { background: @green_soft@;  color: @green@;  border-color: @green@;  font-weight: 600; }
QPushButton#TagChip[tagColor="red"]:checked    { background: @red_soft@;    color: @red@;    border-color: @red@;    font-weight: 600; }
QPushButton#TagChip[tagColor="amber"]:checked  { background: @amber_soft@;  color: @amber@;  border-color: @amber@;  font-weight: 600; }
QPushButton#TagChip[tagColor="blue"]:checked   { background: @blue_soft@;   color: @blue@;   border-color: @blue@;   font-weight: 600; }
QPushButton#TagChip[tagColor="muted"]:checked  { background: @surface_hi@;  color: @muted@;  border-color: @muted@;  font-weight: 600; }

/* 详情面板滚动区 */
QFrame#DetailPane QScrollArea { background: transparent; border: none; }
QFrame#DetailPane QScrollArea > QWidget > QWidget { background: transparent; }

/* 分组标题行（字号/字色见上面「待办」一节：这里以前还重复声明过一次，
   同权重的后一条会赢，把组标题刷成灰色，和滴答的近黑加粗对不上） */
QFrame#GroupHeader { background: transparent; border: none; border-radius: 8px; }
QFrame#GroupHeader:hover { background: @surface_hi@; }

QTableWidget, QTableView {
    background: @surface@; border: 1px solid @border@; border-radius: 12px;
    gridline-color: @border@; alternate-background-color: @bg@; color: @text@;
}
QTableWidget::item { padding: 6px; }
QHeaderView::section {
    background: @bg_alt@; color: @muted@; border: none;
    border-bottom: 1px solid @border@; padding: 8px; font-weight: 600;
}

/* ---------- 滚动区域：默认透明，融入所在卡片 ---------- */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* ---------- 滚动条 ---------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @border_strong@; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: @muted@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: @border_strong@; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ---------- 进度条 / 选项卡 / 复选 ---------- */
QProgressBar {
    background: @surface_hi@; border: none; border-radius: 6px;
    height: 10px; text-align: center;
}
QProgressBar::chunk { background: @accent@; border-radius: 6px; }
QProgressBar[over="true"]::chunk { background: @red@; border-radius: 6px; }
QProgressBar[barColor="green"]::chunk { background: @green@; border-radius: 6px; }

/* 体重管理（华为风格） */
QLabel#BigNum { font-size: 42px; font-weight: 800; color: @text_hi@; }
QFrame#InnerCard { background: @bg@; border-radius: 12px; }

QTabWidget::pane { border: 1px solid @border@; border-radius: 10px; top: -1px; }
QTabBar::tab { background: transparent; color: @muted@; padding: 8px 16px; border: none; }
QTabBar::tab:selected { color: @accent@; border-bottom: 2px solid @accent@; font-weight: 600; }

QCheckBox { spacing: 8px; color: @text@; }
QCheckBox::indicator {
    width: 18px; height: 18px; border: 1px solid @border_strong@;
    border-radius: 5px; background: @bg_alt@;
}
QCheckBox::indicator:hover { border-color: @accent@; }
QCheckBox::indicator:checked {
    background: @accent@; border-color: @accent@;
    image: url("@check_img@");
}

/* ---------- 体重管理（打卡风格） ---------- */
QFrame#HeroCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 @hero_top@, stop:1 @hero_bottom@);
    border: 1px solid @border@;
    border-radius: 16px;
}
QFrame#HeroInner {
    background: @surface@;
    border-radius: 12px;
}
QLabel#HeroWeight { font-size: 46px; font-weight: 800; color: @text_hi@; }
QLabel#HeroUnit   { font-size: 14px; color: @muted@; }
QLabel#HeroMetricName { font-size: 13px; color: @muted@; }
QLabel#HeroMetricVal  { font-size: 20px; font-weight: 700; color: @text_hi@; }
QLabel#HeroCompare { font-size: 13px; color: @muted@; }
QLabel#GoalLabel   { font-size: 12px; color: @muted@; }
QLabel#GoalNum     { font-size: 20px; font-weight: 800; color: @text_hi@; }
QFrame#HeroCard QFrame#hline {
    background: @border@; border: none; max-height: 1px;
}

/* ---------- 理财管理：流水行 / 分组头 / 账户卡 ---------- */
QFrame#LedgerRow { background: transparent; border: none; border-radius: 10px; }
QFrame#LedgerRow:hover { background: @surface_hi@; }
QLabel#RowTitle { font-size: 13.5px; font-weight: 600; color: @text_hi@; }
QLabel#HeroBig { font-size: 34px; font-weight: 800; color: @text_hi@; }
QLabel#HeroCaption { font-size: 12.5px; color: @muted@; }
QToolButton#CatChip {
    background: transparent; border: 1px solid @border@; border-radius: 10px;
    padding: 6px 2px; color: @muted@; font-size: 12px;
}
QToolButton#CatChip:hover { border-color: @accent@; color: @text@; }
QToolButton#CatChip:checked {
    background: @accent_soft@; border-color: @accent@;
    color: @accent@; font-weight: 700;
}
QToolButton#IconPick {
    background: transparent; border: 2px solid transparent; border-radius: 10px;
    padding: 4px;
}
QToolButton#IconPick:hover { background: @surface_hi@; }
QToolButton#IconPick:checked {
    background: @accent_soft@; border-color: @accent@;
}
QFrame#DayHeader { background: transparent; border: none; }
QLabel#DayTitle { font-size: 12.5px; font-weight: 700; color: @text_hi@; }
QLabel#DaySum { font-size: 11.5px; color: @muted@; }
QLabel#Money { font-size: 14px; font-weight: 700; }
QLabel#Money[sign="income"]  { color: @green@; }
QLabel#Money[sign="expense"] { color: @red@; }
QLabel#Money[sign="flat"]    { color: @text_hi@; }
QLabel#Hint { font-size: 12px; color: @muted@; }
QLabel#Hint[level="error"] { color: @red@; font-weight: 600; }
QLabel#Hint[level="ok"]    { color: @green@; }
QPushButton#LinkBtn {
    background: transparent; border: none; border-radius: 6px;
    padding: 4px 6px; color: @accent@; font-size: 12px;
}
QPushButton#LinkBtn:hover { color: @accent_hi@; background: @accent_soft@; }
QPushButton#LinkBtn[danger="true"] { color: @red@; }
QPushButton#LinkBtn[danger="true"]:hover { background: @red_soft@; color: @red@; }
QFrame#AccountCard {
    background: @bg@; border: 1px solid @border@; border-radius: 12px;
}
QFrame#AccountCard:hover { border-color: @accent@; }
QLabel#AccountName { font-size: 12.5px; font-weight: 600; color: @muted@; }
QLabel#AccountBalance { font-size: 18px; font-weight: 800; color: @text_hi@; }

/* ---------- 快速添加：识别高亮 + 日期 chip ---------- */
QPushButton#DateChip {
    background: transparent; border: none; border-radius: 8px;
    padding: 4px 10px; color: @accent@; font-size: 12.5px; font-weight: 600;
}
QPushButton#DateChip:hover { background: @accent_soft@; }

/* ---------- 日期选择弹窗 ---------- */
/* 根节点是 WA_TranslucentBackground 的顶层窗口，QSS 背景画不上去（实测
   grab 出来整张 alpha=0，页面文字直接透出来）。所以根节点保持透明，
   底色 / 边框 / 圆角垫在内层 card 上 —— 和 TickMenuCard 同一个套路。 */
QFrame#DatePickerPopup { background: transparent; border: none; }
QFrame#DatePopupCard {
    background: @surface@;
    border: 1px solid @border@;
    border-radius: 12px;
}
QWidget#PopupTabTrack { background: @surface_hi@; border-radius: 9px; }
QPushButton#PopupTab {
    background: transparent; border: 1px solid transparent; border-radius: 7px;
    padding: 6px 0px; color: @muted@; font-size: 12.5px; font-weight: 600;
}
QPushButton#PopupTab:checked {
    background: @surface@; border-color: @border@;
    color: @accent@; font-weight: 700;
}
QPushButton#QuickBtn {
    background: transparent; border: none; border-radius: 10px;
    padding: 4px 10px; color: @text@; font-size: 14px;
}
QPushButton#QuickBtn:hover { background: @accent_soft@; }
QLabel#CalHeader { font-size: 13.5px; font-weight: 700; color: @text_hi@; }
QPushButton#CalNav {
    background: transparent; border: none; border-radius: 7px;
    min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px;
    color: @muted@; font-size: 14px; padding: 0;
}
QPushButton#CalNav:hover { background: @surface_hi@; color: @text@; }
QLabel#CalWd { color: @muted@; font-size: 11.5px; }
QPushButton#CalDay {
    background: transparent; border: none; border-radius: 16px;
    min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px;
    color: @text@; font-size: 12.5px; padding: 0;
}
QPushButton#CalDay:hover { background: @surface_hi@; }
QPushButton#CalDay[calState="off"] { color: @border_strong@; }
QPushButton#CalDay[calState="today"] { background: transparent; color: @accent@; font-weight: 700; }
/* 过去的空日子：浅灰圆底（滴答把「已经过去的每一天」都画成灰圆） */
QPushButton#CalDay[calState="past"] { background: @surface_hi@; color: @text@; }
QPushButton#CalDay[calState="past"]:hover { background: @border@; }
QPushButton#CalDay[calState="sel"] { background: @accent@; color: #ffffff; font-weight: 700; }
QPushButton#PopupRow {
    background: transparent; border: none; border-radius: 8px;
    padding: 7px 6px; text-align: left; font-size: 13px;
}
QPushButton#PopupRow[rowColor="accent"] { color: @accent@; font-weight: 600; }
QPushButton#PopupRow[rowColor="muted"] { color: @text@; }
QPushButton#PopupRow:hover { background: @surface_hi@; }
QLabel#PopupRowValue { color: @muted@; font-size: 12.5px; background: transparent; }
QListWidget#TimeList {
    background: @bg@; border: 1px solid @border@; border-radius: 8px;
}
QListWidget#TimeList::item { min-height: 26px; padding: 0 10px; border-radius: 6px; }
QListWidget#TimeList::item:selected { background: @accent_soft@; color: @accent@; }

QToolTip {
    background: @surface@; color: @text@;
    border: 1px solid @border@; padding: 6px;
}

/* ---------- 番茄钟：概览面板 + 专注记录 ---------- */
QLabel#OverviewTitle { font-size: 15px; font-weight: 800; color: @text_hi@; }
QFrame#OverviewTile {
    background: @surface_hi@; border: none; border-radius: 10px;
}
QFrame#OverviewTile QLabel { background: transparent; border: none; }
QLabel#OverviewTileLabel { font-size: 12px; color: @muted@; }
QLabel#OverviewTileValue { font-size: 19px; font-weight: 800; color: @text_hi@; }
QLabel#RecordDate {
    font-size: 12px; color: @muted@; font-weight: 600;
    padding-top: 8px;
}
QLabel#RecordTime { font-size: 11.5px; color: @muted@; }
QLabel#RecordTask { font-size: 13px; font-weight: 600; color: @text_hi@; }
QLabel#RecordDur { font-size: 11.5px; color: @muted@; }
QPushButton#RoundBtn {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 12px;
    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
    color: @muted@; font-size: 15px; padding: 0;
}
QPushButton#RoundBtn:hover {
    border-color: @accent@; color: @accent@; background: @accent_soft@;
}

/* ---------- 番茄钟：任务选择 / 设置 ---------- */
/* 滴答的分段切换是两枚无边框实心药丸：选中浅蓝底蓝字，未选浅灰底灰字 */
QPushButton#SegBtn {
    background: @surface_hi@; color: @muted@;
    border: none; border-radius: 9px;
    padding: 7px 18px; font-size: 13px; font-weight: 600;
}
QPushButton#SegBtn:hover { color: @text@; background: @border@; }
QPushButton#SegBtn:checked { background: @accent_soft@; color: @accent@; }
QPushButton#SettingsBtn {
    background: transparent; border: none; border-radius: 10px;
    min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
    color: @muted@; font-size: 15px; padding: 0;
}
QPushButton#SettingsBtn:hover {
    color: @accent@; background: @surface_hi@;
}
QLabel#TaskSelector {
    color: @text@; font-size: 14px; font-weight: 600;
    padding: 6px 14px; border: 1px solid @border@; border-radius: 8px;
}
QLabel#TaskSelector:hover { border-color: @accent@; color: @accent@; background: @accent_soft@; }
QLabel#GoalLabel { font-size: 12px; color: @muted@; }
QLabel#GoalLabel:hover { color: @accent@; }
QPushButton#StartBtn {
    background: @accent@; color: white; border: none; border-radius: 26px;
    font-size: 16px; font-weight: 700;
}
QPushButton#StartBtn:hover { background: @accent_hi@; }

/* 任务选择弹窗 */
QFrame#PickerCard {
    background: @surface@; border: 1px solid @border@; border-radius: 14px;
}
QPushButton#PickerTab {
    background: @surface_hi@; color: @muted@;
    border: none; border-radius: 13px;
    padding: 4px 15px; font-size: 13px; font-weight: 600;
}
QPushButton#PickerTab:hover { color: @text@; }
/* 注意：Picker* 这一组样式只被 pages/pomodoro.py 使用（任务选择弹窗 +
   常用专注弹窗 + 补录弹窗），所以选中态用专注模块的 focus 蓝，
   不要用全局 accent 紫 —— 弹窗从专注页弹出，混进紫色和整页蓝调打架。
   参考设计里两个 tab 都有底色：选中淡蓝，未选中浅灰。 */
QPushButton#PickerTab:checked { background: @focus_soft@; color: @focus@; }
QPushButton#PickerClose {
    background: transparent; border: none; border-radius: 8px;
    min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
    color: @muted@; font-size: 14px; padding: 0;
}
QPushButton#PickerClose:hover { background: @surface_hi@; color: @text@; }
QFrame#PickerSearchBox {
    background: @surface_hi@; border: none; border-radius: 8px;
}
QLineEdit#PickerSearchInner {
    background: transparent; border: none; padding: 7px 0; font-size: 13px;
}
QComboBox#PickerDateCombo {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 8px;
    padding: 4px 8px; font-size: 12px;
}
QLabel#PickerGroup { font-size: 11px; color: @muted@; font-weight: 600; padding: 6px 4px 2px; }
QFrame#PickerRow { background: transparent; border-radius: 6px; }
QFrame#PickerRow:hover { background: @surface_hi@; }
QLabel#FavChip {
    background: @surface_hi@; border: none; border-radius: 9px; font-size: 17px;
}
QPushButton#PickerAction {
    background: transparent; border: none; border-radius: 8px;
    padding: 6px 10px; color: @focus@; font-size: 13px; font-weight: 600;
    text-align: left;
}
QPushButton#PickerAction:hover { background: @surface_hi@; }
QFrame#FocusSep { background: @border@; border: none; }
QLabel#PickerCircle { color: @border_strong@; font-size: 13px; }
QLabel#PickerTitle { font-size: 13px; color: @text_hi@; }
QLabel#PickerDate { font-size: 11px; color: @muted@; }
/* 逾期日期标红（参考设计里「已过期」分组的日期是红的） */
QLabel#PickerDate[overdue="true"] { color: @red@; }

/* 补录弹窗：左侧表单标签 + 右侧下拉字段 */
QLabel#FormLabel { font-size: 13px; color: @text@; }
QFrame#PickerField {
    background: @surface@; border: 1px solid @border@; border-radius: 6px;
}
QFrame#PickerField:hover { border-color: @border_strong@; }
QFrame#PickerField[active="true"] { border-color: @focus@; }
QLabel#PickerFieldText { font-size: 13px; color: @text@; background: transparent; }
QLabel#PickerFieldText[placeholder="true"] { color: @muted@; }
QTextEdit#PickerNote {
    background: @surface_hi@; border: none; border-radius: 8px;
    padding: 8px 10px; font-size: 13px; color: @text@;
}
/* 日期/时间选择弹层 */
QFrame#CalPopup { background: @surface@; border: 1px solid @border@; border-radius: 12px; }
QLabel#CalMonth { font-size: 13px; color: @text_hi@; font-weight: 600; }
QPushButton#CalNav {
    background: transparent; border: none; border-radius: 6px;
    color: @muted@; font-size: 13px; padding: 0;
    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
}
QPushButton#CalNav:hover { background: @surface_hi@; color: @text@; }
QLabel#CalWeek { font-size: 11px; color: @muted@; }
QPushButton#CalDay {
    background: transparent; border: none; border-radius: 14px;
    color: @text@; font-size: 12px; padding: 0;
    min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
}
QPushButton#CalDay:hover { background: @surface_hi@; }
QPushButton#CalDay[muted="true"] { color: @muted@; }
QPushButton#CalDay:checked { background: @focus@; color: #ffffff; font-weight: 700; }
QLineEdit#CalTime {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 6px;
    padding: 6px 8px; font-size: 13px;
}
QLineEdit#CalTime:focus { border-color: @focus@; }

/* 设置弹窗 */
QLabel#SettingsTitle { font-size: 16px; font-weight: 800; color: @text_hi@; }
QFrame#SettingsSep { background: @border@; }
QLabel#SettingsSection { font-size: 13px; font-weight: 700; color: @text_hi@; padding-top: 10px; }
QLabel#Muted { color: @muted@; }
QFrame#MiniThumb { border: 1px solid @border@; border-radius: 10px; }
QFrame#MiniThumbSel { border: 2px solid @accent@; border-radius: 10px; }

/* 专注记录行 */
QFrame#RecordRow { background: transparent; border-radius: 8px; }
QFrame#RecordRow:hover { background: @surface_hi@; }
/* 设置弹窗：分区卡片 + 文字式下拉 */
QFrame#SettingsCard { background: @surface_hi@; border: none; border-radius: 12px; }
QFrame#SettingsCard QLabel { background: transparent; border: none; }
QFrame#SettingsCard QSpinBox, QFrame#SettingsCard QDoubleSpinBox {
    background: @bg@; border: 1px solid @border@; border-radius: 8px;
    padding: 3px 8px; font-size: 13px; color: @text@;
}
QComboBox#TextCombo {
    background: transparent; border: none; color: @text@;
    font-size: 13px; padding: 4px 18px 4px 0;
}
QComboBox#TextCombo:hover { color: @accent@; }
QComboBox#TextCombo::drop-down { border: none; width: 18px; }
QComboBox#TextCombo QAbstractItemView {
    background: @surface@; border: 1px solid @border@; border-radius: 8px;
    selection-background-color: @accent_soft@; selection-color: @accent@;
}
/* 专注记录时间线 */
QFrame#RecordTimeline { background: @border@; border: none; max-width: 1px; }
QLabel#RecordTaskDot { color: @muted@; font-size: 11px; }
/* 添加常用专注弹窗 */
QPushButton#EmojiCell {
    background: transparent; border: none; border-radius: 8px; font-size: 24px;
}
QPushButton#EmojiCell:hover { background: @surface_hi@; }
QLabel#EmojiAvatar { font-size: 26px; background: transparent; border: none; }
QFrame#AvatarRing {
    background: #7ac70c; border: none; border-radius: 26px;
}
QFrame#AvatarRing QLabel { background: transparent; }
QPushButton#GhostBtn {
    background: transparent; border: 1px solid @border@; border-radius: 9px;
    padding: 8px 20px; color: @text@; font-size: 13px;
}
QPushButton#GhostBtn:hover { border-color: @accent@; color: @accent@; }
QPushButton#PrimaryBtn {
    background: @accent@; color: white; border: none; border-radius: 9px;
    padding: 8px 24px; font-size: 13px; font-weight: 600;
}
QPushButton#PrimaryBtn:disabled { background: @accent_soft@; color: @muted@; }
QPushButton#PrimaryBtn:hover { background: @accent_hi@; }

/* ---------- 专注模块（番茄钟 / 统计页） ---------- */
QLabel#FocusTaskSelector {
    color: @muted@; font-size: 13.5px; font-weight: 500;
    padding: 4px 6px; background: transparent; border: none;
}
QLabel#FocusTaskSelector:hover { color: @focus@; }
QPushButton#FocusStart {
    background: @focus@; color: #ffffff; border: none; border-radius: 23px;
    font-size: 15px; font-weight: 600;
}
QPushButton#FocusStart:hover { background: @focus_hi@; }
QPushButton#FocusStart:pressed { background: @focus@; }

QPushButton#FocusIconBtn {
    background: transparent; border: none; border-radius: 8px;
    color: @muted@; font-size: 17px; padding: 0;
}
QPushButton#FocusIconBtn:hover { background: @surface_hi@; color: @text@; }

QFrame#FocusSide {
    background: @bg_alt@;
    border-left: 1px solid @border@;
}
/* 概览栏拖动分隔条：平时透明，悬停/拖动时给出抓取提示 */
QSplitter#FocusSplit::handle:horizontal {
    background: transparent;
    width: 8px;
    margin: 0px;
}
QSplitter#FocusSplit::handle:horizontal:hover { background: @accent_soft@; }
QSplitter#FocusSplit::handle:horizontal:pressed { background: @accent_soft@; }
QLabel#FocusSideTitle { font-size: 15px; font-weight: 700; color: @text_hi@; }
QFrame#FocusTile {
    background: @surface_hi@; border: none; border-radius: 8px;
}
QFrame#FocusTile QLabel { background: transparent; border: none; }
QLabel#FocusTileLabel { font-size: 12px; color: @muted@; }
QLabel#FocusTileValue { font-size: 26px; font-weight: 700; color: @text_hi@; }

QFrame#FocusCard {
    background: @surface@; border: 1px solid @border@; border-radius: 12px;
}
QFrame#FocusCard QLabel { background: transparent; }
QLabel#FocusCardTitle { font-size: 14px; font-weight: 700; color: @text_hi@; }
/* 卡片内的说明文字用次要灰，不要用 @amber@ —— 专注页整体是蓝色调，
   橙色提示会显得突兀，和旁边的「每日平均」等灰色说明也不一致。 */
QLabel#FocusCardHint { font-size: 11px; color: @muted@; }
QLabel#FocusMuted { font-size: 11.5px; color: @muted@; }
QLabel#FocusNavText { font-size: 12.5px; color: @text@; font-weight: 600; }
QLabel#FocusSub { font-size: 11.5px; color: @muted@; }
QLabel#FocusSubUp { font-size: 11.5px; color: @green@; }
QLabel#FocusSubDown { font-size: 11.5px; color: @red@; }
/* 统计页「任务」标签顶部工具条：白色胶囊（粒度下拉 + 周期切换） */
QWidget#FocusPill {
    background: @surface@; border: 1px solid @border@; border-radius: 14px;
}
QWidget#FocusPill QLabel#FocusNavText { font-size: 12.5px; }
QComboBox#FocusPillCombo {
    background: @surface@; border: 1px solid @border@; border-radius: 14px;
    color: @text@; font-size: 12.5px; padding: 3px 22px 3px 12px;
}
QComboBox#FocusPillCombo:hover { border-color: @border_strong@; }
QComboBox#FocusPillCombo::drop-down { border: none; width: 20px; }
QComboBox#FocusPillCombo QAbstractItemView {
    background: @surface@; border: 1px solid @border@; border-radius: 8px;
    selection-background-color: @focus_soft@; selection-color: @focus@;
    padding: 4px; outline: none;
}
QLabel#FocusBigValue { font-size: 22px; font-weight: 700; color: @focus@; }
QLabel#FocusLabel { font-size: 12px; color: @muted@; }

QFrame#FocusMenu { background: transparent; border: none; }
QFrame#FocusMenuCard {
    background: @surface@; border: 1px solid @border@; border-radius: 10px;
}
QFrame#FocusMenuRow { background: transparent; border: none; border-radius: 7px; }
QFrame#FocusMenuRow[hover="true"] { background: @surface_hi@; }
QLabel#FocusMenuLabel { font-size: 13px; color: @text@; }
QLabel#FocusMenuLabel[danger="true"] { color: @red@; }

QFrame#FocusRecordRow { background: transparent; border: none; border-radius: 8px; }
QFrame#FocusRecordRow:hover { background: @surface_hi@; }
QLabel#FocusRecordDate {
    font-size: 12px; color: @muted@; font-weight: 600; padding: 10px 0 4px;
}
QLabel#FocusRecordTime { font-size: 11.5px; color: @muted@; }
QLabel#FocusRecordTask { font-size: 13px; color: @text_hi@; }
QLabel#FocusRecordDur { font-size: 11.5px; color: @muted@; }
QLabel#FocusRecordDot { font-size: 11px; color: @muted@; }
QFrame#FocusRail { background: @border@; border: none; max-width: 1px; }

QPushButton#FocusPrimary {
    background: @focus@; color: #ffffff; border: none; border-radius: 8px;
    padding: 7px 22px; font-size: 13px; font-weight: 600;
}
QPushButton#FocusPrimary:hover { background: @focus_hi@; }
QPushButton#FocusPrimary:disabled { background: @focus_soft@; color: @muted@; }
QPushButton#FocusGhost {
    background: @bg_alt@; border: 1px solid @border_strong@; border-radius: 8px;
    padding: 7px 22px; color: @text@; font-size: 13px;
}
QPushButton#FocusGhost:hover { border-color: @focus@; color: @focus@; }
QPushButton#FocusLink {
    background: transparent; border: none; border-radius: 6px;
    padding: 4px 8px; color: @muted@; font-size: 12px;
}
QPushButton#FocusLink:hover { background: @surface_hi@; color: @focus@; }
QPushButton#FocusLink:checked { color: @focus@; font-weight: 600; }

QRadioButton { color: @text@; font-size: 13px; spacing: 8px; }
/* 「圆环 + 中心圆点」由 focus_ui.RoundRadio 自绘，不用 ::indicator。
   原因：选中态若用粗 border 表达（border:5px + border-radius:8px，16px 的框），
   Qt 会把圆角渲染成圆角方块 —— 实测同尺寸下 border:1.5px 是正圆、
   换成 border:5px 就退化成方块。 */

QFrame#FocusAvatar { background: #8ccf4d; border: none; border-radius: 24px; }
QLabel#FocusAvatarEmoji { font-size: 24px; background: transparent; border: none; }
QFrame#FocusInputBox {
    background: @bg_alt@; border: 1px solid @border_strong@; border-radius: 8px;
}
QFrame#FocusInputBox[focused="true"] { border-color: @focus@; }
QLineEdit#FocusNameInput {
    background: transparent; border: none; font-size: 14px; color: @text@;
    padding: 0 2px;
}
QLabel#FocusInputAction {
    color: @muted@; font-size: 15px; padding: 0 4px; background: transparent;
}
QLabel#FocusInputAction:hover { color: @focus@; }

/* 不要写成 QFrame#FocusPage —— StatsView 是 QWidget，选择器带类型限定就不匹配，
   覆盖层会变成透明（浅色模式下恰好看不出来，深色模式才暴露）。 */
#FocusPage { background: @bg@; }
QLabel#FocusStatsTitle { font-size: 21px; font-weight: 800; color: @text_hi@; }
QLabel#FocusKpiValue { font-size: 22px; font-weight: 700; color: @focus@; }
QLabel#FocusKpiLabel { font-size: 12px; color: @muted@; }
QLabel#FocusKpiSub { font-size: 11px; color: @muted@; }
QLabel#FocusLegend { font-size: 10px; color: @muted@; }

/* ---------- 通用弹层（lifeapp/popups.py） ---------- */
QFrame#AppPopupShell { background: transparent; }
QFrame#AppPopup {
    background: @surface@; border: 1px solid @border@; border-radius: 12px;
}
QFrame#AppPopup QLabel { background: transparent; }
QLabel#PopupCap { font-size: 12px; color: @muted@; }
QLabel#ConfirmText, QLabel#NotifyText { font-size: 13px; color: @text@; }
QLabel#NotifyText[danger="true"] { color: @red@; }
QLineEdit#PopupEdit, QPlainTextEdit#PopupEdit {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 8px;
    padding: 7px 10px; font-size: 13px; color: @text@;
}
QSpinBox#PopupEdit, QDoubleSpinBox#PopupEdit {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 8px;
    padding: 6px 8px; font-size: 13px; color: @text@;
}
QLineEdit#PopupEdit:focus, QPlainTextEdit#PopupEdit:focus,
QSpinBox#PopupEdit:focus, QDoubleSpinBox#PopupEdit:focus {
    border-color: @accent@; background: @surface@;
}
QPlainTextEdit#PopupNote {
    background: @surface_hi@; border: 1px solid @border@; border-radius: 8px;
    padding: 7px 9px; font-size: 13px; color: @text@;
}
QPlainTextEdit#PopupNote:focus { border-color: @accent@; background: @surface@; }
QScrollArea#AppPopup * { background: transparent; }

/* ---------- 习惯打卡 ---------- */
QFrame#HabitListPane {
    background: @bg_alt@;
    border-right: 1px solid @border@;
}
QWidget#HabitPage { background: @bg@; }
QLabel#HabitPaneTitle { font-size: 21px; font-weight: 800; color: @text_hi@; }
QFrame#HabitRow { background: transparent; border: none; border-radius: 10px; }
QFrame#HabitRow:hover { background: @surface_hi@; }
QFrame#HabitRow[selected="true"] { background: @accent_soft@; }
QLabel#HabitName { font-size: 13.5px; font-weight: 600; color: @text_hi@; background: transparent; }
QLabel#HabitCount { font-size: 14px; font-weight: 700; color: @text_hi@; background: transparent; }
QLabel#HabitCountSub { font-size: 10.5px; color: @muted@; background: transparent; }
/* 分组标题：只在真有两个以上分组时才出现（见 HabitPane.reload） */
QLabel#HabitGroupHead {
    font-size: 11.5px; font-weight: 600; color: @muted@;
    background: transparent; padding: 10px 2px 2px;
}
/* 习惯行末的点阵：16px 正圆。未打卡是空心圈、已打卡蓝底白勾 ——
   以前未打卡铺成实心浅灰，一行八个灰点和「已打卡」抢注意力，
   扫一眼看不出坚持了哪几天。 */
QPushButton#HabitDot {
    background: transparent; border: 1px solid @border_strong@;
    border-radius: 8px;
    padding: 0; font-size: 10px; color: transparent;
}
QPushButton#HabitDot:hover { background: @surface_hi@; border-color: @muted@; }
QPushButton#HabitDot[dotState="done"] {
    background: @accent@; border-color: @accent@;
    color: #ffffff; font-weight: 700;
}
QPushButton#HabitDot[dotState="future"] {
    background: transparent; border-color: @surface_hi@;
}
/* 频率没排到的日子（每周三习惯的周一）：淡到几乎没有，也不给点 */
QPushButton#HabitDot[dotState="off"] {
    background: transparent; border-color: @surface_hi@;
}
QPushButton#HabitDot[dotState="off"]:hover {
    background: transparent; border-color: @surface_hi@;
}
QFrame#HabitDetailPane {
    background: @bg_alt@;
    border-left: 1px solid @border@;
}
QLabel#HabitDetailTitle { font-size: 18px; font-weight: 800; color: @text_hi@; }
/* 窄窗口覆盖模式下的 ✕：和 ⋯ 同一档，只是文字换成叉 */
QPushButton#DetailClose {
    background: transparent; border: none; border-radius: 10px;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
    color: @muted@; font-size: 14px; padding: 0;
}
QPushButton#DetailClose:hover { background: @surface_hi@; color: @text@; }
QLabel#HabitDetailSub { font-size: 11px; color: @muted@; }
/* 详情统计卡：滴答是浅灰平底、不带描边 */
QFrame#HabitTile {
    background: @bg@; border: none; border-radius: 12px;
}
QLabel#TileBig { font-size: 25px; font-weight: 800; color: @text_hi@; background: transparent; }
QLabel#TileUnit { font-size: 12px; color: @muted@; background: transparent; }
QLabel#TileLabel { font-size: 12px; color: @text@; background: transparent; }
/* 统计卡上的 ⇄：在「当前连续 / 最高连续」之间切 */
QPushButton#TileSwap {
    background: transparent; border: none; border-radius: 5px;
    color: @muted@; font-size: 12px; padding: 0;
}
QPushButton#TileSwap:hover { background: @surface_hi@; color: @accent@; }
QPushButton#CalDay[calState="done"] { background: @accent@; color: #ffffff; font-weight: 700; }
QPushButton#CalDay[calState="done"]:hover { background: @accent_hi@; }
QPushButton#CalDay[calState="dim"] { color: @border_strong@; }
QPushButton#CircleDay {
    background: transparent; border: 1px solid @border_strong@; border-radius: 15px;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
    color: @muted@; font-size: 12px; padding: 0;
}
QPushButton#CircleDay:checked {
    background: @accent@; border-color: @accent@; color: white; font-weight: 600;
}
QPushButton#ComboBtn {
    background: @surface@; border: 1px solid @border_strong@; border-radius: 9px;
    padding: 8px 30px 8px 12px; text-align: left; color: @text@; font-size: 13px;
}
QPushButton#ComboBtn:hover { border-color: @accent@; }
QPushButton#ComboBtn[active="true"] { border-color: @accent@; }
/* 未设置的值（如提醒的「＋」）在滴答里是淡灰占位，不是正文色 */
QPushButton#ComboBtn[placeholder="true"] { color: @muted@; }
QPushButton#MiniSpin {
    background: @bg@; border: 1px solid @border@; border-radius: 7px;
    min-width: 56px; max-width: 72px; min-height: 28px; padding: 2px 8px;
    color: @text@; font-size: 13px;
}
QLabel#FormLabel { font-size: 13px; color: @text@; background: transparent; }
QLabel#PopupTitle { font-size: 14px; font-weight: 800; color: @text_hi@; }
QLabel#BadgeCell {
    background: transparent; border: none; border-radius: 18px;
    font-size: 20px; min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px;
}
QLabel#BadgeCell:hover { background: @surface_hi@; }
QLabel#BadgeCell[checked="true"] { background: @accent_soft@; }
QLabel#HabitLogTitle { font-size: 13.5px; font-weight: 700; color: @text_hi@; }
QFrame#HabitLogRow { background: transparent; border: none; border-radius: 8px; }
QFrame#HabitLogRow:hover { background: @surface_hi@; }
QLabel#HabitLogDate { font-size: 12px; color: @muted@; font-weight: 600; background: transparent; }
QLabel#HabitLogText { font-size: 12.5px; color: @text@; background: transparent; }
QDialog#TickDialog QLabel { background: transparent; }
QLineEdit#HabitNameInput {
    background: @surface_hi@; border: 1px solid @border_strong@; border-radius: 9px;
    padding: 9px 12px; font-size: 13.5px; color: @text@; selection-background-color: @accent@;
}
QLineEdit#HabitNameInput:focus { border-color: @accent@; background: @surface@; }

/* ---------- 日历管理 ---------- */
QFrame#DayCell {
    background: @surface@;
    border: 1px solid @border@;
    border-radius: 8px;
}
QFrame#DayCell:hover { border-color: @accent@; }
QFrame#DayCell[selected="true"] { border: 2px solid @accent@; }
QWidget#DayCellEmpty { background: transparent; }
QLabel#DayNum {
    font-size: 12px; font-weight: 600; color: @text@;
}
QLabel#WeekdayHeader {
    font-size: 12px; font-weight: 600; color: @muted@;
    padding: 6px 0;
}
QLabel#MonthLabel {
    font-size: 18px; font-weight: 800; color: @text_hi@;
}
QPushButton#MonthNavBtn {
    background: @surface_hi@; border: none; border-radius: 8px;
    color: @text@; font-size: 16px; font-weight: 700;
}
QPushButton#MonthNavBtn:hover { background: @accent_soft@; color: @accent@; }
QLabel#FieldLabel { font-size: 12px; color: @muted@; font-weight: 600; }
QTimeEdit#EventTime {
    background: @bg@; border: 1px solid @border@; border-radius: 9px;
    padding: 6px 8px; color: @text@; min-width: 80px;
}
QLineEdit#EventTitleInput {
    background: @bg@; border: 1px solid @border@; border-radius: 9px;
    padding: 8px 10px; color: @text@; font-size: 14px;
}
QComboBox#EventCategory {
    background: @bg@; border: 1px solid @border@; border-radius: 9px;
    padding: 8px 10px; color: @text@;
}
QTextEdit#EventNote {
    background: @bg@; border: 1px solid @border@; border-radius: 9px;
    padding: 8px 10px; color: @text@;
}

/* ---------- 科研管理 ---------- */
QWidget#ResearchCanvas { background: @bg_alt@; border: none; }
/* 课题卡选中态（论文库已交给 Arxiver，这里不再有 PaperRow / StarBtn） */
QFrame#Card[selected="true"] { border: 1px solid @accent@; }
"""


def build_qss(colors: dict) -> str:
    qss = _QSS_TEMPLATE
    for k, v in colors.items():
        qss = qss.replace(f"@{k}@", v)
    img = check_icon_path()
    if img:
        qss = qss.replace("@check_img@", img)
    else:
        # 生成失败时整行去掉，留着 url("") 会让 Qt 报样式解析错误
        qss = qss.replace('    image: url("@check_img@");\n', "")
    return qss


# ---------- 主题同步文件（内嵌工具跟随 LifeSystem 主题） ----------
_SYNC_NAME = ".life_system_theme"


def _sync_path() -> str:
    return os.path.join(os.path.expanduser("~"), _SYNC_NAME)


def write_sync(dark: bool) -> None:
    """把当前主题写入同步文件，供内嵌的 ApiCluster / RAG / Arxiver 轮询跟随。"""
    try:
        with open(_sync_path(), "w", encoding="utf-8") as f:
            f.write("dark" if dark else "light")
    except Exception:  # noqa: BLE001
        pass


def clear_sync() -> None:
    """删除主题同步文件（LifeSystem 退出后，工具恢复各自独立主题）。"""
    try:
        os.remove(_sync_path())
    except OSError:
        pass


# ---------- 颜色插值 ----------
def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb: tuple) -> str:
    return "#%02x%02x%02x" % rgb


def lerp_color(a: str, b: str, t: float) -> str:
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return _rgb_to_hex((
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bb - ba) * t),
    ))


# ---------- 主题管理器 ----------
class ThemeManager(QObject):
    """负责双主题切换与过渡动画。"""

    changed = Signal()

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._dark = False
        self._colors = dict(LIGHT)
        self._anim: QVariantAnimation | None = None

    @property
    def is_dark(self) -> bool:
        return self._dark

    @property
    def colors(self) -> dict:
        return self._colors

    def get(self, key: str) -> str:
        return self._colors.get(key, "#000000")

    def apply_immediate(self, dark: bool) -> None:
        self._dark = dark
        self._colors = dict(DARK if dark else LIGHT)
        self._app.setStyleSheet(build_qss(self._colors))
        write_sync(dark)
        self.changed.emit()

    def toggle(self) -> None:
        self.set_dark(not self._dark)

    def set_dark(self, dark: bool) -> None:
        if dark == self._dark:
            return
        self._dark = dark
        target = dict(DARK if dark else LIGHT)
        self._swap(target)

    # ------------------------------------------------------------------
    # 换肤：一次性应用 + 快照淡出
    # ------------------------------------------------------------------
    def _swap(self, target: dict) -> None:
        """一次性换肤，用旧界面快照的淡出来充当过渡动画。

        为什么不再逐帧插值改样式表：`app.setStyleSheet()` 每次都要给整棵控件树
        重算样式，成本主要由 **QSS 体积** 决定（实测本应用 398 条规则 / 36KB）：

            空样式表      100 ms
            24 字节样式表 118 ms
            完整 QSS      ~900 ms      ← 固定成本只有 100ms，其余全是解析 + 匹配

        原来的 6 帧动画 = 6 × 900ms ≈ **5.4 秒**，而且每帧之间 GUI 线程完全阻塞，
        用户看到的是 6 次生硬跳色 + 长时间无响应，根本不像动画。
        现在改成：抓一张旧界面快照 → 一次性换肤（约 0.9s）→ 快照盖在最上层淡出。
        总耗时约 1.1s，视觉上是真正的淡入淡出。
        """
        win = self._host_window()
        snap = None
        if win is not None:
            try:
                snap = win.grab()
            except Exception:  # noqa: BLE001
                snap = None

        self._colors = dict(target)
        self._app.setStyleSheet(build_qss(self._colors))
        write_sync(self._dark)
        self.changed.emit()

        if win is not None and snap is not None and not snap.isNull():
            self._fade_out(win, snap)

    def _host_window(self):
        """挑一个可见的顶层窗口来挂过渡遮罩。"""
        w = self._app.activeWindow()
        if w is not None and w.isVisible():
            return w
        for w in self._app.topLevelWidgets():
            if w.isWindow() and w.isVisible():
                return w
        return None

    def _fade_out(self, win, snap) -> None:
        """把旧界面快照盖在窗口上淡出。

        遮罩设了 `WA_TransparentForMouseEvents`，淡出的这 200ms 里点击照常穿透，
        不会出现「点不动」的错觉。
        """
        self._clear_overlay()

        overlay = QLabel(win)
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.setScaledContents(True)     # 快照是物理像素（150% DPI），要缩回逻辑尺寸
        overlay.setPixmap(snap)
        overlay.setGeometry(0, 0, win.width(), win.height())
        overlay.show()
        overlay.raise_()

        eff = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", overlay)
        anim.setDuration(200)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self._clear_overlay)
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        # 保住引用：否则动画对象被 GC，遮罩就永远留在窗口上了
        self._fade_overlay = overlay
        self._fade_anim = anim

    def _clear_overlay(self) -> None:
        """清掉过渡遮罩。

        必须容错：遮罩的 `deleteLater` 已经在上一轮动画的 `finished` 里执行过，
        但 Python 这边还握着包装对象，再碰它就是
        `RuntimeError: Internal C++ object already deleted`。
        """
        ov = getattr(self, "_fade_overlay", None)
        self._fade_overlay = None
        self._fade_anim = None
        if ov is not None:
            try:
                ov.deleteLater()
            except RuntimeError:
                pass


# 全局主题管理器（在 main 中初始化）
manager: ThemeManager | None = None


def get(key: str) -> str:
    """返回当前生效颜色。"""
    if manager is None:
        return LIGHT.get(key, "#000000")
    return manager.get(key)


def is_dark() -> bool:
    """当前是否夜间模式。"""
    return bool(manager is not None and manager.is_dark)
