---
name: leave_turn_off
description: 离开关灯：门传感器关闭（人出门）后延时关灯，并播报提醒。可选延时秒数。
tags: [lighting, door, tts, 基础]
params: sensor, light, room, delay, text
---
场景: {{room}}离开关灯
触发: {{sensor}} 关
延时: {{delay|30}} 秒
动作: light.turn_off({{light}})
调用子流程: demo_notify(text={{text|已为您关闭灯光}}, room={{room}}, level=一般)
预期:
  {{light}} = off
