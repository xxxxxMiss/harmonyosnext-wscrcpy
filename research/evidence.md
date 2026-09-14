# 符号级证据明细

> 本文件是 `官方投屏实现调研.md` 的原始证据，便于复现。所有命令均在本机与真机（BRA-AL00 / HarmonyOS 6.1.1.120）实测得到。

---

## 1. 样本与基本信息

样本目录：
`/Applications/DevEco_Testing_for_App.app/Contents/Python/lib/python3.12/site-packages/devicetest/res/recorder/`

| so | size | md5 | NEEDED 数 |
|---|---|---|---|
| `libscrcpy_server1.z.so` | 3,041,461 | `b067813b1248c8a4ed92e00fef7558e0` | 16 |
| `libscrcpy_server2.z.so` | 309,430 | `474ba825c10f7ed397771283460116a6` | 15 |
| `libscrcpy_server3.z.so` | 288,950 | `0b4e4e5a6cbd5821c8c6204477263384` | 20 |
| `libscrcpy_server4.z.so` | 264,374 | `fa0cefa720b1731b956302fa65195e6b` | 15 |

共性：

```text
SONAME = libscrcpy_server.z.so
ELF 64-bit LSB shared object, ARM aarch64, stripped
源码路径线索：../../../test/testfwk/arkxtest/scrcpy_server/proto/scrcpy.pb.cc
             native_screen_encoder.cpp
             scrcpy_server_so.cpp:(UiTestExtension_OnInit)
入口符号：UiTestExtension_OnInit / DevEcoScrcpy / xdevice_scrcpy
```

## 2. NEEDED 对比

```text
# 变体 1（grpc 静态链全量版）
libabsl_status.z.so  libabsl_str_format_internal.z.so  libabsl_strings.z.so
libabsl_sync.z.so    libnative_media_avmuxer.so  libnative_media_codecbase.so
libnative_media_venc.so  libutils.z.so  libhilog.so  libdm.z.so
libnative_media_core.so  libgpr.z.so  libgrpc.z.so  libsurface.z.so  libc.so  libc++.so

# 变体 2
libgrpcxx.z.so  libprotobuf.z.so  libprotobuf_lite.z.so  libabsl_sync.z.so
libnative_media_avmuxer.so  libnative_media_codecbase.so  libnative_media_venc.so
libutils.z.so  libhilog.so  libdm.z.so  libnative_media_core.so
libaudio_capturer.z.so  libc.so  libsurface.z.so  libc++.so

# 变体 3
libgrpc.z.so  libgrpcxx.z.so  libgpr.z.so  libprotobuf.z.so  libprotobuf_lite.z.so
libabsl_status.z.so  libabsl_sync.z.so  libabsl_log.z.so  libabsl_cord.z.so
libnative_media_avmuxer.so  libnative_media_codecbase.so  libnative_media_venc.so
libutils.z.so  libhilog.so  libdm.z.so  libnative_media_core.so
libaudio_capturer.z.so  libc.so  libsurface.z.so  libc++.so

# 变体 4（最少依赖）
libgrpc.z.so  libgrpcxx.z.so  libgpr.z.so  libprotobuf.z.so
libnative_media_avmuxer.so  libnative_media_codecbase.so  libnative_media_venc.so
libutils.z.so  libhilog.so  libdm.z.so  libnative_media_core.so
libaudio_capturer.z.so  libc.so  libsurface.z.so  libc++.so
```

## 3. 采集侧符号（4 个变体一致，均为未定义待解析）

```text
U _ZN4OHOS5Rosen13ScreenManager11GetInstanceEv
U _ZN4OHOS5Rosen13ScreenManager13GetScreenByIdEm
U _ZN4OHOS5Rosen13ScreenManager19CreateVirtualScreenENS0_19VirtualScreenOptionE
U _ZN4OHOS5Rosen13ScreenManager20DestroyVirtualScreenEm              ← 设备不兼容
U _ZN4OHOS5Rosen13ScreenManager22RegisterScreenListenerENS_4sptrINS1_15IScreenListenerEEE
U _ZN4OHOS5Rosen13ScreenManager23SetVirtualScreenSurfaceEmNS_4sptrINS_7SurfaceEEE
U _ZN4OHOS5Rosen13ScreenManager27SetVirtualScreenRefreshRateEmj
U _ZN4OHOS5Rosen13ScreenManager36SetVirtualMirrorScreenCanvasRotationEmb
U _ZN4OHOS5Rosen13ScreenManager10MakeMirrorEmNSt3__h6vectorImNS2_9allocatorImEEEERm
U _ZN4OHOS5Rosen13ScreenManager10StopMirrorERKNSt3__h6vectorImNS2_9allocatorImEEEE
U _ZN4OHOS5Rosen14DisplayManager11GetInstanceEv
U _ZN4OHOS5Rosen14DisplayManager18GetDisplayByScreenEm
U _ZN4OHOS5Rosen14DisplayManager19GetDefaultDisplayIdEv
U _ZN4OHOS5Rosen14DisplayManager33SetVirtualScreenSecurityExemptionEmjRNSt3__h6vectorImNS2_9allocatorImEEEE
U _ZNK4OHOS5Rosen7Display8GetWidthEv
U _ZNK4OHOS5Rosen7Display9GetHeightEv
U _ZNK4OHOS5Rosen7Display6GetDpiEv
U _ZNK4OHOS5Rosen7Display11GetScreenIdEv
U _Z20NativeStopScreenCopyv
U _Z21NativeStartScreenCopyv
```

### 设备 `/system/lib64/libdm.z.so` 实际导出（节选）

```text
T _ZN4OHOS5Rosen13ScreenManager19CreateVirtualScreenENS0_19VirtualScreenOptionE
T _ZN4OHOS5Rosen13ScreenManager23SetVirtualScreenSurfaceEmNS_4sptrINS_7SurfaceEEE
T _ZN4OHOS5Rosen13ScreenManager27SetVirtualScreenRefreshRateEmj
T _ZN4OHOS5Rosen13ScreenManager20DestroyVirtualScreenEmb          ← 两参版！
T _ZN4OHOS5Rosen13ScreenManager19ResizeVirtualScreenEmjj
T _ZN4OHOS5Rosen13ScreenManager20GetVirtualScreenFlagEm
T _ZN4OHOS5Rosen13ScreenManager20SetVirtualScreenFlagEmNS0_17VirtualScreenFlagE
T _ZN4OHOS5Rosen13ScreenManager22SetVirtualScreenStatusEmNS0_19VirtualScreenStatusE
T _ZN4OHOS5Rosen13ScreenManager25AddVirtualScreenBlockListERKNSt3__h6vectorIiNS2_9allocatorIiEEEE
T _ZN4OHOS5Rosen13ScreenManager25AddVirtualScreenWhiteListEmRKNSt3__h6vectorImNS2_9allocatorImEEEE
T _ZN4OHOS5Rosen13ScreenManager28SetVirtualScreenAutoRotationEmb
T _ZN4OHOS5Rosen13ScreenManager30SetVirtualScreenMaxRefreshRateEmjRj
T _ZN4OHOS5Rosen13ScreenManager19MakeMirrorForRecordERKNSt3__h6vectorImNS2_9allocatorImEEEERS6_Rm
T _ZN4OHOS5Rosen20ScreenManagerAdapter10MakeMirrorEmNSt3__h6vectorImNS2_9allocatorImEEEERmRKNS0_14RotationOptionE
...（共 737 个 Rosen 符号）
```

> 结论：**唯一缺的是 `DestroyVirtualScreen(ulong)` → 设备为 `(ulong, bool)`**。
> 注意设备上还有 `MakeMirrorForRecord`，是给录屏用的专用接口。

## 4. 编码 / 封装侧符号（全部为公开 NDK）

```text
U OH_VideoEncoder_CreateByMime   U OH_VideoEncoder_Configure   U OH_VideoEncoder_GetSurface
U OH_VideoEncoder_Prepare        U OH_VideoEncoder_Start       U OH_VideoEncoder_SetCallback
U OH_VideoEncoder_SetParameter   U OH_VideoEncoder_FreeOutputData
U OH_VideoEncoder_NotifyEndOfStream  U OH_VideoEncoder_Stop    U OH_VideoEncoder_Destroy
U OH_AVFormat_Create / Destroy / SetIntValue / SetLongValue / SetDoubleValue / SetStringValue
U OH_AVMemory_GetAddr
U OH_AVMuxer_Create / AddTrack / Start / WriteSample / Stop / Destroy
U OH_NativeWindow_DestroyNativeWindow

常量：OH_AVCODEC_MIMETYPE_VIDEO_AVC
      OH_MD_KEY_WIDTH / HEIGHT / FRAME_RATE / BITRATE / I_FRAME_INTERVAL / PIXEL_FORMAT
      OH_MD_KEY_VIDEO_ENCODE_BITRATE_MODE / OH_MD_KEY_REQUEST_I_FRAME
```

设备侧确认：

```text
$ nm -D --defined-only /system/lib64/libnative_media_venc.so | grep OH_VideoEncoder_
0000000000006200 T OH_VideoEncoder_GetSurface
0000000000006c64 T OH_VideoEncoder_SetCallback
```

## 5. gRPC 描述符还原

```text
$ python3 -c "import sys; ..."   # 由 scrcpy_pb2.py 的序列化描述符直接解码
DESCRIPTOR = AddSerializedFile(b'\n\x0cscrcpy.proto"\x9a\x01\n\x0cReplyMessage...')
```

对应 `.proto`（已整理，见主报告 §3.3）：

```proto
message ReplyMessage { string data = 1; int32 reply_type = 2; map<string, ParamValue> payload = 3; }
message ParamValue { oneof values { int64 val_int=1; double val_double=2; string val_string=3;
                                    bool val_bool=4; bytes val_bytes=5; float val_float=6; } }
message ReplyEndMessage { int32 result = 1; }
message Empty {}
service ScrcpyService {
  rpc onStart(Empty) returns (stream ReplyMessage);
  rpc onEnd(Empty) returns (ReplyEndMessage);
  rpc onRequestIDRFrame(Empty) returns (ReplyEndMessage);
}
```

PC 侧调用参数：

```python
grpc.insecure_channel(target="{}:{}".format(host, port),
                      options=[('grpc_max_receive_message_length', 10485760)])
```

## 6. 设备侧运行时日志（字符串证据）

```text
%{public}s Invoke StartScreenCopy....
%{public}s Invoke StopScreenCopy....
%{public}s Finish StopScreenCopy...., frame_count: %{public}d
%{public}s create virtual screen failed.
%{public}s Destroy virtual screen ...
%{public}s destroy virtual screen failed..
%{public}s set virtual screen surface failed.
%{public}s set virtual screen refresh frame rate failed.
%{public}s set virtual screen rotation failed.
%{public}s set virtual screen skip privacy windows failed.
%{public}s Screen changed, auto restart ScreenCopy
%{public}s get default screen failed.
%{public}s get displayInfo density:%{public}d
%{public}s Setup by screen Id: %{public}lu.
%{public}s screenId:  %{public}ld.
%{public}s windowsId: %{public}s.
%{public}s frameBitRate:  %{public}ld.
%{public}s frameRefreshInterval:  %{public}d.
%{public}s iFrameInterval:  %{public}ld.
%{public}s first encode output
%{public}s saveFrame:  %{public}d.
%{public}s Invoke RequestIDRFrame....
%{public}s Request to refresh IDR frame from %{public}s
%{public}s ScreenEncoder already stop....
%{public}s ScreenRecorder already stop....
/data/local/tmp/mytest.mp4                     ← 录像落盘路径
libaudio_capturer.z.so                          ← 音频（可选）
%{public}s nativeWindow is null!
%{public}s nativeWindow is not null!
```

## 7. 真机 dlopen 实测（4 个变体全失败）

```text
# 变体 1
W C03F07/MUSL-LDSO: relocating failed: symbol not found. dso=/data/local/tmp/probe_v1.z.so
   s=_ZN4absl12lts_202206235MutexD1Ev
E C03100/uitest/UiTestKit_Addon: [extension_executor.cpp:(ExecuteExtension)] Dlopen ... failed

# 变体 2
   s=_ZN6google8protobuf8internal14ArenaStringPtr3SetENS2_12EmptyDefaultEONSt3__112basic_stringIcNS4_11char_traitsIcEENS4_9allocatorIcEEEEPNS0_5ArenaE

# 变体 3
   同上 protobuf 符号

# 变体 4
   s=_ZN4OHOS5Rosen13ScreenManager20DestroyVirtualScreenEm

# 设备自带（上一轮遗留）libscreen_recorder.z.so（= 变体 1）
   s=_ZN4absl12lts_202206235MutexD1Ev
```

`uitest` 的 extension 加载实现（开源）：

```cpp
bool ExecuteExtension(string_view version, int32_t argc, char *argv[]) {
    const char *name = "agent.so";
    if (argc > 1 && string_view(argv[0]) == "--extension-name") {
        name = argv[1];
        used_argc = TWO;
    }
    string extensionPath = string("/data/local/tmp/") + name;
    ...
}
```

## 8. 缺失符号矩阵（对照设备 13,304 个导出符号）

基线集合：设备 `libdm / libprotobuf / libgrpc / libgrpcxx / libabsl_sync / libnative_media_venc /
libnative_media_core / libnative_media_codecbase / libnative_media_avmuxer / libsurface /
libcomposer / librender_service_client / libutils / libhilog / libc / libc++`。

| 变体 | 未定义符号 | 缺失 | 真实阻塞项 |
|---|---|---|---|
| 1 | 482 | 204 | grpc/absl 20220623 大批量 |
| 2 | 241 | 68 | protobuf 老 ABI + absl 20230802 |
| 3 | 296 | 67 | 同变体 2 |
| **4** | **299** | **43** | **2 个**（见下） |

变体 4 的 43 项中：

```text
libc++ 提供：__at_fini / __deregister_frame_info / __register_frame_info
libnative_media_venc.so 提供：OH_VideoEncoder_*（已验证）
libutils.z.so 提供：OHOS::RefBase::{Inc,Dec}StrongRef / RefPtrCallback / CanPromote ...
libgpr.z.so 提供：gpr_malloc / gpr_free / gpr_inf_future
libhilog.so 提供：HiLogPrint
libnative_media_avmuxer.so 提供：OH_AVMuxer_*

真正缺失：
1) OHOS::Rosen::ScreenManager::DestroyVirtualScreen(unsigned long)   —— 设备为 (ulong, bool)
2) OHOS::AudioStandard::AudioCapturer::Create(...)@1.0               —— 带版本标记；录屏可不需要
```

## 9. 权限模型实测

```text
$ hdc shell id
uid=2000(shell) gid=2000(shell) groups=2000(shell),1006(file_manager),1007(log),
                1097(netsys_socket),3009(readproc) context=u:r:sh:s0

$ hdc shell "snapshot_display -f /data/local/tmp/perm_probe.jpeg"
snapshot: convert rgba8888 to rgb888 successfully.        ← 慢的根因：CPU 软转换
success: snapshot display 0 , write to /data/local/tmp/perm_probe.jpeg as jpeg,
         width: 1216, height: 2688
rc=0

# 同时 hilog：
E C05A01/foundation/ATM: [VerifyAccessToken]PermissionName(ohos.permission.CUSTOM_SCREEN_RECORDING) is not exist.
E C057C2/snapshot_display/IPCObjectProxy: ... error:29201 desc:(subErr:4 SubErrDesc:outer:operation not permitted)
```

## 10. DevEco Testing 运行日志（证明官方管线在本机真机跑通）

来自 `~/Library/Application Support/DevEco Testing/common/modules/launcher/logs/`：

```text
DevEcoTesting_0_0.log:
  [ScreenCastingByUitestServiceImpl] ScreenCastingByUitestServiceImpl start capture screen success
  execute cmd is ".../java" -jar ".../screenCastingResource/screenCastingResource.jar" "9523" \
      ".../hdc" "DevEco Testing" "a9b5954197ff4501a5f076ab8fb65397"
  find commonResourceListObj: resource_name=screenCastingResource versions=["26.0.0.432"] platform=Mac_arm64
                             download_link="https://devecotesting.huawei.com/ts/api/v2/common-resource-files/930"

DevEcoTesting_2_0.log:
  [VideoRecordCasting] Server started!
  [VideoRecordCasting] websocket onopen /9CN0224A11000514_127.0.0.1_8710_a9b5954197ff4501a5f076ab8fb65397
  [VideoRecordCasting] init scrcpyDevice param sn 9CN0224A11000514
  [VideoRecordCasting] start video cast, sn is 9CN0224A11000514
  [VideoRecordCasting] startCaptureScreen onReady
  [VideoRecordCasting] 获取视频流失败UNAVAILABLE: Network closed for unknown reason     ← 27min 后
```

下载接口鉴权（直接 curl）：

```text
$ curl -sSL "https://devecotesting.huawei.com/ts/api/v2/common-resource-files/930"
{"code":401,...}   # http_code=401, size=35
```

## 11. 复现命令速查

```bash
HDC=/Users/chenxianlong/Library/Huawei/Sdk/hmscore/3.1.0/toolchains/hdc
R=/Applications/DevEco_Testing_for_App.app/Contents/Python/lib/python3.12/site-packages/devicetest/res/recorder

# 1) 符号比对
nm -D --undefined-only $R/libscrcpy_server4.z.so
$HDC file recv /system/lib64/libdm.z.so ./devicelibs/
nm -D --defined-only devicelibs/libdm.z.so | grep ScreenManager

# 2) 真机加载测试
$HDC file send $R/libscrcpy_server4.z.so /data/local/tmp/probe_v4.z.so
$HDC shell "/system/bin/uitest start-daemon singleness --extension-name probe_v4.z.so -p 5004 -m 1 -screenId 0"
$HDC shell "hilog -x | grep -E 'MUSL-LDSO|UiTestKit_Addon'"

# 3) 官方 PC 侧调用链
cat "$R/../record_agent.py"      # RecordAgent：push / fport / start-daemon / 生命周期
cat "$R/../rpc_manager.py"       # RpcManager：gRPC onStart/onEnd
```
