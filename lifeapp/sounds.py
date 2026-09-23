"""音效引擎 + 白噪音。

提示音取自滴答清单的 RemindSound（12 个短音），已转成 44.1kHz 单声道 PCM wav
放在 assets/sounds/。播放走 QSoundEffect：延迟低、能调音量、只吃 PCM wav，
既不依赖系统解码器，打包时也不用额外带多媒体插件。

每个「事件」可以单独开关、单独换音色，偏好存在 db 的 settings 表里。
业务代码只需要一行 sounds.play("事件名")；play() 保证不抛异常，
音效永远不能拖垮功能。

白噪音部分仍用 winsound 循环播放程序生成的 wav。它走的是老的波形音频接口，
提示音走 WASAPI，实测两者可以同时出声，所以番茄钟能边放噪音边响结束铃。
"""
from __future__ import annotations

import os
import random
import struct
import time
import wave

from . import config

# ---------------------------------------------------------------------------
# 音色
# ---------------------------------------------------------------------------
# 文件名（assets/sounds/<name>.wav）→ 中文标签。按音长大致从短到长排列，
# 下拉框里短音在前，方便给高频事件挑不吵的。
CLIPS: dict[str, str] = {
    "beep": "哔哔",
    "music_box": "八音盒",
    "blocks": "积木",
    "pulse": "脉冲",
    "harp": "竖琴",
    "leap": "跳跃",
    "africa": "非洲鼓",
    "lattice": "格纹",
    "ladder": "阶梯",
    "matrix": "矩阵",
    "chimes": "风铃",
    "crystal": "水晶",
}
CLIP_LABELS: list[str] = list(CLIPS.values())
_LABEL_TO_CLIP: dict[str, str] = {v: k for k, v in CLIPS.items()}

#: 下拉框可选项（含「无」= 静音）。番茄钟设置里的铃声下拉直接用它。
RING_NAMES: list[str] = ["无", *CLIP_LABELS]

# 旧版 winsound 方波铃声名 → 现在的音色标签。"脉冲" 两边同名，不用迁。
_LEGACY_LABELS: dict[str, str] = {
    "默认": "八音盒",
    "清脆": "风铃",
    "柔和": "竖琴",
}

# ---------------------------------------------------------------------------
# 事件目录：(key, 中文名, 分组, 默认音色, 默认是否开)
# ---------------------------------------------------------------------------
# 默认开的都是「不看屏幕也需要知道」或「低频、有仪式感」的事件；
# 高频的增删改默认关，想要的人自己去设置里打开。
EVENTS: list[tuple[str, str, str, str, bool]] = [
    ("reminder",             "到点提醒（待办 / 习惯）", "提醒",   "chimes",    True),

    ("pomodoro_work_end",    "番茄结束",                "番茄钟", "matrix",    True),
    ("pomodoro_rest_end",    "休息结束",                "番茄钟", "music_box", True),
    ("pomodoro_start",       "开始 / 继续专注",         "番茄钟", "beep",      False),
    ("pomodoro_pause",       "暂停专注",                "番茄钟", "pulse",     False),
    ("pomodoro_abandon",     "放弃本轮",                "番茄钟", "lattice",   False),
    ("countup_recorded",     "正计时结束并记录",        "番茄钟", "beep",      False),

    # 「叮」：实测这几个音色里 crystal（水晶，主峰 1047Hz≈C6、能量最集中）最接近
    # 滴答完成时那一声；原来的 blocks 是 208Hz 的低沉木块「咔」，听着像敲桌子。
    ("todo_done",            "完成待办",                "待办",   "crystal",   True),
    ("subtask_done",         "完成子任务",              "待办",   "music_box", False),
    ("todo_created",         "新建待办",                "待办",   "beep",      False),
    ("todo_deleted",         "删除待办",                "待办",   "lattice",   False),

    ("habit_checkin",        "习惯打卡",                "习惯",   "harp",      True),
    ("habit_streak_record",  "刷新最长连续记录",        "习惯",   "chimes",    True),
    ("habit_target_reached", "达成习惯目标",            "习惯",   "crystal",   True),

    ("calendar_moved",       "拖拽改期",                "日历",   "leap",      False),

    ("finance_saved",        "记一笔",                  "记账",   "ladder",    False),
    ("finance_bill_filled",  "补记周期账单",            "记账",   "leap",      False),

    ("weight_logged",        "记录体重",                "体重",   "pulse",     False),

    ("answer_correct",       "答对 / 独立做出来",       "刷题",   "leap",      True),
    ("answer_wrong",         "答错 / 没做出来",         "刷题",   "lattice",   True),
    ("round_complete",       "一轮刷完",                "刷题",   "africa",    True),

    ("research_stage",       "推进科研阶段",            "科研",   "ladder",    True),
    ("milestone_done",       "完成一个 DDL",            "科研",   "crystal",   True),

    ("sync_ok",              "同步到 Obsidian 成功",    "数据",   "harp",      True),
    ("export_ok",            "导出数据成功",            "数据",   "harp",      True),
    ("popup_danger",         "出错提示",                "数据",   "lattice",   False),
]

_EVENT: dict[str, tuple[str, str, str, str, bool]] = {e[0]: e for e in EVENTS}

# 番茄钟这两个事件复用设置里已有的 db key，这样番茄钟自带的「结束铃声」下拉
# 和设置页的音色下拉指的是同一份配置，改哪边都同步。
_CLIP_KEY_OVERRIDE: dict[str, str] = {
    "pomodoro_work_end": "pomodoro_ring_work",
    "pomodoro_rest_end": "pomodoro_ring_rest",
}

_MASTER_KEY = "sound_master"
_VOLUME_KEY = "sound_volume"
#: 素材已经在 _normalize_sounds.py 里按 RMS 拉齐到 -20 dBFS，默认再走 50%，
#: 落在舒适的提示音量区间；往上一档也不会炸耳。
DEFAULT_VOLUME = 50


def clip_path(clip: str) -> str:
    """某个音色 wav 的绝对路径（兼容 PyInstaller onefile 的 _MEIPASS）。"""
    return config.asset_path(os.path.join("assets", "sounds", f"{clip}.wav"))


def available_clips() -> dict[str, str]:
    """实际带在身上的音色（打包漏文件时不至于整页报错）。"""
    return {c: lbl for c, lbl in CLIPS.items() if os.path.exists(clip_path(c))}


# ---------------------------------------------------------------------------
# 偏好读写
# ---------------------------------------------------------------------------
def _db():
    from . import db
    return db


def enabled() -> bool:
    """总开关。"""
    return _db().get_setting(_MASTER_KEY, "1") != "0"


def set_enabled(on: bool) -> None:
    _db().set_setting(_MASTER_KEY, "1" if on else "0")


def volume() -> int:
    """音量 0-100。"""
    try:
        return max(0, min(100, int(_db().get_setting(_VOLUME_KEY, "")
                                  or DEFAULT_VOLUME)))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME


def set_volume(v: int) -> None:
    _db().set_setting(_VOLUME_KEY, str(max(0, min(100, int(v)))))


def _clip_key(event: str) -> str:
    return _CLIP_KEY_OVERRIDE.get(event, f"sound_clip_{event}")


def event_enabled(event: str) -> bool:
    """某个事件是否要响。"""
    ev = _EVENT.get(event)
    if ev is None:
        return False
    return _db().get_setting(f"sound_on_{event}", "1" if ev[4] else "0") != "0"


def set_event_enabled(event: str, on: bool) -> None:
    if event in _EVENT:
        _db().set_setting(f"sound_on_{event}", "1" if on else "0")


def event_clip(event: str) -> str | None:
    """某个事件当前的音色（文件名）；None = 静音。"""
    ev = _EVENT.get(event)
    if ev is None:
        return None
    key = _clip_key(event)
    raw = (_db().get_setting(key, "") or "").strip()
    if not raw:
        return ev[3]
    if raw == "无":
        return None
    if raw in _LEGACY_LABELS:  # 旧方波铃声名，顺手迁到新标签
        raw = _LEGACY_LABELS[raw]
        _db().set_setting(key, raw)
    return _LABEL_TO_CLIP.get(raw) or (raw if raw in CLIPS else None) or ev[3]


def set_event_clip(event: str, clip: str | None) -> None:
    """设置事件音色。传文件名或中文标签都行；None / "无" = 静音。"""
    if event not in _EVENT:
        return
    if not clip or clip == "无":
        label = "无"
    else:
        label = CLIPS.get(clip, clip)
    _db().set_setting(_clip_key(event), label)


def event_clip_label(event: str) -> str:
    """事件当前音色的中文标签（给下拉框回显用）。"""
    clip = event_clip(event)
    return CLIPS.get(clip or "", "无")


def event_group_order() -> list[str]:
    """分组的出现顺序（给设置页排版用）。"""
    out: list[str] = []
    for ev in EVENTS:
        if ev[2] not in out:
            out.append(ev[2])
    return out


def events_in_group(group: str) -> list[tuple[str, str, str, str, bool]]:
    return [e for e in EVENTS if e[2] == group]


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------
_VOICES: dict[str, list] = {}
_MAX_VOICES = 3          # 同一音色最多几个声部（连点时不至于自己打断自己）
_MIN_GAP_S = 0.12        # 同一事件的最小间隔，挡住手抖连点
_last_at: dict[str, float] = {}


def _acquire_voice(clip: str):
    """取一个空闲声部；都在响就新建一个，超过上限则放弃这次播放。"""
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QSoundEffect

    pool = _VOICES.setdefault(clip, [])
    for v in pool:
        if not v.isPlaying():
            return v
    if len(pool) >= _MAX_VOICES:
        return None
    v = QSoundEffect()
    v.setSource(QUrl.fromLocalFile(clip_path(clip)))
    pool.append(v)
    return v


def _play_clip(clip: str | None) -> None:
    if not clip:
        return
    from PySide6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        return
    if not os.path.exists(clip_path(clip)):
        return
    vol = volume() / 100.0
    if vol <= 0.0:
        return
    voice = _acquire_voice(clip)
    if voice is None:
        return
    voice.setVolume(vol)
    voice.play()


def play(event: str) -> None:
    """按事件播提示音。业务代码只管调，永远不抛异常。"""
    try:
        if not event_enabled(event):
            return
        if not enabled():
            return
        now = time.monotonic()
        if now - _last_at.get(event, 0.0) < _MIN_GAP_S:
            return
        _last_at[event] = now
        _play_clip(event_clip(event))
    except Exception:  # noqa: BLE001
        pass


def preview(clip: str | None) -> None:
    """试听音色。故意绕过总开关——用户正在挑音色，静音状态下也该让他听见。"""
    try:
        _play_clip(clip)
    except Exception:  # noqa: BLE001
        pass


def preview_event(event: str) -> None:
    """试听某个事件当前配的音色。"""
    try:
        _play_clip(event_clip(event))
    except Exception:  # noqa: BLE001
        pass


def prewarm() -> None:
    """把全部音色提前解码进内存。

    QSoundEffect 的 setSource 是异步解码的：第一次 play() 时 status 还是
    Loading，要等几百毫秒才真的出声。对「勾选待办」这类即时反馈来说太明显，
    所以启动完成后空闲时先加载一遍，之后每次都是立刻响。
    """
    try:
        from PySide6.QtCore import QCoreApplication
        if QCoreApplication.instance() is None:
            return
        for clip in available_clips():
            _acquire_voice(clip)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 白噪音（番茄钟氛围音）：程序生成 wav，winsound 循环播放
# ---------------------------------------------------------------------------
_SOUNDS = {
    "white": "白噪音",
    "pink": "粉噪音",
    "brown": "棕噪音",
}
_RATE = 22050
_SECONDS = 10


def sound_names() -> dict:
    return dict(_SOUNDS)


def _white(n: int) -> list:
    return [random.uniform(-1.0, 1.0) for _ in range(n)]


def _pink(n: int) -> list:
    """Voss-McCartney 粉噪音。"""
    num_oct = 8
    octaves = [0.0] * num_oct
    out = []
    for i in range(n):
        for j in range(num_oct):
            if i % (1 << j) == 0:
                octaves[j] = random.uniform(-1.0, 1.0)
        out.append(sum(octaves) / num_oct * 2.0)
    return out


def _brown(n: int) -> list:
    out = []
    last = 0.0
    for _ in range(n):
        last += random.uniform(-1.0, 1.0) * 0.02
        last = max(-1.0, min(1.0, last))
        out.append(last * 3.0)
    return out


def _write_wav(path: str, samples: list) -> None:
    w = wave.open(path, "w")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(_RATE)
    frames = bytearray()
    for s in samples:
        v = max(-32767, min(32767, int(s * 32767)))
        frames += struct.pack("<h", v)
    w.writeframes(bytes(frames))
    w.close()


def _path(kind: str) -> str:
    return os.path.join(config.data_dir(), "sounds", f"{kind}.wav")


def ensure_sounds() -> None:
    """按需生成缺失的白噪音文件。"""
    out_dir = os.path.join(config.data_dir(), "sounds")
    os.makedirs(out_dir, exist_ok=True)
    gen = {"white": _white, "pink": _pink, "brown": _brown}
    for kind, fn in gen.items():
        path = _path(kind)
        if not os.path.exists(path):
            _write_wav(path, fn(_RATE * _SECONDS))


def play_noise(kind: str | None) -> None:
    """循环播放指定白噪音；kind 为 None 时停止。"""
    import winsound
    stop_noise()
    if not kind:
        return
    ensure_sounds()
    path = _path(kind)
    if os.path.exists(path):
        winsound.PlaySound(
            path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)


def stop_noise() -> None:
    import winsound
    try:
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:  # noqa: BLE001
        pass
