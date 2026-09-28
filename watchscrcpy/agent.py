"""agent.so 推流采集（AgentCapture）：手表端「变化触发 + JPEG 推流」，免逐帧拉取。

背景
----
`snapshot_display` 逐帧拉取在手表上只有 ~0.6 fps（瓶颈是设备端截图 IPC，降分辨率
无效）。本模块改用 uitest 官方的 extension 加载机制：把 Hypium 的 `agent.so` 推进
`/data/local/tmp/agent.so`，`uitest start-daemon singleness` 会以 shell 身份加载它，
再由 `agent.so` 内部走 `Rosen::DisplayManager::GetScreenshotWithOption()` 的监听通道，
**画面变化时才推一帧 JPEG**。免掉 PC 逐帧 shell 往返与文件落盘轮询。

协议（逆向自 hdckit 0.12.1 与 DevEco Testing 客户端，见 research/agent.so协议逆向.md）
--------------------------------------------------------------------------------
    请求帧:  b"_uitestkit_rpc_message_head_" + sessionId(u32be) + len(u32be)
             + JSON(len) + b"_uitestkit_rpc_message_tail_"
    应答/推流: 同一种帧格式回传，payload 为 JSON 或裸 JPEG
               （JSON 尾部带 b"\\n"；推流帧的 sessionId = startCaptureScreen 那笔的）

    ⚠ sessionId 必须 > 0xFFFF。设备端按 sid 大小选择应答分帧方式：
      sid<=0xFFFF  -> 回「裸 JSON+\\n」（DevEco 老客户端走这条遗留路径，
                      因为 `OSBase._recv` 正是读到 \\n 为止）
      sid >0xFFFF  -> 回「HEAD+sid+len+payload+TAIL」（hdckit 用 32 位 hash，走这条）
    用固定小 sid 会落到裸路径，推流帧因此失去长度前缀、只能靠 SOI/EOI 硬切。

    握手:    callHypiumApi / Driver.create        -> {"result":"Driver#0"}
    开流:    Captures / startCaptureScreen
             args={"options":{"scale":0.5}}       -> {"result":true}
             注意 scale 必须 <1.0，=1.0 会被拒（{"result":null,"exception":""}）
    停流:    Captures / stopCaptureScreen
    可用性:  Captures.captureLayout 可返回完整 UI 树；CtrlCmd.getDisplaySize
             返回 {"width":466,"height":466}。⚠ 旧版 so 的 copyScreen 在 v1.2.2 已移除
             （返回 Illegal api name）；captureScreen / screenshot 为未知名，调用会
             打挂连接，不要试。

设备端前置条件：`param set persist.ace.testmode.enabled 1`、agent.so 就位、
`uitest start-daemon singleness`（**不带** --extension-name，默认即 /data/local/tmp/agent.so）、
`hdc fport tcp:N localabstract:uitest_socket`。

已知特性：**严格变化触发**——画面静止时 0 帧（实测即使强制亮屏也无帧）。因此首帧要用
`snapshot_display` 兜底；长静止段由 AgentCapture 的 heartbeat 重发上一帧维持时间轴，
否则录制会得到空/超短视频。
"""
from __future__ import annotations

import glob
import io
import json
import logging
import math
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import resources
from .capture import BaseCapture, FrameCallback, parse_jpeg_size
from .hdc import TMP_DIR, Hdc, HdcError

log = logging.getLogger(__name__)

# ---- 协议常量 ----
HEAD = b"_uitestkit_rpc_message_head_"
TAIL = b"_uitestkit_rpc_message_tail_"
HYPIUM_MODULE = "com.ohos.devicetest.hypiumApiHelper"

REMOTE_SO = "/data/local/tmp/agent.so"
UNIX_SOCKET = "uitest_socket"      # abstract socket: @uitest_socket
API_PORT = 8012                    # 老版本（<6.0.2.2 / x86_64）走 TCP 8012

# scale 必须落在 (0,1)：=1.0 被设备端拒绝
SCALE_MIN, SCALE_MAX = 0.05, 0.99

# 默认取 0.99（=462×462，几乎等同原生 466×466）。不要图省流量回到 0.5：
# 0.5 只有 233×233，像素数只剩 1/4，GUI 再把这张图放大到窗口（Retina 上 ~6 倍），
# 肉眼就是「糊」。真机实测 0.99 单帧 15~25KB、峰值带宽 ~340KB/s（USB 上可忽略），
# 帧率由画面变化率决定，与 scale 无关（详见 PLAN 第 15 节）。

_JPEG_SOI, _JPEG_EOI = b"\xff\xd8", b"\xff\xd9"

# agent.so 版本与 uitest 版本的对应关系（照抄 DevEco Testing _init_so_resource）
_UNIX_SOCKET_MIN_UITEST = (6, 0, 2, 1)     # uitest 大于此版本且非 x86_64 -> unix socket + 1.2.2
_AGENT_110_MIN_UITEST = (5, 1, 1, 3)
_AGENT_15_MIN_UITEST = (5, 1, 1, 2)

# DevEco Testing / Hypium 安装位置里自带的 agent so（版权归华为，本仓库不分发）
_DEVTEST_SO_DIRS = (
    "/Applications/DevEco_Testing_for_App.app/Contents/Python/lib/python3.12/"
    "site-packages/devicetest/res/prototype/native",
    "/Applications/DevEco Testing.app/Contents/Python/lib/python3.12/"
    "site-packages/devicetest/res/prototype/native",
)

# 兜底搜索：只在下面这些**绝对**目录里做有界遍历。
#
# ⚠ 血泪教训（2026-09-24 线上现场）：老实现把
# `**/node_modules/hdckit/uitestkit_sdk/*.so` 这类**相对**模式交给
# `glob.glob(..., recursive=True)`，而相对模式是以**进程 CWD** 为起点展开的 ——
# Finder 双击启动的 .app 其 CWD 是 `/`，于是每次查 agent.so 都退化成**全盘递归遍历**
# （实测 CWD=/ 时 70s 都没跑完），表现成「设备已连接但一直找不到 / 一直 loading」。
# 现在：全部绝对锚定 + 深度/目录数/时长三重预算 + 不跟随符号链接。
_SO_SEARCH_BUDGET = 0.75       # 秒：兜底遍历的总时间预算（超时即放弃，绝不拖住调用方）
_SO_SEARCH_MAX_DEPTH = 10      # 目录层级上限（DevEco 的 native 在 ~第 9 层）
_SO_SEARCH_MAX_DIRS = 600      # 访问目录数上限
# 已知不含 agent.so 的目录直接剪枝（node_modules 特殊处理，见 _bounded_find_so）
_SO_PRUNE = frozenset({
    ".git", "__pycache__", ".Trash", "Caches", "Cache", "DerivedData",
    "_cacache", ".cache", "logs", "Logs", "tmp", "Temp",
})


def _env_roots() -> Tuple[str, ...]:
    """Windows 的 Program Files / AppData **可能不在 C:**（企业镜像、系统盘换盘符）。

    写死 `C:/Program Files/Huawei` 在那种机器上等于没写，所以优先用环境变量定位。
    Windows 上 `os.path.isabs(r"C:\\Program Files")` 为真，UNC 路径亦为真。
    """
    keys = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)",
            "LOCALAPPDATA", "APPDATA", "ProgramData")
    out: List[str] = []
    for k in keys:
        v = (os.environ.get(k) or "").strip()
        if v and os.path.isabs(v) and v not in out:
            out.append(v)
    return tuple(out)


def _so_roots() -> Tuple[str, ...]:
    """兜底遍历的根目录：**只保留存在的绝对路径**。

    空串/相对路径绝不允许进来 —— `os.path.join("", "x")` 会退回相对路径，而相对路径
    在 glob/CWD 语义下正是「全盘遍历」那次事故的根因（见上方注释）。这里多一道
    `os.path.isabs` 断言，避免以后有人顺手往列表里加个相对路径又把老 bug 引回来。
    """
    cands = (
        str(Path.home() / "Applications"),
        str(Path.home() / "Library"),
        str(Path.home() / ".deveco"),
        str(Path.home() / ".npm"),
        "/Applications",
        "/opt",
        "/usr/local/lib",
        *_env_roots(),                 # Windows：Program Files / AppData（可能在任意盘）
        "C:/Program Files/Huawei",     # 兜底：默认盘符
    )
    out: List[str] = []
    for c in cands:
        if c and os.path.isabs(c) and c not in out and os.path.isdir(c):
            out.append(c)
    return tuple(out)


# --------------------------------------------------------------------------- #
# agent.so 的多路径解析
#
# 显式来源（--agent-so / WSCRCPY_AGENT_SO / GUI 设置）**既可以是 so 文件本身，
# 也可以是 DevEco Testing 的安装路径**——后者在其下搜索 so。
# 没有任何显式来源时，按「用户目录下的 DevEco Testing → 程序内置 → 各平台标准安装
# 位置 → 定向 glob → 有界兜底遍历」逐级找。本程序可装在任意目录，所以自动发现
# 只看用户目录/标准位置，不依赖自身所在路径。
# --------------------------------------------------------------------------- #
SETTING_DEVECO_PATH = "deveco_path"        # GUI「设置」写入配置文件的键名
_DEVTEST_NAME_MARK = "deveco"              # 自动发现时按目录名关键字匹配

# DevEco Testing 安装根下 so 的**已知相对布局**。
# ⚠ 全部是单层 `*`：绝不用 `**`——相对/递归 glob 会以 CWD 为起点退化成全盘遍历
# （见 _so_roots() 上方的教训）。不在已知布局里的靠 _bounded_find_so 兜底。
_DEVTEST_SO_RELS = (
    "res/prototype/native/uitest_agent_v*.so",
    "devicetest/res/prototype/native/uitest_agent_v*.so",
    "*/res/prototype/native/uitest_agent_v*.so",
    "Contents/Python/lib/python3.*/site-packages/devicetest/res/prototype/native/uitest_agent_v*.so",
    "*/Contents/Python/lib/python3.*/site-packages/devicetest/res/prototype/native/uitest_agent_v*.so",
    "*/devicetest/res/prototype/native/uitest_agent_v*.so",
)

# 用户目录浅层扫描的预算（只找名字含 deveco 的目录，别把家目录当全盘走）
_HOME_SCAN_MAX_DEPTH = 4
_HOME_SCAN_MAX_DIRS = 300
_HOME_SCAN_BUDGET = 0.5
_HOME_SCAN_LIMIT = 4                       # 最多认几个 DevEco 根，够用就行
_HOME_SCAN_PRUNE = _SO_PRUNE | {"node_modules", ".venv", "venv", "Temp", ".git",
                                "site-packages", "dist-info", "__pycache__"}


def _normalize_spec(spec: str) -> str:
    """规整用户/环境变量给的路径：去空白、去首尾引号、展开 ~ 与环境变量。

    引号要按"去掉首尾所有引号字符"处理，而不是只认严格成对：
    Windows 上从资源管理器/终端拖出来的路径常是 `"C:\Program Files\DevEco Testing\"`，
    末尾的反斜杠在引号**里面**，严格配对判断会漏掉，后面就找不到目录了。
    """
    s = (spec or "").strip()
    while s[:1] in ("'", '"'):
        s = s[1:].strip()
    while s[-1:] in ("'", '"'):
        s = s[:-1].strip()
    if not s:
        return ""
    return os.path.expanduser(os.path.expandvars(s))


def search_deveco_root(root: str, deadline: Optional[float] = None) -> List[str]:
    """在**给定的 DevEco Testing 安装路径**下找 agent.so，返回所有命中文件。

    先按已知相对布局做零遍历 glob；布局不匹配（用户指到了中间某一层）再走有界遍历。
    `deadline` 可由调用方传入，让多个候选根**共用**一份预算（否则每个根各花一份，
    候选一多总耗时就成了预算乘以根数）。
    """
    root = _normalize_spec(root)
    if not root or not os.path.isdir(root):
        return []
    hits: List[str] = []
    seen: set = set()
    for rel in _DEVTEST_SO_RELS:
        for f in sorted(glob.glob(os.path.join(root, rel))):
            if not os.path.isfile(f):
                continue
            real = os.path.realpath(f)
            if real not in seen:
                seen.add(real)
                hits.append(f)
    if hits:
        return hits
    return _bounded_find_so(
        (root,), deadline if deadline is not None else time.monotonic() + _SO_SEARCH_BUDGET)


def resolve_so_spec(spec: str) -> List[str]:
    """解析一条显式指定，返回候选 so 文件列表。

    - 指向 .so **文件** → 直接用（不限定文件名，尊重用户指定）；
    - 指向**目录**（DevEco Testing 安装路径）→ 在其下搜索；
    - 路径不存在/啥也没找到 → 空列表（调用方据此回退到其它来源，而不是直接失败）。
    """
    spec = _normalize_spec(spec)
    if not spec:
        return []
    if os.path.isfile(spec):
        return [spec]
    if os.path.isdir(spec):
        return search_deveco_root(spec)
    return []


def describe_deveco_path(raw: str) -> Tuple[bool, str]:
    """GUI「设置」用：解析用户输入的路径并给出人话结论 (是否可用, 说明)。"""
    spec = _normalize_spec(raw)
    if not spec:
        return False, "路径为空"
    if not os.path.exists(spec):
        return False, f"路径不存在：{spec}"
    hits = resolve_so_spec(spec)
    if not hits:
        return False, "该路径下没找到 uitest_agent_v*.so"
    vers = sorted({_so_version_of(h) for h in hits}, key=_uitest_tuple, reverse=True)
    return True, "找到 agent.so v" + "、v".join(vers)


def _find_named_dirs(roots: Sequence[str], mark: str,
                     limit: int = _HOME_SCAN_LIMIT) -> List[str]:
    """在 roots 下**浅层有界**地找名字含 mark 的目录（深度/目录数/时长三重预算）。

    预算既在层与层之间判、也在单个目录内部判：撞上超大目录（一次 scandir 几万条目）
    也要能就地喊停，否则预算会被单层吃掉（_bounded_find_so 踩过同样的坑）。
    """
    import collections
    out: List[str] = []
    deadline = time.monotonic() + _HOME_SCAN_BUDGET
    visited = 0
    queue = collections.deque((r, 0) for r in roots if os.path.isdir(r))
    while queue:
        if time.monotonic() > deadline or visited >= _HOME_SCAN_MAX_DIRS \
                or len(out) >= limit:
            break
        d, depth = queue.popleft()
        visited += 1
        n = 0
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    if not n % 128 and time.monotonic() > deadline:
                        break
                    try:
                        if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if mark in e.name.lower():
                        out.append(e.path)
                        if len(out) >= limit:
                            break
                        continue                    # 命中即 DevEco 根，不再往里深挖
                    if depth >= _HOME_SCAN_MAX_DEPTH or e.name in _HOME_SCAN_PRUNE:
                        continue
                    queue.append((e.path, depth + 1))
        except OSError:
            continue
    if out:
        log.info("用户目录下发现疑似 DevEco 目录：%s", "; ".join(out))
    return out


def _home_deveco_roots() -> List[str]:
    """用户目录下的 DevEco Testing 候选根。

    本程序可能被安装在任意目录（甚至绿色版），所以自动发现**只看用户目录**，不看自身位置。
    常见摆放先零遍历命中；没有再浅层扫一遍找名字含 deveco 的目录。
    """
    home = Path.home()
    quick = [
        home / "DevEco Testing.app", home / "DevEco_Testing_for_App.app",
        home / "Applications" / "DevEco Testing.app",
        home / "Applications" / "DevEco_Testing_for_App.app",
        home / "Applications" / "DevEco Testing",
        home / "DevEco Testing", home / "deveco-testing",
    ]
    for base in (os.environ.get("LOCALAPPDATA", "").strip(),
                 os.environ.get("APPDATA", "").strip()):
        if base:
            quick += [Path(base) / "Huawei", Path(base) / "Programs"]
    out = [str(p) for p in quick if p.is_dir()]
    out += _find_named_dirs([str(home)], _DEVTEST_NAME_MARK)
    seen: set = set()
    uniq: List[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _standard_deveco_roots() -> List[str]:
    """各平台 DevEco Testing 的常见安装根（存在才算；Windows 走环境变量，可能在任意盘）。"""
    cands: List[str] = list(_DEVTEST_SO_DIRS)          # macOS 两个 .app 的 native 精确落点
    cands += [
        "/Applications/DevEco Testing.app",
        "/Applications/DevEco_Testing_for_App.app",
    ]
    for base in _env_roots():
        cands += [os.path.join(base, "Huawei"), os.path.join(base, "DevEco Testing")]
    return [c for c in cands if c and os.path.isdir(c)]



class AgentError(RuntimeError):
    """agent.so 通道不可用（so 缺失、daemon 起不来、协议握手失败等）。"""


# --------------------------------------------------------------------------- #
# so 定位
# --------------------------------------------------------------------------- #
def _uitest_tuple(version: str) -> Tuple[int, ...]:
    """'7.0.0.1' -> (7,0,0,1)；解析失败返回 (0,)。"""
    digits = []
    for part in str(version or "").strip().split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        if not num:
            break
        digits.append(int(num))
    return tuple(digits) or (0,)


def agent_so_version(uitest_version: str, arch: str = "") -> str:
    """按 uitest 版本与 CPU 架构决定该用哪个 agent.so 版本。"""
    if "x86_64" in (arch or ""):
        return "1.1.9"
    if _uitest_tuple(uitest_version) > _UNIX_SOCKET_MIN_UITEST:
        return "1.2.2"
    if _uitest_tuple(uitest_version) >= _AGENT_110_MIN_UITEST:
        return "1.1.10"
    if _uitest_tuple(uitest_version) >= _AGENT_15_MIN_UITEST:
        return "1.1.5"
    return "1.1.3"


def _collect_so_in_dir(path: str, out: List[str], seen: set) -> None:
    """收集一个目录里的 agent.so（不递归）。"""
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        if not name.endswith(".so"):
            continue
        # 只认看起来是 agent 的 so（含 uitest_agent 或位于 uitestkit_sdk）
        if "uitest_agent" not in name and "uitestkit_sdk" not in path:
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        real = os.path.realpath(full)
        if real in seen:
            continue
        seen.add(real)
        out.append(full)


def _fallback_patterns() -> List[str]:
    """非默认安装路径的**定向**模式：全绝对路径，且每级都用 `*` 而非 `**`。

    `*` 只匹配一层，所以 glob 的代价是可预期的；`**` 递归才是全盘遍历的来源。
    """
    home = Path.home()
    roots = [str(home / "Applications"), "/Applications", "/opt", "/usr/local", "C:/Program Files"]
    pats: List[str] = []
    for r in roots:
        for app in ("DevEco Testing.app", "DevEco_Testing_for_App.app", "*"):
            pats.append(f"{r}/{app}/Contents/Python/lib/python3.12/site-packages/"
                        "devicetest/res/prototype/native/uitest_agent_v*.so")
    # hdckit（社区实现）随 npm 包分发
    pats += [
        str(home / ".npm-global/lib/node_modules/hdckit/uitestkit_sdk/*.so"),
        str(home / "node_modules/hdckit/uitestkit_sdk/*.so"),
        str(home / "*" / "lib/node_modules/hdckit/uitestkit_sdk/*.so"),
        "/usr/local/lib/node_modules/hdckit/uitestkit_sdk/*.so",
        "/opt/homebrew/lib/node_modules/hdckit/uitestkit_sdk/*.so",
        "C:/Program Files/Huawei/*/uitestkit_sdk/*.so",
    ]
    return pats


def _bounded_find_so(roots: Sequence[str], deadline: float) -> List[str]:
    """在 roots 下**有界**地兜底找 agent.so（深度/目录数/时长三重预算）。

    为什么自己走目录而不是 `glob('**')`：见 `_so_roots()` 上方的教训。
    只认两个已知形状：`.../prototype/native/uitest_agent_v*.so` 与
    `.../node_modules/hdckit/uitestkit_sdk/*.so`；不跟随符号链接，BFS 逐层推进。

    预算要**在层与层之间、也要在单个目录内部**检查：曾经只在每层开头判一次，
    结果撞上一个超大目录（一次 scandir 要走完几万条目）就超了 4~15 倍预算。
    """
    import collections                      # 局部导入，避免为一个兜底路径加全局依赖
    out: List[str] = []
    seen: set = set()
    queue = collections.deque((r, 0) for r in roots if os.path.isdir(r))
    visited = 0
    stop = False
    while queue and not stop:
        if time.monotonic() > deadline or visited >= _SO_SEARCH_MAX_DIRS:
            log.debug("agent.so 兜底搜索达到预算（已访问 %d 个目录），停止", visited)
            break
        d, depth = queue.popleft()
        visited += 1
        base = os.path.basename(d)
        if base in ("native", "uitestkit_sdk"):        # 已知落点：就在这一层找
            _collect_so_in_dir(d, out, seen)
            continue
        if base in _SO_PRUNE:
            continue
        n = 0
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    if not n % 128 and time.monotonic() > deadline:
                        stop = True                    # 单个目录过大也要就地喊停
                        break
                    try:
                        if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if e.name in ("native", "uitestkit_sdk"):
                        _collect_so_in_dir(e.path, out, seen)
                    elif e.name == "node_modules":
                        # hdckit 只可能是 node_modules 的直接子目录：
                        # 别把整个 node_modules 走一遍
                        try:
                            with os.scandir(e.path) as kids:
                                for k in kids:
                                    if k.name.lower().startswith("hdckit"):
                                        _collect_so_in_dir(
                                            os.path.join(k.path, "uitestkit_sdk"), out, seen)
                        except OSError:
                            pass
                    elif depth < _SO_SEARCH_MAX_DEPTH:
                        queue.append((e.path, depth + 1))
        except OSError:
            continue
    return out


# 查找结果缓存：(env 键, 结果, 记录时刻)。已知落点命中就长期缓存；
# 空结果缓存较久，避免「本机压根没装」的用户每次扫描都重走一遍兜底搜索。
_SO_CACHE: Optional[Tuple[Tuple[str, ...], List[str], float]] = None
_SO_EMPTY_TTL = 300.0


def _explicit_sources(spec: Optional[str]) -> List[Tuple[str, str]]:
    """显式指定的 so 来源，按优先级：(来源说明, 原样路径)。

    三者**语义相同**：都既可以指向 so 文件本身，也可以指向 DevEco Testing 安装路径
    （后者在其下搜索 so）。
    """
    srcs: List[Tuple[str, str]] = []
    if spec and spec.strip():
        srcs.append(("--agent-so", spec))
    env = os.environ.get("WSCRCPY_AGENT_SO", "").strip()
    if env:
        srcs.append(("环境变量 WSCRCPY_AGENT_SO", env))
    cfg = resources.get_setting(SETTING_DEVECO_PATH).strip()
    if cfg:
        srcs.append(("设置里的 DevEco Testing 路径", cfg))
    return srcs


def _collect_from_roots(roots: Sequence[str], label: str,
                       budget: float = _SO_SEARCH_BUDGET) -> List[str]:
    """在一批 DevEco 根目录下搜 so，去重后返回；所有根共用一份时间预算。"""
    out: List[str] = []
    seen: set = set()
    deadline = time.monotonic() + budget
    for root in roots:
        if time.monotonic() > deadline:
            log.debug("agent.so 候选根搜索超预算，提前结束（%s）", label)
            break
        for f in search_deveco_root(root, deadline):
            real = os.path.realpath(f)
            if real not in seen:
                seen.add(real)
                out.append(f)
    if out:
        log.info("agent.so 来自%s：%s", label,
                 "; ".join(sorted({os.path.dirname(f) for f in out})[:3]))
    return out


def _bundled_so_files() -> List[str]:
    """程序内置的 agent.so 落点（打包态 Frameworks/_MEIPASS、开发态仓库）。"""
    out: List[str] = []
    seen: set = set()
    for rel in (os.path.join("vendor", "so"), os.path.join("vendor", "agent"), "so"):
        _collect_so_in_dir(str(resources.resource_path(rel)), out, seen)
    return out


def _scan_so_files(spec: Optional[str]) -> List[str]:
    """按优先级逐级找 so，**前一级有结果就不看后面的**。

    1. `--agent-so`（文件或 DevEco 路径）
    2. `WSCRCPY_AGENT_SO`
    3. GUI「设置」里配置的 DevEco Testing 路径
    4. 用户目录下的 DevEco Testing（自动发现，不依赖本程序装在哪儿）
    5. 程序内置 `vendor/so`
    6. 各平台标准安装位置
    7. 定向 glob（绝对路径、单层 `*`）
    8. 有界兜底遍历（预算内）
    """
    # 1~3 显式来源：谁先解析出 so 就用谁；都解析不出来就继续（不让写错的路径把功能卡死）
    for label, raw in _explicit_sources(spec):
        hits = resolve_so_spec(raw)
        if hits:
            log.info("agent.so 由%s指定：%s → %d 个候选", label, _normalize_spec(raw), len(hits))
            return hits
        log.warning("%s 未解析出 agent.so：%r（继续尝试其它来源）", label, raw)

    # 4 用户目录下的 DevEco Testing
    hits = _collect_from_roots(_home_deveco_roots(), "用户目录下的 DevEco Testing")
    if hits:
        return hits

    # 5 程序内置
    hits = _bundled_so_files()
    if hits:
        return hits

    # 6 标准安装位置
    hits = _collect_from_roots(_standard_deveco_roots(), "DevEco Testing 标准安装位置")
    if hits:
        return hits

    # 7 定向 glob（全绝对路径，每级只用一个 `*`，代价可预期）
    out: List[str] = []
    seen: set = set()
    for pat in _fallback_patterns():
        for hit in sorted(glob.glob(pat)):
            real = os.path.realpath(hit)
            if real not in seen and os.path.isfile(hit):
                seen.add(real)
                out.append(hit)
    if out:
        return out

    # 8 有界遍历（带预算）
    log.info("已知位置未找到 agent.so，开始有界兜底搜索（预算 %.1fs）", _SO_SEARCH_BUDGET)
    return _bounded_find_so(_so_roots(), time.monotonic() + _SO_SEARCH_BUDGET)


def _find_so_files(spec: Optional[str] = None) -> List[str]:
    """收集候选 agent.so（带缓存）。

    缓存键包含三条显式来源，所以：改环境变量、改设置、换 --agent-so 都会自动失效重扫——
    绝不能每次连接都重走目录（那曾是「设备已连接却一直 loading」的根因）。
    """
    global _SO_CACHE
    env = os.environ.get("WSCRCPY_AGENT_SO", "").strip()
    cfg = resources.get_setting(SETTING_DEVECO_PATH).strip()
    key = (spec or "", env, cfg)
    now = time.monotonic()
    if _SO_CACHE and _SO_CACHE[0] == key:
        files, t = _SO_CACHE[1], _SO_CACHE[2]
        if files or now - t < _SO_EMPTY_TTL:
            return list(files)

    files = _scan_so_files(spec)
    _SO_CACHE = (key, list(files), now)
    return list(files)


def clear_so_cache() -> None:
    """丢掉缓存并重扫（GUI 保存设置后调用，让新路径立刻生效）。"""
    global _SO_CACHE
    _SO_CACHE = None


def _so_version_of(path: str) -> str:
    """从文件名 uitest_agent_v1.2.2.so / uitest_agent_v1.1.9.x86_64_so 抽版本号。"""
    name = os.path.basename(path)
    tail = name.split("uitest_agent_v", 1)[-1]
    m = ""
    for ch in tail:
        if ch.isdigit() or ch == ".":
            m += ch
        else:
            break
    return m.rstrip(".") or "0"


def _pick_best(files: Sequence[str], uitest_version: str, arch: str) -> Optional[str]:
    """在一批候选中挑与设备最匹配的 so：精确版本 → 不高过目标的最佳版本 → 任意。"""
    if not files:
        return None
    # 设备版本没读到（hdc 抽风 / 设备没就绪）：不能用 agent_so_version("") 的返回值 ——
    # 那是「uitest 5.x 之前」的老协议默认值 1.1.3，在新设备上等于主动选错 so。
    # 这种情况按最新可用的同架构 so 兜底，并把「读不到版本」这件事写进日志。
    unknown = len(_uitest_tuple(uitest_version)) < 3
    want = agent_so_version(uitest_version, arch)
    is_x86 = "x86_64" in (arch or "")

    def arch_ok(p: str) -> bool:
        x86 = "x86_64" in os.path.basename(p)
        return x86 if is_x86 else not x86

    def ver_key(p: str):
        return _uitest_tuple(_so_version_of(p))

    if not unknown:
        exact = [p for p in files if _so_version_of(p) == want and arch_ok(p)]
        if exact:
            return sorted(exact, key=ver_key, reverse=True)[0]
    same_arch = [p for p in files if arch_ok(p)]
    if same_arch:
        if unknown:
            pick = sorted(same_arch, key=ver_key, reverse=True)[0]
            log.warning("未读到设备 uitest 版本（得到 %r），按最新可用 so 兜底: v%s",
                        uitest_version, _so_version_of(pick))
            return pick
        # 取不高于目标的最佳版本，其次任意
        below = [p for p in same_arch if ver_key(p) <= _uitest_tuple(want)]
        pick = sorted(below or same_arch, key=ver_key, reverse=True)[0]
        log.info("未找到 agent.so v%s，回退 v%s", want, _so_version_of(pick))
        return pick
    return sorted(files, key=ver_key, reverse=True)[0]


def find_agent_so(uitest_version: str = "", arch: str = "",
                  spec: Optional[str] = None) -> Optional[str]:
    """找与设备匹配的 agent.so。

    `spec`（来自 CLI `--agent-so`）优先级最高，且**既可以是 so 文件也可以是 DevEco
    Testing 安装路径**；不传则按环境变量 → 设置 → 用户目录/标准位置 → 内置 → 兜底 的顺序。
    再在其中按版本（uitest）与架构挑最匹配的一个。
    返回 None 表示本机没有可用的 agent.so（需安装 DevEco Testing 或用 --agent-so 指定）。
    """
    return _pick_best(_find_so_files(spec), uitest_version, arch)


# --------------------------------------------------------------------------- #
# 协议客户端
# --------------------------------------------------------------------------- #
def _request_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "%06d" % (time.time_ns() // 1000 % 1000000)


class AgentClient:
    """agent.so 的 RPC 客户端：握手 + 开流 + 帧流迭代。

    仅负责协议；进程/转发生命周期由 AgentCapture 管理。
    """

    def __init__(self, port: int, scale: float = 0.99, timeout: float = 25.0):
        self.port = port
        self.scale = min(max(float(scale), SCALE_MIN), SCALE_MAX)
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.driver: Optional[str] = None
        # ⚠ sessionId 必须 > 0xFFFF：设备端按此决定应答分帧方式。
        #   实测 sid<=0xFFFF 时回「裸 JSON+\\n」（遗留兼容路径，DevEco 老客户端即如此），
        #   sid>0xFFFF 时回「HEAD+sid+len+payload+TAIL」，推流帧同样带 startCaptureScreen
        #   那一笔的 sid。用 32 位随机值即可稳定走分帧路径（hdckit 也是大 sid）。
        self._sid = int.from_bytes(os.urandom(4), "big") | 0x80000000
        self._buf = b""
        self._capturing = False
        self.capture_sid: Optional[int] = None
        self.last_sid: Optional[int] = None

    # ---- 传输 ----
    def connect(self) -> None:
        try:
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        except OSError as e:
            raise AgentError(f"连接 agent.so 端口 {self.port} 失败: {e}") from None
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _next_sid(self) -> int:
        """始终保持高位为 1，确保走设备端的分帧应答路径（见 __init__ 注释）。"""
        self._sid = ((self._sid + 1) & 0x7FFFFFFF) | 0x80000000
        return self._sid

    def send(self, method: str, api: str, args=None, hypium: bool = False) -> int:
        assert self.sock is not None
        if hypium:
            params = {"api": f"Driver.{api}", "this": self.driver,
                      "args": [] if args is None else args, "message_type": "hypium"}
        else:
            params = {"api": api, "args": {} if args is None else args}
        msg = {"module": HYPIUM_MODULE, "method": method, "params": params,
               "request_id": _request_id()}
        body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode()
        sid = self._next_sid()
        self.last_sid = sid
        self.sock.sendall(HEAD + struct.pack(">II", sid, len(body)) + body + TAIL)
        return sid

    # ---- 分帧 ----
    # 正常路径（大 sessionId）: HEAD + sid + len + payload + TAIL，payload 为
    # JSON 或裸 JPEG。另保留「裸 JSON+\\n」与「裸 JPEG」两种遗留形态的兼容分支。
    def _pop_event(self) -> Optional[Tuple[str, Optional[int], bytes]]:
        """切出下一个事件 ('json'|'jpeg', sid, payload)；None 表示数据不足。"""
        buf = self._buf
        if not buf:
            return None
        if buf[:1] == HEAD[:1] and len(buf) < len(HEAD):
            if HEAD.startswith(buf):        # 可能是 HEAD 的前缀，等更多数据
                return None
        if buf.startswith(HEAD):
            if len(buf) < len(HEAD) + 8:
                return None
            sid, ln = struct.unpack(">II", buf[len(HEAD):len(HEAD) + 8])
            start, end = len(HEAD) + 8, len(HEAD) + 8 + ln
            if len(buf) < end + len(TAIL):
                return None
            if buf[end:end + len(TAIL)] != TAIL:
                # 帧尾错位：丢弃 HEAD 重新同步，避免死循环
                log.debug("agent 帧尾错位，重新同步")
                self._buf = buf[len(HEAD):]
                return None
            payload = buf[start:end]
            self._buf = buf[end + len(TAIL):]
            return ("jpeg" if payload[:2] == _JPEG_SOI else "json", sid, payload)
        # --- 兼容：裸 JPEG ---
        if buf[:2] == _JPEG_SOI:
            end = buf.find(_JPEG_EOI)
            if end < 0:
                return None
            self._buf = buf[end + 2:]
            return ("jpeg", None, buf[:end + 2])
        # --- 兼容：裸 JSON 行（sid<=0xFFFF 的遗留应答）---
        nl = buf.find(b"\n")
        if nl < 0:
            if len(buf) > 64:               # 无法识别的噪声，别让缓冲无界增长
                log.debug("agent 流出现无法识别数据，丢弃 %d 字节", len(buf))
                self._buf = b""
            return None
        line = buf[:nl]
        self._buf = buf[nl + 1:]
        return ("json", None, line)

    def _recv_into_buf(self, timeout: float) -> bool:
        """收一段数据进缓冲。返回 False 表示对端关闭/出错。"""
        assert self.sock is not None
        self.sock.settimeout(max(0.05, timeout))
        try:
            data = self.sock.recv(1 << 20)
        except socket.timeout:
            return True                      # 空闲是正常状态（无画面变化）
        except OSError as e:
            log.debug("agent socket 读失败: %s", e)
            return False
        if not data:
            return False
        self._buf += data
        return True

    def read_json(self, timeout: float) -> Optional[bytes]:
        """读一条 JSON 应答（跳过 JPEG）。超时返回 None。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ev = self._pop_event()
            if ev is None:
                if not self._recv_into_buf(deadline - time.monotonic()):
                    raise AgentError("agent.so 连接已断开")
                continue
            kind, _sid, payload = ev
            if kind == "json" and payload.strip():
                return payload.strip()
        return None

    def call(self, method: str, api: str, args=None, hypium: bool = False,
             wait: float = 10.0) -> dict:
        """发一条请求并读回应答（JSON 解析）。"""
        self.send(method, api, args, hypium)
        line = self.read_json(wait)
        if line is None:
            raise AgentError(f"{method}.{api} 无应答（{wait}s 超时）")
        try:
            return json.loads(line)
        except ValueError:
            raise AgentError(f"{method}.{api} 应答不是 JSON: {line[:120]!r}") from None

    # ---- 会话 ----
    def handshake(self) -> str:
        """Driver.create —— 建立 Hypium driver 会话，返回 driver 名。"""
        # 首次调用要等 AccessibilityUITestAbility 起来，实测可到 ~5s，给足余量
        rep = self.call("callHypiumApi", "create", args=[], hypium=True,
                        wait=max(self.timeout, 25.0))
        drv = rep.get("result")
        if not isinstance(drv, str) or not drv:
            raise AgentError(f"Driver.create 失败: {rep}")
        self.driver = drv
        return drv

    def display_size(self) -> Optional[Tuple[int, int]]:
        """设备逻辑分辨率（CtrlCmd.getDisplaySize）；失败返回 None。"""
        rep = self.call("CtrlCmd", "getDisplaySize", args={}, wait=8.0)
        r = rep.get("result") or {}
        w, h = r.get("width"), r.get("height")
        if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
            return (w, h)
        return None

    def start_capture(self) -> None:
        rep = self.call("Captures", "startCaptureScreen",
                        args={"options": {"scale": self.scale}}, wait=12.0)
        if rep.get("exception"):
            raise AgentError(f"startCaptureScreen 失败: {rep}")
        if not rep.get("result"):
            raise AgentError(f"startCaptureScreen 未生效: {rep}")
        # 推流帧沿用这一笔请求的 sessionId（设备端如此回传），据此过滤
        self.capture_sid = self.last_sid
        self._capturing = True

    def stop_capture(self) -> None:
        if not self._capturing:
            return
        try:
            self.call("Captures", "stopCaptureScreen", args={}, wait=5.0)
        except Exception:
            log.debug("stopCaptureScreen 失败（忽略）", exc_info=True)
        self._capturing = False
        self.capture_sid = None

    def next_frame(self, idle_timeout: float = 0.5) -> Optional[bytes]:
        """取下一帧 JPEG；空闲（无画面变化）返回 None；连接断开抛 AgentError。"""
        deadline = time.monotonic() + idle_timeout
        while True:
            ev = self._pop_event()
            if ev is None:
                if time.monotonic() >= deadline:
                    return None
                if not self._recv_into_buf(deadline - time.monotonic()):
                    raise AgentError("agent.so 连接已断开")
                continue
            kind, sid, payload = ev
            if kind != "jpeg":
                continue
            # 只认本会话的帧：连接上可能混有其他会话的推流
            if sid is not None and self.capture_sid is not None and sid != self.capture_sid:
                continue
            return payload


# --------------------------------------------------------------------------- #
# 采集线程
# --------------------------------------------------------------------------- #
@dataclass
class AgentProbe:
    """设备能力探测结果，供 UI 提示。"""
    uitest_version: str = ""
    arch: str = ""
    so_path: Optional[str] = None
    so_version: str = ""
    driver: str = ""


class AgentCapture(BaseCapture):
    """模式C：agent.so 变化触发推流。

    与 PulledCapture/LoopCapture 同一 Frame 出口；`interval` 仅用于空闲轮询节流，
    真正帧率由设备端画面变化决定。
    """

    def __init__(self, hdc: Hdc, interval: float, on_frame: FrameCallback,
                 scale: float = 0.99, so_path: Optional[str] = None,
                 local_port: int = 29400, heartbeat: float = 1.0, **kw):
        super().__init__(hdc, interval, on_frame, **kw)
        self.scale = min(max(float(scale), SCALE_MIN), SCALE_MAX)
        self.so_path = so_path
        self.local_port = local_port
        # >0：静止画面每 heartbeat 秒重发上一帧（保证录制时间轴连续）；<=0 关闭
        self.heartbeat = float(heartbeat)
        self._probe = AgentProbe()
        self._fport_on = False
        self._client: Optional[AgentClient] = None
        self._started_daemon = False
        self._display_size: Optional[Tuple[int, int]] = None
        # 推流帧尺寸：设备端按 **ceil(分辨率*scale)** 算（真机实测 0.99→462、0.8→373、
        # 0.6→280、0.5→233；round 在 0.99 上会算成 461，与设备不一致，见 PLAN 15.6）
        self._stream_size: Optional[Tuple[int, int]] = None

    # ---- 设备侧准备 ----
    def _device_uitest_version(self) -> str:
        try:
            out = self.hdc.shell("uitest", "--version", timeout=15).strip()
        except HdcError:
            return ""
        for token in out.replace(":", " ").split():
            if token[:1].isdigit() and token.count(".") >= 2:
                return token
        return out.strip()

    def _device_arch(self) -> str:
        for key in ("const.product.cpu.abilist", "const.product.cpu.abi"):
            try:
                out = self.hdc.shell("param", "get", key, timeout=10).strip()
            except HdcError:
                continue
            if out and "not found" not in out:
                return out
        return ""

    def probe_device(self) -> AgentProbe:
        """探测 uitest 版本/架构并选出 agent.so。"""
        p = AgentProbe()
        p.uitest_version = self._device_uitest_version()
        p.arch = self._device_arch()
        # so_path 是「显式指定」，可能是 so 文件也可能是 DevEco Testing 路径，交给
        # find_agent_so 统一解析（文件直接用；目录在其下搜）
        p.so_path = find_agent_so(p.uitest_version, p.arch, self.so_path)
        if p.so_path:
            p.so_version = _so_version_of(p.so_path)
        self._probe = p
        return p

    def _ensure_daemon(self) -> None:
        """推 so + 开 testmode + 起 daemon（singleness 默认加载 /data/local/tmp/agent.so）。

        daemon 启动偶发失败（残留 daemon 抢占 socket、so 刚推完尚未就绪等），
        整段序列重试若干次；失败时把 start-daemon 的输出带进异常便于定位。
        """
        try:
            self.hdc.shell("param", "set", "persist.ace.testmode.enabled", "1", timeout=10)
        except HdcError:
            log.debug("设置 testmode 失败（忽略）", exc_info=True)
        try:
            self.hdc.shell("rm", "-f", REMOTE_SO, timeout=10)
        except HdcError:
            pass
        self.hdc.send(self._probe.so_path, REMOTE_SO)
        try:
            self.hdc.shell("chmod", "755", REMOTE_SO, timeout=10)
        except HdcError:
            pass

        out, pid = "", ""
        for attempt in range(3):
            try:
                self.hdc.shell("pkill -9 -f 'uitest.*start-daemon' 2>/dev/null", timeout=10)
            except HdcError:
                pass
            time.sleep(0.8 + 0.4 * attempt)
            try:
                out = self.hdc.shell("uitest", "start-daemon", "singleness", timeout=25)
            except HdcError as e:
                out = f"start-daemon 失败: {e}"
            self._started_daemon = True
            # 等 abstract socket 就位（扩展 OnInit 之后才创建）
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    if UNIX_SOCKET in self.hdc.shell("cat", "/proc/net/unix", timeout=10):
                        return
                except HdcError:
                    pass
                time.sleep(0.4)
            try:
                pid = self.hdc.shell("pidof", "uitest", timeout=10).strip()
            except HdcError:
                pid = "?"
            log.warning("daemon 第 %d 次未创建 socket: pid=%s out=%s",
                        attempt + 1, pid or "(空)", out.strip()[:120])
        hint = "（进程未起来）" if not pid else "（进程在但未建 socket，so 可能未加载）"
        raise AgentError(f"agent.so 未创建 uitest_socket{hint}；start-daemon 输出: "
                         f"{out.strip().splitlines()[-1][:120] if out.strip() else '空'}")

    def _fport_listed(self, want: str) -> bool:
        """hdc 上是否已存在 want -> localabstract:@uitest_socket 的转发。"""
        try:
            out = self.hdc._run("fport", "ls", timeout=10).decode("utf-8", "replace")
        except HdcError:
            return False
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == want and parts[2].endswith(UNIX_SOCKET):
                return True
        return False

    def _open_fport(self) -> None:
        """转发 tcp:<本地端口> -> localabstract:@uitest_socket。

        ⚠ 本套 hdc/手表上 `fport rm` 恒失败（[Fail]...ruler is not exist，rc=0），
        残留转发只能靠 `hdc kill && hdc start` 清。但它指向的是 @uitest_socket ——
        下次 daemon 重建同名 socket 后这条转发依然可用，所以**已存在就直接复用**，
        既省掉必然失败的 add（本机会报 TCP Port listen failed），也不再累积新残留。
        复用到的若是失效残留，run() 会换端口重试一次。
        """
        want = f"tcp:{self.local_port}"
        if self._fport_listed(want):
            log.debug("复用 hdc 已有转发 %s -> localabstract:%s", want, UNIX_SOCKET)
            self._fport_on = True
            return
        last = ""
        for _ in range(5):
            out = self.hdc._run("fport", want, f"localabstract:{UNIX_SOCKET}",
                                timeout=20).decode("utf-8", "replace")
            last = out.strip()
            if "OK" in out:
                self._fport_on = True
                return
            log.warning("fport %s 失败(%s)，换端口重试", want, last[:60])
            self.local_port = random.randint(29401, 29600)
            want = f"tcp:{self.local_port}"
        raise AgentError(f"fport 转发到 {UNIX_SOCKET} 失败: {last[:80]}")

    def _teardown(self) -> None:
        # fport rm 在本套 hdc/手表上恒失败（见 _open_fport 注释），失败仅记 debug：
        # 残留转发指向 @uitest_socket，无害且下次启动会被复用。
        try:
            if self._fport_on:
                out = self.hdc._run("fport", "rm", f"tcp:{self.local_port}",
                                    timeout=10).decode("utf-8", "replace")
                if "OK" not in out:
                    log.debug("fport rm 未成功（已知设备侧限制）: %s", out.strip()[:60])
                self._fport_on = False
        except HdcError as e:
            log.debug("fport rm 异常（忽略）: %s", e)
        try:
            self.hdc.shell("pkill -9 -f 'uitest.*start-daemon' 2>/dev/null", timeout=8)
        except HdcError:
            pass
        try:
            self.hdc.shell("rm", "-f", REMOTE_SO, timeout=8)
        except HdcError:
            pass

    # ---- 线程主体 ----
    def run(self) -> None:
        t0 = time.monotonic()
        try:
            probe = self.probe_device()
            if not probe.so_path:
                raise AgentError(
                    "未找到 uitest agent.so。请安装 DevEco Testing（自带该 so），"
                    "或在界面「⚙ 设置」里填它的安装路径；"
                    "也可用 --agent-so / 环境变量 WSCRCPY_AGENT_SO 指定 so 文件或 DevEco 目录")
            log.info("agent.so: %s (v%s) uitest=%s arch=%s",
                     probe.so_path, probe.so_version, probe.uitest_version, probe.arch)
            self.on_status(f"agent 模式：启动中（agent.so v{probe.so_version}）")
        except Exception as e:
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            self.on_status(f"agent 模式启动失败：{msg}")
            log.exception("agent 模式启动失败")
            self._cleanup()
            self.on_lost()
            return

        # 建链。复用到的残留转发可能是失效的（见 _open_fport），故失败后换端口重试一次。
        for attempt in range(2):
            try:
                self._ensure_daemon()
                self._open_fport()
                client = AgentClient(self.local_port, self.scale)
                self._client = client
                client.connect()
                probe.driver = client.handshake()
                try:
                    size = client.display_size()
                    if size:
                        self._display_size = size
                        self._stream_size = (math.ceil(size[0] * self.scale),
                                             math.ceil(size[1] * self.scale))
                        # 支持排障用：清晰度问题第一件事就是看这行（源分辨率是多少）
                        log.info("agent 推流尺寸 %dx%d（设备显示 %dx%d，scale=%.2f）",
                                 self._stream_size[0], self._stream_size[1],
                                 size[0], size[1], self.scale)
                except Exception:
                    log.debug("getDisplaySize 失败，首帧尺寸兜底将退化为丢弃", exc_info=True)
                client.start_capture()
                break
            except Exception as e:
                self._cleanup()
                if attempt:
                    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                    self.on_status(f"agent 模式启动失败：{msg}")
                    log.exception("agent 模式启动失败")
                    self.on_lost()
                    return
                log.warning("agent 建链失败（%s），换端口重试一次", e)
                self.local_port = random.randint(29401, 29600)
                self._fport_on = False
                time.sleep(0.5)
        self.on_status("agent 推流中（画面变化时推送）")

        # 首帧兜底：画面静止时 agent 不推帧，先用 snapshot 拉一帧把画面点亮
        self._prime_first_frame(t0)

        # 主循环：有变化就转发；长时间无变化则按 heartbeat 重发上一帧。
        # 心跳的意义是让时间轴连续——静态画面下若一帧都不发，录制会得到空/超短视频，
        # GUI 的 FPS 也无从体现。重发的是同一份 JPEG 字节，解码开销可忽略。
        last_jpeg: Optional[bytes] = None
        last_emit = time.monotonic()
        try:
            while not self._stop_evt.is_set():
                jpeg = client.next_frame(idle_timeout=0.5)
                now = time.monotonic()
                if jpeg is not None:
                    if jpeg[:2] != _JPEG_SOI:
                        continue
                    last_jpeg, last_emit = jpeg, now
                    self._emit(jpeg, now - t0)
                elif (last_jpeg is not None and self.heartbeat > 0
                      and now - last_emit >= self.heartbeat):
                    last_emit = now
                    self._emit(last_jpeg, now - t0)
        except AgentError as e:
            if not self._stop_evt.is_set():
                self.on_status(f"agent 推流中断：{e}")
                self.on_lost()
        except Exception as e:
            if not self._stop_evt.is_set():
                self.on_status(f"agent 推流异常：{type(e).__name__}: {e}")
                self.on_lost()
        finally:
            self._cleanup()

    def _prime_first_frame(self, t0: float) -> None:
        """若短时间内无推流帧，用一次 snapshot_display 补齐首帧（只做一次）。

        snapshot_display 取的是原始分辨率，而推流帧是 ceil(分辨率*scale)，两者不一致
        会把录制/合成带偏（ffconcat 要求尺寸统一），故按推流尺寸等比缩放后再送。
        """
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self._stop_evt.is_set():
                return
            time.sleep(0.15)
        try:
            data = self.hdc.grab_frame(f"{TMP_DIR}/agent_prime_{os.getpid()}.jpeg")
        except Exception:
            log.debug("首帧 snapshot 兜底失败", exc_info=True)
            return
        if data[:2] != _JPEG_SOI:
            return
        data = self._conform_prime(data)
        if data:
            self._emit(data, time.monotonic() - t0)

    def _conform_prime(self, data: bytes) -> Optional[bytes]:
        """把首帧缩放到与推流一致的分辨率；拿不到目标尺寸时返回 None（宁缺勿串）。"""
        target = self._stream_size
        if not target:
            log.debug("未知推流尺寸，丢弃首帧兜底图以免尺寸串味")
            return None
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im.load()
            if im.size == target:
                return data
            out = io.BytesIO()
            im.convert("RGB").resize(target, Image.LANCZOS).save(out, "JPEG", quality=85)
            return out.getvalue()
        except Exception:
            log.debug("首帧缩放失败", exc_info=True)
            return None

    def _cleanup(self) -> None:
        if self._client is not None:
            try:
                self._client.stop_capture()
            except Exception:
                pass
            self._client.close()
            self._client = None
        self._teardown()


def capture_supported(hdc: Hdc, spec: Optional[str] = None) -> Tuple[bool, str]:
    """探测 agent 模式是否可用（不启动推流），返回 (可用, 说明)。

    `spec` 是「显式指定」：so 文件或 DevEco Testing 安装路径都行（见 find_agent_so）。
    """
    try:
        version = ""
        try:
            out = hdc.shell("uitest", "--version", timeout=15).strip()
            for token in out.replace(":", " ").split():
                if token[:1].isdigit() and token.count(".") >= 2:
                    version = token
                    break
        except HdcError:
            pass
        arch = ""
        try:
            arch = hdc.shell("param", "get", "const.product.cpu.abilist", timeout=10).strip()
        except HdcError:
            pass
        # 同上：so_path 可以是文件或 DevEco Testing 目录
        so = find_agent_so(version, arch, spec)
        if not so:
            return False, ("本机未找到 agent.so（装 DevEco Testing，"
                           "或用界面「⚙ 设置」/ --agent-so / WSCRCPY_AGENT_SO 指定"
                           " so 文件或 DevEco Testing 目录）")
        return True, f"agent.so v{_so_version_of(so)}（uitest {version or '?'} / {arch or '?'}）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
