"""Core 档手术刀编辑 + 只读 inventory 守卫测试（Phase B #7/#9，离线）。

运行：python tests/test_nr_client_core_surgical.py
不触真实 NR —— 仅纯函数逻辑 + monkeypatch 网络方法。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "autoflow_gateway" / "lib"))

from nr_client import NodeRedClient, _SURGICAL_FORBIDDEN_KEYS

client = NodeRedClient("http://dummy:1880")  # 不触网，仅构造


# ── get_inventory (#9) ─────────────────────────────

def _flows_fixture():
    return [
        {"id": "tab_af", "type": "tab", "label": "af_gw"},
        {"id": "tab_user", "type": "tab", "label": "书房"},
        {"id": "n1", "type": "inject", "z": "tab_af", "name": "入口"},
        {"id": "n2", "type": "weird_node", "z": "tab_af"},          # 未在已装集 → unknown_node_type
        {"id": "n3", "type": "switch", "z": "tab_user", "name": "灯"},
    ]


def test_inventory_structure_and_ownership():
    client.list_flows = lambda: _flows_fixture()
    client.get_installed_node_types = lambda: {"inject", "switch", "tab"}
    inv = client.get_inventory()
    assert inv["tab_count"] == 2, inv
    assert inv["node_count"] == 3, inv
    by_label = {t["label"]: t for t in inv["tabs"]}
    af = by_label["af_gw"]
    assert af["owned_by_af"] is True
    assert af["node_count"] == 2
    # n2 的 weird_node 不在已装集 → 标 unknown_node_type
    n2 = [n for n in af["nodes"] if n["id"] == "n2"][0]
    assert n2["risks"] == ["unknown_node_type"], n2
    user = by_label["书房"]
    assert user["owned_by_af"] is False
    assert user["node_count"] == 1


def test_inventory_protected_flow_flag():
    client.list_flows = lambda: _flows_fixture()
    client.get_installed_node_types = lambda: {"inject", "switch", "tab"}
    inv = client.get_inventory(protected_flow_ids={"tab_user"})
    user = [t for t in inv["tabs"] if t["label"] == "书房"][0]
    assert "protected_flow" in user["risks"], user


def test_inventory_readonly_no_write():
    # list_flows 被调用即可；若误触写路径（update_flow 等）会炸，这里确保不调用它们
    calls = []
    client.list_flows = lambda: _flows_fixture()
    client.get_installed_node_types = lambda: {"inject", "switch", "tab"}
    orig_update = client.update_flow
    client.update_flow = lambda *a, **k: calls.append("WRITE")
    inv = client.get_inventory()
    assert calls == [], "get_inventory 不得触发任何写"
    assert inv["tab_count"] == 2


# ── modify_node_field 守卫 (#7) ──────────────────────

def test_forbid_structural_keys():
    for key in _SURGICAL_FORBIDDEN_KEYS:
        try:
            client.modify_node_field("f", "n1", {key: "x"})
        except ValueError as e:
            assert key in str(e), e
        else:
            raise AssertionError(f"结构键 {key} 应被拦截却放行")


def test_allow_structural_override():
    # allow_structural=True 应放行（危险路径，仅内部用），但本测试只验证不抛 ValueError
    client.get_flow = lambda fid: {"nodes": [{"id": "n1", "type": "inject"}]}
    client.update_flow = lambda fid, flow, **kw: {"ok": True}
    # 用 allow_structural 绕过结构键拦截
    res = client.modify_node_field("f", "n1", {"id": "x"}, allow_structural=True, dry_run=True)
    assert res["dry_run"] is True, res


def test_dry_run_returns_diff_no_write():
    client.get_flow = lambda fid: {"nodes": [{"id": "n1", "type": "inject", "name": "旧"}]}
    wrote = []
    client.update_flow = lambda *a, **k: wrote.append(True)
    res = client.modify_node_field("f", "n1", {"name": "新"}, dry_run=True)
    assert res["dry_run"] is True
    assert res["diff"] == {"name": {"old": "旧", "new": "新"}}, res
    assert wrote == [], "dry_run 不得写"


def test_apply_updates_field_and_keeps_count():
    flow = {"nodes": [{"id": "n1", "type": "inject", "name": "旧", "x": 10}]}
    client.get_flow = lambda fid: flow
    seen = {}
    def fake_update(fid, f, **kw):
        seen["flow"] = f
        return {"ok": True}
    client.update_flow = fake_update
    res = client.modify_node_field("f", "n1", {"name": "新"})
    assert res["success"] is True
    assert res["node_count"] == 1
    assert flow["nodes"][0]["name"] == "新"   # 已就地改
    assert seen["flow"] is flow                # 部署的是同一 flow 对象


def test_apply_brother_count_guard_fires():
    flow = {"nodes": [{"id": "n1", "type": "inject"}, {"id": "n2", "type": "inject"}]}
    client.get_flow = lambda fid: flow
    def fake_update_that_drops_node(fid, f, **kw):
        # 模拟部署误删节点（异常场景），验证防御断言
        f["nodes"].pop()
        return {"ok": True}
    client.update_flow = fake_update_that_drops_node
    try:
        client.modify_node_field("f", "n1", {"name": "x"})
    except RuntimeError as e:
        assert "节点数" in str(e), e
    else:
        raise AssertionError("兄弟节点数变化应被 fail-closed 拦截")


def test_node_not_found():
    client.get_flow = lambda fid: {"nodes": [{"id": "n1", "type": "inject"}]}
    try:
        client.modify_node_field("f", "missing", {"name": "x"})
    except RuntimeError as e:
        assert "missing" in str(e), e
    else:
        raise AssertionError("不存在的节点应抛错")


if __name__ == "__main__":
    test_inventory_structure_and_ownership()
    test_inventory_protected_flow_flag()
    test_inventory_readonly_no_write()
    test_forbid_structural_keys()
    test_allow_structural_override()
    test_dry_run_returns_diff_no_write()
    test_apply_updates_field_and_keeps_count()
    test_apply_brother_count_guard_fires()
    test_node_not_found()
    print("✅ test_nr_client_core_surgical 全部通过")
