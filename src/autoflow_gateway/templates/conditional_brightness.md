---
name: conditional_brightness
description: 多条件亮度开关：人体感应触发后，根据亮度参数选择开灯亮度。白天全亮、夜间柔光。演示分支+变量用法。
tags: [lighting, motion, branch, 进阶]
params: sensor, light, room, day_brightness, night_brightness, night_start
---
场景: {{room}}亮度自适应开灯
触发: {{sensor}} 有人
变量: night_start = {{night_start|22}}
分支: 状态.光照 < night_start
  动作: light.turn_on({{light}}, brightness={{day_brightness|100}})
否则
  动作: light.turn_on({{light}}, brightness={{night_brightness|30}})
预期:
  {{light}} = on
