"""Tier1 编译器不变量普查：smoke（harness 不误报）+ 矩阵模糊测试。

运行：
    python tests/test_compiler_invariants.py            # 零依赖 runner（同 run_tests.py 调度）
    python -m pytest tests/test_compiler_invariants.py -q
不需 live NR/HA——仅校验编译产物的结构不变量（见 compiler_invariants.py）。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                  # 导入 compiler_invariants
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))    # 导入 autoflow_gateway 包

from compiler_invariants import (check_dsl, check_invariants,
                                 all_violations, INVARIANTS)

# ── 已知良好 DSL（与 test_dsl_engine 的 MVP 场景一致，确认编译器已正确实现）──
DSL_HOME = """
场景: 回家开灯与播报
触发: sensor.front_door 有人
动作: light.turn_on(客厅主灯, brightness=80, kelvin=3000)
动作: light.turn_on(玄关灯)
调用子流程: demo_notify(text=欢迎回家, room=客厅)
"""

DSL_SWITCH = """
场景: 有人开灯否则关灯
触发: sensor.motion_living 有人
分支 msg.payload == "有人":
    动作: light.turn_on(客厅主灯)
否则:
    动作: light.turn_off(客厅主灯)
"""

DSL_WEIGH = """
场景: 称重播报
触发: sensor.mi_body_composition_scale_d22e_weight 变化
调用子流程: demo_notify(text=称重完成, room=客厅)
"""

DSL_QUERY = """
场景: 仅灯灭时有人才开灯
触发: binary_sensor.living_motion 有人
查询: light.living_main off
动作: light.turn_on(light.living_main)
"""

DSL_QUERY_ELSE = """
场景: 灯已亮则播报否则忽略
触发: binary_sensor.living_motion 有人
查询: light.living_main on
否则:
    调用子流程: demo_notify(text=灯已亮)
动作: light.turn_on(light.living_main)
"""

DSL_TIME_RANGE = """
场景: 白天开夜灯
触发: sun.sun 日出
时间段: 07:00-23:00
动作: light.turn_on(light.living_main)
"""

DSL_DURATION = """
场景: 书房有人驻留开灯
触发: binary_sensor.study_motion on 持续5分钟
动作: light.turn_on(light.study_desk)
"""

DSL_FIRST = """
场景: 首次感应开灯
触发: binary_sensor.ling_pu 有人 首次
动作: light.turn_on(light.zhuwo)
"""

DSL_READ_EXTRACT = """
场景: 读温湿度并提取
触发: sensor.thermo 变化
取值: sensor.thermo temperature
提取: 温度 = payload.temperature
"""

DSL_DELAY = """
场景: 延时关灯
触发: sensor.motion 无人
延时: 30
动作: light.turn_off(灯)
"""

DSL_IMAGE = """
场景: 视觉能力
触发: 每天 20:00
调用子流程: llm_doubao_image(prompt=`一只猫`)
提取: 图片链接 = payload.reply
"""

DSL_HISTORY = """
场景: 查昨晚空调
触发: inject
调用子流程: history_state_at(entity=climate.书房空调, at=昨晚23:12, attribute=temperature)
提取: 设定温度 = payload.value
"""

DSL_PARALLEL = """
场景: 并行开两灯
触发: sensor.door 有人
并行:
    动作: light.turn_on(灯1)
    动作: light.turn_on(灯2)
"""

DSL_BUILD_HTTP = """
场景: 构建并请求
触发: inject
构建: {"msg":"hi"}
请求: POST https://example.com/api
"""

DSL_COND = """
场景: 条件门控
触发: sensor.motion 有人
条件: light.x = on
动作: light.turn_on(灯)
"""

DSL_TIME_TRIG = """
场景: 定点开灯
触发: 每天 08:00
动作: light.turn_on(灯)
"""

DSL_INJECT = """
场景: 手动开灯
触发: inject
动作: light.turn_on(灯)
"""

DSL_VAR = """
场景: 设置变量后开灯
触发: inject
变量: mode = eco
动作: light.turn_on(灯)
"""

DSL_COMMENT = """
场景: 带注释
触发: inject
注释: 这是说明
动作: light.turn_on(灯)
"""

DSL_DEBUG = """
场景: 观测开灯
触发: inject
动作: light.turn_on(灯)
观测: 看状态
"""

DSL_RAW = """
场景: 原生switch
触发: sensor.motion 有人
原生节点: {"type":"switch","name":"复合","outputs":2,"property":"payload.cond","rules":[{"t":"eq","v":"a"},{"t":"else"}]}
动作: light.turn_on(灯)
"""

# (label, dsl, targets)
SMOKE_CASES = [
    ("home", DSL_HOME, ["staging", "prod"]),
    ("switch", DSL_SWITCH, ["staging", "prod"]),
    ("tts_subflow", DSL_WEIGH, ["staging", "prod"]),
    ("query", DSL_QUERY, ["staging", "prod"]),
    ("query_else", DSL_QUERY_ELSE, ["staging", "prod"]),
    ("time_range", DSL_TIME_RANGE, ["staging", "prod"]),
    ("duration", DSL_DURATION, ["prod"]),
    ("first", DSL_FIRST, ["prod"]),
    ("read_extract", DSL_READ_EXTRACT, ["staging", "prod"]),
    ("delay", DSL_DELAY, ["staging", "prod"]),
    ("image", DSL_IMAGE, ["staging", "prod"]),
    ("history", DSL_HISTORY, ["staging", "prod"]),
    ("parallel", DSL_PARALLEL, ["staging", "prod"]),
    ("build_http", DSL_BUILD_HTTP, ["staging", "prod"]),
    ("cond", DSL_COND, ["staging", "prod"]),
    ("time_trig", DSL_TIME_TRIG, ["staging", "prod"]),
    ("inject", DSL_INJECT, ["staging", "prod"]),
    ("var", DSL_VAR, ["staging", "prod"]),
    ("comment", DSL_COMMENT, ["staging", "prod"]),
    ("debug", DSL_DEBUG, ["staging", "prod"]),
    ("raw", DSL_RAW, ["staging", "prod"]),
]


def test_harness_no_false_positive():
    """已知良好 DSL 在全部 target 下必须零违规：证明 harness 不过度严格。"""
    total = 0
    for label, dsl, targets in SMOKE_CASES:
        for tg in targets:
            total += 1
            res = check_dsl(dsl, target=tg)
            viols = all_violations(res)
            assert not viols, (
                f"[{label}/{tg}] harness 误报 {len(viols)} 条：\n"
                + "\n".join(f"  - {v}" for v in viols)
            )
    assert total >= 20, f"smoke 覆盖不足：{total}"


# ── 矩阵模糊测试（P1）─────────────────────────────────────────────────────
# 原语 × 修饰符 × target 的系统化组合。每条 case 编译后跑全部不变量，
# 任一违规即记为 bug（P3 汇总分类）。修饰符通过字符串模板注入，覆盖：
#   状态触发 state/变化/*、时间触发、持续N分钟/小时/秒、首次、查询/取值
#   分支/否则、时间段、延时、并行、子流程(link_out/实例/http_api)、原生节点、注释、观测
MATRIX = [
    # (label, dsl_factory)  dsl_factory(target) -> dsl 文本
    ("state_on", lambda t: f"场景: x\n触发: sensor.a 有人\n动作: light.turn_on(灯)\n"),
    ("state_off", lambda t: f"场景: x\n触发: sensor.a 无人\n动作: light.turn_off(灯)\n"),
    ("state_change", lambda t: f"场景: x\n触发: sensor.a 变化\n动作: light.turn_on(灯)\n"),
    ("state_value", lambda t: f"场景: x\n触发: sensor.a on\n动作: light.turn_on(灯)\n"),
    ("state_dur_min", lambda t: f"场景: x\n触发: sensor.a on 持续5分钟\n动作: light.turn_on(灯)\n"),
    ("state_dur_hour", lambda t: f"场景: x\n触发: sensor.a on 持续2小时\n动作: light.turn_on(灯)\n"),
    ("state_dur_sec", lambda t: f"场景: x\n触发: sensor.a on 持续30秒\n动作: light.turn_on(灯)\n"),
    ("state_first", lambda t: f"场景: x\n触发: sensor.a 有人 首次\n动作: light.turn_on(灯)\n"),
    ("time_cron", lambda t: "场景: x\n触发: 每天 08:00\n动作: light.turn_on(灯)\n"),
    ("time_cron_wd", lambda t: "场景: x\n触发: 周一至周五 18:30\n动作: light.turn_on(灯)\n"),
    ("inject", lambda t: "场景: x\n触发: inject\n动作: light.turn_on(灯)\n"),
    ("query_on", lambda t: f"场景: x\n触发: sensor.a 有人\n查询: light.b on\n动作: light.turn_on(灯)\n"),
    ("query_off", lambda t: f"场景: x\n触发: sensor.a 有人\n查询: light.b off\n动作: light.turn_on(灯)\n"),
    ("query_else", lambda t: f"场景: x\n触发: sensor.a 有人\n查询: light.b on\n否则:\n    调用子流程: demo_notify(text=亮)\n动作: light.turn_on(灯)\n"),
    ("read_extract", lambda t: f"场景: x\n触发: sensor.a 变化\n取值: sensor.a temperature\n提取: 温度 = payload.temperature\n"),
    ("switch_two", lambda t: f'场景: x\n触发: sensor.a 有人\n分支 msg.payload == "有人":\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n'),
    ("switch_single", lambda t: f'场景: x\n触发: sensor.a 有人\n分支 msg.payload == "有人":\n    动作: light.turn_on(灯)\n'),
    ("switch_num", lambda t: f"场景: x\n触发: sensor.a 有人\n分支 n == 3:\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("switch_bool", lambda t: f"场景: x\n触发: sensor.a 有人\n分支 flag == true:\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("time_range", lambda t: "场景: x\n触发: sun.sun 日出\n时间段: 07:00-23:00\n动作: light.turn_on(灯)\n"),
    ("delay", lambda t: f"场景: x\n触发: sensor.a 无人\n延时: 30\n动作: light.turn_off(灯)\n"),
    ("subflow_linkout", lambda t: f"场景: x\n触发: sensor.a 有人\n调用子流程: demo_notify(text=hi, room=客厅)\n"),
    ("subflow_instance", lambda t: f"场景: x\n触发: sensor.a 变化\n调用子流程: demo_notify(text=hi, room=客厅)\n"),
    ("subflow_httpapi", lambda t: f"场景: x\n触发: 每天 20:00\n调用子流程: llm_doubao_image(prompt=`猫`)\n提取: 图片链接 = payload.reply\n"),
    ("subflow_history", lambda t: f"场景: x\n触发: inject\n调用子流程: history_state_at(entity=climate.x, at=昨晚23:12)\n提取: v = payload.value\n"),
    ("parallel", lambda t: f"场景: x\n触发: sensor.a 有人\n并行:\n    动作: light.turn_on(灯1)\n    动作: light.turn_on(灯2)\n"),
    ("build_http", lambda t: "场景: x\n触发: inject\n构建: {\"m\":\"hi\"}\n请求: POST https://example.com/api\n"),
    ("cond_eq", lambda t: f"场景: x\n触发: sensor.a 有人\n条件: light.b = on\n动作: light.turn_on(灯)\n"),
    ("cond_ne", lambda t: f"场景: x\n触发: sensor.a 有人\n条件: light.b != off\n动作: light.turn_on(灯)\n"),
    ("var", lambda t: f"场景: x\n触发: inject\n变量: mode = eco\n动作: light.turn_on(灯)\n"),
    ("comment", lambda t: f"场景: x\n触发: inject\n注释: 说明\n动作: light.turn_on(灯)\n"),
    ("debug", lambda t: f"场景: x\n触发: inject\n动作: light.turn_on(灯)\n观测: 看状态\n"),
    ("raw_switch", lambda t: f'场景: x\n触发: sensor.a 有人\n原生节点: {{"type":"switch","name":"c","outputs":2,"property":"payload.cond","rules":[{{"t":"eq","v":"a"}},{{"t":"else"}}]}}\n动作: light.turn_on(灯)\n'),
    ("multi_trigger", lambda t: f"场景: x\n触发: sensor.a 有人\n触发: sensor.b 变化\n动作: light.turn_on(灯)\n"),
    ("notify", lambda t: f"场景: x\n触发: inject\n动作: notify.mobile_app(标题=hi)\n"),
    # ── 嵌套 / 组合（R13 孤儿历史重灾区）──
    # 注：DSL 不支持 分支 嵌套在 分支/否则 体内（编译器正确拒绝），故不纳入矩阵。
    ("query_in_branch", lambda t: f"场景: x\n触发: sensor.a 有人\n分支 c == 1:\n    查询: light.b off\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("extract_in_branch", lambda t: f"场景: x\n触发: sensor.a 变化\n取值: sensor.a temp\n分支 temp > 30:\n    提取: 高温 = payload.temp\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("timerange_in_branch", lambda t: "场景: x\n触发: sensor.a 有人\n分支 c == 1:\n    时间段: 07:00-23:00\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("parallel_subflow_delay", lambda t: f"场景: x\n触发: sensor.a 有人\n并行:\n    动作: light.turn_on(灯1)\n    延时: 10\n    调用子流程: demo_notify(text=hi, room=客厅)\n动作: light.turn_on(灯2)\n"),
    ("kitchen_sink", lambda t: f"场景: x\n触发: sensor.a 有人\n查询: light.b off\n分支 c == 1:\n    动作: light.turn_on(灯1)\n    延时: 5\n    调用子流程: demo_notify(text=hi, room=客厅)\n否则:\n    动作: light.turn_on(灯2)\n取值: sensor.a temp\n提取: 温度 = payload.temp\n动作: light.turn_on(灯)\n观测: 看状态\n"),
    ("multi_trigger_var_subflow", lambda t: f"场景: x\n触发: sensor.a 有人\n触发: sensor.b 变化\n变量: mode = eco\n调用子流程: demo_notify(text=hi, room=客厅)\n动作: light.turn_on(灯)\n"),
    ("subflow_httpapi_multi", lambda t: f"场景: x\n触发: 每天 20:00\n调用子流程: llm_doubao_image(prompt=`猫`)\n提取: 图片链接 = payload.reply\n提取: 第二 = payload.reply\n动作: light.turn_on(灯)\n"),
    ("raw_in_branch", lambda t: f'场景: x\n触发: sensor.a 有人\n分支 c == 1:\n    原生节点: {{"type":"change","name":"设v","rules":[{{"t":"set","p":"payload.v","pt":"msg","to":"1","tot":"num"}}]}}\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n'),
    ("cond_then_branch", lambda t: f"场景: x\n触发: sensor.a 有人\n条件: light.b = on\n分支 c == 1:\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("long_entity", lambda t: f"场景: x\n触发: binary_sensor.study_motion_xyz_abc 有人\n动作: light.turn_on(light.study_main_lamp_01)\n"),
    ("dash_entity", lambda t: f"场景: x\n触发: sensor.mi-body-composition-scale_d22e_weight 变化\n调用子流程: demo_notify(text=hi, room=客厅)\n"),
    ("multi_action_params", lambda t: f"场景: x\n触发: sensor.a 有人\n动作: light.turn_on(灯, brightness=80, kelvin=3000)\n动作: light.turn_on(灯2, brightness=50)\n动作: climate.set_temperature(climate.x, temperature=22)\n"),
    ("switch_three", lambda t: f'场景: x\n触发: sensor.a 有人\n分支 msg.payload == "a":\n    动作: light.turn_on(灯1)\n分支 msg.payload == "b":\n    动作: light.turn_on(灯2)\n否则:\n    动作: light.turn_off(灯)\n'),
    ("read_then_branch", lambda t: f"场景: x\n触发: sensor.a 变化\n取值: sensor.a state\n分支 state == on:\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
    ("double_gate", lambda t: f"场景: x\n触发: sensor.a 有人\n查询: light.b on\n查询: light.c off\n动作: light.turn_on(灯)\n"),
    ("time_range_then_action_branch", lambda t: "场景: x\n触发: sun.sun 日出\n时间段: 07:00-23:00\n分支 c == 1:\n    动作: light.turn_on(灯)\n否则:\n    动作: light.turn_off(灯)\n"),
]


def test_invariants_matrix():
    """矩阵模糊测试：每个 (原语×修饰符) × (staging/prod) 必须零违规。"""
    failures = []
    count = 0
    for label, factory in MATRIX:
        for tg in ("staging", "prod"):
            count += 1
            dsl = factory(tg)
            try:
                res = check_dsl(dsl, target=tg)
            except Exception as e:
                failures.append(f"[{label}/{tg}] 编译抛异常：{type(e).__name__}: {e}")
                continue
            viols = all_violations(res)
            if viols:
                failures.append(
                    f"[{label}/{tg}] {len(viols)} 违规:\n"
                    + "\n".join(f"    - {v}" for v in viols)
                )
    assert not failures, (
        f"矩阵发现 {len(failures)} 个失败（共 {count} case）：\n"
        + "\n".join(failures)
    )


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✅ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    raise SystemExit(1 if failed else 0)
