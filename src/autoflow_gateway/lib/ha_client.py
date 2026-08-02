#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ha_client.py — Home Assistant 只读安全客户端（纯标准库，零依赖）

设计原则（对照 node-red-Kai-Dai 的安全守卫思路）：
- 读操作（get_*/list_*/search_*）默认开放，幂等、不改变设备状态。
- 写操作（call_service）是「危险操作」：默认禁止，必须显式 allow_write=True
  且建议只针对明确安全的 domain（light/switch/script/scene 的 turn_on/off）。
- 连接信息优先级：环境变量 HASS_SERVER / HASS_TOKEN > 脚本内默认（本地 NAS）。
- 所有返回都是 dict / list[dict]，直接可 JSON 序列化，便于 agent 处理。

用法：
    from ha_client import HAClient
    ha = HAClient()                       # 读模式（call_service 会被拒绝）
    ha = HAClient(allow_write=True)       # 开启写
    states = ha.get_states()              # 全部
    light = ha.get_state("light.foo")     # 单实体
    ents  = ha.list_entities("sensor")    # 按 domain 列表
    hist  = ha.get_history("sensor.bar", hours=24)
    res   = ha.call_service("light", "turn_on", {"entity_id": "light.foo"})
"""
import json
import os
import asyncio
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

try:
    import websockets
    _HAVE_WS = True
except ImportError:  # 无 websocket 依赖时优雅降级（area/注册表不可用，但 REST 仍正常）
    _HAVE_WS = False

_WS_TIMEOUT = 25
_REG_CACHE_TTL = 300  # 注册表缓存 5 分钟（区域极少变动）

# ---- 连接信息：环境变量优先；缺省回退到本地 NAS（用户已授权本地使用） ----
DEFAULT_SERVER = os.environ.get("HASS_SERVER") or "http://<NAS_IP>:8123"
# 环境变量 HASS_TOKEN 优先于硬编码缺省（便于用户随时换新 token 而无需改源码）
DEFAULT_TOKEN = (
    os.environ.get("HASS_TOKEN")
    or "<HA_JWT_HEADER>"
    ".<HA_JWT_PAYLOAD>"
    ".<HA_JWT_SIG>"
)


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self, server=None, token=None, allow_write=False, timeout=15):
        self.server = (server or DEFAULT_SERVER).rstrip("/")
        self.token = token or DEFAULT_TOKEN
        self.allow_write = allow_write
        self.timeout = timeout
        self._hdr = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # ---------- 底层 ----------
    def _get(self, path):
        url = f"{self.server}/api/{path.lstrip('/')}"
        req = urllib.request.Request(url, headers=self._hdr, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise HAError(f"HTTP {e.code} @ {path}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except urllib.error.URLError as e:
            raise HAError(f"连接失败 @ {self.server}: {e.reason}")

    def _post(self, path, payload=None):
        if not self.allow_write:
            raise HAError("写操作被拒绝：HAClient 以只读模式初始化。请用 allow_write=True。")
        url = f"{self.server}/api/{path.lstrip('/')}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._hdr, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8")
                return json.loads(body) if body else {"ok": True}
        except urllib.error.HTTPError as e:
            raise HAError(f"HTTP {e.code} @ {path}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except urllib.error.URLError as e:
            raise HAError(f"连接失败 @ {self.server}: {e.reason}")

    # ---------- hass-cli 桥接（area / 实体注册表走 websocket，REST 不暴露） ----------
    def _hass_cli(self, args):
        """调用 hass-cli 返回解析后的 JSON。处理其偶发的多层数组/对象包装。"""
        env = dict(os.environ)
        env["HASS_SERVER"] = self.server
        env["HASS_TOKEN"] = self.token
        try:
            out = subprocess.run(
                ["hass-cli", "-o", "json"] + args,
                capture_output=True, text=True, timeout=self.timeout + 10, env=env,
            )
        except FileNotFoundError:
            raise HAError("hass-cli 未安装或不在 PATH。area/实体注册表查询需要它。")
        if out.returncode != 0:
            raise HAError(f"hass-cli 错误: {out.stderr.strip()[:200]}")
        raw = out.stdout.strip()
        if not raw:
            return []
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return []
        # 解开单层/多层列表包装：[[{...}]] -> [{...}]
        while isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], list):
            obj = obj[0]
        if isinstance(obj, dict):
            for k in ("result", "data", "items"):
                if k in obj and isinstance(obj[k], (list, dict)):
                    return obj[k]
        return obj

    def _unwrap_area(self, obj):
        """把 hass-cli area list 的结果规整成 {area_id: name}。"""
        m = {}
        items = obj if isinstance(obj, list) else []
        for a in items:
            if isinstance(a, dict) and a.get("area_id"):
                m[a["area_id"]] = a.get("name") or a["area_id"]
        return m

    # ---------- HA websocket 注册表（area/device 唯一可靠来源；REST 不暴露） ----------
    async def _ws_fetch_registries(self):
        """经 websocket 取 entity/device/area 三个注册表。返回 (ent, dev, area) 三个 list。"""
        if not _HAVE_WS:
            raise HAError("websockets 未安装，无法获取注册表（area/device）。请 pip install websockets。")
        ws_url = self.server.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        async with websockets.connect(ws_url, open_timeout=_WS_TIMEOUT, close_timeout=10,
                                      max_size=64 * 1024 * 1024) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            await ws.recv()  # auth_ok（失败会在后续 result 体现）
            await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
            await ws.send(json.dumps({"id": 2, "type": "config/device_registry/list"}))
            await ws.send(json.dumps({"id": 3, "type": "config/area_registry/list"}))
            res = {}
            for _ in range(3):
                m = json.loads(await ws.recv())
                if m.get("type") == "result" and m.get("id") in (1, 2, 3):
                    r = m.get("result")
                    res[m["id"]] = r if isinstance(r, list) else []
            return res.get(1, []), res.get(2, []), res.get(3, [])

    def _get_registries(self):
        """同步取三个注册表（带 5 分钟缓存）。失败返回 ([],[],[])，上层优雅降级。"""
        now = time.time()
        if getattr(self, "_reg_cache", None) and now - self._reg_cache[0] < _REG_CACHE_TTL:
            return self._reg_cache[1]
        if not _HAVE_WS:
            return [], [], []
        try:
            ent, dev, area = asyncio.run(self._ws_fetch_registries())
        except Exception as e:  # 任何网络/认证/解析错误都不应炸掉 refresh
            return [], [], []
        self._reg_cache = (now, (ent, dev, area))
        return ent, dev, area

    def invalidate_registries(self):
        """强制下次重新拉取注册表（refresh_catalog 入口调用）。"""
        self._reg_cache = None

    def _get_area_index(self):
        """返回 (area_map, entity_area, entity_device)。

        - area_map:     {area_id: name}
        - entity_area:  {entity_id: 解析后的区域名}
                       解析链：entity.area_id → 否则 entity.device_id → device.area_id
                       （HA 大量实体 area_id 为空，区域挂在 device 上，必须走 device 兜底）
        - entity_device:{entity_id: device_id}（供设备归组 B6 用）
        """
        ent, dev, area = self._get_registries()
        area_map = {a.get("area_id"): a.get("name") for a in area if a.get("area_id")}
        dev_area = {d.get("id"): area_map.get(d.get("area_id")) for d in dev if d.get("id")}
        entity_area, entity_device = {}, {}
        for e in ent:
            eid = e.get("entity_id")
            if not eid:
                continue
            entity_device[eid] = e.get("device_id")
            aname = None
            if e.get("area_id"):
                aname = area_map.get(e["area_id"])
            elif e.get("device_id") and dev_area.get(e["device_id"]):
                aname = dev_area[e["device_id"]]
            entity_area[eid] = aname or ""
        return area_map, entity_area, entity_device

    # ---------- 实例 / 配置 ----------
    def get_config(self):
        return self._get("config")

    def get_states(self, domain=None):
        """全部状态；domain 可选过滤（客户端过滤，因为 REST 的 filter_entity_id 在本版本被忽略）。"""
        states = self._get("states")
        if domain:
            states = [s for s in states if s["entity_id"].split(".", 1)[0] == domain]
        return states

    def get_state(self, entity_id):
        return self._get(f"states/{entity_id}")

    def list_entities(self, domain=None, area=None):
        """实体清单。domain 直接过滤；area 按解析后的区域名过滤（走 websocket 注册表）。"""
        if area is not None:
            _, entity_area, _ = self._get_area_index()
            states = {s["entity_id"]: s for s in self.get_states()}
            out = []
            for eid, aname in entity_area.items():
                if not aname:  # 区域未知（HA 本身未分配）跳过
                    continue
                if aname != area:  # entity_area 已解析为真实区域名
                    continue
                if domain and not eid.startswith(domain + "."):
                    continue
                s = states.get(eid, {})
                out.append({
                    "entity_id": eid,
                    "area_id": aname,
                    "friendly_name": (s.get("attributes") or {}).get("friendly_name"),
                    "state": s.get("state"),
                })
            return out
        states = self.get_states(domain)
        return [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "friendly_name": (s.get("attributes") or {}).get("friendly_name"),
            }
            for s in states
        ]

    def search_entities(self, keyword, domain=None):
        """按 friendly_name / entity_id 含关键词模糊搜。keyword 小写匹配。"""
        kw = keyword.lower()
        states = self.get_states(domain)
        out = []
        for s in states:
            fn = (s.get("attributes") or {}).get("friendly_name") or ""
            if kw in s["entity_id"].lower() or kw in fn.lower():
                out.append({"entity_id": s["entity_id"], "state": s["state"], "friendly_name": fn})
        return out

    def get_areas(self):
        """返回 {area_id: name}（走 HA websocket 注册表；REST /api/areas 在本版本 404）。"""
        area_map, _, _ = self._get_area_index()
        return area_map

    def get_areas_http(self):
        """兜底：直接 GET /api/areas（真实 HA 某些版本 404，但虚拟 HA/vhass 支持）。
        返回 {area_id: name}；失败返回 {}。"""
        try:
            raw = self._get("areas")
        except HAError:
            return {}
        if isinstance(raw, list):
            return self._unwrap_area(raw)
        if isinstance(raw, dict):
            # 形如 {"area_id": {"name": ...}} 或 {"area_1": "name"}
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[k] = v.get("name") or k
                else:
                    out[k] = v
            return out
        return {}

    def _resolve_area_id(self, area_name):
        area_map, _, _ = self._get_area_index()
        for aid, name in area_map.items():
            if name == area_name or aid == area_name:
                return aid
        return None

    def entity_areas(self, domain=None):
        """返回 {entity_id: area_name}，高效批量映射（供分组任务用）。

        区域解析链：entity.area_id → entity.device_id → device.area_id（HA 大量实体
        area_id 为空，区域挂在 device 上，必须走 device 兜底）。无 websocket 时返回 {}。
        """
        _, entity_area, _ = self._get_area_index()
        if domain:
            return {eid: aname for eid, aname in entity_area.items()
                    if eid.startswith(domain + ".")}
        return dict(entity_area)

    def entity_device_ids(self):
        """返回 {entity_id: device_id}，供设备归组（B6）与 room_summary 使用。"""
        _, _, entity_device = self._get_area_index()
        return dict(entity_device)

    def get_history(self, entity_id, hours=24):
        """取最近 hours 小时的状态变化历史。返回 [{s, lu, lc, a}] 列表。"""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
        # HA history API: /api/history_period/<start>?filter_entity_id=...&end_time=<end>
        path = (
            f"history_period/{start.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')}"
            f"?filter_entity_id={entity_id}&end_time={end.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')}"
        )
        try:
            raw = self._get(path)
        except HAError:
            return []
        # raw 是 [[ {s, lu, lc, a} ... ]]
        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            return raw[0]
        return raw if isinstance(raw, list) else []

    # ---------- 写操作（需 allow_write）----------
    def call_service(self, domain, service, service_data=None):
        return self._post(f"services/{domain}/{service}", service_data or {})

    # ---------- 便利聚合 ----------
    def domain_counts(self):
        from collections import Counter
        states = self.get_states()
        return dict(Counter(s["entity_id"].split(".", 1)[0] for s in states))

    def find_by_state(self, state_value, domain=None):
        """找处于某状态的所有实体（如 'unavailable' / 'off' / 'on'）。"""
        states = self.get_states(domain)
        return [s["entity_id"] for s in states if s["state"] == state_value]


if __name__ == "__main__":
    ha = HAClient()
    cfg = ha.get_config()
    print("HA:", cfg.get("location_name"), "v" + str(cfg.get("version")))
    print("states:", len(ha.get_states()))
    print("domains:", ha.domain_counts())
