#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow Gateway 单元测试（mock 后端，不触真实 HA/NR）。"""
import os
import sys
import json
import tempfile
import unittest

# 让测试能 import 包（CI/本地通用）
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import autoflow_gateway.config as cfgmod
from autoflow_gateway.config import set_feature_flag, is_raw_node_escape_enabled
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.proposals import ProposalStore
from autoflow_gateway.defense import DefenseLayer, DefenseError
from autoflow_gateway.schemas import SceneIntent, validate_intent
from autoflow_gateway.confirm import ConfirmationGate, ConfirmationError
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.nr_layer import NRLayer


# ── 假后端 ──
class FakeNR:
    def __init__(self):
        self.updates = []
        self.deletes = []
        self._server = "server_x"

    def update_flow(self, fid, flow, force=False):
        self.updates.append((fid, flow))
        return {"ok": True}

    def create_or_update_flow(self, fid, flow, force=False):
        # 测试用 FakeNR：get_flow 永远返回存在 → 走更新分支
        self.updates.append((fid, flow))
        return {"id": fid, "created": False, "raw": {"ok": True}}

    def list_flows(self):
        return []

    def get_flow(self, fid):
        return {"id": fid, "type": "tab", "nodes": []}

    def validate_flow(self, flow):
        return []

    def delete_flow(self, fid):
        self.deletes.append(fid)
        return {"ok": True}

    def dump_all_flows(self, d):
        return 0

    def build_server_state_changed(self, nid, fid, eid, **kw):
        return {"id": nid, "type": "server-state-changed", "z": fid,
                "entities": {"entity": [eid]}}

    def build_inject(self, nid, fid, **kw):
        return {"id": nid, "type": "inject", "z": fid}

    def _get_default_server(self):
        return self._server


class FakeHA:
    def __init__(self, area_broken=False):
        self.area_broken = area_broken
        self.states = [
            {"entity_id": "light.living_room", "state": "off",
             "attributes": {"friendly_name": "客厅主灯", "supported_features": 17},
             "last_changed": "2026-07-09T10:00:00+00:00", "last_updated": "2026-07-09T10:00:00+00:00"},
            {"entity_id": "light.entrance", "state": "off",
             "attributes": {"friendly_name": "玄关灯", "supported_features": 1},
             "last_changed": "2026-07-09T09:00:00+00:00", "last_updated": "2026-07-09T09:00:00+00:00"},
            {"entity_id": "switch.kitchen", "state": "on",
             "attributes": {"friendly_name": "厨房插座"},
             "last_changed": "2026-07-09T08:00:00+00:00", "last_updated": "2026-07-09T08:00:00+00:00"},
        ]
        self.areas = {"area_lr": "客厅", "area_en": "玄关", "area_kt": "厨房"}

    def get_states(self, domain=None):
        st = self.states
        if domain:
            st = [s for s in st if s["entity_id"].split(".", 1)[0] == domain]
        return st

    def get_areas(self):
        if self.area_broken:
            raise RuntimeError("hass-cli 未安装")
        return dict(self.areas)

    def entity_areas(self):
        if self.area_broken:
            raise RuntimeError("hass-cli 未安装")
        return {"light.living_room": "客厅", "light.entrance": "玄关", "switch.kitchen": "厨房"}

    def entity_device_ids(self):
        # 测试用假后端：实体未绑设备，返回空映射（device_id 字段留空即可）
        return {}

    def invalidate_registries(self):
        # 真实 HAClient 用 websocket 缓存，这里无需操作
        pass

    def get_state(self, entity_id):
        for s in self.states:
            if s["entity_id"] == entity_id:
                return s
        raise RuntimeError("not found")

    def call_service(self, d, s, data):
        return {"called": f"{d}.{s}", "data": data}


def make_gateway(env=None, area_broken=False):
    tmp = tempfile.mkdtemp(prefix="af_test_")
    os.environ["AUTOFLLOW_DATA_DIR"] = tmp
    if env:
        os.environ["AUTOFLLOW_ENV"] = env
    cfgmod.reset_config()
    cfg = cfgmod.get_config()
    # 用真实 Layer 包裹假后端，与生产路径一致（Layer 暴露 .build 等）
    return Gateway(
        config=cfg,
        ha_layer=HALayer(config=cfg, backend=FakeHA(area_broken=area_broken)),
        nr_layer=NRLayer(config=cfg, backend=FakeNR()),
    )


VALID_INTENT = {
    "name": "回家开灯",
    "description": "人回家时打开客厅和玄关灯",
    "agent_id": "agent_A",
    "trigger": [{"type": "state_changed", "entity_id": "device_tracker.me", "state": "home"}],
    "condition": [],
    "action": [
        {"domain": "light", "service": "turn_on", "entity_id": "light.living_room"},
        {"domain": "light", "service": "turn_on", "entity_id": "light.entrance"},
    ],
    "expected_postconditions": [
        {"entity_id": "light.living_room", "attribute": "state", "op": "equals", "value": "on"},
        {"entity_id": "light.entrance", "attribute": "state", "op": "equals", "value": "on"},
    ],
}


class TestDefense(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_config()
        self.d = DefenseLayer(cfgmod.get_config())

    def test_protected_flow_blocked(self):
        with self.assertRaises(DefenseError):
            self.d.check_write(operation="update_flow", flow_id="x", label="core_xxx")

    def test_ownership_blocks_other(self):
        with self.assertRaises(DefenseError):
            self.d.check_write(operation="update_flow", flow_id="f1", label="mine",
                               owner_agent="agent_B", acting_agent="agent_A")

    def test_blast_radius(self):
        with self.assertRaises(DefenseError):
            self.d.check_write(operation="update_flow", flow_id="f", label="x", flows_touched=5)

    def test_risk_classification(self):
        self.assertEqual(self.d.classify_domain_risk("lock"), "high")
        self.assertEqual(self.d.classify_domain_risk("light"), "low")
        self.assertEqual(self.d.classify_domain_risk("unknown_dom"), "medium")


class TestIntent(unittest.TestCase):
    def test_missing_postconditions(self):
        bad = dict(VALID_INTENT)
        bad["expected_postconditions"] = []
        errs = validate_intent(SceneIntent.from_dict(bad))
        self.assertTrue(any("expected_postconditions" in e for e in errs))

    def test_unknown_entity_in_catalog(self):
        cat = {"entities": {"light.living_room": {}}}
        bad = dict(VALID_INTENT)
        bad["action"][1]["entity_id"] = "light.nope"
        errs = validate_intent(SceneIntent.from_dict(bad), catalog=cat)
        self.assertTrue(any("light.nope" in e for e in errs))


class TestConfirm(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()

    def test_request_approve_reject(self):
        r = self.gw.commit_scene(VALID_INTENT)
        self.assertTrue(r["ok"])
        pid = r["pending_id"]
        # 未批准前不应有 NR 更新
        self.assertEqual(len(self.gw.nr._backend.updates), 0)
        ap = self.gw.approve(pid)
        self.assertTrue(ap["ok"])
        self.assertEqual(len(self.gw.nr._backend.updates), 1)
        # 重复批准应失败
        ap2 = self.gw.approve(pid)
        self.assertFalse(ap2["ok"])

    def test_reject_no_execution(self):
        r = self.gw.commit_scene(VALID_INTENT)
        self.gw.reject(r["pending_id"], reason="测试拒绝")
        self.assertEqual(len(self.gw.nr._backend.updates), 0)


class TestGatewayFlow(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()

    def test_propose_preview(self):
        r = self.gw.propose_scene(VALID_INTENT)
        self.assertTrue(r["ok"])
        self.assertIn("preview", r)
        self.assertTrue(any(n["type"] == "server-state-changed" for n in r["preview"]["nodes"]))
        self.assertTrue(any(n["type"] == "api-call-service" for n in r["preview"]["nodes"]))

    def test_commit_creates_pending_then_approve_updates_nr(self):
        r = self.gw.commit_scene(VALID_INTENT)
        self.assertTrue(r["ok"])
        self.assertTrue(r["needs_approval"])
        self.assertIn("risk", r)
        fid = r["preview"]["id"]
        ap = self.gw.approve(r["pending_id"])
        self.assertTrue(ap["ok"])
        self.assertEqual(ap["flow_id"], fid)
        # flow_catalog 已登记 owner
        meta = self.gw.state.get_flow_meta(fid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["owner_agent"], "agent_A")

    def test_high_risk_domain_still_pending(self):
        hi = dict(VALID_INTENT)
        hi["action"] = [{"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"}]
        hi["expected_postconditions"] = [{"entity_id": "lock.front_door", "attribute": "state", "op": "equals", "value": "unlocked"}]
        r = self.gw.commit_scene(hi)
        self.assertTrue(r["ok"])
        self.assertEqual(r["risk"], "high")
        self.assertTrue(r["needs_approval"])

    def test_discover_empty_catalog_hint(self):
        r = self.gw.discover(keyword="客厅")
        self.assertEqual(r["entities"], [])
        self.assertIn("hint", r)


class TestEnvIsolation(unittest.TestCase):
    def test_staging_vs_prod_nr_url(self):
        # 端口不再随 env 切换：staging / prod 都默认 1880（可用 NR_URL 覆盖）。
        # env 仅用于 data/ 子目录隔离。
        saved_nr_url = os.environ.pop("NR_URL", None)
        try:
            g1 = make_gateway("staging")
            self.assertEqual(g1.cfg.nr_url, "http://localhost:1880")
            g2 = make_gateway("prod")
            self.assertEqual(g2.cfg.nr_url, "http://localhost:1880")
        finally:
            if saved_nr_url is not None:
                os.environ["NR_URL"] = saved_nr_url


class TestDeviceLibrary(unittest.TestCase):
    def setUp(self):
        self.gw = make_gateway()

    def test_refresh_builds_index_and_mapping(self):
        r = self.gw.refresh_catalog()
        self.assertTrue(r["ok"])
        self.assertEqual(r["added"], 3)
        self.assertEqual(r["entity_total"], 3)
        cat = self.gw.state.get_device_catalog()
        self.assertEqual(cat["entities"]["light.living_room"]["area"], "客厅")
        # friendly_name → entity_id 自动播种
        self.assertEqual(self.gw.state.resolve("客厅主灯"), "light.living_room")
        # 区域索引 + 房间别名
        self.assertIn("客厅", self.gw.state.get_area_index().values())
        self.assertEqual(self.gw.state.get_room_aliases().get("客厅"), "客厅")
        self.assertEqual(self.gw.state.get_room_aliases().get("全屋"), "__all__")

    def test_get_catalog_is_summary_not_full_dump(self):
        self.gw.refresh_catalog()
        c = self.gw.get_catalog()
        self.assertIn("summary", c)
        self.assertNotIn("entities", c)  # 刻意不返全量，防 3000 实体爆炸
        self.assertEqual(c["summary"]["total_entities"], 3)

    def test_room_alias_discovery(self):
        self.gw.refresh_catalog()
        # 客厅 → 区域名 客厅，只返回客厅设备
        d = self.gw.discover(area="客厅")
        self.assertEqual(d["area_resolved"], "客厅")
        eids = [e["entity_id"] for e in d["entities"]]
        self.assertIn("light.living_room", eids)
        self.assertNotIn("switch.kitchen", eids)
        # 全屋 → 不过滤
        d2 = self.gw.discover(area="全屋")
        self.assertEqual(len(d2["entities"]), 3)

    def test_lazy_detail(self):
        self.gw.refresh_catalog()
        det = self.gw.get_detail("light.living_room")
        self.assertTrue(det["ok"])
        self.assertFalse(det["cached"])
        self.assertIn("attributes", det["detail"])
        # 第二次命中缓存
        det2 = self.gw.get_detail("light.living_room")
        self.assertTrue(det2["cached"])

    def test_incremental_diff_only_changed(self):
        self.gw.refresh_catalog()
        r2 = self.gw.refresh_catalog()
        self.assertEqual(r2["added"], 0)
        self.assertEqual(r2["changed"], 0)
        # 模拟 HA 变化
        self.gw.ha._backend.states[0]["state"] = "on"
        self.gw.ha._backend.states[0]["last_changed"] = "2026-07-09T11:00:00+00:00"
        r3 = self.gw.refresh_catalog()
        self.assertEqual(r3["changed"], 1)

    def test_area_unavailable_degrades(self):
        gw = make_gateway(area_broken=True)
        r = gw.refresh_catalog()
        self.assertTrue(r["ok"])
        self.assertFalse(r["area_available"])
        self.assertEqual(r["entity_total"], 3)  # 实体仍在，只是区域留空
        cat = gw.state.get_device_catalog()
        self.assertEqual(cat["entities"]["light.living_room"]["area"], "")



class TestListEntities(unittest.TestCase):
    """P1 · autoflow_list_entities 网关方法（读 device_catalog，离线 FakeHA 后端）。

    与 autoflow_resolve_entity（名称→候选）互补：本类验证「按域/区域/关键词过滤浏览目录」
    + 每个实体带 possible_states + 分页透明。
    """

    def setUp(self):
        self.gw = make_gateway()
        # refresh_catalog 走 FakeHA 后端，离线注入 3 实体（客厅主灯/玄关灯/厨房插座）
        self.gw.refresh_catalog()

    def test_empty_catalog_hint(self):
        gw = make_gateway()  # 全新空目录
        r = gw.list_entities(domain="light")
        self.assertEqual(r["entities"], [])
        self.assertIn("hint", r)
        self.assertEqual(r["matched_count"], 0)

    def test_domain_filter_and_possible_states(self):
        r = self.gw.list_entities(domain="light")
        self.assertEqual(r["matched_count"], 2)
        for e in r["entities"]:
            self.assertEqual(e["domain"], "light")
            self.assertEqual(set(e.keys()),
                             {"entity_id", "friendly_name", "domain", "area", "state", "possible_states"})
            self.assertEqual(e["possible_states"], ["on", "off"])

    def test_area_filter(self):
        r = self.gw.list_entities(area="客厅")
        eids = [e["entity_id"] for e in r["entities"]]
        self.assertEqual(r["area_resolved"], "客厅")
        self.assertIn("light.living_room", eids)
        self.assertNotIn("switch.kitchen", eids)

    def test_keyword_filter(self):
        r = self.gw.list_entities(keyword="厨房")
        self.assertEqual([e["entity_id"] for e in r["entities"]], ["switch.kitchen"])

    def test_intersection_filters(self):
        r = self.gw.list_entities(domain="light", area="客厅")
        self.assertEqual([e["entity_id"] for e in r["entities"]], ["light.living_room"])

    def test_pagination(self):
        r1 = self.gw.list_entities(domain="light", limit=1, offset=0)
        self.assertEqual(r1["returned"], 1)
        self.assertEqual(r1["matched_count"], 2)
        self.assertTrue(r1["truncated"])
        self.assertEqual(r1["next_offset"], 1)
        r2 = self.gw.list_entities(domain="light", limit=1, offset=1)
        self.assertEqual(r2["returned"], 1)
        self.assertFalse(r2["truncated"])
        self.assertIsNone(r2["next_offset"])

    def test_limit_clamped_to_200(self):
        # 超大 limit 被钳到 200（即使全集更小也不应报错）
        r = self.gw.list_entities(domain="light", limit=99999)
        self.assertEqual(r["returned"], 2)


class TestListAutomations(unittest.TestCase):
    """P3 · autoflow_list_automations 网关方法（离线：ProposalStore + flow_catalog）。

    注册表范围（用户确认：仅 flow 自动化）：已部署 flow_catalog + 待审 flow 提案
    （source=compiler/raw），排除网关改进类经验提案（source=unknown）。
    """

    def setUp(self):
        self.gw = make_gateway()
        self.store = ProposalStore(self.gw.cfg)
        # flow 提案（编译器路径）
        self.p_compiler = self.store.submit(
            "agt_a", "书房夜灯", "skill",
            json.dumps({"dsl": "场景: 书房夜灯\n触发: ..."}),
            source="compiler", spec="场景: 书房夜灯\n触发: 人体存在\n动作: light.turn_on")
        # flow 提案（原生手写路径）
        self.p_raw = self.store.submit(
            "agt_b", "客厅窗帘", "skill",
            json.dumps({"nodes": [{"type": "inject"}, {"type": "api-call-service"}]}),
            source="raw", spec="客厅窗帘｜2 nodes: inject×1, api-call-service×1")
        # 经验提案（应排除，source=unknown）
        self.p_idea = self.store.submit(
            "agt_a", "建议加结构化日志", "idea", "blah", source="unknown")
        # 遗留未标记 flow 提案（P0 前提交，source=unknown 但 kind=skill → 仍算 flow 自动化）
        self.p_legacy = self.store.submit(
            "agt_old", "旧版书房场景", "skill",
            json.dumps({"dsl": "场景: 旧版书房场景\n触发: ..."}),
            source="unknown", spec="场景: 旧版书房场景\n触发: 人体存在")
        # 已部署 flow（source_proposal 指向 compiler 提案 → 去重后只以 deployed 呈现）
        self.gw.state.upsert_flow("flow_xyz", {
            "flow_id": "flow_xyz", "label": "书房夜灯", "owner_agent": "agt_a",
            "purpose": "", "source_proposal": self.p_compiler.id,
            "deployed_at": "2026-07-25T00:00:00+00:00"})
        # 真实部署路径会调 mark_deployed 把提案标为已部署（list_automations 凭此去重）
        self.store.mark_deployed(self.p_compiler.id, "flow_xyz")

    def test_flow_only_scope_excludes_ideas(self):
        r = self.gw.list_automations()
        ids = [a["id"] for a in r["automations"]]
        self.assertIn(self.p_compiler.id, ids)   # 已部署（id=source_proposal）
        self.assertIn(self.p_raw.id, ids)         # 待审 raw
        self.assertIn(self.p_legacy.id, ids)       # 遗留未标记 flow 提案仍算
        self.assertNotIn(self.p_idea.id, ids)     # 经验提案排除

    def test_pending_excludes_deployed_proposal(self):
        # p_compiler 已部署 → 不应再出现在 pending 分支（去重）
        r = self.gw.list_automations(only="pending")
        ids = [a["id"] for a in r["automations"]]
        self.assertNotIn(self.p_compiler.id, ids)
        self.assertIn(self.p_raw.id, ids)

    def test_deployed_state_and_source(self):
        r = self.gw.list_automations(only="deployed")
        self.assertEqual(r["matched_count"], 1)
        a = r["automations"][0]
        self.assertEqual(a["state"], "deployed")
        self.assertEqual(a["source"], "compiler")
        self.assertEqual(a["flow_id"], "flow_xyz")

    # ── WB5#1b：注册表 ↔ NR 分叉对账（stale 标记）──
    def test_deployed_stale_when_nr_missing(self):
        # 默认 FakeNR.list_flows()→[]：注册表有 flow_xyz，但 NR 无此 flow → stale=True
        r = self.gw.list_automations(only="deployed")
        a = r["automations"][0]
        self.assertEqual(a["flow_id"], "flow_xyz")
        self.assertTrue(a["stale"], "NR 无此 flow 应标记 stale=True")

    def test_deployed_not_stale_when_nr_has_flow(self):
        # 注入 NR 确有 flow_xyz → stale=False
        self.gw.nr._backend.list_flows = lambda: [{"id": "flow_xyz", "label": "书房夜灯"}]
        r = self.gw.list_automations(only="deployed")
        a = r["automations"][0]
        self.assertEqual(a["flow_id"], "flow_xyz")
        self.assertFalse(a["stale"], "NR 有此 flow 不应标 stale")

    def test_stale_absent_when_nr_unreachable(self):
        # NR 不可达（抛异常）→ 不加 stale 字段，列表本身不阻断
        def _boom():
            raise RuntimeError("NR 连不上")
        self.gw.nr._backend.list_flows = _boom
        r = self.gw.list_automations(only="deployed")
        a = r["automations"][0]
        self.assertNotIn("stale", a, "NR 不可达时不应加 stale 字段")

    def test_keyword_filter(self):
        r = self.gw.list_automations(keyword="窗帘")
        ids = [a["id"] for a in r["automations"]]
        self.assertIn(self.p_raw.id, ids)
        self.assertNotIn(self.p_compiler.id, ids)

    def test_pagination(self):
        r = self.gw.list_automations(limit=1, offset=0)
        self.assertEqual(r["returned"], 1)
        self.assertEqual(r["matched_count"], 3)  # 已部署(compiler)+待审(raw)+遗留(skill)=3 条
        self.assertTrue(r["truncated"])
        self.assertEqual(r["next_offset"], 1)


class TestListDeployedStale(unittest.TestCase):
    """#552 · WebUI /api/deployed 路径：list_deployed(stale_check=True) 注册表↔NR 分叉对账。

    与 list_automations 的 stale 逻辑同源，但走独立方法（WebUI 不依赖 MCP 端点）。
    """

    def setUp(self):
        self.gw = make_gateway()
        self.gw.state.upsert_flow("flow_xyz", {
            "flow_id": "flow_xyz", "label": "书房夜灯", "owner_agent": "agt_a",
            "purpose": "夜灯", "deployed_at": "2026-07-25T00:00:00+00:00"})

    def test_deployed_stale_when_nr_missing(self):
        # 默认 FakeNR.list_flows()→[]：注册表有 flow_xyz，NR 无 → stale=True
        rows = self.gw.list_deployed(stale_check=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["stale"], "NR 无此 flow 应标记 stale=True")

    def test_deployed_not_stale_when_nr_has_flow(self):
        self.gw.nr._backend.list_flows = lambda: [{"id": "flow_xyz", "label": "书房夜灯"}]
        rows = self.gw.list_deployed(stale_check=True)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["stale"], "NR 有此 flow 不应标 stale")

    def test_stale_absent_when_nr_unreachable(self):
        def _boom():
            raise RuntimeError("NR 连不上")
        self.gw.nr._backend.list_flows = _boom
        rows = self.gw.list_deployed(stale_check=True)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("stale", rows[0], "NR 不可达时不应加 stale 字段")

    def test_default_no_stale_call(self):
        # 不带 stale_check → 不触发 NR 对账，无 stale 字段（避免 list_automations 双调用）
        rows = self.gw.list_deployed()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("stale", rows[0])


class TestRawNodeEscapeGate(unittest.TestCase):
    """Phase 4 原生节点逃逸开关（默认关闭，WebUI 可随时开启/关闭）。"""
    RAW_DSL = ('场景: 复合条件\n触发: inject\n'
               '原生节点: {"type":"switch","name":"复合AND/OR","outputs":2,'
               '"property":"payload.cond","rules":[{"t":"eq","v":"1"}]}\n'
               '动作: light.turn_on(客厅主灯)')

    def test_disabled_by_default_rejects_raw_node(self):
        gw = make_gateway()
        # 默认关闭：含 原生节点: 的 DSL 应在编译前被拒（feature_disabled）
        res = gw.verify_task_dsl(self.RAW_DSL)
        self.assertFalse(res["ok"])
        self.assertEqual(res["result_kind"], "compile_error")
        self.assertIn("已关闭", res["error"])

    def test_enabled_accepts_raw_node(self):
        gw = make_gateway()
        cfg = cfgmod.get_config()
        self.assertFalse(is_raw_node_escape_enabled(cfg), "默认应为关闭")
        set_feature_flag(cfg, "raw_node_escape_enabled", True)
        self.assertTrue(is_raw_node_escape_enabled(cfg), "开启后应可读到 True")
        res = gw.verify_task_dsl(self.RAW_DSL)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertGreater(res["node_count"], 0, "编译应包含原生节点")

    def test_toggle_off_rejects_again(self):
        gw = make_gateway()
        cfg = cfgmod.get_config()
        set_feature_flag(cfg, "raw_node_escape_enabled", True)
        self.assertTrue(gw.verify_task_dsl(self.RAW_DSL)["ok"])
        set_feature_flag(cfg, "raw_node_escape_enabled", False)
        res = gw.verify_task_dsl(self.RAW_DSL)
        self.assertFalse(res["ok"])
        self.assertIn("已关闭", res["error"])


class TestBranchRequiredGate(unittest.TestCase):
    """Phase 0 加固（#272）：任务声明需条件分支（requires_branch=True）时，
    DSL 未含『分支』节点判 lint_error 并拦截，避免动作被无条件执行。"""

    BRANCHLESS = ("场景: 有人经过才开灯\n"
                  "触发: binary_sensor.motion on\n"
                  "动作: light.turn_on(light.foo)")

    WITH_BRANCH = ("场景: 主卧高温开空调\n"
                   "触发: sensor.zhu_wo_shi_temperature changed\n"
                   "取值: sensor.zhu_wo_shi_temperature temp\n"
                   "分支: $number(temp) > 28\n"
                   "  动作: climate.set_hvac_mode(climate.xiaomi_cn_533439795_mt0, hvac_mode=cool)")

    def test_branchless_rejected(self):
        gw = make_gateway()
        res = gw.verify_task_dsl(self.BRANCHLESS, requires_branch=True)
        self.assertEqual(res["result_kind"], "lint_error")
        self.assertTrue(
            any(s["rule"] == "R_branch_required" for s in res["lint_summary"]),
            "缺分支 + requires_branch 应命中 R_branch_required",
        )

    def test_with_branch_passes(self):
        gw = make_gateway()
        res = gw.verify_task_dsl(self.WITH_BRANCH, requires_branch=True)
        self.assertFalse(
            any(s["rule"] == "R_branch_required" for s in res["lint_summary"]),
            "含分支不应再触发 R_branch_required",
        )

    def test_requires_branch_false_no_gate(self):
        gw = make_gateway()
        # 不要求分支时，缺分支也不应触发该闸门（对无条件任务零影响）
        res = gw.verify_task_dsl(self.BRANCHLESS, requires_branch=False)
        self.assertFalse(
            any(s["rule"] == "R_branch_required" for s in res["lint_summary"]),
            "requires_branch=False 不应触发 R_branch_required",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
