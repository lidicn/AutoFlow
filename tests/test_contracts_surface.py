"""W5 / D13 —— 内部接口契约守门（**契约的可执行登记处**）

目的：把 AutoFlow 的内部接口契约焊成可执行断言。任何一条契约被代码悄悄改掉，这里就红。

覆盖七条：
  1. `NodeRedClient` 类名存在（挡「NRClient 幻觉」类错误 —— 2026-08-01 实际发生过）
  2. NRLayer 转发的 client 方法在 NodeRedClient 上真实存在（挡转发悬空）
  3. HALayer 转发面 + gateway 绕层直调的方法在 HAClient 上真实存在
  4. 依赖注入形参（backend= / nr_layer= / ha_layer=）不被删除
  5. NRLayer 不得暴露 deploy_all 等整实例替换入口
  6. 存储层构造签名统一 config=None（AuditStore 已登记豁免）
  7. gateway 的失败信封 stage 取值不超出白名单

设计取舍：转发面用 **AST 静态扫描**而非硬编码清单 —— 层里新增一个转发方法会被自动纳入
校验，无需维护两份名单。硬编码的只有「绕层直调」白名单，因为那本就是需要被盯住的破口。

★ 关于「契约登记处」★
本文件**就是**这些契约的唯一登记处；每条契约的登记点见下方 `CONTRACT_ANCHORS`，
并由 `test_contract_anchors_exist` 校验其**真实存在**。
（历史教训：本守卫曾通篇引用 `docs/CONTRACTS.md §X` 作为「要同步的文档」，而该文件
**从未入库** —— 守卫把一个不存在的文档当登记处，报错信息等于把开发者指向死路。
按 `docs/README.md` 维护约定 #2「能自动生成的不要手写」，契约登记处一律落在**代码**里，
不再新建手抄文档。）
"""
import ast
import inspect
import io
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "src", "autoflow_gateway")


def _read(rel):
    with io.open(os.path.join(_SRC, rel), encoding="utf-8") as f:
        return f.read()


def _read_repo(rel):
    with io.open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── 契约 → 唯一登记处（仓库相对路径, 该文件内的登记符号）───────────────────
# 守卫报错时引用这里；test_contract_anchors_exist 校验「路径存在 + 符号存在」，
# 从根上消除「守卫指向不存在的文档 / 已改名的常量」这类幽灵引用。
CONTRACT_ANCHORS = {
    "1 依赖注入缝":      ("tests/test_contracts_surface.py", "_DI_SEAMS"),
    "2.1 NodeRedClient": ("src/autoflow_gateway/lib/nr_client.py", "class NodeRedClient"),
    "2.1 NRLayer 禁项":  ("src/autoflow_gateway/nr_layer.py", "NRLayer"),
    "2.2 NR 转发面":     ("src/autoflow_gateway/nr_layer.py", "self.client"),
    "3.2 HA 转发面":     ("src/autoflow_gateway/ha_layer.py", "self.client"),
    "3.2 绕层直调登记":  ("tests/test_contracts_surface.py", "_DOCUMENTED_HA_BYPASS"),
    "4 存储层构造":      ("tests/test_contracts_surface.py", "_STORES"),
    "5.2 失败信封 stage": ("tests/test_contracts_surface.py", "_STAGE_WHITELIST"),
}


def _strip_anchors_table(text: str) -> str:
    """剔掉 `CONTRACT_ANCHORS` 表本身再搜符号。

    否则「符号名只写在登记表里」会**自证成立**（表里出现该字符串 → 正则命中 → 判为存在），
    使得同文件内的幽灵符号检查形同虚设。跨文件锚点不受影响，但统一处理更稳。
    """
    m = re.search(r"^CONTRACT_ANCHORS\s*=\s*\{", text, re.M)
    if not m:
        return text
    end = text.find("\n}", m.end())
    if end == -1:
        return text[:m.start()]
    return text[:m.start()] + text[end + 2:]


def test_contract_anchors_exist():
    """★ 元守卫：每条契约的登记处必须**真实存在**（防幽灵引用复发）。

    这是对 2026-09-22 那个发现的固化防线：本守卫曾通篇引用一个从未入库的
    `docs/CONTRACTS.md`，导致报错信息把开发者指向死路。现在任何一条契约
    只要登记处指向不存在的文件或已改名的符号，本用例立刻红。
    """
    problems = []
    for cid, (rel, symbol) in CONTRACT_ANCHORS.items():
        path = os.path.join(_ROOT, rel)
        if not os.path.isfile(path):
            problems.append(f"[{cid}] 登记处文件不存在: {rel}")
            continue
        try:
            text = _strip_anchors_table(_read_repo(rel))
        except Exception as e:                                    # pragma: no cover
            problems.append(f"[{cid}] 登记处不可读: {rel}（{e}）")
            continue
        if symbol and not re.search(rf"\b{re.escape(symbol)}\b", text):
            problems.append(f"[{cid}] 登记处 {rel} 内找不到符号 `{symbol}`（改名了？）")
    assert not problems, (
        "契约登记处悬空 —— 守卫会把开发者指向不存在的地方：\n  " + "\n  ".join(problems))


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
    """真实类名是 NodeRedClient。契约 §2.1（登记处：lib/nr_client.py）。

    2026-08-01 有人写了 `from ...nr_client import NRClient`，conftest 级 ImportError
    直接让 119 个测试文件全部无法收集。此断言即为该事故的固化防线。
    """
    from autoflow_gateway.lib import nr_client

    assert inspect.isclass(getattr(nr_client, "NodeRedClient", None)), (
        "NodeRedClient 不存在或不是类；若确要改名，先同步 "
        "src/autoflow_gateway/lib/nr_client.py 的类定义与全部 import 点（契约 §2.1）"
    )


# ── 2/3. 转发面不悬空 ──────────────────────────────────────────────────

def test_nr_layer_forwards_resolve_on_backend():
    """NRLayer 转发的每个 client 方法都要在 NodeRedClient 上存在。契约 §2.2。"""
    from autoflow_gateway.lib.nr_client import NodeRedClient

    forwarded = _self_client_attrs("nr_layer.py")
    assert forwarded, "未扫到任何 self.client.* 转发，扫描逻辑可能失效"
    missing = sorted(m for m in forwarded if not hasattr(NodeRedClient, m))
    assert not missing, (
        f"NRLayer 转发到 NodeRedClient 上不存在的方法: {missing}\n"
        f"要么方法被删/改名，要么转发写错。"
        f"同步登记处 src/autoflow_gateway/nr_layer.py（契约 §2.2）。"
    )


def test_ha_layer_forwards_resolve_on_backend():
    """HALayer 转发的每个方法都要在 HAClient 上存在。契约 §3.2。"""
    from autoflow_gateway.lib.ha_client import HAClient

    forwarded = _self_client_attrs("ha_layer.py")
    assert forwarded, "未扫到任何 self.client.* 转发，扫描逻辑可能失效"
    missing = sorted(m for m in forwarded if not hasattr(HAClient, m))
    assert not missing, (
        f"HALayer 转发到 HAClient 上不存在的方法: {missing}；"
        f"同步登记处 src/autoflow_gateway/ha_layer.py（契约 §3.2）"
    )


# 已登记的「绕过 Layer 直接调 .client」破口（gateway.py:6862-6865）。
# 这是技术债，登记在下方 `_DOCUMENTED_HA_BYPASS`（契约 §3.2 唯一登记处）；
# 新增会让下面的测试红，逼迫先登记。
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
        f"确需保留则登记到本文件 `_DOCUMENTED_HA_BYPASS`（契约 §3.2 唯一登记处）。"
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

# 契约 §1 唯一登记处。离线测试全靠这三个注入形参，删任一即让 11 个测试文件的假后端注入失效。
_DI_SEAMS = [
    ("autoflow_gateway.nr_layer", "NRLayer", ["config", "backend"]),
    ("autoflow_gateway.ha_layer", "HALayer", ["config", "backend"]),
    ("autoflow_gateway.gateway", "Gateway", ["config", "ha_layer", "nr_layer"]),
]


@pytest.mark.parametrize("mod_path, cls_name, params", _DI_SEAMS)
def test_dependency_injection_seams_preserved(mod_path, cls_name, params):
    """离线可测性依赖这三个注入形参。删掉任何一个，11 个测试文件的假后端注入全部失效。

    契约 §1（登记处：本文件 `_DI_SEAMS`）。
    """
    import importlib

    cls = getattr(importlib.import_module(mod_path), cls_name)
    sig = inspect.signature(cls.__init__)
    for p in params:
        assert p in sig.parameters, (
            f"{cls_name}.__init__ 丢失注入形参 `{p}=`；"
            f"这是离线测试的唯一缝隙，登记处见本文件 `_DI_SEAMS`（契约 §1）"
        )
        assert sig.parameters[p].default is None, (
            f"{cls_name}.__init__ 的 `{p}` 默认值应为 None（可选注入）"
        )


# ── 5. 危险入口不得上浮到 Layer ────────────────────────────────────────

@pytest.mark.parametrize("banned", ["deploy_all", "restore_snapshot", "put_flows", "replace_all"])
def test_nr_layer_does_not_expose_replace_all(banned):
    """NRLayer 结构上绝不暴露整实例替换（nr_layer.py 模块头的设计约束）。

    POST /flows = 整实例替换，一旦上浮到 Layer，gateway 任何调用点都可能误删全部 flow。
    """
    from autoflow_gateway.nr_layer import NRLayer

    assert not hasattr(NRLayer, banned), (
        f"NRLayer 暴露了整实例替换入口 `{banned}`，违反 lib 层设计约束（契约 §2.1）"
    )


# ── 6. 存储层构造统一 ──────────────────────────────────────────────────

# 契约 §4 唯一登记处。
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
    """存储层统一 `config=None`，测试才能用 tmp_path 隔离 data_dir。契约 §4。

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
        "AuditStore 不再吃 gateway 实例；请更新本文件 `_STORES` 的豁免登记（契约 §4），"
        "并考虑把它并入统一校验"
    )


# ── 7. 失败信封 stage 白名单 ───────────────────────────────────────────

# ★ 契约 §5.2 唯一登记处。stage 是调用方分诊依据，不是自由日志文本。
_STAGE_WHITELIST = {
    "compile", "deploy", "e2e_gate", "entity_check", "entity_whitelist",
    "feature_disabled", "gate", "get_flow", "ha_server_inject", "input",
    "lint", "lint_block", "lint_branch_required", "lint_strict",
    "link_out_unresolved", "load", "logic_block", "node_gate", "not_found",
    "nr_canary", "nr_create_subflow", "patch", "proposal_store",
    "register_subflow", "resolve_whitelist", "restored",
    "retry_budget_exhausted", "semantic_gap", "verified",
    "direct_write_applied", "direct_write_pending",
    "dsl_too_long", "empty_dsl", "schema_block",
    # #20 部署/验证/回滚审计（2026-09-22）：人路径（提案/批量）与其整批回滚的落痕 stage，
    # 与既有 "restored"/"direct_write_*" 同类（同属 apply_traces 归档轨迹）。
    "deployed", "deployed_batch", "rolled_back_batch",
}

_STAGE_RE = re.compile(r'"stage"\s*:\s*"([a-z_0-9]+)"')


def test_failure_envelope_stages_are_documented():
    """stage 是调用方分诊依据，不是自由日志文本。新增须登记到 `_STAGE_WHITELIST`。"""
    actual = set(_STAGE_RE.findall(_read("gateway.py")))
    assert actual, "未扫到任何 stage 字面量，扫描逻辑可能失效"
    undocumented = sorted(actual - _STAGE_WHITELIST)
    assert not undocumented, (
        f"出现未登记的失败信封 stage: {undocumented}\n"
        f"请先确认能否复用既有 stage；确需新增则登记到本文件 `_STAGE_WHITELIST`"
        f"（契约 §5.2 唯一登记处）。"
    )


def test_stage_whitelist_has_no_stale_entries():
    """白名单反向也要准——已消失的 stage 留在名单里会掩盖真实收敛情况。"""
    actual = set(_STAGE_RE.findall(_read("gateway.py")))
    stale = sorted(_STAGE_WHITELIST - actual)
    assert not stale, (
        f"白名单里有 gateway.py 已不再产生的 stage: {stale}；"
        f"请从本文件 `_STAGE_WHITELIST`（契约 §5.2 唯一登记处）移除"
    )
