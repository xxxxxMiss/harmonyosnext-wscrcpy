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

- hdc：`/Users/chenxianlong/Library/Huawei/Sdk/hmscore/3.1.0/toolchains/hdc`（Ver 3.2.0d；调研时 `hdc list targets` 为空）
- Python 3.12 + Pillow ✓；Tk 支持已补装（`brew install python-tk@3.12`）；ffmpeg 9.0.1 已装（`brew install ffmpeg`）
- ⚠ 实测 ffmpeg 9.0 起 **`-vsync` 选项已移除**，VFR 合成须用 `-fps_mode vfr`（recorder.py 已做新旧版本双兼容）
- 可复用竞态处理基建：`/Users/chenxianlong/workspace/ai-test/auto-shot/autoshot/hdc_driver.py`
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

**Step 2b — Windows 打包 📦 脚本就绪（未实测）**
- `build.ps1`（hdc.exe 定位/伴随 DLL 复制、gyan.dev 静态 ffmpeg 下载）+ CI workflow。
- 真实 Windows 构建与连机冒烟需 Windows 机器。

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

### 9.5 不做的事（本期）
- 触控回注（原 M3）、音频、自动更新、菜单栏常驻、Linux 包（需求出现再加，PyInstaller 本身支持）。
