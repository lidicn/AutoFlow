"""nr_client 底层护栏离线回归测试（G1/G3/G4：create_tab 全量替换护栏、
节点级结构 lint、prod 显式护栏）。纯标准库，不连真实 NR。

运行：python tests/test_nr_client_guards.py
"""
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


def test_is_prod():
    # 端口不再决定 prod：默认（无 env）非 prod
    assert nr.NodeRedClient(url="http://x:1880").is_prod() is False
    # AUTOFLLOW_ENV=prod → prod
    os.environ["AUTOFLLOW_ENV"] = "prod"
    try:
        assert nr.NodeRedClient(url="http://x:1880").is_prod() is True
    finally:
        os.environ.pop("AUTOFLLOW_ENV", None)
    # NR_PROD=1 显式标记
    os.environ["NR_PROD"] = "1"
    try:
        assert nr.NodeRedClient(url="http://x:1880").is_prod() is True
    finally:
        os.environ.pop("NR_PROD", None)


def test_lint_r10_multi_array():
    """单 output 节点得 2 个 wires 数组（R10）→ 必须拦。"""
    bad = [{"id": "f1", "type": "tab", "label": "L", "nodes": [
        {"id": "a", "type": "change", "wires": [["b"], ["c"]]},
        {"id": "b", "type": "link out", "wires": [[]]},
        {"id": "c", "type": "link out", "wires": [[]]},
    ]}]
    probs = nr.NodeRedClient(url="http://x:1880")._lint_flows(bad)
    assert any("2 个 wires 数组" in p for p in probs), f"未检出 R10：{probs}"


def test_lint_clean_passes():
    """合法 flow（单 output 单数组 + 多 output switch 双数组）应通过。"""
    good = [{"id": "f1", "type": "tab", "label": "L", "nodes": [
        {"id": "a", "type": "change", "wires": [["b"]]},
        {"id": "s", "type": "switch", "outputs": 2, "wires": [["x"], ["y"]]},
        {"id": "b", "type": "link out", "wires": [[]]},
    ]}]
    probs = nr.NodeRedClient(url="http://x:1880")._lint_flows(good)
    assert probs == [], f"合法 flow 误报：{probs}"


def test_lint_subflow_malformed():
    """子流程 out 畸形 → 必须拦（编辑器 forEach 崩溃级）。"""
    bad = [{"id": "sf", "type": "subflow", "out": [1], "in": []}]
    probs = nr.NodeRedClient(url="http://x:1880")._lint_flows(bad)
    assert any("端口" in p for p in probs), f"未检出子流程畸形：{probs}"


def test_guard_prod_blocks_and_optin():
    # 非 prod（默认 1880，无 AUTOFLLOW_ENV=prod）→ 不抛（公众用户可正常写）
    nr.NodeRedClient(url="http://x:1880")._guard_prod(False, "test")
    # prod 环境（AUTOFLLOW_ENV=prod）未 opt-in → 必须熔断
    os.environ["AUTOFLLOW_ENV"] = "prod"
    try:
        c = nr.NodeRedClient(url="http://x:1880")
        try:
            c._guard_prod(False, "test")
            assert False, "prod 未 opt-in 却放行"
        except nr.NRGuardError:
            pass
        # opt-in：不抛
        c._guard_prod(True, "test")
    finally:
        os.environ.pop("AUTOFLLOW_ENV", None)


def test_compiled_greeting_passes_lint():
    """真实编译产物（书房场景）过 nr_client 结构 lint —— 网关 force 部署不被误伤。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from autoflow_gateway.dsl_engine import compile_dsl
    os.environ.setdefault("AUTOFLLOW_ENV", "staging")
    os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp()
    from autoflow_gateway import config
    config.reset_config()
    dsl = """场景: 书房入座问候与温湿播报
触发: binary_sensor.study_presence 有人
延时: 360 秒
查询: binary_sensor.computer_power 开
    构建: {"model":"doubao","messages":[{"role":"user","content":"hi"}]}
    请求: POST http://<NAS_IP>:9090/v1/chat/completions Content-Type=application/json
    提取: 问候 = payload.choices[0].message.content
    调用子流程: demo_notify(text=`问候`, room=书房, level=一般)
否则:
    调用子流程: demo_notify(text=电脑没开暂不问候, room=书房, level=一般)
取值: sensor.study_temperature 温度
取值: sensor.study_humidity 湿度
提取: 温度数值 = $number(温度)
构建: `{"text": "书房当前" & 温度 & "度，湿度" & 湿度 & "%。", "room":"书房", "level":"一般"}`
调用子流程: demo_notify(text=`payload.text`, room=书房, level=一般)
分支: 温度数值 > 28
    调用子流程: demo_notify(text=偏热建议开空调, room=书房, level=一般)
否则:
    调用子流程: demo_notify(text=温度舒适无需开空调, room=书房, level=一般)
"""
    flow = compile_dsl(dsl)
    probs = nr.NodeRedClient(url="http://x:1880")._lint_flows([flow])
    # 布局 warn 级（如无意回退）不阻塞部署；结构错误必须为空
    errs = [p for p in probs if "wires 数组" in p or "端口" in p or "不是数组" in p]
    assert errs == [], f"编译产物被结构 lint 误伤：{errs}"


def test_create_or_update_flow_preserves_gate():
    """全路径回归：compiled 书房场景经 create_or_update_flow
    （_normalize_flow → _lint_flows → _guard_prod → POST/PUT）时，
    门节点（2-output api-current-state）不得被 _normalize_api_state 误改单输出，
    从而触发 R10 结构 lint。直接抓「normalize+lint 交互」类回归
    （离线 mock _json，不连真实 NR）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from autoflow_gateway.dsl_engine import compile_dsl
    os.environ.setdefault("AUTOFLLOW_ENV", "staging")
    os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp()
    from autoflow_gateway import config
    config.reset_config()
    dsl = """场景: 书房入座问候与温湿播报
触发: binary_sensor.study_presence 有人
延时: 360 秒
查询: binary_sensor.computer_power 开
    构建: {"model":"doubao","messages":[{"role":"user","content":"hi"}]}
    请求: POST http://<NAS_IP>:9090/v1/chat/completions Content-Type=application/json
    提取: 问候 = payload.choices[0].message.content
    调用子流程: demo_notify(text=`问候`, room=书房, level=一般)
否则:
    调用子流程: demo_notify(text=电脑没开暂不问候, room=书房, level=一般)
取值: sensor.study_temperature 温度
取值: sensor.study_humidity 湿度
提取: 温度数值 = $number(温度)
构建: `{"text": "书房当前" & 温度 & "度，湿度" & 湿度 & "%。", "room":"书房", "level":"一般"}`
调用子流程: demo_notify(text=`payload.text`, room=书房, level=一般)
分支: 温度数值 > 28
    调用子流程: demo_notify(text=偏热建议开空调, room=书房, level=一般)
否则:
    调用子流程: demo_notify(text=温度舒适无需开空调, room=书房, level=一般)
"""
    flow = compile_dsl(dsl)
    fid = flow["id"]
    client = nr.NodeRedClient(url="http://x:1880")  # 非 prod → _guard_prod 放行
    # mock _json：GET /flow/:id 视为不存在 → 走 create 路径(POST+PUT)，均不连真实 NR
    def _fake_json(method, endpoint, **kw):
        if method == "GET" and endpoint.startswith("/flow/"):
            raise RuntimeError("404 not found")
        return {"id": fid}
    client._json = _fake_json
    # 关键断言：不得因门节点被 normalize 误改而抛 NRGuardError（R10）
    res = client.create_or_update_flow(fid, flow, force=True)
    assert res.get("id") == fid, f"create_or_update_flow 未返回预期 id：{res}"
    assert res.get("created") is True, f"create 路径未被走：{res}"


def test_create_tab_has_guard_signature():
    """create_tab 应接受 allow_prod 参数（全量替换高危操作已加护栏）。"""
    import inspect
    sig = inspect.signature(nr.NodeRedClient.create_tab)
    assert "allow_prod" in sig.parameters, "create_tab 缺 allow_prod 护栏参数"


def main():
    tests = [
        test_is_prod,
        test_lint_r10_multi_array,
        test_lint_clean_passes,
        test_lint_subflow_malformed,
        test_guard_prod_blocks_and_optin,
        test_compiled_greeting_passes_lint,
        test_create_or_update_flow_preserves_gate,
        test_create_tab_has_guard_signature,
    ]
    failed = False
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
        except AssertionError as e:
            failed = True
            print(f"❌ {t.__name__}: {e}")
        except Exception as e:
            failed = True
            print(f"💥 {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print("\nnr_client 护栏测试存在失败")
        raise SystemExit(1)
    print(f"\nnr_client 护栏测试全部通过 🎉 ({len(tests)} 项)")


if __name__ == "__main__":
    main()
