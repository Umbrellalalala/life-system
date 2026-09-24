"""Obsidian 对接层：本应用不再自己存笔记，只做「读 vault + 跳 Obsidian + 把结构化数据写成 md」。

分工：Obsidian 负责写和存，Life System 负责提醒 / 计时 / 看板。

三条安全约束（改这个文件前先读）：
- 导出的 md 只落在 ``<vault>/LifeSystem/`` 子树里，绝不写到 vault 别处；
- 每个生成文件带 ``GEN_MARK`` 标记。**同名文件若是用户手写的（没有标记）就跳过**，
  不覆盖人手写的东西；
- 清理孤儿文件时也只删带标记的，标记文件不在本轮产物清单里才删。

vault 路径从 ``%APPDATA%/obsidian/obsidian.json`` 自动发现，用户可在设置页改。
"""
from __future__ import annotations

import html as h
import json
import os
import re
import subprocess
import threading
from datetime import date, datetime
from urllib.parse import quote, unquote

import markdown
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from . import db, mathtex, popups, services, sounds

EXPORT_DIR = "LifeSystem"
GEN_MARK = "generated-by-life-system"

_SKIP_DIRS = {".obsidian", ".git", ".trash", ".obsidian-recovery",
              "node_modules", "__pycache__"}
_MD_EXT = (".md", ".txt")

# 链接语法：[[目标]] / [[目标|别名]]，目标可能带 #标题 或 .md 后缀
_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_TAG_RE = re.compile(r"(?<!\S)#([^\s#]+)")
# Markdown 围栏：``` 或 ~~~，前面最多 3 个空格
_FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
# 行内标记：粗体/斜体/代码/删除线。比对标题文本时要去掉（顺序上 ** 要排在 * 前）
_INLINE_MARK_RE = re.compile(r"\*\*|__|~~|[*`_]")
# 文件名(小写) -> 绝对路径；由 iter_notes 同一次遍历建出来，不额外走一遍盘
_ATTACH_INDEX: dict[str, dict[str, str]] = {}

# 预览里的图片：统一按视口缩放后显示，落不了盘的退成一行灰字（见 render_html）
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_IMG_TAG_RE = re.compile(r"(<img[^>]*>)", re.I)
_SRC_RE = re.compile("""(?<![\\w-])src\\s*=\\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ALT_RE = re.compile("""(?<![\\w-])alt\\s*=\\s*(?:"([^"]*)"|'([^']*)')""", re.I)


# ---------------------------------------------------------------- vault 定位
def obsidian_config_dir() -> str:
    """Obsidian 自己的配置目录（里面有个 obsidian.json 记着所有 vault）。"""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    return os.path.join(appdata, "obsidian")


def discover_vaults() -> list[dict]:
    """从 Obsidian 配置里读出已注册的 vault：[{"name", "path"}]。"""
    path = os.path.join(obsidian_config_dir(), "obsidian.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for info in (data.get("vaults") or {}).values():
        p = (info or {}).get("path")
        if p and os.path.isdir(p):
            out.append({"name": os.path.basename(os.path.normpath(p)),
                        "path": os.path.abspath(p)})
    out.sort(key=lambda v: v["name"].lower())
    return out


def vault_path() -> str:
    """当前使用的 vault 根目录；没配置或目录不存在返回 ""。

    优先用户在设置里手选的，其次自动发现的第一个（Obsidian 最近打开的那个）。
    """
    saved = db.get_setting("obsidian_vault")
    if saved and os.path.isdir(saved):
        return saved
    found = discover_vaults()
    if found:
        return found[0]["path"]
    return ""


def set_vault_path(path: str) -> None:
    db.set_setting("obsidian_vault", path or "")
    clear_cache()


def vault_name(root: str | None = None) -> str:
    """obsidian:// 的 vault 参数。已注册的用注册名，否则用目录名。"""
    root = root or vault_path()
    for v in discover_vaults():
        if os.path.normcase(v["path"]) == os.path.normcase(root):
            return v["name"]
    return os.path.basename(os.path.normpath(root))


def export_root(root: str | None = None) -> str:
    return os.path.join(root or vault_path(), EXPORT_DIR)


# ---------------------------------------------------------------- 读与搜索
def _is_hidden(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS


def iter_notes(root: str | None = None) -> list[dict]:
    """遍历 vault 里的笔记，按「文件夹 / 标题」排序。

    返回 [{"rel": posix 相对路径, "name": 不含扩展名, "folder": 所在文件夹,
           "path": 绝对路径, "mtime": 修改时间}]
    """
    root = root or vault_path()
    if not root:
        return []
    out = []
    attach: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        for fn in filenames:
            if _is_hidden(fn):
                continue
            full = os.path.join(dirpath, fn)
            if not fn.lower().endswith(_MD_EXT):
                # 同一次遍历顺手记下非 md 附件，给 ![[图]] 按文件名找用
                attach.setdefault(fn.lower(), full)
                continue
            stem = os.path.splitext(fn)[0]
            out.append({
                "rel": f"{rel_dir}/{fn}" if rel_dir else fn,
                "name": stem,
                "folder": rel_dir,
                "path": full,
                "mtime": os.path.getmtime(full),
                # Windows 上 st_ctime 就是创建时间；POSIX 下是元数据改动时间，
                # 那时「创建时间」这一档排序会退化，不会报错。
                "ctime": os.path.getctime(full),
            })
    out.sort(key=lambda n: (n["folder"].lower(), n["name"].lower()))
    _ATTACH_INDEX[os.path.normcase(root)] = attach
    _prune_cache(root, out)
    return out


def _prune_cache(root: str, notes: list[dict]) -> None:
    """删掉的笔记要顺手从缓存里清出去，否则换一次库就攒一堆进不了内存的死键。"""
    alive = {n["path"] for n in notes}
    pre = os.path.normcase(root) + os.sep
    for key in [k for k in _TEXT_CACHE
                if os.path.normcase(k).startswith(pre) and k not in alive]:
        _TEXT_CACHE.pop(key, None)


def read_text(rel: str, root: str | None = None) -> str:
    root = root or vault_path()
    return _read_cached(os.path.join(root, rel.replace("/", os.sep)))


# ---------------------------------------------------------------- 文本缓存
# 一次全文搜索要把全库读一遍（实测 208 篇 / 3.1MB 冷读 0.7~7.4 秒），
# 不缓存的话搜索框每敲一个字符界面就冻一次。按 mtime + size 校验，
# 用户在 Obsidian 里改过就自动失效，不需要谁去通知它。
# links 是这篇里所有 [[双链]] 目标，反着算反向链接时不用再跑一遍正则。
_TEXT_CACHE: dict[str, dict] = {}
_MAX_CACHE_FILE = 4 * 1024 * 1024       # 单个文件超过这个大小不缓存，防极端情况


def _read_cached(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        _TEXT_CACHE.pop(path, None)
        return ""
    hit = _TEXT_CACHE.get(path)
    if hit is not None and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
        return hit["text"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        _TEXT_CACHE.pop(path, None)
        return ""
    if st.st_size <= _MAX_CACHE_FILE:
        _TEXT_CACHE[path] = {"mtime": st.st_mtime, "size": st.st_size,
                             "text": text, "links": None}
    return text


def _links_cached(path: str) -> list[tuple[str, int]]:
    """这篇笔记里的 [[双链]]，返回 [(目标, 1-based 行号)]；跟着文本一起失效。"""
    hit = _TEXT_CACHE.get(path)
    if hit is None:
        _read_cached(path)
        hit = _TEXT_CACHE.get(path)
        if hit is None:                 # 文件读不出来（被删 / 没权限）
            return []
    if hit["links"] is None:
        found: list[tuple[str, int]] = []
        for i, line in enumerate(hit["text"].splitlines()):
            if "[[" not in line:
                continue
            for m in _LINK_RE.finditer(line):
                found.append((m.group(1).strip(), i + 1))
        hit["links"] = found
    return hit["links"]


def _line_at(path: str, lineno: int) -> str:
    lines = _read_cached(path).splitlines()
    return lines[lineno - 1] if 0 < lineno <= len(lines) else ""


def clear_cache() -> None:
    """换库 / 需要强制重读时用。平时靠 mtime 校验，不需要手动调。"""
    _TEXT_CACHE.clear()
    _ATTACH_INDEX.clear()


def warm_cache_async() -> None:
    """后台线程先把全库读一遍。

    不这么做的话「刚打开应用就搜索」仍然要现场冷读 1 秒以上 —— 缓存只对
    第二次调用有用。读盘全在守护线程里，GUI 这边随时可能读到一半的条目，
    最坏情况是同一个文件被读两遍，不会读到脏数据。
    """
    root = vault_path()
    if not root or getattr(warm_cache_async, "_running", False):
        return
    warm_cache_async._running = True

    def run() -> None:
        try:
            for note in iter_notes(root):
                _read_cached(note["path"])
        finally:
            warm_cache_async._running = False

    threading.Thread(target=run, name="vault-warm", daemon=True).start()


def _snippet(line: str, kw_lower: str, width: int = 60) -> str:
    s = line.strip()
    i = s.lower().find(kw_lower)
    if i < 0:
        return s[:width] + ("…" if len(s) > width else "")
    start = max(0, i - width // 2)
    return ("…" if start else "") + s[start:start + width] + ("…" if len(s) > start + width else "")


def search(kw: str, root: str | None = None, limit: int = 200,
           notes: list[dict] | None = None) -> list[dict]:
    """全文搜索：正文命中优先，其次标题命中。

    返回 [{"note": iter_notes 的条目, "snippet": 命中行摘要, "line": 行号,
           "in_title": 是否只靠标题命中}]

    匹配用整篇 lower() + find()，不要写成「逐行 lower()」——那会在 3MB 的库上
    按行分配出几万个临时字符串，一次搜索 0.4 秒，搜索框每敲一个字符卡一次。
    notes 由调用方传进来（页面已经扫过一遍），省掉重复遍历目录。
    """
    root = root or vault_path()
    kw = kw.strip().lower()
    if not root or not kw:
        return []
    hits = []
    for note in (notes if notes is not None else iter_notes(root)):
        text = read_text(note["rel"], root)
        i = text.lower().find(kw)
        if i >= 0:
            start = text.rfind("\n", 0, i) + 1
            end = text.find("\n", i)
            line = text[start:end if end >= 0 else len(text)]
            hits.append({"note": note, "snippet": _snippet(line, kw),
                         "line": text.count("\n", 0, i) + 1,
                         "in_title": False})
            continue
        if kw in note["name"].lower() or kw in note["folder"].lower():
            hits.append({"note": note, "snippet": note["folder"] or "根目录",
                         "line": 0, "in_title": True})
    hits.sort(key=lambda h: (h["in_title"], -h["note"]["mtime"]))
    return hits[:limit]


def _target_variants(target: str) -> set[str]:
    """[[链接目标]] 可能指向的几种写法。"""
    t = target.strip()
    stem = t.rsplit("/", 1)[-1]
    stem = os.path.splitext(stem)[0]
    return {t, t + ".md", stem, os.path.splitext(t)[0]}


def resolve_link(target: str, root: str | None = None,
                 notes: list[dict] | None = None) -> str:
    """把 [[双链]] 目标解析成 vault 相对路径；找不到返回 ""。"""
    root = root or vault_path()
    if not root:
        return ""
    want = _target_variants(target)
    for note in (notes if notes is not None else iter_notes(root)):
        if note["rel"] in want or note["name"] in want:
            return note["rel"]
    return ""


def backlinks(rel: str, root: str | None = None,
              notes: list[dict] | None = None) -> list[dict]:
    """反向链接：vault 里哪些笔记用 [[…]] 指到了这篇。

    带 first 命中行号和那一行的摘要，光给个文件名看不出「是谁在什么语境下提到的」。
    """
    root = root or vault_path()
    notes = notes if notes is not None else iter_notes(root)
    note = next((n for n in notes if n["rel"] == rel), None)
    if note is None:
        return []
    want = {note["name"], note["rel"], os.path.splitext(note["rel"])[0]}
    out = []
    for other in notes:
        if other["rel"] == rel:
            continue
        hits = [(t, ln) for t, ln in _links_cached(other["path"])
                if t in want or os.path.splitext(t)[0] in want]
        if not hits:
            continue
        first_target, first_line = hits[0]
        out.append({"note": other, "count": len(hits), "line": first_line,
                    "snippet": _snippet(_line_at(other["path"], first_line),
                                        first_target.lower())})
    return out


def headings(rel: str, root: str | None = None) -> list[tuple[int, str, int]]:
    """标题层级 [(level, text, line)]，供右侧大纲。

    必须跳过 ``` / ~~~ 围栏里的内容：笔记里大量 Python 代码的 `# 注释` 会被误认成
    一级标题，一篇 700 行的能冒出 100 个假标题。
    """
    out = []
    fence = ""
    for i, line in enumerate(read_text(rel, root).splitlines()):
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)[0]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if fence:
            continue
        s = line.strip()
        if not s.startswith("#"):
            continue
        level = len(s) - len(s.lstrip("#"))
        title = s.lstrip("#").strip()
        if title and 1 <= level <= 4:
            out.append((level, title, i))
    return out


def plain_text(s: str) -> str:
    """去掉行内标记、把公式排成渲染后的样子，只用于比对和大纲标签。

    拿源文本直接和渲染后的块文本比字符串必然对不上：`**粗**` 渲染出来没有星号，
    `$V_{\\phi}$` 渲染出来是 `Vφ`。库里有 16 条标题带公式，不排这一步点就没反应。
    顺序不能反：先排公式，`x_{i}` 里的下划线才不会被当成斜体标记先被削掉。
    """
    s = _MATH_ANY_RE.sub(
        lambda m: _TAG_STRIP_RE.sub("", _tex(m.group(1) or m.group(2) or "")), s)
    return _INLINE_MARK_RE.sub("", s).strip()


def _local_image(src: str, note_rel: str, root: str) -> str:
    """把图片 src 解析成磁盘上的绝对路径；网络图和找不到的都返回 ""。

    网络图不发请求：这个库里 1000 多处图片引用几乎全是指向图床的 http 链接，
    真去拉取会让打开一篇笔记卡在网络请求上，而且离线时全是失败占位。
    """
    if not src or src.startswith(("http://", "https://", "data:", "file://")):
        return ""
    cleaned = unquote(src.split("#")[0].split("?")[0])
    base = os.path.dirname(os.path.join(root, note_rel.replace("/", os.sep)))
    for cand in (os.path.join(base, cleaned), os.path.join(root, cleaned)):
        full = os.path.normpath(cand)
        if os.path.isfile(full):
            return full
    # Obsidian 的 ![[图]] 是全库按文件名解析的，附件通常单独放一个目录
    idx = _ATTACH_INDEX.get(os.path.normcase(root)) or {}
    low = os.path.basename(cleaned).lower()
    hit = idx.get(low)
    if hit:
        return hit
    if not low.endswith(_IMG_EXT):
        for ext in _IMG_EXT:
            hit = idx.get(low + ext)
            if hit:
                return hit
    return ""


def image_refs(html: str) -> list[tuple[str, int]]:
    """渲染结果里的图片：[(src, Obsidian 的宽度提示)]，去重、保持出现顺序。

    本地图也走这条路：Qt 按图片原始尺寸排版，一张超宽的图就能把预览栏撑出
    横向滚动条，所以统一交给界面按视口缩放后再塞进文档资源表。
    宽度提示只当上限参考 —— 写进 `width=` 的话 Qt 会照着预留版面，同样会溢出。
    """
    out: list[tuple[str, int]] = []
    seen = set()
    for m in _IMG_TAG_RE.finditer(html):
        tag = m.group(1)
        src = _attr(tag, _SRC_RE)
        if not src or src in seen or src.startswith("mathtex:"):
            continue        # 公式图由页面自己画（见 mathtex），不归下载器管
        seen.add(src)
        hint = re.search(r'data-w="(\d+)"', tag)
        out.append((src, int(hint.group(1)) if hint else 0))
    return out


def stats(rel: str, root: str | None = None) -> str:
    """预览下面那一行：字数 / 行数 / 双链 / 标签。"""
    text = read_text(rel, root)
    if not text:
        return ""
    chars = len(re.sub(r"\s", "", text))
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    links = len(_LINK_RE.findall(text))
    tags = len(_TAG_RE.findall(text))
    bits = [f"{chars:,} 字", f"{lines} 行"]
    if links:
        bits.append(f"{links} 双链")
    if tags:
        bits.append(f"{tags} 标签")
    return " · ".join(bits)


# ---------------------------------------------------------------- 数学式
# 库里 13 篇笔记有 1154 处 LaTeX。两条路都实测过走不通：
# - 原样丢给 markdown：`x_{i}` 的下划线被当成斜体标记，一篇 444 条公式里有 10 条
#   被改写，渲染出 23 个来路不明的 <em>；
# - 引 matplotlib 排真公式：\text{} 里的中文丢字形，还要给 52MB 的分发包加几十 MB。
# 所以走中间路线：markdown 之前先把数学式整段摘走存好（顺手把围栏和行内码也遮住，
# 免得代码里的 $ 被误伤），渲染完再回填成 Unicode 符号 + <sup>/<sub>。
# 命令表是按这个库里实际出现的 54 个命令枚举的，认得的都覆盖到；
# 认不出的命令退成命令名本身 —— 宁可难看，不能像现在这样吃字。

_BLOCK_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$([^$\n]+?)\$(?!\$)")
# plain_text 用的合并版：一条标题里块式和行内式都可能出现
_MATH_ANY_RE = re.compile(r"\$\$(.+?)\$\$|(?<![\\$])\$([^$\n]+?)\$(?!\$)", re.S)
_INLINE_CODE_RE = re.compile(r"(?<!`)`{1,2}([^`\n]+?)`{1,2}(?!`)")
_MATH_SLOT_RE = re.compile(r"@@LTX(\d+)@@")
_MATH_P_RE = re.compile(r"<p>\s*(@@LTX(\d+)@@)\s*(?:<br\s*/?>\s*)*</p>")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _tex(s: str) -> str:
    """一小段 LaTeX -> 退化成文字的富文本（实现在 `mathtex`）。

    语法树和命令表都在 `lifeapp/mathtex.py`，那边一份树两个后端：这里是「退化成
    Unicode + <sup>/<sub>」的那一个，给大纲标签、传给 Obsidian 的标题、以及画图
    失败时兜底；正文预览走 `mathtex.render()` 画真版式。
    """
    try:
        return mathtex.to_html(s)
    except Exception:                   # 用户写得再怪也不该把预览弄崩
        return h.escape(s)


_MATH_HINT_RE = re.compile(r"[A-Za-z\\^_{}]")
_NUM_ONLY_RE = re.compile(r"[\d.,%+\-/*= ]+")


def _is_math(s: str) -> bool:
    """挡掉「价格是 $5 和 $10」：没有字母/记号线索的不算公式。

    光看「是不是纯数字」不够，中文夹在中间就漏了（`5 和` 会被当成公式，
    连带把后面的空格吃掉）。真公式总有拉丁字母或 `^_\\{}` 其中之一。
    """
    t = s.strip()
    return bool(t) and bool(_MATH_HINT_RE.search(t)) \
        and not _NUM_ONLY_RE.fullmatch(t)


def _extract_math(text: str) -> tuple[str, list[str]]:
    """把数学式换成 @@LTXn@@ 槽位，返回 (遮好文本, 每个槽位的 HTML)。

    先遮围栏和行内码再找公式：代码里的 `$` 成对出现时会被行内公式规则吃掉。
    """
    slots: list[str] = []
    code: list[str] = []
    lines: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)[0]
            fence = "" if fence and marker == fence else (fence or marker)
            code.append(line)
            lines.append("@@LTXK%d@@\n" % (len(code) - 1))
            continue
        if fence:
            code.append(line)
            lines.append("@@LTXK%d@@\n" % (len(code) - 1))
            continue
        def hide(mm: re.Match) -> str:
            code.append(mm.group(0))
            return "@@LTXK%d@@" % (len(code) - 1)
        lines.append(_INLINE_CODE_RE.sub(hide, line))
    masked = "".join(lines)

    def take(mm: re.Match, display: bool) -> str:
        src = mm.group(1)
        if not _is_math(src):
            return mm.group(0)
        try:
            html_txt = _tex(src.strip())
        except Exception:                   # 用户写得再怪也不该把预览弄崩
            html_txt = h.escape(src)
        slots.append((html_txt, src.strip()))
        return "@@LTX%d@@" % (len(slots) - 1)

    masked = _BLOCK_MATH_RE.sub(lambda m: take(m, True), masked)
    masked = _INLINE_MATH_RE.sub(lambda m: take(m, False), masked)
    return re.sub(r"@@LTXK(\d+)@@", lambda m: code[int(m.group(1))], masked), slots


def _restore_math(body: str, slots: list[str]) -> str:
    """markdown 渲染完，把槽位换回公式。

    这里给每段公式带上 `data-tex`：页面拿到 HTML 后会就地把它换成
    `mathtex.render()` 画出来的真版式图（见 note.NotePage._mathify）。
    带的是**已排好版的文字**，所以画图失败或没画的时候，界面上仍然是可读的。
    """
    def para(m: re.Match) -> str:
        k = int(m.group(2))
        if k >= len(slots):
            return m.group(0)
        return ('<p align="center" class="mdis" data-tex="%s">%s</p>'
                % (quote(slots[k][1], safe=""), slots[k][0]))

    def inline(m: re.Match) -> str:
        k = int(m.group(1))
        if k >= len(slots):
            return m.group(0)
        return ('<span class="math" data-tex="%s">%s</span>'
                % (quote(slots[k][1], safe=""), slots[k][0]))

    body = _MATH_P_RE.sub(para, body)
    return _MATH_SLOT_RE.sub(inline, body)


def render_html(rel: str, root: str | None = None) -> str:
    """把笔记渲染成给 QTextBrowser 看的 HTML（只读预览用）。"""
    root = root or vault_path()
    if os.path.normcase(root) not in _ATTACH_INDEX:
        iter_notes(root)          # 附件索引可能还没建过（直接调本函数的路径）
    text = read_text(rel, root)
    slots: list[str] = []
    if rel.lower().endswith(".md"):
        text, slots = _extract_math(text)
        body = markdown.markdown(
            text, extensions=["fenced_code", "tables", "toc", "nl2br"])
    else:
        body = "<pre>" + h.escape(text) + "</pre>"

    # Obsidian 的图片嵌入 ![[名]] / ![[名|宽度]]：先换成 <img>，
    # 否则会被下面的 [[链接]] 规则吃掉，屏幕上只剩个「!」加一串字。
    body = re.sub(
        r"!\[\[([^\]]+)\]\]",
        lambda m: '<img alt="%s" src="%s" />' % (
            h.escape(m.group(1).split("|")[0].strip()),
            h.escape(m.group(1).split("|")[0].strip())),
        body)

    def link_repl(m: re.Match) -> str:
        inner = m.group(0)[2:-2]
        target, _, alias = inner.partition("|")
        label = alias or target.split("/")[-1].split("#")[0]
        # 必须是 lifeapp:/ 而不是 lifeapp:// —— 后者 Qt 会把 "note" 当主机名吃掉，
        # url.path() 只剩 /xxx，锚点回调里按前缀分发就永远匹配不上。
        return f'<a href="lifeapp:/note/{quote(target.strip())}">{h.escape(label)}</a>'

    body = re.sub(r"\[\[[^\]]+\]\]", link_repl, body)
    body = _TAG_RE.sub(r'<a class="tag" href="lifeapp:/tag/\1">#\1</a>', body)
    body = _IMG_RE.sub(lambda m: _image_tag(m, rel, root, h), body)
    return _restore_math(body, slots)


def _attr(tag: str, rx: re.Pattern) -> str:
    m = rx.search(tag)
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def _split_alt(alt: str, src: str) -> tuple[str, int]:
    """Obsidian 的宽度提示写在 alt 里：`![image.png|350](...)`。

    image-auto-upload 插件把本地图换成图床链接时，会把原来的 `![[image.png|350]]`
    变成这种 alt，星号那边的宽度提示得拆出来，不然界面上就是「image.png|350」。
    """
    label, _, hint = alt.partition("|")
    width = 0
    if hint.strip().isdigit():
        width = int(hint.strip())
    if not label:
        label = os.path.basename(unquote(src.split("#")[0]))
    return label.strip(), width


def _image_tag(m: re.Match, note_rel: str, root: str, h) -> str:  # noqa: ANN001
    tag = m.group(0)
    src = _attr(tag, _SRC_RE)
    label, width = _split_alt(_attr(tag, _ALT_RE), src)
    path = _local_image(src, note_rel, root)
    if path:
        real = QUrl.fromLocalFile(path).toString()
    elif src.startswith(("http://", "https://")):
        # QTextBrowser 自己不会去拉网络图片，原样留着 <img>，
        # 由界面上的 ImageLoader 下载后 addResource 填进文档资源表。
        real = src
    else:
        return f'<span class="imgmiss">🖼 {h.escape(label or "图片")}</span>'
    w = f' data-w="{width}"' if width else ""
    return f'<img src="{h.escape(real)}" alt="{h.escape(label)}"{w} />'


# ---------------------------------------------------------------- 跳转 Obsidian
def _open_uri(uri: str) -> bool:
    return QDesktopServices.openUrl(QUrl(uri))


def open_note(rel: str, heading: str = "") -> bool:
    """让 Obsidian 打开这篇笔记（它自己会拉起，没开就启动）。"""
    root = vault_path()
    if not root:
        return False
    file_arg = os.path.splitext(rel)[0].replace("\\", "/")
    uri = (f"obsidian://open?vault={quote(vault_name(root))}"
           f"&file={quote(file_arg, safe='/')}")
    if heading:
        uri += f"&heading={quote(heading)}"
    return _open_uri(uri)


def open_search(kw: str) -> bool:
    root = vault_path()
    if not root:
        return False
    return _open_uri(f"obsidian://search?vault={quote(vault_name(root))}"
                     f"&query={quote(kw)}")


def open_vault_home() -> bool:
    root = vault_path()
    if not root:
        return False
    return _open_uri(f"obsidian://open?vault={quote(vault_name(root))}")


def reveal_in_folder(path: str) -> bool:
    """在资源管理器里定位这个文件。"""
    if not os.path.exists(path):
        return False
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    return True


# ---------------------------------------------------------------- 导出到 vault
def _safe_filename(name: str) -> str:
    """Obsidian + Windows 双安全的文件名。

    除了 Windows 保留字符，还要去掉 [ ] # ^ ：Obsidian 的链接语法会吃掉它们。
    """
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name or "未命名"


def _fm(value) -> str:
    """YAML frontmatter 的标量：一律加引号，免得中文全角冒号 / 换行炸掉解析。"""
    s = str(value if value is not None else "")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {_fm(v)}" if not isinstance(v, list)
                     else f"{k}: [{', '.join(_fm(x) for x in v)}]")
    lines.append("---")
    return "\n".join(lines)


def _body(text: str) -> str:
    """去掉正文里的 YAML 块，并统一换行（保证同样的数据写出同样的字节）。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n")


class _SyncState:
    def __init__(self):
        self.written: list[str] = []
        self.skipped: list[str] = []
        self.removed: list[str] = []
        self.keep: set[str] = set()


def _write(state: _SyncState, rel: str, content: str) -> None:
    """写一个生成文件。目标已有同名但无标记的文件（用户手写）→ 跳过不覆盖。"""
    root = vault_path()
    full = os.path.join(export_root(root), rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.exists(full):
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(512)
        except OSError:
            state.skipped.append(rel)
            return
        if GEN_MARK not in head:
            state.skipped.append(rel)
            return
    text = content.replace("\n", "\r\n") if os.linesep == "\r\n" else content
    tmp = full + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, full)
    state.written.append(rel)
    state.keep.add(os.path.normcase(full))


def _cleanup(state: _SyncState) -> None:
    """删掉导出目录里本轮没再生成、且确实带着生成标记的孤儿文件。"""
    root = export_root()
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_hidden(d)]
        for fn in filenames:
            if not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.normcase(full) in state.keep:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    if GEN_MARK not in f.read(512):
                        continue
                os.remove(full)
                state.removed.append(
                    os.path.relpath(full, root).replace("\\", "/"))
            except OSError:
                continue
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.normcase(dirpath) == os.path.normcase(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            continue


# ---- 三类产物：科研进展 / 专注记录 / 待办 ----

def _sync_research(state: _SyncState) -> None:
    for r in services.research_list():
        ms = services.milestone_list(r["id"])
        papers = services.research_paper_list(r["id"])
        title = r["title"].strip() or f"课题 {r['id']}"
        lines = [_frontmatter({
            "type": "life-system-research",
            "id": str(r["id"]),
            "stage": services.route_label(r.get("status") or "s1"),
            "priority": str(r.get("priority", 1)),
            "venue": r.get("venue") or "",
            "venue_deadline": r.get("venue_deadline") or "",
            "role": r.get("role") or "",
            "due_date": r.get("due_date") or "",
            "focus_keywords": r.get("focus_keywords") or "",
            "updated": (r.get("updated_at") or "")[:16],
        }), "", f"# {title}", "", f"> {GEN_MARK} · 科研进展，改这里会被下次同步覆盖", ""]

        notes = _body(r.get("notes") or "")
        lines += ["## 进展", "", notes or "_（还没写进展）_", ""]

        if r.get("venue") or r.get("venue_deadline"):
            bits = []
            if r.get("venue"):
                bits.append(f"目标 **{r['venue']}**")
            if r.get("venue_deadline"):
                bits.append(f"截稿 `{r['venue_deadline']}`")
            if r.get("role"):
                bits.append(f"本人 {r['role']}")
            lines += ["## 投稿", "", " · ".join(bits), ""]

        if ms:
            lines += ["## 节点", ""]
            for m in ms:
                box = "x" if m.get("done") else " "
                due = f" 📅 {m['due_date']}" if m.get("due_date") else ""
                lines.append(f"- [{box}] {m['title']}{due}")
                note = _body(m.get("note") or "")
                if note:
                    lines.append(f"  {note}")
            lines.append("")

        if papers:
            lines += ["## 关联论文", ""]
            for p in papers:
                aid = p.get("arxiv_id") or ""
                name = (p.get("title") or aid).strip() or aid
                lines.append(f"- [{name}](https://arxiv.org/abs/{aid}) · `{aid}`")
            lines.append("")

        _write(state, f"科研/{_safe_filename(title)}.md", "\n".join(lines))


def _sync_focus(state: _SyncState) -> None:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, duration_min, task, completed, note "
            "FROM pomodoro ORDER BY started_at ASC").fetchall()
    by_month: dict[str, list] = {}
    for row in rows:
        r = dict(row)
        month = (r.get("started_at") or "")[:7]
        if len(month) == 7:
            by_month.setdefault(month, []).append(r)

    for month, recs in sorted(by_month.items()):
        total = sum(int(r["duration_min"] or 0) for r in recs
                    if r.get("completed"))
        lines = [_frontmatter({
            "type": "life-system-focus",
            "month": month,
            "total_minutes": str(total),
            "records": str(len(recs)),
        }), "", f"# 专注记录 · {month}", "",
            f"> {GEN_MARK} · 番茄钟记录，只读", "",
            "| 时间 | 任务 | 分钟 | 备注 |",
            "| --- | --- | ---: | --- |"]
        for r in sorted(recs, key=lambda x: x["started_at"], reverse=True):
            when = (r["started_at"] or "")[5:16]
            task = (r["task"] or "自由专注").replace("|", "\\|").replace("\n", " ")
            note = (r.get("note") or "").replace("|", "\\|").replace("\n", " ").strip()
            mins = int(r["duration_min"] or 0)
            flag = "" if r.get("completed") else " "
            lines.append(f"| {when} | {task} | {mins}{flag} | {note} |")
        lines += ["", f"合计 **{total} 分钟**（已完成）。", ""]
        _write(state, f"专注/{month}.md", "\n".join(lines))


_PRI_MARK = {3: "⏫", 2: "🔺", 1: "🔽", 0: ""}


def _sync_todos(state: _SyncState) -> None:
    todos = [t for t in services.todo_list(kind="task")
             if (t.get("title") or "").strip()]
    open_items = [t for t in todos if not t.get("done")]
    cutoff = date.today().isoformat()
    done_items = [t for t in todos
                  if t.get("done") and (t.get("completed_at") or "")[:10] >= cutoff]

    def render(t: dict) -> list[str]:
        box = "x" if t.get("done") else " "
        bits = [f"- [{box}] {t['title'].strip()}"]
        meta = []
        if t.get("due_date"):
            meta.append(f"📅 {t['due_date']}")
        if t.get("due_time"):
            meta.append(f"⏰ {t['due_time']}")
        mark = _PRI_MARK.get(int(t.get("priority") or 0), "")
        if mark:
            meta.append(mark)
        if t.get("list_name"):
            meta.append(f"📒 {t['list_name']}")
        line = " ".join(bits + meta)
        note = _body(t.get("note") or "")
        out = [line]
        out += [f"  {ln}" if ln.strip() else "" for ln in note.splitlines()]
        return out

    stamp = max([(t.get("completed_at") or t.get("created_at") or "")[:16]
                 for t in todos] or [""])
    lines = [_frontmatter({
        "type": "life-system-todos",
        "open": str(len(open_items)),
        "done_today": str(len(done_items)),
        "updated": stamp,
    }), "", "# 待办", "", f"> {GEN_MARK} · 待办快照，只读（改这里不会回写 Life System）", ""]

    if open_items:
        lines += ["", "## 进行中", ""]
        for t in open_items:
            lines += render(t)
    if done_items:
        lines += ["", "## 今天完成", ""]
        for t in sorted(done_items,
                        key=lambda x: x.get("completed_at") or "", reverse=True):
            lines += render(t)
    if not open_items and not done_items:
        lines += ["", "_（没有待办）_", ""]

    _write(state, "待办/待办.md", "\n".join(lines))


def sync_all() -> dict:
    """把 Life System 的结构化数据写成 md 落进 vault 的 LifeSystem/ 子树。

    幂等：数据没变时写出的字节一样，Obsidian 的 git 插件不会看到假改动。
    返回 {"written","skipped","removed"}；没配 vault 时抛 ValueError。
    """
    if not vault_path():
        raise ValueError("还没设置 Obsidian 库目录")
    state = _SyncState()
    _sync_research(state)
    _sync_focus(state)
    _sync_todos(state)
    _cleanup(state)
    return {"written": state.written, "skipped": state.skipped,
            "removed": state.removed}


def last_sync_at() -> str:
    return db.get_setting("obsidian_last_sync", "")


def sync_summary(result: dict) -> str:
    parts = [f"写入 {len(result['written'])} 个文件"]
    if result["removed"]:
        parts.append(f"清理 {len(result['removed'])} 个")
    if result["skipped"]:
        parts.append(f"跳过 {len(result['skipped'])} 个同名手写笔记")
    return " · ".join(parts)


def pick_vault(parent) -> bool:  # noqa: ANN001
    """弹出库目录选择器（已注册的列出来，也可以浏览别的）。返回是否改了。"""
    from PySide6.QtWidgets import QFileDialog
    found = discover_vaults()
    current = vault_path()
    browse = "📂 浏览其它目录…"
    opts = [f"{v['name']}  ·  {v['path']}" for v in found] + [browse]
    idx = next((i for i, v in enumerate(found) if v["path"] == current), 0)
    choice, ok = popups.get_item(parent, "选择 Obsidian 库", "库目录", opts, idx)
    if not ok:
        return False
    if choice == browse:
        chosen = QFileDialog.getExistingDirectory(
            parent, "选择 Obsidian 库目录", current or os.path.expanduser("~"))
        if chosen:
            set_vault_path(chosen)
            return True
        return False
    i = opts.index(choice)
    if i < len(found):
        set_vault_path(found[i]["path"])
        return True
    return False


def sync_from_ui(parent) -> bool:  # noqa: ANN001
    """界面上的「同步到 Obsidian」按钮走这里，笔记页和设置页共用一套行为。

    第一次点先确认（要在用户的库里建目录），之后直接同步。
    返回是否真的写了文件。
    """
    root = vault_path()
    if not root:
        popups.notify(parent, "还没选库", "先指一个 Obsidian 库目录。")
        return False
    target = export_root(root)
    if db.get_setting("obsidian_sync_warned") != "1":
        if not popups.confirm(
                parent, "同步到 Obsidian",
                f"会在你的库里创建 / 更新这个目录：\n{target}\n\n"
                "只动 LifeSystem 这一个子目录，库里别处的笔记不碰；\n"
                "同名文件如果是你手写的，跳过不覆盖。",
                ok_text="确定"):
            return False
        db.set_setting("obsidian_sync_warned", "1")
    try:
        result = sync_all()
    except (OSError, ValueError) as exc:
        popups.notify(parent, "同步失败", str(exc), danger=True)
        return False
    db.set_setting("obsidian_last_sync",
                   datetime.now().strftime("%m-%d %H:%M"))
    msg = sync_summary(result) + f"\n{target}"
    if result["skipped"]:
        msg += "\n\n跳过的是你手写的同名文件：" + "、".join(result["skipped"][:3])
    sounds.play("sync_ok")
    popups.notify(parent, "同步完成", msg)
    return True
