#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iss_07ef8f36bb 回归：propose_dsl 并发调用线程安全 + 零交叉污染。

背景：原 MCP 层在请求热路径里 `importlib.reload(dsl_engine)` 且同步跑编译，
单 worker 事件循环被占满 → 并发编译被串行化（p95 1.45s → 7.8s）。
修复：删掉热路径 reload + 用 `asyncio.to_thread` 把同步编译卸载到线程池。
本测试模拟 to_thread 的真实并发（线程池并发调 GW.propose_dsl），断言：
  1) 全部 ok、各自落独立提案（proposal_id 互异）；
  2) 零交叉污染（每个结果对应自身 DSL 的动作实体正确）；
  3) 并发后 dsl_engine 模块未被污染（仍能正常编译）。
"""
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_cc_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
GW.state.add_mapping("书房主灯", "light.study_main")
GW.state.add_mapping("客厅主灯", "light.living_room_main")
for _eid in ("light.study_main", "light.living_room_main",
             "binary_sensor.study_door", "device_tracker.me"):
    GW.state.add_mapping(_eid, _eid)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


DSL_STUDY = """场景: 书房入户播报2
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯, brightness=80)
调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)
"""

DSL_HOME = """场景: 回家开灯播报
触发: device_tracker.me 回家
动作: light.turn_on(客厅主灯, brightness=70)
调用子流程: demo_notify(text=欢迎回家, room=客厅, level=一般)
"""


def _call(dsl, agent, post):
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
        ("light.living_room_main", "客厅主灯", "客厅", "off", {}),
        ("device_tracker.me", "我", "大门", "not_home", {}),
    )
    return GW.propose_dsl(dsl, agent, post, vhass_store=store)


def test_concurrent_propose_dsl_thread_safe():
    # 10 路并发（复刻 WB16 探针强度），混合两种 DSL + 不同 agent
    jobs = []
    for i in range(10):
        if i % 2 == 0:
            jobs.append((DSL_STUDY, f"agent_{i}",
                         [{"entity_id": "light.study_main", "state": "on"}]))
        else:
            jobs.append((DSL_HOME, f"agent_{i}",
                         [{"entity_id": "light.living_room_main", "state": "on"}]))

    results = [None] * len(jobs)
    errs = []

    def _worker(idx, dsl, agent, post):
        try:
            results[idx] = _call(dsl, agent, post)
        except Exception as e:  # pragma: no cover - 仅用于暴露线程内异常
            errs.append((idx, repr(e)))

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_worker, i, *jobs[i]) for i in range(len(jobs))]
        for f in futs:
            f.result()  # 传播线程内未捕获异常

    assert not errs, f"并发调用抛异常: {errs}"

    # 全部成功且各自落独立提案
    for r in results:
        assert r["ok"], r
        assert r.get("proposal_id"), r
    pids = {r["proposal_id"] for r in results}
    assert len(pids) == len(results), \
        f"提案 id 应互不重复（零交叉污染），实得 {len(pids)}/{len(results)}"

    # 零交叉污染：每条结果对应自身 DSL（动作节点实体正确）
    study = [r for r, j in zip(results, jobs) if j[0] is DSL_STUDY]
    home = [r for r, j in zip(results, jobs) if j[0] is DSL_HOME]
    assert len(study) == 5 and len(home) == 5, (len(study), len(home))
    for r in study:
        assert any("light.turn_on(light.study_main)" in s
                   for s in r["gate"]["replayed_services"]), r["gate"]
    for r in home:
        assert any("light.turn_on(light.living_room_main)" in s
                   for s in r["gate"]["replayed_services"]), r["gate"]


def test_dsl_engine_not_corrupted_after_concurrent_calls():
    # 并发后模块仍可用：再编一个 DSL 应成功（验证无 reload 残留 / 模块命名空间未损）
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL_STUDY, "agent_post",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"], res
    assert res["gate"]["passed"] is True, res["gate"]
