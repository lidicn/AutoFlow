#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BUG-2 关键路径 except:pass 补日志 —— 回归守卫。

审计报告 BUG-2：编译/部署/闸门/校验等关键路径存在大量「except: pass」静默吞异常，
会掩盖真实失败（审计追踪写入失败、部署状态清理失败、闸门触发注入失败、验证种子构建失败等）。

修复策略（定向战役，非全量）：只对「编译 / 部署 / 闸门 / 校验 / 状态完整性」关键路径的空 except
补 logger.warning(..., exc_info=True)；刻意 fail-open 的清理/可选特性（ds_bridge 连接关闭、
raw-deploy 日志、snapshot 目录、knowledge_evo 记录等）保持不动。

本守卫双重锁定：
1. 静态：源码中 BUG-2 标记日志的数量 == 22、且恰好覆盖这 15 个关键函数（回退成 pass 即红）。
2. 动态（变异）：monkeypatch 让 lint_flow 抛异常，验证 _self_lint 不再静默、确实记了 BUG-2 日志。
"""
import ast
import os

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]  # autoflow/
MARKER = "BUG-2 关键路径异常被静默吞没"

# （相对仓库根的路径, 该文件内应被覆盖的关键函数集合）
EXPECTED = {
    "src/autoflow_gateway/gateway.py": {
        "deploy_proposal", "deploy_proposals", "validate_flow_schema", "_gate_node_types",
        "_walk", "_try_auto_deploy_with_token", "_rollback", "undeploy", "run_staging_gate",
        "_e2e_soft_check_entities", "_build_vhass_from_staging", "resolve_entity",
        "get_flow", "modify_flow",
    },
    "src/autoflow_gateway/dsl_engine.py": {"_self_lint"},
}
EXPECTED_TOTAL = 22


def _scan(rel_path):
    tree = ast.parse((REPO / rel_path).read_text(encoding="utf-8"))
    funcs_all = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    funcs = set()
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("warning", "exception", "error"):
                args = node.args
                if (args and isinstance(args[0], ast.Constant)
                        and isinstance(args[0].value, str) and MARKER in args[0].value):
                    total += 1
                    # 取最内层（span 最小）的包含函数，避免嵌套函数误判为父函数
                    cand = [fn for fn in funcs_all
                            if fn.lineno <= node.lineno <= getattr(fn, "end_lineno", fn.lineno)]
                    cand.sort(key=lambda fn: getattr(fn, "end_lineno", fn.lineno) - fn.lineno)
                    funcs.add(cand[0].name if cand else "?")
    return total, funcs


def test_bug2_critical_except_logging_lock():
    """静态锁定：22 处关键路径日志齐全，且恰好覆盖约定函数集。"""
    all_funcs = set()
    grand = 0
    for rel in EXPECTED:
        total, funcs = _scan(rel)
        grand += total
        all_funcs |= funcs
    assert grand == EXPECTED_TOTAL, (
        f"BUG-2 关键路径日志数应为 {EXPECTED_TOTAL}，实为 {grand}（回退成 pass 会使其减少）"
    )
    expected = set().union(*EXPECTED.values())
    missing = expected - all_funcs
    extra = all_funcs - expected
    assert not missing and not extra, (
        f"BUG-2 覆盖函数集不匹配：缺失={missing or '无'} 多余={extra or '无'}"
    )


def test_bug2_self_lint_logs_on_injected_failure(monkeypatch, caplog):
    """动态变异：让 lint_flow 抛异常，验证 _self_lint 不再静默、确实记录 BUG-2 日志。"""
    import logging

    import autoflow_gateway.dsl_engine as de

    def _boom(*_a, **_k):
        raise RuntimeError("injected lint failure")

    monkeypatch.setattr(de, "lint_flow", _boom)
    with caplog.at_level(logging.WARNING, logger="autoflow.dsl_engine"):
        result = de._self_lint([])  # 注入失败路径，必须不崩且记日志
    assert isinstance(result, list), "_self_lint 注入异常后不应崩溃"
    assert any(MARKER in r.getMessage() for r in caplog.records), (
        "注入异常后未记录 BUG-2 日志 —— 守卫失效（可能已被回退成 pass）"
    )
