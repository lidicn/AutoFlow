#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 脚本/JSON 兜底接口

无 MCP 客户端的 agent 用 shell 直接调：所有输入输出都是 JSON，便于机器处理。
例：
  python -m autoflow_gateway.cli discover --keyword 客厅
  python -m autoflow_gateway.cli commit --stdin < intent.json
  python -m autoflow_gateway.cli pending
  python -m autoflow_gateway.cli approve --id op_xxx
"""
import argparse
import json
import sys
from .gateway import Gateway


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_intent(args):
    if args.stdin:
        return json.load(sys.stdin)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return json.load(f)
    raise SystemExit("--file 或 --stdin 必须提供")


def main(argv=None):
    p = argparse.ArgumentParser(prog="autoflow", description="AutoFlow Gateway CLI (JSON 接口)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="发现设备（来自 device_catalog）")
    d.add_argument("--keyword")
    d.add_argument("--domain")
    d.add_argument("--area")
    d.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("search", help="语义桥搜索实体")
    s.add_argument("--keyword", required=True)
    s.add_argument("--domain")

    sub.add_parser("areas", help="列出 HA 区域与中文房间别名")
    sub.add_parser("detail", help="懒加载某实体完整属性").add_argument("--entity", required=True)
    sub.add_parser("catalog", help="输出共享态摘要（不 dump 全量实体）")

    pr = sub.add_parser("propose", help="预览场景（校验+构建，不落地）")
    pr.add_argument("--file")
    pr.add_argument("--stdin", action="store_true")

    cm = sub.add_parser("commit", help="提交场景（进确认闸）")
    cm.add_argument("--file")
    cm.add_argument("--stdin", action="store_true")

    pd = sub.add_parser("pending", help="列出待确认项")
    pd.add_argument("--agent")

    ap = sub.add_parser("approve", help="批准")
    ap.add_argument("--id", required=True)
    ap.add_argument("--reviewer", default="human")

    rj = sub.add_parser("reject", help="拒绝")
    rj.add_argument("--id", required=True)
    rj.add_argument("--reviewer", default="human")
    rj.add_argument("--reason")

    hs = sub.add_parser("ha-service", help="提交 HA 服务调用（进确认闸）")
    hs.add_argument("--domain", required=True)
    hs.add_argument("--service", required=True)
    hs.add_argument("--data-json", default="{}")
    hs.add_argument("--agent", default="unknown")

    rf = sub.add_parser("refresh", help="快照/增量同步 HA 实体进 catalog")
    rf.add_argument("--full", action="store_true", help="强制全量重建")
    rf.add_argument("--domain")
    rf.add_argument("--area")
    sub.add_parser("config", help="输出当前配置（不含凭证明文）")

    # ── P2 虚拟孪生 staging ──
    sv = sub.add_parser("seed-vhass", help="从真实 catalog 生成 vhass 种子 + 可选镜像 staging")
    sv.add_argument("--src", help="源 device_catalog.json（缺省 prod）")
    sv.add_argument("--seed-out", default="vhass_seed.json", help="vhass 种子输出路径")
    sv.add_argument("--mirror", action="store_true", help="同时把 catalog 镜像进 staging（含区域）")
    sv.add_argument("--staging-env", default="staging")

    vh = sub.add_parser("vhass", help="运行虚拟 HA (staging 数据源)")
    vh.add_argument("rest", nargs=argparse.REMAINDER, help="透传给 vhass（--port/--seed/--seed-from-catalog）")

    ma = sub.add_parser("mock-api", help="运行 mock Docker/业务 API (staging 非实体能力)")
    ma.add_argument("rest", nargs=argparse.REMAINDER, help="透传给 mock_docker_api（--port/--registry）")

    # ── 身份 / 提案 / 笔记 ──
    ac = sub.add_parser("agents-create", help="创建 agent 并生成身份识别码")
    ac.add_argument("--name", required=True)
    ac.add_argument("--tier", default="staging", choices=["staging", "prod", "sandbox"])
    ac.add_argument("--notes", default="")
    sub.add_parser("agents-list", help="列出所有 agent")
    arv = sub.add_parser("agents-revoke", help="吊销 agent")
    arv.add_argument("--id", required=True)
    arg = sub.add_parser("agents-regen", help="重置 agent 身份码")
    arg.add_argument("--id", required=True)

    pl = sub.add_parser("proposals-list", help="列出提案")
    pl.add_argument("--agent")
    pl.add_argument("--status")
    pp = sub.add_parser("proposals-promote", help="提案升格一级(raw→candidate→public)")
    pp.add_argument("--id", required=True)

    nl = sub.add_parser("notes-list", help="列出笔记")
    nl.add_argument("--tag")
    nl.add_argument("--q")
    na = sub.add_parser("notes-add", help="新增笔记")
    na.add_argument("--title", default="")
    na.add_argument("--body", default="")
    na.add_argument("--tags", default="")

    # ── 白箱运维旁路（复用 gateway/NR 契约，供人类调试/排障，不依赖 MCP agent）──
    gf = sub.add_parser("get-flow", help="只读：取回已部署 flow 的完整节点图 + 来源标记")
    gf.add_argument("--id", required=True, help="NR flow id（如 57be9a8f1fca2bcd）")
    lf = sub.add_parser("list-flows", help="列出 NR 已部署 flows/tabs（含 label/id/节点数）")
    lf.add_argument("--only", default="all", choices=["all", "deployed"],
                    help="deployed=仅列带部署标记的（剔除 stale）")
    inj = sub.add_parser("inject", help="触发 flow 内 inject 节点（调试用，非部署）")
    inj.add_argument("--flow-id", required=True)
    inj.add_argument("--node-id", default="", help="指定单个 inject 节点；缺省触发 flow 内全部 inject")
    inj.add_argument("--allow-prod", action="store_true",
                     help="目标 NR 被判定为 prod 时仍需显式开启（默认拒绝触发 prod）")
    dl = sub.add_parser("decisions-list", help="列出决策（verify/deploy 产物）")
    dl.add_argument("--status")
    dl.add_argument("--limit", type=int, default=50)

    args = p.parse_args(argv)
    gw = Gateway()

    # ── 身份 / 提案 / 笔记（无头管理）──
    if args.cmd == "agents-create":
        from .identity import AgentStore
        try:
            agent, code = AgentStore(gw.cfg).create_agent(args.name, args.tier, args.notes)
        except ValueError as e:
            _out({"ok": False, "error": str(e)}); return
        _out({"ok": True, "agent_id": agent.agent_id,
              "identity_code": code,  # 仅此刻可见
              "warn": "请立即复制身份码到 agent 的 MCP 配置"})
    elif args.cmd == "agents-list":
        from .identity import AgentStore
        _out({"agents": [a.to_dict() for a in AgentStore(gw.cfg).list_agents()]})
    elif args.cmd == "agents-revoke":
        from .identity import AgentStore
        _out({"ok": AgentStore(gw.cfg).revoke_agent(args.id)})
    elif args.cmd == "agents-regen":
        from .identity import AgentStore
        code = AgentStore(gw.cfg).regenerate_code(args.id)
        _out({"ok": code is not None, "identity_code": code} if code else {"ok": False})
    elif args.cmd == "proposals-list":
        from .proposals import ProposalStore
        _out({"proposals": [p.to_dict() for p in ProposalStore(gw.cfg).list(
            agent_id=args.agent or None, status=args.status or None,
            include_test=True)]})
    elif args.cmd == "proposals-promote":
        from .proposals import ProposalStore
        try:
            _out({"ok": True, "proposal": ProposalStore(gw.cfg).promote(args.id).to_dict()})
        except (KeyError, ValueError) as e:
            _out({"ok": False, "error": str(e)})
    elif args.cmd == "notes-list":
        from .notes import NoteStore
        _out({"notes": [n.to_dict() for n in NoteStore(gw.cfg).list(tag=args.tag or None, q=args.q or None)]})
    elif args.cmd == "notes-add":
        from .notes import NoteStore
        _out({"ok": True, "note": NoteStore(gw.cfg).create(
            args.title, args.body, args.tags.split(",") if args.tags else []).to_dict()})

    elif args.cmd == "discover":
        _out(gw.discover(args.keyword, args.domain, args.area, args.limit))
    elif args.cmd == "search":
        _out(gw.search_entities(args.keyword, args.domain))
    elif args.cmd == "areas":
        _out(gw.list_areas())
    elif args.cmd == "detail":
        _out(gw.get_detail(args.entity))
    elif args.cmd == "catalog":
        _out(gw.get_catalog())
    elif args.cmd == "propose":
        _out(gw.propose_scene(_load_intent(args)))
    elif args.cmd == "commit":
        _out(gw.commit_scene(_load_intent(args)))
    elif args.cmd == "pending":
        _out({"pending": gw.list_pending(args.agent)})
    elif args.cmd == "approve":
        _out(gw.approve(args.id, args.reviewer))
    elif args.cmd == "reject":
        _out(gw.reject(args.id, args.reviewer, args.reason))
    elif args.cmd == "ha-service":
        data = json.loads(args.data_json)
        _out(gw.commit_ha_service(args.domain, args.service, data, args.agent))
    elif args.cmd == "refresh":
        _out(gw.refresh_catalog(full=args.full, domain=args.domain, area=args.area))
    elif args.cmd == "config":
        cfg = gw.cfg
        _out({
            "env": cfg.env,
            "nr_url": cfg.nr_url,
            "hass_server": cfg.hass_server,
            "blast_radius_max_flows": cfg.blast_radius_max_flows,
            "protected_flow_labels": sorted(cfg.protected_flow_labels),
            "elevated_domains": sorted(cfg.elevated_domains),
            "safe_domains": sorted(cfg.safe_domains),
            "auto_approve_low_risk": cfg.auto_approve_low_risk,
            "mcp": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_path}",
        })
    elif args.cmd == "seed-vhass":
        if args.mirror:
            _out(gw.mirror_catalog_to_staging(args.src, args.staging_env, args.seed_out))
        else:
            _out(gw.build_vhass_seed(args.src, args.seed_out))
    elif args.cmd == "vhass":
        from . import vhass
        vhass.main(args.rest)
    elif args.cmd == "mock-api":
        from . import mock_docker_api
        mock_docker_api.main(args.rest)

    elif args.cmd == "get-flow":
        _out(gw.get_flow(args.id))
    elif args.cmd == "list-flows":
        try:
            flows = gw.nr.list_flows()
        except Exception as e:
            _out({"ok": False, "error": f"list_flows 失败: {e}"}); return
        items = flows if isinstance(flows, list) else flows.get("flows", [])
        if args.only == "deployed":
            items = [f for f in items if f.get("deployed")]
        _out({"ok": True, "count": len(items), "flows": [
            {"id": f.get("id"), "label": f.get("label"),
             "node_count": len(f.get("nodes", []))} for f in items]})
    elif args.cmd == "inject":
        if getattr(gw.nr, "is_prod", lambda: False)() and not args.allow_prod:
            _out({"ok": False, "error": "目标 NR 为 prod 实例，触发需显式 --allow-prod",
                  "hint": "inject 会真实触发自动化，请在非 prod 或知情下使用。"})
            return
        try:
            if args.node_id:
                rc = gw.nr.trigger_inject(args.node_id)
                _out({"ok": True, "node_id": args.node_id, "http_status": rc})
            else:
                gw.nr.inject_flow(args.flow_id)
                _out({"ok": True, "flow_id": args.flow_id, "triggered": "all_inject_nodes"})
        except Exception as e:
            _out({"ok": False, "error": f"inject 失败: {e}"})
    elif args.cmd == "decisions-list":
        _out({"ok": True, "decisions": gw.list_decisions(status=args.status, limit=args.limit)})


if __name__ == "__main__":
    main()
