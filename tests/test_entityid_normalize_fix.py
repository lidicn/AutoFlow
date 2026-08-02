# -*- coding: utf-8 -*-
"""复现 + 回归：deploy_raw 归一化(_normalize_flow)静默丢失 entityId 的 bug。

根因（详见 docs/autoflow_study_scene_entityid_report.md）：
- api-current-state：_normalize_api_state 只从 entities(数组)抽 entity_id，
  完全无视 entityId(camelCase 字符串)；且写死 version=3，而 v3 读 entity_id
  而非 entityId → 白箱交 {entityId:"sensor.x"} 被归一化后绑定丢失。
- api-call-service：_normalize_flow 此前根本不归一化该类型；白箱交
  {version:5, entityId:"light.x"(字符串)} 原样交给 0.80.3 迁移 → 空数组。

修复后：两类节点无论写成 entityId(camelCase)/entity_id(snake)/entities(数组)，
实体绑定都须保留（归一到编译器契约形态：api-current-state entityId=str+v7、
api-call-service entityId=array+v7），确保落 0.80.3 后绑定不丢。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway.lib.nr_client import NodeRedClient


def _mk():
    return NodeRedClient.__new__(NodeRedClient)


def test_api_current_state_preserves_entityId_camelCase():
    """白箱交 {entityId:'sensor.x'}（网关 NR5 契约形态）→ 归一化后绑定不丢。
    0.80.3 的 api-current-state 在旧版读 entity_id，故须同时落到 entity_id。"""
    n = {
        "id": "n_temp", "type": "api-current-state", "z": "f1",
        "name": "查询温度", "server": "s1",
        "entityId": "sensor.study_temperature",   # camelCase，v7/NR5 契约
        "halt_if": "", "outputs": 1,
    }
    NodeRedClient._normalize_api_state(n)
    # 绑定必须保留（两种字段名都写到，兼容不同 ha-websocket 版本）
    assert n.get("entityId") == "sensor.study_temperature", n
    assert n.get("entity_id") == "sensor.study_temperature", n
    # 不应被锁死在旧版 v3（v3 才是读 entity_id 的版本；这里用 v7 与编译器契约一致）
    assert n.get("version") == 7, n


def test_api_current_state_gate_node_keeps_two_outputs():
    """回归：双输出门节点(查询门)不被归一化误改单输出（防 R10）。"""
    n = {
        "id": "g1", "type": "api-current-state", "z": "f1",
        "name": "查询 电脑", "server": "s1",
        "entityId": "binary_sensor.computer_power",
        "halt_if": "on", "halt_if_type": "str", "halt_if_compare": "is",
        "outputs": 2, "wires": [["a"], ["b"]],
    }
    NodeRedClient._normalize_api_state(n)
    assert n["outputs"] == 2, n
    assert n["wires"] == [["a"], ["b"]], n
    assert n.get("entityId") == "binary_sensor.computer_power", n
    assert n.get("entity_id") == "binary_sensor.computer_power", n


def test_api_call_service_string_entityId_normalized_to_array():
    """白箱交 {version:5, entityId:'light.x'(字符串)} → 归一成 v7+数组，
    避免 0.80.3 迁移把值弄丢（空数组）。"""
    n = {
        "id": "n_light", "type": "api-call-service", "z": "f1",
        "name": "开灯", "server": "s1",
        "domain": "light", "service": "turn_on",
        "version": 5, "entityId": "light.study_main",   # 字符串（老 v5 形态）
        "data": "{}", "dataType": "json",
    }
    NodeRedClient._normalize_api_call_service(n)
    assert n["entityId"] == ["light.study_main"], n
    assert n["version"] == 7, n


def test_api_call_service_array_entityId_untouched():
    """编译器形态（v7 + 数组 entityId）归一化后不变。"""
    n = {
        "id": "n2", "type": "api-call-service", "z": "f1",
        "name": "开灯2", "server": "s1",
        "domain": "light", "service": "turn_on",
        "version": 7, "entityId": ["light.study_main"],
        "data": "{}", "dataType": "json",
    }
    NodeRedClient._normalize_api_call_service(n)
    assert n["entityId"] == ["light.study_main"], n
    assert n["version"] == 7, n


# ── action 契约补全（WB72 Bug#1 / #705，P0）──────────────────
# 旧行为：v5 形态 {domain, service, 无 action} 被升成 version=7 却不补 action，
# 而 v7 强依赖 action → NR 运行时 ValidationError；网关 schema 校验只要求
# 「action 或 domain 二选一」恰好放行 → 静态全绿、运行必炸。
def test_api_call_service_v5_domain_service_derives_action():
    n = {
        "id": "n_v5", "type": "api-call-service", "z": "f1",
        "name": "开灯", "server": "s1",
        "domain": "light", "service": "turn_on",
        "version": 5, "entityId": "light.study_main",
    }
    NodeRedClient._normalize_api_call_service(n)
    assert n["action"] == "light.turn_on", n
    assert n["version"] == 7, n
    # domain/service 保留，v7 与 v5 两种读法都成立
    assert n["domain"] == "light" and n["service"] == "turn_on", n


def test_api_call_service_action_only_backfills_domain_service():
    """只给 action 的 v7 写法 → 反向补 domain/service。"""
    n = {
        "id": "n_v7", "type": "api-call-service", "z": "f1",
        "server": "s1", "action": "switch.toggle",
        "entityId": ["switch.desk"],
    }
    NodeRedClient._normalize_api_call_service(n)
    assert n["domain"] == "switch" and n["service"] == "toggle", n
    assert n["action"] == "switch.toggle", n
    assert n["version"] == 7, n


def test_api_call_service_existing_action_not_overwritten():
    """已有 action 与 domain/service 不一致时以 action 为准，不改写。"""
    n = {
        "id": "n_mix", "type": "api-call-service", "z": "f1",
        "server": "s1", "action": "light.turn_off",
        "domain": "light", "service": "turn_on", "version": 5,
    }
    NodeRedClient._normalize_api_call_service(n)
    assert n["action"] == "light.turn_off", n
    assert n["service"] == "turn_on", n  # 原字段不动，交由 NR 以 action 为准


def test_api_call_service_no_action_derivable_keeps_version():
    """推不出 action（domain/service 都缺）→ 不强行升 v7，避免造非法节点。"""
    n = {
        "id": "n_bad", "type": "api-call-service", "z": "f1",
        "server": "s1", "version": 5, "entityId": "light.x",
    }
    NodeRedClient._normalize_api_call_service(n)
    assert "action" not in n, n
    assert n["version"] == 5, n
    assert n["entityId"] == ["light.x"], n  # entityId 归一化照常生效


def test_normalize_flow_runs_api_call_service():
    """_normalize_flow 应对 api-call-service 节点调用归一化（此前完全漏处理）。"""
    flow = {
        "id": "f1", "type": "tab", "label": "t",
        "nodes": [
            {"id": "inj", "type": "inject", "z": "f1", "wires": [["acs"]]},
            {"id": "acs", "type": "api-call-service", "z": "f1",
             "server": "s1", "domain": "light", "service": "turn_on",
             "version": 5, "entityId": "light.study_main",
             "data": "{}", "dataType": "json", "wires": [[]]},
        ],
    }
    NodeRedClient._normalize_flow(flow)
    acs = [x for x in flow["nodes"] if x["type"] == "api-call-service"][0]
    assert acs["entityId"] == ["light.study_main"], acs
    assert acs["version"] == 7, acs


# ── #585 扩展：全 HA 实体绑定节点归一化（防跨版本迁移吞 entity） ──────────
# 覆盖：trigger-state / server-state-changed（entities.entity 改名型）
#       api-get-history / poll-state / events-state / wait-until（entityId 稳定型）

def test_trigger_state_v1_entityid_normalized_to_v5_entities():
    """白箱交 trigger-state v1 + entityId 字符串 → 归一到 v5 +
    entities.entity 填实，杜绝 NR 迁移变 [null]（G 节点回归，见 reverify 报告）。"""
    n = {
        "id": "g1", "type": "trigger-state", "z": "f1", "name": "G", "server": "s1",
        "version": 1, "entityId": "sensor.temp_p_2_1001",
        "entityidfiltertype": "exact", "outputs": 1,
        "constraints": [{"id": "c1", "comparatorValue": "unavailable"}],
    }
    NodeRedClient._normalize_trigger_state(n)
    assert n["version"] == 5, n
    assert n["entities"]["entity"] == ["sensor.temp_p_2_1001"], n
    assert "entityId" not in n, n
    assert "entityidfiltertype" not in n, n


def test_trigger_state_substring_filtertype_lands_in_substring_bucket():
    """entityidfiltertype=substring → 落到 entities.substring 而非 entity。"""
    n = {
        "id": "g2", "type": "trigger-state", "z": "f1", "server": "s1",
        "version": 1, "entityId": "motion", "entityidfiltertype": "substring",
        "outputs": 1,
    }
    NodeRedClient._normalize_trigger_state(n)
    assert n["entities"]["substring"] == ["motion"], n
    assert n["entities"]["entity"] == [], n


def test_trigger_state_already_v5_untouched():
    """已是 v5 且 entities.entity 填实 → 原样不动（保留用户结构）。"""
    n = {
        "id": "g3", "type": "trigger-state", "z": "f1", "server": "s1",
        "version": 5,
        "entities": {"entity": ["sensor.x"], "substring": [], "regex": []},
        "outputs": 1,
    }
    NodeRedClient._normalize_trigger_state(n)
    assert n["version"] == 5, n
    assert n["entities"]["entity"] == ["sensor.x"], n


def test_trigger_v1_probe_mirrors_blindspot_report():
    """闭环盲区报告：精确复刻 docs/autoflow_trigger_v1_regression_blindspot_report.md
    的补测探针 pr_058615e885fc —— 同一导出内放 A(v1+entityId 字符串, 逼 v1→v5 升级)
    与 B(v5 直写控制)，经 _normalize_flow（deploy_raw / deploy_proposal 共用的归一化
    入口，create_or_update_flow 第 852 行调用）后：
      - A 的 entities.entity 必须是非 null 的真实值（回归判定 ✅），而非 [null]（❌ 回归仍在）
      - B 的 v5 直写控制节点原样保留
    这条测试把报告里"待用户手动 apply+导出+跑脚本"的验证盲区，固化成可自动化复跑的回归。
    """
    TEMP = "sensor.duka.temp_p_2_1001"
    flow = {
        "id": "f1", "type": "tab", "label": "trigger-v1-probe", "nodes": [
            # 起始 inject
            {"id": "inj", "type": "inject", "z": "f1", "wires": [["A", "B"]]},
            # A：回归源 v1 + entityId 字符串（无 entityidfiltertype → 默认 exact）
            {"id": "A", "type": "trigger-state", "z": "f1", "name": "A-v1",
             "server": "s1", "version": 1, "entityId": TEMP,
             "outputs": 1, "wires": [["dbg_v1"]]},
            # B：控制 v5 直写
            {"id": "B", "type": "trigger-state", "z": "f1", "name": "B-v5",
             "server": "s1", "version": 5,
             "entities": {"entity": [TEMP], "substring": [], "regex": []},
             "outputs": 1, "wires": [["dbg_v5"]]},
            {"id": "dbg_v1", "type": "debug", "z": "f1", "wires": []},
            {"id": "dbg_v5", "type": "debug", "z": "f1", "wires": []},
        ],
    }
    NodeRedClient._normalize_flow(flow)  # 与 deploy 路径同一入口
    by = {x["id"]: x for x in flow["nodes"]}
    # A：v1→v5 升级必须保留实体值（非 [null]）
    assert by["A"]["version"] == 5, by["A"]
    assert by["A"]["entities"]["entity"] == [TEMP], by["A"]
    assert "entityId" not in by["A"], by["A"]
    # B：v5 直写控制节点原样保留
    assert by["B"]["version"] == 5, by["B"]
    assert by["B"]["entities"]["entity"] == [TEMP], by["B"]


def test_server_state_changed_v1_entityid_normalized_to_v6():
    """server-state-changed v1 + entityId → 归一到 v6 + entities.entity 填实。"""
    n = {
        "id": "s1n", "type": "server-state-changed", "z": "f1", "server": "s1",
        "version": 1, "entityId": "light.kitchen",
        "entityidfiltertype": "exact", "outputs": 1,
    }
    NodeRedClient._normalize_server_state_changed(n)
    assert n["version"] == 6, n
    assert n["entities"]["entity"] == ["light.kitchen"], n
    assert "entityId" not in n, n


def test_entity_id_str_nodes_preserve_binding():
    """api-get-history / poll-state / events-state / wait-until：
    跨版本字段名稳定，只需保底 entityId 字符串不丢。"""
    for t in ("api-get-history", "poll-state", "events-state", "wait-until"):
        n = {
            "id": "e1", "type": t, "z": "f1", "server": "s1",
            "version": 1, "entityId": "sensor.x", "outputs": 1,
        }
        NodeRedClient._normalize_entity_id_str_node(n)
        assert n["entityId"] == "sensor.x", (t, n)
        assert "entity_id" not in n, (t, n)


def test_entity_id_str_node_from_snake_entity_id():
    """旧写法 entity_id(snake) 也归一到 entityId 字符串。"""
    n = {"id": "e2", "type": "poll-state", "z": "f1", "server": "s1",
         "version": 1, "entity_id": "sensor.y", "outputs": 1}
    NodeRedClient._normalize_entity_id_str_node(n)
    assert n["entityId"] == "sensor.y", n


def test_normalize_flow_covers_all_ha_entity_binding_types():
    """_normalize_flow 应覆盖全部 8 类实体绑定 HA 节点：
    旧形态 → 最新版且绑定不丢（实体绑定跨版本归一化闭环）。"""
    flow = {"id": "f1", "type": "tab", "label": "t", "nodes": [
        {"id": "inj", "type": "inject", "z": "f1", "wires": [["acs"]]},
        # 1 api-call-service v5 字符串
        {"id": "acs", "type": "api-call-service", "z": "f1", "server": "s1",
         "domain": "light", "service": "turn_on", "version": 5,
         "entityId": "light.a", "data": "{}", "dataType": "json", "wires": [[]]},
        # 2 api-current-state v1 entityId
        {"id": "acs2", "type": "api-current-state", "z": "f1", "server": "s1",
         "entityId": "sensor.b", "outputs": 1, "wires": [[]]},
        # 3 trigger-state v1 entityId exact
        {"id": "ts", "type": "trigger-state", "z": "f1", "server": "s1",
         "version": 1, "entityId": "sensor.c", "entityidfiltertype": "exact",
         "outputs": 1, "wires": [[]]},
        # 4 server-state-changed v1 entityId
        {"id": "ssc", "type": "server-state-changed", "z": "f1", "server": "s1",
         "version": 1, "entityId": "sensor.d", "entityidfiltertype": "exact",
         "outputs": 1, "wires": [[]]},
        # 5 api-get-history v1 entityId
        {"id": "gh", "type": "api-get-history", "z": "f1", "server": "s1",
         "version": 1, "entityId": "sensor.e", "outputs": 1, "wires": [[]]},
        # 6 poll-state v1 entityId
        {"id": "ps", "type": "poll-state", "z": "f1", "server": "s1",
         "version": 1, "entityId": "sensor.f", "outputs": 1, "wires": [[]]},
        # 7 events-state v1 entityId
        {"id": "es", "type": "events-state", "z": "f1", "server": "s1",
         "version": 1, "entityId": "sensor.g", "outputs": 1, "wires": [[]]},
        # 8 wait-until v1 entityId
        {"id": "wu", "type": "wait-until", "z": "f1", "server": "s1",
         "version": 1, "entityId": "sensor.h", "outputs": 1, "wires": [[]]},
    ]}
    NodeRedClient._normalize_flow(flow)
    by = {x["id"]: x for x in flow["nodes"]}
    # 1 api-call-service → 数组 + v7
    assert by["acs"]["entityId"] == ["light.a"], by["acs"]
    assert by["acs"]["version"] == 7, by["acs"]
    # 2 api-current-state → 双字段 + v7
    assert by["acs2"]["entityId"] == "sensor.b", by["acs2"]
    assert by["acs2"]["entity_id"] == "sensor.b", by["acs2"]
    # 3 trigger-state → entities.entity + v5
    assert by["ts"]["entities"]["entity"] == ["sensor.c"], by["ts"]
    assert by["ts"]["version"] == 5, by["ts"]
    # 4 server-state-changed → entities.entity + v6
    assert by["ssc"]["entities"]["entity"] == ["sensor.d"], by["ssc"]
    assert by["ssc"]["version"] == 6, by["ssc"]
    # 5-8 entityId 字符串保留
    expected = {"gh": "sensor.e", "ps": "sensor.f", "es": "sensor.g", "wu": "sensor.h"}
    for nid, exp in expected.items():
        assert by[nid]["entityId"] == exp, (nid, by[nid])


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
