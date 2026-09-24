"""解题代码块：等宽编辑器 + 按语言的语法高亮。

颜色一律从 theme.get() 取，换肤后调 refresh() 重建规则再重着色，不写死色值。
每种语言一份 spec：关键字、行注释、块注释、字符串引号、属性/装饰器写法。
认不出的（other）只染字符串和数字，不做半吊子高亮。
"""
from __future__ import annotations

import re

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import (
    QColor, QFont, QSyntaxHighlighter, QTextCharFormat,
)
from PySide6.QtWidgets import QPlainTextEdit

from . import theme

# C 系共用一大半关键字，拆开写只会有人漏
_C = set("""auto break case const continue default do else enum extern for goto if
    inline register return sizeof static struct switch typedef union volatile while
    int char float double void long short unsigned signed size_t bool true false
    NULL EOF stdin stdout stderr FILE""".split())
_JVM = set("""abstract assert boolean break byte case catch char class const continue
    default do double else enum extends final finally float for goto if implements
    import instanceof int interface long native new package private protected public
    return short static super switch synchronized this throw throws transient try
    var void volatile while true false null""".split())
_JS = set("""async await break case catch class const continue debugger default delete
    do else export extends false finally for function get if import in instanceof let
    new null of return set static super switch this throw true try typeof undefined
    var void while with yield NaN Infinity""".split())

_SPECS: dict[str, dict] = {
    "c": dict(kw=_C, line="//", block=("/*", "*/"), attr=r"^[ \t]*#\w+"),
    "cpp": dict(kw=_C | set("""alignas alignof bool catch class constexpr decltype
        delete dynamic_cast explicit export false final friend inline mutable
        namespace noexcept nullptr operator override private protected public
        reinterpret_cast static_cast template this throw true try typeid typename
        using virtual nullptr""".split()),
        line="//", block=("/*", "*/"), attr=r"^[ \t]*#\w+",
        types=set("""string vector pair map set unordered_map unordered_set queue
            stack deque tuple array optional variant shared_ptr unique_ptr
            make_pair make_tuple begin end size push_back emplace_back""".split())),
    "java": dict(kw=_JVM, line="//", block=("/*", "*/"), attr=r"@\w+"),
    "kotlin": dict(kw=set("""abstract actual annotation by break catch class companion
        const constructor continue crossinline data do else enum external false final
        finally for fun get if import in infix init inline inner interface internal
        is lateinit noinline null object open operator out override package private
        protected public reified return sealed set super suspend tailrec this throw
        true try typealias val var vararg when where while""".split()),
        line="//", block=("/*", "*/"), attr=r"@\w+"),
    "swift": dict(kw=set("""associatedtype break case catch class continue default deinit
        do else enum extension fallthrough false fileprivate for func guard if import
        in init inout internal let nil operator private protocol public repeat
        required return self static struct subscript super switch throw throws true
        try typealias var where while""".split()),
        line="//", block=("/*", "*/"), attr=r"@\w+"),
    "javascript": dict(kw=_JS, line="//", block=("/*", "*/")),
    "typescript": dict(kw=_JS | set("""abstract as declare enum implements interface
        keyof namespace readonly type public private protected satisfies infer
        never unknown any string number boolean""".split()),
        line="//", block=("/*", "*/"), attr=r"@\w+"),
    "go": dict(kw=set("""break case chan const continue default defer else fallthrough
        for func go goto if import interface map package range return select struct
        switch type var true false nil make new len cap append close panic
        recover""".split()),
        line="//", block=("/*", "*/"), attr=r"^[ \t]*//go:\w+",
        types=set("""string int int8 int16 int32 int64 uint uint8 uint16 uint32
            uint64 byte rune float32 float64 bool error""".split())),
    "rust": dict(kw=set("""as async await break const continue crate dyn else enum
        extern false fn for if impl in let loop match mod move mut pub ref return
        self static struct super trait true type unsafe use where while
        i32 u32 i64 u64 usize f64 String Vec Option Result Some None Ok Err""".split()),
        line="//", block=("/*", "*/"), attr=r"#\[?\w+"),
    "csharp": dict(kw=set("""abstract as base bool break byte case catch char checked
        class const continue decimal default delegate do double else enum event
        explicit extern false finally fixed float for foreach goto if implicit in int
        interface internal is lock long namespace new null object operator out override
        params private protected public readonly ref return sbyte sealed short sizeof
        stackalloc static string struct switch this throw true try typeof uint ulong
        unchecked unsafe ushort using var virtual void volatile while""".split()),
        line="//", block=("/*", "*/"), attr=r"@\w+"),
    "php": dict(kw=set("""abstract and array as break callable case catch class clone
        const continue declare default do echo else elseif empty enddeclare endfor
        endforeach endif endswitch endwhile extends final finally fn for foreach
        function global goto if implements include include_once instanceof insteadof
        interface isset list match namespace new or print private protected public
        readonly require require_once return static switch throw trait try unset use
        var while xor yield true false null int string bool""".split()),
        line="//", block=("/*", "*/"), attr=r"^[ \t]*#\w+"),
    "ruby": dict(kw=set("""alias and begin break case class def defined do else elsif
        end ensure false for if in module next nil not or redo rescue retry return
        self super then true undef unless until when while yield attr_accessor
        require puts""".split()),
        line="#", block=None),
    "python": dict(kw=set("""and as assert async await break class continue def del elif
        else except finally for from global if import in is lambda nonlocal not or
        pass raise return while yield with True False None self cls
        print len range sorted min max abs enumerate zip iter next repr format
        deque defaultdict heapq Counter""".split()),
        line="#", triple=True, attr=r"@[\w.]+",
        types=set("""int str list dict set tuple float bool bytes bytearray object
            frozenset""".split())),
    "sql": dict(kw=set("""select from where insert into values update delete create table
        drop alter add primary key foreign references index view join inner left right
        full outer on group by having order asc desc distinct union all and or not null
        as between in like exists case when then else end count sum avg min max limit
        offset constraint default unique check integer varchar text date timestamp""".split()),
        line="--", block=("/*", "*/"), upper_only=True),
    "bash": dict(kw=set("""if then else elif fi for while until do done case esac
        function in select time return break continue export local readonly unset echo
        printf read source alias cd pwd set shift trap true false""".split()),
        line="#", block=None),
    "other": dict(kw=set(), line="", block=None),
}

# 函数名：定义处跟着 def/func/function/fn，调用处是「标识符紧跟左括号」
_DEF_KW = r"(?:def|func|function|fn|sub|proc)"
_DEF_RE = QRegularExpression(r"\b%s\s+([A-Za-z_]\w*)" % _DEF_KW)
_CALL_RE = QRegularExpression(r"\b([A-Za-z_]\w*)(?=\s*\()")
_TYPE_RE = QRegularExpression(r"\b[A-Z][A-Za-z0-9_]*\b")
_NUM_RE = QRegularExpression(r"\b\d+(?:\.\d+)?\b")
_STR_DQ = QRegularExpression(r'"(?:\\.|[^"\\])*"')
_STR_SQ = QRegularExpression(r"'(?:\\.|[^'\\])*'")


def code_font(point: int = 10) -> QFont:
    f = QFont("Consolas")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(point)
    return f


def _fmt(color_key: str, bold: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(theme.get(color_key)))
    if bold:
        fmt.setFontWeight(QFont.Bold)
    return fmt


def _words_re(words) -> QRegularExpression:
    """关键字表拼成一个交替式；长的排前面，免得 int 抢走 integer。"""
    ordered = sorted(words, key=lambda w: (-len(w), w))
    return QRegularExpression(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in ordered))


class CodeHighlighter(QSyntaxHighlighter):
    """规则表按「当前主题 + 当前语言」重建，换肤后必须再 refresh() 一次。"""

    def __init__(self, doc):
        super().__init__(doc)
        self.language = "cpp"
        self._rules: list[tuple] = []
        self._def_fmt = _fmt("nlp_fg")
        self._blocks: list[tuple[str, str, QTextCharFormat]] = []

    def set_language(self, lang: str) -> None:
        self.language = lang or ""
        self.refresh()

    def refresh(self) -> None:
        spec = _SPECS.get(self.language) or _SPECS["other"]
        rules: list[tuple] = []
        # 名字类规则放最前，关键字放后面：同一串字符上关键字的颜色要盖过来
        rules.append((_CALL_RE, _fmt("nlp_fg"), 1))
        rules.append((_TYPE_RE, _fmt("nlp_fg"), 0))
        if spec.get("types"):
            rules.append((_words_re(spec["types"]), _fmt("nlp_fg"), 0))
        if spec["kw"]:
            rules.append((_words_re(spec["kw"]), _fmt("red", bold=True), 0))
        rules.append((_NUM_RE, _fmt("blue"), 0))
        rules.append((_STR_DQ, _fmt("green"), 0))
        rules.append((_STR_SQ, _fmt("green"), 0))
        if spec.get("attr"):
            rules.append((QRegularExpression(spec["attr"]), _fmt("amber"), 0))
        if spec["line"]:
            # 注释放最后：注释里夹的引号、关键字都不该再上色
            rules.append((QRegularExpression(re.escape(spec["line"]) + r".*"),
                          _fmt("muted"), 0))
        self._rules = rules
        comment = _fmt("muted")
        blocks: list[tuple[str, str, QTextCharFormat]] = []
        if spec.get("block"):
            op, cl = spec["block"]
            blocks.append((op, cl, comment))
        if spec.get("triple"):
            blocks.append(('"""', '"""', _fmt("green")))
            blocks.append(("'''", "'''", _fmt("green")))
        self._blocks = blocks
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:      # noqa: N802
        for pattern, fmt, group in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                start = m.capturedStart(group)
                length = m.capturedLength(group)
                if length > 0:
                    self.setFormat(start, length, fmt)
        if self.language in ("python",):
            dm = _DEF_RE.match(text)
            if dm.hasMatch():
                self.setFormat(dm.capturedStart(1), dm.capturedLength(1),
                               self._def_fmt)

        self.setCurrentBlockState(0)
        for i, (opener, closer, fmt) in enumerate(self._blocks):
            if self.previousBlockState() == i + 1:
                start = 0                 # 上一行开的那块还没闭合
            else:
                start = text.find(opener)
                if start < 0:
                    continue
            end = text.find(closer, start + len(opener))
            if end < 0:
                self.setFormat(start, len(text) - start, fmt)
                self.setCurrentBlockState(i + 1)
                break
            self.setFormat(start, end + len(closer) - start, fmt)


class CodeEdit(QPlainTextEdit):
    """代码输入框：不换行（横向滚动）、Tab 缩进 4 格、带语法高亮。"""

    def __init__(self, parent=None, language: str = "cpp"):
        super().__init__(parent)
        self.setObjectName("CodeBlock")
        self.setFont(code_font())
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabChangesFocus(False)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        try:
            self.document().setIndentWidth(4)
        except AttributeError:            # Qt < 6.3
            pass
        self.setPlaceholderText("代码")
        self.highlighter = CodeHighlighter(self.document())
        self.highlighter.language = language
        self.highlighter.refresh()

    def set_language(self, lang: str) -> None:
        if self.highlighter.language != lang:
            self.highlighter.set_language(lang)

    def refresh_theme(self) -> None:
        self.highlighter.refresh()
