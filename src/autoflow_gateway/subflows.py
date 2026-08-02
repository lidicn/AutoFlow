"""AutoFlow 预建子流程注册表（P3 子流程库 #1/#2）。

来源：直接取自 NR 真实 flow（见 docs/dsl_design.md §9）。
- demo_notify   = 演示 link_out 编译路径的示例子流程（占位 entry，需用户自备下游入口），
                 仅用于测试/文档示例，非产品功能。

设计铁律（§18.2/§9.3）：状态ful 基础设施（队列/互斥/计时循环/全局状态）**不属于 DSL 表达对象**，
由这里 hand-build 为预建子流程，agent 只"按名调用"不"重建"。

引擎编译子流程调用时，按 `call` 字段生成：
- type="link_out"  → 生成一个 `change`(设 msg.payload) + `link out`(指向 entry_link_id) 节点对。
  这是与真实 weigh flow 完全一致的方式（它也是 link out 到示例入口 DEMO_NOTIFY_ENTRY_LINK_ID）。
- type="subflow"   → 生成一个 `subflow` 实例节点，引用已部署的 subflow type。
  当前范例是 bark_push；历史查询 history_* 4 个能力同样走此模式（请求/响应，
  子流程经输出口把答案透传回 msg.payload 供下游分支）。与 link_out 的
  fire-and-forget 单向模型本质不同（weather/anysearch/demo_notify 不返回值）。
"""

from __future__ import annotations

import json
import os
import re

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Param:
    name: str
    required: bool = False
    default: Any = None
    type: str = "str"  # str | int | float | bool
    enum: Optional[list[str]] = None
    desc: str = ""


@dataclass
class SubflowSpec:
    name: str
    title: str
    call: dict  # {"type":"link_out","entry_link_id": "..."} | {"type":"subflow","subflow_id":"..."}
    params: dict[str, Param] = field(default_factory=dict)
    description: str = ""
    notes: str = ""
    # "payload" = 入参整体塞进 msg.payload(dict) 或 payload.<k>（默认，适配 link_out/weigh 入口）
    # "flat"    = 入参平铺到 msg.<k>（适配直接读 msg.title/msg.body 的子流程，如 Bark）
    param_style: str = "payload"
    # 位置参数顺序：声明后，调用方可用 无 key 的位置参数（如 bark_push(标题, 正文)），
    # 引擎按此顺序填入对应命名参数，容错 agent 漏写 key= 的常见写法。None 表示不支持位置参数。
    positional: Optional[list[str]] = None
    # 来源：managed（网关预置，参数契约权威，调用方传参严格校验）
    #       imported（用户从 NR 自省导入，input_schema 为 best-effort 推断，传参宽松）
    source: str = "managed"

    def resolve_args(self, raw: dict[str, str]) -> dict[str, Any]:
        """合并默认值、类型转换、枚举校验，返回规范化入参。

        缺必填或枚举非法抛 ValueError（供引擎静态校验捕获）。
        """
        out: dict[str, Any] = {}
        for pname, p in self.params.items():
            # WB24 NEW-F3：必填参数给了「空串」视为缺失（与 validate_args 一致），
            # 避免 anysearch_batch(keywords=) 这类「必填但空值」被当成已填而静默逃逸。
            if pname in raw and str(raw[pname]).strip() != "":
                val = raw[pname]
                if p.enum and val not in p.enum:
                    raise ValueError(
                        f"子流程 {self.name} 参数 {pname}='{val}' 非法，应为 {p.enum}"
                    )
                out[pname] = _coerce(val, p.type)
            elif p.required:
                raise ValueError(f"子流程 {self.name} 缺少必填参数：{pname}")
            elif p.default is not None:
                out[pname] = p.default
        return out

    def validate_args(self, raw: dict[str, str], strict: bool = False) -> None:
        """编译期校验调用方入参。strict=True 时未知参数也报错（managed 子流程用，
        用于捕获拼写错误）；imported 子流程 schema 为 best-effort 推断，宽松不报未知参数。
        缺必填 / 枚举非法 /（strict 时）未知参数 → 抛 ValueError（msg 含可读原因）。

        WB24 NEW-F3：必填参数若给了空串（如 `anysearch_batch(keywords=)`），与缺失等价 ——
        此前「值存在即算填了」导致空必填静默放行，与 history 系列（缺参被拦）校验覆盖不一致。
        现统一：必填 + 空串 → 报「缺少必填参数」。
        """
        for pname, p in self.params.items():
            if pname in raw and str(raw[pname]).strip() != "":
                if p.enum and raw[pname] not in p.enum:
                    raise ValueError(
                        f"子流程 {self.name} 参数 {pname}='{raw[pname]}' 非法，应为 {p.enum}")
            elif p.required:
                raise ValueError(f"子流程 {self.name} 缺少必填参数：{pname}")
        if strict:
            # 空串未知参数视为未提供，不计入未知（容忍 agent 多写空位）
            unknown = [k for k in raw if k not in self.params and str(raw[k]).strip() != ""]
            if unknown:
                raise ValueError(
                    f"子流程 {self.name} 收到未知参数：{unknown}（已声明：{list(self.params)})")


def _coerce(val: str, t: str) -> Any:
    if t == "int":
        return int(val)
    if t == "float":
        return float(val)
    if t == "bool":
        return val.strip().lower() in ("1", "true", "yes", "是")
    return val


# ── 子流程库 ──────────────────────────────────────────────────────────────
# 示例 link_out 入口占位 id（demo_notify 仅演示 link_out 编译路径，非真实部署）
DEMO_NOTIFY_ENTRY_LINK_ID = "b595563939283231"

# Bark 推送子流程：NR 上手动创建的子流程 id（env 内置 BARK_SERVER/BARK_KEY，密钥不进 git）。
BARK_SUBFLOW_ID = "b0bbc86abb2172a5"

# 历史查询 4 个子流程：请求/响应语义（agent 需拿返回值做分支），仿 bark_push 注册为
# type="subflow"，由 NR 子流程实例真正干活（link in → 时间解析 → api-get-history / statistics
# → 计算 → link out），网关只引用 subflow_id。
# 以下 4 个 id 已于 Task #272（2026-07-21）部署到 NR，回填真实子流程 id
# （与构建脚本 build_subflows.py 的 HIST_IDS 对齐；该 dev 脚本不随发布版分发）。
HISTORY_STATE_AT_SUBFLOW_ID = "af_hist_state_at"
HISTORY_OCCURRED_SUBFLOW_ID = "af_hist_occurred"
HISTORY_DURATION_SUBFLOW_ID = "af_hist_duration"
HISTORY_AGGREGATE_SUBFLOW_ID = "af_hist_aggregate"


def _load_subflows() -> dict[str, SubflowSpec]:
    """从 data/subflows/subflows.json 加载网关预置（managed）子流程。

    数据/代码分离：JSON 是 SubflowSpec 的 dataclasses.asdict 序列化数组，与
    api_specs.json 同构；scripts/export_specs_to_json.py 可零手抄往返导出。
    API_SPECS 派生的 link_out 能力【不在此文件内】，在本模块末尾动态合并进 SUBFLOWS。
    """
    path = os.path.join(os.path.dirname(__file__), "data", "subflows", "subflows.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, SubflowSpec] = {}
    for d in raw:
        params = {k: Param(**pv) for k, pv in (d.get("params") or {}).items()}
        rest = {k: v for k, v in d.items() if k != "params"}
        spec = SubflowSpec(params=params, **rest)
        out[spec.name] = spec
    return out


SUBFLOWS: dict[str, SubflowSpec] = _load_subflows()

# 常量 ↔ 数据一致性守卫：拆分后 JSON 里的 subflow_id/entry_link_id 与本文件常量是
# 两份副本，一旦漂移（改了常量忘了改数据）就会编译出指向不存在子流程的 flow，
# 且只在真机部署时才炸。这里启动即校验，快速失败。
_EXPECTED_CALL_IDS = {
    "demo_notify": ("entry_link_id", DEMO_NOTIFY_ENTRY_LINK_ID),
    "bark_push": ("subflow_id", BARK_SUBFLOW_ID),
    "history_state_at": ("subflow_id", HISTORY_STATE_AT_SUBFLOW_ID),
    "history_occurred": ("subflow_id", HISTORY_OCCURRED_SUBFLOW_ID),
    "history_duration": ("subflow_id", HISTORY_DURATION_SUBFLOW_ID),
    "history_aggregate": ("subflow_id", HISTORY_AGGREGATE_SUBFLOW_ID),
}
for _k, (_f, _want) in _EXPECTED_CALL_IDS.items():
    _spec = SUBFLOWS.get(_k)
    if _spec is None:
        raise RuntimeError(
            f"预置子流程 {_k} 缺失：data/subflows/subflows.json 不完整"
            "（发布包是否漏带数据文件？见 tests/test_repo_integrity.py）")
    if _spec.call.get(_f) != _want:
        raise RuntimeError(
            f"预置子流程 {_k} 的 {_f}={_spec.call.get(_f)!r} 与代码常量 {_want!r} 不一致，"
            "请改数据或改常量后重跑 scripts/export_specs_to_json.py")

# ── 历史查询子流程：幂等 ensure（仿 bark_push 的 A3 模式）──────────────
# 4 个历史子流程（af_hist_*）的【原生节点图】存于 data/subflows/nr_defs/subflows_built.json
# （每个子流程 = [def 节点 + n_parse + n_hist + n_catch + n_err + n_calc] 扁平条目数组）。
# Task #272（2026-07-21）已手动部署进 NR 并回填 id；但此前无等价 ensure 函数——
# 一旦 NR 被清空/重置即永久丢失（这是 agent「历史查询子流程无法使用」的三重根因之一）。
# ensure_history_subflow 幂等重建：list_flows 命中即 no-op；缺失则从 built.json 加载、
# 把硬编码 server 替换为 nr.get_default_server_id()（保证可移植），经安全 append 路径部署。
# 仅 staging 实例调用（allow_prod=False），prod 环境需显式 allow_prod。
HISTORY_SUBFLOW_IDS = {
    HISTORY_STATE_AT_SUBFLOW_ID, HISTORY_OCCURRED_SUBFLOW_ID,
    HISTORY_DURATION_SUBFLOW_ID, HISTORY_AGGREGATE_SUBFLOW_ID,
}

_HISTORY_BUILT_PATH = os.path.join(
    os.path.dirname(__file__), "data", "subflows", "nr_defs", "subflows_built.json")


def _load_history_subflows_built() -> list:
    """读取 subflows_built.json → 4 个子流程的原生扁平条目数组列表。"""
    with open(_HISTORY_BUILT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _count_internal_nodes(flows, subflow_ids) -> Dict[str, int]:
    """统计每个历史子流程的内部节点数（z==sid 且 type!=subflow）。双兼容扁平/嵌套。"""
    nodes: List[Dict] = []
    if isinstance(flows, list):
        nodes = [n for n in flows if isinstance(n, dict)]
    elif isinstance(flows, dict):
        for key in ("flows", "nodes", "subflows"):
            v = flows.get(key)
            if isinstance(v, list):
                nodes.extend(n for n in v if isinstance(n, dict))
        if not nodes and isinstance(flows.get("flow"), dict):
            nodes = [flows["flow"]]
    counts = {sid: 0 for sid in subflow_ids}
    for n in nodes:
        z = n.get("z")
        if z in subflow_ids and n.get("type") != "subflow":
            counts[z] += 1
    return counts


def ensure_history_subflow(nr, allow_prod: bool = False) -> Dict[str, Any]:
    """幂等确保 4 个历史查询子流程存在于目标 NR 实例。

    - 已存在且内部节点>0（非空壳）→ no-op。
    - 缺失 或 退化成空壳（内部节点=0，#607 复发态）→ 从 subflows_built.json 重建。
      重建前先从线上剔除该 sid 的全部条目（def + 内部节点），避免 deploy_all 复用旧空壳
      def 不补内部节点（#607 空壳复用陷阱）；用 force+allow_partial 仅替换命中子流程。
    仅 staging 实例调用（allow_prod=False），prod 环境需显式 allow_prod。
    """
    try:
        flows = nr.list_flows()
    except Exception:
        flows = []
    internal = _count_internal_nodes(flows, HISTORY_SUBFLOW_IDS)
    # 存在且内部节点>0 才算 OK；空壳(=0)视为需重建（#607 复发根因：
    # 原 ensure 只看 id 是否存在，空壳 id 在就 no-op，导致 recurring 退化）
    present_ok = {sid for sid in HISTORY_SUBFLOW_IDS if internal.get(sid, 0) > 0}
    missing = [sid for sid in HISTORY_SUBFLOW_IDS if sid not in present_ok]
    if not missing:
        return {"created": False, "exists": True, "missing": [], "rebuilt": [],
                "shells_rebuilt": []}

    # 加载 built.json：每个数组首元素即 subflow def（含 id/name/in/out/info/env），
    # 其余为内部节点（z 已指向 subflow_id）。
    built = _load_history_subflows_built()
    by_id = {arr[0]["id"]: arr for arr in built if arr and arr[0].get("type") == "subflow"}
    server = nr.get_default_server_id()

    all_entries: list = []
    rebuilt: list = []
    for sid in missing:
        arr = by_id.get(sid)
        if not arr:
            continue
        # 替换硬编码 HA server（built.json 里写死 e93e1ad9c034e866，不可移植）
        for e in arr:
            if e.get("type") == "api-get-history":
                e["server"] = server
        all_entries.extend(arr)
        rebuilt.append(sid)

    if all_entries:
        live = nr.list_flows()
        live_nodes: List[Dict] = []
        if isinstance(live, list):
            live_nodes = [n for n in live if isinstance(n, dict)]
        elif isinstance(live, dict):
            for key in ("flows", "nodes", "subflows"):
                v = live.get(key)
                if isinstance(v, list):
                    live_nodes.extend(n for n in v if isinstance(n, dict))
        # 剔除 missing 子流程的全部线上条目（def + 内部节点），其余保留，
        # 仅替换命中子流程（避免清场其余 tab）。再 force+allow_partial 部署完整 def+内部。
        kept = [n for n in live_nodes
                if n.get("id") not in missing and n.get("z") not in missing]
        combined = kept + all_entries
        nr.deploy_all(combined, force=True, allow_partial=True, allow_prod=allow_prod)
    shells = [sid for sid in missing if internal.get(sid, 0) == 0]
    return {"created": bool(rebuilt), "exists": not missing, "missing": missing,
            "rebuilt": rebuilt, "shells_rebuilt": shells}


def flow_uses_history_subflow(nodes) -> bool:
    """判断原始 flow 节点里是否引用了任一历史查询子流程实例。

    兼容两种写法：
      - NR5 前缀型 type="subflow:<id>"（dsl_engine 产出，当前标准写法）
      - 裸型 type="subflow" + c:"<id>"（个别手搓/旧导出）
    """
    for n in nodes or []:
        nt = n.get("type", "")
        for sid in HISTORY_SUBFLOW_IDS:
            if nt == f"subflow:{sid}":
                return True
            if nt == "subflow" and n.get("c") == sid:
                return True
    return False


# ── API 能力从 api_specs 单一真相源派生（见 api_specs.py）──────────────
# 不再在此手搓 llm_doubao_chat / llm_doubao_say：它们的网关注册与 NR tab flow
# 都由 api_specs.API_SPECS 一处定义、两处派生，避免"改一处漏一处"的 split。
from .api_specs import API_SPECS  # noqa: E402  (循环导入：subflows 先定义 Param/SubflowSpec/SUBFLOWS)
for _api_spec in API_SPECS:
    SUBFLOWS[_api_spec.name] = _api_spec.to_subflow_spec()


def get_subflow(name: str, registry_store=None) -> Optional[SubflowSpec]:
    # 1) 网关预置（SUBFLOWS 硬编码清单）优先
    spec = SUBFLOWS.get(name)
    if spec is not None:
        return spec
    # 2) 查注册表（用户从 NR 自省导入的 imported 且 active 子流程）
    #    registry_store 可由调用方传入；否则用网关启动时注入的模块级单例。
    store = registry_store if registry_store is not None else _registry_store
    if store is not None:
        meta = store.get_subflow_meta(name)
        if meta and meta.get("status") == "active":
            kind = meta.get("kind", "subflow")
            if kind == "link_out":
                # 注册表也可登记 link_out 型能力（如导入的 fire-and-forget 能力）：
                # 网关发 link out 到 entry_link_id，无 NR 子流程实例。
                entry = meta.get("entry_link_id")
                if entry:
                    return SubflowSpec(
                        name=name,
                        title=meta.get("title") or name,
                        call={"type": "link_out", "entry_link_id": entry},
                        params=_params_from_schema(meta.get("input_schema") or []),
                        description=meta.get("spec_ref") or "",
                        param_style="payload",
                        source="imported",
                    )
            else:
                nr_id = meta.get("nr_subflow_id")
                if nr_id:
                    return SubflowSpec(
                        name=name,
                        title=meta.get("title") or name,
                        # 调用走子流程实例：type=subflow:<nr_subflow_id>
                        call={"type": "subflow", "subflow_id": nr_id},
                        params=_params_from_schema(meta.get("input_schema") or []),
                        description=meta.get("spec_ref") or "",
                    # 与 introspect 推断的 msg.<x> 读取对齐：入参平铺到 msg.<k>
                    param_style="flat",
                    source="imported",
                )
    return None


# 注册表 store 注入点（模块级单例）。网关启动时调用 set_registry_store(gateway.task_store)
# 注入；离线/测试可手动注入。get_subflow 查注册表即用此单例（除非显式传 registry_store）。
_registry_store = None


def set_registry_store(store) -> None:
    """注入 TaskStore 实例，使 get_subflow 能查 subflow_registry 表。"""
    global _registry_store
    _registry_store = store


def _params_from_schema(schema) -> dict:
    """把注册表的 input_schema（[{name,required,type,default,enum,desc}]）
    转成 SubflowSpec.params（{name: Param}），供 resolve_args 复用默认值/类型/枚举校验。"""
    out: dict = {}
    for p in schema or []:
        name = p.get("name")
        if not name:
            continue
        out[name] = Param(
            name=name,
            required=bool(p.get("required", False)),
            default=p.get("default"),
            type=p.get("type", "str") or "str",
            enum=p.get("enum"),
            desc=p.get("desc", ""),
        )
    return out


def _schema_from_params(params: dict) -> list:
    """_params_from_schema 的逆操作：SubflowSpec.params（{name: Param}）→ 注册表 input_schema 列表。"""
    out = []
    for name, p in (params or {}).items():
        out.append({
            "name": name,
            "required": bool(getattr(p, "required", False)),
            "type": getattr(p, "type", "str") or "str",
            "default": getattr(p, "default", None),
            "enum": getattr(p, "enum", None),
            "desc": getattr(p, "desc", ""),
        })
    return out


# ── 注册校验门（#575 Full 阶段）：注册/导入前的统一校验 ──────────────────
# 校验 key 合法性 + 不与预置撞名 + input_schema/env_requirements 结构，
# 返回 {ok, error, cleaned}。cleaned 为规范化后的字段，供 register_subflow 直接使用。
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_subflow_registration(key, nr_subflow_id, source_type="imported",
                                  title="", input_schema=None,
                                  env_requirements=None) -> dict:
    """注册校验门：返回 {ok, error, cleaned}。

    - key 必须是 DSL 安全标识符（[A-Za-z_][A-Za-z0-9_]*），且不得与网关预置子流程撞名
    - imported 必须带 nr_subflow_id
    - input_schema 必须是 [{name,required?,type?,default?,enum?,desc?}] 列表，每项 name 为字符串
    - env_requirements 必须是字符串列表（或 {name} 字典列表）
    """
    if not key or not str(key).strip():
        return {"ok": False, "error": "key（DSL 调用名）必填"}
    key = str(key).strip()
    if not _KEY_RE.match(key):
        return {"ok": False, "error": f"key 须为字母/下划线开头的标识符（[A-Za-z_][A-Za-z0-9_]*），当前：{key}"}
    if key in SUBFLOWS:
        return {"ok": False, "error": f"key 与网关预置子流程撞名：{key}（请换一个名字）"}
    st = (source_type or "imported").lower()
    if st not in ("managed", "imported"):
        return {"ok": False, "error": f"source_type 须为 managed/imported，当前：{st}"}
    if st == "imported" and not nr_subflow_id:
        return {"ok": False, "error": "imported 子流程必须提供 nr_subflow_id"}
    # input_schema 规范化
    cleaned_schema = []
    if input_schema:
        if not isinstance(input_schema, list):
            return {"ok": False, "error": "input_schema 必须是列表"}
        for i, p in enumerate(input_schema):
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p.get("name"):
                return {"ok": False, "error": f"input_schema[{i}] 缺 name 或 name 非字符串"}
            cleaned_schema.append({
                "name": p["name"],
                "required": bool(p.get("required", False)),
                "type": p.get("type", "str") or "str",
                "default": p.get("default"),
                "enum": p.get("enum"),
                "desc": p.get("desc", ""),
            })
    # env_requirements 规范化 → 字符串列表
    cleaned_env = []
    if env_requirements:
        if not isinstance(env_requirements, list):
            return {"ok": False, "error": "env_requirements 必须是列表"}
        for e in env_requirements:
            if isinstance(e, str):
                cleaned_env.append(e)
            elif isinstance(e, dict) and isinstance(e.get("name"), str):
                cleaned_env.append(e["name"])
            else:
                return {"ok": False, "error": f"env_requirements 项须为字符串或 {{name}} 字典，当前：{e!r}"}
    return {"ok": True, "cleaned": {
        "key": key,
        "title": (title or key).strip(),
        "nr_subflow_id": (nr_subflow_id or None),
        "source_type": st,
        "input_schema": cleaned_schema,
        "env_requirements": cleaned_env,
    }}


# 网关预置、需在注册表登记的「subflow 实例型」子流程（NR 子流程实例，需 nr_subflow_id）。
# link_out 型能力（demo_notify / apisay / weather / anysearch）不再排除，
# 见 seed_managed_subflows：它们以 kind=link_out 纳入治理（fire-and-forget，网关发 link out
# 到 entry_link_id，无 NR 子流程实例，但仍可在 WebUI 查看入参与状态）。
_MANAGED_SUBFLOW_KEYS = (
    "bark_push",
    "history_state_at", "history_occurred",
    "history_duration", "history_aggregate",
)


def _env_requirements_for_managed(key: str) -> list:
    """预置子流程的 env 配置变量需求。

    - bark_push：需 NAS 自建 bark-server 的 BARK_SERVER/BARK_KEY 及加密材料；
    - history_*：无 env 需求。
    """
    if key == "bark_push":
        return ["BARK_SERVER", "BARK_KEY", "BARK_CIPHER_KEY", "BARK_CIPHER_IV"]
    return []


def seed_managed_subflows(store) -> dict:
    """把网关预置子流程写入 subflow_registry（#578/#587，幂等）。

    覆盖两类：
      - subflow 实例型（_MANAGED_SUBFLOW_KEYS：bark_push / history_* 等），需 nr_subflow_id；
      - link_out 型（SUBFLOWS 中 call.type=="link_out" 的能力：demo_notify /
        apisay / weather / anysearch），网关只发 link out 到 entry_link_id，无 NR 子流程实例。
    仅 seed 未在表中的 key（已存在则跳过，保护用户可能手动改过的 status / input_schema /
    title）。返回 {ok, seeded, skipped}。运行时机：网关启动时（Gateway.__init__ 注入 store 后）。
    """
    seeded, skipped = 0, 0
    seen = set()
    # 1) subflow 实例型（_MANAGED_SUBFLOW_KEYS）
    for key in _MANAGED_SUBFLOW_KEYS:
        spec = SUBFLOWS.get(key)
        if spec is None:
            continue
        seen.add(key)
        if store.get_subflow_meta(key):
            skipped += 1
            continue
        call = spec.call
        nr_id = call.get("subflow_id") if call.get("type") == "subflow" else None
        r = store.register_subflow(
            key, title=spec.title, nr_subflow_id=nr_id,
            source_type="managed",
            input_schema=_schema_from_params(spec.params),
            env_requirements=_env_requirements_for_managed(key),
            owner="system", status="active", spec_ref=key,
            kind="subflow",
        )
        if r["ok"]:
            seeded += 1
    # 2) link_out 型能力（fire-and-forget，网关发 link out 到 entry_link_id）
    for key, spec in SUBFLOWS.items():
        call = spec.call or {}
        if call.get("type") != "link_out":
            continue
        if key in seen:
            continue
        if store.get_subflow_meta(key):
            skipped += 1
            continue
        r = store.register_subflow(
            key, title=spec.title, nr_subflow_id=None,
            source_type="managed",
            input_schema=_schema_from_params(spec.params),
            env_requirements=[],
            owner="system", status="active", spec_ref=key,
            kind="link_out", entry_link_id=call.get("entry_link_id"),
        )
        if r["ok"]:
            seeded += 1
    return {"ok": True, "seeded": seeded, "skipped": skipped}


# ── NR 子流程自省（#576）：导入用户既有子流程时免手填前置参数 ──────────────
# NR 子流程 def 不声明形式入参；真实调用方入参藏在内部节点读 msg.<x> 里，
# 子流程级配置变量则在 def.env 显式声明。自省二者，供 WebUI 注册时自动填充。
_MSG_READ_RE = re.compile(r"msg\.(?:payload\.)?([A-Za-z_][A-Za-z0-9_]*)")
# 消息信封字段：非业务入参，过滤掉避免噪声
_RESERVED_MSG = {"payload", "topic", "headers", "url", "req", "res",
                 "error", "_session", "statusCode", "responseUrl"}


def _introspect_nr_subflow_from_flows(flows, nr_subflow_id: str) -> dict:
    """从 NR flows 列表（list_flows 返回）自省抽取某子流程的『前置参数』。

    返回 {ok, nr_subflow_id, title, in_ports, out_ports, env_requirements,
          input_schema, internal_node_count} 或 {ok:False, error}。
      - env_requirements：def.env → 子流程级配置变量（owner 需提供），[{name,type}]
      - input_schema    ：扫描内部节点 function/change 的 msg.<x> 读取，best-effort
                          推断调用方入参（NR 不声明形式入参，真实入参藏在函数读 msg 中）
    纯函数、无副作用，便于离线 mock 测试。
    """
    def_entry = None
    for f in flows or []:
        if f.get("type") == "subflow" and f.get("id") == nr_subflow_id:
            def_entry = f
            break
    if def_entry is None:
        return {"ok": False, "error": f"NR 中未找到 type=subflow 且 id={nr_subflow_id}"}

    z = nr_subflow_id
    internal = [n for n in flows if n.get("z") == z]
    text = []
    for n in internal:
        if n.get("type") == "function":
            text.append(n.get("func", "") or "")
        elif n.get("type") == "change":
            for r in n.get("rules", []) or []:
                text.append(f"{r.get('to', '')} {r.get('p', '')}")
        elif n.get("type") == "template":
            text.append(n.get("format", "") or n.get("field", ""))
    reads = set()
    for t in text:
        for m in _MSG_READ_RE.finditer(t or ""):
            reads.add(m.group(1))
    input_names = sorted(reads - _RESERVED_MSG)
    input_schema = [{
        "name": nm, "required": False, "type": "str",
        "default": None, "enum": None,
        "desc": f"（自省推断）来自子流程内部 msg.{nm} 读取",
    } for nm in input_names]

    env_reqs = [{"name": e.get("name"), "type": e.get("type", "str")}
                for e in def_entry.get("env", []) or []]

    return {
        "ok": True,
        "nr_subflow_id": nr_subflow_id,
        "title": def_entry.get("name", "") or "",
        "in_ports": len(def_entry.get("in", []) or []),
        "out_ports": len(def_entry.get("out", []) or []),
        "env_requirements": env_reqs,
        "input_schema": input_schema,
        "internal_node_count": len(internal),
    }


def introspect_nr_subflow(nr, nr_subflow_id: str) -> dict:
    """生产路径：经 nr 客户端读 /flows 后自省（见 _introspect_nr_subflow_from_flows）。"""
    try:
        flows = nr.list_flows()
    except Exception as e:
        return {"ok": False, "error": f"读取 NR flows 失败: {type(e).__name__}: {e}"}
    return _introspect_nr_subflow_from_flows(flows, nr_subflow_id)
