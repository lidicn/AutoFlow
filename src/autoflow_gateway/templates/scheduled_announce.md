---
name: scheduled_announce
description: 定时语音播报：在指定时间通过 demo_notify 子流程播报一段文本（如每日日程提醒、天气预报播报）。agent 填时间/文本/房间即可。
tags: [tts, schedule, announce, 基础]
params: time, text, room, level
---
场景: {{room}}定时播报
触发: 每天 {{time}}
调用子流程: demo_notify(text={{text}}, room={{room}}, level={{level|一般}})
预期:
  subflow: demo_notify
