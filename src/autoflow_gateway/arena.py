"""
AutoFlow 竞技场核心模块 (v2.0.0-alpha)

自由作文优先的竞技场：
- Agent 自己命题（题目锁定机制）
- vhass 虚拟设备验收
- 创造力评分
- 分区隔离（书房/客厅/主卧室）

MVP 阶段：验收用 propose-dsl 的 vhass staging 闸门，不真实部署到 NR。
"""

import json
import os
import time
import uuid
import difflib
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── 初始分区定义 ──────────────────────────────────────────────

DEFAULT_ARENAS = [
    {
        "id": "study_room",
        "name": "书房竞技场",
        "description": "书房场景：电脑、显示器、台灯、空调、窗帘。适合自动化办公和学习场景。",
        "phase2_threshold": 20,
        "creativity_threshold": 0.3,
        "devices": [
            {"entity_id": "switch.computer", "friendly_name": "电脑", "state": "off", "domain": "switch"},
            {"entity_id": "light.desk_lamp", "friendly_name": "台灯", "state": "off", "domain": "light", "attributes": {"brightness": 0}},
            {"entity_id": "light.monitor_lamp", "friendly_name": "显示器挂灯", "state": "off", "domain": "light", "attributes": {"brightness": 0}},
            {"entity_id": "climate.study_ac", "friendly_name": "书房空调", "state": "off", "domain": "climate", "attributes": {"temperature": 26.0}},
            {"entity_id": "cover.study_curtain", "friendly_name": "书房窗帘", "state": "closed", "domain": "cover"},
            {"entity_id": "sensor.study_temperature", "friendly_name": "书房温度", "state": "24.5", "domain": "sensor"},
            {"entity_id": "binary_sensor.study_motion", "friendly_name": "书房人体感应", "state": "off", "domain": "binary_sensor"},
            {"entity_id": "input_boolean.focus_mode", "friendly_name": "专注模式", "state": "off", "domain": "input_boolean"},
        ],
    },
    {
        "id": "living_room",
        "name": "客厅竞技场",
        "description": "客厅场景：电视、音响、灯光、窗帘、空调。适合影音娱乐和会客场景。",
        "phase2_threshold": 20,
        "creativity_threshold": 0.3,
        "devices": [
            {"entity_id": "media_player.tv", "friendly_name": "客厅电视", "state": "off", "domain": "media_player"},
            {"entity_id": "media_player.soundbar", "friendly_name": "音响", "state": "off", "domain": "media_player"},
            {"entity_id": "light.living_main", "friendly_name": "客厅主灯", "state": "off", "domain": "light", "attributes": {"brightness": 0}},
            {"entity_id": "light.living_ambient", "friendly_name": "氛围灯", "state": "off", "domain": "light", "attributes": {"brightness": 0}},
            {"entity_id": "cover.living_curtain", "friendly_name": "客厅窗帘", "state": "open", "domain": "cover"},
            {"entity_id": "climate.living_ac", "friendly_name": "客厅空调", "state": "off", "domain": "climate", "attributes": {"temperature": 26.0}},
            {"entity_id": "binary_sensor.living_motion", "friendly_name": "客厅人体感应", "state": "off", "domain": "binary_sensor"},
            {"entity_id": "input_boolean.movie_mode", "friendly_name": "观影模式", "state": "off", "domain": "input_boolean"},
        ],
    },
    {
        "id": "master_bedroom",
        "name": "主卧室竞技场",
        "description": "主卧室场景：床灯、空调、窗帘、闹钟。适合睡眠和起床场景。",
        "phase2_threshold": 20,
        "creativity_threshold": 0.3,
        "devices": [
            {"entity_id": "light.bedside_left", "friendly_name": "床头灯左", "state": "off", "domain": "light", "attributes": {"brightness": 0}},
            {"entity_id": "light.bedside_right", "friendly_name": "床头灯右", "state": "off", "domain": "light", "attributes": {"brightness": 0}},
            {"entity_id": "climate.bedroom_ac", "friendly_name": "卧室空调", "state": "off", "domain": "climate", "attributes": {"temperature": 26.0}},
            {"entity_id": "cover.bedroom_curtain", "friendly_name": "卧室窗帘", "state": "closed", "domain": "cover"},
            {"entity_id": "input_datetime.alarm", "friendly_name": "闹钟", "state": "07:00:00", "domain": "input_datetime"},
            {"entity_id": "binary_sensor.bedroom_motion", "friendly_name": "卧室人体感应", "state": "off", "domain": "binary_sensor"},
            {"entity_id": "sensor.bedroom_temperature", "friendly_name": "卧室温度", "state": "25.0", "domain": "sensor"},
            {"entity_id": "input_boolean.sleep_mode", "friendly_name": "睡眠模式", "state": "off", "domain": "input_boolean"},
        ],
    },
]


# ── 题目审核：实体重叠度 ──────────────────────────────────────

def _entity_overlap(new_entities: List[str], existing_entities: List[str]) -> float:
    """计算新题目与已有题目的实体重叠度。

    ★ B21 修复：取**双向覆盖率的最大值**（旧实现只按新题实体数做分母，新题是旧题
    实体的超集时会被稀释到阈值下）。实况：旧题 {湿度,空调} vs 新题 {湿度,空调,温度,
    光照} → 单向 0.50（漏判），反向 1.00。即「挂几个无关实体」即可绕过判重。
    双向取大后该例=1.00 → 正确判重。仅当新题实体面基本覆盖旧题（或反之）时才判重，
    属 fail-safe 方向，对实体面不同的题无影响。
    """
    if not new_entities:
        return 0.0
    new_set = set(e.lower().strip() for e in new_entities if e)
    exist_set = set(e.lower().strip() for e in existing_entities if e)
    if not new_set or not exist_set:
        return 0.0
    inter = len(new_set & exist_set)
    return max(inter / len(new_set), inter / len(exist_set))


def _text_similarity(text1: str, text2: str) -> float:
    """文本相似度（difflib，零依赖）。"""
    if not text1 or not text2:
        return 0.0
    return difflib.SequenceMatcher(None, text1.lower(), text2.lower()).ratio()


def _creativity_score(
    title: str,
    description: str,
    entity_ids: List[str],
    arena_devices: List[Dict],
    existing_tasks: List[Dict],
) -> Tuple[float, Dict]:
    """计算创造力评分（0-1）。

    维度：
    - 新颖性 (40%)：与已有题目的差异度
    - 复杂度 (20%)：涉及设备数量和逻辑复杂度
    - 实用性 (20%)：是否基于真实设备
    - 描述质量 (20%)：题目描述的详细程度
    """
    # 新颖性：与所有已有题目的最小相似度的反向
    max_sim = 0.0
    for task in existing_tasks:
        sim = _text_similarity(title + " " + description, task.get("title", "") + " " + task.get("description", ""))
        max_sim = max(max_sim, sim)
    novelty = 1.0 - max_sim

    # 复杂度：涉及设备数量（2-4个最优，过多或过少扣分）
    n_entities = len(set(entity_ids))
    if 2 <= n_entities <= 4:
        complexity = 1.0
    elif n_entities == 1:
        complexity = 0.5
    elif n_entities <= 6:
        complexity = 0.8
    else:
        complexity = 0.6

    # 实用性：涉及的设备是否都在分区设备列表中
    arena_entity_ids = set(d["entity_id"] for d in arena_devices)
    valid_entities = [e for e in entity_ids if e in arena_entity_ids]
    practicality = len(valid_entities) / max(len(entity_ids), 1)

    # 描述质量：描述长度（20-100字最优）
    desc_len = len(description.strip())
    if 20 <= desc_len <= 100:
        quality = 1.0
    elif desc_len < 20:
        quality = desc_len / 20.0
    else:
        quality = max(0.5, 1.0 - (desc_len - 100) / 200.0)

    score = novelty * 0.4 + complexity * 0.2 + practicality * 0.2 + quality * 0.2
    breakdown = {
        "novelty": round(novelty, 3),
        "complexity": round(complexity, 3),
        "practicality": round(practicality, 3),
        "description_quality": round(quality, 3),
    }
    return round(score, 3), breakdown


# ── 竞技场管理器 ──────────────────────────────────────────────

class ArenaManager:
    """竞技场管理器：分区、题目、提交、验收。"""

    def __init__(self, data_dir: str, gateway=None):
        self.data_dir = os.path.join(data_dir, "arena")
        self.arenas_file = os.path.join(self.data_dir, "arenas.json")
        self.tasks_file = os.path.join(self.data_dir, "tasks.json")
        self.submissions_file = os.path.join(self.data_dir, "submissions.json")
        self._lock = threading.Lock()
        self.gateway = gateway  # Gateway 实例，用于 propose-dsl 验收
        self._vhass_stores = {}  # arena_id -> VHassStore
        os.makedirs(self.data_dir, exist_ok=True)
        self._init_arenas()

    # ── 初始化 ──

    def _init_arenas(self):
        """初始化分区数据（如果不存在）。"""
        if not os.path.exists(self.arenas_file):
            arenas = []
            for a in DEFAULT_ARENAS:
                arenas.append({
                    **a,
                    "created_at": _utcnow_iso(),
                    "phase": "free_writing",  # free_writing | challenge
                    "locked_task_count": 0,
                })
            self._save_json(self.arenas_file, {"arenas": arenas})
        if not os.path.exists(self.tasks_file):
            self._save_json(self.tasks_file, {"tasks": []})
        if not os.path.exists(self.submissions_file):
            self._save_json(self.submissions_file, {"submissions": []})

    def _save_json(self, path: str, data: Dict):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _load_arenas(self) -> List[Dict]:
        with open(self.arenas_file, encoding="utf-8") as f:
            return json.load(f).get("arenas", [])

    def _load_tasks(self) -> List[Dict]:
        with open(self.tasks_file, encoding="utf-8") as f:
            return json.load(f).get("tasks", [])

    def _load_submissions(self) -> List[Dict]:
        with open(self.submissions_file, encoding="utf-8") as f:
            return json.load(f).get("submissions", [])

    def _get_arena(self, arena_id: str) -> Optional[Dict]:
        for a in self._load_arenas():
            if a["id"] == arena_id:
                return a
        return None

    def _get_vhass(self, arena_id: str):
        """获取或创建分区的 vhass 实例。"""
        if arena_id in self._vhass_stores:
            return self._vhass_stores[arena_id]
        arena = self._get_arena(arena_id)
        if not arena:
            return None
        try:
            from .vhass import VHassStore
            seed = {"areas": {arena_id: arena["name"]}, "entities": arena["devices"]}
            seed_path = os.path.join(self.data_dir, f"{arena_id}_seed.json")
            if not os.path.exists(seed_path):
                with open(seed_path, "w", encoding="utf-8") as f:
                    json.dump(seed, f, ensure_ascii=False, indent=2)
            state_path = os.path.join(self.data_dir, f"{arena_id}_state.json")
            store = VHassStore(seed_path=seed_path, state_path=state_path)
            self._vhass_stores[arena_id] = store
            return store
        except Exception as e:
            print(f"[arena] 创建 vhass 失败: {e}")
            return None

    def _reset_vhass(self, arena_id: str):
        """重置分区的 vhass 到初始状态。"""
        arena = self._get_arena(arena_id)
        if not arena:
            return
        try:
            from .vhass import VHassStore
            seed_path = os.path.join(self.data_dir, f"{arena_id}_seed.json")
            state_path = os.path.join(self.data_dir, f"{arena_id}_state.json")
            # 删除状态文件，强制从 seed 重新加载
            if os.path.exists(state_path):
                os.remove(state_path)
            store = VHassStore(seed_path=seed_path, state_path=state_path)
            self._vhass_stores[arena_id] = store
        except Exception as e:
            print(f"[arena] 重置 vhass 失败: {e}")

    # ── 分区管理 ──

    def list_arenas(self) -> List[Dict]:
        """列出所有分区（含统计信息）。"""
        arenas = self._load_arenas()
        tasks = self._load_tasks()
        result = []
        for a in arenas:
            arena_tasks = [t for t in tasks if t.get("arena_id") == a["id"]]
            locked = [t for t in arena_tasks if t.get("status") == "locked"]
            result.append({
                **a,
                "total_tasks": len(arena_tasks),
                "locked_tasks": len(locked),
                "available_tasks": len([t for t in arena_tasks if t.get("status") == "available"]),
                "phase2_progress": round(len(locked) / a.get("phase2_threshold", 20) * 100, 1),
            })
        return result

    def get_arena(self, arena_id: str) -> Optional[Dict]:
        """获取单个分区详情。"""
        arena = self._get_arena(arena_id)
        if not arena:
            return None
        tasks = [t for t in self._load_tasks() if t.get("arena_id") == arena_id]
        return {
            **arena,
            "devices": arena.get("devices", []),
            "tasks": tasks,
            "total_tasks": len(tasks),
            "locked_tasks": len([t for t in tasks if t.get("status") == "locked"]),
        }

    # ── 设备管理（从真实 HA 同步） ──

    def get_ha_devices(self, area_name: Optional[str] = None,
                       domain: Optional[str] = None,
                       keyword: Optional[str] = None) -> List[Dict]:
        """从 gateway 的 device_catalog + HA websocket 注册表获取真实 HA 设备列表。

        device_catalog 的 area 字段可能为空（refresh 失败时），
        用 gateway.ha.entity_areas() 补全区域信息（entity_id → area_name）。

        Args:
            area_name: 按区域名筛选（如 "书房"），None 返回全部
            domain: 按 domain 筛选（如 "light"），None 返回全部
            keyword: 按 entity_id 或 friendly_name 关键词过滤
        Returns:
            设备列表，每项含 entity_id, friendly_name, area, state, domain, attributes
        """
        if not self.gateway:
            return []
        try:
            cat = self.gateway.state.get_device_catalog()
            ents = cat.get("entities", {})
            if isinstance(ents, dict):
                ents = list(ents.values())
            # 从 HA websocket 注册表获取区域映射（比 device_catalog 的 area 更可靠）
            area_map = {}
            try:
                if hasattr(self.gateway, 'ha') and self.gateway.ha:
                    # HALayer 没有 entity_areas，需要用 .client
                    client = getattr(self.gateway.ha, 'client', None) or self.gateway.ha
                    if hasattr(client, 'entity_areas'):
                        area_map = client.entity_areas() or {}
            except Exception:
                pass
            result = []
            for e in ents:
                eid = e.get("entity_id", "")
                if not eid:
                    continue
                # 优先用 websocket 注册表的区域，兜底用 device_catalog 的 area
                area = area_map.get(eid) or e.get("area", "")
                if area_name and area != area_name:
                    continue
                dom = eid.split(".", 1)[0]
                if domain and dom != domain:
                    continue
                fn = e.get("friendly_name") or eid
                if keyword and keyword.lower() not in eid.lower() and keyword.lower() not in fn.lower():
                    continue
                result.append({
                    "entity_id": eid,
                    "friendly_name": fn,
                    "area": area,
                    "state": e.get("state") or "",
                    "domain": dom,
                    "attributes": e.get("attributes") or {},
                })
            return result
        except Exception as ex:
            print(f"[arena] get_ha_devices 失败: {ex}")
            return []

    def get_ha_areas(self) -> List[Dict]:
        """获取 HA 所有区域及设备数量，用于筛选下拉。"""
        if not self.gateway:
            return []
        try:
            devices = self.get_ha_devices()
            area_count = {}
            for d in devices:
                a = d.get("area") or "未分组"
                area_count[a] = area_count.get(a, 0) + 1
            return [{"name": a, "count": c} for a, c in sorted(area_count.items(), key=lambda x: -x[1])]
        except Exception as ex:
            print(f"[arena] get_ha_areas 失败: {ex}")
            return []

    def sync_devices(self, arena_id: str, entity_ids: List[str]) -> Dict:
        """将选定的真实 HA 设备同步到竞技场分区。

        - 从 device_catalog 查找这些 entity_id 的完整信息
        - 追加到分区的 devices 列表（已存在的跳过，去重）
        - 更新 vhass 种子文件并重置 vhass
        """
        arena = self._get_arena(arena_id)
        if not arena:
            return {"ok": False, "error": "分区不存在"}
        if not self.gateway:
            return {"ok": False, "error": "gateway 未连接"}
        # 从 device_catalog 查找
        cat = self.gateway.state.get_device_catalog()
        ents = cat.get("entities", {})
        if isinstance(ents, dict):
            ents = list(ents.values())
        cat_map = {e.get("entity_id"): e for e in ents if e.get("entity_id")}

        existing_ids = set(d["entity_id"] for d in arena.get("devices", []))
        added = []
        for eid in entity_ids:
            eid = eid.strip()
            if not eid or eid in existing_ids:
                continue
            e = cat_map.get(eid)
            if not e:
                continue
            domain = eid.split(".", 1)[0]
            arena.setdefault("devices", []).append({
                "entity_id": eid,
                "friendly_name": e.get("friendly_name") or eid,
                "area": e.get("area", ""),
                "state": e.get("state") or "",
                "domain": domain,
                "attributes": e.get("attributes") or {},
                "synced_from_ha": True,
                "synced_at": _utcnow_iso(),
            })
            existing_ids.add(eid)
            added.append(eid)

        # 保存 arenas.json
        arenas = self._load_arenas()
        for i, a in enumerate(arenas):
            if a["id"] == arena_id:
                arenas[i] = arena
                break
        self._save_json(self.arenas_file, {"arenas": arenas})

        # 更新 vhass 种子文件
        self._rewrite_seed(arena_id, arena)
        # 重置 vhass
        self._reset_vhass(arena_id)

        return {"ok": True, "added": len(added), "added_ids": added,
                "total_devices": len(arena.get("devices", []))}

    def remove_device(self, arena_id: str, entity_id: str) -> Dict:
        """从竞技场分区移除设备。"""
        arena = self._get_arena(arena_id)
        if not arena:
            return {"ok": False, "error": "分区不存在"}
        devices = arena.get("devices", [])
        before = len(devices)
        arena["devices"] = [d for d in devices if d["entity_id"] != entity_id]
        after = len(arena["devices"])
        if before == after:
            return {"ok": False, "error": "设备不存在"}

        arenas = self._load_arenas()
        for i, a in enumerate(arenas):
            if a["id"] == arena_id:
                arenas[i] = arena
                break
        self._save_json(self.arenas_file, {"arenas": arenas})

        self._rewrite_seed(arena_id, arena)
        self._reset_vhass(arena_id)
        return {"ok": True, "removed": entity_id, "total_devices": after}

    def _rewrite_seed(self, arena_id: str, arena: Dict):
        """根据 arena.devices 重写 vhass 种子文件。"""
        seed_path = os.path.join(self.data_dir, f"{arena_id}_seed.json")
        seed = {
            "version": 1,
            "areas": {arena_id: arena.get("name", arena_id)},
            "entities": arena.get("devices", []),
        }
        with open(seed_path, "w", encoding="utf-8") as f:
            json.dump(seed, f, ensure_ascii=False, indent=2)

    # ── 题目管理 ──

    def list_tasks(self, arena_id: str, status: Optional[str] = None) -> List[Dict]:
        """列出分区的题目。"""
        tasks = [t for t in self._load_tasks() if t.get("arena_id") == arena_id]
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        return tasks

    def propose_task(
        self,
        arena_id: str,
        title: str,
        description: str,
        entity_ids: List[str],
        agent_id: str,
    ) -> Dict:
        """提交题目（审核 + 创造力评分）。

        返回：
        - ok: 是否通过审核
        - is_duplicate: 是否重复
        - duplicate_of: 重复的题目 ID
        - creativity_score: 创造力评分
        - task_id: 通过后分配的题目 ID
        - reason: 拒绝原因
        """
        arena = self._get_arena(arena_id)
        if not arena:
            return {"ok": False, "error": f"分区 {arena_id} 不存在"}

        # F-04（FFL 验收）：类型守卫——非法类型返回 400 语义的干净错误，而不是抛异常变 500
        if not isinstance(title, str) or not isinstance(description, str) or not isinstance(agent_id, str):
            return {"ok": False, "error": "title / description / agent_id 必须是字符串"}
        if not isinstance(entity_ids, list) or not all(isinstance(e, str) for e in entity_ids):
            return {"ok": False, "error": "entity_ids 必须是字符串数组"}

        title = (title or "").strip()
        description = (description or "").strip()
        entity_ids = [e.strip() for e in entity_ids if e.strip()]

        if not title:
            return {"ok": False, "error": "题目标题不能为空"}
        if not description:
            return {"ok": False, "error": "题目描述不能为空"}
        if not entity_ids:
            return {"ok": False, "error": "至少涉及一个设备"}

        # 输入校验（FFL 验收 F-05/F-06：垃圾题/超长描述/编造实体必须在此拦下）
        if len(title) < 2:
            return {"ok": False, "error": "标题至少 2 个字符"}
        if len(title) > 50:
            return {"ok": False, "error": f"标题过长（{len(title)} 字符，上限 50）"}
        if len(description) > 2000:
            return {"ok": False, "error": f"描述过长（{len(description)} 字符，上限 2000）"}
        known = {d.get("entity_id") for d in arena.get("devices", []) if d.get("entity_id")}
        unknown = [e for e in entity_ids if e not in known]
        if unknown:
            return {
                "ok": False,
                "error": "以下实体不在分区设备清单内: " + ", ".join(unknown[:5])
                         + ("…" if len(unknown) > 5 else ""),
                "unknown_entities": unknown,
            }

        with self._lock:
            tasks = self._load_tasks()
            # ★ 判重作用域（FFL 验收 F-02）：必须覆盖 available/in_progress/locked。
            # 旧实现只对比 locked 题——刚提出的题是 available，互相之间不参与比对，
            # 串行连发重复题即可全部放行（实测：同一道题串行提 2 次全部通过，与并发无关）。
            arena_tasks = [
                t for t in tasks
                if t.get("arena_id") == arena_id
                and t.get("status") in ("available", "in_progress", "locked")
            ]

            # 第一层：实体重叠度 > 0.6 → 重复
            for t in arena_tasks:
                overlap = _entity_overlap(entity_ids, t.get("entity_ids", []))
                if overlap > 0.6:
                    return {
                        "ok": False,
                        "is_duplicate": True,
                        "duplicate_of": t["id"],
                        "duplicate_title": t.get("title"),
                        "reason": f"实体重叠度 {overlap:.0%} 超过 60%，与题目「{t.get('title')}」重复",
                    }

            # 第二层：文本相似度 > 0.85 → 重复
            for t in arena_tasks:
                sim = _text_similarity(title + " " + description, t.get("title", "") + " " + t.get("description", ""))
                if sim > 0.85:
                    return {
                        "ok": False,
                        "is_duplicate": True,
                        "duplicate_of": t["id"],
                        "duplicate_title": t.get("title"),
                        "reason": f"文本相似度 {sim:.0%} 超过 85%，与题目「{t.get('title')}」重复",
                    }

            # 第三层：LLM 考官（模糊区间 0.6-0.85，由 LLM 仲裁是否真的重复）
            # 仅对文本相似度落在模糊区间的题目调用 LLM，避免不必要的 token 消耗
            for t in arena_tasks:
                sim = _text_similarity(title + " " + description, t.get("title", "") + " " + t.get("description", ""))
                if 0.6 <= sim <= 0.85:
                    judge = self._llm_judge_duplicate(
                        {"title": title, "description": description, "entity_ids": entity_ids},
                        {"title": t.get("title", ""), "description": t.get("description", ""), "entity_ids": t.get("entity_ids", [])},
                    )
                    if judge is not None and judge.get("is_duplicate"):
                        return {
                            "ok": False,
                            "is_duplicate": True,
                            "duplicate_of": t["id"],
                            "duplicate_title": t.get("title"),
                            "reason": f"LLM 考官判定与题目「{t.get('title')}」为同一自动化场景（文本相似度 {sim:.0%}）：{judge.get('reason', '')}",
                            "llm_judge": judge,
                        }

            # 创造力评分
            score, breakdown = _creativity_score(
                title, description, entity_ids, arena.get("devices", []), arena_tasks
            )

            threshold = arena.get("creativity_threshold", 0.3)
            if score < threshold:
                return {
                    "ok": False,
                    "is_duplicate": False,
                    "creativity_score": score,
                    "creativity_breakdown": breakdown,
                    "reason": f"创造力评分 {score} 低于阈值 {threshold}，题目太简单或缺乏创意",
                }

            # 通过审核，创建题目（状态 available，等待 Agent 提交 flow）
            task_id = _new_id("task")
            task = {
                "id": task_id,
                "arena_id": arena_id,
                "title": title,
                "description": description,
                "entity_ids": entity_ids,
                "status": "available",  # available | in_progress | locked | failed
                "creativity_score": score,
                "creativity_breakdown": breakdown,
                "proposed_by": agent_id,
                "proposed_at": _utcnow_iso(),
                "locked_by": None,
                "locked_at": None,
                "flow_dsl": None,
                "verification": None,
                "token_used": 0,
            }
            tasks.append(task)
            self._save_json(self.tasks_file, {"tasks": tasks})

            return {
                "ok": True,
                "task_id": task_id,
                "creativity_score": score,
                "creativity_breakdown": breakdown,
                "status": "available",
                "message": "题目审核通过，请提交 DSL flow 进行验收",
            }

    # ── 提交与验收 ──

    def submit_flow(
        self,
        arena_id: str,
        task_id: str,
        dsl: str,
        agent_id: str,
    ) -> Dict:
        """提交 flow 进行验收。

        验收流程：
        1. 重置 vhass 到初始状态
        2. 调用 gw.propose_dsl(dsl, vhass_store, expected_postconditions)
        3. staging 闸门在 vhass 上重放 flow，检查后置状态
        4. 通过 → 题目锁定；失败 → 返回失败原因
        """
        arena = self._get_arena(arena_id)
        if not arena:
            return {"ok": False, "error": f"分区 {arena_id} 不存在"}

        with self._lock:
            tasks = self._load_tasks()
            task = next((t for t in tasks if t["id"] == task_id and t.get("arena_id") == arena_id), None)
            if not task:
                return {"ok": False, "error": f"题目 {task_id} 不存在"}
            # ★ F-01（FFL 验收 P0）：只允许从 available 进入验收。
            # 旧守卫「in_progress 且 locked_by != agent_id 才拒」会被**同 agent_id** 的并发
            # 提交绕过：两条 DSL 都通过检查、都验收、后写覆盖前写。题目是一次性的，
            # in_progress / locked 一律拒绝（验收失败会解锁回 available，重试不受影响）。
            if task.get("status") == "locked":
                return {"ok": False, "error": "题目已被锁定", "locked_by": task.get("locked_by")}
            if task.get("status") != "available":
                return {"ok": False, "error": f"题目正在验收中（status={task.get('status')}），请稍后再试"}

            # 标记为进行中
            task["status"] = "in_progress"
            task["locked_by"] = agent_id
            task["locked_at"] = _utcnow_iso()
            self._save_json(self.tasks_file, {"tasks": tasks})

        # 验收（不持有锁，避免阻塞）
        try:
            result = self._verify_flow(arena_id, task, dsl, agent_id)
        except Exception as e:
            result = {"ok": False, "error": f"验收异常: {e}", "stage": "exception"}

        # 更新题目状态
        with self._lock:
            tasks = self._load_tasks()
            for t in tasks:
                if t["id"] == task_id:
                    # F-01 防御性复核：写回前确认状态仍处于本次验收的 in_progress，
                    # 状态若已变化则本次结果不得覆盖
                    if t.get("status") != "in_progress" or t.get("locked_by") != agent_id:
                        result = {"ok": False,
                                  "error": "题目状态已变化，本次提交未生效",
                                  "stage": "race_guard"}
                        continue
                    if result.get("ok"):
                        t["status"] = "locked"
                        t["flow_dsl"] = dsl
                        t["verification"] = result.get("gate", {})
                        t["token_used"] = result.get("_telemetry", {}).get("estimated_tokens", 0)
                        # 更新分区锁定计数
                        arenas = self._load_arenas()
                        for a in arenas:
                            if a["id"] == arena_id:
                                a["locked_task_count"] = a.get("locked_task_count", 0) + 1
                                # 检查是否进入 Phase 2
                                if a["locked_task_count"] >= a.get("phase2_threshold", 20):
                                    a["phase"] = "challenge"
                        self._save_json(self.arenas_file, {"arenas": arenas})
                    else:
                        t["status"] = "available"  # 失败后解锁，其他 Agent 可以选
                        t["locked_by"] = None
                        t["locked_at"] = None
                    break
            self._save_json(self.tasks_file, {"tasks": tasks})

            # 记录提交
            submissions = self._load_submissions()
            submissions.append({
                "id": _new_id("sub"),
                "arena_id": arena_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "dsl": dsl,
                "success": bool(result.get("ok")),
                "error": result.get("error", ""),
                "stage": result.get("stage", ""),
                "token_used": result.get("_telemetry", {}).get("estimated_tokens", 0),
                "created_at": _utcnow_iso(),
            })
            self._save_json(self.submissions_file, {"submissions": submissions})

        return result

    def _verify_flow(self, arena_id: str, task: Dict, dsl: str, agent_id: str) -> Dict:
        """调用 propose-dsl 进行 vhass 验收。"""
        if not self.gateway:
            return {"ok": False, "error": "网关未初始化，无法验收", "stage": "init"}

        # 重置 vhass
        self._reset_vhass(arena_id)
        vhass_store = self._get_vhass(arena_id)
        if not vhass_store:
            return {"ok": False, "error": "vhass 初始化失败", "stage": "vhass"}

        # 从题目描述推断期望的后置状态（简单规则：提到的设备如果是"打开/开启"则期望 on）
        expected = self._infer_postconditions(task, dsl)

        try:
            result = self.gateway.propose_dsl(
                dsl=dsl,
                agent_id=f"arena_{agent_id}",
                expected_postconditions=expected if expected else None,
                vhass_store=vhass_store,
                strict=False,
            )
            return result
        except Exception as e:
            return {"ok": False, "error": str(e), "stage": "propose_dsl_exception"}

    def _llm_judge_duplicate(self, new_task: Dict, existing_task: Dict) -> Optional[Dict]:
        """第三层 LLM 考官：判断两个题目是否为同一自动化场景。

        返回 {"is_duplicate": bool, "reason": str}，LLM 不可用时返回 None（fail-open）。
        仅在文本相似度 0.6-0.85 模糊区间调用，避免不必要的 token 消耗。
        """
        try:
            from .llm_client import chat_sync
        except Exception:
            return None  # llm_client 不可用（缺 httpx 等），fail-open

        prompt = f"""你是智能家居自动化场景的考官。请判断以下两个自动化场景是否本质上是同一个场景（触发条件和期望效果相同，只是表述不同）。

【新题目】
标题：{new_task.get('title', '')}
描述：{new_task.get('description', '')}
涉及设备：{', '.join(new_task.get('entity_ids', []))}

【已有题目】
标题：{existing_task.get('title', '')}
描述：{existing_task.get('description', '')}
涉及设备：{', '.join(existing_task.get('entity_ids', []))}

判断标准：
- 如果两个题目的触发条件（什么事件触发）和期望效果（最终达到什么状态）本质相同，即使措辞不同，也判定为重复。
- 如果触发条件不同（如一个是"电脑开机"，一个是"人进入书房"），或期望效果不同（如一个是"开灯"，一个是"开空调"），则不重复。
- 涉及设备重叠但触发/效果不同，不算重复。

请严格只返回 JSON，不要其他文字：
{{"is_duplicate": true/false, "reason": "简短理由"}}"""

        try:
            resp = chat_sync(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
            )
            # 解析 JSON 响应（LLM 可能返回 markdown 代码块）
            text = resp.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:]
                text = text.strip()
            # 提取第一个 { 到最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
            result = json.loads(text)
            return {
                "is_duplicate": bool(result.get("is_duplicate", False)),
                "reason": str(result.get("reason", ""))[:200],
            }
        except Exception as e:
            print(f"[arena] LLM 考官调用失败: {e}")
            return None  # LLM 调用失败，fail-open 不判重

    def _infer_postconditions(self, task: Dict, dsl: str) -> List[Dict]:
        """从题目和 DSL 推断期望后置状态。

        MVP 简化版：从题目描述中提取"打开/开启/启动"对应的设备，期望状态为 on。
        更精确的推断在后续版本由 LLM 考官完成。
        """
        expected = []
        desc = (task.get("title", "") + " " + task.get("description", "")).lower()
        dsl_lower = dsl.lower()
        combined = desc + " " + dsl_lower

        for eid in task.get("entity_ids", []):
            domain = eid.split(".")[0] if "." in eid else ""
            entity_name = eid.split(".")[-1] if "." in eid else eid
            # 检查是否提到打开/开启/启动
            open_keywords = ["打开", "开启", "启动", "开灯", "开空调", "开电视", "turn on", "open"]
            close_keywords = ["关闭", "关掉", "关灯", "关空调", "turn off", "close"]
            if any(kw in combined for kw in open_keywords) and domain in ("light", "switch", "media_player", "climate", "fan"):
                expected.append({"entity_id": eid, "state": "on"})
            elif any(kw in combined for kw in close_keywords) and domain in ("light", "switch", "media_player", "climate", "fan"):
                expected.append({"entity_id": eid, "state": "off"})
        return expected

    # ── 排行榜 ──

    def get_leaderboard(self, arena_id: Optional[str] = None) -> List[Dict]:
        """获取排行榜（按创造力总分 + 锁定题目数）。"""
        tasks = self._load_tasks()
        if arena_id:
            tasks = [t for t in tasks if t.get("arena_id") == arena_id]
        locked_tasks = [t for t in tasks if t.get("status") == "locked"]

        # 按 Agent 聚合
        agents = {}
        for t in locked_tasks:
            agent = t.get("locked_by", "unknown")
            if agent not in agents:
                agents[agent] = {
                    "agent_id": agent,
                    "locked_tasks": 0,
                    "total_creativity": 0.0,
                    "total_token": 0,
                }
            agents[agent]["locked_tasks"] += 1
            agents[agent]["total_creativity"] += t.get("creativity_score", 0)
            agents[agent]["total_token"] += t.get("token_used", 0)

        # 排序：锁定题目数降序，然后平均创造力降序
        leaderboard = []
        for agent, stats in agents.items():
            avg_creativity = stats["total_creativity"] / max(stats["locked_tasks"], 1)
            leaderboard.append({
                **stats,
                "avg_creativity": round(avg_creativity, 3),
                "score": round(stats["locked_tasks"] * avg_creativity, 3),
            })
        leaderboard.sort(key=lambda x: x["score"], reverse=True)
        return leaderboard

    # ── 统计 ──

    def get_stats(self) -> Dict:
        """获取竞技场全局统计。"""
        arenas = self._load_arenas()
        tasks = self._load_tasks()
        submissions = self._load_submissions()
        locked = [t for t in tasks if t.get("status") == "locked"]
        return {
            "total_arenas": len(arenas),
            "total_tasks": len(tasks),
            "locked_tasks": len(locked),
            # 语义说明（FFL 验收 F-07）：total_submissions 是 submit 端点的**调用次数**
            #（含验收失败的尝试），不等于有效提交数；valid_submissions 才是当前
            # flow_dsl 非空（= 验收通过）的任务数。
            "total_submissions": len(submissions),
            "valid_submissions": len([t for t in tasks if t.get("flow_dsl")]),
            "success_rate": round(
                len([s for s in submissions if s.get("success")]) / max(len(submissions), 1) * 100, 1
            ),
            "total_token_used": sum(s.get("token_used", 0) for s in submissions),
            "phase2_arenas": [a["id"] for a in arenas if a.get("phase") == "challenge"],
        }
