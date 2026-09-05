#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync.py - AutoFlow 版本号驱动同步核心逻辑（dev -> prod）

被两处复用：
  - scripts/push_flow.py  (CLI)
  - webui.py              (WebUI /api/sync/* 后端)

固定方向：SRC(dev) -> DST(prod)。
标签写在 flow 的 info 字段：autoflow-stage: release|dev|agent / autoflow-version: x.y.z
缺省 stage=dev（安全：绝不自动推 prod）；agent=练手版绝不推。

护栏：写 DST 前自动快照（nr_client 写前快照）；子流程依赖预检；
      节点数熔断复用 nr_client；状态文件记录已推版本防重复推。
reseed(2026-07-12) 后两实例子流程 id 一致，无需 remap。
"""
import os, re, sys, json, datetime

# nr_client 在 lib/ 下
_LIB = os.path.join(os.path.dirname(__file__), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
from nr_client import NodeRedClient

SRC_URL = os.environ.get("NR_SRC_URL", "http://<NAS_IP>:1880")   # dev
DST_URL = os.environ.get("NR_DST_URL", "http://<NAS_IP>:1880")   # prod
STATE_FILE = os.path.join(os.path.expanduser("~"), ".workbuddy", "autoflow_sync_state.json")

STAGE_RE = re.compile(r"autoflow-stage:\s*([a-z]+)", re.I)
VER_RE   = re.compile(r"autoflow-version:\s*([0-9][\w.]*)", re.I)
VALID_STAGES = {"release", "dev", "agent"}


# ── 标签读写 ──────────────────────────────────────────
def parse_stage(info: str) -> str:
    m = STAGE_RE.search(info or "")
    return m.group(1).lower() if m else "dev"   # 缺省 = dev（安全）

def parse_version(info: str) -> str:
    m = VER_RE.search(info or "")
    return m.group(1) if m else "0.0.0"

def make_label_block(stage: str, version: str) -> str:
    return f"autoflow-stage: {stage}\nautoflow-version: {version}"

def set_label_in_info(info: str, stage: str, version: str) -> str:
    """移除旧 autoflow-* 行，追加新标签块，保留其余 info。"""
    lines = (info or "").split("\n")
    kept = [l for l in lines if not l.strip().startswith("autoflow-")]
    kept = [l for l in kept if l.strip() != ""]
    block = make_label_block(stage, version)
    return "\n".join(kept) + "\n\n" + block if kept else block


# ── 辅助 ──────────────────────────────────────────────
def resolve_id(nr: NodeRedClient, key: str) -> str:
    """key 可以是 id 前缀或 label 子串，返回唯一匹配 tab id。"""
    try:
        f = nr.get_flow(key)
        if f.get("id"):
            return f["id"]
    except Exception:
        pass
    flows = nr.list_flows()
    matches = []
    for e in flows:
        if e.get("type") != "tab":
            continue
        if e["id"].startswith(key) or (key.lower() in (e.get("label") or "").lower()):
            matches.append(e["id"])
    if not matches:
        raise SystemExit(f"找不到匹配 '{key}' 的 tab")
    if len(matches) > 1:
        raise SystemExit(f"匹配到多个 tab：{matches}，请改用完整 id")
    return matches[0]

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def dst_has_subflows(nr_dst: NodeRedClient, flow: dict):
    """检查 src flow 引用的 subflow 在 dst 是否都存在。返回 (ok, missing_list)。"""
    dst_flows = nr_dst.list_flows()
    dst_subs = {e["id"] for e in dst_flows if e.get("type") == "subflow"}
    missing = set()
    for n in flow.get("nodes", []):
        t = n.get("type", "")
        if t.startswith("subflow:"):
            sid = t.split(":", 1)[1]
            if sid not in dst_subs:
                missing.add(sid)
        if n.get("flow") and n.get("type") != "tab" and n.get("flow") not in dst_subs:
            missing.add(n.get("flow"))
    return (len(missing) == 0), sorted(missing)


# ── 核心操作（返回可序列化 dict，供 CLI/WebUI 共用）──────────
def scan_stages(src_url: str = SRC_URL) -> dict:
    nr = NodeRedClient(url=src_url)
    tabs = [e for e in nr.list_flows() if e.get("type") == "tab"]
    rows = []
    for t in tabs:
        info = nr.get_flow(t["id"]).get("info") or ""   # list_flows 不含 info，须 get_flow
        stage = parse_stage(info)
        ver = parse_version(info)
        rows.append({
            "id": t["id"],
            "label": t.get("label") or "",
            "stage": stage,
            "version": ver,
            "would_push": (stage == "release"),
        })
    rows.sort(key=lambda r: (r["stage"], r["label"]))
    return {"count": len(rows), "flows": rows}

def set_stage(fid: str, stage: str, version: str = None,
              src_url: str = SRC_URL) -> dict:
    if stage not in VALID_STAGES:
        return {"ok": False, "error": f"stage 必须是 {sorted(VALID_STAGES)}"}
    nr = NodeRedClient(url=src_url)
    fid = resolve_id(nr, fid)
    flow = nr.get_flow(fid)
    info = flow.get("info") or ""
    ver = version or parse_version(info) or "1.0.0"
    flow["info"] = set_label_in_info(info, stage, ver)
    nr.update_flow(fid, flow, force=True)
    return {"ok": True, "id": fid, "label": flow.get("label"),
            "stage": stage, "version": ver}

def push_one(fid: str, dry_run: bool = False,
             src_url: str = SRC_URL, dst_url: str = DST_URL) -> dict:
    nr_src = NodeRedClient(url=src_url)
    nr_dst = NodeRedClient(url=dst_url)
    fid = resolve_id(nr_src, fid)
    flow = nr_src.get_flow(fid)
    ok, missing = dst_has_subflows(nr_dst, flow)
    if not ok:
        return {"ok": False, "id": fid, "error": f"引用 1880 不存在的子流程 {missing}"}
    flow["disabled"] = False   # 强制 enabled，使其在 prod 运行
    if dry_run:
        return {"ok": True, "dry_run": True, "id": fid,
                "label": flow.get("label"), "action": "would push (enabled)"}
    dst_ids = {e["id"] for e in nr_dst.list_flows()}
    if fid in dst_ids:
        nr_dst.update_flow(fid, flow, force=True)
    else:
        nr_dst.create_flow(flow, force=True)
    return {"ok": True, "id": fid, "label": flow.get("label"), "action": "pushed (enabled)"}

def push_release(dry_run: bool = False,
                 src_url: str = SRC_URL, dst_url: str = DST_URL) -> dict:
    nr_src = NodeRedClient(url=src_url)
    nr_dst = NodeRedClient(url=dst_url)
    tabs = [e for e in nr_src.list_flows() if e.get("type") == "tab"]
    state = load_state()
    candidates = []
    for t in tabs:
        info = nr_src.get_flow(t["id"]).get("info") or ""
        if parse_stage(info) != "release":
            continue
        ver = parse_version(info)
        last = state.get(t["id"], {}).get("version", "0.0.0")
        if ver > last or t["id"] not in state:
            candidates.append((t["id"], t.get("label"), ver, last))
    if not candidates:
        return {"ok": True, "pushed": [], "message": "没有需要推送的 release flow"}
    pushed = []
    for fid, label, ver, last in candidates:
        flow = nr_src.get_flow(fid)
        ok, missing = dst_has_subflows(nr_dst, flow)
        if not ok:
            pushed.append({"ok": False, "id": fid, "label": label,
                           "error": f"引用 1880 不存在的子流程 {missing}"})
            continue
        flow["disabled"] = False
        if dry_run:
            pushed.append({"ok": True, "dry_run": True, "id": fid, "label": label,
                           "version": ver, "last": last, "action": "would push"})
            continue
        dst_ids = {e["id"] for e in nr_dst.list_flows()}
        if fid in dst_ids:
            nr_dst.update_flow(fid, flow, force=True)
        else:
            nr_dst.create_flow(flow, force=True)
        state[fid] = {"version": ver, "label": label,
                      "pushed_at": datetime.datetime.now().isoformat()}
        pushed.append({"ok": True, "id": fid, "label": label, "version": ver,
                       "action": "pushed (enabled)"})
    if not dry_run:
        save_state(state)
    return {"ok": True, "pushed": pushed,
            "message": f"推送 {len([p for p in pushed if p.get('ok')])} 个 flow"}
