"""Test deploy_raw + validate_flow_schema (dual-mode)."""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from autoflow_gateway.gateway import Gateway


def test_validate_schema():
    gw = Gateway()

    # 1. Good flow
    good = {"id": "test-1", "label": "ok", "nodes": [
        {"id": "n1", "type": "inject", "z": "1", "wires": [["n2"]]},
        {"id": "n2", "type": "debug", "z": "1", "wires": []},
    ]}
    issues = gw.validate_flow_schema(good)
    errors = [v for v in issues if v["level"] == "error"]
    warnings = [v for v in issues if v["level"] == "warning"]
    print(f"Good flow: {len(errors)} err, {len(warnings)} warn")
    assert len(errors) == 0, f"Unexpected errors: {errors}"

    # 2. POST without body → should error
    bad_body = {"id": "test-2", "label": "no-body", "nodes": [
        {"id": "n1", "type": "http request", "z": "1", "method": "POST",
         "url": "http://example.com", "wires": [["n2"]]},
        {"id": "n2", "type": "debug", "z": "1", "wires": []},
    ]}
    issues = gw.validate_flow_schema(bad_body)
    errors = [v for v in issues if v["level"] == "error"]
    print(f"POST no body: {len(errors)} err")
    assert len(errors) > 0, "Should catch missing POST body"
    assert any("body" in e["message"] for e in errors), f"Error should mention body: {errors}"

    # 3. POST with string body → should error
    str_body = dict(bad_body)
    str_body["nodes"][0]["body"] = '{"model":"x"}'  # String!
    str_body["nodes"][0]["bodyType"] = "json"
    issues = gw.validate_flow_schema(str_body)
    errors = [v for v in issues if v["level"] == "error"]
    print(f"POST string body: {len(errors)} err")
    assert any("字符串" in e.get("message","") or "string" in e.get("message","").lower()
               for e in errors), f"Should catch string body: {errors}"

    # 4. POST with dict body → OK
    ok_body = dict(bad_body)
    ok_body["nodes"][0]["body"] = {"model": "doubao"}
    ok_body["nodes"][0]["bodyType"] = "json"
    issues = gw.validate_flow_schema(ok_body)
    errors = [v for v in issues if v["level"] == "error"]
    print(f"POST dict body: {len(errors)} err")
    assert len(errors) == 0, f"Dict body should be fine: {errors}"

    print("✅ validate_flow_schema all cases passed")


def test_deploy_raw_dry_run():
    """deploy_raw with a simple flow (won't actually hit NR if we skip)."""
    gw = Gateway()
    flow = {
        "id": "lab-test-flow",
        "label": "Lab Test",
        "nodes": [
            {"id": "a1", "type": "inject", "z": "lab-test-flow",
             "payload": "", "payloadType": "date",
             "props": [{"p": "_src", "v": "test", "vt": "str"}],
             "repeat": "", "crontab": "", "once": False,
             "wires": [["a2"]], "x": 100, "y": 100},
            {"id": "a2", "type": "debug", "z": "lab-test-flow",
             "active": True, "tosidebar": True, "console": False,
             "complete": "true", "targetType": "full",
             "wires": [], "x": 300, "y": 100},
        ],
    }

    # Just validate, don't deploy
    validation = gw.validate_flow_schema(flow)
    errors = [v for v in validation if v["level"] == "error"]
    print(f"\ndeploy_raw dry-run: {len(errors)} schema errors")
    assert len(errors) == 0, f"Schema should pass: {errors}"
    print("✅ deploy_raw schema check passed")


def _switch_flow_z1():
    """复现用户贴的 switch 路由 flow：z='1' 占位符 + 短 id n1..n6。"""
    return {
        "id": "1",
        "label": "switch-demo",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "1",
             "payload": '{"cmd":"开灯"}', "payloadType": "json", "wires": [["n2"]]},
            {"id": "n2", "type": "switch", "z": "1",
             "property": "payload.cmd", "propertyType": "msg", "outputs": 3,
             "rules": [{"t": "eq", "v": "开灯", "vt": "str"},
                       {"t": "eq", "v": "关灯", "vt": "str"},
                       {"t": "else", "v": "true", "vt": "jsonata"}],
             "wires": [["n3"], ["n4"], ["n5"]]},
            {"id": "n3", "type": "change", "z": "1",
             "rules": [{"t": "set", "p": "payload.result", "pt": "msg",
                        "to": "灯已开", "tot": "str"}], "wires": [["n6"]]},
            {"id": "n4", "type": "change", "z": "1",
             "rules": [{"t": "set", "p": "payload.result", "pt": "msg",
                        "to": "灯已关", "tot": "str"}], "wires": [["n6"]]},
            {"id": "n5", "type": "change", "z": "1",
             "rules": [{"t": "set", "p": "payload.result", "pt": "msg",
                        "to": "未知指令", "tot": "str"}], "wires": [["n6"]]},
            {"id": "n6", "type": "debug", "z": "1", "wires": []},
        ],
    }


def test_remap_ids_and_z():
    gw = Gateway()
    flow = _switch_flow_z1()
    target = "2c75a1584ad37102"
    new_flow, id_map, had_ph = gw._remap_raw_flow_ids(flow, target)

    assert had_ph is True, "应检测到占位符 z='1'"
    new_ids = [n["id"] for n in new_flow["nodes"]]
    assert len(set(new_ids)) == len(new_ids), f"节点 id 必须唯一: {new_ids}"
    assert all(i.startswith("rw") for i in new_ids), f"id 应重映射: {new_ids}"
    assert all(n["z"] == target for n in new_flow["nodes"]), "所有 z 应重写为 target"
    # 关键：NR 会静默丢弃无 x/y 的节点，白盒必须补坐标
    assert all(("x" in n and "y" in n) for n in new_flow["nodes"]), "所有节点必须有 x/y 坐标"

    # wires 引用必须同步改写
    by_old = {n["id"]: n for n in flow["nodes"]}
    for old_n, new_n in zip(flow["nodes"], new_flow["nodes"]):
        old_wires = old_n.get("wires", [])
        new_wires = new_n.get("wires", [])
        flat_old = [w for grp in old_wires for w in grp]
        flat_new = [w for grp in new_wires for w in grp]
        assert [id_map[o] for o in flat_old] == flat_new, \
            f"wires 未同步: {flat_old} -> {flat_new}"
    print("✅ _remap_raw_flow_ids: z 重写 + id 重映射 + wires 同步 全部正确")


def test_placeholder_z_validation():
    gw = Gateway()
    flow = _switch_flow_z1()
    issues = gw.validate_flow_schema(flow)
    warns = [v for v in issues if v["level"] in ("warning", "info")]
    assert any("占位符" in w["message"] for w in warns), \
        f"应提示占位符 z: {warns}"
    print("✅ validate_flow_schema: 能识别占位符 z")


class _FakeNR:
    def __init__(self, live_flow=None):
        self.last_flow = None
        self.last_id = None
        self.live_flow = live_flow  # A8 dry-run：模拟线上现有 flow（供 diff）
    def list_flows(self):
        return []
    def get_default_server_id(self):
        return ""
    def get_flow(self, fid):
        return self.live_flow
    def create_or_update_flow(self, fid, flow_data, force=False):
        self.last_id = fid
        self.last_flow = flow_data
        return {"id": fid, "created": True}


def test_deploy_raw_remaps_before_nr():
    """核心回归：deploy_raw 在落到 NR 前必须已完成 remap，否则真实 NR 会报 duplicate id。"""
    gw = Gateway()
    gw.nr = _FakeNR()
    gw.defense.check_write = lambda **k: None  # 跳过防御限制
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    flow = _switch_flow_z1()
    res = gw.deploy_raw(flow, agent_id="deepseek++", target="staging", run_gate=False)

    assert res["ok"], f"deploy_raw 应成功: {res}"
    sent = gw.nr.last_flow
    sent_ids = [n["id"] for n in sent["nodes"]]
    # 关键断言：送出去的节点 id 全部是 rw 前缀（无 n1 这种占位/撞车 id）
    assert all(i.startswith("rw") for i in sent_ids), f"NR 收到的仍是旧 id: {sent_ids}"
    # 关键断言：z 全部指向真实目标 flow id
    assert all(n["z"] == gw.nr.last_id for n in sent["nodes"]), "z 未重写"
    # 断言：wires 引用的都是 remap 后的 id
    all_ids = set(sent_ids)
    for n in sent["nodes"]:
        for grp in n.get("wires", []):
            for w in grp:
                assert w in all_ids, f"wires 引用了不存在的 id {w}"
    print(f"✅ deploy_raw 已先 remap 再部署（flow_id={gw.nr.last_id}，"
          f"{len(sent_ids)} 节点全部 remap）— 不会再触发 NR duplicate id")


def _orphan_service_flow():
    """白盒 agent 漏连典型坑：写了 api-call-service 却没接进主链（无入边）。"""
    return {
        "id": "orphan-svc",
        "label": "OrphanSvc",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "orphan-svc", "wires": [["n2"]]},
            {"id": "n2", "type": "debug", "z": "orphan-svc", "wires": []},
            {"id": "n3", "type": "api-call-service", "z": "orphan-svc",
             "server": "REPLACE_WITH_HA_SERVER",
             "action": "light.turn_on", "entity_id": "light.x", "wires": [[]]},
        ],
    }


def test_b3_blocks_orphan_service():
    """B3 采纳：deploy_raw 应阻塞含孤儿 api-call-service(R13) 的流，且不落 NR。"""
    gw = Gateway()
    gw.nr = _FakeNR()
    gw.defense.check_write = lambda **k: None
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    res = gw.deploy_raw(_orphan_service_flow(), agent_id="deepseek++",
                        target="staging", run_gate=False)
    assert res["ok"] is False, f"应被 B3 阻塞: {res}"
    assert res["stage"] == "lint_block", f"应标记为 lint_block: {res}"
    assert gw.nr.last_flow is None, "阻塞后绝不应落到 NR"
    assert any(b.get("rule") == "R13" for b in res["lint"]), "应含 R13 硬伤"
    print("✅ B3：孤儿 api-call-service 被阻塞，未落 NR")


def test_b3_block_is_configurable():
    """B3 可配置：block_on_lint_error=False 时放行（兼容需要强制覆盖的场景）。"""
    gw = Gateway()
    gw.nr = _FakeNR()
    # 自给自足：_FakeNR.get_default_server_id() 返回空，HA 注入依赖 cfg.nr_ha_server_id。
    # 该值来自全局 env(NR_HA_SERVER_ID)，会被兄弟测试在导入期改空且未恢复（#684 根因），
    # 故此处显式置位，使测试聚焦 B3 阻塞逻辑而非 HA 注入。
    gw.cfg.nr_ha_server_id = "test_ha_server"
    gw.defense.check_write = lambda **k: None
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    res = gw.deploy_raw(_orphan_service_flow(), agent_id="deepseek++",
                        target="staging", run_gate=False, block_on_lint_error=False)
    assert res["ok"] is True, f"关闭阻塞后应放行: {res}"
    assert gw.nr.last_flow is not None, "放行后应落到 NR"
    print("✅ B3：block_on_lint_error=False 时孤儿流被放行（配置生效）")


def test_b3_no_block_on_clean_flow():
    """B3 不应误伤：正常无孤儿流照常部署。"""
    gw = Gateway()
    gw.nr = _FakeNR()
    gw.defense.check_write = lambda **k: None
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    flow = _switch_flow_z1()
    res = gw.deploy_raw(flow, agent_id="deepseek++", target="staging", run_gate=False)
    assert res["ok"] is True, f"干净流应成功: {res}"
    print("✅ B3：干净流不被误伤")


def test_remap_unique_across_deploys():
    """回归：同一 flow 重复部署，两次产生的节点 id 集合必须不相交（否则 NR duplicate id）。"""
    gw = Gateway()
    flow = _switch_flow_z1()
    _, m1, _ = gw._remap_raw_flow_ids(flow, secrets_token())
    _, m2, _ = gw._remap_raw_flow_ids(flow, secrets_token())
    set1, set2 = set(m1.values()), set(m2.values())
    assert set1.isdisjoint(set2), f"两次部署节点 id 撞车: {set1 & set2}"
    print(f"✅ 跨次部署节点 id 不相交（首次 {len(set1)} 个 / 二次 {len(set2)} 个）— 杜绝 duplicate id")


def secrets_token():
    import secrets as _s
    return "abcdef0123456789" + _s.token_hex(4)  # 16-hex 合法 NR flow id


# ── A8：deploy dry-run / diff 预览（部署前预览增删改，绝不落 NR）──

def test_build_node_diff_create():
    """_build_node_diff：live=None 时全部节点为 added。"""
    from autoflow_gateway.gateway import _build_node_diff
    flow = _switch_flow_z1()
    d = _build_node_diff(None, flow)
    assert d["removed"] == [] and d["changed"] == []
    # 6 个业务节点（无 tab），全部 added
    assert len(d["added"]) == 6, f"应全为 added: {d}"
    assert "新建" in d["report"]
    print("✅ _build_node_diff：新建场景全部计入 added")


def test_deploy_raw_dry_run_create_no_write():
    """dry_run 新建：返回预览、would=create，且绝不落 NR。"""
    gw = Gateway()
    gw.nr = _FakeNR()  # get_flow → None（无线上）
    gw.defense.check_write = lambda **k: None
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    res = gw.deploy_raw(_switch_flow_z1(), agent_id="deepseek++",
                        target="staging", run_gate=False, dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True, res
    assert res["would"] == "create", res
    assert len(res["node_diff"]["added"]) == 6, res["node_diff"]
    assert res["would_block_on_lint"] is False
    assert "_trace_id" in res
    # 关键：dry-run 绝不写 NR
    assert gw.nr.last_flow is None, "dry-run 不应落 NR"
    print("✅ deploy_raw dry-run(create)：返回预览且未落 NR")


def test_deploy_raw_dry_run_update_diff():
    """dry_run 更新：给 target_flow_id + 线上现有 flow，diff 出增/删/改。"""
    live = {
        "id": "lab-live", "label": "switch-demo",
        "nodes": [
            {"id": "L1", "type": "inject", "z": "lab-live", "wires": [["L2"]]},
            {"id": "L2", "type": "debug", "z": "lab-live", "wires": []},
            {"id": "L9", "type": "comment", "z": "lab-live", "name": "老节点将被删", "wires": []},
        ],
    }
    gw = Gateway()
    gw.nr = _FakeNR(live_flow=live)
    gw.defense.check_write = lambda **k: None
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    res = gw.deploy_raw(_switch_flow_z1(), agent_id="deepseek++",
                        target_flow_id="lab-live", run_gate=False, dry_run=True)
    assert res["ok"] and res["dry_run"] and res["would"] == "update", res
    nd = res["node_diff"]
    # 新流有 switch/change 系列 → added 非空；老流的 comment 未出现在新流 → removed 命中
    assert len(nd["added"]) > 0, nd
    assert any(x["type"] == "comment" for x in nd["removed"]), f"应检出被删的 comment: {nd}"
    assert gw.nr.last_flow is None, "dry-run 不应落 NR"
    print("✅ deploy_raw dry-run(update)：正确 diff 出增/删/改，未落 NR")


def test_deploy_raw_dry_run_reports_would_block():
    """dry_run 对含 R13 硬伤的流不早退，改为报 would_block_on_lint=True，且不落 NR。"""
    gw = Gateway()
    gw.nr = _FakeNR()
    # 同 test_b3_block_is_configurable：显式置位 HA server，隔离全局 env 污染（#684）。
    gw.cfg.nr_ha_server_id = "test_ha_server"
    gw.defense.check_write = lambda **k: None
    gw.state.get_flow_catalog = lambda: {"flows": {}}

    res = gw.deploy_raw(_orphan_service_flow(), agent_id="deepseek++",
                        target="staging", run_gate=False, dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True, res
    assert res["would_block_on_lint"] is True, res
    assert "R13" in res["would_block_rules"], res
    assert gw.nr.last_flow is None, "dry-run 即便会被拦也不落 NR"
    print("✅ deploy_raw dry-run：预告 would_block(R13)，未落 NR")


if __name__ == "__main__":
    test_validate_schema()
    test_deploy_raw_dry_run()
    test_remap_ids_and_z()
    test_placeholder_z_validation()
    test_deploy_raw_remaps_before_nr()
    test_remap_unique_across_deploys()
    test_b3_blocks_orphan_service()
    test_b3_block_is_configurable()
    test_b3_no_block_on_clean_flow()
    test_build_node_diff_create()
    test_deploy_raw_dry_run_create_no_write()
    test_deploy_raw_dry_run_update_diff()
    test_deploy_raw_dry_run_reports_would_block()
    print("\n🎉 All dual-mode tests passed!")
