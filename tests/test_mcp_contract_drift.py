"""MCP 契约守门测试（零依赖，纯 ast 静态解析）。

背景（R4 §1.1）：MCP 的 tool schema 由 FastMCP 从 Python 函数签名 + docstring **自动生成**
（`grep inputSchema mcp_server.py` == 0，无一处手写 schema）。所以「schema 手写两份」并不
存在，真正的漂移风险在另一层：

    @mcp.tool() 包装函数的签名  ⟷  它实际调用的 gateway 方法签名

包装少一个形参 / 传错一个名字 / 底层新增能力没上浮，schema 就会对 agent 说谎——A20
（底层 `allow_prod` 存在、报错文案还引用它，但工具 schema 里根本没这个参数，agent 无从传）
就是这么产生的。本测试把这层「签名对齐」焊成硬约束。

三条不变量：
  A. 反向漂移：包装对 `gw.X(...)` 传入的每个 kwarg 名，必须是 `gateway.X` 的真实形参。
  B. 死旋钮：包装若声明了 `allow_prod` 形参，就必须真的透传给某个 gateway 调用；
     否则 schema 暴露了一个不起作用的开关（比缺参更有害）。
  C. A20 回归：白箱 e2e 类工具必须显式暴露 `allow_prod`（默认 False），
     否则遇到 prod 写护栏时 agent 只能看着 NRGuardError 干瞪眼。

不 import FastMCP / gateway，纯 ast，可独立运行：
    python tests/test_mcp_contract_drift.py
"""

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_PKG = os.path.join(_REPO, "src", "autoflow_gateway")
GW_PATH = os.path.join(_PKG, "gateway.py")
MCP_PATH = os.path.join(_PKG, "mcp_server.py")

# 包装层本地注入、不属于「用户可传参数」的 kwarg（身份等），不参与 A 检查豁免
KNOWN_WRAPPER_LOCAL: set = set()

# C：必须暴露 allow_prod 的白箱工具（A20 回归锚点）
A20_TOOLS_MUST_EXPOSE_ALLOW_PROD = {
    "autoflow_verify_flow",
    "autoflow_run_e2e_trace",
}


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _gateway_methods(tree):
    """返回 {方法名: [形参名...]}（仅实例方法，排除 self）。"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args
            if args and args[0].arg == "self":
                names = [a.arg for a in args[1:]]
                names += [a.arg for a in node.args.kwonlyargs]
                out[node.name] = names
    return out


def _mcp_tools(tree):
    """返回 [(工具名, [形参名], [(gateway方法名, [传入kwarg名])])]。"""
    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        deco_text = [ast.unparse(d) for d in node.decorator_list]
        if not any(("mcp.tool" in d) or ("mcp_admin.tool" in d) for d in deco_text):
            continue
        params = [a.arg for a in node.args.args]
        params += [a.arg for a in node.args.kwonlyargs]
        calls = []
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            if not isinstance(f, ast.Attribute):
                continue
            recv = f.value
            hit = False
            if isinstance(recv, ast.Call):
                inner = recv.func
                hit = isinstance(inner, ast.Name) and inner.id in ("_gw", "gw")
            elif isinstance(recv, ast.Name):
                hit = recv.id in ("_gw", "gw")
            if hit:
                calls.append((f.attr, [k.arg for k in call.keywords if k.arg]))
        tools.append((node.name, params, calls))
    return tools


def _check():
    gw = _gateway_methods(_parse(GW_PATH))
    tools = _mcp_tools(_parse(MCP_PATH))
    problems = []
    seen_tools = set()

    for tname, params, calls in tools:
        seen_tools.add(tname)
        forwarded = set()
        for method, kw in calls:
            forwarded.update(kw)
            if method not in gw:
                continue  # 非 gateway 实例方法（模块级函数等），不在本测试职责内
            gw_params = gw[method]
            # 不变量 A
            for k in kw:
                if k in KNOWN_WRAPPER_LOCAL:
                    continue
                if k not in gw_params:
                    problems.append(
                        f"[A 反向漂移] {tname}: 调用 gateway.{method}(...) 传入 '{k}'，"
                        f"但 gateway.{method} 形参为 {gw_params}"
                    )
        # 不变量 B
        if "allow_prod" in params and "allow_prod" not in forwarded:
            problems.append(
                f"[B 死旋钮] {tname}: 声明了 allow_prod 形参但未透传给任何 gateway 调用"
            )

    # 不变量 C
    for must in sorted(A20_TOOLS_MUST_EXPOSE_ALLOW_PROD):
        if must not in seen_tools:
            problems.append(f"[C A20] 工具 {must} 未找到（是否被改名/删除？请同步更新本测试锚点）")
            continue
        params = next(p for n, p, _ in tools if n == must)
        if "allow_prod" not in params:
            problems.append(
                f"[C A20] {must} 必须暴露 allow_prod 形参（默认 False），"
                f"当前形参为 {params}"
            )
    return problems


def test_no_mcp_contract_drift():
    problems = _check()
    assert not problems, "MCP 契约漂移检测失败:\n" + "\n".join(problems)


if __name__ == "__main__":
    probs = _check()
    if probs:
        print("MCP 契约漂移检测失败:", file=sys.stderr)
        for p in probs:
            print("  -", p, file=sys.stderr)
        sys.exit(1)
    print("OK: 未发现 MCP 契约漂移（A/B/C 不变量均通过）")
