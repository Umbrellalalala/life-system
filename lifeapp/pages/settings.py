"""设置页：开机自启、音效、Obsidian 库、导出数据、外观、关于。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QCheckBox, QComboBox, QFileDialog)

from .. import autostart, config, export, popups, sounds, theme, vault, widgets
from .base import Page



def db_get(key: str) -> str:
    """便签那几个设置：没写过就用 sticky._DEFAULTS 里的默认值。"""
    from .. import db, sticky
    return db.get_setting(key) or sticky._DEFAULTS.get(key, "")


def db_set(key: str, value: str) -> None:
    from .. import db
    db.set_setting(key, value)


class SettingsPage(Page):
    def __init__(self):
        super().__init__("设置", "配置应用与数据", scrollable=True)

        # 通用：开机自启
        general = widgets.Card("通用")
        row = QHBoxLayout()
        lbl = QLabel("开机自启")
        lbl.setObjectName("Strong")
        hint = QLabel("登录 Windows 后自动启动 Life System")
        hint.setObjectName("Muted")
        self.autostart_check = QCheckBox()
        self.autostart_check.setCursor(Qt.PointingHandCursor)
        self.autostart_check.setChecked(autostart.is_enabled())
        self.autostart_check.toggled.connect(self._toggle_autostart)
        row.addWidget(lbl)
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(self.autostart_check)
        general.body().addLayout(row)
        self.body().addWidget(general)

        # 音效：总开关 + 音量 + 每个事件单独开关/换音色/试听
        self.body().addWidget(self._build_sound_card())

        # Obsidian：笔记由 Obsidian 负责写和存，这里只读 + 跳转 + 单向导出
        obs = widgets.Card("Obsidian 库")
        orow = QHBoxLayout()
        olbl = QLabel("当前库")
        olbl.setObjectName("Strong")
        self.vault_lbl = QLabel()
        self.vault_lbl.setObjectName("Muted")
        self.vault_lbl.setWordWrap(True)
        self.vault_btn = QPushButton("更换")
        self.vault_btn.setObjectName("Ghost")
        self.vault_btn.setCursor(Qt.PointingHandCursor)
        self.vault_btn.clicked.connect(self._pick_vault)
        orow.addWidget(olbl)
        orow.addWidget(self.vault_lbl, 1)
        orow.addWidget(self.vault_btn)
        obs.body().addLayout(orow)

        hint = QLabel("笔记的编辑和存放交给 Obsidian，本应用只读库目录、搜索、跳转，"
                      "并把科研进展 / 专注记录 / 待办快照单向写成库里的 md。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        obs.body().addWidget(hint)

        syncrow = QHBoxLayout()
        self.sync_lbl = QLabel()
        self.sync_lbl.setObjectName("Muted")
        self.sync_lbl.setWordWrap(True)
        sync_btn = QPushButton("⇪ 同步到 Obsidian")
        sync_btn.setObjectName("Primary")
        sync_btn.setCursor(Qt.PointingHandCursor)
        sync_btn.clicked.connect(self._sync_vault)
        syncrow.addWidget(self.sync_lbl, 1)
        syncrow.addWidget(sync_btn)
        obs.body().addLayout(syncrow)
        self.body().addWidget(obs)

        self.body().addWidget(self._build_sticky_card())
        self._refresh_vault_lbl()

        # 外观
        appearance = widgets.Card("外观")
        arow = QHBoxLayout()
        albl = QLabel("主题")
        albl.setObjectName("Strong")
        arow.addWidget(albl)
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("Ghost")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self._toggle_theme)
        self._update_theme_btn()
        arow.addWidget(self.theme_btn)
        arow.addStretch(1)
        appearance.body().addLayout(arow)
        self.body().addWidget(appearance)

        # 数据
        data = widgets.Card("数据")
        drow = QHBoxLayout()
        dlbl = QLabel("数据目录")
        dlbl.setObjectName("Strong")
        dpath = QLabel(config.data_dir())
        dpath.setObjectName("Muted")
        dpath.setWordWrap(True)
        drow.addWidget(dlbl)
        drow.addWidget(dpath, 1)
        data.body().addLayout(drow)
        export_btn = QPushButton("📦 导出全部数据（JSON / CSV）")
        export_btn.setObjectName("Primary")
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.clicked.connect(self._export_data)
        data.body().addWidget(export_btn)
        self.body().addWidget(data)

        # 关于
        about = widgets.Card("关于")
        arow2 = QHBoxLayout()
        ver = QLabel(f"Life System v{config.APP_VERSION}")
        ver.setObjectName("Strong")
        arow2.addWidget(ver)
        arow2.addStretch(1)
        about.body().addLayout(arow2)
        self.body().addWidget(about)

        theme.manager.changed.connect(self._update_theme_btn)

    def _toggle_autostart(self, checked: bool) -> None:
        if not autostart.sync(checked):
            popups.notify(self, "开机自启", "设置失败，请检查系统权限后重试。",
                          danger=True)
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(not checked)
            self.autostart_check.blockSignals(False)

    # ---------- 音效 ----------
    def _build_sticky_card(self) -> widgets.Card:
        """便签：颜色 / 不透明度 / 字号 / 置顶 / 间距。

        这几个值不是存了就完事 —— `sticky.prefs()` 每次开便签都读它们，
        已经开着的便签改设置时会立刻重刷一遍样式。
        """
        from PySide6.QtWidgets import QSlider
        from .. import sticky
        card = widgets.Card("便签")
        body = card.body()

        hint = QLabel("右键任意待办 →「打开便签」把它贴到桌面上。便签改的字直接写回待办，"
                      "两边是同一条数据，不留副本。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        body.addWidget(hint)

        crow = QHBoxLayout()
        clbl = QLabel("默认颜色")
        clbl.setObjectName("Strong")
        crow.addWidget(clbl)
        crow.addStretch(1)
        self.sticky_colors = []
        for name, hex_value in sticky.STICKY_COLORS:
            b = QPushButton()
            b.setObjectName("StickyDot")
            b.setFixedSize(24, 24)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip("%s %s" % (name, hex_value))
            b.setStyleSheet("background: %s; border: 2px solid %s; border-radius: 6px;"
                            % (hex_value, "#00a5ff" if hex_value == db_get("sticky_color")
                               else "transparent"))
            b.clicked.connect(lambda _c=False, h=hex_value, btn=b:
                              self._on_sticky_color(h, btn))
            crow.addWidget(b)
            self.sticky_colors.append(b)
        body.addLayout(crow)

        orow = QHBoxLayout()
        olbl = QLabel("不透明度")
        olbl.setObjectName("Strong")
        self.sticky_opacity = QSlider(Qt.Horizontal)
        self.sticky_opacity.setRange(30, 100)
        self.sticky_opacity.setFixedWidth(200)
        self.sticky_opacity.setCursor(Qt.PointingHandCursor)
        self.sticky_opacity.setValue(int(db_get("sticky_opacity")))
        self.sticky_val = QLabel("%d%%" % self.sticky_opacity.value())
        self.sticky_val.setObjectName("Muted")
        self.sticky_opacity.valueChanged.connect(self._on_sticky_opacity)
        orow.addWidget(olbl)
        orow.addStretch(1)
        orow.addWidget(self.sticky_opacity)
        orow.addWidget(self.sticky_val)
        body.addLayout(orow)

        for label, key, choices in (("字体大小", "sticky_font", list(sticky.FONT_SIZES)),
                                    ("默认便签间距", "sticky_gap", list(sticky.GAPS))):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setObjectName("Strong")
            box = QComboBox()
            box.addItems(choices)
            box.setCurrentText(db_get(key))
            box.setCursor(Qt.PointingHandCursor)
            box.currentTextChanged.connect(
                lambda v, k=key: self._on_sticky_set(k, v))
            r.addWidget(l)
            r.addStretch(1)
            r.addWidget(box)
            body.addLayout(r)
            if key == "sticky_font":
                self.sticky_font_box = box
            else:
                self.sticky_gap_box = box

        trow = QHBoxLayout()
        tlbl = QLabel("新便签默认置顶")
        tlbl.setObjectName("Strong")
        self.sticky_top = QCheckBox()
        self.sticky_top.setCursor(Qt.PointingHandCursor)
        self.sticky_top.setChecked(db_get("sticky_topmost") != "0")
        self.sticky_top.toggled.connect(
            lambda on: (db_set("sticky_topmost", "1" if on else "0"),
                        sticky.reopen_for_prefs()))
        trow.addWidget(tlbl)
        trow.addStretch(1)
        trow.addWidget(self.sticky_top)
        body.addLayout(trow)
        return card

    def _on_sticky_color(self, hex_value: str, btn) -> None:
        db_set("sticky_color", hex_value)
        for b in self.sticky_colors:
            b.setStyleSheet("background: %s; border: 2px solid transparent; "
                            "border-radius: 6px;" % b.toolTip().split(" ")[1])
        btn.setStyleSheet("background: %s; border: 2px solid #00a5ff; "
                          "border-radius: 6px;" % hex_value)
        from .. import sticky
        sticky.reopen_for_prefs()

    def _on_sticky_opacity(self, v: int) -> None:
        self.sticky_val.setText("%d%%" % v)
        db_set("sticky_opacity", str(v))
        from .. import sticky
        sticky.reopen_for_prefs()

    def _on_sticky_set(self, key: str, value: str) -> None:
        db_set(key, value)
        from .. import sticky
        sticky.reopen_for_prefs()

    def _build_sound_card(self) -> widgets.Card:
        card = widgets.Card("音效")
        body = card.body()

        row = QHBoxLayout()
        lbl = QLabel("提示音")
        lbl.setObjectName("Strong")
        hint = QLabel("完成待办、习惯打卡、番茄结束、到点提醒等操作的提示音")
        hint.setObjectName("Muted")
        self.sound_master = QCheckBox()
        self.sound_master.setCursor(Qt.PointingHandCursor)
        self.sound_master.setChecked(sounds.enabled())
        self.sound_master.toggled.connect(self._on_sound_master)
        row.addWidget(lbl)
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(self.sound_master)
        body.addLayout(row)

        vrow = QHBoxLayout()
        vlbl = QLabel("音量")
        vlbl.setObjectName("Strong")
        self.volume_combo = QComboBox()
        self.volume_combo.addItems([f"{v}%" for v in range(0, 101, 10)])
        vol = sounds.volume()
        self.volume_combo.setCurrentText(f"{vol - vol % 10}%")
        self.volume_combo.currentTextChanged.connect(self._on_volume)
        vrow.addWidget(vlbl)
        vrow.addWidget(self.volume_combo)
        vrow.addStretch(1)
        body.addLayout(vrow)

        self._sound_rows: list[tuple[str, QCheckBox, QComboBox]] = []
        for group in sounds.event_group_order():
            glbl = QLabel(group)
            glbl.setObjectName("Muted")
            body.addWidget(glbl)
            for key, label, _g, _clip, _on in sounds.events_in_group(group):
                body.addLayout(self._sound_row(key, label))
        return card

    def _sound_row(self, key: str, label: str) -> QHBoxLayout:
        r = QHBoxLayout()
        r.setSpacing(8)
        cb = QCheckBox()
        cb.setCursor(Qt.PointingHandCursor)
        cb.setChecked(sounds.event_enabled(key))
        cb.toggled.connect(lambda on, k=key: self._on_event_toggle(k, on))
        name = QLabel(label)
        combo = QComboBox()
        combo.addItems(sounds.RING_NAMES)
        combo.setCurrentText(sounds.event_clip_label(key))
        combo.currentTextChanged.connect(lambda txt, k=key: self._on_event_clip(k, txt))
        # clicked 会带一个 checked 布尔，必须用第一个位置参数接掉，
        # 否则它会顶掉 k 的默认值，把 False 当事件名传进去。
        btn = QPushButton("▶")
        btn.setObjectName("Ghost")
        btn.setFixedWidth(30)
        btn.setToolTip("试听")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _checked=False, k=key: sounds.preview_event(k))
        r.addWidget(cb)
        r.addWidget(name, 1)
        r.addWidget(combo)
        r.addWidget(btn)
        self._sound_rows.append((key, cb, combo))
        return r

    def _on_sound_master(self, on: bool) -> None:
        sounds.set_enabled(on)
        if on:
            sounds.preview("chimes")  # 打开时响一声，让人立刻知道生效了

    def _on_volume(self, text: str) -> None:
        try:
            sounds.set_volume(int(text.rstrip("%")))
        except ValueError:
            return
        sounds.preview("blocks")

    def _on_event_toggle(self, key: str, on: bool) -> None:
        sounds.set_event_enabled(key, on)
        if on:
            sounds.preview_event(key)

    def _on_event_clip(self, key: str, label: str) -> None:
        sounds.set_event_clip(key, None if label == "无" else label)
        sounds.preview_event(key)

    def _toggle_theme(self) -> None:
        theme.manager.toggle()

    def _update_theme_btn(self) -> None:
        self.theme_btn.setText("☀️ 日间模式" if theme.manager.is_dark else "🌙 夜间模式")

    # ---------- Obsidian ----------
    def _refresh_vault_lbl(self) -> None:
        root = vault.vault_path()
        n = len(vault.iter_notes(root)) if root else 0
        self.vault_lbl.setText(
            f"{root}  ·  {n} 篇笔记" if root else "未设置（去笔记页选一个库目录）")
        last = vault.last_sync_at()
        self.sync_lbl.setText(
            f"导出到 {vault.EXPORT_DIR}/ · 上次同步 {last}" if last
            else f"导出到 {vault.EXPORT_DIR}/ · 还没同步过")

    def _pick_vault(self) -> None:
        if vault.pick_vault(self):
            self._refresh_vault_lbl()

    def _sync_vault(self) -> None:
        if vault.sync_from_ui(self):
            self._refresh_vault_lbl()

    def _export_data(self) -> None:
        default_dir = os.path.join(config.data_dir(), "exports")
        chosen = QFileDialog.getExistingDirectory(self, "选择导出目录", default_dir)
        if not chosen:
            return
        files = export.export_all(chosen)
        sounds.play("export_ok")
        popups.notify(self, "导出完成", f"已导出 {len(files)} 个文件到：\n{chosen}")
