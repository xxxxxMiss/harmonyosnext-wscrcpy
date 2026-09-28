# harmonyosnext-scrcpy 规划文档

> HarmonyOS NEXT 穿戴设备（手表）投屏 / 录屏 / 截图工具 —— 路线2（连拍截图 + 合成）实施方案
>
> 调研时间：2026-09（前序会话完成调研，本文档落盘）
> 状态：**M1 MVP 已开发完成并通过本地自检**（无设备场景）；所有设备侧行为待手表真机验证（连上后先跑 `python3 wscrcpy.py --probe`）

---

## 1. 背景与目标

为 HarmonyOS NEXT **穿戴设备（手表）** 做一个 PC 端投屏工具：USB（hdc）连接后，PC 实时显示手表画面，并支持录屏、截图。

非目标（当前版本）：
- 反向控制（触控/按键注入回手表）—— 管线已具备 `uitest uiInput` 能力，留待后续版本
- 音频转发
- 无线（Wi-Fi）模式 —— 作为充电线无数据通道时的备选（`hdc tconn`）

## 2. 路线调研与决策

| # | 路线 | 结论 |
|---|------|------|
| 1 | 手表端 Agent App + `AVScreenCaptureRecorder`（API 12+） | **已否决**：用户确认该 API 不支持穿戴设备 |
| 2 | **连拍截图 + 合成**（`snapshot_display` 循环抓帧，PC 端拉取渲染） | **✅ 已选定** |
| 3 | DevEco Studio 手动录屏 | 仅作对照/兜底，非工具化目标 |
| 4 | 系统侧 / 厂商路线（系统签名 CAPTURE_SCREEN、Cast+、云真机） | 应用层走不通时的唯一出路，暂不投入 |

### 关键调研事实
- hdc **无原生录屏命令**（官方开发中）；`uitest uiRecord record` 录的是**操作事件 CSV**，不是视频。
- 商用 NEXT 设备上 **scrcpy 式方案走不通**（无 `app_process` 等价物）；社区 OHScrcpy 仅在 OpenHarmony 开发板（RK3568）跑通，仅参考其架构思路。
- `snapshot_display` 比 `uitest screenCap` 快，且输出 JPEG（体积小，利于 USB 传输）。

## 3. 路线2 技术方案

### 3.1 架构

```
┌─────────────── 手表（无 Agent，纯 shell）────────┐      ┌──────────── PC 端（Python）────────┐
│                                                  │      │                                     │
│  [模式A: PC 逐帧驱动]                             │ hdc  │  拉帧管线 FrameSource                │
│    snapshot_display ←── 每帧由 PC 触发 ───────────────→  单帧: rm → 触发 → 轮询 → recv        │
│                                                  │ USB  │                                     │
│  [模式B: caploop.sh 后台连拍（实验性）]            │      │  ├─ 投屏: tkinter 渲染（2x + 圆遮罩）│
│    caploop.sh 环形缓冲 ~40 帧 ←── PC 只拉新帧 ────────→  └─ 录制: 帧落盘 + 时间戳 → ffmpeg     │
└──────────────────────────────────────────────────┘      └─────────────────────────────────────┘
```

预期帧率：**1–3 fps 准实时**（受 `snapshot_display` 单帧耗时 + USB 传输限制）。

### 3.2 单帧采集（竞态安全，复用 auto-shot 已验证模式）

```bash
hdc shell rm -f /data/local/tmp/wscrcpy_f.jpeg      # 1. 删旧文件（消除"拿到上一帧"的竞态）
hdc shell snapshot_display -f /data/local/tmp/wscrcpy_f.jpeg   # 2. 触发截图
# 3. 轮询等新文件出现且有效（snapshot_display 异步写盘）
hdc file recv /data/local/tmp/wscrcpy_f.jpeg local.jpeg        # 4. 拉取
```

有效性校验：JPEG 魔数 `\xff\xd8` + 长度阈值；部分隐私页面**禁止截屏会得到黑帧/失败，标记而非当故障**。
兼容降级：`snapshot_display` 不存在时回退 `uitest screenCap -p`（PNG）。

### 3.3 提帧率关键：caploop.sh（模式B，实验性）

- `caploop.sh` 经 `hdc file send` 推上手表，`nohup sh caploop.sh &` 后台跑；
- 环形缓冲保留约 40 帧（`f%06d.jpeg`，写满回绕覆盖）；
- PC 每 ~250ms `hdc shell ls /data/local/tmp/wscrcpy/` 轮询，**只 recv 新序号帧**（序号单调递增，回绕时以 mtime 兜底）；
- **风险**：toybox sh 对 `while`/算术的支持需真机验证；不支持则退回模式A（PC 逐帧驱动，功能等价只是帧率低）。

### 3.4 录屏 = 帧落盘 + 停止后合成

- 采集循环同步把每帧写 `out_frames/f%06d.jpeg` + `timestamps.txt`（帧号 + 相对秒）；
- 停止后用 **concat demuxer + duration** 做 VFR 合成（帧间隔抖动大，固定 framerate 会变速）：

```bash
ffmpeg -f concat -safe 0 -i ffconcat.txt \
       -vf "scale=iw*2:ih*2,format=yuv420p" -c:v libx264 -vsync vfr out.mp4
```

- ffmpeg 不在 PATH 时：保留帧目录，打印可直接执行的合成命令（本机调研确认 ffmpeg 未装，需 `brew install ffmpeg`）。

### 3.5 PC 端 MVP

PC 逐帧驱动版（模式A）：tkinter + Pillow，约 70 行核心逻辑；**投屏与录制共享同一条拉帧管线**（录制只是在循环里多写一份帧文件）。

## 4. 手表特有的坑（按优先级）

| # | 风险 | 对策 | 优先级 |
|---|------|------|--------|
| 1 | 息屏导致黑帧/截图失败 | 先试 `hdc shell power-shell wakeup` / `setmode`；不行则提示手动设置最长亮屏时长。**MVP 第一天解决** | P0 |
| 2 | 充电线可能无数据通道 | 先验证 `hdc list targets` 连通；备选 `hdc tconn <ip>:<port>` 无线调试 | P0 |
| 3 | 隐私页截图黑帧/失败 | 检测到黑帧/魔数无效时在 UI 标记"（隐私页/黑帧）"，不中断循环、不当故障报 | P1 |
| 4 | 圆屏手表：帧为方形 framebuffer（约 466×466） | PC 端 2x 放大 + 可选圆形遮罩渲染 | P1 |

## 5. 里程碑

- **M1（MVP，本次交付）**：PC 逐帧驱动投屏 + 录制 + 单帧截图 + `--probe` 自检；hdc 封装含竞态处理与双命令降级。
- **M2**：真机验证清单跑通后，视帧率决定是否启用 caploop.sh 模式B。
- **M3**：打磨——帧间隔自适应、点击回控（`uitest uiInput`）、打包一条命令安装。

## 6. 真机验证结果

### 手机（9CN0224A11000514，HarmonyOS NEXT，1216x2688）——2026-09-05 全部通过 ✅

| 项目 | 结果 |
|------|------|
| probe 验证清单 | **6/6 通过**：连通 / `param get`→phone / 1216x2688 / snapshot 单帧 0.65s·399KB / power-shell 唤醒可用 / sh 算术扩展支持 |
| pull 模式无头采集 | 10s 15 帧，**1.58 fps**，0 错误 0 黑帧（单帧 0.68s，采集耗时主导） |
| 录制闭环 | 边投边录 6s → `ffconcat` VFR 合成 mp4（1216x2688，10 帧，5.4s 时长），ffprobe 验证通过 |
| loop 模式（caploop） | **真机可行**：1.62 fps，停止后设备侧进程正确退出（seq 不再增长） |
| GUI 投屏 | 窗口实时拉帧验证（帧文件 mtree 持续更新，~399KB/帧），进程干净退出 |

### 真机暴露并已修复的问题
1. **NEXT 无 `getparam` 命令**，正确的是 `param get <key>`（已做双命令降级）
2. **hdc 客户端往 stdout 混入 `[W]` 时间戳日志行**，会污染数值解析（seq/cat 等），已统一清洗
3. 重定向 stdout 时运行时状态消息被缓冲，关键 print 已加 flush

### 待手表真机验证（手机无法覆盖的部分）
- 手表上 `snapshot_display` 是否存在/耗时（决定实际帧率）
- 手表上 `power-shell wakeup` 行为、方形 framebuffer 圆形遮罩的视觉效果
- 环境注意：电脑侧截屏/无障碍权限未授权给 ZCode，GUI 画面需人工目视确认

（原始验证清单见下，保留作参照）

```bash
hdc list targets                                        # 1. 连通性
hdc shell param get const.product.devicetype            # 2. 设备类型（NEXT 用 param get）
hdc shell snapshot_display -f /data/local/tmp/t.jpeg    # 3. 单帧命令是否存在/耗时
hdc file recv /data/local/tmp/t.jpeg t.jpeg             # 4. 文件可拉取且为有效 JPEG
hdc shell power-shell wakeup                            # 5. 唤醒命令是否可用
hdc shell "N=1; echo $((N+1))"                          # 6. sh 脚本能力（决定 caploop 可行性）
```

本工具已内置：`python3 wscrcpy.py --probe` 自动跑以上清单并输出报告。

## 7. 环境（2026-09-05 实测）

- hdc：`/Users/<user>/Library/Huawei/Sdk/hmscore/3.1.0/toolchains/hdc`（Ver 3.2.0d；调研时 `hdc list targets` 为空）
- Python 3.12 + Pillow ✓；Tk 支持已补装（`brew install python-tk@3.12`）；ffmpeg 9.0.1 已装（`brew install ffmpeg`）
- ⚠ 实测 ffmpeg 9.0 起 **`-vsync` 选项已移除**，VFR 合成须用 `-fps_mode vfr`（recorder.py 已做新旧版本双兼容）
- 可复用竞态处理基建：`/Users/<user>/workspace/ai-test/auto-shot/autoshot/hdc_driver.py`
- hdc 命令参考：github.com/codematrixer/awesome-hdc

## 8. 项目结构

```
harmonyosnext-scrcpy/
├── PLAN.md                # 本文档
├── README.md              # 使用说明
├── wscrcpy.py             # CLI 入口（mirror / record / shot / probe）
├── scripts/caploop.sh     # 手表端后台连拍脚本（实验性，模式B）
└── watchscrcpy/
    ├── hdc.py             # hdc 封装：设备、截屏（竞态安全+降级）、唤醒、输入
    ├── capture.py         # 拉帧管线 FrameSource（投屏/录制共享）
    ├── viewer.py          # tkinter 渲染窗口（缩放、圆形遮罩、黑帧标记）
    └── recorder.py        # 录制：帧落盘 + 时间戳 + ffmpeg VFR 合成
```

## 9. 里程碑 M4：跨平台 GUI 应用（零环境依赖）

> **GUI 方案已定（2026-09-05 与用户对齐）：PySide6 深色控制台风**——自绘无边框标题栏、
> HUD 视频面板（角标括号+扫描线）、霓虹描边按钮、呼吸式 REC 指示灯，深色 #0A0E1A 底 +
> 青色霓虹 #00E5FF 强调。放弃 tkinter（复古观感且多一个系统依赖；python-tk 不再需要），
> 采集/录制管线不动只换壳。备选方案 CustomTkinter（上限不够炫）、pywebview+HTML
> （视觉天花板但多一层 JS 桥）已评估未采用。

> 目标形态：**macOS 双击 `Wscrcpy.app`、Windows 双击 `Wscrcpy.exe`** → 弹出投屏窗口，
> 窗口内按钮直接【截屏】【录制】【停止】。**用户机器零配置**——Python/Qt/Pillow/hdc/ffmpeg
> 全部打进程序包，不装 DevEco、不装 ffmpeg、不配 PATH。CLI 保留为开发者入口。

### 9.1 现状差距

| # | 差距 | 说明 |
|---|------|------|
| 1 | GUI 无按钮 | 截屏/录制目前只有 `s`/`r` 快捷键和 CLI 参数，双击用户不可发现 |
| 2 | 录制输出位置 | 现在靠 `--record out.mp4` 指定；GUI 需要文件对话框 + 智能默认名 |
| 3 | 错误不可见 | 打包后无终端，`connect()` 的 `sys.exit` 文本和异常用户根本看不到 |
| 4 | 依赖散落本机 | Pillow/Tk 靠 pip/brew；hdc 写死 macOS 路径；ffmpeg 靠 PATH——换机即失效 |
| 5 | 平台差异未处理 | hdc 二进制按 OS 不同；Windows 下子进程弹黑框；高分屏 DPI；字体名 |
| 6 | ffmpeg 合成阻塞 UI | stop 后同步跑 ffmpeg，录制长时窗口会卡住数秒 |

### 9.2 依赖打包矩阵（零配置的核心）

| 依赖 | macOS 包 | Windows 包 | 来源 |
|------|----------|-----------|------|
| Python + tkinter + Pillow | PyInstaller 打进 .app | PyInstaller 打进 .exe（onedir） | 构建机自备 |
| hdc | `hdc`（macOS 版）→ Resources/ | `hdc.exe` → Resources/ | DevEco Command Line Tools 各平台包提取 |
| ffmpeg | 静态 ffmpeg → Resources/ | 静态 ffmpeg.exe → Resources/ | evermeet.cx（mac）/ gyan.dev 或 BtbN（win） |
| caploop.sh | Resources/（设备侧脚本，两平台同用） | 同左 | 已在仓库 |

查找顺序统一为：**程序内置资源 → 平台默认路径 → PATH**；Windows 的 hdc 调用自动带 `.exe`。
PyInstaller 不跨平台交叉编译：**mac 包在 mac 上构建，win 包在 Windows/CI 上构建**
（提供 `build.sh` + `build.ps1` + GitHub Actions workflow，产物即取即用）。

### 9.3 实施步骤与进度（2026-09-05）

**Step 1 — GUI 补齐 + 跨平台代码改造 ✅ 完成**
- PySide6 深色控制台窗口（`watchscrcpy/gui.py`，tkinter viewer 已删除）：
  无边框自绘标题栏（拖拽/最小化/关闭）、HUD 视频面板（角标括号+扫描线+辉光边）、
  霓虹描边按钮、HUD 状态条（DEV/FPS/FRM/T+）、录制计时与红色态样式；
- Recorder 惰性创建：首次点【录制】弹文件对话框（默认桌面/图片目录，
  `wscrcpy_YYYYmmdd_HHMMSS.mp4`）；截屏同理 PNG；`--record` 参数=打开即录；
- ffmpeg 合成后台线程 + 完成信号回 UI（「合成中…」→「已保存 xxx」）；
- 跨平台收口 `resources.py`：内置资源定位（`_MEIPASS` + Frameworks/Resources 多候选根）、
  hdc/ffmpeg 查找链、Windows `CREATE_NO_WINDOW`、日志目录（~/Library/Logs、%APPDATA%）；
- 错误面 UI 化：无设备 QMessageBox 弹窗（冻结态已实测），根 excepthook 兜底弹窗+日志。
- **离屏自动化闭环（QT_QPA_PLATFORM=offscreen + exec 事件循环）真机通过**：
  帧渲染→截屏 PNG→录制 7 帧→后台合成 mp4→状态回调，全程零手工。

**Step 1+ — 交互与清洁度优化（用户反馈，✅ 完成 2026-09-05）**
- **无设备可启动**：连接逻辑改为 GUI 内状态机（未连接 → ⟳ 刷新扫描 → 投屏中）。
  启动不再被"未发现设备"拦截：面板显示占位文案，点「⟳ 刷新」（或 Ctrl+R）扫描并在
  找到设备后自动进入投屏；投屏中设备失联（连续 5 帧失败）自动回到未连接态，
  已录制的帧会先保住再退出采集。
- **中间数据不残留**：
  - 纯投屏本就不落盘（仅复用同一个本机中转临时文件）；
  - 录制在合成 mp4 **成功后自动删除中间帧目录**（ffmpeg 缺失时才保留帧并提示命令）；
  - 程序退出时清理设备端 `/data/local/tmp/wscrcpy` 与本机中转临时文件（后台 best-effort），
    启动时再按 mtime 清理 >1h 的陈旧中转文件兜底异常退出。
- **多实例隔离**：远端帧路径与本机中转文件均按进程 PID 隔离，两个窗口同时投屏互不干扰。
- 离屏验证：无设备启动/刷新连接/渲染/录制/帧目录删除/截屏/退出清理 全部通过
  （真机走真实 hdc；接线验证用 FakeHdc 桩覆盖无设备场景）。

**Step 1++ — hdc 版本耦合治理（✅ 完成 2026-09-05 晚）**

手机系统升级后抬高 hdc 最低协议版本，本机全部旧 hdc 出现 `E000001 version is too low`
（list 正常、shell 全拒）。定位与治理：
- 本机 hdc 有 4 套（hmscore 3.2.0d / openharmony 1.2.0a / Huawei-10 1.2.0a / DevEco 1.2.0a），
  唯一可用的是升级 SDK 后的 `~/Library/OpenHarmony/Sdk/23/toolchains/hdc`；
- toolchains 内的 `HdcExternal`（1.0.6 外部模式 server）会抢占 5037 端口且被新手机拒绝——
  build.sh **刻意不打包** libexternal_hdc.dylib，避免打包版陷入同一陷阱；
- `find_hdc()`/build.sh 改为**版本感知**（OpenHarmony Sdk 新→旧 → hmscore 新→旧 →
  DevEco 内置 → PATH）；`hdc.py` 增加 E000001 检测，报错直接给出升级指引；
- 修复 `snapshot_display` 目录依赖（目标目录不存在时报 invalid realpath，grab 前先 mkdir -p）。
- 打包解决的是"用户不用装 hdc"，不解决"hdc 快照要跟得上手机"——hdc 属驱动级组件，
  手机 OTA 后需用新 SDK 重新 build.sh 即可，运行时依旧零外部依赖。

**Step 2a — macOS 打包 ✅ 完成（含冻结态连机全链路复验 2026-09-05）**
- `wscrcpy.spec` + `build.sh`：`dist/Wscrcpy.app`（140MB，arm64）；
- 内置 ffmpeg 9.0.1（homebrew arm64 + 22 个动态库由 PyInstaller 收集）实测可运行；
- ⚠ 实测 hdc **非单文件二进制**：依赖 `libusb_shared.dylib`（@rpath 链接）与
  `libexternal_hdc.dylib`（运行时 dlopen 探测），已随包打入 Frameworks 根与 bin/ 旁；
- **冻结态连机复验（拷贝至 /tmp 模拟裸机）全部通过**：
  `--probe` 6/6；GUI 双击启动 + `--record` 自动录制 17s/27 帧；AppleScript 优雅退出
  → closeEvent 触发后台合成 → mp4（1216x2688, ffprobe 验证）→ 进程干净退出。

**Step 2b — Windows 打包 ✅ 完成（GitHub Actions 云构建出包，2026-09-06）**
- `build.ps1`（hdc.exe 定位/伴随 DLL 复制、BtbN 主源 + gyan 备源自动下载静态 ffmpeg）
  + CI workflow（windows-latest）**已实测出包**：`Wscrcpy-Windows` artifact 112.9MB；
- 排障历程（私有仓库日志不可读，靠 artifact/分支回传 + 本地 pwsh 验证迭代）：
  1. **根因**：spec 里 `EXE = ".exe"` 变量遮蔽 PyInstaller 的 `EXE()` 全局 → 双平台
     spec 解析即崩（v0.1.0-v0.1.2 全挂）→ 改名 EXE_SUFFIX；
  2. build.sh `set -e` 下 dylib 空匹配循环中断 → nullglob；
  3. `HDC_BIN=vendor/bin/hdc` 时 `cp -f` 自拷报错 → realpath 同文件守卫（sh/ps1 双侧）；
  4. CI_PAT 密钥（可选，见下）用于失败日志回传 `ci-logs/*` 分支；
- 待用户在 Windows 真机冒烟（README 清单）。

**密钥配置（可选）**：仓库 Secrets → Actions → `CI_PAT`（经典 PAT，勾选 repo）。
构建本身**零密钥**（hdc 入库、ffmpeg 自动下载、GITHUB_TOKEN 内置）；
CI_PAT 仅用于构建失败时把日志推回 `ci-logs/*` 分支供远程诊断（未配置时仅能
在 Actions 页面看日志/artifact）。

**Step 3 — 打磨（未开始）**
- 图标、Dock/任务栏名称、签名、记住上次输出目录。

### 9.4 风险与对策

| 风险 | 对策 |
|------|------|
| PyInstaller + tkinter 打包后窗口起不来 | Step 2a 先出裸冒烟包，问题前置暴露 |
| 静态 ffmpeg 体积（每个 ~25MB，包体 ~60MB） | 接受；onedir 首启快；若苛求可换 ffmpeg-lgpl 去掉 x264 |
| Windows 未签名 exe 被 SmartScreen/杀软拦 | 自签名 + 文档说明放行；不购买证书（个人/内部使用） |
| hdc 各平台提取源（DevEco CLT 分平台 zip） | build 脚本固定下载/缓存路径，版本与 hdc 3.2.0d 对齐 |
| tkinter 对话框线程规则 | 文件对话框只在按钮回调（UI 线程）里调，采集线程只发事件 |
| Windows 无法本机自动化验证 | CI 构建 + 冒烟清单人工过一遍；mac 侧自动化照旧 |

## 9.5 路线2-B 调研：官方流畅投屏/录屏的真实现（2026-09-06 完成）✅

> 背景：连续截图在动画场景基本不可用（1-2fps），而 DevEco Testing 投屏/录屏流畅。
> 通过解剖本机 `DevEco_Testing_for_App.app`（明文 Python 客户端 + so 资源）完整还原了官方实现。

### 官方实现全链路（已 100% 还原）

1. **素材在 PC 侧**：`devicetest/res/recorder/libscrcpy_server1~4.z.so`（ARM64 ELF，4 份对应
   不同系统版本；华为内部就叫 scrcpy server，思路同 Android scrcpy 的 app_process）
2. **推送**：`hdc file send` → 设备 `/data/local/tmp/libscreen_recorder.z.so`（带 md5 增量更新）
3. **启动（关键魔法）**：`/system/bin/uitest start-daemon singleness --extension-name
   libscreen_recorder.z.so -p 5001 -m 1 -screenId 0`——**uitest 是系统签名二进制，
   dlopen 该 so 并以系统权限运行**，so 导出 `UiTestExtension_OnInit/OnRun` 插件入口 +
   `OHOS::DelayedSingleton<RpcServer>`。这就是绕过"屏幕采集需系统权限"的正门
   （普通应用无法使用，但 uitest 扩展机制可以）。
4. **传输**：设备端 gRPC 服务（TCP 5001 或 abstract unix socket `screen_record_grpc_socket`），
   PC 侧 `hdc fport tcp:<local> tcp:5001` 转发后 gRPC 连接
   （grpc_max_receive_message_length=10MB）。
5. **协议（proto 已完整拿到）**：
   ```
   service ScrcpyService {
     rpc onStart(Empty) returns (stream ReplyMessage);   // 启动并持续收视频流
     rpc onEnd(Empty) returns (ReplyEndMessage);          // 停止，result=帧数
     rpc onRequestIDRFrame(Empty) returns (...);          // 按需请求关键帧（实时投屏铁证）
   }
   ReplyMessage { string data; int32 reply_type; ParamValue payload; }
   ```
6. **产物/渲染**：录屏模式设备端并行写 `/data/local/tmp/mytest.mp4`（stop 后 pull）；
   实时投屏 = 持续消费 `onStart` 流（data 内 H.264 帧）+ 按需 IDR。
   帧数 < MIN_FRAME_COUNT 时客户端回退截图。
7. **门槛**：设备 uitest ≥ 4.1.4.6（本机手机实测 6.0.2.3 ✓，系统 OpenHarmony-6.1.1.120）。

### 为什么流畅（与路线2 的本质区别）

设备端**系统级采集（RenderService 层）+ 硬件编码 H.264**，传输的是压缩视频流，
30-60fps、只传变化；而 snapshot_display 单帧截图 ~0.6s/帧、全量 JPEG，差 1-2 个数量级。
（旁证：官方论坛有帖子反馈 DevEco Testing 投屏会持续触发 `on('screenshot')` 监听。）

### 复用评估

- **技术上全部可复现**：so（本机 4 份）+ proto（scrcpy_pb2 可直接用）+ 客户端逻辑
  （record_agent.py/rpc_manager.py 可照抄）。实现路径：新增 `scrcpy_server.py`，
  在现有 GUI 里加"流畅模式"，投屏消费 gRPC 流，录制直接用设备端 mp4（免 PC 合成）。
- **合规红线**：so 是华为版权二进制——**不能打包进我们的发布物**。可行做法：
  运行时从本机 DevEco Testing 安装目录自动定位（`/Applications/DevEco_Testing_for_App.app`），
  未安装则提示用户装（类比 scrcpy 依赖 adb 的关系）。
- 版本匹配：4 份 so 与系统的映射逻辑在 `check_uitest_version`/`_compare_software_version`，
  实施时先真机试 server4（最新最小 264KB），不行再降级。

### 与现有工具的关系

M1-M4 的截图管线保留为兜底（无 DevEco Testing / 手表等 so 不可用场景）；
流畅模式作为 M5 主升级：投屏帧率 1-2fps → 视频级，录屏免 ffmpeg 合成。

## 10. 里程碑 M5：流畅模式（H.264 视频流）✅ 完成 2026-09-14

> 调研见 §9.5。实测打通：HarmonyOS 6.1.1 / uitest 6.0.2.3 / screencopy_v2_1.3.so。

### 实现全链路（全部真机验证）
- **so 来源**：HoKit v1.8.7 发行包内加密 so（AES-256-CBC，key 内嵌其 JS，已提取）；
  解密后 `vendor/so/` 以加密形态入库（合规：不明文分发），运行时解密到 ~/.cache。
  `watchscrcpy/scrcpy_server.py` 另支持从本机 HoKit 安装自动提取。
- **设备端**：推 so → `uitest start-daemon singleness --extension-name scrcpy_server.so
  -scale 2 -frameRate 30 -bitRate 10M -p 5001 -screenId 0 -encodeType 0
  -iFrameInterval 2000 -repeatInterval 33`（参数照抄 HoKit 实测）→ abstract socket
  `scrcpy_grpc_socket` → `hdc fport` 转发（端口自适应，hdc server 会积累残留）。
- **协议修正**（较 DevEco 版）：视频帧在 `payload["data"].val_bytes`；flags: 8=SPS/PPS
  2=IDR 0=P；onEnd 返回 Empty、onRequestIDRFrame 返回 ReplyEndMessage。
- **实测约束**：① onStart 一个 daemon 只能消费一次（中断不得重连，须重启 daemon）；
  ② daemon singleness 单例（启动前 pkill）；③ so 版本须匹配系统（v2_1.4 因系统
  libprotobuf 缺符号不可用于 6.1.1，v2_1.3 验证可用）；④ 屏幕静止只推变化帧
  （IDR 间隔 2s），动画场景 36-37fps 实测。
- **PC 端**：`stream.py` H264Decoder（PyAV 软解+丢旧帧策略）与 StreamRecorder
  （裸流直写零 CPU + 停止时 ffmpeg -c copy 秒级封装，录制预填 SPS/PPS+IDR 保证
  裸流独立可解码，-r 30 修正时长）。
- **GUI**：mode=stream 默认；失败（无 so/版本不配/端口问题）自动回落 pull 截图模式
  并提示；掉线/流中断回未连接态；录制/截屏/剪贴板全功能在流模式下复用。
- **验证**：模块层 224 帧/6s=37fps、H.264 Annex-B 起始码+SPS 验证；GUI 离屏
  36fps 渲染、录制 mp4（ffprobe h264 608x1344）、截屏、回落路径（fport 残留时
  自动降级）实测；冻结态（DMG 包）stream 录制闭环实测。

---

## 11. 穿戴设备（手表）真机全链路验证 —— 2026-09-23

设备：**HUAWEI 手表 NIZ-AL00**，`const.product.devicetype=wearable`，
HarmonyOS NEXT **7.0.0.109(SP8C00E100R1P102log)** / API 26 / HongMeng Kernel 1.13.0，
uitest **7.0.0.1**，屏幕 **466×466 圆屏**，序列号 `7KLB****0444`。

### 11.1 probe 清单 —— 7/7 通过 ✅

| 项 | 结果 |
|---|---|
| 连通性 | ✓ `7KLB****0444` |
| 设备类型 | ✓ **wearable** |
| 分辨率 | ✓ 466×466（方形帧，圆屏需遮罩） |
| snapshot 单帧 | ✓ 21021 B，**1.44 s**（手机 0.65 s，约慢 1 倍） |
| power-shell 唤醒 | ✓ 命令可用 |
| sh 脚本能力 | ✓ 算术扩展支持 → caploop 可行 |
| agent 推流通道 | ✓ `agent.so v1.2.2`（uitest 7.0.0.1 / arm64-v8a）→ 见第 12 节 |

### 11.2 流式（H.264）在手表上**不可用** ❌ —— 根因已定位到设备侧

链路各环节其实**都成功**，失败点只在「虚拟屏不产出帧」：

- so 加载：`screencopy_v2_1.3.so` 解密、推送、`uitest start-daemon singleness` 全部成功，
  so 自打印 `Welcome to xdevice scrcpy so!` / `version: 6.6-20260418`；
- gRPC：abstract socket `scrcpy_grpc_socket` 正常创建、`hdc fport` 转发 `OK`；
- 采集初始化：`CreateVirtualScreen: create success. ScreenId: 1003, rsId: 4294967295`
  → `SetVirtualScreenSurface: success` → `video encoder was successfully started`
  （`screen 466×466` → `video 234×234 @scale2`）；
- **但编码器全程零帧**：`VENC_DRV_EncStatics chan 0 input cnt: 0, output cnt: 0`，
  因此 `onStart` 流永不推送，PC 侧 `DEADLINE_EXCEEDED`（有连接、无帧）。
- 试过 `-repeatInterval 33` 去掉、`-scale 1`、`bitRate 4M`、`frameRate 30` 四种参数组合，
  均无帧；另见编码器参数被拒日志 `InputFrameRate:(120) out of range [1,60]` +
  `SetParamVideoAvc: Parameter of AVC not support`（so 把 repeatInterval 映射成 120fps）。
- 旁证：daemon 未产出任何 mp4（`saveFrame`/mpr 均空）；`snapshot_display` 同屏幕却正常出图，
  排除「息屏/黑屏」因素。

**结论**：`gui.py` 的 stream→agent→pull 自动回落是该设备的**正确主路径**，不是异常降级 ——
手表上 stream 这一档必然失败，但 **agent 档可用且能跑到 30 fps**（第 12 节），
不需要落到 0.6 fps 的 pull 档。

### 11.3 连拍链路（实际可用路径）实测 ✅

| 模式 | 帧率 | 单帧耗时 | 黑帧 | 错误帧 | 产物 |
|---|---|---|---|---|---|
| pull（PC 逐帧驱动） | 13 帧/20s = **0.61 fps** | 1.64 s | 0 | 0 | 平均 24.9 KB |
| loop（设备端 caploop.sh） | 14 帧/20s = **0.69 fps** | — | 0 | 0 | 平均 24.8 KB |

- loop 模式真机可行：脚本经 `hdc file send` + `nohup sh` 后台跑，停止后设备进程
  **正确退出**（`pidof` 空、seq 停在 14）。
- 录制闭环：15 s 采集 9 帧 → ffconcat VFR 合成 → **H.264 932×932 mp4**（466×466×2），
  `nb_frames=10`，`duration=13.52 s`，中间帧目录自动清理。
- GUI 端到端（offscreen）：识别 wearable → **自动开启圆形遮罩** → 回落 pull →
  渲染 9 帧 466×466，状态栏与掉线回退正常。
- 截图：`--shot` 落盘 24923 B JPEG；`_round_mask` 四角 alpha=0、中心不透明，圆遮罩正确。
- 帧内容判别：灰度范围 0–255、非黑占比 62.8%、中心 226 / 四角 0 → 圆屏表盘内容真实。

### 11.4 验证中发现并修复的 bug —— 资源解析（开发态）

`watchscrcpy/resources.py`：

1. `resource_path("bin" / Path(...))`、`resource_path("data" / "caploop.sh")` 用 `/`
   拼 **str**，Python 3.9+ 直接 `TypeError`（`--mode loop` 必崩）；
2. 开发态 `_candidate_roots()` 只收录仓库根，导致内置资源找不到：`find_hdc`/`find_ffmpeg`
   静默落到本机 SDK，`find_caploop_script` 抛异常。

修复：`/` 改为 `os.path.join`；开发态候选根补 `root/"vendor"` 与 `root`。
修后 `find_hdc→vendor/bin/hdc`、`find_ffmpeg→vendor/bin/ffmpeg`、
`find_caploop_script→vendor/data/caploop.sh` 全部命中，内置 hdc 在本手表上
`list targets` 与 `shell` 均正常。打包态（frozen）行为不变。

### 11.5 遗留

- 流式若要上手表，需解决虚拟屏零帧（当前无三方可行手段；`Ohos` 侧需系统签名/多屏支持）。
- `libexternal_hdc.dylib` 在 macOS 上因 Team ID 签名不符无法 dlopen，hdc 打该 dylib 的
  扩展符号时打印 `[F]` 噪音，但**不影响** `list/shell/file/fport`（已实测，与 build.sh
  刻意排除该 dylib 的决策一致）。
- 手表帧率上限 ≈ 0.7 fps，由 `snapshot_display` 单帧 1.44 s 决定，非传输瓶颈。
  —— **2026-09-23 已由第 12 节推翻**：agent 档实测 30 fps。

## 12. 手表投屏第三通道：uitest `agent.so` 变化触发推流 ✅ 完成 2026-09-23

> 完整协议逆向记录见 [`research/agent.so协议逆向.md`](research/agent.so协议逆向.md)。
> 实现见 `watchscrcpy/agent.py`（`AgentCapture` / `AgentClient` / `AgentProbe`）。

### 12.1 一句话结论

`uitest start-daemon` 的 extension 机制可以把华为自带的 `uitest_agent_v1.2.2.so`
装进 `uitest` 进程，走 DMS 的截图监听通道**由设备端主动推 JPEG**。
手表实测**连续滚动时 30.8 fps**（pull 档 0.61 fps 的 50 倍；低变化画面 1.3~14 fps，
见 12.3），启停干净、录制裁剪闭环通过。
`--mode agent` 已接入 CLI/GUI，并成为 `auto` 回落链的第二档（stream → **agent** → pull）。

### 12.2 线协议要点（踩坑最久的一条：sid 决定应答分帧）

```
HEAD(28B "_uitestkit_rpc_message_head_") | sessionId u32be | len u32be | payload | TAIL(28B)
```

| 请求 sid | 应答形态 |
|---|---|
| `<= 0xFFFF` | **裸 `JSON\n`**（无分帧）——DevEco 遗留路径 |
| `> 0xFFFF` | **完整分帧** ← hdckit 走这条，本实现固定用 32 位随机 sid |

推流帧复用同一种帧格式，其 `sessionId` 等于 `startCaptureScreen` 那一笔请求的 sid。
若用小于 64K 的 sid，推流帧没有长度前缀，只能靠 SOI/EOI 硬切，粘包即崩。

`scale` 必须**严格 < 1.0**（`1.0` 返回 `{"result":null,"exception":""}` 且不报错，
正是 hdckit 里 `if (scale >= 1) delete options.scale` 的由来）。

### 12.3 实测（HUAWEI watch NIZ-AL00，466×466 圆屏）

> ⚠ agent 档的帧率 **等于画面实际变化率**，不是固定帧率。下表 `heartbeat=0`
> （只统计真实变化帧）按画面内容分档实测，同一台表同一份代码可差 20 倍以上 ——
> 引用数字时必须带上画面条件。

| 画面内容 | agent 实测帧率 |
|---|---|
| 静态页面（无任何变化） | **0.00 fps**（设备端根本不推帧） |
| 表盘 / 自带动画页面 | ~1.3 fps（设备端自己就在推） |
| 垂直上滑（列表滚动） | 13.9 fps |
| 水平滑动 | 9.2 fps |
| 单击 | 1.4 fps |
| 连续滚动 / 动画 | **30.79 fps**（472 帧 / 15.3 s，本仓库测到的峰值） |

| 其它指标 | pull 档 | **agent 档** |
|---|---|---|
| 单帧 | 24.9 KB @466² | **8.2 KB @233²**（scale 0.5），min 4.1 KB / max 12.5 KB |
| 首帧延迟 | 1.6 s | 9~22 s（推 so + 起 daemon + 握手 + 首帧兜底） |
| 黑帧 / 错误帧 | 0 / 0 | 0 / 0 |
| 帧尺寸一致性 | 466×466 | 全部 = 233×233 |
| 停止耗时 | — | 0.00 s，线程正常 join |

- **静止画面 0 帧**（严格变化触发）→ 客户端 `heartbeat=1.0` 重发上一帧，把下限抬到
  ~1 fps 并保证录制时间轴连续（实测静止时 0.92 fps）。
- 首帧兜底：开流 1.5 s 无帧则用一次 `snapshot_display`，并**缩放到推流尺寸**后送出
  （466² 与 233² 混帧会让 ffconcat 合成失败）。
- scale ↔ 分辨率：0.99→461²、0.9→419²、0.5→233²、0.25→117²（均 `{"result":true}`）。

### 12.4 GUI / 录制端到端（offscreen，mode=agent）✅

```
effective_mode=agent   round=True（wearable 自动圆遮罩）   cap=AgentCapture scale=0.5
① 高变化画面：渲染 426 帧 / 15.0 s = 28.37 fps
   产物 gui_agent_rec.mp4 744000 B → ffprobe: h264 232×232, nb_frames=437, duration=24.80s
② 低变化画面（同一脚本、同一代码，仅画面内容不同）：27 帧 / 15.0 s = 1.80 fps
   产物 gui_agent_rec.mp4 36397 B  → ffprobe: h264 232×232, nb_frames=30,  duration=15.36s
清理：pidof uitest 空、/data/local/tmp/agent.so 已删
```

`auto` 回归：`effective_mode=agent`、`pull` 档独立可用（首帧 2.2 s，~0.50 fps）。
会话健壮性：**连续静止 112 s 不掉线**；`power-shell suspend`（息屏）也不会中断推流会话，
唤醒后继续出帧（20 s 内 31 帧）。

### 12.5 接入 GUI 时发现并修复的 3 个真 bug

1. **失败回落后残留的 `H264Stream` 会杀掉 agent 的 daemon**（最隐蔽的一个）。
   `H264Stream._teardown()` 会 `pkill -9 -f 'uitest.*start-daemon'`，而 agent 档用的正是
   同一个 `singleness` daemon。`auto` 链里 stream 档失败后对象仍挂在窗口上，
   关窗时 `stop()` 收尾它就顺手把 agent 正在用的 daemon 杀了 ——
   现象是「点关闭后 0.3 s 报 agent 推流中断 / 设备连接已断开」。
   修法：回落时立刻 `_teardown_stream()`；并把 `stop()` / `_enter_disconnected()` 的顺序
   改成**先停帧采集、后收 stream**。
2. **wearable 直接跳过 stream 档**：手表虚拟屏零帧（11.2），这一档必然失败却要耗
   10~15 s 才超时；`_mode_chain()` 见到 `wearable` 就移除，启动路径直接走 agent。
3. `_on_status(text, err)` 少传参数（`TypeError`）：异常从 `_start_capture` 抛到 Qt 槽，
   顺手**阻断了 `_connect` 里后续的自动录制**；已补默认值 `err=False`。

### 12.6 新增/改动文件

| 文件 | 说明 |
|---|---|
| `watchscrcpy/agent.py` | **新增** 协议实现：`AgentClient`（分帧/握手/推流）、`AgentProbe`（版本探测与 so 查找）、`AgentCapture`（`BaseCapture` 子类：推流 + 首帧兜底 + 心跳 + 建链失败换端口重试） |
| `watchscrcpy/gui.py` | 回落链 `_MODE_CHAINS` / `_mode_chain`（wearable 跳过 stream）、`_start_agent`、`_teardown_stream`、收尾顺序修正、`_on_status(err=)` 默认值 |
| `wscrcpy.py` | `--mode auto\|stream\|agent\|pull\|loop`、`--agent-scale`、`--agent-so`；probe 增加第 7 项 |
| `research/agent.so协议逆向.md` | 逆向全过程记录（证据、失败路径、复现脚本） |

### 12.7 设备侧准备与三个设备/工具链坑

```bash
hdc shell param set persist.ace.testmode.enabled 1
hdc file send uitest_agent_v1.2.2.so /data/local/tmp/agent.so && hdc shell chmod 755 …
hdc shell "pkill -9 -f 'uitest.*start-daemon'"
hdc shell uitest start-daemon singleness          # 不带 --extension-name
hdc fport tcp:29400 localabstract:uitest_socket
```

1. **只有 abstract socket**：unix-socket 模式下 daemon 不监听 TCP 8012
   （hdckit 写死 8012 是面向旧设备），必须 `fport … localabstract:uitest_socket`。
2. **`fport rm` 在本套 hdc/手表上恒失败**（`[Fail]…ruler is not exist`，rc=0），
   残留转发只能 `hdc kill && hdc start` 清。但它指向的是 `@uitest_socket`，
   daemon 重建同名 socket 后**这条转发依然可用** → 实现里只要 `fport ls` 里已有就直接
   复用，省掉必然失败的 add，也不再累积新残留；只有 add 真失败（端口被占）才换端口
   （实测 29400 被占时自动轮到 29558）。若复用到的残留已失效，`run()` 会换端口重试一次。
3. `uitest start-daemon` **偶发**不建 socket：整段序列重试 3 次，失败时把
   `start-daemon` 输出 + `pidof uitest` 一起写进异常。

### 12.8 遗留 / 限制

- **`agent.so` 不可随包分发**（华为版权二进制）：运行时从本机 DevEco Testing 安装目录、
  hdckit 的 `node_modules` 或 `WSCRCPY_AGENT_SO` / `vendor/so` 提取；
  未找到时 `auto` 链会自动跳过 agent 档。
- 静止画面依赖客户端心跳，设备端无「固定间隔推流」选项（`options` 只解析 `scale`/`displayId`）。
- 仅在 arm64 手表 + uitest 7.0.0.1 上验证；x86_64 与旧 uitest（1.1.x so）未跑通实机。
- `Captures / captureScreen`、`screenshot` 为未知名，调用会**打挂连接**，勿试。

## 13. 打包与分发验收 —— 2026-09-24

### 13.1 产物

| 产物 | 大小 | 校验 |
|---|---|---|
| `dist/Wscrcpy.app` | 237 MB | `codesign --verify` = valid on disk / satisfies its DR |
| `dist/Wscrcpy-macOS-app.zip` | 91.8 MB | 用 `ditto -c -k --sequesterRsrc --keepParent` 打包，解压后签名仍有效 |
| `Wscrcpy-macOS.dmg` | — | **本环境未生成**，原因见 13.4 |

### 13.2 打包态真机验证 ✅

1. **资源解析（frozen）** —— 新增 `--selfcheck`（不需要设备）：

```
1. 运行形态          打包 frozen
2. 资源根            …/Wscrcpy.app/Contents/Frameworks
4. hdc              ✓  [内置] …/Contents/Frameworks/bin/hdc
5. ffmpeg           ✓  [内置] …/Contents/Frameworks/bin/ffmpeg
6. caploop.sh       ✓  [内置] …/Contents/Frameworks/data/caploop.sh
7. agent.so          ✓  [本机] /Applications/DevEco_Testing_for_App.app/…/uitest_agent_v1.2.2.so
9. 内置 vendor/so     ['screencopy_v2_1.2.so', 'screencopy_v2_1.3.so']
```

2. **打包版 `--probe` 真机 7/7 通过**（含第 7 项 `agent.so v1.2.2`）。
3. **打包版 GUI（offscreen）+ agent + 录制**：自动跳过 stream 档 →
   `agent 模式: agent.so v1.2.2（uitest 7.0.0.1 / arm64-v8a）` → 推流中；
   采到 **26 帧，全部 233×233**；用**包内 ffmpeg** 合成 → `h264 232×232, 27 帧, 2.64 s`。
   （合成用的是 bundle 里的 `Contents/Frameworks/bin/ffmpeg`，证明内置二进制可用。）

### 13.3 验收过程中修掉的问题

1. **写不了日志文件就整个启动失败**（真实脆弱点）。`resources.setup_logging()` 直接
   `RotatingFileHandler(~/Library/Logs/wscrcpy.log)`，该路径不可写时抛 `PermissionError`；
   而它发生在 `sys.excepthook` 安装**之前**，双击启动的表现是「什么都不发生」。
   改为三级降级：首选路径 → 系统临时目录 → 仅 stderr，返回实际路径（stderr 时为空串）。
2. **`build.sh` 的 DMG 步骤会被上次的残留挂载卡住**：上次构建的 DMG 在 Finder 里开过、
   没推出时，`/Volumes/Wscrcpy*` 占着卷名，`hdiutil create` 报「目录非空」。
   已加守卫：只卸载**指向本仓库 DMG** 的挂载点（按 `hdiutil info` 的 image-path 匹配）。
3. **新增 `--selfcheck`**：不连设备即可确认打包后 hdc/ffmpeg/agent.so 的解析结果，
   专门用于排查「装完提示找不到 hdc / agent.so」这类分发问题。

### 13.4 已知环境约束

- `hdiutil create` 需要写 `/dev/rdisk*` 才能格式化新卷；在受限/沙箱环境里会失败，
  且错误消息被错映射成 `create failed - 目录非空`，真因要从 `-verbose` 里看：
  `newfs_apfs: /dev/rdisk19s1: Operation not permitted`。普通终端执行 `./build.sh` 无此问题。
- **`agent.so` 仍未内置**（版权），打包产物在装有 DevEco Testing 的机器上自动命中，
  在没装的机器上 `auto` 链会跳过 agent 档、退到 pull。

## 14. 「连不上 / 一直 loading」根因定位与修复 —— 2026-09-24

现场报障：**设备已插着，但程序查不到设备，界面一直卡在 loading**。

### 14.1 定位过程与关键证据

1. 先确认不是设备/工具链问题：`hdc list targets` 能看到 `7KLB****0444`，
   `param get const.product.devicetype` = `wearable`，`uitest --version` 1.0s 返回，
   `cat /proc/net/unix` 0.2s 返回 —— 底层链路当时是通的，问题在客户端。
2. 翻 `~/Library/Logs/wscrcpy.log`，最后一轮是这样收尾的：

   ```
   18:33:59 mirror start: mode=auto
   18:34:00 status: 未发现设备
   18:34:21 wearable 设备跳过 stream 档（虚拟屏零帧）      ← 之后什么都没有了
   ```

   `connected:` 与 `agent 模式:` 两行**都缺**。而代码里 `logging.info("connected: ...")`
   写在 `_start_capture()` **之后**，所以卡点必然在 `_start_capture()` 内部。
3. 用慢 hdc 替身复现（`list targets` 睡 11s，超过当时的 10s 超时）：

   ```
   [t=10s] status='正在扫描设备…' btn='扫描中…' enabled=False visible=True scanning=True
   Exception in thread Thread-1: subprocess.TimeoutExpired: ... timed out after 10 seconds
   [t=40s] status='正在扫描设备…' btn='扫描中…' enabled=False visible=True scanning=True
   ```

   与用户描述完全一致：**永久 loading，且刷新按钮是 disabled 的，点不动**。

### 14.2 根因（三条，全在客户端）

| # | 位置 | 问题 |
|---|------|------|
| 1 | `Hdc.list_devices` | 只捕获 `FileNotFoundError`；`subprocess.TimeoutExpired` 逃出去，而调用方 `refresh().work()` 只捕获 `HdcError` → **扫描线程未捕获异常直接死掉**，`_scanning` 永远为 `True`，按钮永久「扫描中…」禁用 |
| 2 | `Hdc._run` / `list_devices` | `subprocess.run(timeout=)` 在 hdc 上**并不保险**：hdc 首次调用会 fork 出常驻 server 并继承 stdout/stderr 管道；超时后 `run()` kill 掉客户端**又** `communicate()` 等管道 EOF —— 该管道被 server 一直持有，永不 EOF，于是「带 timeout」的调用可以永久挂住 |
| 3 | `MirrorWindow._enter_connected` | UI 线程上做 hdc I/O：`device_type()`（2 次 shell）+ `_start_capture()` → `agent_supported()`（2 次 shell），最坏几十秒；一旦卡住整个窗口冻结，而刷新按钮此刻已被 `_set_connected_ui(True)` 隐藏 → 只能强杀进程 |

补充一个连带 bug：`_enter_connected` 会直接建新采集，**不先收旧的**。重连/重试时新旧两个
`AgentCapture` 抢同一个 fport/端口，表现成「刚连上就报设备连接已断开」。修复前实测能稳定复现。

### 14.3 修复

- **`_spawn()` 统一子进程出口**：新开进程组（`start_new_session`）+ 超时 `killpg`
  连整组一起杀 + 收尾读取只再等 3s，`_run()` 与 `list_devices()` 都改走它 —— 再也不会有
  「说了超时却永久挂着」的调用。
- **`list_devices` 异常全收口**：`TimeoutExpired` → `HdcError`（带可操作提示），
  超时 10s → **20s 且重试 1 次**（首次调用常要拉起 hdc server，比后续慢得多），
  并支持 `on_progress` 回调把「正在重试」实时写进状态栏；非零退出码不再被当成空列表。
- **扫描线程兜底**：`work()` 改为 `except Exception` + `finally: emit(payload)` ——
  无论成败都必须回报，否则 UI 永远等不到 `scan_done`。
- **阻塞探测搬出 UI 线程**：`device_type()` 与 `agent_supported()` 挪到扫描线程一次探好，
  经 payload 回传（`_agent_probe` 缓存）；`_start_agent()` 只读缓存。
- **启动看门狗**：连上后 45s 仍 0 帧，就提示「启动超时仍未收到画面」并把
  「⟳ 刷新」放回来 —— 保证任何情况下都有一条退路。
- **`_teardown_capture()`**：重连前先停/join 旧采集线程 + 收 stream 残留，消除双采集互抢。
- 日志顺序调整：`connected: <sn> dtype=<...>` 现在打在 `_start_capture()` **之前**，
  下次再卡住至少知道卡在哪一步。

### 14.4 修复验证（全部实跑）

| 场景 | 结果 |
|------|------|
| 慢 hdc 25s（> 20s 超时） | t=25s 状态栏显示「hdc 无响应（20s 超时），正在重试…」；t=45s 显示可操作提示、按钮恢复「⟳ 刷新」可点；**不再永久 loading** |
| 随后换回真 hdc 点刷新 | 正常连上 → `agent` 档 → `实时画面正常`，持续出帧 63 帧 |
| 扫描线程抛非 `HdcError`（`RuntimeError`） | 状态栏「扫描异常：RuntimeError: …」，按钮恢复可用，异常带栈写入日志 |
| 连上后始终 0 帧（看门狗） | t=5s 仍隐按钮（投屏态）；t=48s 提示「启动超时仍未收到画面：可点「⟳ 刷新」重试…」且按钮可见可点 |
| 重连（原先「刚连上就报断开」） | 不再复现，重连后画面正常持续输出 |

### 14.5 环境侧观察（需要用的人自己确认）

定位期间手表**一度整体从 USB 总线消失**：`ioreg -p IOUSB` 只剩 hub 与网卡，
两个 hdc 版本（3.2.0c / 1.2.0a）的 `list targets` 都是 `[Empty]`。
这与日志里 18:29~18:34 的间歇性 `未发现设备`（三次启动都是 0.5s 内返回空）吻合，
说明**设备侧连接本身在抖动**（接触不良 / 手表休眠 / 调试授权未确认），不是程序造成的。
遇到这种情况：重插 USB、点亮手表、确认屏幕上的「是否允许调试」对话框，
必要时在终端 `hdc kill && hdc start` 清掉服务端残留。

### 14.6 追加定位（同日第二次报障）：**agent.so 查找把整个磁盘走了一遍** ⚠ 最深的坑

修完 14.1~14.4 后现场仍是「设备已连接但找不到」。用 `/usr/bin/sample` 抓运行中的
进程栈，发现 Python 线程**不是阻塞，而是在热跑 `readdir`** —— 它在遍历目录。

根因在 `agent.py` 的 so 查找：`_HDCKIT_SO_GLOBS` 里有一条**相对**模式

```python
"**/node_modules/hdckit/uitestkit_sdk/*.so"        # 交给 glob.glob(..., recursive=True)
```

`glob` 的**相对模式以进程 CWD 为起点**展开。而 **Finder 双击启动的 .app，其 CWD 是 `/`**
（`lsof -p <pid>` 实测 `cwd DIR /`），于是每次查 agent.so 都变成**全盘递归遍历**：

| 进程 CWD | `_find_so_files()` 耗时 |
|---|---|
| `/`（双击启动的真实形态） | **70s 仍未结束**（被掐断） |
| 仓库目录（脚本启动，CWD 恰好是仓库） | 3.7s |

这也解释了为什么以前"很快"：**agent 通道是 9/24 才加的**，更早的版本根本不会调用这段
查找。而我在 14.3 里把 `agent_supported()`（→`find_agent_so`）挪到了扫描线程，
于是"找不到设备"就成了扫描的直接阻塞项 —— 现象变成「一直 loading」。

**修法（`agent.py`）**：

1. **全部绝对锚定**：删掉两条相对 `**` 模式，改为绝对路径的定向模式
   （`<root>/DevEco*Testing*.app/Contents/Python/lib/python3.12/site-packages/
   devicetest/res/prototype/native/uitest_agent_v*.so` 等，每级只用 `*` 不用 `**`）。
2. **三级查找**：已知目录（零遍历）→ 定向模式 → **有界遍历**（深度 10 / 600 个目录 /
   0.75s 三重预算，不跟随符号链接，`node_modules` 只下一层找 `hdckit`）。
3. **结果缓存**：命中长期缓存，空结果缓存 300s —— 扫描每次都会调这里，不能每次都走目录。
4. 预算**在层间和单个目录内部都要检查**：第一版只在每层开头判一次，撞上一个超大目录
   就超预算 4~15 倍（实测 15.4s / 4.3s）；改成就地每 128 条目判一次后为 **0.14s**。
5. **同类坑一并清掉**：`--probe` 原来把单帧写成相对路径 `probe_frame.jpeg` —— CWD=`/`
   时直接 `PermissionError: 'probe_frame.jpeg'`，把整条 probe 打断（4~7 项全不跑）。
   已改为绝对路径（优先桌面，不可写退系统临时目录），并在输出里打印实际落盘位置。
   至此全仓库不再有依赖 CWD 的路径（`glob` 用法全部绝对锚定）。

**验证**：

| 项 | 结果 |
|---|---|
| `_find_so_files()` @ CWD=/ | **70s+ → 0.002s**，仍返回同样的 4 个 so |
| `find_agent_so('7.0.0.1','arm64-v8a')` @ CWD=/ | 0.000s → `uitest_agent_v1.2.2.so` |
| 定向模式（架空已知目录后单独测） | 0.008s，独立找到全部 4 个 so |
| 有界遍历 | 0.14s / 0.02s，预算内 |
| 端到端（`cd /` 启动，即双击形态） | 4.2s 已进入 `agent 模式：启动中` → dtype=wearable → 持续出帧 20 帧 |

**教训（值得记住的一条）**：任何 `glob` / 相对路径都隐含"CWD 是什么"这个前提，
而 GUI 双击启动时 CWD 是 `/`。要么绝对锚定，要么把预算写死。

## 15. 「画面不清晰」根因与修复 —— 2026-09-24

报障：录制已正常，但投屏/录制的画面明显发糊。

先列嫌疑，再逐条用真机/离屏渲染实测，**不靠猜**：

| # | 嫌疑 | 判定 | 证据 |
|---|---|---|---|
| 1 | agent 默认 `scale=0.5` → 推流只有 233×233 | ✅ **主因** | 真机实测 233×233 / 6.2 KB，而原生 `snapshot_display` 是 466×466 / 25.6 KB |
| 2 | Retina 上先缩到逻辑像素、再由 Qt 放大到 2x 背板（两次重采样） | ✅ **次因** | 离屏 Qt 实测 `dpr=2`：老路径 Pixmap 638×638(dpr=1) → 合成器再放大 2.00x 到 1276 |
| 3 | 合成 mp4 时 libx264 二次有损编码 | ❌ 排除 | `-crf 23` 对源帧 **PSNR 46.5 dB**（`-crf 17` 51.6 dB）—— 46 dB 已在视觉无损区，不是糊的来源 |
| 4 | 手表屏幕本身只有 466×466 | ⚠️ 上限 | 原生截图实测 466×466；**任何**录制的上限就是这个数，放到 1440p 全屏必然显软 |

### 15.1 量化（真机原生截图 → 放大到 1276 物理像素，拉普拉斯方差 = 高频能量）

| 流程 | 高频能量 | 相对理想 |
|---|---|---|
| 理想：466 源一次放大 | 76.3 | 100% |
| **修复后**：462 源（≈原生）一次放大 | **41.1** | 54% |
| 修复前：233 源（scale 0.5）一次放大 | 9.3 | 12% |
| **修复前真实路径**：233 源 + 双重重采样 | **5.0** | **6.6%** |

即：修复前的高频细节只剩理想的 ~7%，修复后回到 ~54%（余下差值来自 Qt 平滑插值与 462/466 的采样差）。
老路径的两次重采样单独也会让高频能量掉一半（34.2 → 70.6，同一源同一目标尺寸）。

### 15.2 scale 提到 0.99 的代价（真机实测，HUAWEI NIZ-AL00）

| scale | 推流分辨率 | 单帧 | 带宽 | 帧率 |
|---|---|---|---|---|
| 0.5 | 233×233 | 6.2 KB | 27~67 KB/s | 4.12 fps（对照 1.68） |
| **0.99（新默认）** | **462×462** | **15.5~24.8 KB** | **129~340 KB/s** | **4.30 fps（对照 21.41）** |

帧率由**画面变化率**决定（变化触发），两组对照里 0.99 都不低于 0.5；带宽几百 KB/s 在
USB 上可忽略。**没有任何理由为了省流量把画质砍成 1/4 像素。**

### 15.3 修复

1. `agent_scale` 默认 `0.5 → 0.99`（`AgentCapture` / `AgentClient` / CLI / GUI 四处默认值统一）。
2. GUI 底部新增「**◐ 画质**」档位按钮：原生 0.99（462×462）/ 清晰 0.8 / 流畅 0.5，
   点击即切换并**按新档重启采集**；`pull` 档下按钮置灰（它本身就是原生截图，无需档位）。
3. HUD 增加**采集侧真实分辨率**（`FRM 0123 462×462`）：一眼区分"源分辨率低"还是"窗口放大"。
4. `VideoPanel.set_frame()` 改为按**物理像素**缩放再 `setDevicePixelRatio(dpr)`，
   绘制矩形用 `deviceIndependentSize()` 换算 —— 高清屏上只采样一次。

**顺带被"真机实跑"逼出来的三个 bug**（不实跑就发现不了，代码看着都对）：

5. **画质切换会把新采集打死**：`AgentCapture` 收尾时会 `pkill uitest.*start-daemon` 并删
   设备上的 `agent.so`。第一版切换在 UI 线程 `_teardown_capture()`（join 3s）后立刻起
   新采集 —— 旧线程没死透，它的 `pkill` 把**新 daemon** 一起杀了。实测现象：
   「切完画质 2 秒后报设备连接已断开」。修法：收旧采集放到**工作线程**并 `wait=20s`
   等它真退出，收干净后用信号回 UI 线程再起新采集；切换期间置灰按钮 + 置位防重入。
6. **看门狗定时器越攒越多**：`_arm_bringup_watchdog()` 原来每次都 `QTimer(self)` 新建一个，
   旧定时器被 parent 持有不会销毁 —— 切几次画质就攒下一串旧定时器，它们会在新采集刚启动、
   还没出首帧时跳「启动超时仍未收到画面」的**假警报**（实测日志里出现过）。
   修法：定时器只建一次，重复 arm 只是 `start(45000)` 重置计时；掉线时主动 `stop()`。
7. **设备版本读不到时会静默选错 so**：`find_agent_so()` 走
   `agent_so_version("") → "1.1.3"`（那是 uitest 5.x 之前老协议的默认值），然后**精确匹配**
   命中本机的 1.1.3 —— 在新设备上等于主动选错 so，且只留一条 INFO 日志。
   修法：版本字段读不全（`<3` 段）时不走精确匹配，直接取最新同架构 so 并 **WARNING 留痕**。
8. `agent.py` 增一行日志 `agent 推流尺寸 462x462（设备显示 466x466，scale=0.99）`：
   清晰度类问题的第一现场证据，省得再靠猜。

### 15.4 验证

| 项 | 结果 |
|---|---|
| 真机 agent @0.99 | 收帧 462×462，单帧 15.5~24.8 KB（与原生截图 25.6 KB 同量级） |
| 离屏 Qt `dpr=2` | 新路径 pixmap 1276×1276(dpr=2)，绘制矩形 638 逻辑 = 1276 物理 → **1:1，无二次放大** |
| 高频能量（同上表） | 5.0 → 41.1（8 倍） |
| **离屏跑真 GUI（真代码 + 真机）** | 初始 462×462 → 点一下 373×373 → 再点 233×233 → 再点回 462×462，**全程 status=实时画面正常，无掉线** |
| 看门狗假警报 | 切档时刻刻意压在旧定时器到点前 → 全程 **0 次**「启动超时」 |
| so 选择 | `7.0.0.1→1.2.2`、`6.0.2.1→1.1.10`、`5.1.1.2→1.1.5`、`5.0.0.1→1.1.3`、**未知→1.2.2** |
| 打包产物 | 见 15.5 |

### 15.5 打包产物验证（CWD=/，双击形态）

| 项 | 结果 |
|---|---|
| `dist/Wscrcpy.app` 从 `/` 启动 | 日志 `agent 推流尺寸 462x462（设备显示 466x466，scale=0.99）`，进入推流 |
| 打包版 `--probe` | 7/7 通过 |

**教训**：清晰度这种事必须**量**——"糊"的三个嫌疑里，编码器那条实测是 46 dB（无害），
真正吃像素的是默认参数和一次多余的重采样。另外，帧率与 scale 无关（变化触发模型），
"降分辨率换帧率"在这个通道上根本不成立，属于白丢画质。
**第二条教训**：新加的交互（切档重启采集）必须**真跑一遍完整状态机** ——
上面 5/6/7 三个 bug 都是"逐行看代码没问题、一跑就现形"的那类。

### 15.6 追加：推流尺寸是 `ceil(分辨率×scale)`，不是 `round`

核对"日志说 461、HUD 说 462"这个不一致时发现的：

| scale | 466×scale | 设备实测推流 | round | ceil | floor |
|---|---|---|---|---|---|
| 0.99 | 461.34 | **462** | 461 ✗ | 462 ✓ | 461 ✗ |
| 0.8  | 372.8  | **373** | 373 ✓ | 373 ✓ | 372 ✗ |
| 0.7  | 326.2  | （未取到帧） | 326 | 327 | 326 |
| 0.6  | 279.6  | **280** | 280 ✓ | 280 ✓ | 279 ✗ |
| 0.5  | 233.0  | **233** | 233 ✓ | 233 ✓ | 233 ✓ |

唯一能同时解释 0.99→462、0.6→280、0.5→233 的是 **ceil**（`int(x)+1` 被 0.5→233 否掉）。
客户端原先用 `round` 算 `_stream_size`，于是首帧兜底图会被缩成 461×461，而设备推流是
462×462 —— **一录进去就是混尺寸**。

- 修：`_stream_size` 改用 `math.ceil`（`agent.py`），画质提示里的 ≈ 尺寸同步改；
- 加固：`Recorder._normalize()` 以**首帧尺寸为基准**，遇到不一致的帧就地 LANCZOS 缩放
  （尺寸用 SOF 头读，不做整帧解码；只有真不一致才解码）。`ffconcat` 要求尺寸统一，
  一旦混进一张不同尺寸的（首帧兜底、切换采集档、设备端取整差异……）ffmpeg 会
  `Input link parameters do not match` 直接失败 —— 这个加固把整类问题一次消掉。
  单测：喂 `[461,462,462,373,462,462]` → 落盘全部 461×461，合成成功（mp4 460×460，
  之所以 460 是合成里 `trunc(iw*1/2)*2` 的偶数对齐）。
- 顺带把 `parse_jpeg_size` 从 `agent.py` 下移到 `capture.py`（两处都要用；`agent.parse_jpeg_size`
  保留为导入别名，旧调用不受影响）。
