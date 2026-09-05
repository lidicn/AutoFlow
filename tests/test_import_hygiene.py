#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入卫生守卫（FEEDBACK #13）。

背景：2026-08-02 发现 site-packages 里存在 editable 安装
`_editable_impl_autoflow_gateway.pth` → `D:\\...\\autoflow_gateway\\src`，
解释器启动即把**另一处旧仓库副本**加进 sys.path；而 `tests/conftest.py`
在顶层就 import 了 autoflow_gateway，早于各 test 文件自己的 sys.path.insert
→ 包从旧副本载入并缓存，**测试跑的根本不是本仓库代码**。

这种情况只有在两份代码出现「符号级差异」（新增/删除模块、常量、函数名）时
才会以 ImportError 意外暴露；若差异只在函数体内部，会静默测错对象，
产出**虚假绿灯** —— 比任何单点 bug 都危险。

本文件是那道守卫：凡关键模块，其解析路径必须落在本仓库 src/ 内。
"""
from pathlib import Path

import pytest

_REPO_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()

# 关键模块：编译器 / 网关 / NR 客户端 / 子流程
_CRITICAL_MODULES = [
    "autoflow_gateway",
    "autoflow_gateway.dsl_engine",
    "autoflow_gateway.subflows",
    "autoflow_gateway.lib.nr_client",
]


def _module_path(dotted: str) -> Path:
    mod = __import__(dotted, fromlist=["__file__"])
    return Path(mod.__file__).resolve()


@pytest.mark.parametrize("dotted", _CRITICAL_MODULES)
def test_module_resolves_inside_this_repo(dotted):
    """模块必须来自本仓库 src/，而非 site-packages / 其它仓库副本。"""
    p = _module_path(dotted)
    assert _REPO_SRC in p.parents, (
        f"\n模块 {dotted} 解析到:\n  {p}\n"
        f"但本仓库 src 是:\n  {_REPO_SRC}\n"
        "→ 测试正在验证【别处的代码】，结果不可信。\n"
        "排查：① site-packages 里是否有 _editable_impl_autoflow_gateway.pth 指向旧仓库；\n"
        "     ② tests/conftest.py 是否在 import autoflow_gateway 之前就把本仓库 src "
        "insert 到 sys.path[0]（conftest 先于 test module 导入，顺序错了就无效）。"
    )


def test_no_duplicate_package_copies_on_syspath():
    """sys.path 上若同时存在多份 autoflow_gateway 包，给出明确告警。

    不直接判红（本地可能合法并存），但必须让首位命中的是本仓库，
    避免「改了 A 处代码、跑的却是 B 处」这类幽灵问题。
    """
    import sys

    found = []
    for entry in sys.path:
        if not entry:
            continue
        cand = Path(entry) / "autoflow_gateway" / "__init__.py"
        try:
            if cand.exists():
                found.append(cand.resolve())
        except OSError:
            continue

    assert found, "sys.path 上找不到 autoflow_gateway 包"
    assert _REPO_SRC in found[0].parents, (
        f"sys.path 上首个 autoflow_gateway 是 {found[0]}，不是本仓库 {_REPO_SRC}。\n"
        f"全部候选: {found}"
    )
