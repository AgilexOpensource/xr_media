# SDK API 参考

[English](API.md) | [简体中文](API.zh-CN.md) · [返回首页](../README.zh-CN.md)

本文描述 `xr_media` 的 Python 接口。首次安装、证书和启动步骤见首页。

## 目录

- [接口选择与快速示例](#1-接口选择与快速示例)
- [服务配置与生命周期](#2-服务配置与生命周期)
- [接收浏览器音视频](#3-接收浏览器音视频)
- [向浏览器发送音视频](#4-向浏览器发送音视频)
- [帧格式与时间戳](#5-帧格式与时间戳)
- [XR 数据](#6-xr-数据)
- [状态与统计](#7-状态与统计)
- [CLI、浏览器操作与访问规则](#8-cli浏览器操作与访问规则)
- [自定义网页与 HTTP 接口（进阶）](#9-自定义网页与-http-接口进阶)

## 1. 接口选择与快速示例

| 目标 | 接口 |
|---|---|
| 创建包含媒体和 XR 的服务 | `XrMediaServer(config=None, on_xr=None)` |
| 仅创建媒体服务 | `MediaServer(config=None)` |
| 浏览器视频／音频 → Python | `register_browser_input()` + `add_video_listener()` / `add_audio_listener()` |
| Python 视频／音频 → 浏览器 | `register_video_stream()` / `register_audio_stream()` + `push_video_frame()` / `push_audio_frame()` |
| 获取 XR | `on_xr(sample)` 或 `latest_xr` |

以上类型均可从 `xr_media` 导入。`XrMediaServer` 继承全部 `MediaServer` 接口。
注册卡片和监听器后再启动；新增卡片后，已连接页面需重新连接以重新协商。

先手动执行 `xr_media --init-certs`。每个示例单独保存为 Python 文件后运行。

### 接收音视频和 XR

打开打印的地址，启动上行卡片；在头显进入 XR 会话获取姿态。

```python
import time

from xr_media import AudioFrame, VideoFrame, XrMediaServer, XrSample


def on_video(stream_id: str, frame: VideoFrame):
    print(stream_id, frame.data.shape, frame.pixel_format)


def on_audio(stream_id: str, frame: AudioFrame):
    print(stream_id, frame.samples, frame.sample_rate)


def on_xr(sample: XrSample):
    if sample.hmd_tracked:
        print("headset", sample.hmd.px, sample.hmd.py, sample.hmd.pz)
    if sample.left.tracked:
        print("left trigger", sample.left.input.trigger_pressed)


server = XrMediaServer(on_xr=on_xr)
server.register_browser_input("camera", "video", "Camera")
server.register_browser_input("microphone", "audio", "Microphone")
server.add_video_listener(on_video)
server.add_audio_listener(on_audio)
server.start_background()
try:
    for url in server.page_urls:
        print(url)
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    server.stop_background()
```

### 发送图像和音频

示例发送绿色图像与静音 PCM；实际使用时替换数组数据。

```python
import time
import numpy as np
from xr_media import AudioFrame, VideoFrame, XrMediaServer

server = XrMediaServer()
server.register_video_stream("image", "Image")
server.register_audio_stream("sound", "Sound")
server.start_background()
try:
    for url in server.page_urls:
        print(url)
    while True:
        stamp = time.monotonic_ns()
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        image[:, :, 1] = 180
        server.push_video_frame(VideoFrame(image, 320, 240, "bgr24", stamp), "image")
        pcm = np.zeros(320, dtype=np.int16)
        server.push_audio_frame(AudioFrame(pcm, timestamp_ns=stamp), "sound")
        time.sleep(0.02)
except KeyboardInterrupt:
    pass
finally:
    server.stop_background()
```

## 2. 服务配置与生命周期

### 配置

`MediaServerConfig` 是不可变数据类；创建服务时传入。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `host` | `"0.0.0.0"` | 监听地址 |
| `port` | `9443` | 1–65535 |
| `cert_path` / `key_path` | `None` | `Path`，默认查找用户配置目录的 `cert.pem`、`key.pem` |
| `codec` | `"h264"` | `h264`、`vp8`、`vp9` |
| `ice_servers` | `()` | `(url, username, credential)` 元组序列；不需认证时后两项为 `None` |
| `max_clients` | `1` | 正整数，整体媒体连接上限 |
| `video_bitrate_min` | `1_000_000` | 最低视频码率，bit/s |
| `video_bitrate_default` | `3_000_000` | 初始视频码率，bit/s |
| `video_bitrate_max` | `6_000_000` | 最高视频码率，bit/s |

码率满足 `100000 <= min <= default <= max`，会影响同进程编码器。端口、码率、客户端上限或编码类型不合法时抛出 `ValueError`。
服务不会生成证书；先手动执行 `xr_media --init-certs`。证书或私钥文件不存在时，当前实现使用 HTTP。

### 启动和停止

| 接口 | 返回值 | 用法 |
|---|---|---|
| `start_background(timeout=10.0)` | `None` | 普通 Python 程序；后台启动网络线程，超时单位为秒 |
| `stop_background(timeout=4.0)` | `None` | 与后台启动配对；应放在 `finally` 中 |
| `await start()` | `None` | 在已有 asyncio 事件循环中启动 |
| `await stop()` | `None` | 与异步启动配对 |
| `page_urls` | `list[str]` | 本机网络地址及当前 HTTP/HTTPS 协议 |

两种生命周期方式选一种，不要混用。`start_background()` 启动等待超时抛出 `TimeoutError`，初始化失败抛出 `RuntimeError`。停止超时会记录警告。

### asyncio 示例

已有事件循环时使用以下方式；`start()` 完成初始化后返回，需由应用保持事件循环运行。取消任务或退出时执行 `stop()`。

```python
import asyncio
from xr_media import XrMediaServer


async def main():
    server = XrMediaServer()
    try:
        await server.start()
        print(*server.page_urls, sep="\n")
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

## 3. 接收浏览器音视频

```python
server.register_browser_input(stream_id, media_type, label="", on_control=None)
server.add_video_listener(callback)
server.add_audio_listener(callback)
server.remove_video_listener(callback)
server.remove_audio_listener(callback)
```

这些方法返回 `None`。

- `stream_id`：非空，限字母、数字、`_`、`-`；不同卡片使用不同 ID。
- `media_type`：`"video"` 或 `"audio"`；无效类型或 ID 抛出 `ValueError`。
- `label`：页面名称，空值时使用 ID。
- `on_control(action, value)`：可选普通函数，接收上行卡片操作，如 `start`、`pause`、`resume`、`stop`、`ended`、`preview`、`speaker`。`start` 的值为来源，预览／扬声器值为布尔值；其余通常为 `None`。
- 音视频回调签名：`callback(stream_id, frame)`，返回值忽略；支持普通或 `async` 函数。取消监听时传入原函数对象。

`VideoFrame.data` 为 BGR NumPy 数组；`AudioFrame.data` 为一维 int16 PCM 数组。回调仅处理已接收、解码且获准转发的数据。每张上行卡片独占；暂停保留占用，停止或断线清理后释放。

普通回调在线程中执行，异步音视频回调在网络事件循环中执行。保持回调简短；共享数据需自行同步，耗时任务交给独立工作队列。回调异常会记录日志，不作为返回值通知调用者。

同一接收任务按顺序等待每个音视频回调，不同轨道或客户端的任务可能并发。移除监听不取消已开始的回调；移除前已取得的回调快照也可能继续执行。`on_control` 使用普通函数，在线程中执行，不支持异步函数。

| 上行动作 | `value` | 含义 |
|---|---|---|
| `start` | 来源字符串 | 浏览器已启动所选来源 |
| `pause` / `resume` | `None` | 暂停／恢复 |
| `stop` / `ended` | `None` | 停止／来源结束 |
| `preview` / `speaker` | `bool` | PC 预览／扬声器开关 |

这些是控制通知，应用不需要再次打开浏览器设备；内部占用申请 `claim` 不交付给控制回调。

## 4. 向浏览器发送音视频

| 方法 | 返回值 | 说明 |
|---|---|---|
| `register_video_stream(stream_id, label="", kind="", on_control=None)` | `None` | 注册视频；新流空 `kind` 使用 `live`，已有流空 `kind` 保留原值 |
| `push_video_frame(frame, stream_id="main", label="")` | `None` | 写入最新视频帧，不代表浏览器已显示 |
| `register_audio_stream(stream_id, label="", kind="audio")` | `None` | 注册音频 |
| `push_audio_frame(frame, stream_id="audio", label="")` | `None` | 写入 PCM 音频块 |

推送时不存在的流会自动注册；建议先注册再连接浏览器。视频只保留最新帧，不保证每次推送都显示。推送后不要原地修改图像数组；复用采集缓冲区时传入副本。

视频随新帧到达发送，SDK 不主动限制发送帧率；没有新帧时等待，不重复发送最后一帧。实际帧率取决于采集、编码与网络能力。

`kind` 描述来源，不负责打开设备或文件，也不是强制枚举：

| 媒体 | 内置取值 | 行为 |
|---|---|---|
| 视频 | `live`、`camera`、`ros`、`video_test` | 普通视频卡片；新流的 `kind=""` 按 `live` 处理 |
| 视频 | `file` | 显示文件控制，应用通过 `on_control` 处理请求 |
| 音频 | `audio`、`audio_test`、`ros` | 音频来源标识，默认 `audio` |

允许自定义字符串，按普通卡片处理。先注册来源，再推送帧；推送不会修改来源。重新注册视频时，`kind=""` 保留原来源。

**重复注册：** 视频更新非空 `label`、`kind` 和非 `None` 的 `on_control`；音频更新非空 `label`、`kind`，省略 `kind` 会使用默认值 `audio`。上行同名注册会重建卡片状态，应在启动前完成。所有方向及媒体类型的卡片使用全局唯一 ID，避免浏览器映射冲突，不要依赖服务端拒绝重名。

**音频节奏与缓冲：** 按采样时长推送，例如每 20 ms 推送 320 个样本，不要一次推完整文件。当前每流最多保留 200 个块，满时移除最旧块；容量单位是块。客户端独立读取，不会互相取走数据；新连接只读取连接后的块，无新块时发送 320 个静音样本。慢客户端可能丢失已移除的块。推送后不要修改样本数组。

`kind="file"` 显示文件控制；`on_control(action, value)` 接收播放请求，源的暂停／继续等行为需由应用实现。设置元数据不等于读取或播放文件。

| 下行动作 | `value` | 应用应执行的操作 |
|---|---|---|
| `play` | `None` | 继续读取源 |
| `pause` | `None` | 暂停读取源 |
| `stop` | `None` | 停止并回到起点 |
| `replay` | `None` | 回到起点并播放 |

以上是当前页面产生的文件控制请求，不提供进度拖动。控制通道可透传其他动作，但不代表内置播放器已实现；`loop` 是 CLI 源内部支持项，不是当前页面按钮。

可将控制请求交给采集线程处理，避免在回调中执行耗时操作：

```python
from queue import SimpleQueue

commands = SimpleQueue()

def on_control(action, value):
    commands.put((action, value))

server.register_video_stream("movie", kind="file", on_control=on_control)
```

采集线程消费 `commands`，实现 `play`、`pause`、`stop`、`replay`、`loop`，再更新下面的播放状态。此示例只接收命令，不实现文件播放器。

```python
server.set_stream_status("movie", "paused")
server.set_stream_playback("movie", position=12.0, duration=60.0,
                           paused=True, ended=False, loop=False)
```

状态可用 `waiting`、`live`、`paused`、`seeking`、`ended`、`error`。无效状态抛出 `ValueError`，流不存在抛出 `KeyError`。播放位置和时长单位为秒，方法返回 `None`。这些接口更新视频元数据，不控制浏览器本地上传文件。

## 5. 帧格式与时间戳

### VideoFrame

`VideoFrame(data, width, height, pixel_format="bgr24", timestamp_ns=0)`

| 字段 | 说明 |
|---|---|
| `data` | NumPy 数组或紧密排列的字节；彩色数组通常为 `(height, width, 3)` |
| `width` / `height` | 正整数，单位为像素 |
| `pixel_format` | `bgr24`、`rgb24`、`gray8`、`i420`、`nv12` |
| `timestamp_ns` | 纳秒时间戳；默认 0，建议生产者显式传入 |

非法尺寸或格式抛出 `ValueError`。构造函数不完整验证数据长度，生产者需保证数据与尺寸、格式匹配。

所有视频数组使用 `uint8`，字节数据紧密排列、不带行填充。以下 `H`、`W` 为原始高度和宽度：

| 格式 | 数组 shape | 字节数 | 排列 |
|---|---|---|---|
| `bgr24` / `rgb24` | `(H, W, 3)` | `H × W × 3` | BGR / RGB |
| `gray8` | `(H, W)` | `H × W` | 灰度 |
| `i420` | `(H × 3 / 2, W)` | `H × W × 3 / 2` | Y、U、V 平面连续排列 |
| `nv12` | `(H × 3 / 2, W)` | `H × W × 3 / 2` | Y 平面后接交错 UV |

`i420`、`nv12` 的宽高必须为偶数；其他格式的奇数边缘会在发送时补齐为偶数，浏览器收到的尺寸可能增加一行或一列。

### AudioFrame

`AudioFrame(data, sample_rate=16000, channels=1, timestamp_ns=0)`

`data` 使用一维 NumPy `int16` 数组。只支持 16000 Hz、单声道，且样本数大于 0，否则抛出 `ValueError`。只读属性 `samples` 返回样本数。320 个样本对应 20 ms；不是 320 字节。

传入原始 PCM 样本，不是 WAV 文件内容或 MP3 压缩数据；文件须先解码。

接收帧的时间戳来源于媒体时间线，缺失时使用本机单调时钟；不要当作 UTC 时间或跨设备同步依据。

## 6. XR 数据

`XrMediaServer(on_xr=callback)` 的回调签名为 `callback(sample: XrSample)`；使用普通函数，返回值忽略。`latest_xr` 在第一帧前为 `None`，之后保存最后收到的样本，不保证仍在跟踪。

| 类型 | 字段 |
|---|---|
| `XrSample` | `t_ms=0`、`headset_id="headset"`、`hmd`、`left`、`right`、`hmd_tracked=False`、`passthrough=False` |
| `RigidPose` | `px/py/pz=0.0`（米）；`qx/qy/qz=0.0`、`qw=1.0`（四元数 x/y/z/w） |
| `TrackedHand` | `pose`、`input`、`tracked=False`、`source_type="none"`、`joints={}` |
| `HandInput` | 按键与模拟量，见下表 |

| 输入字段 | 类型 | 默认值 |
|---|---|---|
| `trigger_pressed`、`grip_pressed` | `bool` | `False` |
| `stick_clicked`、`face_primary`、`face_secondary` | `bool` | `False` |
| `trigger_analog`、`grip_analog` | `float` | `0.0` |
| `stick_x`、`stick_y` | `float` | `0.0` |

`hmd` 为 `RigidPose`，`left/right` 为 `TrackedHand`。`joints` 是关节名称到 `RigidPose` 的字典，可能为空。使用姿态前检查对应跟踪标志。`t_ms` 是客户端样本时间（毫秒），不保证与 PC 时钟同步。

浏览器将 WebXR 位姿转换为 Z-up。手部捏合映射到 `trigger_pressed` 和 `trigger_analog`。`source_type` 用于区分输入来源，例如 `controller`、`hand`、`none`。

解析辅助方法：`RigidPose.from_mapping(data)`、`HandInput.from_mapping(data)`、`TrackedHand.from_mapping(data)` 和 `XrSample.from_json(payload)`。最后一个接收已解析的字典，不是 JSON 字符串；解析缺省值不代表跟踪有效。

## 7. 状态与统计

| 接口 | 结果 |
|---|---|
| `list_streams()` | 视频下行流的 `list[dict]`，不包含全部音频和上行卡片 |
| `get_stats()` | `MediaServerStats` |
| `get_xr_stats()` | XR 收到、派发、替换计数；仅 `XrMediaServer` |
| `GET /api/streams` | `{"streams": [...]}`，包含视频、音频和上行卡片 |
| `GET /api/stats` | `MediaServerStats` 字段对应的 JSON |
| `GET /api/xr/state` | `{"xr": 样本或null, "session": {...}}`；仅 `XrMediaServer` |

`MediaServerStats` 字段：`clients`、`frames_received`、`frames_replaced`、`frames_sent`、`latest_timestamp_ns`、`streams`、`audio_frames_received`、`audio_frames_sent`。`clients` 统计媒体 Peer，不等同于所有已预占入场名额；视频收到／替换计数来自下行帧缓冲，替换不等于网络丢包。

`get_xr_stats()` 返回 `xr_server_received`、`xr_server_dispatched`、`xr_server_replaced`。HTTP 接口不提供用户认证，仅在可信网络使用。

### 返回示例

推送一帧后，`list_streams()` 中的视频条目示例：

```json
{
  "id": "main",
  "label": "main",
  "media_type": "video",
  "ready": true,
  "status": "live",
  "kind": "live",
  "playback": {},
  "width": 640,
  "height": 480,
  "frames_received": 1,
  "frames_replaced": 0,
  "capture_fps": 0.0
}
```

通过 `dataclasses.asdict(server.get_stats())` 将统计对象转为字典。
首个 XR 样本到达前，`/api/xr/state` 返回：

```json
{
  "xr": null,
  "session": {
    "mode": null,
    "passthrough": false,
    "preview": false,
    "ui_locked": false
  }
}
```

## 8. CLI、浏览器操作与访问规则

### 证书初始化

命令见首页的[生成证书](../README.zh-CN.md#生成证书)。每次执行都会重新生成并覆盖 `cert.pem` 和 `key.pem`，不检查是否存在或过期；服务启动时不会自动生成证书。换证后重启服务，并在访问设备上信任新证书。

开发证书保存在 `$XDG_CONFIG_HOME/xr_media/tls`，未设置该变量时为
`~/.config/xr_media/tls`；服务端自动读取。首次访问时，需在访问设备上按浏览器要求
配置信任。远端摄像头、麦克风和 WebXR 需要安全上下文；无证书时服务可能使用 HTTP。

### CLI 来源

通过 `--stream [label=]source[,key:value...]` 重复添加卡片。

| 来源 | 方向 | 选项／说明 |
|---|---|---|
| `video_test` | PC → 浏览器 | `width`、`height`；内部以 30 Hz 产生帧 |
| `audio_test` | PC → 浏览器 | 测试音频 |
| `camera[:index]` | PC → 浏览器 | `width`、`height`、`fourcc`（默认 `MJPG`）；不额外限频 |
| `video_file:path` | PC → 浏览器 | 原尺寸、文件帧率；帧率无效时警告并回退到 30 Hz |
| `audio_file:path` | PC → 浏览器 | 解码并循环播放音频 |
| `video_input` | 浏览器 → PC | 浏览器视频来源 |
| `audio_input` | 浏览器 → PC | 浏览器音频来源 |

`video_file:` 只读取视频，不含音轨；`audio_file:` 读取第一个音轨（也可来自视频容器），
转换为 16 kHz 单声道 PCM，按采样速率循环播放。
同一文件可用不同标签及两种前缀分别添加音视频卡片，但两路独立播放，不保证音画同步。

### 浏览器操作与多客户端

| 项目 | 行为 |
|---|---|
| 整体入场 | `--max-clients` 为正整数，默认 1；获准后初始化卡片 |
| 满员 | 显示在线人数、上限、连接编号及浏览器类型；空位释放后点击重试 |
| 视频／音频上行 | 每张卡片只允许一个客户端使用；不同卡片独立 |
| 上行暂停／停止 | 暂停保留占用；停止或断线清理后释放；断线检测并非即时 |
| 视频／音频下行 | 已入场客户端可共同接收；源端播放控制可能影响所有观看者 |
| XR | 同时仅一个 XR 会话；退出或断线后释放 |

上行卡片选择 `Device` 或 `File` 后点击启动。`PC View` 请求在服务端电脑
打开预览；音频上行的 `PC Out` 控制服务端扬声器，需要 `paplay` 或 `aplay`。
下行音频卡片的 `PC Out` 控制当前浏览器扬声器，并非远端 PC。
音频默认静音，播放或波形分析可能需要用户交互。
XR 功能需要兼容的头显及浏览器，普通桌面浏览器仍可使用媒体卡片。

### 常见问题

缺少 Python 依赖时，启动错误会列出缺失项并给出当前 Python 环境的安装命令；SDK 不自动安装。`--help` 和 `--init-certs` 不要求安装媒体库，证书生成仍需要 Bash 和 OpenSSL。OpenCV 仅在使用摄像头或本地视频文件时检查。

| 现象 | 处理 |
|---|---|
| 摄像头或 XR 不可用 | 检查 HTTPS、证书信任及浏览器权限 |
| 已满员或卡片被占用 | 等待占用者退出，再重试 |
| 音频无声 | 点击播放并检查静音、音量与输出设备 |
| 视频停留在最后一帧 | 检查源是否继续调用 `push_video_frame()` |
| 摄像头／文件启动提示缺少 OpenCV | 安装 `opencv` 可选依赖，见首页 |

### 使用限制

- 服务不提供用户认证，仅用于可信网络；人数限制和卡片独占不替代鉴权。
- 使用 `--host` 指定监听地址，不直接暴露到公网。
- 使用受信任的 TLS 证书，并妥善保管私钥。

## 9. 自定义网页与 HTTP 接口（进阶）

需要增加 HTTP 接口或替换网页时，继承服务类。增加音视频卡片仍使用注册接口，不需要继承。

### 添加 HTTP 接口

保存并运行以下示例，先按首页说明生成证书。访问打印地址下的 `/api/app/status` 即可查询应用状态；按 `Ctrl+C` 退出。

```python
import time
from aiohttp import web
from xr_media import XrMediaServer


class AppServer(XrMediaServer):
    def configure_app(self, app):
        super().configure_app(app)
        app.router.add_get("/api/app/status", self.app_status)

    async def app_status(self, request):
        return web.json_response({"running": True})


server = AppServer()
try:
    server.start_background()
    print(*server.page_urls, sep="\n")
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    server.stop_background()
```

`configure_app()` 在启动时、开始监听前调用。保留 `super()` 调用以注册 XR 路由；自定义路径建议使用 `/api/app/` 前缀，不要重复注册 `/`、`/signal`、`/xr-signal` 或已有 `/api/` 路径。

处理函数使用 `async def` 并返回 aiohttp 响应；不要在其中执行阻塞采集或耗时计算。新增接口不会自动获得身份认证或卡片占用保护，敏感操作需自行鉴权。

### 替换媒体网页

以下示例适用于 `MediaServer`。将自定义资源放在脚本旁的 `web/` 目录，并使用前例的启动、停止方式运行 `AppServer()`：

```python
from pathlib import Path
from xr_media import MediaServer


class AppServer(MediaServer):
    def asset_root(self):
        return Path(__file__).resolve().parent / "web"
```

```text
app.py
web/
  index.html
  app.css
  client.js
```

`/` 读取 `web/index.html`，`/static/` 映射整个 `web/`。沿用默认界面时，从 SDK 的 `static/` 复制 `index.html`、`app.css`、`client.js`，保留页面元素与脚本的对应关系；新增资源也放在此目录。不要在公开目录放私钥或配置凭据。

**XR 服务的区别：** 当前 `XrMediaServer` 的首页直接读取内置模板并注入 XR 资源，`/xr/` 也固定使用内置资源。仅重写 `asset_root()` 只替换 `/static/`，不会替换 XR 首页或 `/xr/`。完整替换 XR 页面需要额外定制页面处理与资源路由，不属于这两个扩展接口的直接能力。
