"""Task #100 (dev/dsl-round4) — 十处漏网 bug 的复现固化测试。

覆盖工单 R1..R10 的验收点。每一条都打在**真源**（dsl_engine / flow_linter /
gateway / debug_bridge），不是只写进测试生成器。

  R1  int 参数裸 ValueError → 可读「参数名+类型+合法示例」错误（C_SUBFLOW_ARG）
  R2  子流程 ${} 模板未插值 → 编译产物里变量已被替换
  R3  debug 桥断连（恒 connected:false）→ 默认裸 ws 握手 + 运行期可 retarget
  R4  并行块错挂 switch 首分支 → 并行块回挂到上游，不接 switch 的「命中」输出
  R5  反引号 JSONata 插值未编译 → 反引号内 ${} 被插值、反引号被剥离
  R6  分支:false/true 恒真假 → lint R35 警告（fail-open，不阻塞）
  R7  link-out 异步子流程后接提取挂错侧 → 编译器编成副链；手搓错侧触发 R37
  R8  提取字段无存在性/自赋值校验 → 编译期 C_EXTRACT_* 警告 + lint R36
  R9  deploy schema error 不阻断部署 → S1..S5 致命项硬拦 + 自检 will_deploy_block
  R10 decision_id 错位静默闭环断 → request_decision 读回自检，错位即 ok=False
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import compile_dsl, parse, validate, DSLError
from autoflow_gateway.flow_linter import lint_flow
from autoflow_gateway.gateway import Gateway, schema_blocking_issues
from autoflow_gateway.debug_bridge import DebugBridge


# ── 共享桩 ────────────────────────────────────────────────────────────────

class _FakeNR:
    """最小 NR 桩：记录最后一次收到的 flow，用于断言「致命 schema 未落 NR」。"""

    def __init__(self):
        self.last_flow = None
        self.last_id = None

    def list_flows(self):
        return []

    def get_default_server_id(self):
        return ""

    def get_flow(self, fid):
        return None

    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        self.last_id = fid
        self.last_flow = flow_data
        return {"id": fid, "created": True}


def _gw(monkeypatch, tmp_path):
    """构造一个不触网的 Gateway：侧车文件落 tmp_path，推送/Bark 行为被noop。"""
    monkeypatch.chdir(tmp_path)
    gw = Gateway()
    gw.nr = _FakeNR()
    gw.defense.check_write = lambda **k: None
    if hasattr(gw, "state"):
        try:
            gw.state.get_flow_catalog = lambda: {"flows": {}}
        except Exception:
            pass
    gw._bark_push = lambda *a, **k: None  # R10：避免后台 Bark 线程触网
    return gw


# ── R1：int 参数校验可读化 ────────────────────────────────────────────────

def test_R1_int_param_readable_error():
    """bark_push(bark_badge=abc)：不得抛裸 int('abc')，必须带参数名+示例。"""
    dsl = (
        "场景: R1\n"
        "触发: sensor.a 变化\n"
        "调用子流程: bark_push(title=t, body=b, bark_badge=abc)\n"
    )
    with pytest.raises(DSLError) as exc:
        compile_dsl(dsl)
    e = exc.value
    assert e.code == "C_SUBFLOW_ARG", f"应带 C_SUBFLOW_ARG 码，实际 {getattr(e,'code',None)}"
    assert e.line == 3, f"应带 DSL 行号 3，实际 {getattr(e,'line',None)}"
    msg = str(e)
    assert "bark_badge" in msg, "错误信息必须点名出错参数"
    assert "42" in msg, "必须给出合法示例"


def test_R1_valid_int_passes():
    """合法 int 不应报错。"""
    dsl = (
        "场景: R1c\n"
        "触发: sensor.a 变化\n"
        "调用子流程: bark_push(title=t, body=b, bark_badge=7)\n"
    )
    flow = compile_dsl(dsl)  # 不应抛
    assert flow and flow.get("nodes")


# ── R2：${} 模板插值 ──────────────────────────────────────────────────────

def test_R2_subflow_template_interpolation():
    """子流程参数里的 ${阈值} 必须被插值成实际值。"""
    dsl = (
        "场景: R2\n"
        "触发: sensor.a 变化\n"
        "变量: 阈值=30\n"
        "调用子流程: demo_notify(text=阈值 ${阈值} 度)\n"
    )
    flow = compile_dsl(dsl)
    by = {n["id"]: n for n in flow["nodes"]}
    set_node = next(n for n in flow["nodes"] if "入参" in (n.get("name") or ""))
    payload_rule = set_node["rules"][0]
    assert "阈值 30 度" in payload_rule["to"], f"${'阈值'} 未插值：{payload_rule['to']}"


# ── R3：debug 桥 ──────────────────────────────────────────────────────────

def test_R3_bare_handshake_by_default():
    """R3 根因：NR5 /comms 不接受 Authorization 头。默认必须裸握手（无 Bearer）。"""
    bridge = DebugBridge(nr_client=object(), nr_url="http://localhost:1880")
    bare = bridge._handshake_request("localhost", 1880, "/comms", None)
    assert b"Authorization" not in bare, "默认握手不应带 Authorization 头"
    with_tok = bridge._handshake_request("localhost", 1880, "/comms", "SECRET")
    assert b"Authorization: Bearer SECRET" in with_tok, "带 token 时才回退 Bearer"


def test_R3_retarget_changes_url():
    """运行期改 NR_URL：换址应生效并返回 True；同址应返回 False（不重复踹断）。"""
    bridge = DebugBridge(nr_client=object(), nr_url="http://localhost:1880")
    old = bridge.ws_url
    assert bridge.retarget("http://localhost:1880") is False
    assert bridge.retarget("http://localhost:1990") is True
    assert bridge.ws_url != old
    assert "1990" in bridge.ws_url


# ── R4：并行块接线 ────────────────────────────────────────────────────────

def test_R4_parallel_block_hangs_on_upstream_not_switch():
    """分支后的并行块必须回挂到触发（上游），绝不挂 switch 的「命中」输出。"""
    dsl = (
        "场景: R4\n"
        "触发: sensor.a 变化\n"
        "分支 payload == \"有人\":\n"
        "    动作: light.turn_on(客厅主灯)\n"
        "否则:\n"
        "    动作: light.turn_off(客厅主灯)\n"
        "并行:\n"
        "    动作: switch.turn_on(风扇)\n"
        "    动作: switch.turn_on(加湿器)\n"
    )
    flow = compile_dsl(dsl)
    by = {n["id"]: n for n in flow["nodes"]}
    trigger = next(n for n in flow["nodes"] if n["type"] == "server-state-changed")
    switch = next(n for n in flow["nodes"] if n["type"] == "switch")
    parallel_ids = {n["id"] for n in flow["nodes"]
                    if n["type"] == "api-call-service" and "switch" in (n.get("name") or "")}
    trig_wires = {w for grp in trigger["wires"] for w in grp}
    # 触发同时直连 switch 与两个并行节点 → 并行块不挂在 switch 之后
    assert switch["id"] in trig_wires, "触发应直连分支(switch)"
    assert parallel_ids <= trig_wires, f"并行块应直挂触发，实际 {parallel_ids} 不在 {trig_wires}"
    # switch 的两条分支输出必须正确（不串到并行块）
    sw_wires = [w for grp in switch["wires"] for w in grp]
    sw_targets = {by[w]["name"] for w in sw_wires}
    assert {"light.turn_on", "light.turn_off"} == sw_targets, f"switch 分支错位：{sw_targets}"


# ── R5：反引号插值 ────────────────────────────────────────────────────────

def test_R5_backtick_jsonata_interpolation():
    """反引号包裹的 JSONata 入参里的 ${阈值} 必须被插值，且反引号被剥离。"""
    dsl = (
        "场景: R5\n"
        "触发: sensor.a 变化\n"
        "变量: 阈值=30\n"
        "调用子流程: demo_notify(text=`阈值 ${阈值} 度`)\n"
    )
    flow = compile_dsl(dsl)
    set_node = next(n for n in flow["nodes"] if "入参" in (n.get("name") or ""))
    payload_rule = set_node["rules"][0]
    assert "阈值 30 度" in payload_rule["to"], f"反引号内 ${'阈值'} 未插值：{payload_rule['to']}"
    assert "`" not in payload_rule["to"], f"反引号应被剥离：{payload_rule['to']}"


# ── R6：常量条件分支 ──────────────────────────────────────────────────────

def test_R6_constant_branch_lint_R35():
    """分支: false / 分支: true 恒真假 → flow_linter 必须产出 R35 警告（fail-open）。"""
    dsl = (
        "场景: R6\n"
        "触发: sensor.a 变化\n"
        "分支 false:\n"
        "    动作: light.turn_on(客厅主灯)\n"
        "分支 true:\n"
        "    动作: light.turn_off(客厅主灯)\n"
    )
    flow = compile_dsl(dsl)
    r35 = [i for i in lint_flow(flow) if i["rule"] == "R35"]
    assert len(r35) >= 2, f"应至少 2 条 R35（false/true 各一），实际 {len(r35)}"


# ── R7：link-out 异步子流程的提取接线 ─────────────────────────────────────

def test_R7_extract_hangs_on_upstream_not_request_side():
    """link-out 型子流程之后的 提取 必须挂到调用「之前」的上游，而非被入参覆写的请求侧。"""
    dsl = (
        "场景: R7\n"
        "触发: sensor.a 变化\n"
        "动作: light.turn_on(客厅主灯)\n"
        "调用子流程: demo_notify(text=称重完成, room=客厅)\n"
        "提取: 温度 = payload.reply\n"
        "动作: light.turn_off(客厅主灯)\n"
    )
    flow = compile_dsl(dsl)
    by = {n["id"]: n for n in flow["nodes"]}
    link_out = next(n for n in flow["nodes"] if n["type"] == "link out")
    extract = next(n for n in flow["nodes"] if "提取" in (n.get("name") or ""))
    # 反向找 extract 的唯一入边来源
    srcs = [nid for nid, n in by.items() if extract["id"] in
            {w for grp in n.get("wires", []) for w in grp}]
    assert srcs, "提取节点必须有人连入"
    src = by[srcs[0]]
    assert src["id"] != link_out["id"], "提取不能挂在 link out（请求侧）之后"
    assert src["type"] == "api-call-service" and "turn_on" in (src.get("name") or ""), \
        f"提取应挂在调用前的上游动作，实际挂在 {src.get('name')}"


def test_R7_lint_R37_wrong_side_handcraft():
    """手搓错侧形状（设参 change 同时接 link out 与取返回值 change）→ R37 警告。"""
    bad = {"id": "t1", "nodes": [
        {"id": "n1", "type": "inject", "z": "t1", "name": "触发", "wires": [["n2"]]},
        {"id": "n2", "type": "change", "z": "t1", "name": "设置 weather 入参",
         "rules": [{"t": "set", "p": "payload", "pt": "msg",
                    "to": '{"city":"北京"}', "tot": "json"}],
         "wires": [["n3", "n4"]]},
        {"id": "n3", "type": "link out", "z": "t1", "name": "→ weather",
         "links": ["af_weather_in"]},
        {"id": "n4", "type": "change", "z": "t1", "name": "取返回值",
         "rules": [{"t": "set", "p": "temp", "pt": "msg",
                    "to": "payload.reply.temp", "tot": "jsonata"}],
         "wires": [[]]},
    ]}
    r37 = [i for i in lint_flow(bad) if i["rule"] == "R37"]
    assert r37, "错侧挂接必须触发 R37"
    assert r37[0]["node_id"] == "n4", "R37 应定位到取返回值节点 n4"


# ── R8：提取字段校验 ──────────────────────────────────────────────────────

def test_R8_self_assign_and_wrong_field_warnings():
    """提取自赋值 → C_EXTRACT_SELF_ASSIGN；http_api 子流程后取错字段 → C_EXTRACT_FIELD_SUSPECT。"""
    dsl = (
        "场景: R8\n"
        "触发: sensor.a 变化\n"
        "调用子流程: llm_doubao_chat(user_msg=hi)\n"
        "提取: payload.reply = payload.reply\n"
        "提取: x = payload.resp\n"
    )
    issues = list(validate(parse(dsl)))
    msgs = {i.message for i in issues}
    assert any("C_EXTRACT_SELF_ASSIGN" in m for m in msgs), \
        f"自赋值应报 C_EXTRACT_SELF_ASSIGN，实际 {[i.message[:60] for i in issues]}"
    assert any("C_EXTRACT_FIELD_SUSPECT" in m for m in msgs), \
        f"错字段应报 C_EXTRACT_FIELD_SUSPECT，实际 {[i.message[:60] for i in issues]}"


def test_R8_lint_R36_handcraft_self_assign():
    """手搓自赋值 change（msg.payload.reply = msg.payload.reply）→ lint R36。"""
    bad = {"id": "t1", "nodes": [
        {"id": "n1", "type": "change", "z": "t1", "name": "取返回值",
         "rules": [{"t": "set", "p": "payload.reply", "pt": "msg",
                    "to": "payload.reply", "tot": "jsonata"}], "wires": [[]]},
    ]}
    r36 = [i for i in lint_flow(bad) if i["rule"] == "R36"]
    assert r36, "自赋值 change 必须触发 R36"


def test_R8_llm_no_auto_self_assign_node():
    """A28：llm_* 子流程编译后不应再自动产出自赋值空节点（R36 应为 0）。"""
    flow = compile_dsl(
        "场景: R8b\n触发: sensor.a 变化\n调用子流程: llm_doubao_chat(user_msg=hi)\n"
    )
    r36 = [i for i in lint_flow(flow) if i["rule"] == "R36"]
    assert r36 == [], "llm_* 编译产物不应含自赋值空节点"


# ── R9：部署 schema 致命错误硬拦 ──────────────────────────────────────────

def _ha_missing_server_flow():
    """api-call-service 缺 server → S3 致命项。"""
    return {
        "id": "bad-ha", "label": "BadHA",
        "nodes": [
            {"id": "n1", "type": "inject", "z": "bad-ha", "wires": [["n2"]]},
            {"id": "n2", "type": "api-call-service", "z": "bad-ha",
             "domain": "light", "service": "turn_on",
             "data": '{"entity_id":"light.x"}', "wires": [[]]},
        ],
    }


def test_R9_schema_rule_codes():
    """validate_flow_schema 的致命项必须带 S1..S5 码。"""
    # S2：缺 type
    s2 = Gateway().validate_flow_schema(
        {"id": "x", "label": "x", "nodes": [{"id": "n1", "z": "x", "wires": []}]})
    assert any(v.get("rule") == "S2" for v in s2), f"S2 未命中：{s2}"
    # S3：HA 节点缺 server
    s3 = Gateway().validate_flow_schema(_ha_missing_server_flow())
    assert any(v.get("rule") == "S3" for v in s3), f"S3 未命中：{s3}"
    # S5：空 nodes
    s5 = Gateway().validate_flow_schema({"id": "x", "label": "x", "nodes": []})
    assert any(v.get("rule") == "S5" for v in s5), f"S5 未命中：{s5}"


def test_R9_schema_blocking_issues_helper():
    """schema_blocking_issues 只提取 S1..S5，且只看 level==error。"""
    issues = [
        {"level": "error", "rule": "S3", "node_id": "n2"},
        {"level": "error", "rule": "R13", "node_id": "n3"},  # 非 S 码
        {"level": "warning", "rule": "S4", "node_id": "n4"},  # warning 不拦
        {"level": "error", "rule": "S1", "node_id": "_root"},
    ]
    blk = schema_blocking_issues(issues)
    rules = {b["rule"] for b in blk}
    assert rules == {"S3", "S1"}, f"应只收 S 码 error，实际 {rules}"


def test_R9_deploy_raw_blocks_fatal_schema(monkeypatch, tmp_path):
    """致命 schema（S3）必须阻止 deploy_raw 落到 NR，返回 stage=schema_block。"""
    gw = _gw(monkeypatch, tmp_path)
    res = gw.deploy_raw(_ha_missing_server_flow(), agent_id="t", target="staging",
                         run_gate=False)
    assert res["ok"] is False, f"致命 schema 应阻止部署：{res}"
    assert res["stage"] == "schema_block", f"stage 应为 schema_block，实际 {res.get('stage')}"
    assert "S3" in res.get("schema_blocking_rules", []), "应报告 S3"
    assert gw.nr.last_flow is None, "致命 schema 下 NR 绝不能被写入"


def test_R9_propose_raw_reports_schema_block(monkeypatch, tmp_path):
    """propose_raw 对致命 schema 必须 node_gate_ok=False + will_block_on_schema=True。"""
    gw = _gw(monkeypatch, tmp_path)
    res = gw.propose_raw(_ha_missing_server_flow(), agent_id="t", target="staging",
                         run_gate=False)
    assert res.get("node_gate_ok") is False, f"致命 schema 下 node_gate_ok 应 False：{res}"
    assert res.get("would_block_on_schema") is True, "应报 would_block_on_schema"
    _sb = res.get("schema_blocking") or []
    assert any(b.get("rule") == "S3" for b in _sb), f"致命 schema 应含 S3：{_sb}"


# ── R10：decision_id 读回自检 ─────────────────────────────────────────────

class _FakeDecisionStore:
    """模拟 DecisionStore：控制 get 返回的 id 是否与 create 回执一致。"""

    def __init__(self, get_returns_id=None):
        self._id = "dec_test123"
        self.get_returns_id = get_returns_id  # None=一致；否则返回错位的 id

    def create(self, question, options, source="deepseek"):
        return {"id": self._id, "question": question,
                "options": list(options), "status": "pending"}

    def get(self, did):
        rid = self._id if self.get_returns_id is None else self.get_returns_id
        return {"id": rid, "question": "q", "options": ["a"], "status": "pending"}


def test_R10_decision_id_roundtrip_ok(monkeypatch, tmp_path):
    """正常路径：回执 decision_id 能从库读回 → ok=True 且 decision_id 一致。"""
    gw = _gw(monkeypatch, tmp_path)
    gw.decisions = _FakeDecisionStore(get_returns_id=None)
    res = gw.request_decision("开灯吗？", ["开", "关"])
    assert res["ok"] is True, f"正常路径应 ok=True：{res}"
    assert res["decision_id"] == "dec_test123", "decision_id 必须与库一致"


def test_R10_decision_id_misalign_detected(monkeypatch, tmp_path):
    """错位路径：回执 id 读回不一致 → ok=False 且如实说明，绝不发死 id。"""
    gw = _gw(monkeypatch, tmp_path)
    gw.decisions = _FakeDecisionStore(get_returns_id="dec_OTHER456")
    res = gw.request_decision("开灯吗？", ["开", "关"])
    assert res["ok"] is False, f"错位必须 ok=False：{res}"
    assert "读回自检失败" in res.get("error", ""), "必须点明读回自检失败"
    assert res["decision_id"] == "dec_test123", "仍应平铺回执 id 供排查"
