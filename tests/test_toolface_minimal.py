# -*- coding: utf-8 -*-
"""v3.0.0 #19 · 最小稳定工具面守卫（工具面对角色的可见性不变量）。

背景：工具面分三层——
  · `/mcp` + `/mcp-white` → `mcp`（用户服务器）：普通/专家身份；部署刀经 `_DEPLOY_KNIVES` 过滤。
  · `/mcp-admin`         → `mcp_admin`（管理服务器）：仅 developer，额外挂运维刀。
#19 的验收门是「**最小可用集；运维动作不可达**」。本测试把这层角色可见性焊成硬约束，
防止有人（含未来的自己）把运维刀误挂回用户服务器、或让 `_DEPLOY_KNIVES` 出现幽灵名。

不变量：
  A. `_DEPLOY_KNIVES` 每个名字都是用户服务器上**真实存在**的工具（防改名后黑名单失效）。
  B. 运维刀（重启/清池/发布/池统计/提交审计/缺陷闭环）**只挂管理服务器**，用户服务器**不存在**
     → 任何非 admin 身份在 /mcp、/mcp-white 上 tools/list 与 tools/call 均不可达。
  C. 用户服务器上不存在任何名字命中运维刀模式的新工具（防新增泄漏）。
  D. `_filter_tools_list` 真能剥除 `_DEPLOY_KNIVES`（list 层过滤有效）。

零依赖真实网关：仅 import mcp_server 的注册表（内存），不连 NR/HA。
运行：pytest tests/test_toolface_minimal.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 内共享 helper

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_tfmin_")

from autoflow_gateway.config import reset_config  # noqa: E402
reset_config()

import autoflow_gateway.mcp_server as M  # noqa: E402
# ★ 唯一登记处：工具面契约（刀 / 运维刀 / 可见集）不再在本文件手抄一份
import mcp_toolface_contract as C  # noqa: E402

# 运维刀：接管/重启网关、重置/发布任务池、池统计、提交与缺陷审计闭环。
# 这些**不得**出现在用户服务器（/mcp、/mcp-white）——运维动作对普通/专家身份不可达。
OPS_KNIVES = C.OPS_KNIVES

# 用户服务器上任何命中这些子串的工具名都视为「可能泄漏的运维刀」。
_OPS_PATTERNS = ("restart_gateway", "reset_pool", "publish_tasks", "pool_stats",
                 "list_submissions", "list_issues", "resolve_issue")


def _user_tools():
    return set(M._tools_of(M.mcp))


def _admin_tools():
    return set(M._tools_of(M.mcp_admin))


def test_deploy_knives_all_real():
    """A：黑名单无幽灵——每个名字都在用户服务器上真实注册。"""
    ghosts = sorted(set(M._DEPLOY_KNIVES) - _user_tools())
    assert not ghosts, f"_DEPLOY_KNIVES 含不存在（改名/删除后未同步）的工具名: {ghosts}"


def test_ops_knives_admin_only():
    """B：运维刀只在管理服务器，用户服务器不可达。"""
    user = _user_tools()
    admin = _admin_tools()
    leaked = sorted(OPS_KNIVES & user)
    assert not leaked, f"运维刀泄漏到用户服务器（/mcp 可达）: {leaked}"
    missing = sorted(OPS_KNIVES - admin)
    assert not missing, f"运维刀未挂在管理服务器（应仅在 /mcp-admin）: {missing}"


def test_ops_knives_match_runtime_face_diff():
    """B'：契约登记的运维刀必须**恰好**等于「管理面 − 用户面」的运行时差集。

    这是运维刀侧的唯一真相源对账（刀侧的对账在 test_mcp_server_merge.py）。
    差集漂移即意味着有人把运维刀挂进了用户面、或新增了管理面独有的工具未登记。
    """
    diff = _admin_tools() - _user_tools()
    assert diff == set(OPS_KNIVES), (
        f"管理面独有集与契约不一致：\n"
        f"  新增未登记: {sorted(diff - set(OPS_KNIVES))}\n"
        f"  已消失/改名: {sorted(set(OPS_KNIVES) - diff)}"
    )


def test_no_ops_pattern_on_user_server():
    """C：用户服务器无任何命中运维模式的新工具（前瞻护栏）。"""
    hits = sorted(t for t in _user_tools() if any(p in t for p in _OPS_PATTERNS))
    assert not hits, f"用户服务器出现疑似运维刀（应移入 /mcp-admin）: {hits}"


def test_admin_superset_of_knives():
    """D：管理服务器须包含全部部署刀与运维刀（admin 看得到一切）。"""
    admin = _admin_tools()
    need = set(M._DEPLOY_KNIVES) | OPS_KNIVES
    missing = sorted(need - admin)
    assert not missing, f"管理服务器缺少工具（admin 应全量可见）: {missing}"


def test_filter_tools_list_strips_knives():
    """E：list 层过滤真能剥除部署刀，且不动普通工具。"""
    assert M._DEPLOY_KNIVES, "黑名单不应为空"
    knife = sorted(M._DEPLOY_KNIVES)[0]
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"tools": [{"name": knife}, {"name": "autoflow_list_tabs"}]},
    }).encode()
    out = json.loads(M._filter_tools_list(body))
    names = [t["name"] for t in out["result"]["tools"]]
    assert knife not in names, "部署刀未被 list 层过滤"
    assert "autoflow_list_tabs" in names, "普通工具被误删"


def test_trigger_inject_expert_only():
    """决策1 回归：手动扳机（可让设备真实动作、跳过审批）必须收进专家档——普通不可见。"""
    assert "autoflow_trigger_inject" in M._DEPLOY_KNIVES, \
        "trigger_inject 必须属 _DEPLOY_KNIVES（决策1：收进专家档）"
    assert "autoflow_trigger_inject" not in (_user_tools() - set(M._DEPLOY_KNIVES)), \
        "trigger_inject 不得对普通身份可见"


def test_normal_visible_excludes_all_knives():
    """F：普通身份可见集 = 用户服务器 − 部署刀，且不含任何运维刀。"""
    normal_visible = _user_tools() - set(M._DEPLOY_KNIVES)
    assert not (normal_visible & OPS_KNIVES), "普通可见集混入运维刀"
    # 核心只读/提案能力应在普通集内（存在性锚点，防止整集被清空）
    for anchor in ("autoflow_whoami", "autoflow_resolve_entity",
                   "autoflow_propose_dsl", "autoflow_list_entities"):
        assert anchor in normal_visible, f"普通可见集缺核心工具: {anchor}"


def test_audit_index_expert_only():
    """#20 回归：审计轨迹索引属**治理面**——普通身份不可见（_DEPLOY_KNIVES），admin 可见。

    与 autoflow_get_trace 同层：审计读取不砍功能，但只对专家/原生手写身份开放，
    普通身份拿不到网关操作痕迹，避免把运维可观测面暴露给最小可用集。
    """
    tool = "autoflow_list_apply_traces"
    assert tool in M._DEPLOY_KNIVES, f"{tool} 必须属 _DEPLOY_KNIVES（审计属治理面）"
    assert tool in _admin_tools(), f"{tool} 应挂在管理服务器（/mcp-admin）"
    assert tool not in (_user_tools() - set(M._DEPLOY_KNIVES)), \
        f"{tool} 不得对普通身份可见"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
