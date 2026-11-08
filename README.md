## Introduction✨

`直播适配器` 是一个全新的直播全链路企划。正如企划原型角色来自直播间里的 VTuber 一样，本企划的主角同样应当是一个「活着的人」，而不是一段只会输出文字的模型。类似于 VTuber 在现实中的实现，本企划致力于为她提供一个真正待在直播间里的能力。

我们知道，由于框架限制，Chatbot 在直播间的输出总是固定的，读得懂文本，读不懂一场正在进行的直播。所以我们期望，她能在代码层面上突破这些限制，比如让 B 站与抖音的弹幕、醒目留言、礼物、上舰真的流进她的上下文，比如让她的回答被合成成声音播出去，比如让表情和动作跟着她正在说的那句话合成到 Live2D 的参数上，但这些永远不够，我们希望她能更了解自己正在经历的事，所以我们会给她一份日程表，让她到点自己去开网页看点什么，期望有朝一日当她开口时，讲的是她真的看到过的东西。

综上所述，我们期望 `直播适配器` 具有以下能力：

1. 双平台弹幕接入：B 站与抖音的弹幕、醒目留言、礼物、上舰都能被她听见
2. 拟人 Live2D 的控制模拟：皮套一直在动，表情与道具跟着台词走
3. 主动对话系统：不用别人叫，她自己会开口
4. 语音与口型：把话真的说出口

基于上述见解，插件通过 MaiBot 的插件系统接入直播链路：弹幕归一化后注入聊天流，回复再交给 Breeze-TTS-2.cpp、VTube Studio 与有机演出引擎。

## Features🪄

- [x] **双平台弹幕接入**：B 站与抖音的弹幕、醒目留言、礼物、上舰归一化后注入同一条聊天流

- [x] **拟人 Live2D 的控制模拟**：子进程引擎按 60fps 合成参数驱动 VTS，表情与道具由 AI 在台词里点单

- [x] **主动对话系统**：纯事件驱动，语音播完或游戏推进时自己开口

- [x] **口型与字幕**：语音播到虚拟声卡由 VTS 驱动口型，字幕逐句跟随

- [x] **日程表**：按时段安排该做的事，到点自动换挡

- [x] **ASR 语音对话**：按住热键说话，松开直接和 AI 对话

## Installation🚀

### 1. 依赖环境

| 组件 | 是否必需 | 说明 |
| --- | --- | --- |
| MaiBot 1.3.x + 插件 SDK 2.20+ | 必需 | 宿主要支持 `maisaka.*` 系列钩子 |
| VTube Studio | 演出功能必需 | 设置里打开 API（默认 8001） |
| [Breeze-TTS-2.cpp](https://github.com/HoppouAI/Breeze-TTS-2.cpp)（`breeze-server.exe`） | 语音必需 | 独立进程，默认 `http://127.0.0.1:9880` |
| 虚拟声卡（VB-Cable 等） | 口型必需 | TTS 输出到 `CABLE Input`，VTS 从 `CABLE Output` 录 |
| SiliconFlow API Key | 可选 | 只有用 ASR 语音对话才需要 |

### 2. 装上插件

1. 把整个目录放进 `MaiBot/plugins/live-danmaku-adapter/`；
2. 启动 MaiBot，在 WebUI 的插件页启用它，首次启动会生成 `config.toml`；
3. 在 WebUI 的插件配置里填房间号、TTS 地址等，保存即热更新，**不用重启**；
4. 建议先让小号确认弹幕能收到，再接 VTS 与 TTS——否则一次改太多，出问题分不清是哪一环。

## Usage🎬

### 弹幕接入

事件进上下文时长这样：

```text
[B站弹幕] 昵称: 哈哈哈哈哈
[抖音礼物] 昵称 送出了 小心心x1，¥0.1
[B站醒目留言 ¥30] 昵称: 加油啊
[B站上舰·舰长] 昵称 开通了舰长
[语音弹幕] 昵称: 主播你在干嘛
```

B 站填 `[bilibili].room_id`，`sessdata` 留空也能收弹幕，只是昵称被打码。抖音填 `[douyin].web_rid`，
握手签名靠 `mini-racer`。同内容去重、方括号指令弹幕丢弃、超长截断由 `[filter]` 管。

### 语音播报

```text
AI 回复 → 摘动作记号 → Breeze-TTS-2.cpp（POST /v1/audio/speech）
        → 24kHz 裸 PCM 流式播放 → 虚拟声卡 → VTS ULipSync 驱动口型
                                  └→ 按真实播放进度逐句推字幕
```

后端是 [Breeze-TTS-2.cpp](https://github.com/HoppouAI/Breeze-TTS-2.cpp)，插件只当它的 HTTP 客户端，
服务地址由 `[tts].server` 指定（默认 `http://127.0.0.1:9880`）。返回的是 24kHz 裸 PCM，首包百毫秒级。

音色填 `[tts].voice_id`（`voices/` 里的 `.breeze` 文件名），给了参考音频和逐字转写就会在启动时自动注册。
播放设备留空则优先走虚拟声卡，**想让 VTS 有口型就必须这样**。回复超过 `auto_speak_max_length`
（默认 200 字）不合成语音，但仍上字幕。

### 语气与音效

全角 `【】` 是给皮套的动作记号，观众看不到；半角 `[]` 是给 TTS 的音效标签，观众听得到。

```text
【语气:发现真相后委屈难过，声音低落柔弱，带轻微哽咽，语速稍慢】呜……你别凶我
【压低声音】我跟你讲个秘密
```

快捷词有 `【压低声音】` `【兴奋语气】` `【带哭腔】` `【正常语气】` 等几个。音效标签是封闭词表，
词表内的（`[叹气]` `[laugh]`）被演成真实声音且不上字幕，词表外的一律摘掉。

### 表情与动作

VTS 里的表情和热键是模型作者随便起的名字，LLM 猜不到，所以插件把「当前这个皮套真能演什么」
写进提示词，让它自己在台词里点单：

| 写法 | 含义 | 例子 |
| --- | --- | --- |
| `【动作】` | 阻断式：换外观 / 掏道具 / 上表情 | `【流泪】` `【狗尾巴草】` |
| `<动作>` | 非阻断：边说边做的小动作 | `<点头>` `<眨眼>` |
| 组合 | 一个括号里逗号分隔多个 | `【微笑,看向屏幕】` |
| 重置 | 收起所有表情/道具/姿势，视线回镜头 | `【重置】` |
| 视线 | 朝向某个方向，几秒后自动滑回镜头 | `【看向镜头】` |
| 语气 | 只影响那句话的 TTS | `【语气:…】` |

一回合最多派发 3 个动作，未知名静默跳过。写记号最准；没写时插件也会按台词关键词猜一个表情，
只是容易猜偏。表情时长按台词内容算，同一情绪反复演会变淡。

`[performance].auto_props` 默认关闭，道具交给 AI 用记号决定；打开后引擎自己挑，但换装类与背景特效类
热键永不自动使用。换皮套不用改配置，插件按 `.exp3.json` / `.motion3.json` 的文件内容判断，
读不到模型文件时退回名字匹配。

### 主动对话与日程

主动对话没有轮询定时器，只在整段 TTS 播完或游戏推进一步时判断要不要开口。它和弹幕完全解耦：
主动轮会把直播消息正文换成占位符，让她只能自言自语。

```toml
[[schedule.periods]]
start = "20:12"
name = "娱乐档"
focus = "去b站看名侦探柯南"
```

时段是硬性安排，整段注入 Planner 与 Replyer，到点自动换挡并直接排一轮主动对话。

### 语音对话（ASR）

按住热键（默认 `f9`）说话，松开直接和 AI 对话。录音 16kHz 单声道，走 SiliconFlow
`/audio/transcriptions`（默认 `Qwen/Qwen3-ASR-1.7B`）。她正在播报时按下会先打断她，留一点残响
缓冲再开始录。`bypass_planner = true` 时由 Replyer 直接作答，更快但不会调工具。


## Development🛠️

### 目录结构

```text
plugin.py              主插件：配置模型、生命周期、全部钩子、命令与 API
core/                  事件模型、统一通道、消息映射与过滤
core/prefix_cache.py    DeepSeek 前缀缓存稳定层（身份钉回 / 工具表冻结 / 唯一尾部槽 / 命中率观测）
platforms/             B 站（blivedm）与抖音（签名 + 手写 protobuf 解析）链路
vendor/blivedm/        内置的 B 站弹幕库（MIT）
tts/                   Breeze-TTS-2.cpp 客户端、设备枚举、三级降级播放
vts/                   VTS 客户端/发现、表情标定、记号、道具、时序、资产文件解析
engine/                有机演出引擎（子进程本体 + 管理器 + 情绪联动 + cortico 片段库）
asr.py                 热键录音 + SiliconFlow 转写
proactive.py           主动对话的时机与意图
schedule_model.py      时段表数据模型（跨零点取模）
subtitle.py            字幕层 HTTP 服务
danmaku.py             弹幕墙 HTTP 服务
game_events.py         游戏事件接收口
tests/                 离线自测
```

## About🎗️

> [!WARNING]
> 大模型输出结果将按**原样**提供。由于提示注入攻击等复杂原因，模型有可能输出有害内容。
> 模型输出内容**不代表**项目开发者立场。
> 使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、
> **因驱动 VTS / 播放音频 / 调用系统 API 而导致的表现异常或文件风险**），开发者不承担任何责任。
>
> 另外请注意：本插件会向直播间平台发起未公开接口的请求（抖音网页版推送通道），
> 此类接口随时可能变更或失效，也请自行确认是否符合平台服务条款。

本项目以 **GPL-3.0-or-later** 提供（见 `_manifest.json` 的 `license` 字段），再分发时请保留许可文件副本。
内置 `vendor/blivedm` 为 MIT；`assets/douyin_sign.js` 来自 AGPL-3.0 的 DouyinLiveWebFetcher，
若要再分发二进制包或修改该文件，请自行确认许可证兼容性要求。

### 致谢

- [MaiBot](https://github.com/Mai-with-u/maibot)
- [blivedm](https://github.com/xfgryujk/blivedm)
- [Breeze-TTS-2.cpp](https://github.com/HoppouAI/Breeze-TTS-2.cpp)
- [DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher)
- [cortico-world-vtuber](https://github.com/Pal-AI-Lab/cortico-world-vtuber)
- [astrbot_plugin_live_stream_companion](https://github.com/menglimi/astrbot_plugin_live_stream_companion)
