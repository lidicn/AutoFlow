---
name: conditional_brightness
description: 多条件亮度开关：人体感应触发后，根据亮度（照度传感器）选择开灯亮度。白天全亮、夜间柔光。演示分支+变量+取值用法。
tags: [lighting, motion, branch, 进阶]
params: sensor, light, room, lux, day_brightness, night_brightness, night_start
---
场景: {{room}}亮度自适应开灯
触发: {{sensor}} 有人
变量: night_start = {{night_start|22}}
取值: {{lux}} 光照
分支: 光照 < night_start
  动作: light.turn_on({{light}}, brightness_pct={{day_brightness|100}})
否则
  动作: light.turn_on({{light}}, brightness_pct={{night_brightness|30}})
预期:
  {{light}} = on
