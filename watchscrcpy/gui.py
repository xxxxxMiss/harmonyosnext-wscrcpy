"""PySide6 深色控制台 GUI：无边框自绘窗口 + HUD 视频面板 + 霓虹按钮。

连接状态机：未连接（面板占位 + 刷新按钮）→ 投屏中（帧渲染/录制/截屏）
→ 掉线自动回到未连接态。无设备也能正常启动，不拦截。
采集线程经 Signal 跨线程送帧，QImage 在工作线程转换、UI 线程只做缩放与绘制。
对话框路径可注入（test_record_path/test_shot_path）以便离屏自动化测试。
"""
from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import resources
from .capture import BaseCapture, Frame, LoopCapture, PulledCapture
from .hdc import HOST_TEMP, TMP_DIR, Hdc, HdcError
from .recorder import Recorder

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
        pm = QPixmap.fromImage(qimg)
        box = self.rect().adjusted(26, 26, -26, -26)
        self._pixmap = pm.scaled(box.width(), box.height(),
                                 Qt.KeepAspectRatio, Qt.SmoothTransformation)
        w, h = self._pixmap.width(), self._pixmap.height()
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


class MirrorWindow(QWidget):
    """主窗口：标题栏 + HUD 条 + 视频面板 + 状态栏 + 按钮栏。

    未连接设备也可启动：面板显示占位提示，点「⟳ 刷新」扫描并进入投屏；
    投屏中设备失联自动回到未连接态。
    """

    def __init__(self, interval: float = 0.35, round_mode: str = "auto", mode: str = "pull",
                 serial: Optional[str] = None, hdc_path: Optional[str] = None,
                 wakeup: bool = True, auto_record: Optional[str] = None,
                 test_record_path: Optional[str] = None, test_shot_path: Optional[str] = None):
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
        btn_wrap.addStretch(1)
        self.btn_rec = QPushButton("● 录制")
        self.btn_shot = QPushButton("⧉ 截屏")
        self.btn_quit = QPushButton("⏻ 退出")
        for b in (self.btn_rec, self.btn_shot, self.btn_quit):
            b.setProperty("neon", True)
            b.setCursor(Qt.PointingHandCursor)
            btn_wrap.addWidget(b)
        btn_wrap.addStretch(1)
        root.addLayout(btn_wrap)

        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_shot.clicked.connect(self.save_screenshot)
        self.btn_quit.clicked.connect(self.close)

        self.rec_timer = QTimer(self)
        self.rec_timer.timeout.connect(self._tick_rec_time)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._update_hud)
        self._clock.start(500)
        self._set_connected_ui(False)

    def _bind_keys(self):
        QShortcut(QKeySequence("R"), self, activated=self.toggle_record)
        QShortcut(QKeySequence("S"), self, activated=self.save_screenshot)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

    # ---------- 连接状态机 ----------
    def _set_connected_ui(self, connected: bool):
        self.btn_rec.setEnabled(connected)
        self.btn_shot.setEnabled(connected)
        self.btn_refresh.setVisible(not connected)

    def refresh(self):
        """扫描设备：找到即自动连接进入投屏；否则停留在未连接态提示。"""
        if self._scanning:
            return
        self._scanning = True
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("扫描中…")
        self.status.setText("正在扫描设备…")

        def work():
            payload = {"serials": [], "hdc": None, "err": ""}
            try:
                hdc_path = self._hdc_path or resources.find_hdc()
                if not hdc_path:
                    payload["err"] = ("找不到 hdc：程序内置资源缺失，且 PATH 中也没有 hdc。"
                                      "请安装 DevEco Command Line Tools 后重试")
                else:
                    serials = Hdc.list_devices(hdc_path)
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
                        payload["serials"], payload["hdc"] = serials, h
            except HdcError as e:
                payload["err"] = str(e)
            self.bridge.scan_done.emit(payload)

        threading.Thread(target=work, daemon=True).start()

    def _on_scan(self, payload: dict):
        self._scanning = False
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("⟳ 刷新")
        if payload["hdc"] is None:
            self._enter_disconnected(payload["err"] or "未发现设备")
        else:
            self._enter_connected(payload["hdc"], payload["serials"])

    def _enter_connected(self, hdc: Hdc, serials: list):
        self.hdc = hdc
        self._last_hdc = hdc
        dtype = ""
        try:
            dtype = hdc.device_type()
        except HdcError:
            pass
        self.round = (self.round_mode == "on") or \
                     (self.round_mode == "auto" and "wearable" in dtype)
        self.title.set_subtitle(f"{dtype or 'DEVICE'} · {hdc.serial}")
        note = f"（共 {len(serials)} 台，已连第一台）" if len(serials) > 1 else ""
        self.frames_rendered = 0
        self._t0 = time.monotonic()
        self._set_connected_ui(True)
        self._start_capture()
        self.status.setText(f"已连接 {hdc.serial}{note}，正在拉取画面…")
        logging.info("connected: %s dtype=%s", hdc.serial, dtype)
        if self._auto_record:
            path = self._auto_record
            self._auto_record = None
            self.test_record_path = path
            self.toggle_record()

    def _enter_disconnected(self, msg: str):
        if self.recorder and self.recorder.recording:
            self._stop_record()                    # 掉线先保住已录帧
        if self.cap:
            self.cap.stop()
            self.cap = None
        self.hdc = None
        self._set_connected_ui(False)
        self.panel.clear_frame("未发现设备", "请连接设备后点击「⟳ 刷新」开始投屏")
        self._last_image = None
        self.title.set_subtitle("未连接")
        self._on_status(msg, err=True)

    def _start_capture(self):
        cls = LoopCapture if self.mode == "loop" else PulledCapture
        self.cap = cls(self.hdc, self.interval, self._worker_frame,
                       on_status=lambda m: self.bridge.status_changed.emit(m, True),
                       on_lost=self.bridge.lost.emit)
        self.cap.start()

    def stop(self):
        if self.cap:
            self.cap.stop()
            self.cap.join(timeout=3)
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

    def _on_status(self, text, err):
        self.status.setText(text)
        if err:
            logging.warning("status: %s", text)

    def _update_hud(self):
        if not self.hdc:
            return
        fps = (self.frames_rendered / (time.monotonic() - self._t0)) if self.frames_rendered else 0
        el = int(time.monotonic() - self._t0)
        rec = f"   ● REC {int(time.monotonic() - self.rec_t0)}s" if self.rec_t0 else ""
        self.hud.setText(f"DEV {self.hdc.serial}   FPS {fps:.1f}   FRM {self.frames_rendered:04d}"
                         f"   T+{el // 60:02d}:{el % 60:02d}{rec}")

    def _tick_rec_time(self):
        if self.rec_t0:
            s = int(time.monotonic() - self.rec_t0)
            self.btn_rec.setText(f"■ 停止 {s // 60:02d}:{s % 60:02d}")

    # ---------- 录制 / 截屏 ----------
    def toggle_record(self):
        if not self.hdc:
            self._on_status("未连接设备，无法录制", err=True)
            return
        if self.recorder and self.recorder.recording:
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
            self._on_status("未连接设备或尚无画面，无法截取", err=True)
            return
        path = self._ask_path("截屏保存为", "PNG 图片 (*.png)", "png")
        if not path:
            return
        self._last_image.save(path)
        self.status.setText(f"已保存 {path}")
        logging.info("shot: %s", path)

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
