# Wscrcpy —— HarmonyOS NEXT 手表/手机投屏 / 录屏 / 截图

双击即用的桌面应用：**macOS `Wscrcpy.app` / Windows `Wscrcpy.exe`**，深色控制台风格 GUI
（PySide6），窗口内按钮直接【录制】【截屏】。**目标机零配置**——Python/Qt/Pillow/hdc/ffmpeg
全部内置，不装 DevEco、不装 ffmpeg、不配 PATH。

技术路线：手表/手机端 `snapshot_display` 连拍 → USB(hdc) → PC 端拉帧渲染/录制。
设计文档见 [PLAN.md](PLAN.md)。

## 使用（最终用户）

1. 双击 `Wscrcpy.app`（macOS 首次右键 → 打开）或 `dist\Wscrcpy\Wscrcpy.exe`（Windows）——
   **无设备也能启动**，窗口会显示"未发现设备"占位提示；
2. 设备开启开发者模式 + USB 调试并授权，数据线连接电脑，点窗口里的 `⟳ 刷新`（或 Ctrl+R）
   自动连接并开始投屏；
3. 窗口内：`● 录制` 开始/停止（保存对话框默认桌面/图片目录）、`⧉ 截屏` 保存 PNG、
   `⏻ 退出`；快捷键 `R` / `S` / `Ctrl+Q` 同效；
4. 投屏中拔线/掉线会自动回到"未连接"状态，重连后点 `⟳ 刷新` 即可；
5. 录制完成自动合成 mp4 并清理中间帧，不在磁盘堆积垃圾数据。

帧率约 1–2 fps（受设备端 `snapshot_display` 耗时限制）；隐私页会显示黑帧标记，属预期。

## 从源码构建（开发者）

```bash
pip install -r requirements.txt   # PySide6 / Pillow / pyinstaller
./build.sh                        # macOS → dist/Wscrcpy.app（hdc/ffmpeg 取本机最新 SDK）
./build.ps1                       # Windows → dist/Wscrcpy/（自动下载 ffmpeg，hdc.exe 需 -HdcBin）
```

构建机需要：Python 3.10+、本机一份 hdc（DevEco Command Line Tools）、ffmpeg
（`brew install ffmpeg` / gyan.dev）。CI 见 `.github/workflows/build.yml`。

### 构建 Windows 版（无需 Windows 机器）

PyInstaller **不支持交叉编译**，exe 必须在 Windows 环境产出，两条路径：

- **GitHub Actions 云构建（推荐，无需 Windows 机器）**：
  1. 取一份 Windows 版 `hdc.exe`（华为开发者站下载 Windows 版 Command Line Tools 解压即得），
     放到 `vendor/bin/hdc.exe` 并提交（ffmpeg 由 CI 自动下载，无需提供）；
  2. `git init && git add -A && git commit && git push`（推到 GitHub，公开仓库免费）；
  3. 仓库 Actions 页 → **build** → Run workflow → 下载 `Wscrcpy-Windows` artifact，
   解压即用。
- **本地 Windows / 虚拟机**：拷贝项目 → `.\build.ps1 -HdcBin <hdc.exe路径>` → `dist\Wscrcpy\Wscrcpy.exe`。

Wine 跑 Windows Python 的交叉构建方案在 Apple Silicon 上为双层模拟、PySide6 体积巨大，
可行性极低，未采用。

## CLI 高级用法

```bash
python3 wscrcpy.py --probe            # 真机验证清单（连通/设备类型/单帧/唤醒/sh 能力）
python3 wscrcpy.py --record out.mp4   # 打开 GUI 并立即开始录制
python3 wscrcpy.py --shot out.jpeg    # 单帧截图后退出（纯 CLI）
python3 wscrcpy.py --mode loop        # 实验性: 设备端 caploop.sh 连拍提帧率
python3 wscrcpy.py --serial XXX       # 多设备时指定
```

## 项目结构

```
wscrcpy.py                 # CLI/GUI 入口
wscrcpy.spec               # PyInstaller 打包配置（双平台同源）
build.sh / build.ps1       # macOS / Windows 一键构建
scripts/caploop.sh         # 设备端后台连拍脚本（实验性）
watchscrcpy/
  ├── gui.py               # PySide6 深色控制台窗口
  ├── hdc.py               # hdc 封装：竞态安全截屏、命令降级、日志噪声清洗
  ├── capture.py           # 拉帧管线（pull / loop 双模式，投屏录制共享）
  ├── recorder.py          # 录制：帧落盘 + ffconcat VFR 合成（后台线程）
  └── resources.py         # 跨平台资源定位 / hdc·ffmpeg 查找链 / 日志目录
vendor/                    # 构建素材（hdc、ffmpeg、caploop.sh），build 脚本自动备齐
```

## 已知限制

- 帧率受截图机制限制（1–2 fps）；触控回注、音频未实现（见 PLAN.md M5+）。
- macOS 未签名应用首次运行需右键 → 打开；Windows 自签名/无签名会过 SmartScreen 提示。
- 手机已真机全链路验证（见 PLAN.md 第 6 节数据）；手表端三项目（snapshot 存在性、
  唤醒、圆屏遮罩效果）待手表真机确认。

## 关于 hdc 的版本耦合（重要）

程序内置了 hdc，运行时**不需要**安装 DevEco Studio；但 hdc 是与手机端 daemon 配对的
"驱动级"组件——**手机系统升级会抬高 hdc 最低协议版本**，封存在包内的 hdc 快照可能变旧。
典型症状：设备能列出但所有 shell 命令报
`E000001: The sdk hdc.exe version is too low`（[官方 HDC 文档](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hdc)）。

应对：本工具会自动检测 E000001 并明确提示；修复方式 = 用新版 hdc **重新构建**
（`./build.sh` 会自动选取本机最新的 SDK hdc，优先级：OpenHarmony Sdk → hmscore →
DevEco Studio 内置），开发态亦同序自动探测。也可用 `--hdc-path` 临时指定。
另外注意：部分旧版工具链目录含 `HdcExternal`（1.0.6 外部模式 server），会抢占 5037
端口且被新手机拒绝；本工具打包时刻意不携带它，排查端口冲突可先
`lsof -iTCP:5037` 查看 server 归属。
