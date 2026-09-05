import json

SERVER = "e93e1ad9c034e866"
LIGHT = "light.yeelink_cn_88345456_bslamp1_s_2_light"

def inj(fid, nid, wires, payload="go"):
    return {"id": nid, "type": "inject", "z": fid, "name": "\u89e6\u53d1",
            "props": [{"p": "payload"}], "repeat": "", "crontab": "",
            "once": False, "onceDelay": 0.1, "topic": "", "payload": payload,
            "payloadType": "str", "x": 100, "y": 100, "wires": wires}

def linkout(fid, nid, links, x=250):
    return {"id": nid, "type": "link out", "z": fid, "name": "", "links": links, "x": x, "y": 100, "wires": []}

def linkin(fid, nid, links, wires, x=320):
    return {"id": nid, "type": "link in", "z": fid, "name": "", "links": links, "x": x, "y": 100, "wires": wires}

def switch(fid, nid, wires, x=420):
    return {"id": nid, "type": "switch", "z": fid, "name": "\u8def\u7531",
            "property": "payload", "propertyType": "msg",
            "rules": [{"t": "eq", "v": "go", "vt": "str"}], "checkall": "true",
            "repair": False, "outputs": 1, "x": x, "y": 100, "wires": wires}

def api(fid, nid, x=560):
    return {"id": nid, "type": "api-call-service", "z": fid, "name": "\u5f00\u706f",
            "server": SERVER, "version": 7, "debugenabled": False,
            "action": "light.turn_on", "floorId": [], "areaId": [], "deviceId": [],
            "entityId": [], "labelId": [],
            "data": json.dumps({"entity_id": LIGHT}), "dataType": "json",
            "mergeContext": "", "mustacheAltTags": False, "outputProperties": [],
            "blockInputOverrides": False, "domain": "light", "service": "turn_on",
            "x": x, "y": 100, "wires": [[]]}

def delay(fid, nid, wires, x=440):
    return {"id": nid, "type": "delay", "z": fid, "name": "\u5ef6\u65f61s",
            "pauseType": "delay", "timeout": "1", "timeoutUnits": "seconds",
            "rate": "1", "nbRateUnits": "1", "rateUnits": "second",
            "randomFirst": "1", "randomLast": "5", "randomUnits": "seconds",
            "drop": False, "allowrate": False, "outputs": 1, "x": x, "y": 100, "wires": wires}

def dbg(fid, nid, x=580):
    return {"id": nid, "type": "debug", "z": fid, "name": "",
            "active": True, "tosidebar": True, "console": False, "tostatus": False,
            "complete": "false", "targetType": "undefined", "statusVal": "",
            "statusType": "auto", "x": x, "y": 100, "wires": []}

def subflow(fid, nid, wires, x=440):
    return {"id": nid, "type": "subflow:sf_test_append_v2", "z": fid, "name": "",
            "x": x, "y": 100, "wires": wires}

v1 = {"id": "af_r21_d31_v1", "label": "R21 D31 v1 link->switch->api", "nodes": [
    inj("af_r21_d31_v1", "inj1", [["lo1"]]),
    linkout("af_r21_d31_v1", "lo1", ["li1"]),
    linkin("af_r21_d31_v1", "li1", ["lo1"], [["sw1"]]),
    switch("af_r21_d31_v1", "sw1", [["api1"]]),
    api("af_r21_d31_v1", "api1"),
]}
v2 = {"id": "af_r21_d31_v2", "label": "R21 D31 v2 link->subflow->debug", "nodes": [
    inj("af_r21_d31_v2", "inj2", [["lo2"]]),
    linkout("af_r21_d31_v2", "lo2", ["li2"]),
    linkin("af_r21_d31_v2", "li2", ["lo2"], [["sf2"]]),
    subflow("af_r21_d31_v2", "sf2", [["dbg2"]]),
    dbg("af_r21_d31_v2", "dbg2"),
]}
v3 = {"id": "af_r21_d31_v3", "label": "R21 D31 v3 link->delay->debug", "nodes": [
    inj("af_r21_d31_v3", "inj3", [["lo3"]]),
    linkout("af_r21_d31_v3", "lo3", ["li3"]),
    linkin("af_r21_d31_v3", "li3", ["lo3"], [["dly3"]]),
    delay("af_r21_d31_v3", "dly3", [["dbg3"]]),
    dbg("af_r21_d31_v3", "dbg3"),
]}
v4 = {"id": "af_r21_d31_v4", "label": "R21 D31 v4 nested link->api", "nodes": [
    inj("af_r21_d31_v4", "inj4", [["lo4a"]]),
    linkout("af_r21_d31_v4", "lo4a", ["li4a"]),
    linkin("af_r21_d31_v4", "li4a", ["lo4a"], [["lo4b"]]),
    linkout("af_r21_d31_v4", "lo4b", ["li4b"], x=420),
    linkin("af_r21_d31_v4", "li4b", ["lo4b"], [["api4"]], x=500),
    api("af_r21_d31_v4", "api4", x=620),
]}
ctrl = {"id": "af_r21_d31_ctrl", "label": "R21 D31 ctrl switch->api (no link)", "nodes": [
    inj("af_r21_d31_ctrl", "injc", [["swc"]]),
    switch("af_r21_d31_ctrl", "swc", [["apic"]], x=250),
    api("af_r21_d31_ctrl", "apic", x=400),
]}

def d33(n):
    fid = f"af_r21_d33_{n}"
    apis = [f"api{i}" for i in range(n)]
    nodes = [inj(fid, "inj", [apis])]
    for a in apis:
        nodes.append(api(fid, a))
    return {"id": fid, "label": f"R21 D33 {n} parallel api", "nodes": nodes}

d33_10 = d33(10)
d33_5 = d33(5)

# 诊断：嵌套 link -> debug（排除是我的接线问题还是 2 级 link 下游普遍断）
v4b = {"id": "af_r21_d31_v4b", "label": "R21 D31 v4b nested link->debug", "nodes": [
    inj("af_r21_d31_v4b", "inj4", [["lo4a"]]),
    linkout("af_r21_d31_v4b", "lo4a", ["li4a"]),
    linkin("af_r21_d31_v4b", "li4a", ["lo4a"], [["lo4b"]]),
    linkout("af_r21_d31_v4b", "lo4b", ["li4b"], x=420),
    linkin("af_r21_d31_v4b", "li4b", ["lo4b"], [["dbg4"]], x=500),
    dbg("af_r21_d31_v4b", "dbg4"),
]}
# 诊断：嵌套 link -> switch -> api（与 v1 同结构但放在 2 级 link 之后）
v4c = {"id": "af_r21_d31_v4c", "label": "R21 D31 v4c nested link->switch->api", "nodes": [
    inj("af_r21_d31_v4c", "inj4", [["lo4a"]]),
    linkout("af_r21_d31_v4c", "lo4a", ["li4a"]),
    linkin("af_r21_d31_v4c", "li4a", ["lo4a"], [["lo4b"]]),
    linkout("af_r21_d31_v4c", "lo4b", ["li4b"], x=420),
    linkin("af_r21_d31_v4c", "li4b", ["lo4b"], [["sw4"]]),
    switch("af_r21_d31_v4c", "sw4", [["api4"]], x=580),
    api("af_r21_d31_v4c", "api4", x=720),
]}

# 控制：单级 link -> api 直连（v5）——定位 D31 是「link-in->api 直连」还是「嵌套专属」
v5 = {"id": "af_r21_d31_v5", "label": "R21 D31 v5 single link->api direct", "nodes": [
    inj("af_r21_d31_v5", "inj5", [["lo5"]]),
    linkout("af_r21_d31_v5", "lo5", ["li5"]),
    linkin("af_r21_d31_v5", "li5", ["lo5"], [["api5"]]),
    api("af_r21_d31_v5", "api5"),
]}

# 控制：inject -> 10 并行 function（可插桩非 sink）——定位 D33 是并 fan-out 还是 api 专属
def fn_parallel(n):
    fid = f"af_r21_d33_fn{n}"
    fns = [f"fn{i}" for i in range(n)]
    nodes = [inj(fid, "inj", [fns])]
    for fn in fns:
        nodes.append({"id": fn, "type": "function", "z": fid, "name": "透传",
                      "func": "return msg;", "outputs": 1, "noerr": 0,
                      "initialize": "", "finalize": "", "libs": [],
                      "x": 400, "y": 100, "wires": [[]]})
    return {"id": fid, "label": f"R21 D33 {n} parallel function", "nodes": nodes}

d33_fn10 = fn_parallel(10)
d33_fn5 = fn_parallel(5)

# 控制：单级 link -> api -> debug（api 非终点，带下游 sink，对齐 round22 金标准结构）
v5b = {"id": "af_r21_d31_v5b", "label": "R21 D31 v5b single link->api->debug", "nodes": [
    inj("af_r21_d31_v5b", "inj5b", [["lo5b"]]),
    linkout("af_r21_d31_v5b", "lo5b", ["li5b"]),
    linkin("af_r21_d31_v5b", "li5b", ["lo5b"], [["api5b"]]),
    api("af_r21_d31_v5b", "api5b", x=460),
    dbg("af_r21_d31_v5b", "dbg5b", x=620),
]}
# 控制：inject -> api 直连（无 link，api 为终点）——排除「是否 api 作终点节点在 e2e 不被追踪」
v5c = {"id": "af_r21_d31_v5c", "label": "R21 D31 v5c inject->api direct", "nodes": [
    inj("af_r21_d31_v5c", "inj5c", [["api5c"]]),
    api("af_r21_d31_v5c", "api5c"),
]}

flows = {"d31_v1": v1, "d31_v2": v2, "d31_v3": v3, "d31_v4": v4, "d31_v4b": v4b,
         "d31_v4c": v4c, "d31_v5": v5, "d31_v5b": v5b, "d31_v5c": v5c, "d31_ctrl": ctrl,
         "d33_10": d33_10, "d33_5": d33_5, "d33_fn10": d33_fn10, "d33_fn5": d33_fn5}

if __name__ == "__main__":
    for k, f in flows.items():
        s = json.dumps(f, ensure_ascii=False, separators=(",", ":"))
        print(f"@@{k}@@" + json.dumps(s))
