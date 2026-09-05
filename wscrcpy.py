#!/usr/bin/env python3
"""wscrcpy —— HarmonyOS NEXT 手表/手机投屏、录屏、截图（PySide6 GUI + CLI）。

用法:
  python3 wscrcpy.py                          # 打开投屏 GUI（按钮录制/截屏）
  python3 wscrcpy.py --record out.mp4         # 打开 GUI 并立即开始录制
  python3 wscrcpy.py --shot out.jpeg          # 单帧截图后退出（CLI）
  python3 wscrcpy.py --probe                  # 真机验证清单（CLI）
  python3 wscrcpy.py --mode loop              # 实验性: 手表端 caploop 连拍提帧率
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from watchscrcpy import resources
from watchscrcpy.hdc import Hdc, HdcError


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HarmonyOS NEXT 手表/手机投屏、录屏、截图")
    p.add_argument("--serial", help="多设备时指定 -t 序列号")
    p.add_argument("--hdc-path", default=None, help="指定 hdc 路径（默认: 内置 → DevEco → PATH）")
    p.add_argument("--interval", type=float, default=0.35,
                   help="目标帧间隔秒（默认 0.35，约 1-2 fps）")
    p.add_argument("--round", choices=["auto", "on", "off"], default="auto",
                   help="圆形遮罩（圆屏手表）；auto=穿戴设备自动开")
    p.add_argument("--record", metavar="OUT.mp4", help="GUI 打开后立即开始录制到此路径")
    p.add_argument("--shot", metavar="OUT.jpeg", help="单帧截图后退出（CLI）")
    p.add_argument("--mode", choices=["pull", "loop"], default="pull",
                   help="pull=PC 逐帧驱动（默认）；loop=手表端 caploop（实验性）")
    p.add_argument("--no-wakeup", action="store_true", help="启动时不尝试唤醒亮屏")
    p.add_argument("--probe", action="store_true", help="运行真机验证清单（CLI）")
    return p.parse_args()


def connect(args) -> Hdc:
    """解析设备。失败抛 HdcError（CLI 打印 / GUI 弹窗）。"""
    serials = Hdc.list_devices(args.hdc_path)
    if not serials:
        raise HdcError("未发现设备。检查: ① 数据线是否支持传输 ② 设备开发者模式/USB调试 "
                       "③ 已授权调试 ④ 或用无线: hdc tconn <ip>:<port>")
    if args.serial:
        if args.serial not in serials:
            raise HdcError(f"指定序列号未连接: {args.serial}（在列: {', '.join(serials)}）")
        serial = args.serial
    elif len(serials) > 1:
        raise HdcError(f"连接了多台设备，请用 --serial 指定: {', '.join(serials)}")
    else:
        serial = serials[0]
    return Hdc(args.hdc_path, serial)


def probe(hdc: Hdc) -> None:
    """PLAN.md 第6节验证清单的自动化版。"""
    print("== wscrcpy 真机验证清单 ==")
    ok = lambda b: "✓" if b else "✗"
    print(f"1. 连通性            ✓  serial={hdc.serial}")
    try:
        dtype = hdc.device_type()
        print(f"2. 设备类型          {ok(bool(dtype))}  {dtype}")
    except HdcError as e:
        print(f"2. 设备类型          ✗  {e}")
    try:
        w, h = hdc.display_size()
        print(f"3. 分辨率            ✓  {w}x{h}{'（方形帧，圆屏表盘需遮罩）' if w == h else ''}")
    except HdcError as e:
        print(f"3. 分辨率            ✗  {e}")
    try:
        t0 = time.monotonic()
        data = hdc.grab_frame("/data/local/tmp/wscrcpy_probe.jpeg")
        dt = time.monotonic() - t0
        dark = "?"
        try:
            from watchscrcpy.capture import _decode
            dark = "疑似黑帧/隐私页" if _decode(data)[1] else "正常"
        except Exception:
            dark = "解码失败"
        with open("probe_frame.jpeg", "wb") as f:
            f.write(data)
        try:
            hdc.shell("rm", "-f", "/data/local/tmp/wscrcpy_probe.jpeg", timeout=10)
        except HdcError:
            pass
        print(f"4. snapshot 单帧     ✓  {len(data)} 字节, {dt:.2f}s, {dark} → probe_frame.jpeg")
    except HdcError as e:
        print(f"4. snapshot 单帧     ✗  {e}")
    print(f"5. power-shell 唤醒  {ok(hdc.wakeup())}  （False 需手动设置最长亮屏时长）")
    try:
        hdc.shell("mkdir", "-p", "/data/local/tmp/wscrcpy", timeout=10)
        out = hdc.shell("N=1; echo $((N+1))", timeout=10).strip()
        arith = out == "2"
        print(f"6. sh 脚本能力       {ok(arith)}  算术扩展={'支持' if arith else '不支持'}"
              f" → caploop 模式{'可行' if arith else '不可用，用默认 pull 模式'}")
    except HdcError as e:
        print(f"6. sh 脚本能力       ✗  {e}")
    print("== 完成 ==")


def mirror(args) -> int:
    """GUI 路径：不做启动前拦截——无设备也能打开窗口，界面上刷新连接。"""
    from PySide6.QtWidgets import QApplication

    from watchscrcpy import gui

    resources.setup_logging()
    logging.info("mirror start: mode=%s interval=%s", args.mode, args.interval)
    app = QApplication.instance() or QApplication(sys.argv)

    from watchscrcpy.hdc import Hdc as _H
    try:
        _H(hdc_path=args.hdc_path).purge_stale_temp()
    except Exception:
        pass

    def hook(t, v, tb):
        logging.exception("unhandled", exc_info=(t, v, tb))
        QMessageBox_critical(t, v)
        sys.__excepthook__(t, v, tb)

    def QMessageBox_critical(t, v):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "WSCRCPY", f"{t.__name__}: {v}")
    sys.excepthook = hook

    win = gui.MirrorWindow(interval=args.interval, round_mode=args.round, mode=args.mode,
                           serial=args.serial, hdc_path=args.hdc_path,
                           wakeup=not args.no_wakeup, auto_record=args.record)
    win.show()
    win.refresh()                      # 启动即扫描；未连接则停留在占位提示
    code = app.exec()
    logging.info("mirror exit: %s", code)
    return code


def shot(hdc: Hdc, out: str) -> int:
    try:
        data = hdc.grab_frame("/data/local/tmp/wscrcpy_shot.jpeg")
        with open(out, "wb") as f:
            f.write(data)
        try:
            hdc.shell("rm", "-f", "/data/local/tmp/wscrcpy_shot.jpeg", timeout=10)
        except HdcError:
            pass
        print(f"[i] 已保存: {out}（{len(data)} 字节）")
    except HdcError as e:
        print(f"[!] 截图失败: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    args = parse_args()
    if args.probe or args.shot:          # 纯 CLI 场景需要先连设备
        try:
            hdc = connect(args)
        except HdcError as e:
            print(f"[!] {e}", file=sys.stderr)
            return 1
        if args.probe:
            probe(hdc)
            return 0
        return shot(hdc, args.shot)
    return mirror(args)                  # GUI 自带扫描/连接状态机，无设备也能启动


if __name__ == "__main__":
    sys.exit(main())
