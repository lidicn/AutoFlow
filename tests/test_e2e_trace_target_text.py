"""A23(#round5)：run_e2e_trace_raw 拦截文案的「staging 环境」硬编码动态化。

背景：run_e2e_trace_raw 在无 inject/事件入口时返回的拦截理由硬编码「staging 环境」，
但方法已有 target 参数（staging/prod），prod 跑时措辞误导排障。改为 {target} 环境。

验证：target="prod" 文案含「prod 环境」；target="staging" 行为不变（回归）。
拦截发生在部署前、且不触网（纯 flow 结构判断），故用 FakeNR + noop bark 即可。

运行：python -m pytest tests/test_e2e_trace_target_text.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.gateway import Gateway


class _FakeNR:
    def list_flows(self):
        return []

    def get_default_server_id(self):
        return ""

    def get_flow(self, fid):
        return None

    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        return {"id": fid, "created": True}


def _no_inject_flow():
    """无 inject、无 state 入口的 flow → 必命中拦截点，且部署前返回。"""
    return {"id": "t1", "label": "t", "nodes": [
        {"id": "n1", "type": "change", "z": "t1", "wires": [[]]}
    ]}


def _gw(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    gw = Gateway()
    gw.nr = _FakeNR()
    gw._bark_push = lambda *a, **k: None
    # 软校验对「无 HA 节点」flow 本就跳过；显式 stub 彻底隔离实体解析依赖
    gw._e2e_soft_check_entities = lambda flow: []
    return gw


def test_target_prod_renders_prod_env_text(monkeypatch, tmp_path):
    gw = _gw(monkeypatch, tmp_path)
    res = gw.run_e2e_trace_raw(_no_inject_flow(), target="prod")
    assert res["verdict"] == "拦截", f"应拦截：{res}"
    assert any("prod 环境" in r for r in (res.get("reasons") or [])), f"prod 文案应出现：{res.get('reasons')}"


def test_target_staging_keeps_staging_env_text(monkeypatch, tmp_path):
    gw = _gw(monkeypatch, tmp_path)
    res = gw.run_e2e_trace_raw(_no_inject_flow(), target="staging")
    assert res["verdict"] == "拦截", f"应拦截：{res}"
    assert any("staging 环境" in r for r in (res.get("reasons") or [])), f"staging 文案应保留（回归）：{res.get('reasons')}"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\ne2e_trace_target_text: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
