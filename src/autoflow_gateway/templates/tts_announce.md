---
name: tts_announce
description: 纯语音播报：经 demo_notify 子流程播报一段文本（不依赖传感器，可用 inject 或定时触发）。
tags: [tts, announce, 基础]
params: text, room, level
---
场景: {{room}}语音播报
触发: inject
调用子流程: demo_notify(text={{text}}, room={{room}}, level={{level|一般}})
预期:
  subflow: demo_notify
