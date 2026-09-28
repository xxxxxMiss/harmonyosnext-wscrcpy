"""PySide6 深色控制台 GUI：无边框自绘窗口 + HUD 视频面板 + 霓虹按钮。

连接状态机：未连接（面板占位 + 刷新按钮）→ 投屏中（帧渲染/录制/截屏）
→ 掉线自动回到未连接态。无设备也能正常启动，不拦截。
采集线程经 Signal 跨线程送帧，QImage 在工作线程转换、UI 线程只做缩放与绘制。
对话框路径可注入（test_record_path/test_shot_path）以便离屏自动化测试。
"""
from __future__ import annotations

import datetime
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from . import resources
from .agent import (AgentCapture, SETTING_DEVECO_PATH,
                    capture_supported as agent_supported, clear_so_cache,
                    describe_deveco_path)
from .capture import BaseCapture, Frame, PulledCapture
from .hdc import HOST_TEMP, TMP_DIR, Hdc, HdcError
from .recorder import Recorder
from .scrcpy_server import H264Stream, find_local_so
from .stream import H264Decoder, StreamRecorder

BG = "#0A0E1A"
PANEL = "#101624"
LINE = "#1C2740"
NEON = "#00E5FF"
TEXT = "#C8D3F5"
DIM = "#5A6B8C"
RED = "#FF3B5C"
MONO = "'JetBrains Mono','Menlo','Consolas','Courier New',monospace"

QSS = f"""
#root {{ background: {BG}; }}
#title {{ color: #E6F1FF; font-size: 15px; font-weight: 600; letter-spacing: 5px; }}
#subtitle {{ color: {DIM}; font-family: {MONO}; font-size: 11px; }}
#chromeBtn {{ color: {DIM}; background: transparent; border: none; font-size: 14px;
             padding: 6px 12px; }}
#chromeBtn:hover {{ color: #E6F1FF; background: rgba(255,255,255,0.06); }}
#chromeBtn#closeBtn:hover {{ color: #FFFFFF; background: rgba(255,59,92,0.85); }}
#hud {{ color: {NEON}; font-family: {MONO}; font-size: 12px; letter-spacing: 1px; }}
#status {{ color: {DIM}; font-family: {MONO}; font-size: 12px; }}
QPushButton[neon="true"] {{
    color: {TEXT}; background: rgba(28,39,64,0.45); border: 1px solid {LINE};
    border-radius: 6px; padding: 10px 30px; font-size: 13px;
}}
QPushButton[neon="true"]:hover {{
    color: {NEON}; border-color: {NEON}; background: rgba(0,229,255,0.10);
}}
QPushButton[neon="true"]:pressed {{ background: rgba(0,229,255,0.18); }}
QPushButton[neon="true"]:disabled {{ color: #3A4661; border-color: {LINE}; }}
QPushButton[neon="true"][rec="true"] {{
    color: {RED}; border-color: {RED}; background: rgba(255,59,92,0.10);
}}
#overlay {{ background: rgba(4,8,16,0.74); }}
#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
#cardTitle {{ color: #E6F1FF; font-size: 14px; font-weight: 600; letter-spacing: 3px; }}
#cardHint {{ color: {NEON}; font-size: 12px; }}
#cardNote {{ color: {DIM}; font-family: {MONO}; font-size: 11px; }}
#cardMsg {{ color: {TEXT}; font-size: 12px; }}
#cardMsg[bad="true"] {{ color: {RED}; }}
#cardInput {{ color: #E6F1FF; background: rgba(10,14,26,0.9); border: 1px solid {LINE};
              border-radius: 6px; padding: 9px 11px; font-family: {MONO}; font-size: 12px; }}
#cardInput:focus {{ border-color: {NEON}; }}
"""


def _pil_to_qimage(im):
    im = im.convert("RGB")
    qimg = QImage(im.tobytes(), im.width, im.height, im.width * 3, QImage.Format_RGB888)
    return qimg.copy()          # 脱离 Python 缓冲区，允许跨线程持有


def _round_mask(im):
    from PIL import Image, ImageDraw
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, im.width - 1, im.height - 1), fill=255)
    out = Image.new("RGB", im.size, (16, 22, 36))
    out.paste(im.convert("RGB"), (0, 0), mask)
    return out


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class FrameBridge(QWidget):
    """采集/扫描线程 → UI 线程 的事件桥（Signal 自动排队跨线程）。"""
    frame_arrived = Signal(object)          # (QImage, dark, err)
    status_changed = Signal(str, bool)      # (文本, 是否错误)
    scan_done = Signal(object)              # 扫描结果 payload dict
    lost = Signal()                         # 设备失联
    quality_restart = Signal()              # 画质档切换：旧采集已收干净，请按新档重启


class TitleBar(QFrame):
    """无边框自绘标题栏：拖拽移动 + 最小化/关闭。"""

    def __init__(self, subtitle: str, on_close):
        super().__init__()
        self.setFixedHeight(46)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 8, 0)
        lay.setSpacing(10)
        logo = QLabel("◈")
        logo.setStyleSheet(f"color:{NEON}; font-size:17px; background:transparent;")
        title = QLabel("WSCRCPY", objectName="title")
        self.subtitle = QLabel(subtitle, objectName="subtitle")
        lay.addWidget(logo)
        lay.addWidget(title)
        lay.addSpacing(14)
        lay.addWidget(self.subtitle)
        lay.addStretch(1)
        self.btn_min = QPushButton("—", objectName="chromeBtn")
        self.btn_min.setCursor(Qt.PointingHandCursor)
        self.btn_min.clicked.connect(lambda: self.window().showMinimized())
        self.btn_close = QPushButton("✕", objectName="chromeBtn")
        self.btn_close.setObjectName("closeBtn")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(on_close)
        for b in (self.btn_min, self.btn_close):
            lay.addWidget(b)

    def set_subtitle(self, text: str):
        self.subtitle.setText(text)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.window().windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, ev):
        w = self.window()
        w.showNormal() if w.isMaximized() else w.showMaximized()


class VideoPanel(QWidget):
    """HUD 视频面板：画面居中 + 青色角标括号 + 扫描线；无画面时显示占位文案。"""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(860, 540)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(f"background:{PANEL}; border:1px solid {LINE}; border-radius:8px;")
        self._pixmap: Optional[QPixmap] = None
        self._video_rect = QRectF()
        self._ph_title = "未发现设备"
        self._ph_sub = "请连接设备后点击「⟳ 刷新」"

    def set_frame(self, qimg: QImage):
        box = self.rect().adjusted(26, 26, -26, -26)
        # ⚠ 高清屏（Retina）上必须**按物理像素**缩放，再把 devicePixelRatio 设回去。
        # 老写法按逻辑像素缩放（Pixmap dpr=1），Qt 绘制时又要把它放大 2 倍填满 2x 背板
        # ——两次重采样，本来就只有 462px 的源图会被二次糊化。现在只采样一次。
        dpr = float(self.devicePixelRatioF() or 1.0)
        pw = max(1, int(round(box.width() * dpr)))
        ph = max(1, int(round(box.height() * dpr)))
        pm = QPixmap.fromImage(qimg).scaled(pw, ph,
                                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self._pixmap = pm
        sz = pm.deviceIndependentSize()          # 逻辑尺寸（= 物理 / dpr）
        w, h = sz.width(), sz.height()
        self._video_rect = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        self.update()

    def clear_frame(self, title: str, sub: str):
        self._pixmap = None
        self._ph_title, self._ph_sub = title, sub
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(PANEL))
        if self._pixmap:
            p.drawPixmap(int(self._video_rect.x()), int(self._video_rect.y()), self._pixmap)
            self._draw_brackets(p, self._video_rect)
        else:
            p.setPen(QPen(QColor(DIM)))
            f = QFont()
            f.setFamilies(["PingFang SC", "Microsoft YaHei", "Sans Serif"])
            f.setPixelSize(22)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, -30, 0, -30), Qt.AlignCenter, self._ph_title)
            f.setPixelSize(13)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 20, 0, 20), Qt.AlignCenter, self._ph_sub)
        p.setPen(QPen(QColor(0, 229, 255, 8), 1))
        for y in range(0, self.height(), 4):        # 扫描线
            p.drawLine(0, y, self.width(), y)
        p.end()

    def _draw_brackets(self, p: QPainter, r: QRectF):
        arm, inset = 20.0, 10.0
        p.setPen(QPen(QColor(NEON), 2))
        x0, y0, x1, y1 = r.left() - inset, r.top() - inset, r.right() + inset, r.bottom() + inset
        for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                               (x0, y1, 1, -1), (x1, y1, -1, -1)):
            p.drawLine(QPointF(cx, cy + dy * arm), QPointF(cx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx + dx * arm, cy))


class SettingsOverlay(QFrame):
    """「设置」浮层：整窗半透明遮罩 + 居中卡片。

    只配置一件事：DevEco Testing 安装路径（用于找 agent.so 开原生画质）。
    遮罩铺满主窗口，任何窗口尺寸变化都由父窗口 resizeEvent 同步。
    """

    HINT = "如要开启原生画质，请配置DevEco Testing安装路径。"

    closed = Signal()                       # 关闭时通知父窗口（用来恢复快捷键）

    def __init__(self, parent, on_save):
        super().__init__(parent)
        self.setObjectName("overlay")
        self._on_save = on_save                 # 回调 (输入文本) -> (是否接受, 提示文案)
        self.hide()

        card = QFrame(self)
        card.setObjectName("card")
        card.setFixedWidth(560)
        self.card = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        title = QLabel("设置", card)
        title.setObjectName("cardTitle")
        # 提示语在输入框**上方**（按需求原文）
        self.hint = QLabel(self.HINT, card)
        self.hint.setObjectName("cardHint")
        self.hint.setWordWrap(True)
        self.input = QLineEdit(card)
        self.input.setObjectName("cardInput")
        self.input.setPlaceholderText("例如 /Applications/DevEco Testing.app"
                                      " 或 C:\\Program Files\\Huawei\\DevEco Testing")
        self.input.returnPressed.connect(self._save)
        note = QLabel("填 DevEco Testing 安装目录，或直接填 uitest_agent_v*.so 文件；"
                      "留空＝按默认顺序自动查找。保存后立即生效。", card)
        note.setObjectName("cardNote")
        note.setWordWrap(True)
        self.msg = QLabel("", card)
        self.msg.setObjectName("cardMsg")
        self.msg.setWordWrap(True)
        self.msg.hide()

        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_browse = QPushButton("浏览…", card)
        self.btn_browse.setProperty("neon", True)
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.clicked.connect(self._browse)
        self.btn_cancel = QPushButton("取消", card)
        self.btn_cancel.setProperty("neon", True)
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.close_overlay)
        self.btn_save = QPushButton("保存", card)
        self.btn_save.setProperty("neon", True)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self._save)
        row.addWidget(self.btn_browse)
        row.addStretch(1)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_save)

        lay.addWidget(title)
        lay.addWidget(self.hint)
        lay.addWidget(self.input)
        lay.addWidget(note)
        lay.addWidget(self.msg)
        lay.addSpacing(4)
        lay.addLayout(row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        outer.addWidget(card, 0, Qt.AlignHCenter)
        outer.addStretch(1)

    # ---- 生命周期 ----
    def open_overlay(self, current: str):
        self.input.setText(current or "")
        self._set_msg("", bad=False)
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.input.setFocus()
        self.input.selectAll()

    def close_overlay(self):
        if not self.isVisible():
            return
        self.hide()
        self.closed.emit()

    def sync_geometry(self):
        if self.isVisible():
            self.setGeometry(self.parentWidget().rect())

    def _set_msg(self, text: str, bad: bool):
        self.msg.setText(text)
        self.msg.setProperty("bad", "true" if bad else "false")
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)
        self.msg.setVisible(bool(text))

    def _browse(self):
        """挑目录优先（DevEco 安装路径就是目录）；也允许直接挑 so 文件。"""
        start = self.input.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "选择 DevEco Testing 安装目录", start)
        if d:
            self.input.setText(d)
            return
        f, _ = QFileDialog.getOpenFileName(self, "或直接选择 agent.so", start,
                                           "agent.so (uitest_agent_v*.so);;所有文件 (*)")
        if f:
            self.input.setText(f)

    def _save(self):
        ok, text = self._on_save(self.input.text())
        self._set_msg(text, bad=not ok)
        if ok:
            self.close_overlay()

    # ---- 交互：点遮罩空白处关闭、Esc 关闭 ----
    def mousePressEvent(self, ev):
        if not self.card.geometry().contains(ev.position().toPoint()):
            self.close_overlay()
            return
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.close_overlay()
            return
        super().keyPressEvent(ev)


class MirrorWindow(QWidget):
    """主窗口：标题栏 + HUD 条 + 视频面板 + 状态栏 + 按钮栏。

    未连接设备也可启动：面板显示占位提示，点「⟳ 刷新」扫描并进入投屏；
    投屏中设备失联自动回到未连接态。
    """

    # agent 推流画质档位：(标签, scale)。手表 466×466 圆屏，0.99 → 462×462 近原生。
    QUALITY_PRESETS = (("原生", 0.99), ("清晰", 0.8), ("流畅", 0.5))

    def __init__(self, interval: float = 0.35, round_mode: str = "auto", mode: str = "auto",
                 serial: Optional[str] = None, hdc_path: Optional[str] = None,
                 wakeup: bool = True, auto_record: Optional[str] = None,
                 test_record_path: Optional[str] = None, test_shot_path: Optional[str] = None,
                 agent_scale: float = 0.99, agent_so: Optional[str] = None):
        super().__init__(objectName="root")
        self.setWindowTitle("WSCRCPY")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setStyleSheet(QSS)
        self.resize(940, 770)

        self.interval = interval
        self.round_mode = round_mode
        self.mode = mode
        self._desired_serial = serial
        self._hdc_path = hdc_path
        self.do_wakeup = wakeup
        self.test_record_path = test_record_path
        self.test_shot_path = test_shot_path
        self._auto_record = auto_record          # 连接成功后自动开始录制的路径
        self._scanning = False

        self.hdc: Optional[Hdc] = None
        self._last_hdc: Optional[Hdc] = None     # 供退出清理使用
        self.cap: Optional[BaseCapture] = None
        self.stream: Optional[H264Stream] = None
        self.decoder: Optional[H264Decoder] = None
        self.stream_rec: Optional[StreamRecorder] = None
        # loop(设备端截图连拍)已并入 pull；agent 单列（见 _mode_chain）
        self.mode = "pull" if mode == "loop" else mode
        self._dtype = ""                           # 连接后填入，供 _mode_chain 判断 wearable
        self._agent_probe = None                   # 扫描线程探好的 (可用, 说明)
        self.agent_scale = agent_scale
        self.agent_so = agent_so
        self._src_size: Optional[tuple] = None     # 最近一帧的实际像素尺寸（HUD 显示用）
        self._quality_switching = False            # 画质档切换中：期间忽略重复点击
        self._watchdog = None                      # 启动兜底定时器（只建一次，见 _arm_bringup_watchdog）
        self.effective_mode = self.mode            # 回落链实际命中的那一档
        self.recorder: Optional[Recorder] = None
        self.rec_t0: Optional[float] = None
        self._last_image: Optional[QImage] = None
        self.frames_rendered = 0
        self._t0 = time.monotonic()

        self.bridge = FrameBridge()
        self.bridge.frame_arrived.connect(self._on_frame)
        self.bridge.status_changed.connect(self._on_status)
        self.bridge.scan_done.connect(self._on_scan)
        self.bridge.lost.connect(lambda: self._enter_disconnected("设备连接已断开，请重新连接后点「⟳ 刷新」"))
        self.bridge.quality_restart.connect(self._restart_capture)

        self._build_ui()
        self._bind_keys()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.title = TitleBar("未连接", self.close)
        root.addWidget(self.title)

        hud_wrap = QHBoxLayout()
        hud_wrap.setContentsMargins(26, 6, 26, 4)
        self.hud = QLabel("DEV —   FPS —   FRM 0000   T+00:00", objectName="hud")
        hud_wrap.addWidget(self.hud)
        hud_wrap.addStretch(1)
        root.addLayout(hud_wrap)

        video_wrap = QHBoxLayout()
        video_wrap.setContentsMargins(18, 4, 18, 4)
        self.panel = VideoPanel()
        video_wrap.addWidget(self.panel)
        root.addLayout(video_wrap, 1)

        status_wrap = QHBoxLayout()
        status_wrap.setContentsMargins(26, 2, 26, 6)
        self.status = QLabel("正在扫描设备…", objectName="status")
        status_wrap.addWidget(self.status)
        status_wrap.addStretch(1)
        root.addLayout(status_wrap)

        btn_wrap = QHBoxLayout()
        btn_wrap.setContentsMargins(24, 0, 24, 20)
        btn_wrap.setSpacing(14)
        self.btn_refresh = QPushButton("⟳ 刷新")
        self.btn_refresh.setProperty("neon", True)
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh)
        btn_wrap.addWidget(self.btn_refresh)
        # 画质档位：只影响 agent 推流分辨率（pull 本来就是原生截图）
        self.btn_quality = QPushButton()
        self.btn_quality.setProperty("neon", True)
        self.btn_quality.setCursor(Qt.PointingHandCursor)
        self.btn_quality.clicked.connect(self.cycle_quality)
        btn_wrap.addWidget(self.btn_quality)
        self._sync_quality_btn()
        btn_wrap.addStretch(1)
        self.btn_rec = QPushButton("● 录制")
        self.btn_shot = QPushButton("⧉ 截屏")
        self.btn_quit = QPushButton("⏻ 退出")
        for b in (self.btn_rec, self.btn_shot, self.btn_quit):
            b.setProperty("neon", True)
            b.setCursor(Qt.PointingHandCursor)
            btn_wrap.addWidget(b)
        btn_wrap.addStretch(1)
        # 设置放**最右**：与设备无关的本地配置，未连接也要能点
        self.btn_settings = QPushButton("⚙ 设置")
        self.btn_settings.setProperty("neon", True)
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.setToolTip("配置 DevEco Testing 安装路径（用于开启原生画质推流）")
        btn_wrap.addWidget(self.btn_settings)
        root.addLayout(btn_wrap)

        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_shot.clicked.connect(self.save_screenshot)
        self.btn_quit.clicked.connect(self.close)
        self.btn_settings.clicked.connect(self.open_settings)

        # 设置浮层：铺满主窗口的遮罩 + 居中卡片（geometry 由 resizeEvent 跟随）
        self.settings = SettingsOverlay(self, self._on_settings_save)
        self.settings.closed.connect(self._on_settings_closed)

        self.rec_timer = QTimer(self)
        self.rec_timer.timeout.connect(self._tick_rec_time)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._update_hud)
        self._clock.start(500)
        self._set_connected_ui(False)

    def _bind_keys(self):
        # 存起来是为了在设置浮层输入时**临时停用**：这些是 WindowShortcut，
        # 不停用的话在输入框里敲 R/S 会被录制/截屏抢走（Qt 快捷键先于焦点控件处理）。
        self._shortcuts = [
            QShortcut(QKeySequence("R"), self, activated=self.toggle_record),
            QShortcut(QKeySequence("S"), self, activated=self.save_screenshot),
            QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh),
            QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close),
        ]

    # ---------- 连接状态机 ----------
    def _set_connected_ui(self, connected: bool):
        self.btn_rec.setEnabled(connected)
        self.btn_shot.setEnabled(connected)
        self.btn_refresh.setVisible(not connected)
        self.btn_quality.setVisible(connected)

    # ---------- 画质档位 ----------
    def _quality_index(self) -> int:
        """当前 scale 命中的档位下标；无精确匹配时取最接近的一档。"""
        best, dist = 0, None
        for i, (_, s) in enumerate(self.QUALITY_PRESETS):
            d = abs(s - self.agent_scale)
            if d < 1e-9:
                return i
            if dist is None or d < dist:
                best, dist = i, d
        return best

    def _sync_quality_btn(self):
        i = self._quality_index()
        label, scale = self.QUALITY_PRESETS[i]
        if abs(scale - self.agent_scale) > 1e-9:
            label = f"自定义 {self.agent_scale:g}"
        self.btn_quality.setText(f"◐ 画质 {label}")
        self.btn_quality.setToolTip(
            "agent 推流分辨率档位（点击切换，切换即重启采集）：\n"
            # 尺寸按设备端的 ceil(分辨率*scale) 算，和真机实测一致（0.99→462、0.8→373、0.5→233）
            + "\n".join(f"  {lb}  scale {sc}  ≈ {math.ceil(466 * sc)}×{math.ceil(466 * sc)}"
                        for lb, sc in self.QUALITY_PRESETS)
            + f"\n当前：scale {self.agent_scale:g}")

    # ---------- 设置 ----------
    def open_settings(self):
        """弹出设置浮层（预填当前配置）。"""
        for sc in getattr(self, "_shortcuts", []):
            sc.setEnabled(False)
        self.settings.open_overlay(resources.get_setting(SETTING_DEVECO_PATH))

    def _on_settings_closed(self):
        for sc in getattr(self, "_shortcuts", []):
            sc.setEnabled(True)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        overlay = getattr(self, "settings", None)   # _build_ui 之前也会触发 resize
        if overlay is not None:
            overlay.sync_geometry()

    def _on_settings_save(self, raw: str):
        """保存 DevEco Testing 路径。返回 (是否接受, 给用户看的提示)。"""
        text = (raw or "").strip()
        if not text:
            where = resources.set_setting(SETTING_DEVECO_PATH, "")
            clear_so_cache()
            logging.info("设置：清空 DevEco Testing 路径（写盘=%r）", where)
            self._on_status("已清空 DevEco Testing 路径，恢复自动查找", err=False)
            self._maybe_restart_for_settings()
            return True, "已清空，恢复自动查找"

        ok, msg = describe_deveco_path(text)
        if not ok:
            logging.warning("设置：路径不可用 %r —— %s", text, msg)
            return False, msg

        where = resources.set_setting(SETTING_DEVECO_PATH, text)
        clear_so_cache()
        logging.info("设置：DevEco 路径 = %r（%s），写盘=%r", text, msg, where)
        note = "" if where else "（配置文件写入失败，仅本次运行生效）"
        self._on_status(f"设置已保存：{msg}{note}", err=False)
        self._maybe_restart_for_settings()
        return True, f"已保存：{msg}{note}"

    def _maybe_restart_for_settings(self):
        """设置改了 so 来源：agent 推流中就用新配置重启采集，否则下次连接自然生效。"""
        if self.hdc is None or self.effective_mode != "agent":
            return
        self._on_status("设置已更新，正在按新配置重启采集…", err=False)
        self._restart_capture_async("设置变更")

    # ---------- 采集重启（画质切换 / 设置变更共用）----------
    def _restart_capture_async(self, why: str) -> bool:
        """收干净旧采集后重启；成功排上队返回 True。"""
        if self._quality_switching:
            self._on_status("采集正在重启中，请稍候…", err=False)
            return False
        logging.info("%s：重启采集", why)
        # 置灰 + 置位：重启完成前不接受第二次点击（否则两个重启流程会互相踩）
        self.btn_quality.setEnabled(False)
        self._quality_switching = True

        def work():
            # 收旧采集必须在**工作线程**里等它彻底死掉：AgentCapture 收尾会
            # `pkill uitest.*start-daemon`，若旧线程还活着就起了新的，旧线程的 pkill
            # 会把新 daemon 一起杀掉（实测现象：切完画质 2 秒后「设备连接已断开」）。
            self._teardown_capture(wait=20.0)
            self.bridge.quality_restart.emit()

        threading.Thread(target=work, daemon=True).start()
        return True

    def cycle_quality(self):
        """切换 agent 推流分辨率；正在 agent 推流时立即按新档重启采集。"""
        if self._quality_switching:
            self._on_status("画质正在切换中，请稍候…", err=False)
            return
        i = self._quality_index()
        _, scale = self.QUALITY_PRESETS[(i + 1) % len(self.QUALITY_PRESETS)]
        self.agent_scale = scale
        self._sync_quality_btn()
        text = self.btn_quality.text().strip()
        if self.hdc is None:
            return
        if self.effective_mode != "agent":
            self._on_status("画质档位只对 agent 推流生效"
                            "（当前是 pull 逐帧截图，本身就是原生分辨率）", err=False)
            return
        self._on_status(f"{text}，正在按新档重启采集…", err=False)
        self._restart_capture_async(f"切换画质 scale={scale:g}")

    def _restart_capture(self):
        """UI 线程槽：旧采集已收干净，按新画质档重启（采集本身在各自线程里跑）。"""
        if self.hdc is None:
            self._quality_switching = False
            return
        self.frames_rendered = 0
        self._src_size = None
        self._t0 = time.monotonic()
        self._start_capture()
        self._arm_bringup_watchdog()
        self._quality_switching = False

    def refresh(self):
        """扫描设备：找到即自动连接进入投屏；否则停留在未连接态提示。"""
        if self._scanning:
            return
        self._scanning = True
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("扫描中…")
        self.status.setText("正在扫描设备…")

        def work():
            payload = {"serials": [], "hdc": None, "err": "", "dtype": "", "agent": None}
            try:
                hdc_path = self._hdc_path or resources.find_hdc()
                if not hdc_path:
                    payload["err"] = ("找不到 hdc：程序内置资源缺失，且 PATH 中也没有 hdc。"
                                      "请安装 DevEco Command Line Tools 后重试")
                else:
                    serials = Hdc.list_devices(
                        hdc_path,
                        on_progress=lambda m: self.bridge.status_changed.emit(m, False))
                    if self._desired_serial:
                        serials = [s for s in serials if s == self._desired_serial]
                        if not serials:
                            payload["err"] = f"指定的设备 {self._desired_serial} 未连接"
                    if serials:
                        h = Hdc(hdc_path, serials[0])
                        if self.do_wakeup:
                            try:
                                h.wakeup()
                            except HdcError:
                                pass
                        # dtype 与 agent 可用性一并在这里探好：它们都是 hdc 调用
                        # （device_type 2 次 shell、agent_probe 2 次 shell，合计最坏
                        # 几十秒），放到 UI 线程会把窗口冻成「一直 loading」。
                        try:
                            payload["dtype"] = h.device_type()
                        except HdcError:
                            pass
                        payload["agent"] = agent_supported(h, self.agent_so)
                        payload["serials"], payload["hdc"] = serials, h
            except HdcError as e:
                payload["err"] = str(e)
            except Exception as e:          # 兜底：扫描线程绝不能静默死掉
                payload["err"] = f"扫描异常：{type(e).__name__}: {e}"
                logging.exception("扫描设备失败")
            finally:
                # 无论成败都必须回报：漏掉 emit 会让 _scanning 永远为 True，
                # 界面就永久停在「正在扫描设备… / 扫描中…」且按钮点不动。
                self.bridge.scan_done.emit(payload)

        threading.Thread(target=work, daemon=True).start()

    def _on_scan(self, payload: dict):
        self._scanning = False
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("⟳ 刷新")
        if payload["hdc"] is None:
            self._enter_disconnected(payload["err"] or "未发现设备")
        else:
            self._agent_probe = payload.get("agent")
            self._enter_connected(payload["hdc"], payload["serials"],
                                  payload.get("dtype", ""))

    def _teardown_capture(self, wait: float = 3.0):
        """收掉当前帧采集线程 + stream 档残留。

        重连/重试前必须先收：否则旧 cap 还活着，新 cap 会和它抢同一个 fport/端口，
        表现成「刚连上就报设备连接已断开」。

        `wait` 是等采集线程真正退出的秒数。**必须等干净**：AgentCapture 收尾时会
        `pkill uitest.*start-daemon` 并删掉设备上的 agent.so，若旧线程还活着就起了新的，
        旧线程的 pkill 会把**新 daemon** 一起杀掉（现象：切完画质 2 秒后「连接已断开」）。
        调用方若紧接着要起新采集，就传足够大的 wait，别用默认值。
        """
        if self.cap:
            cap, self.cap = self.cap, None
            cap.stop()
            try:
                cap.join(timeout=wait)
            except Exception:
                logging.debug("采集线程 join 失败（忽略）", exc_info=True)
            if cap.is_alive():
                logging.warning("采集线程 %.1fs 内未退出（其收尾可能误杀新 daemon）", wait)
        self._teardown_stream()

    def _enter_connected(self, hdc: Hdc, serials: list, dtype: str = ""):
        self._teardown_capture()          # 重复连接前先收旧的（见方法注释）
        self.hdc = hdc
        self._last_hdc = hdc
        self._dtype = dtype
        self.round = (self.round_mode == "on") or \
                     (self.round_mode == "auto" and "wearable" in dtype)
        self.title.set_subtitle(f"{dtype or 'DEVICE'} · {hdc.serial}")
        note = f"（共 {len(serials)} 台，已连第一台）" if len(serials) > 1 else ""
        self.frames_rendered = 0
        self._src_size = None
        self._t0 = time.monotonic()
        self._set_connected_ui(True)
        # 先落日志再启采集：_start_capture 里若卡在某个 hdc 调用上，日志里至少
        # 留下「已连上谁、什么类型」，否则只剩上一行，无法判断卡在哪一步。
        logging.info("connected: %s dtype=%s", hdc.serial, dtype)
        self._on_status(f"已连接 {hdc.serial}{note}，正在启动采集…", err=False)
        self._start_capture()
        self._arm_bringup_watchdog()
        if self._auto_record:
            path = self._auto_record
            self._auto_record = None
            self.test_record_path = path
            self.toggle_record()

    def _arm_bringup_watchdog(self):
        """启动兜底：迟迟不出画面时把「⟳ 刷新」放回来，别让界面永久无法恢复。

        连接成功后刷新按钮是被隐藏的（投屏态）；万一采集在某个 hdc 调用上卡住，
        用户就会看到「一直 loading」且无按钮可点，只能强杀进程。这里给一条退路。

        定时器**只建一次**（重复 arm 只是重置计时）：早先每次新建一个 QTimer(self)，
        旧定时器被 parent 持有不会销毁，切画质/重连几次就会攒下一串旧定时器，
        它们会在新采集刚启动、还没出首帧时跳出「启动超时」的假警报。
        """
        def check():
            if self.hdc is None or self.frames_rendered > 0:
                return
            self.btn_refresh.setVisible(True)
            self.btn_refresh.setEnabled(True)
            self._on_status("启动超时仍未收到画面：可点「⟳ 刷新」重试"
                            "（或检查设备授权 / USB 连接）", err=True)
        if self._watchdog is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(check)
            self._watchdog = t
        self._watchdog.start(45000)

    def _enter_disconnected(self, msg: str):
        if self.stream_rec and self.stream_rec.recording:
            kind, text = self.stream_rec.stop()    # 掉线先保住已录流
            if kind == "done":
                self._on_status(f"掉线前录制已保存 {text}", err=False)
        if self.recorder and self.recorder.recording:
            self._stop_record()                    # 掉线先保住已录帧
        self._teardown_capture()
        if self._watchdog is not None:
            self._watchdog.stop()                  # 已掉线就别再报「启动超时」
        self.hdc = None
        self._set_connected_ui(False)
        self.panel.clear_frame("未发现设备", "请连接设备后点击「⟳ 刷新」开始投屏")
        self._last_image = None
        self._src_size = None
        self.title.set_subtitle("未连接")
        self._on_status(msg, err=True)

    # 采集回落链：auto 走满三档；显式指定则只在其后追加更保守的档
    _MODE_CHAINS = {
        "auto":   ["stream", "agent", "pull"],
        "stream": ["stream", "agent", "pull"],
        "agent":  ["agent", "pull"],
        "pull":   ["pull"],
    }

    def _mode_chain(self):
        chain = list(self._MODE_CHAINS.get(self.mode, ["pull"]))
        # 手表虚拟屏不产出帧（PLAN 11.2），stream 档必然失败且要耗 10~15s 才超时：
        # 直接跳过，省启动时间，也避免留下半死的 H264Stream（其收尾会 pkill uitest daemon）
        if "stream" in chain and "wearable" in self._dtype:
            chain.remove("stream")
            logging.info("wearable 设备跳过 stream 档（虚拟屏零帧）")
        return chain

    def _start_capture(self):
        """按回落链起采集，逐档降级，最后一档是 pull（截图 0.6-1.6fps）。

        stream: H.264 视频流（手机 30fps+）；agent: 设备端变化触发 JPEG 推流
        （手表实测 ~30fps，但画面静止时不推帧）；pull: PC 逐帧截图（最稳、最慢）。
        """
        chain = self._mode_chain()
        errors = []
        for m in chain:
            try:
                if m == "stream":
                    self._start_stream()
                elif m == "agent":
                    self._start_agent()
                else:
                    self._start_pull()
            except Exception as e:              # 含 HdcError / grpc / av / OSError
                errors.append(f"{m}: {str(e)[:70]}")
                logging.warning("%s 模式启动失败，尝试下一档: %s", m, e)
                # 关键：失败的 stream 尝试会留下 H264Stream/H264Decoder，其 stop() 会
                # pkill uitest daemon —— 留着它会在关窗时把 agent 档正在用的 daemon 杀掉。
                self._teardown_stream()
                continue
            self.effective_mode = m
            self.btn_quality.setEnabled(m == "agent")   # pull 已是原生分辨率，无需档位
            if errors:
                self._on_status(f"已降级为 {m} 模式（{'；'.join(errors)}）", err=True)
            elif m == "agent":
                self._on_status("agent 推流中（画面变化时推送）", err=False)
            return
        self.btn_quality.setEnabled(False)
        self._on_status("所有采集模式均不可用：" + "；".join(errors), err=True)
        self.bridge.lost.emit()

    def _start_agent(self):
        # 扫描线程已探过一次（agent_supported 含 2 次 hdc shell），这里直接用结果，
        # 避免在 UI 线程再等一遍 hdc；只有探针缺失（异常路径）才现探。
        probe = self._agent_probe
        ok, why = probe if probe else agent_supported(self.hdc, self.agent_so)
        logging.info("agent 通道探针: ok=%s %s", ok, why)
        if not ok:
            raise HdcError(why)
        cap = AgentCapture(self.hdc, self.interval, self._worker_frame,
                           scale=self.agent_scale, so_path=self.agent_so,
                           on_status=lambda m: self.bridge.status_changed.emit(m, True),
                           on_lost=self.bridge.lost.emit)
        cap.start()
        self.cap = cap
        logging.info("agent 模式: %s", why)

    def _start_pull(self):
        self.cap = PulledCapture(self.hdc, self.interval, self._worker_frame,
                                 on_status=lambda m: self.bridge.status_changed.emit(m, True),
                                 on_lost=self.bridge.lost.emit)
        self.cap.start()

    def _start_stream(self):
        def on_stream_status(msg: str):
            self.bridge.status_changed.emit(msg, True)
            if "中断" in msg:
                self.bridge.lost.emit()       # 流断开视同掉线，回未连接态
        self.stream = H264Stream(self.hdc, on_frame=self._on_stream_frame,
                                 on_status=on_stream_status)
        self.decoder = H264Decoder(
            on_image=lambda img: self.bridge.frame_arrived.emit((img, False, None)))
        self.decoder.start()
        self.stream.start(timeout=15)
        logging.info("stream 模式启动: so=%s", self.stream.so_path)

    def _on_stream_frame(self, flags: int, data: bytes, pts: int):
        """流回调（grpc 线程）：喂解码器 + 喂流录制器。"""
        if self.decoder:
            self.decoder.feed(flags, data)
        rec = self.stream_rec
        if rec and rec.recording:
            rec.write(flags, data, pts)

    def _teardown_stream(self):
        """收掉 stream 档留下的一切。

        ⚠ 必须在回落到 agent 档时立刻调用：`H264Stream._teardown()` 会
        `pkill -9 -f 'uitest.*start-daemon'`，而 agent 档用的正是同一个 singleness
        daemon —— 留着半死的 H264Stream，关窗时 `stop()` 会把 agent 的 daemon 一起杀掉，
        表现成「刚关闭就报设备连接已断开」。
        """
        if self.decoder:
            self.decoder.stop()
            self.decoder = None
        if self.stream:
            try:
                self.stream.stop()
            except Exception:
                logging.warning("stream 收尾失败（忽略）", exc_info=True)
            self.stream = None

    def stop(self):
        # 先停帧采集（agent/pull 的线程），再收 stream —— 反过来 stream 的收尾会
        # pkill 掉 agent 正在用的 uitest daemon，让采集线程误报「连接已断开」。
        if self.cap:
            self.cap.stop()
            self.cap.join(timeout=3)
        self._teardown_stream()
        if self.stream_rec and self.stream_rec.recording:
            kind, text = self.stream_rec.stop()
            if kind == "saved_frames":
                self._on_status(text, err=True)
        rec = self.recorder
        if rec and rec.recording:
            kind, _ = rec.stop()
            if kind == "composing" and rec.last_thread:
                rec.last_thread.join(timeout=60)   # 关窗前确保 mp4 落盘

    def _worker_frame(self, frame: Frame):
        """采集线程回调：喂录制器 + 圆屏遮罩 + PIL→QImage，经信号送 UI。"""
        rec = self.recorder
        if rec and rec.recording:
            rec.write(frame)
        im = frame.image
        if im is not None:
            # 记录**采集侧真实像素尺寸**：它是画面清晰度的上限（HUD 显示），
            # 也让「显示发糊」能一眼区分是源分辨率低还是窗口放大所致。
            self._src_size = im.size
        if im is not None and self.round:
            im = _round_mask(im)
        qimg = _pil_to_qimage(im) if im is not None else QImage()
        self.bridge.frame_arrived.emit((qimg, frame.dark, frame.error))

    # ---------- UI 线程槽 ----------
    def _on_frame(self, payload):
        qimg, dark, err = payload
        note = ""
        if not qimg.isNull():
            self.panel.set_frame(qimg)
            self._last_image = qimg
            self.frames_rendered += 1
            if dark:
                note = "  ⚠ 黑帧/隐私页"
        if err:
            note = f"✗ {err[:80]}"
        self.status.setText(f"实时画面正常{note}" if not err else note)

    def _on_status(self, text, err=False):
        self.status.setText(text)
        if err:
            logging.warning("status: %s", text)

    def _update_hud(self):
        if not self.hdc:
            return
        fps = (self.frames_rendered / (time.monotonic() - self._t0)) if self.frames_rendered else 0
        el = int(time.monotonic() - self._t0)
        rec = f"   ● REC {int(time.monotonic() - self.rec_t0)}s" if self.rec_t0 else ""
        src = f"   {self._src_size[0]}×{self._src_size[1]}" if self._src_size else ""
        self.hud.setText(f"DEV {self.hdc.serial}   FPS {fps:.1f}   FRM {self.frames_rendered:04d}"
                         f"{src}   T+{el // 60:02d}:{el % 60:02d}{rec}")

    def _tick_rec_time(self):
        if self.rec_t0:
            s = int(time.monotonic() - self.rec_t0)
            self.btn_rec.setText(f"■ 停止 {s // 60:02d}:{s % 60:02d}")

    # ---------- 录制 / 截屏 ----------
    def toggle_record(self):
        if not self.hdc:
            self._on_status("未连接设备，无法录制", err=True)
            return
        if self.stream_rec and self.stream_rec.recording:
            self._stop_record()
        elif self.recorder and self.recorder.recording:
            self._stop_record()
        else:
            self._start_record()

    def _ask_path(self, title: str, flt: str, suffix: str) -> Optional[str]:
        injected = self.test_record_path if suffix == "mp4" else self.test_shot_path
        if injected:
            return injected
        default = str(resources.default_save_dir() /
                      f"wscrcpy_{datetime.datetime.now():%Y%m%d_%H%M%S}.{suffix}")
        path, _ = QFileDialog.getSaveFileName(self, title, default, flt)
        return path or None

    def _start_record(self):
        path = self._ask_path("录制保存为", "MP4 视频 (*.mp4)", "mp4")
        if not path:
            return
        if self.effective_mode == "stream":
            self.stream_rec = StreamRecorder(path)
            pre = []
            if self.stream:
                if self.stream.last_config:
                    pre.append(self.stream.last_config)
                if self.stream.last_idr:
                    pre.append(self.stream.last_idr)
            self.stream_rec.start(prefill=pre)
            self.rec_t0 = time.monotonic()
            self.btn_rec.setProperty("rec", True)
            self.btn_rec.setText("■ 停止 00:00")
            _repolish(self.btn_rec)
            self.rec_timer.start(500)
            self.status.setText(f"录制中（视频流直录）→ {path}")
            logging.info("stream record start: %s", path)
            return
        self.recorder = Recorder(path, scale=1,
                                 on_compose_done=lambda ok, p: self.bridge.status_changed.emit(
                                     f"已保存 {p}" if ok else f"合成失败（帧已保留）：{p}", not ok))
        self.recorder.start()
        self.rec_t0 = time.monotonic()
        self.btn_rec.setProperty("rec", True)
        self.btn_rec.setText("■ 停止 00:00")
        _repolish(self.btn_rec)
        self.rec_timer.start(500)
        self.status.setText(f"录制中 → {path}")
        logging.info("record start: %s", path)

    def _stop_record(self):
        if self.stream_rec and self.stream_rec.recording:
            self.rec_t0 = None
            self.rec_timer.stop()
            kind, text = self.stream_rec.stop()
            self.btn_rec.setProperty("rec", False)
            self.btn_rec.setText("● 录制")
            _repolish(self.btn_rec)
            if kind == "done":
                self.status.setText(f"已保存 {text}（{getattr(self.stream_rec, 'frames', 0)} 帧，无重编码）")
            else:
                self._on_status(text, kind == "empty")
            logging.info("stream record stop: %s %s", kind, text)
            return
        rec = self.recorder
        if not rec:
            return
        self.rec_t0 = None
        self.rec_timer.stop()
        kind, text = rec.stop()
        self.btn_rec.setProperty("rec", False)
        self.btn_rec.setText("● 录制")
        _repolish(self.btn_rec)
        if kind == "composing":
            self.status.setText("合成中…（后台进行，完成后提示）")
        else:
            self._on_status(text, kind != "empty")
        logging.info("record stop: %s %s", kind, text)

    def save_screenshot(self):
        if not self.hdc or self._last_image is None:
            self.status.setText("✗ 未连接设备或尚无画面，无法截取")
            return
        # 无条件复制到系统剪贴板，与是否保存文件无关
        QApplication.clipboard().setImage(self._last_image)
        path = self._ask_path("截屏保存为", "PNG 图片 (*.png)", "png")
        if path:
            self._last_image.save(path)
            self.status.setText(f"已保存 {path}（并已复制到剪贴板）")
            logging.info("shot: %s (copied to clipboard)", path)
        else:
            self.status.setText("截图已复制到剪贴板")
            logging.info("shot: clipboard only")

    # ---------- 关闭 ----------
    def closeEvent(self, ev):
        self.stop()
        self._start_cleanup()
        ev.accept()

    def _start_cleanup(self):
        """退出清理：设备端临时帧目录 + 本机中转临时文件（后台 best-effort）。"""
        hdc = self._last_hdc

        def work():
            try:
                if hdc:
                    hdc.shell("rm", "-rf", TMP_DIR, timeout=8)
            except Exception:
                pass
            try:
                os.remove(HOST_TEMP)
            except OSError:
                pass

        threading.Thread(target=work, daemon=True).start()
