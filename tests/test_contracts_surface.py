"""W5 / D13 —— 内部接口契约守门（docs/CONTRACTS.md 的可执行版）

目的：让 `docs/CONTRACTS.md` 不沦为会过期的文档。任何一条契约被代码悄悄改掉，这里就红。

覆盖 CONTRACTS.md §8 的七条：
  1. `NodeRedClient` 类名存在（挡「NRClient 幻觉」类错误 —— 2026-08-01 实际发生过）
  2. NRLayer 转发的 client 方法在 NodeRedClient 上真实存在（挡转发悬空）
  3. HALayer 转发面 + gateway 绕层直调的方法在 HAClient 上真实存在
  4. 依赖注入形参（backend= / nr_layer= / ha_layer=）不被删除
  5. NRLayer 不得暴露 deploy_all 等整实例替换入口
  6. 存储层构造签名统一 config=None（AuditStore 已登记豁免）
  7. gateway 的失败信封 stage 取值不超出文档白名单

设计取舍：转发面用 **AST 静态扫描**而非硬编码清单 —— 层里新增一个转发方法会被自动纳入
校验，无需维护两份名单。硬编码的只有「绕层直调」白名单，因为那本就是需要被盯住的破口。
"""
import ast
import inspect
import io
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "autoflow_gateway")


def _read(rel):
    with io.open(os.path.join(_SRC, rel), encoding="utf-8") as f:
        return f.read()


def _self_client_attrs(rel):
    """AST 扫出源码里所有 `self.client.<attr>` 的 attr 名。"""
    tree = ast.parse(_read(rel))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        inner = node.value
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == "client"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
        ):
            found.add(node.attr)
    return found


def _bypass_attrs(layer_attr):
    """AST 扫出 gateway.py 里 `self.<layer_attr>.client.<attr>` 的绕层直调。"""
    tree = ast.parse(_read("gateway.py"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        c = node.value  # 期望 self.<layer>.client
        if not (isinstance(c, ast.Attribute) and c.attr == "client"):
            continue
        lay = c.value  # 期望 self.<layer>
        if (
            isinstance(lay, ast.Attribute)
            and lay.attr == layer_attr
            and isinstance(lay.value, ast.Name)
            and lay.value.id == "self"
        ):
            found.add(node.attr)
    return found


# ── 1. 类名 ────────────────────────────────────────────────────────────

def test_nr_client_class_name_is_node_red_client():
    """真实类名是 NodeRedClient。CONTRACTS.md §2.1。

    2026-08-01 有人写了 `from ...nr_client import NRClient`，conftest 级 ImportError
    直接让 119 个测试文件全部无法收集。此断言即为该事故的固化防线。
    """
    from autoflow_gateway.lib import nr_client

    assert inspect.isclass(getattr(nr_client, "NodeRedClient", None)), (
        "NodeRedClient 不存在或不是类；若确要改名，先改 docs/CONTRACTS.md §2.1"
    )


# ── 2/3. 转发面不悬空 ──────────────────────────────────────────────────

def test_nr_layer_forwards_resolve_on_backend():
    """NRLayer 转发的每个 client 方法都要在 NodeRedClient 上存在。CONTRACTS.md §2.2。"""
    from autoflow_gateway.lib.nr_client import NodeRedClient

    forwarded = _self_client_attrs("nr_layer.py")
    assert forwarded, "未扫到任何 self.client.* 转发，扫描逻辑可能失效"
    missing = sorted(m for m in forwarded if not hasattr(NodeRedClient, m))
    assert not missing, (
        f"NRLayer 转发到 NodeRedClient 上不存在的方法: {missing}\n"
        f"要么方法被删/改名，要么转发写错。同步 docs/CONTRACTS.md §2.2。"
    )


def test_ha_layer_forwards_resolve_on_backend():
    """HALayer 转发的每个方法都要在 HAClient 上存在。CONTRACTS.md §3.2。"""
    from autoflow_gateway.lib.ha_client import HAClient

    forwarded = _self_client_attrs("ha_layer.py")
    assert forwarded, "未扫到任何 self.client.* 转发，扫描逻辑可能失效"
    missing = sorted(m for m in forwarded if not hasattr(HAClient, m))
    assert not missing, (
        f"HALayer 转发到 HAClient 上不存在的方法: {missing}；同步 docs/CONTRACTS.md §3.2"
    )


# 已登记的「绕过 Layer 直接调 .client」破口（gateway.py:6862-6865）。
# 这是技术债，登记在 CONTRACTS.md §3.2；新增会让下面的测试红，逼迫先更新文档。
_DOCUMENTED_HA_BYPASS = {"invalidate_registries", "entity_areas", "entity_device_ids"}
_DOCUMENTED_NR_BYPASS = set()


def test_ha_client_bypass_calls_are_documented_and_exist():
    """gateway 绕过 HALayer 直调 .client 的方法：既要真实存在，也不许偷偷新增。

    假 HA 后端必须实现「9 个转发 + 这 3 个直调」共 12 个方法，少一个 refresh_catalog 就 AttributeError。
    """
    from autoflow_gateway.lib.ha_client import HAClient

    actual = _bypass_attrs("ha")
    undocumented = sorted(actual - _DOCUMENTED_HA_BYPASS)
    assert not undocumented, (
        f"新增了未登记的绕层直调 self.ha.client.{undocumented}。\n"
        f"绕层会让假后端契约变大且不易察觉——请优先收进 HALayer；"
        f"确需保留则同步 docs/CONTRACTS.md §3.2 并登记到 _DOCUMENTED_HA_BYPASS。"
    )
    missing = sorted(m for m in actual if not hasattr(HAClient, m))
    assert not missing, f"绕层直调了 HAClient 上不存在的方法: {missing}"


def test_nr_client_bypass_calls_are_documented():
    """NR 侧目前不应有绕层直调（写操作必须经 NRLayer 的防御层）。"""
    actual = _bypass_attrs("nr")
    undocumented = sorted(actual - _DOCUMENTED_NR_BYPASS)
    assert not undocumented, (
        f"出现绕过 NRLayer 的直调 self.nr.client.{undocumented}。\n"
        f"NR 写路径必须经 NRLayer（防御层 + 确认闸），绕层等于绕掉护栏。"
    )


# ── 4. 依赖注入缝不被拆 ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "mod_path, cls_name, params",
    [
        ("autoflow_gateway.nr_layer", "NRLayer", ["config", "backend"]),
        ("autoflow_gateway.ha_layer", "HALayer", ["config", "backend"]),
        ("autoflow_gateway.gateway", "Gateway", ["config", "ha_layer", "nr_layer"]),
    ],
)
def test_dependency_injection_seams_preserved(mod_path, cls_name, params):
    """离线可测性依赖这三个注入形参。删掉任何一个，11 个测试文件的假后端注入全部失效。

    CONTRACTS.md §1。
    """
    import importlib

    cls = getattr(importlib.import_module(mod_path), cls_name)
    sig = inspect.signature(cls.__init__)
    for p in params:
        assert p in sig.parameters, (
            f"{cls_name}.__init__ 丢失注入形参 `{p}=`；这是离线测试的唯一缝隙，见 CONTRACTS.md §1"
        )
        assert sig.parameters[p].default is None, (
            f"{cls_name}.__init__ 的 `{p}` 默认值应为 None（可选注入）"
        )


# ── 5. 危险入口不得上浮到 Layer ────────────────────────────────────────

@pytest.mark.parametrize("banned", ["deploy_all", "restore_snapshot", "put_flows", "replace_all"])
def test_nr_layer_does_not_expose_replace_all(banned):
    """NRLayer 结构上绝不暴露整实例替换（nr_layer.py:7 的设计约束）。

    POST /flows = 整实例替换，一旦上浮到 Layer，gateway 任何调用点都可能误删全部 flow。
    """
    from autoflow_gateway.nr_layer import NRLayer

    assert not hasattr(NRLayer, banned), (
        f"NRLayer 暴露了整实例替换入口 `{banned}`，违反 CONTRACTS.md §2.1 的设计约束"
    )


# ── 6. 存储层构造统一 ──────────────────────────────────────────────────

_STORES = [
    ("state", "SharedState"),
    ("proposals", "ProposalStore"),
    ("task_store", "TaskStore"),
    ("decision_store", "DecisionStore"),
    ("command_store", "CommandStore"),
    ("plan_store", "PlanStore"),
    ("notes", "NoteStore"),
    ("device_guard", "DeviceGuardStore"),
]


@pytest.mark.parametrize("mod_name, cls_name", _STORES)
def test_store_constructors_accept_config(mod_name, cls_name):
    """存储层统一 `config=None`，测试才能用 tmp_path 隔离 data_dir。CONTRACTS.md §4。

    AuditStore(gateway) 是唯一登记豁免，不在本清单。
    """
    import importlib

    cls = getattr(importlib.import_module(f"autoflow_gateway.{mod_name}"), cls_name)
    sig = inspect.signature(cls.__init__)
    assert "config" in sig.parameters, f"{cls_name}.__init__ 缺少 `config` 形参"
    assert sig.parameters["config"].default is None, f"{cls_name} 的 `config` 默认值应为 None"


def test_audit_store_exemption_is_still_accurate():
    """AuditStore 的豁免是「如实登记」，不是「随它去」——它若改成 config= 就该收编回上面的清单。"""
    from autoflow_gateway.audit import AuditStore

    sig = inspect.signature(AuditStore.__init__)
    assert "gateway" in sig.parameters, (
        "AuditStore 不再吃 gateway 实例；请更新 CONTRACTS.md §4 的豁免登记，"
        "并考虑把它并入 _STORES 统一校验"
    )


# ── 7. 失败信封 stage 白名单 ───────────────────────────────────────────

_STAGE_WHITELIST = {
    "compile", "deploy", "e2e_gate", "entity_check", "entity_whitelist",
    "feature_disabled", "gate", "get_flow", "ha_server_inject", "input",
    "lint", "lint_block", "lint_branch_required", "lint_strict",
    "link_out_unresolved", "load", "logic_block", "node_gate", "not_found",
    "nr_canary", "nr_create_subflow", "patch", "proposal_store",
    "register_subflow", "resolve_whitelist", "restored",
    "retry_budget_exhausted", "semantic_gap", "verified",
    "direct_write_applied", "direct_write_pending",
    "dsl_too_long", "empty_dsl", "schema_block", "ssrf_block",
}

_STAGE_RE = re.compile(r'"stage"\s*:\s*"([a-z_0-9]+)"')


def test_failure_envelope_stages_are_documented():
    """stage 是调用方分诊依据，不是自由日志文本。新增须同步 CONTRACTS.md §5.2。"""
    actual = set(_STAGE_RE.findall(_read("gateway.py")))
    assert actual, "未扫到任何 stage 字面量，扫描逻辑可能失效"
    undocumented = sorted(actual - _STAGE_WHITELIST)
    assert not undocumented, (
        f"出现未登记的失败信封 stage: {undocumented}\n"
        f"请先确认能否复用既有 stage；确需新增则同步 docs/CONTRACTS.md §5.2 与本白名单。"
    )


def test_stage_whitelist_has_no_stale_entries():
    """白名单反向也要准——已消失的 stage 留在名单里会掩盖真实收敛情况。"""
    actual = set(_STAGE_RE.findall(_read("gateway.py")))
    stale = sorted(_STAGE_WHITELIST - actual)
    assert not stale, (
        f"白名单里有 gateway.py 已不再产生的 stage: {stale}；请从 CONTRACTS.md §5.2 与本白名单移除"
    )
