#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vhass — AutoFlow 虚拟 Home Assistant（数字孪生 / staging 数据源）

为什么不是真 HA 容器：
- 只实现网关 ha_client 实际使用的 REST 子集（states / services / trigger / areas / config），
  零第三方依赖、纯标准库，NAS 无 Python 也能跑（容器化后自洽）。
- 状态完全可控：可注入合成事件（POST /api/trigger）、可断言，正好是 P3 验证闭环要的。
- entity_id 与真实 HA 同构 → 在 staging 验证通过的 flow，promotion 到 prod 逻辑零改动。

服务变更语义（与真实 HA 对齐的常见 domain）：
- light/switch/fan/input_boolean: turn_on→on, turn_off→off
- cover: open_cover→open, close_cover→closed, stop_cover→stopped
- lock: lock→locked, unlock→unlocked
- climate: turn_on/off 改 state；set_hvac_mode 改 state（HA 里 climate 的 state 就是
  hvac_mode）；set_temperature/set_fan_mode/... 改对应 attributes
- fan/cover/media_player/humidifier: set_percentage / set_cover_position / volume_set 等
  改对应 attributes，state 不变（见 _ATTR_FROM_DATA）
- scene: turn_on 为 no-op（记忆被触发）
- 未建模 service：**state 保持不变**（绝不写成 service 名），只在
  attributes.last_service / _unmodeled_service 留痕并登记 store.unmodeled_calls，
  由 staging 闸门如实降级为「后置条件未验证」（A14）

合成触发（POST /api/trigger）模拟"现实世界发生了某事"：
- 例：{"entity_id":"device_tracker.me","state":"home"} 模拟人回家
- 这会驱动 staging 里 Agent 构建的 flow（若 NR 指向 vhass 且事件流接通，见 README）。

运行：
    python -m autoflow_gateway.vhass --port 8124 --seed vhass_seed.json
    python -m autoflow_gateway.vhass --seed-from-catalog ../data/prod/state/device_catalog.json --port 8124
"""
import json
import os
import sys
import time
import threading
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_PORT = int(os.environ.get("VHASS_PORT", "8124"))
DEFAULT_SEED = os.environ.get("VHASS_SEED", "vhass_seed.json")
DEFAULT_STATE = os.environ.get("VHASS_STATE", "vhass_state.json")

# ── 按 domain 推导初始状态（当真实 catalog 无 state 时使用）──
_DOMAIN_DEFAULT_STATE = {
    "light": "off", "switch": "off", "fan": "off", "input_boolean": "off",
    "cover": "closed", "lock": "locked", "climate": "auto", "scene": "unknown",
    "sensor": "0", "binary_sensor": "off", "device_tracker": "not_home",
    "automation": "on", "script": "unknown", "number": "0", "select": "unknown",
    "media_player": "off", "vacuum": "docked", "alarm_control_panel": "disarmed",
    "valve": "closed", "water_heater": "off", "input_number": "0", "input_select": "unknown",
}

# ── 服务 → 新状态 映射 ──
_ON_OFF = {"turn_on": "on", "turn_off": "off", "enable": "on", "disable": "off"}
_COVER = {"open_cover": "open", "close_cover": "closed", "stop_cover": "stopped",
          "open": "open", "close": "closed"}
_LOCK = {"lock": "locked", "unlock": "unlocked"}

# ── A14：非 toggle 服务的真实副作用建模 ──────────────────────────────────
# 为什么需要：旧实现对「不在上面三张表里的服务」一律 `state = service 名`，
# 于是 climate.set_hvac_mode 会把 state 写成字面量 "set_hvac_mode"。
# staging 闸门随后按 state 回读断言，必然与真实 HA 语义（climate 的 state
# 就是 hvac_mode）不符 → 误判 FAIL；反之若期望恰好写了服务名则误判 PASS。
# 下面三张表按 HA 文档把服务映射到「它真正写哪个 state / 哪个 attribute」。

# (domain, service) → 固定终态
_FIXED_STATE = {
    ("media_player", "media_play"): "playing",
    ("media_player", "media_pause"): "paused",
    ("media_player", "media_stop"): "idle",
    ("media_player", "play_media"): "playing",
    ("vacuum", "start"): "cleaning",
    ("vacuum", "pause"): "paused",
    ("vacuum", "stop"): "idle",
    ("vacuum", "return_to_base"): "returning",
    ("alarm_control_panel", "alarm_arm_home"): "armed_home",
    ("alarm_control_panel", "alarm_arm_away"): "armed_away",
    ("alarm_control_panel", "alarm_arm_night"): "armed_night",
    ("alarm_control_panel", "alarm_disarm"): "disarmed",
    ("valve", "open_valve"): "open",
    ("valve", "close_valve"): "closed",
    ("humidifier", "turn_on"): "on",
}

# (domain, service) → state 取自 data 的哪个键（HA 中这类实体 state 即该值）
_STATE_FROM_DATA = {
    ("climate", "set_hvac_mode"): "hvac_mode",
    ("water_heater", "set_operation_mode"): "operation_mode",
    ("input_select", "select_option"): "option",
    ("select", "select_option"): "option",
    ("input_number", "set_value"): "value",
    ("number", "set_value"): "value",
    ("input_text", "set_value"): "value",
    ("text", "set_value"): "value",
    ("input_datetime", "set_datetime"): "datetime",
}

# (domain, service) → {data 键: attribute 名}；state 保持不变
_ATTR_FROM_DATA = {
    ("climate", "set_temperature"): {"temperature": "temperature",
                                     "target_temp_high": "target_temp_high",
                                     "target_temp_low": "target_temp_low"},
    ("climate", "set_fan_mode"): {"fan_mode": "fan_mode"},
    ("climate", "set_preset_mode"): {"preset_mode": "preset_mode"},
    ("climate", "set_swing_mode"): {"swing_mode": "swing_mode"},
    ("climate", "set_humidity"): {"humidity": "humidity"},
    ("fan", "set_percentage"): {"percentage": "percentage"},
    ("fan", "set_preset_mode"): {"preset_mode": "preset_mode"},
    ("fan", "set_direction"): {"direction": "current_direction"},
    ("fan", "oscillate"): {"oscillating": "oscillating"},
    ("cover", "set_cover_position"): {"position": "current_position"},
    ("cover", "set_cover_tilt_position"): {"tilt_position": "current_tilt_position"},
    ("media_player", "volume_set"): {"volume_level": "volume_level"},
    ("media_player", "select_source"): {"source": "source"},
    ("media_player", "select_sound_mode"): {"sound_mode": "sound_mode"},
    ("humidifier", "set_humidity"): {"humidity": "humidity"},
    ("humidifier", "set_mode"): {"mode": "mode"},
    ("light", "turn_on"): {"brightness": "brightness", "brightness_pct": "brightness_pct",
                           "color_temp": "color_temp", "rgb_color": "rgb_color"},
    ("water_heater", "set_temperature"): {"temperature": "temperature"},
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(epoch):
    """虚拟时钟 epoch(秒) → ISO8601(UTC)。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _norm_iso(value):
    """归一化 ISO 字符串（兼容 HA 的 'Z' 结尾）为可解析形式。"""
    if isinstance(value, str) and value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


def _entity_id_list(data):
    eid = data.get("entity_id")
    if eid is None:
        return []
    if isinstance(eid, str):
        if eid in ("all", "group.all"):
            return ["__ALL__"]
        return [eid]
    if isinstance(eid, list):
        return eid
    return [str(eid)]


class VHassStore:
    """内存态 + JSON 持久化。线程安全。"""

    def __init__(self, seed_path=None, state_path=None):
        self._lock = threading.Lock()
        self.areas = {}            # area_id -> name
        self.entities = {}         # entity_id -> state dict (HA shape)
        self.seed_path = seed_path
        self.state_path = state_path
        if seed_path and os.path.exists(seed_path):
            self.load_seed(seed_path)
        elif state_path and os.path.exists(state_path):
            self.load_state(state_path)
        else:
            self._demo()
        self._clock_lock = threading.Lock()
        self._vclock_epoch = time.time()  # 虚拟时钟（秒，epoch）
        # A14：本 store 生命周期内被调用过、但 vhass 未建模真实副作用的服务。
        # staging 闸门据此把「后置状态未被验证」如实降级，而不是假装验证过。
        self.unmodeled_calls = []

    # ── 载入 ──
    def load_seed(self, path):
        with open(path, "r", encoding="utf-8") as f:
            seed = json.load(f)
        self.areas = seed.get("areas", {})
        for e in seed.get("entities", []):
            self.entities[e["entity_id"]] = self._normalize(e)

    def load_state(self, path):
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.areas = st.get("areas", {})
        self.entities = st.get("entities", {})

    def _demo(self):
        demo = build_seed_from_entities([
            ("light.living_room_main", "客厅主灯", "客厅", "off", {"supported_features": 147, "brightness": 0}),
            ("light.entrance", "玄关灯", "玄关", "off", {}),
            ("switch.kitchen_power", "厨房电源", "厨房", "off", {}),
            ("cover.living_room_curtain", "客厅窗帘", "客厅", "closed", {}),
            ("climate.living_room_ac", "客厅空调", "客厅", "off", {"temperature": 26.0}),
            ("device_tracker.me", "我", "全屋", "not_home", {}),
            ("media_player.living_room_tv", "客厅电视", "客厅", "off", {}),
            ("lock.front_door", "大门锁", "玄关", "locked", {}),
        ])
        self.areas = demo["areas"]
        for e in demo["entities"]:
            self.entities[e["entity_id"]] = self._normalize(e)

    @staticmethod
    def _normalize(e):
        eid = e["entity_id"]
        domain = eid.split(".", 1)[0]
        attrs = dict(e.get("attributes", {}))
        if "friendly_name" not in attrs:
            attrs["friendly_name"] = e.get("friendly_name") or eid
        rec = {
            "entity_id": eid,
            "state": e.get("state", _DOMAIN_DEFAULT_STATE.get(domain, "unknown")),
            "attributes": attrs,
            "last_changed": e.get("last_changed", now_iso()),
            "last_updated": e.get("last_updated", now_iso()),
            "context": e.get("context", {"id": "", "parent_id": None, "user_id": None}),
        }
        if e.get("area"):
            rec["area"] = e["area"]
        return rec

    # ── 持久化 ──
    def persist(self):
        if not self.state_path:
            return
        st = {"areas": self.areas, "entities": self.entities}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    # ── 查询 ──
    def list_states(self, domain=None):
        out = []
        for e in self.entities.values():
            if domain and e["entity_id"].split(".", 1)[0] != domain:
                continue
            out.append(e)
        return out

    def get_state(self, entity_id):
        return self.entities.get(entity_id)

    # ── 变更 ──
    def apply_service(self, domain, service, data):
        """返回被变更的实体列表（HA 风格 [{'entity_id','state'}]）。"""
        targets = _entity_id_list(data)
        if targets == ["__ALL__"]:
            targets = [eid for eid in self.entities if eid.split(".", 1)[0] == domain]
        changed = []
        with self._lock:
            for eid in targets:
                e = self.entities.get(eid)
                if e is None or eid.split(".", 1)[0] != domain:
                    continue
                self._mutate(e, domain, service, data)
                changed.append({"entity_id": eid, "state": e["state"]})
            if changed:
                self.persist()
        return changed

    def _mutate(self, e, domain, service, data):
        """把一次 service 调用的副作用写进实体。返回 True=该服务已建模。

        A14：未建模的服务【绝不】把 service 名伪造成 state——那会让 staging 闸门
        按一个不存在的状态做断言，制造误判 FAIL / 误判 PASS。未建模时保持 state
        原样、只留痕，并登记到 store.unmodeled_calls 供闸门降级为「未验证」。
        """
        t = now_iso()
        e["last_changed"] = t
        e["last_updated"] = t
        attrs = e.setdefault("attributes", {})
        key = (domain, service)
        modeled = True

        # 1) 属性副作用（可与终态叠加，如 light.turn_on + brightness）
        attr_map = _ATTR_FROM_DATA.get(key)
        if attr_map:
            for dkey, aname in attr_map.items():
                if dkey in data:
                    attrs[aname] = data[dkey]

        # 2) 终态
        if service in _ON_OFF:
            e["state"] = _ON_OFF[service]
        elif service in _COVER:
            e["state"] = _COVER[service]
        elif service in _LOCK:
            e["state"] = _LOCK[service]
        elif key in _FIXED_STATE:
            e["state"] = _FIXED_STATE[key]
        elif key in _STATE_FROM_DATA:
            dkey = _STATE_FROM_DATA[key]
            if dkey in data and data[dkey] is not None:
                e["state"] = str(data[dkey])
                attrs[dkey] = data[dkey]
            else:
                # 服务已建模但调用缺关键参数 → 无法推导终态，同样不许瞎写
                modeled = False
        elif domain == "light" and service == "toggle":
            e["state"] = "off" if e["state"] == "on" else "on"
        elif domain == "cover" and service == "toggle":
            e["state"] = "closed" if e["state"] == "open" else "open"
        elif service == "toggle":
            e["state"] = "off" if e["state"] == "on" else "on"
        elif attr_map:
            pass  # 纯属性服务（如 fan.set_percentage）：state 不变，属已建模
        else:
            modeled = False

        attrs["last_service"] = f"{domain}.{service}"
        if modeled:
            attrs.pop("_unmodeled_service", None)
        else:
            attrs["_unmodeled_service"] = f"{domain}.{service}"
            call = f"{domain}.{service}({e['entity_id']})"
            calls = getattr(self, "unmodeled_calls", None)
            if calls is None:
                calls = self.unmodeled_calls = []
            if call not in calls:
                calls.append(call)
        return modeled

    def inject_trigger(self, entity_id, state, attributes=None):
        """模拟现实事件（合成触发）。返回更新后的实体。"""
        with self._lock:
            e = self.entities.get(entity_id)
            if e is None:
                # 允许 trigger 动态创建实体（如 device_tracker）
                e = self._normalize({"entity_id": entity_id, "state": state,
                                      "attributes": attributes or {}})
                self.entities[entity_id] = e
            else:
                e["state"] = state
                if attributes:
                    e["attributes"].update(attributes)
                e["last_changed"] = now_iso()
                e["last_updated"] = now_iso()
            self.persist()
            return e

    # ── 虚拟时钟（C4：时间快进 / 时间段场景）──
    def _set_clock_nolock(self, value):
        if isinstance(value, (int, float)):
            self._vclock_epoch = float(value)
        else:
            self._vclock_epoch = datetime.fromisoformat(_norm_iso(value)).timestamp()

    def clock_now(self):
        with self._clock_lock:
            return self._vclock_epoch

    def clock_now_iso(self):
        return _iso_from_epoch(self.clock_now())

    def set_clock(self, value):
        """value: epoch 秒(数字) 或 ISO 字符串。"""
        with self._clock_lock:
            self._set_clock_nolock(value)
        return self.clock_now_iso()

    def advance_clock(self, seconds):
        with self._clock_lock:
            self._vclock_epoch += float(seconds)
        return self.clock_now_iso()

    # ── 分支感知重放（C4：多步 / 状态类场景）──
    def apply_replay(self, steps):
        """按虚拟时间序重放世界事件。

        steps: [{at, entity_id, state, attributes?}, ...]
          - at: 相对重放起点秒偏移(数字) 或 绝对 ISO/epoch；缺省=当前虚拟时刻
          - entity_id/state/attributes: 同 inject_trigger（模拟现实事件）
        返回 {timeline, final_states, clock_iso}。
        关键：事件按序注入并推进虚拟时钟 → 下游（time-range-switch / 多步场景）
        可据虚拟时刻+事件序判定分支，这正是「分支感知重放」的底座。
        """
        start = self.clock_now()
        plan = []
        with self._clock_lock:
            for step in steps:
                at = step.get("at")
                if at is None:
                    vt = self._vclock_epoch
                elif isinstance(at, (int, float)):
                    vt = start + float(at)
                    self._vclock_epoch = vt
                else:
                    self._set_clock_nolock(at)
                    vt = self._vclock_epoch
                plan.append((vt, step.get("entity_id"), step.get("state"),
                            step.get("attributes")))
        # clock 锁外执行状态变更（inject_trigger 取 _lock）
        for vt, eid, st, attrs in plan:
            self.inject_trigger(eid, st, attrs)
        return {
            "timeline": [
                {"at_epoch": vt, "at_iso": _iso_from_epoch(vt),
                 "entity_id": eid, "state": st, "attributes": attrs}
                for vt, eid, st, attrs in plan
            ],
            "final_states": {eid: e["state"] for eid, e in self.entities.items()},
            "clock_iso": self.clock_now_iso(),
        }


# ── 种子生成：从真实 device_catalog 镜像 ──
def build_seed_from_entities(rows):
    """rows: [(entity_id, friendly_name, area, state, attributes_dict), ...]"""
    areas = {}
    entities = []
    for eid, fn, area, state, attrs in rows:
        domain = eid.split(".", 1)[0]
        if area and area not in areas.values():
            areas[f"area_{len(areas)}"] = area
        entities.append({
            "entity_id": eid,
            "friendly_name": fn,
            "area": area,
            "state": state or _DOMAIN_DEFAULT_STATE.get(domain, "unknown"),
            "attributes": attrs or {},
            "last_changed": now_iso(),
            "last_updated": now_iso(),
        })
    return {"version": 1, "areas": areas, "entities": entities}


def build_seed_from_catalog(catalog_path, out_path=None, limit=None):
    """从真实 device_catalog.json 生成 vhass 种子（同构 entity_id / 区域）。"""
    with open(catalog_path, "r", encoding="utf-8") as f:
        cat = json.load(f)
    ents = cat.get("entities", {})
    if isinstance(ents, dict):
        ents = list(ents.values())
    areas = {}
    rows = []
    for e in ents:
        if limit and len(rows) >= limit:
            break
        eid = e.get("entity_id")
        if not eid:
            continue
        domain = eid.split(".", 1)[0]
        area = e.get("area") or ""
        if area and area not in areas.values():
            areas[f"area_{len(areas)}"] = area
        caps = e.get("capabilities") or []
        if isinstance(caps, str):
            caps = [caps]
        attrs = {"friendly_name": e.get("friendly_name") or eid}
        if "brightness" in caps or "dimable" in caps:
            attrs["supported_features"] = attrs.get("supported_features", 0) | 1
        if "rgb_color" in caps or "color" in caps:
            attrs["supported_features"] = attrs.get("supported_features", 0) | 16
        state = e.get("state") or _DOMAIN_DEFAULT_STATE.get(domain, "unknown")
        rows.append((eid, e.get("friendly_name") or eid, area, state, attrs))
    seed = build_seed_from_entities(rows)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(seed, f, ensure_ascii=False, indent=2)
    return seed


# ── HTTP 服务 ──
class Handler(BaseHTTPRequestHandler):
    store = None  # 类属性，由 main 注入

    def log_message(self, *args):
        pass  # 静默

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path.rstrip("/") or "/"
        st = self.store
        if path in ("/", "/api/", "/health", "/api/health"):
            return self._send(200, {"name": "vhass", "state": "running", "entities": len(st.entities)})
        if path == "/api/config":
            return self._send(200, {"location_name": "AutoFlow Staging", "version": "vhass-1.0"})
        if path == "/api/areas":
            return self._send(200, st.areas)
        if path == "/api/clock":
            return self._send(200, {"epoch": st.clock_now(), "iso": st.clock_now_iso()})
        if path == "/api/states":
            return self._send(200, st.list_states())
        if path.startswith("/api/states/"):
            eid = path[len("/api/states/"):]
            e = st.get_state(eid)
            if e is None:
                return self._send(404, {"error": "entity not found", "entity_id": eid})
            return self._send(200, e)
        return self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path.rstrip("/")
        st = self.store
        body = self._read_body()
        # POST /api/services/{domain}/{service}
        if path.startswith("/api/services/"):
            parts = path[len("/api/services/"):].split("/")
            if len(parts) != 2:
                return self._send(400, {"error": "usage: /api/services/<domain>/<service>"})
            domain, service = parts
            changed = st.apply_service(domain, service, body)
            return self._send(200, changed)
        # POST /api/trigger 合成事件注入
        if path in ("/api/trigger", "/api/events"):
            eid = body.get("entity_id")
            if not eid:
                return self._send(400, {"error": "entity_id required"})
            e = st.inject_trigger(eid, body.get("state"), body.get("attributes"))
            return self._send(200, e)
        # POST /api/clock 虚拟时钟控制
        if path == "/api/clock":
            action = body.get("action", "set")
            val = body.get("value", 0)
            if action == "advance":
                iso = st.advance_clock(val)
            else:
                iso = st.set_clock(val)
            return self._send(200, {"epoch": st.clock_now(), "iso": iso})
        # POST /api/replay 分支感知重放（多步场景）
        if path == "/api/replay":
            if "steps" not in body or not isinstance(body["steps"], list):
                return self._send(400, {"error": "steps[] required"})
            result = st.apply_replay(body["steps"])
            return self._send(200, result)
        return self._send(404, {"error": "not found", "path": path})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vhass", description="AutoFlow 虚拟 HA (staging)")
    ap.add_argument("--host", default=os.environ.get("VHASS_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--seed", default=DEFAULT_SEED, help="种子 JSON（entity_id 镜像）")
    ap.add_argument("--state", default=DEFAULT_STATE, help="运行态持久化 JSON")
    ap.add_argument("--seed-from-catalog", help="从真实 device_catalog.json 生成种子并写入 --seed")
    ap.add_argument("--seed-limit", type=int, default=None, help="仅镜像前 N 个实体")
    args = ap.parse_args(argv)

    if args.seed_from_catalog:
        build_seed_from_catalog(args.seed_from_catalog, args.seed, args.seed_limit)
        print(f"[vhass] 已从 {args.seed_from_catalog} 生成种子 → {args.seed}")

    store = VHassStore(seed_path=args.seed, state_path=args.state)
    Handler.store = store
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[vhass] 虚拟 HA 已启动：http://{args.host}:{args.port}  "
          f"({len(store.entities)} 实体, {len(store.areas)} 区域)")
    print(f"[vhass] 合成触发: POST /api/trigger {{entity_id,state}}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[vhass] 停止。")


if __name__ == "__main__":
    main()
