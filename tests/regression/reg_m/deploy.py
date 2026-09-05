# -*- coding: utf-8 -*-
"""把 build_flow.py 生成的 REG-M tab 增量部署到 Node-RED（默认 1990 测试实例）。

只动目标 tab 一个（``PUT /flow/:id``），其余 tab 一律不碰 —— 这是 flows 数「只增不损」
的硬保证。目标 tab id 来源：命令行 > 台账文件 > 按 label 认领（认领后回写台账）。

用法::

    NR_URL=http://host:1990 NR_USER=xx NR_PASS=yy \\
        python tests/regression/reg_m/deploy.py [tab_id]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nr_admin import NRAdmin, NRAdminError  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
FLOW_JSON = REPO / "tests" / "fixtures_local" / "reg_m_flow.json"
LEDGER = REPO / "tests" / "fixtures_local" / "reg_m_tab.id"
LABEL = "REG-M 能力矩阵 (M1-M5, 28断言)"


def resolve_tab(api: NRAdmin, argv: list[str]) -> str:
    """确定目标 tab id，必要时按 label 认领并回写台账。

    Args:
        api: 已登录的 admin 客户端。
        argv: 命令行参数列表。

    Returns:
        目标 tab id。

    Raises:
        NRAdminError: 远端不存在同名 tab 且未显式指定 id。
    """
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    if LEDGER.exists() and LEDGER.read_text(encoding="utf-8").strip():
        tid = LEDGER.read_text(encoding="utf-8").strip()
        try:
            api.get_flow(tid)
            return tid
        except NRAdminError:
            print("[warn] 台账 tab %s 在远端不存在，改按 label 认领" % tid)
    tid = api.find_tab_by_label(LABEL)
    if not tid:
        raise NRAdminError("远端无 label=%r 的 tab，请先手工建 tab 或传 id" % LABEL)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(tid, encoding="utf-8")
    print("[ledger] 认领并登记 tab id = %s" % tid)
    return tid


def main() -> None:
    """执行部署：读 JSON → 保留 tab 元信息 → 整体替换该 tab 的 nodes。"""
    flow = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    api = NRAdmin()
    api.login()

    before = api.get_flows()
    n_tabs_before = sum(1 for n in before if n.get("type") == "tab")
    print("[before] 总节点=%d  tab数=%d" % (len(before), n_tabs_before))

    tab_id = resolve_tab(api, sys.argv)
    old = api.get_flow(tab_id)
    print("[target] tab=%s label=%r 原节点数=%d"
          % (tab_id, old.get("label"), len(old.get("nodes") or [])))

    for node in flow["nodes"]:
        node["z"] = tab_id
    payload = {
        "id": tab_id,
        "label": flow["label"],
        "disabled": bool(old.get("disabled", False)),
        "info": old.get("info", ""),
        "configs": old.get("configs") or [],
        "nodes": flow["nodes"],
    }
    api.update_flow(tab_id, payload)
    print("[deploy] PUT /flow/%s  新节点数=%d" % (tab_id, len(flow["nodes"])))

    after = api.get_flows()
    n_tabs_after = sum(1 for n in after if n.get("type") == "tab")
    print("[after ] 总节点=%d  tab数=%d" % (len(after), n_tabs_after))
    if n_tabs_after < n_tabs_before:
        raise SystemExit("[!!!] tab 数减少（%d → %d），疑似误删，请立即检查"
                         % (n_tabs_before, n_tabs_after))
    live = {n["id"] for n in after if n.get("z") == tab_id}
    missing = [n["id"] for n in flow["nodes"] if n["id"] not in live]
    if missing:
        raise SystemExit("[!!!] 未落盘节点 %d 个: %s" % (len(missing), missing[:10]))
    print("[OK] 部署已落盘校验通过（%d 节点全部在线）" % len(flow["nodes"]))


if __name__ == "__main__":
    main()
