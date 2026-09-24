"""待办模块的线性图标集（对齐滴答清单的视觉语言）。

为什么不用 emoji：emoji 是彩色的、字形由系统决定，是「不像滴答清单」最明显的破绽。
这里用 QPainter 按 1.6px 圆头描边自绘，颜色取自主题色表，
所以日/夜切换、选中态变色都能自动跟随。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPointF, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPainterPath
from PySide6.QtWidgets import QWidget

from . import theme


def _c(name: str) -> QColor:
    return QColor(theme.get(name))


def is_emoji_icon(value: str) -> bool:
    """清单图标是不是一个 emoji（线性图标的 kind 全是 ASCII 名字）。"""
    return any(ord(ch) > 0x2000 for ch in value)


def emoji_text(value: str) -> str:
    """补 U+FE0F 强制走彩色 emoji 呈现。

    ✏ ⌨  ❤ ☕ 这些默认是「文本呈现」，Windows 会拿 Segoe UI Symbol 画成
    单色剪影；带上变体选择符才交给 Segoe UI Emoji 上色。库里存的还是原样。
    """
    return value if value.endswith("\uFE0F") else value + "\uFE0F"


class TickIcon(QWidget):
    """单色线性图标。``color_key`` 走主题色表，``color_hex`` 用于清单/标签自定义色。"""

    def __init__(self, kind: str, size: int = 16, color_key: str = "muted",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._kind = kind
        self._color_key = color_key
        self._color_hex = ""
        self._glyph = ""
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        # grab() 出来的图默认拿调色板的窗口色打底（实测 #f3f3f3）。亮色底上看不
        # 出来，暗色主题里每个图标都顶着一块白斑。
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.update()

    def set_glyph(self, text: str) -> None:
        """日历图标格子里那行小字：滴答在「今天」里写日期号、「最近7天」里写星期。"""
        if text != self._glyph:
            self._glyph = text
            self.update()

    def set_color_key(self, key: str) -> None:
        self._color_key = key
        self._color_hex = ""
        self.update()

    def set_color_hex(self, hex_value: str) -> None:
        self._color_hex = hex_value or ""
        self.update()

    def _color(self) -> QColor:
        if self._color_hex:
            return QColor(self._color_hex)
        return _c(self._color_key)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.size()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = float(min(self.width(), self.height()))
        col = self._color()
        pen = QPen(col, max(1.35, s * 0.085))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        def fill():
            p.setPen(Qt.NoPen)
            p.setBrush(col)

        def stroke():
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)

        k = self._kind
        if is_emoji_icon(k):
            # 清单图标可以直接放一个 emoji（和主窗口左侧那条模块栏同一套画法）。
            # 不上主题色也不加粗 —— 交给系统的彩色 emoji 字体画，描边反而会把它压成
            # 单色块。判据用码位：线性图标的 kind 全是 ASCII 名字。
            f = QFont(self.font())
            f.setPixelSize(max(9, round(s * 0.88)))
            p.setFont(f)
            p.setPen(QColor(theme.get("text")))
            p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, emoji_text(k))
            return
        pt = QPointF
        rl = lambda x, y, w, h, r: p.drawRoundedRect(QRectF(x, y, w, h), r, r)  # noqa: E731

        if k in ("today", "week", "calendar"):
            # 日历：顶部两根装订针 + 圆角框；today 在格内填一小块
            rl(s * .12, s * .22, s * .76, s * .66, s * .13)
            p.drawLine(pt(s * .30, s * .10), pt(s * .30, s * .26))
            p.drawLine(pt(s * .70, s * .10), pt(s * .70, s * .26))
            p.drawLine(pt(s * .12, s * .42), pt(s * .88, s * .42))
            if self._glyph:
                # 滴答把「今天」画成日期号、「最近7天」画成星期缩写，格子里那行
                # 小字是这两个入口最主要的辨识点，没有它就只是两个一样的日历。
                f = QFont(self.font())
                f.setPixelSize(max(6, round(s * 0.34)))
                f.setBold(True)
                f.setLetterSpacing(QFont.AbsoluteSpacing, 0)
                p.setFont(f)
                p.setPen(col)
                p.drawText(QRectF(s * .10, s * .40, s * .80, s * .50),
                           Qt.AlignCenter, self._glyph)
                stroke()
            elif k == "today":
                fill()
                rl(s * .56, s * .56, s * .22, s * .20, s * .05)
                stroke()
            elif k == "week":
                for i, y in enumerate((.56, .68, .80)):
                    p.drawLine(pt(s * .24, s * y), pt(s * (.52 if i == 2 else .76), s * y))
        elif k == "inbox":
            # 收集箱：托盘 + 落入箭头
            path = QPainterPath()
            path.moveTo(s * .12, s * .52)
            path.lineTo(s * .30, s * .52)
            path.lineTo(s * .38, s * .68)
            path.lineTo(s * .62, s * .68)
            path.lineTo(s * .70, s * .52)
            path.lineTo(s * .88, s * .52)
            path.lineTo(s * .88, s * .80)
            path.lineTo(s * .12, s * .80)
            path.closeSubpath()
            p.drawPath(path)
            p.drawLine(pt(s * .50, s * .12), pt(s * .50, s * .42))
            p.drawLine(pt(s * .38, s * .31), pt(s * .50, s * .43))
            p.drawLine(pt(s * .62, s * .31), pt(s * .50, s * .43))
        elif k in ("all", "tasks"):
            rl(s * .16, s * .10, s * .68, s * .80, s * .12)
            for y in (.34, .50, .66):
                p.drawLine(pt(s * .30, s * y), pt(s * .70, s * y))
            fill()
            p.drawEllipse(pt(s * .245, s * .34), s * .028, s * .028)
            p.drawEllipse(pt(s * .245, s * .50), s * .028, s * .028)
            stroke()
        elif k == "list":
            fill()
            for y in (.26, .50, .74):
                p.drawEllipse(pt(s * .20, s * y), s * .045, s * .045)
            stroke()
            for y in (.26, .50, .74):
                p.drawLine(pt(s * .38, s * y), pt(s * .82, s * y))
        elif k == "board":
            # 看板：三列高低不同的竖条（⋯ 菜单「视图」那一格）
            for x, top in ((.14, .30), (.44, .16), (.74, .40)):
                rl(s * x, s * top, s * .13, s * (.82 - top), s * .04)
        elif k == "timeline":
            # 时间线：三条错开的横条
            for y, x0, w in ((.22, .12, .46), (.44, .34, .52), (.66, .18, .34)):
                rl(s * x0, s * y, s * w, s * .12, s * .06)
        elif k in ("folder", "folder_open"):
            path = QPainterPath()
            path.moveTo(s * .10, s * .30)
            path.lineTo(s * .40, s * .30)
            path.lineTo(s * .47, s * .40)
            path.lineTo(s * .90, s * .40)
            path.lineTo(s * .90, s * .80)
            path.lineTo(s * .10, s * .80)
            path.closeSubpath()
            p.drawPath(path)
            if k == "folder_open":
                p.drawLine(pt(s * .10, s * .80), pt(s * .26, s * .52))
                p.drawLine(pt(s * .26, s * .52), pt(s * .90, s * .52))
        elif k == "tag":
            path = QPainterPath()
            path.moveTo(s * .46, s * .12)
            path.lineTo(s * .88, s * .12)
            path.lineTo(s * .88, s * .54)
            path.lineTo(s * .20, s * .88)
            path.lineTo(s * .12, s * .46)
            path.closeSubpath()
            p.drawPath(path)
            fill()
            p.drawEllipse(pt(s * .68, s * .32), s * .055, s * .055)
            stroke()
        elif k == "flag":
            p.drawLine(pt(s * .26, s * .10), pt(s * .26, s * .90))
            path = QPainterPath()
            path.moveTo(s * .26, s * .16)
            path.lineTo(s * .84, s * .16)
            path.lineTo(s * .68, s * .36)
            path.lineTo(s * .84, s * .56)
            path.lineTo(s * .26, s * .56)
            path.closeSubpath()
            fill()
            p.drawPath(path)
            stroke()
        elif k == "flag_none":
            # 「无优先级」是只描边的旗子，不填色 —— 滴答卡片右上角和优先级
            # 菜单最后一行都是这么画的，画成实心灰旗会看不出「当前没选档」。
            p.drawLine(pt(s * .26, s * .10), pt(s * .26, s * .90))
            path = QPainterPath()
            path.moveTo(s * .26, s * .16)
            path.lineTo(s * .84, s * .16)
            path.lineTo(s * .68, s * .36)
            path.lineTo(s * .84, s * .56)
            path.lineTo(s * .26, s * .56)
            path.closeSubpath()
            p.drawPath(path)
            stroke()
        elif k == "gear":
            # 显示设置（齿轮）：齿要和外圈接上，否则看着像个太阳
            cx = cy = s * .5
            for i in range(8):
                a = math.pi / 4 * i
                p.drawLine(QPointF(cx + s * .17 * math.cos(a),
                                   cy + s * .17 * math.sin(a)),
                           QPointF(cx + s * .41 * math.cos(a),
                                   cy + s * .41 * math.sin(a)))
            p.drawEllipse(QRectF(cx - s * .26, cy - s * .26, s * .52, s * .52))
            p.drawEllipse(QRectF(cx - s * .11, cy - s * .11, s * .22, s * .22))
        elif k == "rss":
            # 日历订阅：滴答画的是「圆角方框里套一个 RSS」—— 框 + 左下角实心点
            # + 两道朝右上张开的四分之一弧
            rl(s * .10, s * .12, s * .80, s * .76, s * .16)
            ox, oy = s * .32, s * .68
            fill()
            p.drawEllipse(QRectF(ox - s * .055, oy - s * .055,
                                 s * .11, s * .11))
            stroke()
            for rad in (s * .17, s * .33):
                p.drawArc(QRectF(ox - rad, oy - rad, rad * 2, rad * 2),
                          -90 * 16, 90 * 16)
        elif k == "printer":
            # 打印：上面进纸、中间机身、下面出纸，机身上点一颗指示灯
            rl(s * .22, s * .12, s * .56, s * .24, s * .04)
            rl(s * .12, s * .36, s * .76, s * .30, s * .07)
            rl(s * .24, s * .66, s * .52, s * .22, s * .04)
            fill()
            p.drawEllipse(QRectF(s * .74, s * .43, s * .08, s * .08))
            stroke()
        elif k == "quad":
            # 四象限过滤器：直角实心小方块 + 一圈细描边。
            # 描边是必需的 —— 滴答的「不重要不紧急」是白底，没描边就看不见。
            fill()
            rl(s * .14, s * .14, s * .72, s * .72, 1)
            stroke()
            pen2 = QPen(_c("border_strong"), 1.2)
            p.setPen(pen2)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(s * .14, s * .14, s * .72, s * .72))
        elif k == "done":
            rl(s * .12, s * .12, s * .76, s * .76, s * .18)
            p.drawLine(pt(s * .28, s * .50), pt(s * .44, s * .66))
            p.drawLine(pt(s * .44, s * .66), pt(s * .74, s * .32))
        elif k == "check_circle":
            p.drawEllipse(QRectF(s * .12, s * .12, s * .76, s * .76))
            p.drawLine(pt(s * .30, s * .50), pt(s * .45, s * .65))
            p.drawLine(pt(s * .45, s * .65), pt(s * .72, s * .34))
        elif k == "check":
            p.drawLine(pt(s * .20, s * .54), pt(s * .41, s * .75))
            p.drawLine(pt(s * .41, s * .75), pt(s * .80, s * .26))
        elif k == "none":
            # 「无颜色」：圆圈加一道斜杠（滴答调色板的第一格）
            p.drawEllipse(QRectF(s * .16, s * .16, s * .68, s * .68))
            p.drawLine(pt(s * .28, s * .72), pt(s * .72, s * .28))
        elif k == "circle":
            p.drawEllipse(QRectF(s * .14, s * .14, s * .72, s * .72))
        elif k == "trash":
            p.drawLine(pt(s * .16, s * .26), pt(s * .84, s * .26))
            p.drawLine(pt(s * .38, s * .26), pt(s * .43, s * .13))
            p.drawLine(pt(s * .62, s * .26), pt(s * .57, s * .13))
            p.drawLine(pt(s * .43, s * .13), pt(s * .57, s * .13))
            path = QPainterPath()
            path.moveTo(s * .24, s * .26)
            path.lineTo(s * .30, s * .88)
            path.lineTo(s * .70, s * .88)
            path.lineTo(s * .76, s * .26)
            p.drawPath(path)
            for x in (.42, .58):
                p.drawLine(pt(s * x, s * .42), pt(s * (x + .01), s * .72))
        elif k == "archive":
            rl(s * .10, s * .16, s * .80, s * .22, s * .06)
            p.drawLine(pt(s * .20, s * .38), pt(s * .20, s * .84))
            p.drawLine(pt(s * .80, s * .38), pt(s * .80, s * .84))
            p.drawLine(pt(s * .20, s * .84), pt(s * .80, s * .84))
            p.drawLine(pt(s * .42, s * .54), pt(s * .58, s * .54))
        elif k == "restore":
            # 垃圾桶「恢复」：逆时针回旋箭头（↺）
            cx, cy, r = s * .50, s * .54, s * .30
            rect = QRectF(cx - r, cy - r, r * 2, r * 2)
            p.drawArc(rect, 118 * 16, 300 * 16)
            a = math.radians(118)
            ex, ey = cx + r * math.cos(a), cy - r * math.sin(a)
            p.drawLine(pt(ex, ey), pt(ex - s * .16, ey - s * .02))
            p.drawLine(pt(ex, ey), pt(ex + s * .02, ey + s * .16))
        elif k == "sun":
            # 快捷日期「今天」：太阳（原来是 ☀ emoji，在 Windows 上渲染成彩色）
            cx = cy = s * .50
            r = s * .18
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
            for i in range(8):
                a = math.radians(i * 45)
                p.drawLine(pt(cx + math.cos(a) * r * 1.55, cy + math.sin(a) * r * 1.55),
                           pt(cx + math.cos(a) * r * 2.05, cy + math.sin(a) * r * 2.05))
        elif k == "sunrise":
            # 「明天」：地平线上的半轮太阳 + 上箭头（滴答就是这个形）
            p.drawLine(pt(s * .12, s * .72), pt(s * .88, s * .72))
            p.drawArc(QRectF(s * .30, s * .34, s * .40, s * .38), 0, 180 * 16)
            p.drawLine(pt(s * .50, s * .12), pt(s * .50, s * .34))
            p.drawLine(pt(s * .38, s * .24), pt(s * .50, s * .12))
            p.drawLine(pt(s * .62, s * .24), pt(s * .50, s * .12))
            p.drawLine(pt(s * .22, s * .86), pt(s * .78, s * .86))
        elif k == "moon":
            # 「今晚」：月牙 = 大圆挖掉偏右上的同径圆
            path = QPainterPath()
            path.addEllipse(QRectF(s * .20, s * .14, s * .66, s * .72))
            hole = QPainterPath()
            hole.addEllipse(QRectF(s * .38, s * .06, s * .60, s * .60))
            p.drawPath(path.subtracted(hole))
        elif k == "copy":
            # 创建副本：两张错开的圆角矩形（后一张只露出上边和右边）
            p.drawLine(pt(s * .34, s * .22), pt(s * .80, s * .22))
            p.drawLine(pt(s * .80, s * .22), pt(s * .80, s * .62))
            rl(s * .20, s * .34, s * .58, s * .46, s * .10)
        elif k == "pin":
            # 置顶：向上箭头 + 底线
            p.drawLine(pt(s * .50, s * .16), pt(s * .50, s * .68))
            p.drawLine(pt(s * .32, s * .34), pt(s * .50, s * .16))
            p.drawLine(pt(s * .68, s * .34), pt(s * .50, s * .16))
            p.drawLine(pt(s * .22, s * .86), pt(s * .78, s * .86))
        elif k == "edit":
            p.drawLine(pt(s * .22, s * .78), pt(s * .70, s * .30))
            p.drawLine(pt(s * .70, s * .30), pt(s * .80, s * .40))
            p.drawLine(pt(s * .80, s * .40), pt(s * .32, s * .88))
            p.drawLine(pt(s * .32, s * .88), pt(s * .22, s * .78))
            p.drawLine(pt(s * .62, s * .22), pt(s * .78, s * .06))
            p.drawLine(pt(s * .78, s * .06), pt(s * .94, s * .22))
            p.drawLine(pt(s * .94, s * .22), pt(s * .80, s * .40))
        elif k == "more":
            fill()
            for x in (.22, .50, .78):
                p.drawEllipse(pt(s * x, s * .50), s * .065, s * .065)
            stroke()
        elif k == "sort":
            p.drawLine(pt(s * .18, s * .28), pt(s * .62, s * .28))
            p.drawLine(pt(s * .18, s * .52), pt(s * .52, s * .52))
            p.drawLine(pt(s * .18, s * .76), pt(s * .42, s * .76))
            p.drawLine(pt(s * .78, s * .22), pt(s * .78, s * .78))
            p.drawLine(pt(s * .68, s * .68), pt(s * .78, s * .80))
            p.drawLine(pt(s * .88, s * .68), pt(s * .78, s * .80))
        elif k == "search":
            p.drawEllipse(QRectF(s * .18, s * .18, s * .48, s * .48))
            p.drawLine(pt(s * .60, s * .60), pt(s * .84, s * .84))
        elif k == "repeat":
            rect = QRectF(s * .16, s * .20, s * .68, s * .60)
            p.drawArc(rect, 200 * 16, 300 * 16)
            p.drawLine(pt(s * .24, s * .34), pt(s * .24, s * .50))
            p.drawLine(pt(s * .24, s * .34), pt(s * .40, s * .34))
            p.drawArc(rect, 20 * 16, 300 * 16)
            p.drawLine(pt(s * .76, s * .66), pt(s * .76, s * .50))
            p.drawLine(pt(s * .76, s * .66), pt(s * .60, s * .66))
        elif k == "overdue":
            # 已过期：小回旋箭头（滴答在过期日期前画的那个 ↻）
            rect = QRectF(s * .18, s * .22, s * .64, s * .56)
            p.drawArc(rect, 300 * 16, 250 * 16)
            p.drawLine(pt(s * .78, s * .40), pt(s * .80, s * .56))
            p.drawLine(pt(s * .80, s * .56), pt(s * .64, s * .54))
        elif k == "note":
            rl(s * .18, s * .10, s * .64, s * .80, s * .10)
            for y in (.34, .50, .66):
                p.drawLine(pt(s * .30, s * y), pt(s * .70, s * y))
        elif k == "clock":
            p.drawEllipse(QRectF(s * .12, s * .12, s * .76, s * .76))
            p.drawLine(pt(s * .50, s * .28), pt(s * .50, s * .52))
            p.drawLine(pt(s * .50, s * .52), pt(s * .68, s * .62))
        elif k == "duration":
            # 沙漏（时长）：上下两条横梁 + 两支斜线交成 X
            p.drawLine(pt(s * .26, s * .12), pt(s * .74, s * .12))
            p.drawLine(pt(s * .26, s * .88), pt(s * .74, s * .88))
            p.drawLine(pt(s * .30, s * .12), pt(s * .30, s * .26))
            p.drawLine(pt(s * .70, s * .12), pt(s * .70, s * .26))
            p.drawLine(pt(s * .30, s * .88), pt(s * .30, s * .74))
            p.drawLine(pt(s * .70, s * .88), pt(s * .70, s * .74))
            path = QPainterPath()
            path.moveTo(s * .30, s * .26)
            path.lineTo(s * .70, s * .74)
            path.moveTo(s * .70, s * .26)
            path.lineTo(s * .30, s * .74)
            p.drawPath(path)
        elif k == "close":
            p.drawLine(pt(s * .24, s * .24), pt(s * .76, s * .76))
            p.drawLine(pt(s * .76, s * .24), pt(s * .24, s * .76))
        elif k == "plus":
            p.drawLine(pt(s * .50, s * .18), pt(s * .50, s * .82))
            p.drawLine(pt(s * .18, s * .50), pt(s * .82, s * .50))
        elif k == "chevron_down":
            p.drawLine(pt(s * .28, s * .40), pt(s * .50, s * .62))
            p.drawLine(pt(s * .50, s * .62), pt(s * .72, s * .40))
        elif k == "chevron_right":
            p.drawLine(pt(s * .40, s * .28), pt(s * .62, s * .50))
            p.drawLine(pt(s * .62, s * .50), pt(s * .40, s * .72))
        elif k == "arrow_up":
            p.drawLine(pt(s * .50, s * .22), pt(s * .50, s * .80))
            p.drawLine(pt(s * .28, s * .44), pt(s * .50, s * .22))
            p.drawLine(pt(s * .50, s * .22), pt(s * .72, s * .44))
        elif k == "arrow_down":
            p.drawLine(pt(s * .50, s * .20), pt(s * .50, s * .78))
            p.drawLine(pt(s * .28, s * .56), pt(s * .50, s * .78))
            p.drawLine(pt(s * .50, s * .78), pt(s * .72, s * .56))
        elif k == "grip":
            # ⋮⋮ 拖拽柄
            fill()
            for cx in (.36, .64):
                for cy in (.24, .5, .76):
                    p.drawEllipse(pt(s * cx, s * cy), s * .055, s * .055)
            stroke()
        elif k == "subtask_list":
            # 详情标题右侧的子任务开关：三行，前两行带方框
            for i, y in enumerate((.26, .5, .74)):
                p.drawLine(pt(s * (.42 if i < 2 else .16), s * y),
                           pt(s * .86, s * y))
            for y in (.26, .5):
                rl(s * .14, s * (y - .11), s * .22, s * .22, s * .05)
        elif k == "subtask":
            rl(s * .10, s * .16, s * .30, s * .30, s * .07)
            p.drawLine(pt(s * .17, s * .31), pt(s * .22, s * .37))
            p.drawLine(pt(s * .22, s * .37), pt(s * .34, s * .22))
            p.drawLine(pt(s * .50, s * .31), pt(s * .90, s * .31))
            rl(s * .10, s * .54, s * .30, s * .30, s * .07)
            p.drawLine(pt(s * .50, s * .69), pt(s * .90, s * .69))
        elif k == "menu":
            for y in (.28, .50, .72):
                p.drawLine(pt(s * .16, s * y), pt(s * .84, s * y))
        elif k == "filter":
            path = QPainterPath()
            path.moveTo(s * .14, s * .20)
            path.lineTo(s * .86, s * .20)
            path.lineTo(s * .58, s * .52)
            path.lineTo(s * .58, s * .84)
            path.lineTo(s * .42, s * .72)
            path.lineTo(s * .42, s * .52)
            path.closeSubpath()
            p.drawPath(path)
        elif k == "habit":
            # 今日打卡：闹钟
            p.drawEllipse(QRectF(s * .16, s * .22, s * .68, s * .62))
            p.drawLine(pt(s * .20, s * .14), pt(s * .30, s * .22))
            p.drawLine(pt(s * .80, s * .14), pt(s * .70, s * .22))
            p.drawLine(pt(s * .50, s * .46), pt(s * .50, s * .58))
            p.drawLine(pt(s * .50, s * .58), pt(s * .62, s * .62))
        elif k == "empty_box":
            # 空状态插画：一个打开的盒子 + 飘着的清单
            rl(s * .22, s * .18, s * .42, s * .50, s * .07)
            for y in (.32, .44, .56):
                p.drawLine(pt(s * .30, s * y), pt(s * (.56 if y < .5 else .48), s * y))
            path = QPainterPath()
            path.moveTo(s * .12, s * .74)
            path.lineTo(s * .34, s * .74)
            path.lineTo(s * .40, s * .88)
            path.lineTo(s * .60, s * .88)
            path.lineTo(s * .66, s * .74)
            path.lineTo(s * .88, s * .74)
            path.lineTo(s * .88, s * .94)
            path.lineTo(s * .12, s * .94)
            path.closeSubpath()
            p.drawPath(path)
        elif k == "link":
            # 在浏览器打开：方框 + 右上角出去的箭头
            rl(s * .14, s * .40, s * .46, s * .46, s * .10)
            p.drawLine(pt(s * .50, s * .50), pt(s * .86, s * .14))
            p.drawLine(pt(s * .64, s * .14), pt(s * .86, s * .14))
            p.drawLine(pt(s * .86, s * .14), pt(s * .86, s * .36))
        elif k == "abandon":
            # 放弃：方框里一个叉（和「清除日期」那种纯叉号区分开）
            rl(s * .14, s * .14, s * .72, s * .72, s * .16)
            p.drawLine(pt(s * .36, s * .36), pt(s * .64, s * .64))
            p.drawLine(pt(s * .64, s * .36), pt(s * .36, s * .64))
        elif k == "move_out":
            # 移动到：左边一个抽屉框，右边一个出去的箭头
            p.drawLine(pt(s * .30, s * .14), pt(s * .12, s * .14))
            p.drawLine(pt(s * .12, s * .14), pt(s * .12, s * .86))
            p.drawLine(pt(s * .12, s * .86), pt(s * .30, s * .86))
            p.drawLine(pt(s * .22, s * .50), pt(s * .80, s * .50))
            p.drawLine(pt(s * .64, s * .34), pt(s * .80, s * .50))
            p.drawLine(pt(s * .64, s * .66), pt(s * .80, s * .50))
        elif k == "floppy":
            # 转换为笔记：软盘 —— 右上角切一刀，上面写口、下面标签
            path = QPainterPath()
            path.moveTo(s * .16, s * .16)
            path.lineTo(s * .70, s * .16)
            path.lineTo(s * .84, s * .30)
            path.lineTo(s * .84, s * .84)
            path.lineTo(s * .16, s * .84)
            path.closeSubpath()
            p.drawPath(path)
            rl(s * .34, s * .16, s * .26, s * .22, s * .04)
            rl(s * .30, s * .56, s * .40, s * .28, s * .04)
        elif k == "focus":
            # 开始专注：一圈一点的目标
            p.drawEllipse(QRectF(s * .14, s * .14, s * .72, s * .72))
            fill()
            r = s * .16
            p.drawEllipse(QRectF(s * .50 - r, s * .50 - r, r * 2, r * 2))
            stroke()
        # 未知 kind 留白，不画任何东西（避免崩）


class ColorDot(QWidget):
    """清单 / 标签的颜色圆点（滴答在导航行右侧和行内清单名前画的那个）。"""

    def __init__(self, hex_value: str = "", size: int = 9,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._hex = hex_value
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_color_hex(self, hex_value: str) -> None:
        self._hex = hex_value or ""
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._hex:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._hex))
        d = min(self.width(), self.height())
        p.drawEllipse(QRectF((self.width() - d) / 2, (self.height() - d) / 2, d, d))


# 优先级 → 主题色键。滴答清单用勾选框的描边色表达优先级，而不是单独画点。
PRIO_COLOR_KEY = {0: "", 1: "blue", 2: "amber", 3: "red"}


class PrioCheckBox(QWidget):
    """滴答式勾选框：圆角方框，描边色 = 优先级色，选中后填充并画白色对勾。

    不用 QCheckBox 是因为 QSS 的 ``::indicator`` 一旦设了 background 就会丢掉
    原生对勾，只能画成纯色块；自绘才能同时控制描边色、圆角、对勾和悬停环。
    """

    toggled = Signal(bool)

    def __init__(self, checked: bool = False, size: int = 19,
                 shape: str = "square", parent: QWidget | None = None):
        super().__init__(parent)
        self._checked = checked
        self._prio = 0
        self._cross = False
        self._shape = shape
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def set_checked(self, checked: bool) -> None:
        if self._checked != checked:
            self._checked = checked
            self.update()

    def set_cross(self, on: bool) -> None:
        """勾里画 ✕ 而不是 ✓：滴答用这个区分「放弃」和「完成」。

        只换笔画，尺寸 / 底色都跟着完成态走 —— 放弃项同样不再出现在待做里。
        """
        on = bool(on)
        if self._cross != on:
            self._cross = on
            self.update()

    def toggle(self) -> None:
        """供整行点击复用：翻状态并把 toggled 发出去。"""
        self._checked = not self._checked
        self.update()
        self.toggled.emit(self._checked)

    def is_checked(self) -> bool:
        return self._checked

    def set_priority(self, prio: int) -> None:
        self._prio = int(prio or 0)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 吃掉按下：否则事件冒到整行上，勾一下会顺带把详情面板点开
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self.rect().contains(event.position().toPoint()):
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)
        # 必须吃掉松手：只 accept 按下不够，松手事件 ignore 的话照旧冒到整行，
        # 于是「勾一下」同时触发行上的动作（点开详情 / 再翻一次勾 = 没变）
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = float(min(self.width(), self.height()))
        inset = s * 0.10
        box = QRectF(inset, inset, s - inset * 2, s - inset * 2)
        radius = s * (0.5 if self._shape == "circle" else 0.26)
        key = PRIO_COLOR_KEY.get(self._prio, "")
        prio_col = _c(key) if key else _c("border_strong")

        if self._checked:
            # 勾上之后是中性灰底白勾（滴答的做法）：优先级只在没勾的时候
            # 用描边色表示，完成态再着优先级色会让整列五颜六色。
            p.setPen(Qt.NoPen)
            p.setBrush(_c("check_done"))
            p.drawRoundedRect(box, radius, radius)
            pen = QPen(QColor("#ffffff"), max(1.6, s * 0.11))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            if self._cross:
                # 放弃：同一个灰底方框里画叉，位置笔画粗细都跟着勾不变
                p.drawLine(QPointF(s * .32, s * .32), QPointF(s * .68, s * .68))
                p.drawLine(QPointF(s * .68, s * .32), QPointF(s * .32, s * .68))
                return
            p.drawLine(QPointF(s * .28, s * .52), QPointF(s * .44, s * .68))
            p.drawLine(QPointF(s * .44, s * .68), QPointF(s * .73, s * .33))
            return

        w = max(1.5, s * 0.10)
        p.setBrush(Qt.NoBrush)
        if self._hover:
            # 悬停：优先级色描边加深，并在外圈补一层同色半透明光晕
            glow = QColor(prio_col)
            glow.setAlpha(46)
            p.setPen(QPen(glow, w + s * 0.13))
            p.drawRoundedRect(box, radius, radius)
        p.setPen(QPen(prio_col, w))
        p.drawRoundedRect(box, radius, radius)


class FlagButton(QWidget):
    """详情面板右上角的优先级旗：点击弹菜单选档，颜色跟随当前优先级。"""

    clicked_ = Signal()

    def __init__(self, priority: int = 0, size: int = 30,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._prio = int(priority or 0)
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        d = int(min(self.width(), self.height()) * 0.66)
        self._icon = TickIcon(self._kind_for(self._prio), d, "muted", self)
        self._icon.move((self.width() - d) // 2, (self.height() - d) // 2)
        self._icon.show()

    @staticmethod
    def _kind_for(prio: int) -> str:
        """没选档位时是描边旗，选了才是实心彩旗。"""
        return "flag" if prio else "flag_none"

    def set_priority(self, prio: int) -> None:
        self._prio = int(prio or 0)
        self._icon.set_kind(self._kind_for(self._prio))
        self._icon.set_color_key(PRIO_COLOR_KEY.get(self._prio, "muted") or "muted")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked_.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(_c("surface_hi")))
            d = min(self.width(), self.height())
            p.drawRoundedRect(QRectF((self.width() - d) / 2 + 1,
                                     (self.height() - d) / 2 + 1, d - 2, d - 2), 6, 6)
        p.end()
        # 旗子本身是 __init__ 里建的常驻子控件。以前这里每次重绘都 new 一个
        # TickIcon 挂到 self 上，悬停一次多一个、永远不释放；而且它把 kind 写死成
        # "flag"，把 set_priority 里换好的 flag_none（无优先级的描边旗）又盖回去了。


