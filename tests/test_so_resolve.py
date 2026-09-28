#!/usr/bin/env python3
"""agent.so 多路径解析回归测试（不需要设备、不需要 DevEco）。

    python3 tests/test_so_resolve.py

伪造几种 DevEco Testing 布局（macOS .app / Windows 安装根 / 用户目录下），
逐条验证「从哪里找 so」的规则：显式指定（文件或目录）、环境变量、界面设置、
用户目录自动发现、版本与架构挑选、写错路径时的回落、缓存。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

FAIL = []


def check(name, ok, detail=""):
    print(f"{'✓' if ok else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def build_fake_trees(root):
    """三套布局 + 一个空目录，返回关键路径。"""
    # 布局 A：macOS .app，含 v1.2.2 与 v1.1.3（用来验证版本挑选）
    a = (f"{root}/dv_mac/DevEco Testing.app/Contents/Python/lib/python3.12/"
         "site-packages/devicetest/res/prototype/native")
    # 布局 B：Windows 风格（安装根下 res/prototype/native）
    b = f"{root}/dv_win/DevEco Testing/res/prototype/native"
    # 布局 C：放在"用户目录"下，验证自动发现
    c = (f"{root}/home/Applications/DevEco_Testing_for_App.app/Contents/Python/"
         "lib/python3.12/site-packages/devicetest/res/prototype/native")
    for d in (a, b, c):
        os.makedirs(d, exist_ok=True)
    for name, d in (("uitest_agent_v1.2.2.so", a), ("uitest_agent_v1.1.3.so", a),
                    ("uitest_agent_v1.1.10.so", b), ("uitest_agent_v1.2.2.so", c)):
        with open(os.path.join(d, name), "wb") as f:
            f.write(b"\x7fELF" + b"\x00" * 64)          # 内容不重要，只看路径/文件名
    os.makedirs(f"{root}/empty", exist_ok=True)
    return a, b, c


def main():
    root = tempfile.mkdtemp(prefix="wscrcpy-sotest-")
    a, b, c = build_fake_trees(root)
    os.environ["WSCRCPY_CONFIG_DIR"] = f"{root}/cfg"    # 别碰真实家目录
    os.environ.pop("WSCRCPY_AGENT_SO", None)
    os.chdir(root)

    import watchscrcpy.agent as agent
    import watchscrcpy.resources as resources

    def pick(spec=None, uitest="7.0.0.1", arch="arm64-v8a"):
        agent.clear_so_cache()
        return agent.find_agent_so(uitest, arch, spec)

    print("=== 1. --agent-so 直接指 so 文件 ===")
    f = f"{a}/uitest_agent_v1.1.3.so"
    check("指文件 → 就用该文件（即使不是最优版本）", pick(f) == f)

    print("\n=== 2. --agent-so 指 DevEco Testing 安装路径（目录）===")
    check("指 .app 根 → 其下搜到并选中最匹配版本 v1.2.2",
          (pick(f"{root}/dv_mac/DevEco Testing.app") or "").endswith("uitest_agent_v1.2.2.so"))
    check("指 Windows 风格 native 目录 → 直接用",
          (pick(b) or "").endswith("uitest_agent_v1.1.10.so"))
    check("指 Windows 风格安装根 → res/prototype/native 命中",
          (pick(f"{root}/dv_win/DevEco Testing") or "").endswith("uitest_agent_v1.1.10.so"))
    check("指中间某一层（site-packages）→ 有界遍历仍能命中",
          (pick(f"{root}/dv_mac/DevEco Testing.app/Contents/Python/lib/python3.12/"
                "site-packages") or "").endswith("uitest_agent_v1.2.2.so"))

    print("\n=== 3. 路径书写容错（引号 / ~ / 空白）===")
    check("带双引号+前后空白仍能解析",
          (pick(f'  "{root}/dv_mac/DevEco Testing.app"  ') or "").endswith("uitest_agent_v1.2.2.so"))
    os.environ["HOME"] = f"{root}/home"                 # expanduser 在 POSIX 上跟 $HOME 走
    check("含 ~ 的路径会展开到用户目录",
          (pick("~/Applications/DevEco_Testing_for_App.app") or "").endswith("uitest_agent_v1.2.2.so"))
    del os.environ["HOME"]

    print("\n=== 4. 环境变量 WSCRCPY_AGENT_SO（文件 / 目录两种）===")
    os.environ["WSCRCPY_AGENT_SO"] = f"{root}/dv_win"
    check("环境变量指目录 → 其下搜到", (pick() or "").endswith("uitest_agent_v1.1.10.so"))
    os.environ["WSCRCPY_AGENT_SO"] = f"{a}/uitest_agent_v1.2.2.so"
    check("环境变量指文件 → 直接用", pick() == f"{a}/uitest_agent_v1.2.2.so")
    os.environ.pop("WSCRCPY_AGENT_SO")

    print("\n=== 5. 界面「设置」里的路径（配置文件）===")
    where = resources.set_setting(agent.SETTING_DEVECO_PATH, f"{root}/dv_win")
    check("设置已落盘", bool(where) and os.path.isfile(where), where)
    check("设置里的路径参与解析", (pick() or "").endswith("uitest_agent_v1.1.10.so"))
    ok, msg = agent.describe_deveco_path(f"{root}/dv_win")
    check("describe_deveco_path 报成功", ok, msg)
    ok2, msg2 = agent.describe_deveco_path(f"{root}/nope")
    check("不存在的路径被识别为失败", not ok2, msg2)
    ok3, msg3 = agent.describe_deveco_path(f"{root}/empty")
    check("空目录被识别为「没找到 so」", not ok3, msg3)
    resources.set_setting(agent.SETTING_DEVECO_PATH, "")
    agent.clear_so_cache()

    print("\n=== 6. 用户目录自动发现（程序装在任意目录都行）===")
    os.environ["HOME"] = f"{root}/home"
    agent.clear_so_cache()
    t0 = time.monotonic()
    got = pick()
    el = time.monotonic() - t0
    check("从用户目录自动发现 DevEco 并命中 v1.2.2",
          (got or "").endswith("v1.2.2.so"), f"得到 {got}")
    check("自动发现耗时可控（<1.5s）", el < 1.5, f"{el:.2f}s")
    del os.environ["HOME"]

    print("\n=== 7. 版本/架构挑选 ===")
    cases = [
        (f"{root}/dv_mac/DevEco Testing.app", "7.0.0.1", "uitest_agent_v1.2.2.so",
         "命中精确版本"),
        (f"{root}/dv_mac/DevEco Testing.app", "5.1.1.2", "uitest_agent_v1.1.3.so",
         "无更高版本→取不高于目标的"),
        (f"{root}/dv_win", "6.0.2.1", "uitest_agent_v1.1.10.so", "命中 1.1.10"),
        (f"{root}/dv_mac/DevEco Testing.app", "6.0.2.1", "uitest_agent_v1.1.3.so",
         "目标 1.1.10 不在该目录→回退不高于它的 1.1.3（不误用 1.2.2）"),
    ]
    for spec, uv, want, note in cases:
        got = os.path.basename(pick(spec, uv) or "")
        check(f"uitest {uv} {note}", got == want, f"得到 {got}")

    print("\n=== 8. 显式路径写错时不卡死，仍走其它来源 ===")
    t0 = time.monotonic()
    got = pick(f"{root}/definitely-not-exist-xyz")
    el = time.monotonic() - t0
    check("写错路径 → 回落到其它来源（本机 DevEco 或内置 vendor/so）",
          bool(got), f"得到 {got}")
    check("且耗时可接受（<2s）", el < 2.0, f"{el:.2f}s")

    print("\n=== 9. 缓存 ===")
    agent.clear_so_cache()
    t0 = time.monotonic(); pick(f"{root}/dv_win"); t1 = time.monotonic()
    t2 = time.monotonic(); pick(f"{root}/dv_win"); t3 = time.monotonic()
    check("同参数第二次调用不走目录", (t3 - t2) <= (t1 - t0) + 1e-6,
          f"首扫 {t1 - t0:.4f}s / 缓存 {t3 - t2:.4f}s")

    shutil.rmtree(root, ignore_errors=True)
    print("\n结果:", "全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
