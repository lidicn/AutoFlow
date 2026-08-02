---
name: entry_announce
description: 入户播报（源自真实场景「书房入户播报」）：人进门→开灯+TTS 语音欢迎。agent 填传感器/灯/房间/欢迎语即可。
tags: [announce, lighting, tts, 基础]
params: sensor, light, room, text, brightness
---
场景: {{room}}入户播报
触发: {{sensor}} 有人
动作: light.turn_on({{light}}, brightness={{brightness|100}})
调用子流程: demo_notify(text={{text}}, room={{room}}, level=一般)
预期:
  {{light}} = on
  subflow: demo_notify
