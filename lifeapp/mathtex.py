"""小型数学排版器：把 LaTeX 的一个子集排成真正的公式图。

为什么不用 matplotlib（实测过）：它的 \\text{} 走内置 rm 字体，中文丢字形，而这个库
的公式里全是中文注解；单张 15~260ms，一篇 444 张要 7 秒；还要给分发包再加约 100MB。
自绘只依赖 Qt，单张亚毫秒，中文交给 Qt 自己的字体回退。

一份语法树、两个后端：

- `to_html()` 退化成 Unicode + <sup>/<sub>。给大纲标签、传给 Obsidian 的标题、
  以及「画图失败时」兜底 —— 那条路上比对的是纯文本，必须是字。
- `render()` 走盒子模型画成 QImage。给正文预览，分式真的分两层、根号真的有上横线。

只覆盖这个库里实际出现的写法：分式、根号、上下标（可嵌套）、大算子带极限、
重音、\\left\\right 自动加高的括号、\\text 里的中文。认不出的退成命令名本身，
宁可难看，不能吞字。
"""
from __future__ import annotations

import re

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath

# ---------------------------------------------------------------- 语法表
# 命令名 -> (字符, 类别)。类别决定左右留不留缝：ord 贴着排，bin/rel 要留。
SYM = {
    "alpha": ("α", "ord"), "beta": ("β", "ord"), "gamma": ("γ", "ord"),
    "delta": ("δ", "ord"), "epsilon": ("ε", "ord"), "varepsilon": ("ε", "ord"),
    "zeta": ("ζ", "ord"), "eta": ("η", "ord"), "theta": ("θ", "ord"),
    "vartheta": ("ϑ", "ord"), "iota": ("ι", "ord"), "kappa": ("κ", "ord"),
    "lambda": ("λ", "ord"), "mu": ("μ", "ord"), "nu": ("ν", "ord"),
    "xi": ("ξ", "ord"), "pi": ("π", "ord"), "rho": ("ρ", "ord"),
    "sigma": ("σ", "ord"), "varsigma": ("ς", "ord"), "tau": ("τ", "ord"),
    "upsilon": ("υ", "ord"), "phi": ("φ", "ord"), "varphi": ("ϕ", "ord"),
    "chi": ("χ", "ord"), "psi": ("ψ", "ord"), "omega": ("ω", "ord"),
    "Gamma": ("Γ", "ord"), "Delta": ("Δ", "ord"), "Theta": ("Θ", "ord"),
    "Lambda": ("Λ", "ord"), "Xi": ("Ξ", "ord"), "Pi": ("Π", "ord"),
    "Sigma": ("Σ", "ord"), "Upsilon": ("Υ", "ord"), "Phi": ("Φ", "ord"),
    "Psi": ("Ψ", "ord"), "Omega": ("Ω", "ord"),
    "cdot": ("·", "bin"), "ast": ("∗", "bin"), "star": ("⋆", "bin"),
    "circ": ("∘", "bin"), "bullet": ("∙", "bin"), "odot": ("⊙", "bin"),
    "oplus": ("⊕", "bin"), "ominus": ("⊖", "bin"), "otimes": ("⊗", "bin"),
    "oslash": ("⊘", "bin"), "bigcirc": ("○", "bin"),
    "times": ("×", "bin"), "div": ("÷", "bin"), "pm": ("±", "bin"),
    "mp": ("∓", "bin"), "setminus": ("∖", "bin"),
    "leq": ("≤", "rel"), "le": ("≤", "rel"), "geq": ("≥", "rel"),
    "ge": ("≥", "rel"), "neq": ("≠", "rel"), "ne": ("≠", "rel"),
    "approx": ("≈", "rel"), "equiv": ("≡", "rel"), "sim": ("∼", "rel"),
    "simeq": ("≃", "rel"), "cong": ("≅", "rel"), "propto": ("∝", "rel"),
    "ll": ("≪", "rel"), "gg": ("≫", "rel"), "doteq": ("≐", "rel"),
    "mid": ("∣", "rel"), "parallel": ("∥", "rel"), "perp": ("⊥", "rel"),
    "rightarrow": ("→", "rel"), "to": ("→", "rel"),
    "longrightarrow": ("⟶", "rel"), "leftarrow": ("←", "rel"),
    "leftrightarrow": ("↔", "rel"), "Rightarrow": ("⇒", "rel"),
    "Leftarrow": ("⇐", "rel"), "Leftrightarrow": ("⇔", "rel"),
    "mapsto": ("↦", "rel"), "implies": ("⟹", "rel"), "iff": ("⟺", "rel"),
    "uparrow": ("↑", "ord"), "downarrow": ("↓", "ord"),
    "in": ("∈", "rel"), "notin": ("∉", "rel"), "subset": ("⊂", "rel"),
    "subseteq": ("⊆", "rel"), "supset": ("⊃", "rel"), "supseteq": ("⊇", "rel"),
    "cup": ("∪", "bin"), "cap": ("∩", "bin"), "wedge": ("∧", "bin"),
    "land": ("∧", "bin"), "vee": ("∨", "bin"), "lor": ("∨", "bin"),
    "lnot": ("¬", "ord"), "neg": ("¬", "ord"), "top": ("⊤", "ord"),
    "bot": ("⊥", "ord"), "angle": ("∠", "ord"),
    "therefore": ("∴", "rel"), "because": ("∵", "rel"), "forall": ("∀", "ord"),
    "exists": ("∃", "ord"), "emptyset": ("∅", "ord"), "varnothing": ("∅", "ord"),
    "infty": ("∞", "ord"), "partial": ("∂", "ord"), "nabla": ("∇", "ord"),
    "prime": ("′", "ord"), "hbar": ("ℏ", "ord"), "ell": ("ℓ", "ord"),
    "aleph": ("ℵ", "ord"), "degree": ("°", "ord"),
    "ldots": ("…", "inner"), "dots": ("…", "inner"), "cdots": ("⋯", "inner"),
    "vdots": ("⋮", "ord"), "ddots": ("⋱", "ord"),
    "langle": ("⟨", "open"), "rangle": ("⟩", "close"),
    "lceil": ("⌈", "open"), "rceil": ("⌉", "close"),
    "lfloor": ("⌊", "open"), "rfloor": ("⌋", "close"),
    "lbrace": ("{", "open"), "rbrace": ("}", "close"),
    "lbrack": ("[", "open"), "rbrack": ("]", "close"),
    "vert": ("|", "ord"), "lvert": ("|", "open"), "rvert": ("|", "close"),
    "mvert": ("|", "ord"), "Vert": ("‖", "ord"), "lVert": ("‖", "open"),
    "rVert": ("‖", "close"), "mVert": ("‖", "ord"),
    "backslash": ("\\", "ord"),
    # 间距：真排版用宽度比例，别用空格字符（HTML 会把连写的空格并掉）
    "quad": ("\u2003", "space"), "qquad": ("\u2003", "space"),
}
BIGOP = {"sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬",
         "oint": "∮", "bigcup": "⋃", "bigcap": "⋂"}
LIMITED = {"sum", "prod", "coprod", "bigcup", "bigcap"}   # display 下极限上下放
OPNAME = {"log", "ln", "lg", "exp", "det", "dim", "ker", "deg", "gcd", "arg",
          "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos",
          "arctan", "sinh", "cosh", "tanh", "Pr", "mod", "min", "max", "sup",
          "inf", "lim"}
RAW = {"text", "textrm", "mbox", "textnormal", "mathrm", "mathtt", "mathsf",
       "operatorname"}
BOLD = {"mathbf", "bm", "boldsymbol", "bfseries"}
BB = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "H": "ℍ"}
CAL = {"R": "ℛ", "L": "ℒ", "E": "ℰ", "B": "ℬ", "F": "ℱ", "M": "ℳ"}
ACCENT = {"hat": "^", "widehat": "^", "tilde": "~", "widetilde": "~",
          "bar": "‾", "overline": "‾", "vec": "→", "dot": "˙", "ddot": "¨",
          "check": "ˇ", "acute": "´"}
# 合并符号：文本后端用它们，图后端自己画
ACCENT_COMBINING = {"^": "̂", "~": "̃", "‾": "̄", "˙": "̇", "¨": "̈",
                    "ˇ": "̌", "´": "́", "→": "⃗"}
# \left( ... \right) 的定界符要跟着内容加高，普通 () 不会 —— 这是两回事，
# 所以 left/right 不能一丢了之：吃掉命令，把紧跟的那个括号标成 dsl（可加高）。
DELIM_STRETCH = {"left", "right", "middle"}
SKIP = {"big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
        "biggl", "biggr", "Biggl", "Biggr", "limits", "nolimits",
        "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
        "nonumber", "notag", "centering", "mathopen", "mathclose",
        "mathbin", "mathrel", "mathop"}
SKIP_ARG = {"begin", "end", "label", "ref", "eqref", "tag"}
ESC = {"|": ("‖", "ord"), "{": ("{", "open"), "}": ("}", "close"),
       "%": ("%", "ord"), "&": ("&", "ord"), "#": ("#", "ord"),
       "_": ("_", "ord"), "$": ("$", "ord"), "*": ("*", "bin"),
       "^": ("^", "ord"), "~": (" ", "space")}
SP = {",": 0.17, ";": 0.28, ":": 0.22, " ": 0.25, "!": -0.1}

WORD_RE = re.compile(r"\\([A-Za-z]+|.)", re.S)
TEXT_ESC_RE = re.compile(r"\\([\\_%$#&{}~^,;:!])")

# 节点：
#   ('g', kind, text, style)      一个字形串；kind: ord/bin/rel/open/close/punct/inner
#   ('run', [node...])            水平序列（隐式分组）
#   ('script', base, sub|None, sup|None)
#   ('frac', num, den, has_bar)
#   ('sqrt', body)
#   ('accent', body, mark)
#   ('big', glyph, sub|None, sup|None)   大算子，极限上下放
#   ('op', text)                  正体函数名
#   ('lim', text, base)           \xrightarrow{label}：标注在箭头上方
#   ('sp', em)                    固定间距
#   ('dl', ch)                    括号，会跟着同排内容加高

_GLYPH = ("g", "run", "script", "frac", "sqrt", "accent", "big", "op", "lim",
          "sp", "dl")


def _group(s: str, i: int) -> tuple[str, int]:
    """读 s[i] 处的一个参数：`{...}` 整段，或跳过空格后的单个记号。"""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth, k = 1, i + 1
        while k < len(s) and depth:
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
            k += 1
        return s[i + 1:k - 1], k
    if s[i] == "\\":
        m = WORD_RE.match(s, i)
        if m:
            return m.group(0), m.end()
    return s[i], i + 1


def _nodes(s: str) -> tuple:
    return ("run", parse(s))


def _trail_scripts(src: str, i: int) -> tuple[tuple | None, tuple | None, int]:
    """吃掉紧跟在命令后面的 `_{}` / `^{}`，作为它的上下限。"""
    sub = sup = None
    while i < len(src):
        if src[i] == " ":
            i += 1
            continue
        if src[i] in "^_" and (sup if src[i] == "^" else sub) is None:
            arg, j = _group(src, i + 1)
            if src[i] == "^":
                sup = _nodes(arg)
            else:
                sub = _nodes(arg)
            i = j
            continue
        break
    return sub, sup, i


def _delim_after(src: str, i: int) -> tuple[str, int]:
    r"""读 \left / \right 后面那个定界符：可能是 `(`、`\{`、`\langle`、`.`。"""
    if i >= len(src):
        return "", i
    c = src[i]
    if c == "\\":
        m = WORD_RE.match(src, i)
        if not m:
            return "", i + 1
        name = m.group(1)
        if name in SYM:
            return SYM[name][0], m.end()
        return (name if len(name) == 1 else ""), m.end()
    if c == ".":                          # \left. 是占位的隐形括号
        return "", i + 1
    return c, i + 1


def parse(src: str) -> list:
    r"""LaTeX 片段 -> 节点列表。

    下标推进只有一条规矩：**每个分支自己把 i 推到下一个未读字符**，循环末尾不再
    偷偷 +1。之前那里有个 `i += 1`，和分支里的 `i = j` 叠在一起，等于每个命令后面
    紧跟的那个字符都被吃掉 —— `\hat{y}_i` 的下划线就是这么丢的。
    """
    out: list = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            m = WORD_RE.match(src, i)
            if not m:
                i += 1
                continue
            name, i = m.group(1), m.end()
            if name == "\\":
                out.append(("sp", 0.0))
            elif name in DELIM_STRETCH:
                ch, i = _delim_after(src, i)
                if ch:
                    out.append(("dsl", ch))
            elif name in SKIP:
                pass
            elif name in SKIP_ARG:
                _, i = _group(src, i)
            elif name in BIGOP:
                sub, sup, i = _trail_scripts(src, i)
                out.append(("big", BIGOP[name], sub, sup))
            elif name in SYM:
                ch, kind = SYM[name]
                if kind == "space":
                    out.append(("sp", 0.6 if name == "quad" else 1.2))
                else:
                    out.append(("g", kind, ch, "up"))
            elif name in OPNAME:
                sub, sup, i = _trail_scripts(src, i)
                out.append(("script", ("op", name), sub, sup)
                           if (sub or sup) else ("op", name))
            elif name in RAW:
                arg, i = _group(src, i)
                out.append(("g", "ord",
                            TEXT_ESC_RE.sub(lambda mm: mm.group(1), arg), "up"))
            elif name in BOLD:
                arg, i = _group(src, i)
                out.append(("run", _bold(parse(arg))))
            elif name == "mathbb":
                arg, i = _group(src, i)
                out.append(("g", "ord",
                            "".join(BB.get(ch, ch) for ch in arg), "up"))
            elif name == "mathcal":
                arg, i = _group(src, i)
                out.append(("g", "ord",
                            "".join(CAL.get(ch, ch) for ch in arg), "up"))
            elif name in ACCENT:
                arg, i = _group(src, i)
                out.append(("accent", _nodes(arg), ACCENT[name]))
            elif name in ("frac", "dfrac", "tfrac", "cfrac"):
                a, i = _group(src, i)
                b, i = _group(src, i)
                out.append(("frac", _nodes(a), _nodes(b), True))
            elif name == "binom":
                a, i = _group(src, i)
                b, i = _group(src, i)
                out.append(("dl", "("))
                out.append(("frac", _nodes(a), _nodes(b), False))
                out.append(("dl", ")"))
            elif name == "sqrt":
                if src[i:i + 1] == "[":            # \sqrt[3]{x} 的次数先不排
                    k = src.find("]", i)
                    i = k + 1 if k > 0 else i + 1
                arg, i = _group(src, i)
                out.append(("sqrt", _nodes(arg)))
            elif name in ("xrightarrow", "overrightarrow", "longrightarrow"):
                arg, i = _group(src, i)
                out.append(("lim", arg.strip(), ("g", "rel", "→", "up")))
            elif name in ("xleftarrow", "overleftarrow"):
                arg, i = _group(src, i)
                out.append(("lim", arg.strip(), ("g", "rel", "←", "up")))
            elif name in ("underbrace", "overbrace", "stackrel", "substack",
                          "phantom", "mathstrut"):
                arg, i = _group(src, i)
                out.extend(parse(arg))
            elif len(name) == 1:
                ch, kind = ESC.get(name, (name, "ord"))
                if kind == "space":
                    out.append(("sp", SP.get(name, 0.25)))
                elif kind in ("open", "close"):
                    out.append(("dl", ch))
                else:
                    out.append(("g", kind, ch, "up"))
            else:
                out.append(("g", "ord", name, "up"))     # 认不出：留字，别吞
        elif c in "^_":
            arg, j = _group(src, i + 1)
            i = j
            if c == "^" and arg.strip() in (r"\circ", "circ"):
                out.append(("g", "ord", "°", "up"))   # 度数不是上标
                continue
            script = _nodes(arg)
            base = out[-1] if out else None
            if base is not None and base[0] == "script":
                want = 3 if c == "^" else 2
                if base[want] is None:
                    out[-1] = _with_script(base, c, script)
                else:
                    out.append(_script_on(("g", "ord", "", "up"), c, script))
            elif base is not None and base[0] in _SCRIPTABLE:
                out[-1] = _script_on(base, c, script)
            else:
                out.append(_script_on(("g", "ord", "", "up"), c, script))
        elif c == "{":
            arg, i = _group(src, i)
            out.extend(parse(arg))
        elif c == "}":
            i += 1
        elif c == "&":
            out.append(("sp", 0.0))
            i += 1
        elif c.isspace():
            out.append(("sp", 0.18))
            i += 1
        elif c.isascii() and c.isalpha():
            j = i
            while j < n and src[j].isascii() and src[j].isalpha():
                j += 1
            out.append(("g", "ord", src[i:j], "it"))
            i = j
        elif c.isdigit():
            j = i
            while j < n and src[j].isdigit():
                j += 1
            out.append(("g", "ord", src[i:j], "up"))
            i = j
        elif c in ".,;:":
            out.append(("g", "punct", c, "up"))
            i += 1
        elif c in "([{":
            out.append(("dl", c))
            i += 1
        elif c in ")]}":
            out.append(("dl", c))
            i += 1
        elif c in "|":
            out.append(("g", "ord", "|", "up"))
            i += 1
        elif c in "+-×":
            out.append(("g", "bin", "−" if c == "-" else c, "up"))
            i += 1
        elif c in "=<>":
            out.append(("g", "rel", c, "up"))
            i += 1
        else:
            out.append(("g", "ord", c, "up"))
            i += 1
    return _tidy(out)


_SCRIPTABLE = ("g", "op", "run", "frac", "sqrt", "script", "big", "accent",
               "lim")


def _script_on(base, c, script):
    return ("script", base, script if c == "_" else None,
            script if c == "^" else None)


def _with_script(node, c, script):
    """给已经有下标的节点补上标（或反过来）。"""
    if c == "^":
        return ("script", node[1], node[2], script)
    return ("script", node[1], script, node[3])


def _bold(nodes: list) -> list:
    out = []
    for nd in nodes:
        if nd[0] == "g":
            out.append(("g", nd[1], nd[2], "bf"))
        elif nd[0] == "run":
            out.append(("run", _bold(nd[1])))
        elif nd[0] == "script":
            out.append(("script", _one(_bold([nd[1]])), nd[2], nd[3]))
        else:
            out.append(nd)
    return out


def _one(nodes: list):
    return nodes[0] if len(nodes) == 1 else ("run", nodes)


def _tidy(nodes: list) -> list:
    """合并相邻空格、丢掉空节点。"""
    out: list = []
    for nd in nodes:
        if nd[0] == "sp" and out and out[-1][0] == "sp":
            out[-1] = ("sp", max(out[-1][1], nd[1]))
        elif nd[0] == "g" and not nd[2]:
            continue
        else:
            out.append(nd)
    return out


# ---------------------------------------------------------------- 文本后端
def _esc(s: str) -> str:
    import html
    return html.escape(s, quote=False)


def to_html(src: str) -> str:
    """退化成 Unicode + <sup>/<sub>。大纲标签和兜底走这条。"""
    return _run_html(parse(src))


def _run_html(nodes: list) -> str:
    out = []
    for nd in nodes:
        k = nd[0]
        if k == "g":
            txt = _esc(nd[2])
            if nd[3] == "it":
                txt = f"<i>{txt}</i>"
            elif nd[3] == "bf":
                txt = f"<b>{txt}</b>"
            out.append(txt)
        elif k == "op":
            out.append(_esc(nd[1]))
        elif k == "run":
            out.append(_run_html(nd[1]))
        elif k == "script":
            _, base, sub, sup = nd
            b = _run_html([base]) if base[0] != "run" else _run_html(base[1])
            if sub:
                b += f"<sub>{_run_html([sub]) if sub[0] != 'run' else _run_html(sub[1])}</sub>"
            if sup:
                b += f"<sup>{_run_html([sup]) if sup[0] != 'run' else _run_html(sup[1])}</sup>"
            out.append(b)
        elif k == "frac":
            _, a, b, _bar = nd
            out.append("(%s)/(%s)" % (_run_html([a]) if a[0] != "run"
                                      else _run_html(a[1]),
                                      _run_html([b]) if b[0] != "run"
                                      else _run_html(b[1])))
        elif k == "sqrt":
            out.append("√(" + _run_html(nd[1][1]) + ")")
        elif k == "accent":
            body = _run_html(nd[1][1])
            mark = ACCENT_COMBINING.get(nd[2], "")
            out.append(body + (_esc(mark) if nd[2] != "‾" else body and mark))
        elif k == "big":
            _, glyph, sub, sup = nd
            s = _esc(glyph)
            if sub:
                s += f"<sub>{_run_html(sub[1])}</sub>"
            if sup:
                s += f"<sup>{_run_html(sup[1])}</sup>"
            out.append(s)
        elif k == "lim":
            _, label, base = nd
            out.append(_run_html([base]) +
                       ("(%s)" % _run_html(parse(label)) if label else ""))
        elif k == "sp":
            out.append(" " if nd[1] > 0 else "")
        elif k in ("dl", "dsl"):
            out.append(_esc(nd[1]))
    return "".join(out)


# ---------------------------------------------------------------- 版式后端
# 盒子模型：每个盒子记「宽 / 基线以上多高 / 基线以下多深」，绘制时给一个基线原点。
# 一个串里可能同时有 x、∑ 和「因为」，所以文字要按字符类切段、逐段配字体。
SERIF = "Cambria"
CJK = "Microsoft YaHei"
SYMFONT = "Segoe UI Symbol"
SCRIPT = 0.72          # 上下标缩放
SUP_RAISE = 0.50       # 上标基线抬多少（相对父字号）
SUB_DROP = 0.24
AXIS = 0.26            # 分式横线相对基线的高度
FRAC_PAD = 0.14
BIG_SCALE = 1.5        # 大算子字号
RULE = 0.055           # 线宽（相对字号）


class Box:
    __slots__ = ("w", "a", "d", "fn")

    def __init__(self, w: float, a: float, d: float, fn=None):
        self.w, self.a, self.d, self.fn = float(w), float(a), float(d), fn

    def paint(self, p, x, y) -> None:      # noqa: ANN001
        if self.fn:
            self.fn(p, x, y)


class Ctx:
    """一套字号/颜色下的字体与量尺。整个缓存挂在调用方（页面）上复用。"""

    def __init__(self, px: float, color: str, display: bool = False):
        self.px = float(px)
        self.color = QColor(color)
        self.display = bool(display)
        self._f: dict = {}

    def font(self, style: str, size: float) -> QFont:
        key = (style, round(size, 2))
        f = self._f.get(key)
        if f is None:
            f = QFont()
            f.setPixelSize(max(1, int(round(size))))
            if style == "cjk":
                f.setFamily(CJK)
            elif style == "sym":
                f.setFamily(SYMFONT)
            else:
                f.setFamily(SERIF)
                if style.startswith("it"):
                    f.setItalic(True)
            if style.endswith("bf"):
                f.setBold(True)
            f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            self._f[key] = f
        return f

    def m(self, style: str, size: float) -> QFontMetricsF:
        key = ("m", style, round(size, 2))
        got = self._f.get(key)
        if got is None:
            got = QFontMetricsF(self.font(style, size))
            self._f[key] = got
        return got


def _cls(ch: str) -> str:
    o = ord(ch)
    if o < 128:
        return "asc"
    if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
        return "cjk"
    return "sym"


def _style_for(cls: str, style: str) -> str:
    if cls == "cjk":
        return "cjkbf" if style == "bf" else "cjk"
    if cls == "sym":
        return "symbf" if style == "bf" else "sym"
    if style == "bf":
        return "bf"
    return style if style in ("it", "up") else "up"


def _runs(text: str, style: str, size: float, ctx: Ctx):
    out = []
    cur = ""
    cls = None
    for ch in text:
        k = _cls(ch)
        if k != cls:
            if cur:
                out.append((cls, cur))
            cur, cls = ch, k
        else:
            cur += ch
    if cur:
        out.append((cls, cur))
    return [(s, _style_for(c, style), size) for c, s in out if s]


def _text_box(text: str, style: str, size: float, ctx: Ctx) -> Box:
    segs = _runs(text, style, size, ctx)
    x = 0.0
    parts = []
    a = size * 0.42
    d = size * 0.05
    for s, st, sz in segs:
        mm = ctx.m(st, sz)
        parts.append((x, st, sz, s))
        a = max(a, mm.ascent())
        d = max(d, mm.descent())
        x += mm.horizontalAdvance(s)

    def paint(p, bx, by):
        for off, st, sz, s in parts:
            p.setFont(ctx.font(st, sz))
            p.drawText(QPointF(bx + off, by), s)

    return Box(x, a, d, paint if parts else None)


def _kind(node) -> str:
    k = node[0]
    if k == "g":
        return node[1]
    if k in ("dl", "dsl"):
        return "open" if node[1] in "([{\u27e8" else (
            "close" if node[1] in ")]}\u27e9" else "ord")
    if k == "run":
        return _kind(node[1][-1]) if node[1] else "ord"
    return "ord"


def _gap(prev, nxt, px: float) -> float:
    g = 0.0
    for nd in (prev, nxt):
        if nd is None:
            continue
        k = _kind(nd)
        if k == "bin":
            g = max(g, 0.22 * px)
        elif k == "rel":
            g = max(g, 0.28 * px)
        elif k == "punct" and nd is prev:
            g = max(g, 0.16 * px)
    return g


def _hlist(nodes, size: float, ctx: Ctx, max_w: float = 0.0) -> Box:
    """水平序列：共用一条基线，括号按同排最高内容加高并垂直居中。

    max_w 只从 render() 传进来：一条公式排不下就按关系符/逗号断成几行。
    必须在源头断行 —— Qt 排行内图片时按图片自身的像素宽度算文档宽度，
    写 `width=` 和调 devicePixelRatio 都压不住它（实测虚出 321~510px 滚动条）。
    """
    if max_w > 0:
        one = _hlist(nodes, size, ctx, 0.0)
        if one.w > max_w:
            lines = _wrap(nodes, size, ctx, max_w)
            if len(lines) > 1:
                return _stack(lines, size, ctx)
        return one
    if not nodes:
        return Box(0.0, size * 0.42, size * 0.05, None)
    items = []
    for nd in nodes:
        if nd[0] == "sp":
            items.append((nd, Box(max(0.0, nd[1] * size), 0.0, 0.0, None)))
        else:
            items.append((nd, _measure(nd, size, ctx)))
    body = [b for nd, b in items if nd[0] not in ("dl", "dsl")]
    tall_a = max([b.a for b in body], default=size * 0.42)
    tall_d = max([b.d for b in body], default=size * 0.05)
    w = 0.0
    a = size * 0.42
    d = size * 0.05
    placed = []
    prev = None
    for idx, (nd, b) in enumerate(items):
        if nd[0] != "sp":
            nxt = items[idx + 1][0] if idx + 1 < len(items) else None
            w += _gap(prev, nxt, size)
        dy = 0.0
        if nd[0] == "dsl":
            # 只有 \left( ... \right) 那种才跟着内容加高，普通 () 保持原字号
            need = tall_a + tall_d
            if need > b.a + b.d > 0:
                b = _text_box(nd[1], "up",
                              size * min(3.0, need / (b.a + b.d) * 0.92), ctx)
            dy = ((tall_a - tall_d) - (b.a - b.d)) / 2.0
        placed.append((w, dy, b))
        a = max(a, b.a + dy)
        d = max(d, b.d - dy)
        w += b.w
        if nd[0] != "sp":
            prev = nd

    def paint(p, x0, y0):
        for off, dy, b in placed:
            b.paint(p, x0 + off, y0 + dy)

    return Box(w, a, d, paint)


def _wrap(nodes, size: float, ctx: Ctx, max_w: float) -> list:
    """在 = ≤ + , 这类地方把长公式折成几行，每行的宽度尽量贴近 max_w。"""
    items = [nd for nd in nodes]
    chunks: list[list] = []
    cur: list = []
    for nd in items:
        cur.append(nd)
        if _kind(nd) in ("rel", "bin", "punct") or nd[0] == "sp":
            w = _hlist(cur, size, ctx).w
            if w > max_w * 0.72:
                chunks.append(cur)
                cur = []
    if cur:
        if chunks and _hlist(cur, size, ctx).w < size * 1.2:
            chunks[-1].extend(cur)          # 尾巴太短就别单开一行
        else:
            chunks.append(cur)
    return chunks


def _stack(lines, size: float, ctx: Ctx) -> Box:
    """把折好的几行竖着摞起来，行距留 0.3 个字。"""
    boxes = [_hlist(ln, size, ctx) for ln in lines]
    gap = 0.30 * size
    w = max(b.w for b in boxes)
    total = sum(b.a + b.d for b in boxes) + gap * (len(boxes) - 1)
    offs = []
    y = 0.0
    for b in boxes:
        offs.append(y)
        y += b.a + b.d + gap
    first = boxes[0]

    def paint(p, x0, y0):
        yy = y0
        for b, off in zip(boxes, offs):
            b.paint(p, x0 + (w - b.w) / 2, yy + b.a)
            yy += b.a + b.d + gap

    return Box(w, first.a + (total - first.a - first.d), boxes[-1].d, paint)


def _measure(node, size: float, ctx: Ctx) -> Box:
    k = node[0]
    if k == "g":
        return _text_box(node[2], node[3], size, ctx)
    if k == "op":
        return _text_box(node[1], "up", size, ctx)
    if k == "run":
        return _hlist(node[1], size, ctx)
    if k == "sp":
        return Box(max(0.0, node[1] * size), 0.0, 0.0, None)
    if k in ("dl", "dsl"):
        return _text_box(node[1], "up", size, ctx)
    if k == "script":
        return _script(node[1], node[2], node[3], size, ctx)
    if k == "frac":
        return _frac(node[1], node[2], node[3], size, ctx)
    if k == "sqrt":
        return _sqrt(node[1], size, ctx)
    if k == "accent":
        return _accent(node[1], node[2], size, ctx)
    if k == "big":
        return _big(node[1], node[2], node[3], size, ctx)
    if k == "lim":
        return _lim(node[1], node[2], size, ctx)
    return Box(0.0, size * 0.4, size * 0.05, None)


def _script(base, sub, sup, size: float, ctx: Ctx) -> Box:
    b = _measure(base, size, ctx)
    ss = size * SCRIPT
    sb = _measure(sub, ss, ctx) if sub else None
    tb = _measure(sup, ss, ctx) if sup else None
    raise_ = SUP_RAISE * size
    drop = SUB_DROP * size
    parts = [(0.0, 0.0, b)]
    a, d = b.a, b.d
    if tb:
        parts.append((b.w, -raise_, tb))
        a = max(a, raise_ + tb.a)
        d = max(d, tb.d - raise_)
    if sb:
        parts.append((b.w, drop, sb))
        a = max(a, sb.a - drop)
        d = max(d, drop + sb.d)
    side = max(tb.w if tb else 0.0, sb.w if sb else 0.0)
    w = b.w + side

    def paint(p, x0, y0):
        for off, dy, box in parts:
            box.paint(p, x0 + off, y0 + dy)

    return Box(w, a, d, paint)


def _frac(num, den, bar, size: float, ctx: Ctx) -> Box:
    inner = size if ctx.display else size * 0.92
    nb = _measure(num, inner, ctx)
    db = _measure(den, inner, ctx)
    axis = AXIS * size
    gap = FRAC_PAD * size
    pad = 0.12 * size
    w = max(nb.w, db.w) + 2 * pad
    ny = axis + gap + nb.d            # 分子基线在盒基线之上多少
    dy = gap + db.a - axis            # 分母基线在盒基线之下多少
    a = ny + nb.a
    d = dy + db.d
    t = max(1.0, RULE * size)

    def paint(p, x0, y0):
        nb.paint(p, x0 + (w - nb.w) / 2, y0 - ny)
        db.paint(p, x0 + (w - db.w) / 2, y0 + dy)
        if bar:
            pen = p.pen()
            pen.setColor(ctx.color)
            pen.setWidthF(t)
            p.setPen(pen)
            y = y0 - axis
            p.drawLine(QPointF(x0 + pad * 0.3, y),
                       QPointF(x0 + w - pad * 0.3, y))

    return Box(w, a, d, paint)


def _sqrt(body, size: float, ctx: Ctx) -> Box:
    """根号：一个勾子 + 盖住被开方数的上横线。"""
    b = _measure(body, size, ctx)
    t = max(1.0, RULE * size)
    gap = 0.18 * size
    hook = 0.62 * size
    w = b.w + hook + 0.16 * size
    a = b.a + gap
    d = b.d + 0.10 * size
    bx = hook + 0.06 * size

    def paint(p, x0, y0):
        pen = p.pen()
        pen.setColor(ctx.color)
        pen.setWidthF(t)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        top = y0 - a + t
        bot = y0 + d - 0.12 * size
        path = QPainterPath()
        path.moveTo(x0 + 0.04 * size, top + 0.42 * (bot - top))
        path.lineTo(x0 + 0.17 * size, bot)
        path.lineTo(x0 + hook * 0.72, top)
        path.lineTo(x0 + w - 0.03 * size, top)
        p.drawPath(path)
        b.paint(p, x0 + bx, y0)

    return Box(w, a, d, paint)



def _accent(body, mark, size: float, ctx: Ctx) -> Box:
    b = _measure(body, size, ctx)
    if mark == "‾":
        t = max(1.0, RULE * size)
        a = b.a + 0.14 * size + t

        def paint_rule(p, x0, y0):
            pen = p.pen()
            pen.setColor(ctx.color)
            pen.setWidthF(t)
            p.setPen(pen)
            y = y0 - a + t
            p.drawLine(QPointF(x0, y), QPointF(x0 + b.w, y))
            b.paint(p, x0, y0)

        return Box(b.w, a, b.d, paint_rule)
    mb = _text_box(mark, "up", size * 0.72, ctx)
    lift = b.a + 0.02 * size
    a = lift + mb.a * 0.62

    def paint_mark(p, x0, y0):
        mb.paint(p, x0 + (b.w - mb.w) / 2, y0 - lift + mb.d * 0.55)
        b.paint(p, x0, y0)

    return Box(b.w, a, b.d, paint_mark)


def _big(glyph, sub, sup, size: float, ctx: Ctx) -> Box:
    if not ctx.display or (sub is None and sup is None):
        return _script(("g", "ord", glyph, "up"), sub, sup, size, ctx)
    g = _text_box(glyph, "up", size * BIG_SCALE, ctx)
    ss = size * 0.72
    sb = _measure(sub, ss, ctx) if sub else None
    tb = _measure(sup, ss, ctx) if sup else None
    gap = 0.10 * size
    w = max(g.w, sb.w if sb else 0.0, tb.w if tb else 0.0) + 0.12 * size
    a = g.a + (tb.a + tb.d + gap if tb else 0.0)
    d = g.d + (sb.a + sb.d + gap if sb else 0.0)

    def paint(p, x0, y0):
        g.paint(p, x0 + (w - g.w) / 2, y0)
        if tb:
            tb.paint(p, x0 + (w - tb.w) / 2, y0 - g.a - gap - tb.d)
        if sb:
            sb.paint(p, x0 + (w - sb.w) / 2, y0 + g.d + gap + sb.a)

    return Box(w, a, d, paint)


def _lim(label, base, size: float, ctx: Ctx) -> Box:
    b = _measure(base, size, ctx)
    lab = _hlist(parse(label), size * 0.68, ctx) if label.strip() else None
    w = b.w + (lab.w + 0.08 * size if lab else 0.0)
    a = b.a + (lab.a + 0.10 * size if lab else 0.0)

    def paint(p, x0, y0):
        b.paint(p, x0, y0)
        if lab:
            lab.paint(p, x0 + (b.w - lab.w) / 2, y0 - b.a - 0.10 * size - lab.d)

    return Box(w, a, b.d, paint)


def render(tex: str, *, px: float, color: str, display: bool = False,
           dpr: float = 1.0, pad_top: float = 0.0, max_w: float = 0.0):
    """把一段 LaTeX 画成透明底图，返回 (QImage, 基线 y)。

    基线必须交出去：Qt 把行内图片的**底边**钉在文字基线上，垫多少由调用方定。
    """
    ctx = Ctx(px, color, display)
    box = _hlist(parse(tex), px, ctx, max_w)
    w = box.w + 2.0
    h = box.a + box.d + 2.0 + pad_top
    img = QImage(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))),
                 QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    img.setDevicePixelRatio(dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setPen(ctx.color)
    box.paint(p, 1.0, pad_top + box.a + 1.0)
    p.end()
    return img, pad_top + box.a + 1.0
