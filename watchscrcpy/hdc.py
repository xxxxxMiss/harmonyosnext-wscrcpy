"""hdc 设备驱动：设备管理、竞态安全截屏、息屏唤醒。

截屏采用「删旧 → 触发 → 轮询 → 拉取」四步，规避 snapshot_display 异步写盘
导致拉到上一帧/半截文件的竞态（模式复用自 auto-shot 实测验证的 screenCap 逻辑）。
命令兼容降级：snapshot_display 不存在时回退 uitest screenCap。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

from . import resources

# 离线帧文件统一放这里；snapshot 与 caploop 模式共用
TMP_DIR = "/data/local/tmp/wscrcpy"

# 本机中转临时文件：每帧复用（不堆积），按进程隔离避免多实例互删；
# 进程退出由 GUI 清理，异常退出残留的散文件无害且会被后续同名覆写/系统清理
HOST_TEMP = os.path.join(tempfile.gettempdir(), f"wscrcpy_frame_{os.getpid()}.bin")

# hdc 客户端会往 stdout 混入带时间戳的 W/E/F 日志行（含 ANSI 色码），
# 污染 seq/序号等数值解析，shell 输出统一清洗
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NOISE = re.compile(r"^\[[WFE]\]\[\d{4}-\d{2}-\d{2}")

# 手机系统升级后会抬高 hdc 最低协议版本，旧客户端表现为 list 正常但 shell 全拒
_VERSION_FAIL_MARKS = ("version is too low", "[Fail][E000001]")


class HdcError(RuntimeError):
    pass


class Hdc:
    def __init__(self, hdc_path: Optional[str] = None, serial: Optional[str] = None):
        self.hdc_path = hdc_path or resources.find_hdc()
        if not self.hdc_path:
            raise HdcError("找不到 hdc：程序内置资源缺失，且 PATH 中也没有 hdc。"
                           "Windows 请把 hdc.exe 与程序放在一起，或安装 DevEco Command Line Tools")
        self.serial = serial

    # ---- 基础 ----
    def _run(self, *args: str, timeout: float = 30.0) -> bytes:
        cmd = [self.hdc_path]
        if self.serial:
            cmd += ["-t", self.serial]
        cmd += list(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  creationflags=resources.subprocess_flags())
        except FileNotFoundError:
            raise HdcError(f"找不到 hdc（{self.hdc_path}），请用 --hdc-path 指定") from None
        except subprocess.TimeoutExpired:
            raise HdcError(f"hdc 命令超时: {' '.join(cmd)}") from None
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise HdcError(f"hdc 失败({proc.returncode}): {' '.join(cmd)}\n{err}")
        return proc.stdout

    def shell(self, *args: str, timeout: float = 30.0) -> str:
        raw = self._run("shell", *args, timeout=timeout).decode("utf-8", "replace")
        if any(m in raw for m in _VERSION_FAIL_MARKS):
            raise HdcError(
                "hdc 版本过旧（E000001）：设备要求更新版 hdc，本机 hdc 已无法执行 shell 命令。\n"
                "  修复：升级 DevEco Studio（设置→SDK→勾选最新 Command Line Tools），\n"
                "  或从华为开发者网站下载最新 Command Line Tools，\n"
                "  然后用 --hdc-path 指向新 hdc，或放到本程序自动查找的路径下")
        lines = [l for l in raw.splitlines() if not _NOISE.match(_ANSI.sub("", l))]
        return "\n".join(lines)

    def recv(self, remote: str, local: str, timeout: float = 20.0) -> None:
        self._run("file", "recv", remote, local, timeout=timeout)

    def send(self, local: str, remote: str, timeout: float = 20.0) -> None:
        self._run("file", "send", local, remote, timeout=timeout)

    # ---- 设备 ----
    @staticmethod
    def list_devices(hdc_path: Optional[str] = None) -> List[str]:
        hdc_path = hdc_path or resources.find_hdc()
        if not hdc_path:
            raise HdcError("找不到 hdc，请确认程序资源完整或安装 DevEco Command Line Tools")
        try:
            proc = subprocess.run([hdc_path, "list", "targets"], capture_output=True, timeout=10,
                                  creationflags=resources.subprocess_flags())
        except FileNotFoundError:
            raise HdcError(f"找不到 hdc（{hdc_path}）") from None
        out = proc.stdout.decode("utf-8", "replace")
        serials = []
        for line in out.splitlines():
            s = re.sub(r"\x1b\[[0-9;]*m", "", line).strip().split("\t")[0].strip()
            # 序列号不含空格；排除 [Empty] 与混入 stdout 的 W/E 日志行
            if s and " " not in s and s != "[Empty]":
                serials.append(s)
        return serials

    def device_type(self) -> str:
        # NEXT 用 param get；老版本是 getparam
        for args in (["param", "get", "const.product.devicetype"],
                     ["getparam", "const.product.devicetype"]):
            try:
                out = self.shell(*args, timeout=10).strip()
            except HdcError:
                continue
            # param get 输出可能带 "key = value" 前缀，取最后一个词
            if out and "inaccessible" not in out and "not found" not in out:
                return out.split()[-1]
        return ""

    def wakeup(self) -> bool:
        """尝试唤醒/保持亮屏。任一命令成功即认为可能生效（无法从输出确认）。"""
        for args in (["power-shell", "wakeup"],
                     ["power-shell", "setmode", "602"]):
            try:
                self.shell(*args, timeout=10)
                return True
            except HdcError:
                continue
        return False

    # ---- 截屏（竞态安全） ----
    def grab_frame(self, remote_path: str, attempts: int = 10, poll: float = 0.08) -> bytes:
        """抓一帧返回原始图片字节。

        步骤：确保目录 → 删旧文件 → snapshot_display 触发 → 轮询拉取并校验 JPEG/PNG 魔数。
        snapshot_display 不会自建目录（报 invalid realpath），故先 mkdir -p。
        snapshot_display 不可用时回退 uitest screenCap。
        隐私页禁止截屏会得到黑帧——这里只保证拿到"本次触发"的产物，黑帧交给上层判断。
        """
        local = HOST_TEMP
        parent = os.path.dirname(remote_path)
        if parent:
            self.shell("mkdir", "-p", parent, timeout=10)
        self.shell("rm", "-f", remote_path, timeout=10)
        last_err = "无"
        for trigger in (["snapshot_display", "-f", remote_path],
                        ["uitest", "screenCap", "-p", remote_path]):
            try:
                self.shell(*trigger, timeout=20)
            except HdcError as e:
                last_err = str(e).splitlines()[0]
                continue  # 命令不存在/失败，试下一个触发方式
            for _ in range(attempts):
                time.sleep(poll)
                try:
                    self.recv(remote_path, local, timeout=15)
                    with open(local, "rb") as f:
                        data = f.read()
                    if len(data) > 100 and (data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n"):
                        return data
                    last_err = f"文件无效（{len(data)} 字节）"
                except (HdcError, OSError) as e:
                    last_err = str(e).splitlines()[0]
                # 半截文件/未落盘：继续轮询
        raise HdcError(f"截屏未产出有效文件（最后错误: {last_err}）。"
                       "隐私页禁止截屏时会出现黑图/空图，属预期行为")

    # ---- 屏幕 ----
    def purge_stale_temp(self, max_age: float = 3600.0) -> None:
        """清理本机陈旧的中转临时文件（异常退出会绕过退出清理，按 mtime 兜底）。"""
        try:
            d = os.path.dirname(HOST_TEMP)
            now = time.time()
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if f.startswith("wscrcpy_frame") and now - os.path.getmtime(p) > max_age:
                    os.remove(p)
        except OSError:
            pass

    def display_size(self) -> Tuple[int, int]:
        for svc, arg in (("RenderService", "screen"), ("WindowManagerService", "-a")):
            try:
                out = self.shell("hidumper", "-s", svc, "-a", arg, timeout=15)
            except HdcError:
                continue
            m = re.search(r"(\d{3,4})\s*[xX×]\s*(\d{3,4})", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        raise HdcError("无法获取屏幕分辨率")
