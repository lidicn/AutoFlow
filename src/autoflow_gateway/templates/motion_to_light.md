---
name: motion_to_light
description: 人体/移动传感器触发开灯（可选亮度）。最基础的高频场景。
tags: [lighting, motion, 基础]
params: sensor, light, room, brightness
---
场景: {{room}}人体感应开灯
触发: {{sensor}} 有人
动作: light.turn_on({{light}}, brightness={{brightness|100}})
预期:
  {{light}} = on
