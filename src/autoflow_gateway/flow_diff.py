# -*- coding: utf-8 -*-
"""
flow_diff — 两个 Node-RED flow 节点集的「拓扑 + 字段级」diff 工具。

设计意图（C3 / 验证闭环）：
- 把「已知良好」(golden / 真实 NR 导出) 与「编译器产物」做可复用比对，
  抓出 节点缺失 / 多余 / 字段值偏离 / 连线拓扑偏离 四类回归。
- 这是 `test_compile_patterns`(严格) 与 `test_compile_golden`(容忍多余) 之上抽出的
  **统一 diff 骨干**，Plan C 的 `golden_eval` 会直接复用本模块做「金标准 diff 闭环」。

匹配策略：
- 若 reference 与 candidate 的节点 id 集合完全一致 → 按 id 配对（此时可比对 wires 拓扑）。
- 否则 → 按 (type, 签名) 贪心配对（签名=name/action/entityId 之一），用于「编译器产物 id
  与 golden 不一致」的场景（此时跳过 wires 拓扑比对，仅比业务字段）。

用法：
    from autoflow_gateway.flow_diff import diff_flows, DiffResult
    res = diff_flows(golden_nodes, emitted_nodes, ignore={"x","y","z","id","wires"})
    if not res.ok:
        print(res.report())
"""
from typing import Any, Dict, List, Optional, Tuple

# 布局/标识类默认不参与比对
VOLATILE_DEFAULT = {"id", "x", "y", "z"}


def _node_sig(n: Dict[str, Any]) -> str:
    """同类型内区分签名：name > action > entityId > property。"""
    return (
        n.get("name")
        or n.get("action")
        or (",".join(n.get("entityId") or []))
        or n.get("property")
        or ""
    )


def _match(
    reference: List[Dict[str, Any]], candidate: List[Dict[str, Any]]
) -> List[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """把 reference 与 candidate 节点配对，返回 [(ref, cand), ...]。"""
    ref_ids = [n.get("id") for n in reference]
    can_ids = [n.get("id") for n in candidate]
    same_id_set = (
        set(ref_ids) == set(can_ids)
        and len(ref_ids) == len(can_ids)
        and None not in ref_ids
    )
    if same_id_set:
        can_by_id = {n["id"]: n for n in candidate}
        return [(rn, can_by_id.get(rn.get("id"))) for rn in reference]

    # 按 (type, 签名) 贪心配对
    pairs: List[Tuple[Optional[Dict], Optional[Dict]]] = []
    used = set()
    can_by_type: Dict[str, List[Tuple[int, Dict]]] = {}
    for i, cn in enumerate(candidate):
        can_by_type.setdefault(cn.get("type"), []).append((i, cn))
    for rn in reference:
        rt = rn.get("type")
        rsig = _node_sig(rn)
        cands = can_by_type.get(rt, [])
        match = None
        for idx, cn in cands:  # 先按签名精确匹配
            if idx in used:
                continue
            if _node_sig(cn) == rsig:
                match = (idx, cn)
                break
        if match is None:  # 退而求其次：同类型任意未用节点
            for idx, cn in cands:
                if idx in used:
                    continue
                match = (idx, cn)
                break
        if match is not None:
            used.add(match[0])
            pairs.append((rn, match[1]))
        else:
            pairs.append((rn, None))
    for i, cn in enumerate(candidate):  # 余下候选 = 多余节点
        if i not in used:
            pairs.append((None, cn))
    return pairs


class DiffResult:
    """结构化 diff 结果。"""

    def __init__(self):
        self.missing_nodes: List[Dict[str, Any]] = []   # reference 有、candidate 无
        self.extra_nodes: List[Dict[str, Any]] = []     # candidate 有、reference 无
        # 字段偏离：(节点标识, type, field, reference值, candidate值)
        self.field_issues: List[Tuple[str, str, str, Any, Any]] = []
        # 拓扑偏离：(node_id, reference_wires, candidate_wires)
        self.topology_issues: List[Tuple[str, Any, Any]] = []
        self.strict: bool = True

    @property
    def ok(self) -> bool:
        return not (
            self.missing_nodes
            or self.extra_nodes
            or self.field_issues
            or self.topology_issues
        )

    def report(self) -> str:
        lines = []
        if self.missing_nodes:
            lines.append(f"[缺失节点 {len(self.missing_nodes)}]")
            for n in self.missing_nodes:
                lines.append(f"   - {n.get('type')} ({_node_sig(n)})")
        if self.extra_nodes:
            lines.append(f"[多余节点 {len(self.extra_nodes)}]")
            for n in self.extra_nodes:
                lines.append(f"   + {n.get('type')} ({_node_sig(n)})")
        if self.field_issues:
            lines.append(f"[字段偏离 {len(self.field_issues)}]")
            for ident, ntype, fld, rv, cv in self.field_issues:
                lines.append(f"   ~ {ident} ({ntype}).{fld}: golden={rv!r} emitted={cv!r}")
        if self.topology_issues:
            lines.append(f"[拓扑偏离 {len(self.topology_issues)}]")
            for nid, rw, cw in self.topology_issues:
                lines.append(f"   # {nid}: golden={rw} emitted={cw}")
        if not lines:
            lines.append("✅ 完全一致（拓扑+字段）")
        mode = "严格" if self.strict else "容忍多余字段"
        return f"[flow_diff · {mode}]\n" + "\n".join(lines)


def diff_flows(
    reference: List[Dict[str, Any]],
    candidate: List[Dict[str, Any]],
    ignore: Optional[set] = None,
    strict: bool = True,
    topology: bool = True,
) -> DiffResult:
    """核心 diff。

    - reference：已知良好（golden / 真实导出）
    - candidate：编译器产物
    - ignore：不参与比对的字段（默认 VOLATILE_DEFAULT）
    - strict：True=候选不得有多余字段（全字段一致）；False=容忍候选多余字段
    - topology：是否比对 wires（仅当两端 id 一致时生效）
    """
    ignore = (ignore or set()) | VOLATILE_DEFAULT
    res = DiffResult()
    res.strict = strict
    pairs = _match(reference, candidate)
    for rn, cn in pairs:
        if rn is None:
            res.extra_nodes.append(cn)
            continue
        if cn is None:
            res.missing_nodes.append(rn)
            continue
        rk = {k: v for k, v in rn.items() if k not in ignore}
        ck = {k: v for k, v in cn.items() if k not in ignore}
        for k in rk:
            if k not in ck:
                res.field_issues.append(
                    (rn.get("id") or _node_sig(rn), rn.get("type"), k, rk[k], "<missing>")
                )
            elif ck[k] != rk[k]:
                res.field_issues.append(
                    (rn.get("id") or _node_sig(rn), rn.get("type"), k, rk[k], ck[k])
                )
        if strict:
            for k in ck:
                if k not in rk:
                    res.field_issues.append(
                        (cn.get("id") or _node_sig(cn), cn.get("type"), k, "<none>", ck[k])
                    )
        if (
            topology
            and rn.get("id")
            and cn.get("id")
            and rn.get("id") == cn.get("id")
        ):
            if rn.get("wires") != cn.get("wires"):
                res.topology_issues.append((rn["id"], rn.get("wires"), cn.get("wires")))
    return res


def diff_flow_dicts(
    reference_flow: Dict[str, Any],
    candidate_flow: Dict[str, Any],
    ignore: Optional[set] = None,
    strict: bool = True,
    topology: bool = True,
) -> DiffResult:
    """便捷封装：直接传入 {'nodes': [...]} 形态的 flow。"""
    return diff_flows(
        reference_flow.get("nodes", []),
        candidate_flow.get("nodes", []),
        ignore=ignore,
        strict=strict,
        topology=topology,
    )
