# -*- coding: utf-8 -*-
"""
P5 · E2E 执行追踪（真实跑通证明 + 断点报告）。

核心断言：
  - 插桩层：_instrument_flow 在每个非 sink 原节点后加 tap、加全局 catch，
    且不改动原节点下游连线。
  - 比对层：_compare_trace 能从 trace 推出「到达/断点/运行时错误」。
  - 编排层：run_e2e_trace 在 FakeNRLayer 上跑通 happy / breakpoint / 拦截 三态，
    且无论成败都清理插桩副本（不残留）。

不依赖真实 NR：用 FakeNRLayer 模拟「部署→触发→执行→读回 trace」，
并把断点模拟成『第 N 个原节点执行后报错』，从而确定性地验证
断点报告逻辑。真实 NR 冒烟由 AUTOFLLOW_E2E_LIVE=1 门控（默认不跑）。
"""
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_e2e_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway.config import reset_config


# ── 假 NR 层：模拟「部署→触发→执行→读回 trace」──
class FakeNRLayer:
    """行为对齐真实 nr_client，但执行是模拟的：

    - create_or_update_flow：存下插桩后的 flow。
    - trigger_inject / inject_flow：从指定/入口节点 BFS 执行**原节点**，
      每到一个非 sink 原节点就把其 id 推入 trace（模拟 tap 记录），
      当已执行原节点数达到 break_after 时模拟「该节点报错」并停止（下游不再到达）。
    - get_context 返回最新 trace（模拟 global[trace_key] 读回）。
    - delete_flow：移除副本（验证清理）。
    """

    def __init__(self):
        self.flows: Dict[str, Dict] = {}
        self._trace: List[Dict] = []
        self.server_id = "srv_fake"
        self.break_after: Optional[int] = None   # 执行到第几个原节点后模拟报错
        self.deleted: List[str] = []

    # ── 读 / 安全写（与 NRLayer 同签名）──
    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        self.flows[fid] = flow_data
        return {"id": fid, "created": True}

    def get_default_server_id(self):
        return self.server_id

    def trigger_inject(self, node_id):
        self._run(node_id)
        return 200

    def inject_flow(self, flow_id):
        flow = self.flows.get(flow_id, {})
        for s in self._entries(flow):
            self._run(s)

    def get_context(self, store, key):
        return list(self._trace)

    def set_context(self, store, key, value):
        if value in ([], None, {}):
            self._trace = []
        return None

    def delete_context(self, store, key):
        self._trace = []
        return {}

    def delete_flow(self, fid, force=False, allow_prod=False):
        self.flows.pop(fid, None)
        self.deleted.append(fid)
        return {"deleted": True}

    # ── 内部：模拟执行 ──
    @staticmethod
    def _incoming(flow, nid):
        for n in flow.get("nodes", []):
            for w in (n.get("wires") or []):
                if isinstance(w, list) and nid in w:
                    return True
        return False

    def _entries(self, flow):
        return [n["id"] for n in flow.get("nodes", [])
                if n.get("type") == "inject" or not self._incoming(flow, n["id"])]

    def _run(self, start_id):
        flow = None
        for f in self.flows.values():
            if any(n.get("id") == start_id for n in f.get("nodes", [])):
                flow = f
                break
        if flow is None:
            return
        nodes = {n["id"]: n for n in flow.get("nodes", [])}
        seen = set()
        stack = [start_id]
        order = 0
        trace: List[Dict] = []
        while stack:
            nid = stack.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            n = nodes.get(nid)
            if n is None:
                continue
            # 插桩节点（tap / catch / 错误记录）跳过，但继续沿其连线
            if n.get("_af_trace_tap") or n.get("type") == "catch" or n.get("_af_err_sink"):
                for w in (n.get("wires") or []):
                    if isinstance(w, list):
                        stack.extend(w)
                continue
            # 原节点执行 → 由 tap 记录其 id
            trace.append({"node": nid, "t": order, "topic": None, "payload": "x"})
            order += 1
            if self.break_after is not None and order >= self.break_after:
                trace.append({"error": True, "node": nid,
                              "message": f"simulated runtime error at {nid}"})
                break  # 报错 → 下游不再到达
            for w in (n.get("wires") or []):
                if isinstance(w, list):
                    stack.extend(w)
        self._trace = trace


# ── 假 HA 层：observe_postconditions 只读 ──
class FakeHA:
    def __init__(self, states: Dict[str, Dict]):
        self.states = states

    def get_state(self, entity_id):
        return self.states.get(entity_id)


DSL = """场景: e2e-冒烟
触发: binary_sensor.e2e_motion 有人
动作: light.turn_on(light.e2e_lamp, brightness=50)
动作: light.turn_on(light.e2e_fan)
动作: light.turn_off(light.e2e_lamp)
预期:
    light.e2e_lamp = off
"""

ENTITIES = ["binary_sensor.e2e_motion", "light.e2e_lamp", "light.e2e_fan"]

DSL_BAD_ENTITY = """场景: e2e-未知实体
触发: binary_sensor.e2e_motion 有人
动作: light.turn_on(light.does_not_exist)
预期:
    light.does_not_exist = on
"""

DSL_BAD_DSL = "这不是合法的 DSL 场景：：：\n动作: 语法错误"


def _make_gw(nr=None, ha=None):
    reset_config()
    gw = G.Gateway()
    gw._e2e_settle = 0  # 单测：跳过真机落定等待（Fake 同步返回 trace）
    for e in ENTITIES:
        gw.state.add_mapping(e, e)
    if nr is not None:
        gw.nr = nr
    if ha is not None:
        gw.ha = ha
    return gw


def _mini_compiled_flow():
    """手工构造一个编译产物形态（含 inject 触发器 + 两 action + debug sink）。"""
    return {
        "id": "tab_mini", "label": "mini",
        "nodes": [
            {"id": "n_trig", "type": "inject", "z": "tab_mini", "wires": [["n_a"]]},
            {"id": "n_a", "type": "api-call-service", "z": "tab_mini",
             "name": "开灯", "wires": [["n_b"]]},
            {"id": "n_b", "type": "api-call-service", "z": "tab_mini",
             "name": "开风扇", "wires": [["n_sink"]]},
            {"id": "n_sink", "type": "debug", "z": "tab_mini", "wires": [[]]},
        ],
    }


# ── 1) 插桩层：结构正确 ──
def test_instrument_adds_taps_and_catch():
    gw = _make_gw()
    flow = _mini_compiled_flow()
    out = gw._instrument_flow(flow, "__trace__:tok")
    nodes = out["nodes"]
    taps = [n for n in nodes if n.get("_af_trace_tap")]
    catches = [n for n in nodes if n.get("type") == "catch"]
    errs = [n for n in nodes if n.get("_af_err_sink")]
    # n_a / n_b 是非 sink 原节点 → 各一个 tap；inject / debug 不插桩
    assert len(taps) == 2, [n.get("name") for n in taps]
    assert len(catches) == 1, "应有 1 个 catch 节点"
    assert len(errs) == 1, "应有 1 个错误记录节点"
    # catch 的 scope 覆盖两个原节点
    assert set(catches[0]["scope"]) == {"n_a", "n_b"}, catches[0]["scope"]
    # 原节点连线被追加 tap 作为额外分支，未丢失原下游
    by_id = {n["id"]: n for n in nodes}
    assert "n_b" in by_id["n_a"]["wires"][0], by_id["n_a"]["wires"]   # n_a→n_b 原下游仍在
    assert "n_sink" in by_id["n_b"]["wires"][0]                         # n_b→n_sink 原下游仍在
    # n_a / n_b 各自多出一个 tap 分支
    assert len(by_id["n_a"]["wires"][0]) == 2, by_id["n_a"]["wires"]
    assert len(by_id["n_b"]["wires"][0]) == 2, by_id["n_b"]["wires"]
    # 入参未被污染
    assert flow["nodes"][1]["wires"] == [["n_b"]] or "n_a 入参被改"


# ── 2) 比对层：happy 全到达 ──
def test_compare_trace_happy():
    gw = _make_gw()
    flow = _mini_compiled_flow()
    trace = [
        {"node": "n_trig", "t": 0},
        {"node": "n_a", "t": 1},
        {"node": "n_b", "t": 2},
        {"node": "n_sink", "t": 3},
    ]
    rep = gw._compare_trace(flow, trace)
    assert rep["verdict"] == "通过", rep
    assert rep["reached_count"] == 4, rep["reached"]
    assert rep["missing"] == [], rep["missing"]
    assert rep["failed_at"] is None
    assert rep["runtime_errors"] == []


# ── 3) 比对层：断点 + 运行时错误 ──
def test_compare_trace_breakpoint():
    gw = _make_gw()
    flow = _mini_compiled_flow()
    # 到了 n_a，n_a 报错，n_b / n_sink 没到
    trace = [
        {"node": "n_trig", "t": 0},
        {"node": "n_a", "t": 1},
        {"error": True, "node": "n_a", "message": "entity not found: x"},
    ]
    rep = gw._compare_trace(flow, trace)
    assert rep["verdict"] == "断点", rep
    assert "开风扇" in rep["missing"], rep["missing"]
    assert rep["failed_at"] == "开风扇", rep["failed_at"]
    assert rep["runtime_errors"], rep
    assert "开风扇" in rep["breakpoint"], rep["breakpoint"]
    assert "entity not found" in rep["breakpoint"]


# ── 3b) 回归守卫：不可插桩节点(inject/debug/link/catch)不得被冤枉成断点 ──
# 真机实测发现：_instrument_flow 不给 SINK 类节点加 tap，故它们永远不在 trace 里
# 自报；旧 _compare_trace 把 inject 算进期望路径 → inject 触发器被误报为 failed_at。
# 此测试锁死：期望路径按 E2E_SINK_TYPES 过滤，只比对可插桩节点。
def test_compare_trace_excludes_untappable_nodes():
    gw = _make_gw()
    flow = _mini_compiled_flow()  # inject → api-call ×2 → debug
    # 真实 trace 只会含两个 api-call（tap 记录），不含 inject/debug（无 tap）
    trace = [
        {"node": "n_a", "t": 1},
        {"node": "n_b", "t": 2},
    ]
    rep = gw._compare_trace(flow, trace)
    # inject('n_trig') / debug('n_sink') 不可插桩 → 不得出现在 missing / failed_at
    assert rep["failed_at"] is None, rep
    assert "手动触发" not in (rep["missing"] or [])
    assert rep["missing"] == [], rep["missing"]
    assert rep["verdict"] == "通过", rep
    assert rep["expected_count"] == 2, rep  # 仅两个 api-call 计入期望


# ── 4) 编排层：happy ──
def test_run_e2e_happy():
    nr = FakeNRLayer()
    # 假 HA：灯以 off 收尾（与 DSL 末动作 turn_off 一致）→ 副作用达标
    gw = _make_gw(nr=nr, ha=FakeHA({
        "light.e2e_lamp": {"state": "off", "attributes": {}},
        "light.e2e_fan": {"state": "on", "attributes": {}},
    }))
    res = gw.run_e2e_trace(DSL, expected_postconditions=[
        {"entity_id": "light.e2e_lamp", "state": "off"}])
    assert res["e2e"] is True, res
    assert res["verdict"] == "通过", res["report"]
    assert res["report"]["reached_count"] >= 3, res["report"]
    assert res["postconditions"]["ok"] is True
    # 插桩副本被清理
    assert nr.deleted, "E2E 后插桩副本应被删除"
    assert res["flow_id"] in nr.deleted


# ── 5) 编排层：断点（第 2 个原节点后报错）──
def test_run_e2e_breakpoint():
    nr = FakeNRLayer()
    nr.break_after = 2  # 执行到第 2 个原节点后模拟报错
    gw = _make_gw(nr=nr)
    res = gw.run_e2e_trace(DSL)
    assert res["e2e"] is True, res
    assert res["verdict"] == "断点", res["report"]
    assert res["report"]["failed_at"] is not None, res["report"]
    assert res["report"]["runtime_errors"], res["report"]
    assert "simulated runtime error" in res["report"]["breakpoint"]
    # 无论成败都清理
    assert res["flow_id"] in nr.deleted


# ── 6) 编排层：编译失败 → 拦截 ──
def test_run_e2e_compile_fail():
    nr = FakeNRLayer()
    gw = _make_gw(nr=nr)
    res = gw.run_e2e_trace(DSL_BAD_DSL)
    assert res["e2e"] is False, res
    assert res["stage"] == "compile", res
    assert res["verdict"] == "拦截", res
    # 没部署过，无需清理
    assert nr.deleted == []


# ── 7) 编排层：未知实体 → 拦截 ──
def test_run_e2e_unknown_entity():
    nr = FakeNRLayer()
    gw = _make_gw(nr=nr)  # ENTITIES 中不含 light.does_not_exist
    res = gw.run_e2e_trace(DSL_BAD_ENTITY)
    assert res["e2e"] is False, res
    assert res["stage"] == "entity_check", res
    assert res["verdict"] == "拦截", res


# ── 8) 编排层：HA 副作用未达标也判断点 + 清理 ──
def test_run_e2e_postcondition_miss():
    nr = FakeNRLayer()
    # 假 HA 里灯仍是 on（与预期的 off 不符）
    gw = _make_gw(nr=nr, ha=FakeHA({
        "light.e2e_lamp": {"state": "on", "attributes": {}},
        "light.e2e_fan": {"state": "on", "attributes": {}},
    }))
    res = gw.run_e2e_trace(DSL, expected_postconditions=[
        {"entity_id": "light.e2e_lamp", "state": "off"}])
    assert res["e2e"] is True
    assert res["verdict"] == "断点", res  # 路径通过但 HA 副作用不符
    assert res["postconditions"]["ok"] is False
    assert res["flow_id"] in nr.deleted


# ── 8b) 回归守卫：nr_client.get_context 必须解包 NR 的 {"msg","format"} 信封 ──
# 真机实测发现：NR Admin Context API 返回 {"msg":"<值的字符串化>","format":"array[1]"}，
# 值裹在 msg 里且是 JSON 字符串。旧实现直接返回信封 dict → run_e2e_trace 里
# `if not isinstance(trace, list): trace = []` → 真实 trace 被丢弃 → 每次真机 E2E 假断点。
# 且 NR 无 POST 写端点，清理必须走 DELETE。此测试锁死这两点。
def test_get_context_unwraps_nr_envelope():
    import importlib.util
    from pathlib import Path as _P
    p = _P(__file__).resolve().parents[1] / "src" / "autoflow_gateway" / "lib" / "nr_client.py"
    spec = importlib.util.spec_from_file_location("_nrc_probe", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cli = mod.NodeRedClient.__new__(mod.NodeRedClient)  # 不触发 __init__（免连网）

    calls = {}

    def fake_json(method, endpoint, **kw):
        calls["last"] = (method, endpoint)
        # 模拟 NR 各种返回信封
        if endpoint.endswith("/arr"):
            return {"msg": '[{"node":"n_a","t":1}]', "format": "array[1]"}
        if endpoint.endswith("/miss"):
            return {"msg": "(undefined)", "format": "undefined"}
        if endpoint.endswith("/boolv"):
            return {"msg": "false", "format": "boolean"}
        if endpoint.endswith("/numv"):
            return {"msg": "42", "format": "number"}
        if endpoint.endswith("/del"):
            return {}
        return {}

    cli._json = fake_json  # type: ignore

    # array 信封 → 还原成 list
    v = cli.get_context("global", "arr")
    assert v == [{"node": "n_a", "t": 1}], v
    # undefined → None
    assert cli.get_context("global", "miss") is None
    # boolean → bool
    assert cli.get_context("global", "boolv") is False
    # number → int
    assert cli.get_context("global", "numv") == 42
    # 清理走 DELETE（set_context([]) 映射到 delete_context）
    cli.set_context("global", "del", [])
    assert calls["last"] == ("DELETE", "/context/global/del"), calls["last"]
    # 写非空值应显式报错（NR 无 POST 端点，不能静默失败）
    try:
        cli.set_context("global", "x", [1, 2])
        assert False, "写非空 context 应抛错"
    except RuntimeError:
        pass


# ── 9) 真实 NR 冒烟（默认关闭，AUTOFLLOW_E2E_LIVE=1 开启）──
# 不依赖 FakeNRLayer，直连 staging(NR)。会真实部署插桩副本、触发、读回、
# 再删除——属外部动作，故默认跳过，仅用户显式开启时跑。
#
# 用【inject 触发 + 永假分支】构造确定性『死胡同』：分支前的动作真机上到达，
# 分支体内动作永远到不了 → 断点报告应 failed_at=分支、reached=1。
# 用 inject 触发（不用状态触发）是因为 nr_client.inject_flow 仍是桩（只 print），
# 状态触发的 flow 在真机上无法由 E2E 主动触发——这是已知限制。
# 用幽灵实体（注册进 mapping 骗过实体校验，但 HA 里不存在）→ 零真实设备副作用。
def test_run_e2e_live_smoke():
    if os.environ.get("AUTOFLLOW_E2E_LIVE") != "1":
        print("  SKIP test_run_e2e_live_smoke（设 AUTOFLLOW_E2E_LIVE=1 开启真实 NR 冒烟）")
        return
    gw = _make_gw()  # 真实 NRLayer + 真实 HA 层
    gw._e2e_settle = 2.0  # 真机必须给异步执行落定时间（settle=0 会读到空 trace → 假断点）
    for e in ("light.af_e2e_ghost_a", "light.af_e2e_ghost_c"):
        gw.state.add_mapping(e, e)
    dsl = (
        "场景: e2e-live-死胡同冒烟\n"
        "触发: inject\n"
        "动作: light.turn_on(light.af_e2e_ghost_a)\n"
        "分支: payload = \"__NEVER_MATCH__\"\n"
        "    动作: light.turn_on(light.af_e2e_ghost_c)\n"
    )
    try:
        res = gw.run_e2e_trace(dsl)
    except Exception as e:  # 网络/NR 不可达时干净跳过，不污染套件
        print(f"  SKIP test_run_e2e_live_smoke（NR 不可达：{e}）")
        return
    assert res["e2e"] is True, res
    rep = res["report"]
    # 真机断点定位：只到达分支前的动作，分支门后断流
    assert rep["reached_count"] == 1, rep
    assert res["verdict"] == "断点", rep
    assert rep["failed_at"] is not None, rep
    # 清理核验：插桩副本已回滚，不残留在 NR
    ids_now = {f.get("id") for f in gw.nr.list_flows()}
    assert res["flow_id"] not in ids_now, "E2E 后插桩副本应已从 NR 删除"


# ── 9b) 真实 NR 复验 P0 修复：时间段 → time-range-switch（2 输出）──
# 旧版误发未注册的 time-range，部署即坏（陌生节点静默丢 msg），
# E2E 永远 reached=0。修复后应为 time-range-switch（已在 NR 注册），
# 窗口内（00:00-23:59 恒过）放行主链 → 动作到达（reached>0、verdict=通过）。
# 幽灵实体零真实副作用。默认关闭，AUTOFLLOW_E2E_LIVE=1 开启。
def test_run_e2e_live_time_range_switch():
    if os.environ.get("AUTOFLLOW_E2E_LIVE") != "1":
        print("  SKIP test_run_e2e_live_time_range_switch（设 AUTOFLLOW_E2E_LIVE=1 开启真实 NR 复验）")
        return
    gw = _make_gw()  # 真实 NRLayer + 真实 HA 层
    gw._e2e_settle = 2.0  # 真机必须给异步执行落定时间
    for e in ("light.af_e2e_ghost_a",):
        gw.state.add_mapping(e, e)
    dsl = (
        "场景: e2e-live-时间段开关复验\n"
        "触发: inject\n"
        "时间段: 00:00-23:59\n"
        "动作: light.turn_on(light.af_e2e_ghost_a)\n"
    )
    try:
        res = gw.run_e2e_trace(dsl)
    except Exception as e:  # 网络/NR 不可达时干净跳过，不污染套件
        print(f"  SKIP test_run_e2e_live_time_range_switch（NR 不可达：{e}）")
        return
    assert res["e2e"] is True, res
    rep = res["report"]
    # 窗口内：time-range-switch 正确放行，动作应到达（证明非陌生节点）
    assert rep["reached_count"] >= 2, rep
    assert res["verdict"] == "通过", rep
    # 清理核验：插桩副本已回滚，不残留在 NR
    ids_now = {f.get("id") for f in gw.nr.list_flows()}
    assert res["flow_id"] not in ids_now, "E2E 后插桩副本应已从 NR 删除"


# ── 9c) 真实 NR 复验 history_* 子流程 + 节点闸门不再误杀 ───
# 调用子流程: history_state_at 编译为 subflow 实例（请求/响应）。
# 幽灵实体只产出空历史 → 信息流跑通到动作。
# 本测试证明：① history_* 子流程在真实 NR 上成功部署并运行（依赖 #272 部署）；
# ② 节点注册表闸门已不再因 /nodes 别名与 /flows 分页的错配而误杀合法节点。
# 默认关闭，AUTOFLLOW_E2E_LIVE=1 开启（且需先完成 #272 子流程部署）。
def test_run_e2e_live_history_attribution():
    if os.environ.get("AUTOFLLOW_E2E_LIVE") != "1":
        print("  SKIP test_run_e2e_live_history_attribution（设 AUTOFLLOW_E2E_LIVE=1 开启真实 NR）")
        return
    gw = _make_gw()  # 真实 NRLayer + 真实 HA 层
    gw._e2e_settle = 2.0  # 真机必须给异步执行落定时间
    ghost = "binary_sensor.af_e2e_ghost_hist"
    gw.state.add_mapping(ghost, ghost)  # 骗过实体校验，但 HA 里不存在
    gw.state.add_mapping("light.af_e2e_ghost_a", "light.af_e2e_ghost_a")
    dsl = (
        "场景: e2e-live-历史归因\n"
        "触发: inject\n"
        f"调用子流程: history_state_at(entity={ghost}, at=昨晚23:12)\n"
        f"动作: light.turn_on(light.af_e2e_ghost_a)\n"
    )
    try:
        res = gw.run_e2e_trace(dsl)
    except RuntimeError as e:
        # 节点注册表闸门触发 → 真实失败（非 NR 不可达），必须 loud 失败而非吞成 SKIP
        if "节点类型未注册" in str(e):
            raise AssertionError(f"历史原语被节点闸门误杀（P0 同类 bug 复发）：{e}")
        print(f"  SKIP test_run_e2e_live_history_attribution（NR 不可达：{e}）")
        return
    except Exception as e:  # 网络/NR 不可达时干净跳过
        print(f"  SKIP test_run_e2e_live_history_attribution（NR 不可达：{e}）")
        return
    assert res["e2e"] is True, res
    rep = res["report"]
    # C1 证明：api-get-history（历史原语）在真实 NR 上成功部署并运行——
    # 幽灵实体只产出空历史数组（不静默断流），故信息流跑通到动作。
    # 这同时验证节点注册表闸门已不再误杀该合法节点（P0 同类 bug 不复发）。
    assert res["verdict"] == "通过", rep
    assert rep["reached_count"] >= 2, rep
    reached_names = " ".join(rep["reached"])
    assert "历史" in reached_names, rep   # 历史节点确实被跑到
    assert "开灯" in reached_names or "turn_on" in reached_names, rep
    # 清理核验：插桩副本已回滚
    ids_now = {f.get("id") for f in gw.nr.list_flows()}
    assert res["flow_id"] not in ids_now, "E2E 后插桩副本应已从 NR 删除"


# ── 10) 节点注册表闸门：纯函数 + 方法（不依赖网络）──
def test_check_unknown_node_types():
    """check_unknown_node_types 应列出 flow 中在目标 NR 未注册的类型。"""
    flow = {
        "nodes": [
            {"type": "inject", "id": "a"},
            {"type": "time-range-switch", "id": "b"},
            {"type": "ghost-node-xyz", "id": "c"},  # 未注册
        ]
    }
    installed = {"inject", "time-range-switch", "api-call-service"}
    unk = G.check_unknown_node_types(flow, installed)
    assert unk == ["ghost-node-xyz"], unk
    # 全部已注册 → 空
    assert G.check_unknown_node_types(
        {"nodes": [{"type": "inject"}, {"type": "time-range-switch"}]},
        installed) == []


# ── 11) 回归：e2e 入口实体不得退化为 unknown.entity（entityId 静默丢失根因）──
class CaptureNRLayer(FakeNRLayer):
    """在 create_or_update_flow 时顺便记下插桩副本，供断言实体回填。"""
    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        self.last_deployed = flow_data
        return super().create_or_update_flow(fid, flow_data, force=force)


def test_e2e_entry_entity_id_helper():
    """实体抽取需覆盖各 HASS 节点版本存储位置（尤其 server-state-changed v6
    的 entities.entity 数组）——否则会退化成 unknown.entity。"""
    f = G.Gateway._e2e_entry_entity_id
    # server-state-changed v6：entity 在 entities.entity（数组）
    assert f({"type": "server-state-changed", "version": 6,
              "entities": {"entity": ["sensor.x"], "substring": [], "regex": []}}) == "sensor.x"
    # entities.entity 字符串
    assert f({"type": "server-state-changed",
              "entities": {"entity": "sensor.y"}}) == "sensor.y"
    # 顶层 entityId（server-event / 旧版 trigger / poll-state）
    assert f({"type": "server-event", "entityId": "light.z"}) == "light.z"
    # 顶层 entity 数组（旧版 trigger / device）
    assert f({"type": "trigger", "entity": ["switch.w"]}) == "switch.w"
    # 无任何实体绑定（纯时间/注入触发器）→ 回退 unknown.entity（可接受）
    assert f({"type": "inject", "wires": [[]]}) == "unknown.entity"


def test_e2e_entry_to_inject_uses_real_entity():
    """_e2e_entry_to_inject 必须把入口的真实实体写进合成 inject 的 topic/payload，
    绝不能统一写成 unknown.entity。"""
    node = {"id": "n", "type": "server-state-changed", "version": 6,
            "entities": {"entity": ["binary_sensor.real"], "substring": [], "regex": []},
            "wires": [["x"], []]}
    G.Gateway._e2e_entry_to_inject(node, {})
    assert node["type"] == "inject"
    assert node["topic"] == "binary_sensor.real", node["topic"]
    assert "unknown.entity" not in node["topic"]


def test_e2e_entry_entity_id_preserved():
    """端到端：server-state-changed v6 入口在 e2e 合成触发时保留真实实体，
    且下游 api-current-state 在插桩副本里被置 blockInputOverrides=true，
    避免被入口事件 topic 污染（否则下游读 unknown.entity → HASS 报错）。"""
    nr = CaptureNRLayer()
    occ = "binary_sensor.af_e2e_occ"
    lux = "sensor.af_e2e_lux"
    gw = _make_gw(nr=nr)
    gw.state.add_mapping(occ, occ)
    gw.state.add_mapping(lux, lux)
    flow = {
        "id": "tab_e", "label": "e",
        "nodes": [
            {"id": "n_trig", "type": "server-state-changed", "z": "tab_e",
             "version": 6, "server": "REPLACE_WITH_HA_SERVER",
             "entities": {"entity": [occ], "substring": [], "regex": []},
             "for": "5", "forType": "num", "forUnits": "minutes",
             "wires": [["n_state"]]},
            {"id": "n_state", "type": "api-current-state", "z": "tab_e",
             "version": 3, "server": "REPLACE_WITH_HA_SERVER",
             "entityId": lux, "entityIdType": "str",
             "blockInputOverrides": False, "wires": [["n_dbg"]]},
            {"id": "n_dbg", "type": "debug", "z": "tab_e", "wires": [[]]},
        ],
    }
    res = gw.run_e2e_trace_raw(flow)
    # 入口注入节点必须携带真实实体（非 unknown.entity）。
    # 注意：run_e2e_trace_raw 会重映射 id，故按「type=inject + topic=真实实体」定位。
    inj = next(n for n in nr.last_deployed["nodes"]
               if n.get("type") == "inject" and n.get("topic") == occ)
    assert inj["topic"] == occ, f"入口实体被改写：{inj['topic']}"
    assert "unknown.entity" not in inj["topic"], "回归：入口退化为 unknown.entity"
    # 下游 api-current-state 在插桩副本里被置 blockInputOverrides=true
    # （id 重映射不影响 entityId，按 entityId 定位）
    st = next(n for n in nr.last_deployed["nodes"]
              if n.get("type") == "api-current-state" and n.get("entityId") == lux)
    assert st.get("blockInputOverrides") is True, st
    # e2e 仍跑通，且不应出现 unknown.entity 导致的运行时错误
    assert res["e2e"] is True, res
    assert not any("unknown.entity" in (e.get("message", "") or "")
                   for e in (res.get("report", {}).get("runtime_errors") or [])), res


def test_gate_node_types_raises_on_unregistered():
    """_gate_node_types 在目标 NR 未装某节点类型时应抛错拦截（P0 防御）。"""
    class _FakeClient:
        def get_installed_node_types(self):
            return {"inject", "api-call-service"}  # 故意不含 time-range-switch
    class _FakeNR:
        def __init__(self, c):
            self.client = c
    gw = _make_gw()
    gw.nr = _FakeNR(_FakeClient())
    bad = {"nodes": [{"type": "time-range-switch", "id": "x"}]}
    try:
        gw._gate_node_types(bad)
        assert False, "未注册节点类型应被闸门拦截"
    except RuntimeError as e:
        assert "time-range-switch" in str(e), e


# ── B3 断点归因增强：区分『实体不存在→静默无输出』与『连线断裂』──
def test_breakpoint_message_attr_history_silent_no_output():
    """断在 api-get-history / api-current-state 且无运行时错误 →
    提示『实体可能不存在 → 静默无输出』，区别于连线断裂。"""
    gw = _make_gw()
    nodes = {
        "n_hist": {"type": "api-get-history", "name": "历史 领普"},
        "n_state": {"type": "api-current-state", "name": "取值 温度"},
    }
    msg = gw._breakpoint_message("n_hist", nodes, errors=[])
    assert "静默无输出" in msg, msg
    assert "实体" in msg
    msg2 = gw._breakpoint_message("n_state", nodes, errors=[])
    assert "静默无输出" in msg2, msg2
    # 普通节点仍给通用提示（连线断裂/未产出 msg）
    nodes2 = {"n_x": {"type": "api-call-service", "name": "开灯"}}
    msg3 = gw._breakpoint_message("n_x", nodes2, errors=[])
    assert ("连线断裂" in msg3) or ("未产出" in msg3), msg3


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
