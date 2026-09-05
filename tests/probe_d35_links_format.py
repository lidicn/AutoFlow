# -*- coding: utf-8 -*-
"""D35 根因实证探针：对象数组 links 在真实 NR 是否建立 link 连接。

在 NAS prod 容器内运行。复现 round24 三种矩阵：
  1) links=["li1"]              字符串数组 (NR 标准)
  2) links=[{"id":"li1"}]       对象数组 (D30 修复格式)
  3) links=[{"id":"li1","type":"link in"}] 对象数组+type

直接调 gateway._remap_raw_flow_ids 看 remap 后形态，
再调 run_e2e_trace_raw 看真实 NR 行为（reached / trace / verdict）。
"""
import sys, json, traceback
sys.path.insert(0, "/app/src")

from autoflow_gateway import gateway as G
from autoflow_gateway.config import reset_config


def make_gw():
    reset_config()
    gw = G.Gateway()
    return gw


def link_flow(links_value, links_in=None, lo_mode=None):
    lo = {"id": "lo1", "type": "link out", "z": "f", "wires": [], "links": links_value}
    if lo_mode:
        lo["mode"] = lo_mode
    li = {"id": "li1", "type": "link in", "z": "f", "wires": [["ch1"]]}
    if links_in is not None:
        li["links"] = links_in
    return {"nodes": [
        {"id": "i1", "type": "inject", "z": "f", "wires": [["lo1"]],
         "props": [{"p": "payload"}], "repeat": "", "crontab": "",
         "once": False, "onceDelay": 0.1, "topic": "", "payload": "hello",
         "payloadType": "str"},
        lo,
        li,
        {"id": "ch1", "type": "change", "z": "f",
         "rules": [{"t": "set", "p": "payload", "pt": "msg", "to": "processed", "tot": "str"}],
         "wires": [["d1"]]},
        {"id": "d1", "type": "debug", "z": "f", "wires": []},
    ]}


def show_remap(gw, label, flow):
    remapped, id_map, hpz = gw._remap_raw_flow_ids(flow, "FFFFFFFFFFFFFFFF")
    los = [n for n in remapped["nodes"] if n["type"] == "link out"]
    lis = [n for n in remapped["nodes"] if n["type"] == "link in"]
    print(f"\n=== REMAP [{label}] ===")
    print("  link out links:", json.dumps(los[0]["links"]) if los else "NONE")
    print("  link in  links:", json.dumps(lis[0].get("links")) if lis else "NONE")


def run_case(gw, label, links_value, links_in=None, lo_mode=None):
    print(f"\n########## CASE [{label}] ##########")
    flow = link_flow(links_value, links_in, lo_mode)
    show_remap(gw, label, flow)
    try:
        res = gw.run_e2e_trace_raw(
            flow_json=flow,
            trigger="inject",
            allow_prod=True,
            live=False,
        )
    except Exception as e:
        print("  !! run_e2e_trace_raw EXCEPTION:", repr(e))
        traceback.print_exc()
        return
    rep = res.get("report", {})
    print("  verdict:", res.get("verdict"))
    print("  reached_count:", rep.get("reached_count"), "expected_count:", rep.get("expected_count"))
    print("  reached:", rep.get("reached"))
    print("  missing:", rep.get("missing"))
    print("  failed_at:", rep.get("failed_at"))
    rt = res.get("trace") or []
    print("  raw trace nodes:", [t.get("node") for t in rt])
    print("  runtime_errors:", rep.get("runtime_errors"))


def main():
    gw = make_gw()
    # 1) 字符串数组
    run_case(gw, "str-array", ["li1"], links_in=["lo1"])
    # 2) 对象数组 {"id":...}
    run_case(gw, "obj-array", [{"id": "li1"}], links_in=[{"id": "lo1"}])
    # 3) 对象数组 + type
    run_case(gw, "obj-array-type", [{"id": "li1", "type": "link in"}],
             links_in=[{"id": "lo1", "type": "link out"}])


if __name__ == "__main__":
    main()
