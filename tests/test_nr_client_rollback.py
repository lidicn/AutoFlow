# -*- coding: utf-8 -*-
"""
P6 加固（Phase 4，离线）：行为级回滚 + 熔断锁定。

- 回滚能力：nr_client 此前只「写前拍快照」却从不恢复本次部署；
  本文件验证新增的 restore_snapshot（整实例回放）+ create_or_update_flow
  的「写前捕获 last-good → 写失败回滚」手术式逻辑（NRRollbackError）。
- 熔断锁定：deploy_all 节点跌幅熔断 + delete_flow 大 flow 删除熔断
  （之前规划记为「已砍」，实际代码已具备，此处显式锁住，防回归）。

纯标准库 + mock _json，不连真实 NR / 不碰 1880 prod。
运行：python tests/test_nr_client_rollback.py   或   python run_tests.py
"""
import json
import os
import sys
import tempfile
import importlib.util

# 直接 import 权威版（绕过 ensure_latest 网路/副本逻辑，纯测逻辑）
_LIB = os.path.join(
    os.path.dirname(__file__), "..", "src", "autoflow_gateway", "lib", "nr_client.py"
)
spec = importlib.util.spec_from_file_location("nr_client_auth", os.path.abspath(_LIB))
nr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nr)


def _minimal_flow(fid, n_nodes=2):
    """最小合法 flow（inject+link out），过 lint 与 normalize。"""
    nodes = [{"id": "a", "type": "inject", "wires": [["b"]]}]
    for i in range(1, n_nodes):
        nodes.append({"id": f"b{i}", "type": "link out", "wires": [[]]})
    return {"id": fid, "label": "x", "nodes": nodes}


def test_restore_snapshot_reapplies_all_flows():
    """restore_snapshot 把快照里每个 flow 重放（create_or_update_flow），
    返回恢复的 flow 数；断言每个 flow 都走了 PUT。"""
    fid = "af_restore"
    flow = _minimal_flow(fid)
    snap_path = os.path.join(tempfile.gettempdir(), "nr_restore_test.json")
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump({
            "_meta": {"label": "t", "node_count": 2},
            "flows": [flow, _minimal_flow("af_other")],
        }, f)

    puts = []
    client = nr.NodeRedClient(url="http://x:1880")

    def _fake_json(method, endpoint, **kw):
        if method == "GET" and endpoint.startswith("/flow/"):
            # 视为已存在 → 走 update 路径（1 PUT）
            return {"id": endpoint.split("/")[-1]}
        if method == "PUT":
            puts.append(endpoint)
            return {"id": endpoint.split("/")[-1]}
        return {"id": "x"}

    client._json = _fake_json
    restored = client.restore_snapshot(snap_path)
    assert restored == 2, f"应恢复 2 个 flow，实得 {restored}"
    # 每个 flow 一次 PUT（update 路径）
    assert puts.count(f"/flow/{fid}") == 1, puts
    assert puts.count("/flow/af_other") == 1, puts


def test_create_or_update_rolls_back_on_failure():
    """写失败（PUT 抛异常）→ 捕获的 last-good 被回滚，
    抛 NRRollbackError；断言写尝试 + 回滚尝试各至少一次 PUT。"""
    fid = "af_rb"
    flow = _minimal_flow(fid)
    last_good = _minimal_flow(fid)  # 部署前该 flow 的态
    puts = []
    client = nr.NodeRedClient(url="http://x:1880")

    def _fake_json(method, endpoint, **kw):
        if method == "GET" and endpoint == f"/flow/{fid}":
            return last_good  # 存在 → 走 update 路径
        if method == "PUT" and endpoint == f"/flow/{fid}":
            puts.append(endpoint)
            raise RuntimeError("write failed")  # 模拟写入失败
        return {"id": fid}

    client._json = _fake_json
    try:
        client.create_or_update_flow(fid, flow, force=True)
        assert False, "写失败却未抛 NRRollbackError"
    except nr.NRRollbackError as e:
        assert e.snapshot_path is None
    # 写尝试(PUT) + 回滚尝试(update_flow→PUT) 各至少一次
    assert puts.count(f"/flow/{fid}") >= 2, f"应至少 2 次 PUT（写+回滚）：{puts}"


def test_create_or_update_no_rollback_on_success():
    """写成功 → 不回滚、返回 created=True、不抛异常；
    仅 1 次 PUT（create 路径内的 update_flow）。"""
    fid = "af_ok"
    flow = _minimal_flow(fid)
    puts = []
    client = nr.NodeRedClient(url="http://x:1880")

    def _fake_json(method, endpoint, **kw):
        if method == "GET" and endpoint.startswith("/flow/"):
            raise RuntimeError("404")  # 不存在 → create 路径
        if method == "PUT" and endpoint == f"/flow/{fid}":
            puts.append(endpoint)
        return {"id": fid}

    client._json = _fake_json
    res = client.create_or_update_flow(fid, flow, force=True)
    assert res.get("created") is True, res
    assert res.get("id") == fid, res
    # create 路径：POST 建壳 + update_flow 内 1 次 PUT
    assert puts.count(f"/flow/{fid}") == 1, f"成功路径应仅 1 次 PUT：{puts}"


def test_circuit_breaker_deploy_all_drop():
    """deploy_all 节点跌幅熔断：线上 100 节点、提案 5 节点（非 force）
    → 抛 NRGuardError 且含「熔断」。"""
    live = [{"id": "t", "type": "tab", "label": "L"}]
    for i in range(100):
        live.append({"id": f"n{i}", "type": "link out", "wires": [[]], "z": "t"})
    client = nr.NodeRedClient(url="http://x:1880")
    client._json = lambda m, e, **kw: live if (m == "GET" and e == "/flows") else {"id": "x"}
    try:
        client.deploy_all([_minimal_flow("af_small", 5)], force=False)
        assert False, "节点跌幅应被熔断"
    except nr.NRGuardError as e:
        assert "熔断" in str(e), str(e)


def test_circuit_breaker_delete_big_flow():
    """delete_flow 大 flow 删除熔断：目标 flow 含 ≥20 节点（非 force）
    → 抛 NRGuardError 且含「熔断」。"""
    target = "t_big"
    live = [{"id": target, "type": "tab", "label": "BIG"}]
    for i in range(25):  # ≥ NR_DELETE_NODE_THRESHOLD(20)
        live.append({"id": f"m{i}", "type": "link out", "wires": [[]], "z": target})
    client = nr.NodeRedClient(url="http://x:1880")
    client._json = lambda m, e, **kw: live if (m == "GET" and e == "/flows") else {"ok": True}
    try:
        client.delete_flow(target, force=False)
        assert False, "大 flow 删除应被熔断"
    except nr.NRGuardError as e:
        assert "熔断" in str(e), str(e)


if __name__ == "__main__":
    tests = [
        test_restore_snapshot_reapplies_all_flows,
        test_create_or_update_rolls_back_on_failure,
        test_create_or_update_no_rollback_on_success,
        test_circuit_breaker_deploy_all_drop,
        test_circuit_breaker_delete_big_flow,
    ]
    failed = False
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed = True
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed = True
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'全部通过' if not failed else '存在失败'}")
    sys.exit(1 if failed else 0)
