#!/usr/bin/env python3
"""界面「设置」浮层回归测试（离屏渲染，不需要设备）。

    python3 tests/test_gui_settings.py

验证：底部最右有「设置」按钮 → 点开浮层（输入框上方一行提示语）→ 保存合法路径
落盘并立即生效 → 非法路径不保存且不关窗 → Esc/空值/未连接/已连接各条分支。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # 无显示器也能跑
os.environ.pop("WSCRCPY_AGENT_SO", None)

from PySide6.QtCore import QEvent, Qt                      # noqa: E402
from PySide6.QtGui import QKeyEvent                        # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QPushButton      # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(f"{'✓' if ok else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def main():
    root = tempfile.mkdtemp(prefix="wscrcpy-guitest-")
    cfgdir = f"{root}/cfg"
    os.makedirs(cfgdir, exist_ok=True)
    os.environ["WSCRCPY_CONFIG_DIR"] = cfgdir
    good = f"{root}/dv_mac/DevEco Testing.app"
    native = (f"{good}/Contents/Python/lib/python3.12/site-packages/devicetest"
              "/res/prototype/native")
    os.makedirs(native, exist_ok=True)
    for name in ("uitest_agent_v1.2.2.so", "uitest_agent_v1.1.3.so"):
        with open(os.path.join(native, name), "wb") as f:
            f.write(b"\x7fELF" + b"\x00" * 64)

    import watchscrcpy.agent as agent
    from watchscrcpy.gui import MirrorWindow, SettingsOverlay

    app = QApplication([])
    win = MirrorWindow(agent_scale=0.99)
    win.resize(940, 770)
    win.show()
    app.processEvents()
    cfg_path = os.path.join(cfgdir, "settings.json")

    print("=== 1. 底部按钮栏：设置在最右 ===")
    buttons = [win.btn_refresh, win.btn_quality, win.btn_rec, win.btn_shot, win.btn_quit,
               win.btn_settings]
    rights = {b.text().strip(): b.geometry().right() for b in buttons}
    check("存在「设置」按钮", win.btn_settings.text().strip().endswith("设置"))
    check("设置按钮在所有底部按钮的最右",
          win.btn_settings.geometry().right() == max(rights.values()), str(rights))
    check("未连接也能点（本地配置）", win.btn_settings.isEnabled())

    print("\n=== 2. 浮层结构与文案 ===")
    ov: SettingsOverlay = win.settings
    check("初始隐藏", not ov.isVisible())
    win.btn_settings.click()
    app.processEvents()
    check("点击后浮层可见", ov.isVisible())
    check("hint 文案逐字一致",
          ov.hint.text() == "如要开启原生画质，请配置DevEco Testing安装路径。",
          repr(ov.hint.text()))
    check("hint 在输入框上方",
          ov.hint.geometry().bottom() <= ov.input.geometry().top())
    check("有单行输入框", isinstance(ov.input, QLineEdit))
    check("浮层铺满窗口", ov.geometry() == win.rect())
    check("输入时窗口快捷键被停用（R/S 不会被录制/截屏抢走）",
          all(not sc.isEnabled() for sc in win._shortcuts))

    print("\n=== 3. 保存合法路径 ===")
    ov.input.setText(good)
    ov.btn_save.click()
    app.processEvents()
    check("浮层关闭", not ov.isVisible())
    check("快捷键恢复", all(sc.isEnabled() for sc in win._shortcuts))
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    check("配置已落盘且键名正确", cfg.get(agent.SETTING_DEVECO_PATH) == good, str(cfg))
    check("状态栏给出结论", "设置已保存" in win.status.text(), win.status.text())
    agent.clear_so_cache()
    so = agent.find_agent_so("7.0.0.1", "arm64-v8a")
    check("保存后解析立即生效", bool(so) and so.startswith(good), str(so))

    print("\n=== 4. 再次打开预填当前值 ===")
    win.btn_settings.click()
    app.processEvents()
    check("预填已保存路径", ov.input.text() == good, ov.input.text())

    print("\n=== 5. 非法路径：不保存、不关窗、说明原因 ===")
    ov.input.setText(f"{root}/nope")
    ov.btn_save.click()
    app.processEvents()
    check("浮层保持打开", ov.isVisible())
    check("提示为错误态", ov.msg.property("bad") == "true", str(ov.msg.text()))
    check("提示说明了原因", "不存在" in ov.msg.text(), ov.msg.text())
    with open(cfg_path, encoding="utf-8") as f:
        check("配置未被改坏", json.load(f).get(agent.SETTING_DEVECO_PATH) == good)

    print("\n=== 6. Esc 关闭 & 空值清空 ===")
    ov.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    check("Esc 关闭浮层", not ov.isVisible())
    win.btn_settings.click(); app.processEvents()
    check("重新打开可见", ov.isVisible())
    ov.input.setText("")
    ov.btn_save.click(); app.processEvents()
    check("空值＝清空设置并关闭", not ov.isVisible())
    with open(cfg_path, encoding="utf-8") as f:
        check("配置里路径已清空", json.load(f).get(agent.SETTING_DEVECO_PATH) == "")

    print("\n=== 7. 浮层随窗口尺寸同步 ===")
    win.btn_settings.click(); app.processEvents()
    win.resize(1100, 800); app.processEvents()
    check("resize 后浮层仍铺满窗口", ov.geometry() == win.rect())
    ov.close_overlay()

    print("\n=== 8. 直接填 so 文件也被接受 ===")
    win.btn_settings.click(); app.processEvents()
    ov.input.setText(os.path.join(native, "uitest_agent_v1.2.2.so"))
    ov.btn_save.click(); app.processEvents()
    check("填 so 文件路径同样保存成功",
          not ov.isVisible() and "设置已保存" in win.status.text(), win.status.text())

    print("\n=== 9. 已连接 + agent 推流中：保存后自动重启采集 ===")
    win.hdc = object()                     # 只验证调度，不真连设备
    win.effective_mode = "agent"
    try:
        win.bridge.quality_restart.disconnect()
    except Exception:
        pass
    torn = []
    win._teardown_capture = lambda wait=3.0: torn.append(wait)
    ov.input.setText(good)
    ov.btn_save.click()
    app.processEvents()
    check("保存后自动排上重启（防重入：按钮置灰+置位）",
          win._quality_switching and not win.btn_quality.isEnabled())
    time.sleep(0.6)
    app.processEvents()
    check("重启流程按 wait=20 收掉旧采集", torn == [20.0], str(torn))

    print("\n=== 10. 未连接时不重启（只存配置）===")
    win._quality_switching = False
    win.btn_quality.setEnabled(True)
    win.hdc = None
    win.btn_settings.click(); app.processEvents()
    ov.input.setText(good)
    ov.btn_save.click(); app.processEvents()
    check("未连接：不触发重启，仅提示已保存",
          not win._quality_switching and "设置已保存" in win.status.text(),
          win.status.text())
    ov.close_overlay(); app.processEvents()

    print("\n=== 11. 卡片里的按钮文字不能被切（曾经的 bug：被父布局压扁 10px）===")

    def glyph_margins(w):
        """渲染该控件，返回文字（亮像素）到四边的留白（设备像素）。"""
        img = w.grab().toImage()
        iw, ih = img.width(), img.height()

        def bright(x, y):
            c = img.pixelColor(x, y)
            return (c.red() + c.green() + c.blue()) / 3 > 90

        rows = [y for y in range(ih) if any(bright(x, y) for x in range(iw))]
        cols = [x for x in range(iw) if any(bright(x, y) for y in range(ih))]
        if not rows or not cols:
            return None
        return (min(cols), min(rows), iw - 1 - max(cols), ih - 1 - max(rows))

    win.hdc = None
    win._quality_switching = False
    win.btn_settings.click(); app.processEvents()
    ov = win.settings
    for name, b in (("浏览目录…", ov.btn_browse), ("浏览文件…", ov.btn_browse_file),
                    ("取消", ov.btn_cancel), ("保存", ov.btn_save)):
        hint = b.sizeHint()
        check(f"{name} 拿到完整高度（不被父布局压扁）",
              b.height() >= hint.height() and b.width() >= hint.width(),
              f"实际 {b.width()}x{b.height()} / sizeHint {hint.width()}x{hint.height()}")
        m = glyph_margins(b)
        check(f"{name} 文字四周都没贴边（渲染实测）",
              m is not None and min(m) >= 4, f"留白(设备像素)={m}")
    for name, w in (("hint", ov.hint),):
        check(f"{name} 自动换行后高度足够",
              w.height() >= w.heightForWidth(w.width()),
              f"{w.height()} vs 需要 {w.heightForWidth(w.width())}")
    check("卡片完整落在浮层内",
          ov.card.width() <= ov.width() and ov.card.height() <= ov.height(),
          f"卡片 {ov.card.width()}x{ov.card.height()} / 浮层 {ov.width()}x{ov.height()}")
    check("卡片水平居中",
          abs(ov.card.x() - (ov.width() - ov.card.width()) // 2) <= 2,
          f"x={ov.card.x()}")
    ov._set_msg("路径不存在：/tmp/xxx——请确认选的是 DevEco Testing 安装根目录，"
                "而不是它下面的某个子目录；也可以直接填 uitest_agent_v*.so 文件路径。", bad=True)
    app.processEvents()
    check("长错误文案：卡片随之变高且文字不被切",
          ov.msg.height() >= ov.msg.heightForWidth(ov.msg.width())
          and ov.card.height() <= ov.height() and ov.btn_save.height() >= ov.btn_save.sizeHint().height(),
          f"msg {ov.msg.height()} / 需要 {ov.msg.heightForWidth(ov.msg.width())}，"
          f"卡片 {ov.card.height()}，按钮 {ov.btn_save.height()}")
    ov._set_msg("", bad=False)
    ov.close_overlay()

    print("\n=== 12. 浏览按钮：一次点击只弹一个对话框，取消就是取消（曾经的 bug）===")
    dialogs = []
    real_dir, real_file = QFileDialog.getExistingDirectory, QFileDialog.getOpenFileName
    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: (dialogs.append("目录"), "")[1])
    QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (dialogs.append("文件"), ("", ""))[1])
    try:
        win.btn_settings.click(); app.processEvents()
        ov.input.setText("")
        ov.btn_browse.click(); app.processEvents()
        check("点「浏览目录…」只弹 1 个对话框（取消后不再弹第二个）",
              dialogs == ["目录"], str(dialogs))
        dialogs.clear()
        ov.btn_browse_file.click(); app.processEvents()
        check("点「浏览文件…」只弹 1 个文件对话框", dialogs == ["文件"], str(dialogs))
        dialogs.clear()
        # 选中路径要回填输入框
        QFileDialog.getExistingDirectory = staticmethod(
            lambda *a, **k: (dialogs.append("目录"), "/tmp/sores-ish")[1])
        ov.btn_browse.click(); app.processEvents()
        check("选中目录后回填输入框", ov.input.text() == "/tmp/sores-ish", ov.input.text())
        check("回填时也只弹了 1 个对话框", dialogs == ["目录"], str(dialogs))
    finally:
        QFileDialog.getExistingDirectory, QFileDialog.getOpenFileName = real_dir, real_file
    ov.close_overlay()

    shutil.rmtree(root, ignore_errors=True)
    print("\n结果:", "全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
