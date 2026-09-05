"""跨平台资源与目录收口：程序内置资源定位、hdc/ffmpeg 查找链、日志目录。

打包态（PyInstaller）资源位于 sys._MEIPASS 下的 bin/、data/；
开发态回退仓库相对路径与本机默认安装位置。所有平台差异集中在这里。

⚠ hdc 选择必须是「最新」的：手机系统升级会抬高 hdc 最低协议版本，
旧版 hdc 表现为 list 正常但 shell 全拒（E000001 version is too low），
因此候选顺序按 SDK 版本号从新到旧。
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    """内置资源根目录：打包态为解包目录，开发态为仓库根。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _candidate_roots():
    """PyInstaller 在不同平台/形态下资源落点不同，逐一候选：
    - _MEIPASS（Windows/Linux onedir 根、macOS Frameworks）
    - macOS .app: ../Frameworks（binaries=）与 ../Resources（datas=）
    - 可执行文件同目录（onedir 兜底）
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    exe_dir = Path(sys.executable).resolve().parent
    if is_frozen():
        roots += [exe_dir.parent / "Frameworks", exe_dir.parent / "Resources", exe_dir]
    return [r for r in roots if r.is_dir()]


def resource_path(rel: str) -> Path:
    """在候选根中查找内置资源；开发态回退仓库相对路径。"""
    for root in _candidate_roots():
        p = root / rel
        if p.exists():
            return p
    return resource_root() / rel


def _platform_bin(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def _sdk_version_key(path: str):
    """从 .../Sdk/<版本>/toolchains/hdc 提取数字版本号用于新→旧排序。"""
    m = re.search(r"[Ss]dk[/\\]([\d.]+)[/\\]", path)
    return [int(x) for x in m.group(1).split(".")] if m else [0]


def _host_hdc_candidates() -> list:
    """本机 hdc 候选，按新→旧：OpenHarmony Sdk → hmscore → DevEco Studio 内置。

    ⚠ 顺序很关键：手机系统升级会抬高 hdc 最低协议版本，旧版 hdc 表现为
    list 正常但 shell 全拒（E000001 version is too low）。
    """
    home = os.path.expanduser("~")
    hits = []
    for pat in (f"{home}/Library/OpenHarmony/Sdk/*/toolchains/" + _platform_bin("hdc"),
                f"{home}/Library/Huawei/Sdk/hmscore/*/toolchains/" + _platform_bin("hdc")):
        found = glob.glob(pat)
        found.sort(key=_sdk_version_key, reverse=True)
        hits += found
    hits += glob.glob("/Applications/DevEco-Studio.app/Contents/sdk/default/"
                      "openharmony/toolchains/" + _platform_bin("hdc"))
    return hits


def find_hdc() -> str:
    """hdc 查找链：程序内置 → 本机 SDK（新→旧）→ PATH。返回空串表示找不到。"""
    bundled = resource_path("bin" / Path(_platform_bin("hdc")))
    if bundled.is_file():
        return str(bundled)
    for cand in _host_hdc_candidates():
        if os.path.isfile(cand):
            return cand
    return shutil.which("hdc") or ""


def find_ffmpeg() -> str:
    """ffmpeg 查找链：程序内置 → PATH。"""
    bundled = resource_path("bin" / Path(_platform_bin("ffmpeg")))
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg") or ""


def subprocess_flags() -> int:
    """Windows GUI 下调外部命令必须隐藏控制台，否则每次 hdc/ffmpeg 都闪黑框。"""
    return subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def log_file() -> Path:
    if IS_MACOS:
        d = Path.home() / "Library" / "Logs"
    elif IS_WINDOWS:
        d = Path(os.environ.get("APPDATA", Path.home())) / "wscrcpy"
    else:
        d = Path.home() / ".local" / "share" / "wscrcpy"
    d.mkdir(parents=True, exist_ok=True)
    return d / "wscrcpy.log"


def setup_logging() -> str:
    path = log_file()
    handler = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return str(path)


def default_save_dir() -> Path:
    """截图/录制对话框的默认目录：mac 桌面，Windows 图片。"""
    if IS_MACOS:
        d = Path.home() / "Desktop"
    elif IS_WINDOWS:
        d = Path.home() / "Pictures"
    else:
        d = Path.home()
    return d if d.is_dir() else Path.home()


def find_caploop_script() -> str:
    """caploop.sh：打包内置 → 仓库 scripts/。"""
    p = resource_path("data" / "caploop.sh")
    if p.is_file():
        return str(p)
    p = resource_root() / "scripts" / "caploop.sh"
    return str(p)
