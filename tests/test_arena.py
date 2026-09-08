# -*- coding: utf-8 -*-
"""竞技场（arena.py，v2.0）冒烟测试。

该模块自引入起零测试覆盖，本文件补齐：
- 纯函数：_entity_overlap / _text_similarity / _creativity_score 各维度；
- ArenaManager 初始化：默认 3 分区落盘；
- propose_task 三层判重：
  · 第一层 实体重叠度 >0.6 → 判重（只对 status=locked 的题目，边界 0.6 不拦）；
  · 第二层 文本相似度 >0.85 → 判重（实体不重叠时也能拦住）；
  · 第三层 LLM 考官 fail-open：LLM 不可用时不得误杀、不得炸；
- 创造力阈值：低于 creativity_threshold 拒绝（用阈值 0.99 构造确定性用例）；
- 输入校验：空标题/空描述/无设备/分区不存在；
- leaderboard / stats 冒烟。

注意：propose_task 的判重只针对 status=="locked" 的题目，
测试判重路径时先把已建题目在 tasks.json 里手动置为 locked。
"""
import os
import sys
import json
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.arena import (
    ArenaManager,
    _entity_overlap,
    _text_similarity,
    _creativity_score,
)

STUDY_DEVICES = ["switch.computer", "light.desk_lamp", "light.monitor_lamp"]


class TestPureFunctions(unittest.TestCase):
    def test_overlap_full(self):
        self.assertAlmostEqual(_entity_overlap(["a", "b"], ["a", "b"]), 1.0)

    def test_overlap_half(self):
        self.assertAlmostEqual(_entity_overlap(["a", "b"], ["a", "c"]), 0.5)

    def test_overlap_case_insensitive(self):
        self.assertAlmostEqual(_entity_overlap(["Light.A"], ["light.a"]), 1.0)

    def test_overlap_empty_new_is_zero(self):
        self.assertEqual(_entity_overlap([], ["a"]), 0.0)

    def test_text_similarity_identical(self):
        self.assertAlmostEqual(_text_similarity("开灯", "开灯"), 1.0)

    def test_text_similarity_different(self):
        self.assertLess(_text_similarity("电脑开机打开灯", "下雨收衣服"), 0.5)

    def test_text_similarity_empty(self):
        self.assertEqual(_text_similarity("", "x"), 0.0)

    def _score(self, title, desc, entities, devices, tasks):
        return _creativity_score(title, desc, entities, devices, tasks)

    def test_creativity_brand_new_perfect_setup(self):
        devices = [{"entity_id": e} for e in STUDY_DEVICES]
        score, bd = self._score(
            "电脑开机联动灯光",
            "当书房电脑开机时，自动打开显示器挂灯和台灯，进入工作状态",
            STUDY_DEVICES, devices, [])
        self.assertEqual(bd["novelty"], 1.0)       # 无已有题目
        self.assertEqual(bd["complexity"], 1.0)    # 3 个设备（2-4 最优）
        self.assertEqual(bd["practicality"], 1.0)  # 全在池中
        self.assertEqual(bd["description_quality"], 1.0)  # 20-100 字
        self.assertGreaterEqual(score, 0.9)

    def test_creativity_entity_not_in_pool_lowers_practicality(self):
        devices = [{"entity_id": "switch.computer"}]
        score, bd = self._score(
            "测试题目", "一个足够长的题目描述用来通过质量分检查",
            ["switch.computer", "light.nonexistent"], devices, [])
        self.assertEqual(bd["practicality"], 0.5)

    def test_creativity_short_desc_lowers_quality(self):
        devices = [{"entity_id": e} for e in STUDY_DEVICES]
        _, bd = self._score("题目", "太短", STUDY_DEVICES, devices, [])
        self.assertLess(bd["description_quality"], 0.5)

    def test_creativity_single_entity_half_complexity(self):
        devices = [{"entity_id": "switch.computer"}]
        _, bd = self._score("题目", "一个足够长的题目描述用来通过质量分",
                            ["switch.computer"], devices, [])
        self.assertEqual(bd["complexity"], 0.5)

    def test_creativity_similar_existing_lowers_novelty(self):
        devices = [{"entity_id": e} for e in STUDY_DEVICES]
        existing = [{"title": "电脑开机开灯", "description": "电脑开机时自动开灯"}]
        _, bd = self._score("电脑开机开灯", "电脑开机时自动开灯",
                            STUDY_DEVICES, devices, existing)
        self.assertLess(bd["novelty"], 0.2)


class _ManagerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_arena_")
        self.mgr = ArenaManager(self.tmp)  # gateway=None：只测题目审核，不验收

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _lock_task(self, task_id: str):
        """把已建题目手动置为 locked（判重只针对 locked 题目）。"""
        with open(self.mgr.tasks_file, encoding="utf-8") as f:
            tasks = json.load(f)["tasks"]
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "locked"
        self.mgr._save_json(self.mgr.tasks_file, {"tasks": tasks})

    def _propose_ok(self, title="电脑开机联动灯光", desc=None, entities=None):
        desc = desc or "当书房电脑开机时，自动打开显示器挂灯和台灯，进入工作状态"
        entities = entities or STUDY_DEVICES
        r = self.mgr.propose_task("study_room", title, desc, entities, "agent-1")
        self.assertTrue(r["ok"], f"预期通过却被拒: {r}")
        return r["task_id"]


class TestInit(_ManagerBase):
    def test_default_three_arenas_created(self):
        arenas = self.mgr.list_arenas()
        self.assertEqual(len(arenas), 3)
        ids = {a["id"] for a in arenas}
        self.assertEqual(ids, {"study_room", "living_room", "master_bedroom"})
        for a in arenas:
            self.assertEqual(len(a["devices"]), 8)


class TestProposeValidation(_ManagerBase):
    def test_ok(self):
        tid = self._propose_ok()
        tasks = self.mgr.list_tasks("study_room")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], tid)
        self.assertEqual(tasks[0]["status"], "available")
        self.assertGreaterEqual(tasks[0]["creativity_score"], 0.3)

    def test_empty_title(self):
        r = self.mgr.propose_task("study_room", "", "desc", STUDY_DEVICES, "a")
        self.assertFalse(r["ok"])

    def test_empty_description(self):
        r = self.mgr.propose_task("study_room", "t", "", STUDY_DEVICES, "a")
        self.assertFalse(r["ok"])

    def test_no_entities(self):
        r = self.mgr.propose_task("study_room", "t", "d", [], "a")
        self.assertFalse(r["ok"])

    def test_arena_not_found(self):
        r = self.mgr.propose_task("nope", "t", "d", STUDY_DEVICES, "a")
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])


class TestLayer1EntityOverlap(_ManagerBase):
    def test_overlap_above_60pct_rejected(self):
        tid = self._propose_ok()
        self._lock_task(tid)
        # 3 个实体中 2 个重叠 = 0.667 > 0.6 → 拦
        r = self.mgr.propose_task(
            "study_room", "完全不同的另一个题目",
            "这个题目的描述与之前完全不同，只是设备有重叠",
            ["switch.computer", "light.desk_lamp", "climate.study_ac"], "agent-2")
        self.assertFalse(r["ok"])
        self.assertTrue(r["is_duplicate"])
        self.assertEqual(r["duplicate_of"], tid)

    def test_overlap_at_50pct_passes(self):
        tid = self._propose_ok()
        self._lock_task(tid)
        # 2 个实体中 1 个重叠 = 0.5，未超 0.6 → 不拦（第一层）
        r = self.mgr.propose_task(
            "study_room", "雨天自动关窗提醒",
            "检测到下雨且窗户未关时，发送提醒并关闭窗帘",
            ["switch.computer", "climate.study_ac"], "agent-2")
        self.assertTrue(r["ok"], f"0.5 重叠不应被第一层拦截: {r}")


class TestLayer2TextSimilarity(_ManagerBase):
    def test_near_identical_text_rejected(self):
        tid = self._propose_ok(
            title="观影模式自动调光",
            desc="打开电视时自动调暗客厅主灯并打开氛围灯，营造观影氛围")
        self._lock_task(tid)
        # 实体完全不同（绕开第一层），文本几乎相同（sim>0.85 → 第二层拦）
        r = self.mgr.propose_task(
            "study_room", "观影模式自动调光",
            "打开电视时自动调暗客厅主灯并打开氛围灯，营造观影氛围。",
            ["climate.study_ac", "cover.study_curtain"], "agent-2")
        self.assertFalse(r["ok"])
        self.assertTrue(r["is_duplicate"])
        self.assertIn("文本相似度", r["reason"])


class TestLayer3LlmFailOpen(_ManagerBase):
    def test_llm_unavailable_does_not_crash(self):
        """LLM 考官不可用时必须 fail-open：返回 None 或合法判定，绝不抛异常。"""
        r = self.mgr._llm_judge_duplicate(
            {"title": "a", "description": "b", "entity_ids": []},
            {"title": "c", "description": "d", "entity_ids": []})
        self.assertTrue(r is None or "is_duplicate" in r)


class TestCreativityThreshold(_ManagerBase):
    def test_below_threshold_rejected(self):
        # 满分题目总分=1.0，阈值调到 1.01 → 必然低于阈值，确定性验证比较逻辑
        with open(self.mgr.arenas_file, encoding="utf-8") as f:
            arenas = json.load(f)["arenas"]
        for a in arenas:
            if a["id"] == "study_room":
                a["creativity_threshold"] = 1.01
        self.mgr._save_json(self.mgr.arenas_file, {"arenas": arenas})
        r = self.mgr.propose_task("study_room", "电脑开机联动灯光",
                                  "当书房电脑开机时，自动打开显示器挂灯和台灯",
                                  STUDY_DEVICES, "agent-1")
        self.assertFalse(r["ok"])
        self.assertIn("创造力", r["reason"])
        self.assertIn("creativity_breakdown", r)


class TestBoardAndStats(_ManagerBase):
    def test_leaderboard_and_stats(self):
        self._propose_ok()
        lb = self.mgr.get_leaderboard("study_room")
        self.assertIsInstance(lb, list)
        stats = self.mgr.get_stats()
        self.assertIn("total_tasks", stats)
        self.assertGreaterEqual(stats["total_tasks"], 1)


class TestFflRegression(_ManagerBase):
    """FFL 书房竞技场验收（2026-09-08）炸出的缺陷回归守卫。

    缺陷原始报告在 FFL 工作区 FINDINGS.md（F-01~F-07），
    本类把其中可在单测层复现的用例钉死，防止修复被回退。
    """

    def test_f02_serial_duplicate_rejected(self):
        """F-02：判重必须覆盖 available 题——串行（无并发）重复提交也必须被拦。"""
        self._propose_ok(title="串行重复实验",
                         desc="两道完全相同的题，串行提交第二道必须被判重拦截",
                         entities=STUDY_DEVICES[:2])
        r = self.mgr.propose_task(
            "study_room", "串行重复实验",
            "两道完全相同的题，串行提交第二道必须被判重拦截",
            STUDY_DEVICES[:2], "agent-2")
        self.assertFalse(r["ok"], "串行重复题必须被拦（判重作用域须覆盖 available）")
        self.assertTrue(r.get("is_duplicate"))

    def test_f05_unknown_entity_rejected(self):
        """F-05：编造实体必须被拒，而不是只扣创意分。"""
        r = self.mgr.propose_task(
            "study_room", "编造实体测试",
            "这个题目引用了不存在于分区设备清单的实体",
            ["nonexistent.entity123"], "agent-1")
        self.assertFalse(r["ok"])
        self.assertIn("不在分区设备清单", r["error"])
        self.assertEqual(r.get("unknown_entities"), ["nonexistent.entity123"])

    def test_f06_description_length_cap(self):
        r = self.mgr.propose_task("study_room", "超长描述测试", "长" * 2001,
                                  STUDY_DEVICES, "agent-1")
        self.assertFalse(r["ok"])
        self.assertIn("上限 2000", r["error"])

    def test_f06_title_length_cap(self):
        r = self.mgr.propose_task("study_room", "标" * 51, "正常描述，只是标题超长",
                                  STUDY_DEVICES, "agent-1")
        self.assertFalse(r["ok"])
        self.assertIn("上限 50", r["error"])

    def test_f06_title_min_length(self):
        r = self.mgr.propose_task("study_room", "x", "单字符标题应被拒",
                                  STUDY_DEVICES, "agent-1")
        self.assertFalse(r["ok"])
        self.assertIn("至少 2 个字符", r["error"])

    def test_f04_non_string_title_rejected_not_crash(self):
        r = self.mgr.propose_task("study_room", 12345, "描述", STUDY_DEVICES, "a")
        self.assertFalse(r["ok"])
        self.assertIn("字符串", r["error"])

    def test_f04_non_list_entities_rejected(self):
        r = self.mgr.propose_task("study_room", "标题", "描述", 12345, "a")
        self.assertFalse(r["ok"])
        self.assertIn("entity_ids", r["error"])

    def test_f01_submit_rejects_in_progress_even_same_agent(self):
        """F-01（P0）：同 agent_id 的二次提交也必须被拒——旧守卫只挡不同 agent，
        同 agent 并发提交两条 DSL 会全部通过并互相覆盖。"""
        tid = self._propose_ok()
        with open(self.mgr.tasks_file, encoding="utf-8") as f:
            tasks = json.load(f)["tasks"]
        for t in tasks:
            if t["id"] == tid:
                t["status"] = "in_progress"
                t["locked_by"] = "agent-1"
        self.mgr._save_json(self.mgr.tasks_file, {"tasks": tasks})
        # 同 agent_id 提交（旧实现此处放行 → 竞态覆盖）
        r = self.mgr.submit_flow("study_room", tid, "flow: []", "agent-1")
        self.assertFalse(r["ok"], "同 agent_id 的二次提交也必须被拒")
        self.assertIn("验收中", r["error"])

    def test_f01_submit_rejects_locked(self):
        tid = self._propose_ok()
        self._lock_task(tid)
        r = self.mgr.submit_flow("study_room", tid, "flow: []", "agent-2")
        self.assertFalse(r["ok"])
        self.assertIn("已被锁定", r["error"])

    def test_f07_stats_has_valid_submissions(self):
        self._propose_ok()
        s = self.mgr.get_stats()
        self.assertIn("valid_submissions", s)
        self.assertEqual(s["valid_submissions"], 0)  # 题目尚无 flow_dsl


if __name__ == "__main__":
    unittest.main()
