"""录制：帧落盘 + 时间戳，停止后用 ffmpeg concat demuxer 做 VFR 合成。

帧间隔抖动大（0.2~1s 级），固定 -framerate 会变速，故用 ffconcat 的
duration 逐帧声明时长。合成在后台线程执行（GUI 不卡顿）；
ffmpeg 优先用程序内置资源，缺失时保留帧目录并给出提示。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Callable, List, Optional, Tuple

from . import resources
from .capture import Frame


class Recorder:
    def __init__(self, out_path: str, scale: int = 2, frames_dir: Optional[str] = None,
                 on_compose_done: Optional[Callable[[bool, str], None]] = None,
                 keep_frames: bool = False):
        self.out_path = os.path.abspath(out_path)
        self.scale = scale
        self.frames_dir = frames_dir or os.path.splitext(self.out_path)[0] + "_frames"
        self.on_compose_done = on_compose_done   # (成功, mp4路径或错误尾文)，在合成线程回调
        self.keep_frames = keep_frames           # True=保留中间帧目录（默认合成后即删）
        self._ffconcat = os.path.join(self.frames_dir, "ffconcat.txt")
        self._frames: List[Tuple[str, float]] = []   # (文件名, 起始秒)
        self._n = 0
        self._start: Optional[float] = None

    @property
    def recording(self) -> bool:
        return self._start is not None

    def start(self) -> None:
        os.makedirs(self.frames_dir, exist_ok=True)
        self._n = 0
        self._frames.clear()
        self._start = time.monotonic()

    def write(self, frame: Frame) -> None:
        """每帧调用；空帧（采集失败）不落盘，时长由下一帧的 t 自然补齐。"""
        if not self.recording or frame.jpeg is None:
            return
        name = f"f{self._n:06d}.jpeg"
        with open(os.path.join(self.frames_dir, name), "wb") as f:
            f.write(frame.jpeg)
        self._frames.append((name, frame.t))
        self._n += 1

    def stop(self) -> Tuple[str, str]:
        """停止录制并后台合成（不阻塞调用方，完成后回调 on_compose_done）。

        返回 (kind, text)：kind ∈ composing / saved_frames / empty，
        text 为可直接展示给用户的消息或产物路径。
        """
        if not self.recording:
            return ("empty", "没有进行中的录制")
        self._start = None
        if not self._frames:
            return ("empty", "录制期间未捕获到任何帧")
        self._write_ffconcat()
        if not resources.find_ffmpeg():
            return ("saved_frames",
                    f"未内置 ffmpeg，帧已保留在 {self.frames_dir}\n"
                    f"安装后执行: {self.build_ffmpeg_cmd()}")
        self.last_thread = threading.Thread(target=self._compose, daemon=True)
        self.last_thread.start()
        return ("composing", self.out_path)

    def _compose(self) -> None:
        code, err = self._run_ffmpeg()
        if code == 0 and not self.keep_frames:
            shutil.rmtree(self.frames_dir, ignore_errors=True)   # 中间帧用完即清，不留垃圾
        if self.on_compose_done:
            self.on_compose_done(code == 0, self.out_path if code == 0 else err)

    def _run_ffmpeg(self) -> Tuple[int, str]:
        proc = subprocess.run(self._ffmpeg_args(), capture_output=True, text=True,
                              creationflags=resources.subprocess_flags())
        if proc.returncode != 0:
            # ffmpeg ≥5.1 用 -fps_mode，旧版只有 -vsync（9.0 起移除）
            proc = subprocess.run(self._ffmpeg_args(legacy_vsync=True), capture_output=True,
                                  text=True, creationflags=resources.subprocess_flags())
        if proc.returncode != 0:
            print(f"[!] ffmpeg 合成失败（保留帧目录 {self.frames_dir}）：\n{proc.stderr[-800:]}")
            return proc.returncode, proc.stderr[-800:]
        return 0, ""

    def _ffmpeg_args(self, legacy_vsync: bool = False) -> List[str]:
        s = self.scale
        vfr = "-vsync" if legacy_vsync else "-fps_mode"
        return [resources.find_ffmpeg(), "-y", "-f", "concat", "-safe", "0",
                "-i", self._ffconcat,
                "-vf", f"scale=trunc(iw*{s}/2)*2:trunc(ih*{s}/2)*2,format=yuv420p",
                "-c:v", "libx264", vfr, "vfr", self.out_path]

    def _write_ffconcat(self) -> None:
        lines = ["ffconcat version 1.0"]
        for i, (name, t) in enumerate(self._frames):
            lines.append(f"file '{name}'")
            if i + 1 < len(self._frames):
                lines.append(f"duration {max(0.05, self._frames[i + 1][1] - t):.3f}")
        # concat demuxer 要求最后一个 file 再声明一次，否则尾帧时长取自前一条 duration
        lines.append(f"file '{self._frames[-1][0]}'")
        with open(self._ffconcat, "w") as f:
            f.write("\n".join(lines) + "\n")

    def build_ffmpeg_cmd(self) -> str:
        """可打印/可手工执行的合成命令（仅在 ffmpeg 缺失时提示用）。"""
        return " ".join(self._ffmpeg_args())
