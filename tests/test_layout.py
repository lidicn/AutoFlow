"""坐标系（BFS 分层自动布局 + 布局 lint + SVG 只读预览）离线回归测试。

设计意图：根治「全部 x=200 单列 / 顺序颠倒但连线正确 / 节点重叠」。
编译后按图结构做最长路径分层，节点坐标有体系、永不重叠；配套布局 lint
（重叠 + 连线回退）与 render_flow_svg（即时可视化，零依赖）。

运行：python tests/test_layout.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import (
    compile_dsl, layout_flow, _lint_layout, render_flow_svg,
)

DSL = """场景: 书房入座问候与温湿播报
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


def _compile():
    os.environ.setdefault("AUTOFLLOW_ENV", "staging")
    os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp()
    from autoflow_gateway import config
    config.reset_config()
    return compile_dsl(DSL)


def test_bfs_layered_columns():
    """编译后应有多个列（x 不同），且节点数 == 坐标去重数（无重叠）。"""
    flow = _compile()
    ns = flow["nodes"]
    xs = sorted(set(n.get("x") for n in ns))
    # 旧版全 x=200 单列；新版应至少 5 列（BFS 深度分层）
    assert len(xs) >= 5, f"列数过少（疑似单列）：{xs}"
    # 坐标去重数 == 节点数 → 无两节点重叠
    seen = set()
    for n in ns:
        k = (n.get("x"), n.get("y"))
        assert k not in seen, f"节点重叠 {k}：{n['id']}"
        seen.add(k)
    assert len(seen) == len(ns)


def test_bfs_no_backward_edge():
    """布局 lint 不应报连线回退（子节点 x 必 >= 父节点 x）。"""
    flow = _compile()
    backward = [i for i in flow["lint"] if i.get("rule") == "LAYOUT_BACKWARD"]
    assert not backward, f"存在连线回退：{backward}"


def test_overlap_lint_detects():
    """_lint_layout 应能抓出坐标重叠。"""
    nodes = [
        {"id": "a", "type": "change", "x": 40, "y": 120, "wires": [[]]},
        {"id": "b", "type": "change", "x": 40, "y": 120, "wires": [[]]},
    ]
    issues = _lint_layout(nodes)
    assert any(i["rule"] == "LAYOUT_OVERLAP" for i in issues), "未检出重叠"


def test_lint_layout_clean_ok():
    """合法分层布局不应产生任何 LAYOUT 级问题。"""
    flow = _compile()
    issues = _lint_layout(flow["nodes"])
    assert issues == [], f"合法布局误报：{issues}"


def test_render_svg_both_modes():
    """render_flow_svg 两模式均返回非空 SVG，且可写盘。"""
    flow = _compile()
    ns = flow["nodes"]
    out = os.path.join(tempfile.mkdtemp(), "prev.svg")

    vert = render_flow_svg(ns, title="纵向", path=out)
    assert vert.startswith("<svg") and "viewBox" in vert
    assert os.path.exists(out), "纵向预览未写盘"

    horiz = render_flow_svg(ns, orientation="horizontal")
    assert horiz.startswith("<svg") and "viewBox" in horiz


def test_layout_idempotent_on_compile():
    """layout_flow 是编译产物的一部分：同 DSL 两次编译坐标一致。"""
    a = _compile()
    b = _compile()
    pa = {(n["id"], n["x"], n["y"]) for n in a["nodes"]}
    pb = {(n["id"], n["x"], n["y"]) for n in b["nodes"]}
    assert pa == pb, "布局非确定性（同输入坐标不一致）"


def main():
    tests = [
        test_bfs_layered_columns,
        test_bfs_no_backward_edge,
        test_overlap_lint_detects,
        test_lint_layout_clean_ok,
        test_render_svg_both_modes,
        test_layout_idempotent_on_compile,
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
        print("\n坐标系测试存在失败")
        raise SystemExit(1)
    print(f"\n坐标系测试全部通过 🎉 ({len(tests)} 项)")


if __name__ == "__main__":
    main()
