场景: 全功能厨房水槽
触发: binary_sensor.study_motion 有人
触发: sensor.door 变化
查询: light.living_main off
分支 msg.payload == "有人":
    动作: light.turn_on(light.living_main, brightness=80)
    延时: 5
    调用子流程: demo_notify(text=欢迎, room=客厅)
否则:
    动作: light.turn_off(light.living_main)
取值: sensor.temp temperature
提取: 温度 = payload.temperature
时间段: 07:00-23:00
动作: light.turn_on(light.hall)
调用子流程: llm_doubao_image(prompt=`猫`)
提取: 图片链接 = payload.reply
并行:
    动作: light.turn_on(light.kitchen)
    构建: {"k":"v"}
    请求: POST https://example.com/api
条件: light.fan = on
动作: climate.set_temperature(climate.x, temperature=22)
观测: 看状态
注释: 说明文字
原生节点: {"type":"change","name":"手写","rules":[{"t":"set","p":"payload.x","pt":"msg","to":"1","tot":"num"}]}
