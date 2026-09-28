# uitest agent.so 推流协议逆向（wearable 投屏第三通道）

> 目标：让手表投屏摆脱 `snapshot_display` 逐帧拉取的 ~0.6 fps，改用 uitest 官方
> extension 机制的**设备端变化触发 JPEG 推流**。
> 结论：**打通**，手表实测 **30.8 fps**（pull 模式 0.61 fps 的 50 倍）。
> 实现见 `watchscrcpy/agent.py`。

---

## 1. 为什么是 agent.so

`snapshot_display` 的瓶颈在设备端 DMS 截图 IPC，与分辨率无关（466→117 像素仍 ~1.1s），
逐帧拉取没有优化空间（见 `PLAN.md` 第 11 节）。

社区里能在**商用 HarmonyOS NEXT** 上做投屏的工具，走的都是同一条路：
`uitest start-daemon` 的 extension 加载机制 —— 借 shell 身份把华为自己的 so 装进
`uitest` 进程，绕开 `OH_AVScreenCapture` 对三方应用的 system_basic/restricted 权限要求。

| 工具 | 加载的 so | 采集机制 |
|---|---|---|
| ECHO / hdckit | `uitest_agent_v1.1.0.so`（hdckit 自带分发） | `Rosen::DisplayManager::GetScreenshotWithOption` + libjpeg |
| DevEco Testing | `uitest_agent_v{1.1.3,1.1.5,1.1.10,1.2.2}.so` | 同上（本仓库从其安装目录提取） |
| 官方投屏 | `libscrcpy_server*.so` | 虚拟屏 + 硬件 H.264（手表上编码器不出帧，见 PLAN 11.2） |

---

## 2. 证据来源

| 来源 | 路径 / 链接 | 价值 |
|---|---|---|
| hdckit 0.12.1 `dist/hdc/UiDriver.js` | [npm hdckit](https://www.npmjs.com/package/hdckit) | **完整线协议**（分帧、握手、开流、api 名） |
| DevEco Testing 客户端 | `/Applications/DevEco_Testing_for_App.app/…/devicetest/controllers/device.py`、`ohos/proxy/{proxy_base,ui_proxy}.py` | 传输/端口/版本选择规则、遗留「裸 JSON」路径 |
| agent.so 二进制 | `…/res/prototype/native/uitest_agent_v*.so` | 符号与字符串：api 名、`GetScreenshotWithOption`、日志 tag |
| 真机 hilog | tag `UiTestKit_Addon` / `UiTestKit_Base` | 设备端请求/回包/调度全流程日志 |

关键日志（手表 NIZ-AL00，uitest 7.0.0.1）：

```
I UiTestKit_Addon: Welcome to devicetest agent so!
I UiTestKit_Addon: UiTestExtension_OnInit done, uitestVersion=7.0.0.1, extensionVersion=1.2.2
I UiTestKit_Addon: create socket file
I UiTestKit_Addon: RpcServer service running!
I UiTestKit_Addon: Got connection from domain socket, Peer PID: 1503, Peer UID: 0, Peer GID: 0
I UiTestKit_Addon: Begin handle request, session=60834185216 data='{"module":"com.ohos.devicetest.hypiumApiHelper",...}'
I UiTestKit_Base:  [frontend_api_handler.cpp:(Call)] Begin to invoke api 'Driver.create', '[]'
I UiTestKit_Addon: Reply json request, session=60834185216
I UiTestKit_Addon: DispatchRequest finish
```

`NotifyCaptureStatusChanged: agent is null` 是 DMS 侧噪音，与功能无关。

---

## 3. 线协议

### 3.1 帧格式（双向同构）

```
+-------------------------------+ 28 B ASCII
| "_uitestkit_rpc_message_head_"|
+-------------------------------+
| sessionId   uint32 BE         |  4 B
+-------------------------------+
| payloadLen  uint32 BE         |  4 B
+-------------------------------+
| payload     payloadLen bytes  |      JSON（尾部带 \n）或裸 JPEG
+-------------------------------+
| "_uitestkit_rpc_message_tail_"| 28 B ASCII
+-------------------------------+
```

同一连接上，应答与**推流帧**都按此格式回传；推流帧的 `sessionId` 等于
`startCaptureScreen` 那一笔请求的 sid，据此过滤。

### 3.2 ⚠ 核心坑：sessionId 决定应答分帧方式

这是整个逆向里最花时间的一点 —— **同一个服务端存在两种应答编码，由 sid 大小切换**：

| 请求 sid | 应答形态 | 谁在用 |
|---|---|---|
| `<= 0xFFFF` | **裸 `JSON\n`**（无 HEAD/TAIL、无长度前缀） | DevEco `OSBase._recv` 读到 `\n` 为止；hdckit 之外的遗留客户端 |
| `> 0xFFFF` | **完整 HEAD+sid+len+payload+TAIL** | hdckit（sid 来自 32 位 `strHash`） |

实测对照（同一 daemon、同一连接参数，只改 sid）：

```
sid=1        framed=False  b'{"result":"Driver#0","pts":...}\n'
sid=42       framed=False  b'{"result":"Driver#0","pts":...}\n'
sid=65535    framed=False  b'{"result":"Driver#0","pts":...}\n'
sid=2^31     framed=True   b'_uitestkit_rpc_message_head_\x80\x00\x00\x00\x00\x00\x00*{"result":...
sid=random   framed=True   b'_uitestkit_rpc_message_head_\x12a\xf0E\x00\x00\x00*{"result":...
```

若用小 sid（落到裸路径），推流帧就没有长度前缀，只能靠 JPEG 的 SOI/EOI 硬切；
一旦某帧字节里出现截断或粘包就难以恢复。**实现里固定用 32 位随机 sid（高位恒 1）**，
走确定的分帧路径；同时保留裸路径兼容分支。

设备端也有对应痕迹：`SendReply success, dataLen: %zu, msgLen: %zu, sockFd: %d` 与
`SendReply naked success, dataLen: %zu, sockFd: %d` 两个函数。

### 3.3 报文

外层固定 `module = com.ohos.devicetest.hypiumApiHelper`：

```
{ "module": "com.ohos.devicetest.hypiumApiHelper",
  "method":  <见下表>,
  "params":  { ... },
  "request_id": "%Y%m%d%H%M%S%f" }
```

| method | params 形状 | 用途 |
|---|---|---|
| `callHypiumApi` | `{"api":"Driver.<x>","this":<driver>,"args":…,"message_type":"hypium"}` | Hypium 系 api；`Driver.create` 的 `args` 是 **list** |
| `Captures` | `{"api":"<capture>","args":…}` | 采集系 |
| `CtrlCmd` | `{"api":"getDisplaySize","args":{}}` | 屏幕信息 |
| `Gestures` | `{"api":"touchDown","args":{"x":..,"y":..}}` | 注入触摸（可当独立的“画面变化”源） |

### 3.4 实测可用的 api

| api | 结果 |
|---|---|
| `callHypiumApi / Driver.create`（args=[]） | ✅ `{"result":"Driver#0","pts":…}`，首次约 5s（等 AccessibilityUITestAbility） |
| `CtrlCmd / getDisplaySize` | ✅ `{"result":{"width":466,"height":466}}` |
| `Captures / captureLayout` | ✅ 返回完整 UI 树 JSON |
| `Captures / startCaptureScreen`（`{"options":{"scale":S}}`） | ✅ `{"result":true}` → 开始推流 |
| `Captures / stopCaptureScreen` | ✅ 停止 |
| `Captures / copyScreen` | ❌ `{"result":null,"exception":"Illegal api name: copyScreen"}`（v1.2.2 已移除，仅剩字符串与 `Auto stop copyScreen since session dead`） |
| `Captures / captureScreen`、`screenshot` | ❌ 无应答且**会把连接打挂**，慎用 |

### 3.5 scale 必须 < 1.0

```
scale 0.99 -> {"result":true}   461x461
scale 0.9  -> {"result":true}   419x419
scale 0.5  -> {"result":true}   233x233
scale 0.25 -> {"result":true}   117x117
scale 1.0  -> {"result":null,"exception":""}   无帧，且不给错误信息
```

hdckit 里那句 `if (options.scale >= 1 || options.scale <= 0) delete options.scale`
正是为了绕开这个坑。实现里把 scale 夹到 `[0.05, 0.99]`。

---

## 4. 设备端机制与「变化触发」

`agent.so` 内部（符号表证据）：

```
OHOS::Rosen::DisplayManager::GetScreenshotWithOption(CaptureOption*, DmErrorCode*)
OHOS::Rosen::DisplayManager::getDisplaySize() / getDisplayRotation()
OHOS::uitest::HandleCapture / CaptureHandleSessionDeathBroadcast
```

即走 DMS 的 **监听通道**（与 `snapshot_display` 同源，但由设备端主动推），
画面**无变化时不产生新帧**。用 `heartbeat=0`（只统计真实变化帧）分档实测：

| 画面内容 | 帧率 |
|---|---|
| 静态页面（毫无变化） | **0.00 fps** —— 即使每秒 `power-shell wakeup` 强制亮屏也无帧 |
| 表盘 / 自带动画页面 | ~1.3 fps（设备端自己就在推，全是 unique 帧） |
| 垂直上滑（列表滚动） | 13.9 fps |
| 水平滑动 | 9.2 fps |
| 单击 | 1.4 fps |
| 连续滚动 / 动画 | **30.79 fps**（测到的峰值） |

**帧率 = 画面实际变化率**，同一台表同一份代码可差 20 倍以上；引用时必须带画面条件。
客户端 `heartbeat=1.0` 把下限抬到 ~1 fps 并维持录制时间轴（实测静止时 0.92 fps）。

因此实现里做了两件事：

1. **首帧兜底**：开流后若 1.5s 无帧，用一次 `snapshot_display` 补一帧，并按
   `round(分辨率*scale)` 等比缩放后再送 —— 否则录制会因尺寸不一致而失败
   （ffconcat 要求所有帧同尺寸）。
2. **心跳**：静止超过 `heartbeat` 秒就重发上一帧的同一份字节。没有它，录制一段
   静止画面会得到空/超短视频，GUI 的 FPS 也永远是 0。

---

## 5. 设备侧准备序列

```bash
hdc shell param set persist.ace.testmode.enabled 1
hdc file send <agent.so> /data/local/tmp/agent.so
hdc shell chmod 755 /data/local/tmp/agent.so
hdc shell "pkill -9 -f 'uitest.*start-daemon'"
hdc shell uitest start-daemon singleness          # 不带 --extension-name：
                                                  # 默认加载 /data/local/tmp/agent.so
hdc fport tcp:29400 localabstract:uitest_socket
# 然后 TCP 连 127.0.0.1:29400 走上面的协议
```

### 版本选择（照抄 DevEco `_init_so_resource`）

| 条件 | agent.so 版本 |
|---|---|
| `arch == x86_64` | 1.1.9（`uitest_agent_v1.1.9.x86_64_so`） |
| `uitest > 6.0.2.1` 且非 x86_64 → **unix socket 模式** | **1.2.2** |
| `uitest >= 5.1.1.3` | 1.1.10 |
| `uitest >= 5.1.1.2` | 1.1.5 |
| 其它 | 1.1.3 |

手表（uitest 7.0.0.1 / arm64-v8a）命中 **1.2.2**。
x86_64 那版另有 `startCaptureScreen/stopCaptureScreen/startCaptureUiAction/stopCaptureUiAction`
字符串，arm64 v1.2.2 只剩 `copyScreen`（已废弃）。

### 传输：只有 abstract socket

`uitest start-daemon singleness` 在 unix socket 模式下**只建 `@uitest_socket`**，
不监听 TCP 8012（`/proc/net/tcp` 里查不到 1F4C）。hdckit 写死 `tcp:8012` 是面向旧设备；
新设备必须 `fport … localabstract:uitest_socket`。

### ⚠ `fport rm` 在这套 hdc/手表上恒失败

```
$ hdc fport rm tcp:29400
[Fail]Remove forward ruler failed, ruler is not exist tcp:29400   # rc=0
$ hdc fport ls
… tcp:29400 localabstract:uitest_socket    [Forward]              # 还在
```

残留转发连得上但没人应答，只能靠 `hdc kill && hdc start` 清。对策：**不做预清**，
固定端口重复 `add`（同一端口只保留一条记录）；且转发指向 `@uitest_socket`，
下次 daemon 重建同名 socket 后这条转发会重新可用。

### `uitest start-daemon` 偶发失败

实测偶发「socket 没建出来」。实现里整段序列重试 3 次，失败时把 `start-daemon` 输出与
`pidof uitest` 一起带进异常（区分“进程没起来”与“进程在但 so 没加载”）。

---

## 6. 实测结果（HUAWEI watch NIZ-AL00，466×466 圆屏）

| 指标 | pull 模式 | agent 模式 |
|---|---|---|
| 帧率 | 0.61 fps | **1.3~30.8 fps**（= 画面变化率：静态 0，表盘动画 ~1.3，滚动峰值 30.8） |
| 单帧 | 24.9 KB（原分辨率） | 8.2 KB（scale 0.5 → 233×233） |
| 首帧延迟 | ~1.6 s | 9~22 s（含推 so / 起 daemon / 握手 / 首帧兜底） |
| 黑帧 / 错误帧 | 0 / 0 | 0 / 0 |
| 尺寸一致性 | 466×466 | 全部 233×233 |
| 连续静止 112 s | — | 不掉线 |
| `power-shell suspend` 息屏 | — | 不中断，唤醒后继续出帧 |

启停干净：`stop()` 后 `pidof uitest` 为空、`/data/local/tmp/agent.so` 已删。
录制闭环（GUI offscreen）：录制 15s → ffconcat VFR → mp4 通过 ffprobe 校验。

---

## 7. 复现脚本

逆向过程中在 `/tmp/agentprobe/` 留下一组探针（未入库）：

| 脚本 | 作用 |
|---|---|
| `probe.py` / `probe2.py` | 早期探针（分帧方式未知，走了弯路） |
| `probe10.py` | **定位 sid→分帧规则**的实验 |
| `probe11.py` | 确认大 sid 下应答与推流帧都是完整分帧 |
| `probe9.py` | scale 矩阵（每档独立重启 daemon，避免误判） |
| `e2e2.py` | `AgentCapture` 端到端 + 帧率/尺寸/清理校验 |
| `hb.py` | 心跳（静止画面）验证 |
| `gui_e2e.py` | GUI offscreen + 录制 mp4 闭环 |

---

## 8. 尚未解决的问题

1. **静止画面必须靠客户端心跳**，设备端没有「强制刷新/固定间隔」选项
   （`options` 只解析 `scale` 与 `displayId`，无 interval/fps/quality 字段）。
2. **`agent.so` 不可分发**：华为版权二进制，只能运行时从本机 DevEco Testing 安装目录
   或 hdckit 包里提取（`WSCRCPY_AGENT_SO` 可显式指定）。仓库不内置。
3. **x86_64 / 旧 uitest 设备未验证**：本仓库只在 arm64 手表 + uitest 7.0.0.1 上跑通。
4. `Captures` 下是否有一次性截图 api（`captureScreen`/`screenshot` 直接把连接打挂）
   未继续追；当前用 `snapshot_display` 兜底首帧已够用。
