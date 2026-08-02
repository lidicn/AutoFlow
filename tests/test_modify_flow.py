"""Gateway.modify_flow 外科式改 flow 单测（C3）。

不依赖 live NR —— 用 FakeNR 双 stub（get_flow / create_or_update_flow /
client.get_installed_node_types）覆盖四条路：
  1. node_patches 最小改动（匹配 name/id/type）
  2. dsl 重编译模式
  3. 空参数拒绝
  4. 节点注册表闸门拦截未知类型
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway import gateway as G


# ── FakeNR（含 client，可测节点闸门）────────────────────────
class _FakeNRClient:
    def get_installed_node_types(self):
        # 已知类型集：故意不含 time-range（旧未注册类型）
        return {"inject", "change", "api-call-service", "comment",
                "api-current-state", "time-range-switch", "server-state-changed",
                "api-get-history"}


class _FakeNR:
    def __init__(self):
        self.client = _FakeNRClient()
        self.deploy_calls = 0          # #701：证明 fail-closed 时零部署
        self._flow = {
            "id": "fid", "label": "demo",
            "nodes": [
                {"id": "a", "type": "inject", "name": "触发", "wires": [["b"]]},
                {"id": "b", "type": "change", "name": "旧文案", "wires": [[]], "rules": []},
            ],
        }

    def get_flow(self, fid, use_cache=True):
        # 返回副本，避免测试污染
        import copy
        return copy.deepcopy(self._flow)

    def get_default_server_id(self):
        # 抄 NRLayer.get_default_server_id（nr_layer.py:141）：返回 NR 里第一个
        # HA server 配置节点 id。#706 起 modify_flow 会调 _inject_ha_server，
        # 该链路需要此方法（cfg.nr_ha_server_id 为空时的兜底来源）。
        return "ha_srv_fake"

    def create_or_update_flow(self, fid, flow, force=False, allow_prod=False):
        self.deploy_calls += 1
        self._flow = flow
        return {"id": fid, "created": False}


def _gw():
    gw = G.Gateway()
    gw.nr = _FakeNR()
    return gw


# ── 1) node_patches 最小改动 ───────────────────────────────
def test_modify_flow_patch_by_name():
    gw = _gw()
    res = gw.modify_flow("fid",
                           node_patches=[{"match": {"name": "旧文案"},
                                         "set": {"name": "新文案"}}])
    assert res["ok"] is True, res
    assert res["mode"] == "node_patches"
    assert res["changed_nodes"] == 1
    changed = [n for n in gw.nr._flow["nodes"] if n["id"] == "b"][0]
    assert changed["name"] == "新文案"


def test_modify_flow_patch_by_type_remove():
    gw = _gw()
    # 匹配 type=change，移除 rules 字段
    res = gw.modify_flow("fid",
                           node_patches=[{"match": {"type": "change"},
                                         "remove": ["rules"]}])
    assert res["ok"] is True, res
    assert res["changed_nodes"] == 1
    changed = [n for n in gw.nr._flow["nodes"] if n["id"] == "b"][0]
    assert "rules" not in changed


# ── 1b) fail-closed（#701 / R5-BLOCKER）─────────────────────
# 旧行为：零匹配仍 ok=True + changed_nodes=0 且照样重部署 → 谎报成功。
# 新契约：一律 ok=False / stage="patch" / 零部署 / flow 原样不动。
def test_modify_flow_patch_no_match():
    gw = _gw()
    before = [dict(n) for n in gw.nr._flow["nodes"]]
    res = gw.modify_flow("fid",
                           node_patches=[{"match": {"name": "不存在"}, "set": {"x": 1}}])
    assert res["ok"] is False, res
    assert res["stage"] == "patch", res
    assert res.get("changed_nodes") == 0
    assert res["unmatched"] and res["unmatched"][0]["match"] == {"name": "不存在"}
    # 未部署 + flow 未改动
    assert gw.nr.deploy_calls == 0
    assert gw.nr._flow["nodes"] == before


def test_modify_flow_bad_format_flat_rejected():
    """WB2 R5 实际误用：扁平 {"node_id":..,"func":..}，无 match 键。"""
    gw = _gw()
    res = gw.modify_flow("fid",
                           node_patches=[{"node_id": "b", "func": "return msg;"}])
    assert res["ok"] is False, res
    assert res["stage"] == "patch", res
    assert "match" in (res.get("error") or "")
    assert res.get("got_keys") == ["func", "node_id"]
    assert gw.nr.deploy_calls == 0


def test_modify_flow_nonexistent_id_rejected():
    """格式正确但 id 不存在 → 同样 fail-closed，并回吐可选节点清单。"""
    gw = _gw()
    res = gw.modify_flow("fid",
                           node_patches=[{"match": {"id": "no-such-node"},
                                         "set": {"name": "x"}}])
    assert res["ok"] is False, res
    assert res["stage"] == "patch", res
    assert gw.nr.deploy_calls == 0
    ids = {n["id"] for n in res["available_nodes"]}
    assert ids == {"a", "b"}


def test_modify_flow_patch_missing_set_and_remove_rejected():
    gw = _gw()
    res = gw.modify_flow("fid", node_patches=[{"match": {"id": "b"}}])
    assert res["ok"] is False, res
    assert res["stage"] == "patch", res
    assert gw.nr.deploy_calls == 0


def test_modify_flow_partial_match_aborts_whole_batch():
    """一条命中、一条落空 → 整批中止，不允许「改一半还报成功」。"""
    gw = _gw()
    before = [dict(n) for n in gw.nr._flow["nodes"]]
    res = gw.modify_flow("fid", node_patches=[
        {"match": {"id": "b"}, "set": {"name": "新文案"}},
        {"match": {"id": "ghost"}, "set": {"name": "x"}},
    ])
    assert res["ok"] is False, res
    assert [u["index"] for u in res["unmatched"]] == [1]
    assert gw.nr.deploy_calls == 0
    assert gw.nr._flow["nodes"] == before   # 内存里的真流未被写回


# ── 2) dsl 重编译模式 ───────────────────────────────
def test_modify_flow_dsl_recompile():
    gw = _gw()
    gw.state.add_mapping("light.x", "light.x")  # 登记实体，过实体校验
    dsl = """场景: 改后
触发: inject
动作: light.turn_on(light.x)
"""
    res = gw.modify_flow("fid", dsl=dsl)
    assert res["ok"] is True, res
    assert res["mode"] == "dsl_recompile"
    types = {n["type"] for n in gw.nr._flow["nodes"]}
    assert "inject" in types and "api-call-service" in types
    # 复用目标 flow 的 id / label
    assert gw.nr._flow["id"] == "fid"
    assert gw.nr._flow["label"] == "demo"


# ── 2b) HA server 占位符必须被解析（WB72 Bug#2 / #706 回归）──────
# 旧行为：modify_flow 全程不调 _inject_ha_server，dsl 重编译产出的 HA 节点
# 带着字面量 REPLACE_WITH_HA_SERVER 落盘 → 流"部署成功"但永远不动。
def test_modify_flow_dsl_resolves_ha_server_placeholder():
    gw = _gw()
    gw.state.add_mapping("light.x", "light.x")
    dsl = """场景: 改后
触发: inject
动作: light.turn_on(light.x)
"""
    res = gw.modify_flow("fid", dsl=dsl)
    assert res["ok"] is True, res
    servers = [n.get("server") for n in gw.nr._flow["nodes"] if "server" in n]
    assert servers, "dsl 重编译应产出带 server 字段的 HA 节点"
    assert "REPLACE_WITH_HA_SERVER" not in servers, gw.nr._flow["nodes"]
    assert all(s for s in servers), servers


def test_modify_flow_ha_server_unresolved_fails_closed():
    """解析不出 HA server（cfg 空 + NR 无默认）→ fail-fast，绝不部署坏 flow。"""
    gw = _gw()
    gw.state.add_mapping("light.x", "light.x")
    gw.cfg.nr_ha_server_id = ""
    gw.nr.get_default_server_id = lambda: ""
    dsl = """场景: 改后
触发: inject
动作: light.turn_on(light.x)
"""
    res = gw.modify_flow("fid", dsl=dsl)
    assert res["ok"] is False, res
    assert res["stage"] == "ha_server_inject", res
    assert gw.nr.deploy_calls == 0, "占位符未解析时必须零部署"


# ── 3) 空参数拒绝 ───────────────────────────────
def test_modify_flow_empty_rejected():
    gw = _gw()
    res = gw.modify_flow("fid")
    assert res["ok"] is False
    assert "dsl" in res["error"] and "node_patches" in res["error"]


# ── 4) 节点注册表闸门拦截未知类型 ───────────────────────────────
def test_modify_flow_node_gate_blocks_unknown():
    gw = _gw()
    # 把 b 改成未注册类型 time-range（P0 旧类型）
    res = gw.modify_flow("fid",
                           node_patches=[{"match": {"id": "b"},
                                         "set": {"type": "time-range"}}])
    assert res["ok"] is False, res
    assert res["stage"] == "node_gate"
    assert "未注册" in (res.get("error") or "")


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
