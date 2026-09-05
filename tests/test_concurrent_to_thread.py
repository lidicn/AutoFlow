"""并发测试：验证 webui async 处理器不再阻塞事件循环。"""

import re


def test_gw_calls_wrapped_in_to_thread():
    with open("src/autoflow_gateway/webui.py", encoding="utf-8-sig") as f:
        source = f.read()
    critical_handlers = [
        "core_propose_dsl", "core_deploy_proposal", "core_deploy_raw",
        "core_entities", "core_resolve_entity", "list_pending", "approve",
        "reject", "plan_view", "plan_update", "list_commands", "submit_command",
        "list_decisions", "catalog_view", "catalog_import", "entities_view",
        "nr_tabs", "diagnostics_view",
    ]
    for handler in critical_handlers:
        match = re.search(rf'async def {handler}\(request:', source)
        assert match, f"未找到 handler: {handler}"
        start = match.end()
        end = len(source)
        next_func = re.search(r'^(async def |def )\w+', source[start:], re.MULTILINE)
        if next_func:
            end = start + next_func.start() + 4
        func_body = source[start:end]
        gw_calls = re.findall(r'\bgw\.\w+\(', func_body)
        if gw_calls:
            assert 'asyncio.to_thread(' in func_body, f"{handler} 中有 gw.* 调用但未用 to_thread 包裹"
            print(f"  OK {handler} ({len(gw_calls)} gw calls wrapped)")


def test_count_to_thread_wraps():
    with open("src/autoflow_gateway/webui.py", encoding="utf-8-sig") as f:
        source = f.read()
    count = source.count("asyncio.to_thread(")
    print(f"  共 {count} 处 asyncio.to_thread 包裹")
    assert count >= 50, f"预期至少 50 处，实际只有 {count}"


def test_hot_path_gw_no_direct_calls():
    """验证 hot path handler 中 gw.* 调用全在 to_thread 内。"""
    with open("src/autoflow_gateway/webui.py", encoding="utf-8-sig") as f:
        source = f.read()
    async_funcs = list(re.finditer(r'async def (\w+)\(', source))
    hot_path = {
        "core_propose_dsl", "core_deploy_proposal", "core_deploy_raw",
        "core_entities", "core_resolve_entity", "list_pending", "approve",
        "reject", "plan_view", "plan_update", "list_commands", "submit_command",
        "list_decisions", "catalog_view", "catalog_import", "entities_view",
        "nr_tabs", "diagnostics_view",
    }
    for i, m in enumerate(async_funcs):
        name = m.group(1)
        if name not in hot_path:
            continue
        start = m.end()
        end = len(source)
        if i + 1 < len(async_funcs):
            end = async_funcs[i + 1].start()
        func_body = source[start:end]
        # Find all gw.xxx( calls and check each is near a to_thread
        for gm in re.finditer(r'gw\.(\w+)\(', func_body):
            pos = gm.start()
            prefix = func_body[max(0, pos - 300):pos]
            if 'asyncio.to_thread(' not in prefix:
                raise AssertionError(
                    f"Hot path '{name}' has un-wrapped gw.{gm.group(1)}() call"
                )
        print(f"  OK {name}: no direct blocking gw.* calls")


if __name__ == "__main__":
    print("测试 1: 关键 handler 的 to_thread 包裹情况")
    test_gw_calls_wrapped_in_to_thread()
    print()
    print("测试 2: 统计 to_thread 使用数量")
    test_count_to_thread_wraps()
    print()
    print("测试 3: 验证 hot path 无直接阻塞调用")
    test_hot_path_gw_no_direct_calls()
    print()
    print("全部通过！")
