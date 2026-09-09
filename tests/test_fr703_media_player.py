# -*- coding: utf-8 -*-
"""F-R7-03 守卫：media_player 语义态对齐（playing/off）。

R7 实况：media_player 断言恒推 on，与 vhass 终态（media_play→playing）
错位，团队只能 on+取值 绕过。修复后：
- 推断器 media_player 打开/播放 → playing，关闭 → off；
- vhass turn_on 归一 playing（电视模型：开机即播放），media_play/turn_on
  两条服务路径收敛到同一断言。

运行：pytest tests/test_fr703_media_player.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ.setdefault("AUTOFLLOW_DATA_DIR", tempfile.mkdtemp(prefix="af_fr703_env_"))

from autoflow_gateway.arena import ArenaManager
from autoflow_gateway.vhass import VHassStore
from autoflow_gateway.config import reset_config

reset_config()


def _mgr():
    return ArenaManager(tempfile.mkdtemp(prefix="af_fr703_"), gateway=object())


def test_infer_media_player_playing():
    mgr = _mgr()
    task = {"title": "打开电视看新闻", "description": "当有人时打开电视并播放新闻。",
            "entity_ids": ["media_player.tv", "binary_sensor.pir"]}
    exp = mgr._infer_postconditions(task, "动作: media_player.media_play media_player.tv")
    got = {e["entity_id"]: e["state"] for e in exp}
    assert got.get("media_player.tv") == "playing", got  # 不再是 on


def test_infer_media_player_off():
    mgr = _mgr()
    task = {"title": "关闭电视", "description": "当门关闭时关闭电视。",
            "entity_ids": ["media_player.tv"]}
    exp = mgr._infer_postconditions(task, "动作: media_player.turn_off media_player.tv")
    got = {e["entity_id"]: e["state"] for e in exp}
    assert got.get("media_player.tv") == "off", got


def test_infer_light_unchanged():
    # 回归保护：light/switch/climate 仍推 on/off
    mgr = _mgr()
    task = {"title": "开灯", "description": "当有人时打开台灯。",
            "entity_ids": ["light.desk"]}
    exp = mgr._infer_postconditions(task, "动作: light.turn_on light.desk")
    assert exp == [{"entity_id": "light.desk", "state": "on"}]


def test_vhass_turn_on_converges_to_playing():
    seed = {"version": 1, "areas": {"t": "t"}, "entities": [
        {"entity_id": "media_player.tv", "friendly_name": "电视", "area": "t",
         "state": "off", "domain": "media_player", "attributes": {}}]}
    with tempfile.TemporaryDirectory() as d:
        sp = os.path.join(d, "seed.json")
        json.dump(seed, open(sp, "w", encoding="utf-8"), ensure_ascii=False)
        store = VHassStore(seed_path=sp)
        # turn_on 与 media_play 两条路径终态一致（都 playing）
        store.apply_service("media_player", "turn_on",
                            {"entity_id": "media_player.tv"})
        assert store.get_state("media_player.tv")["state"] == "playing"
        store.apply_service("media_player", "turn_off",
                            {"entity_id": "media_player.tv"})
        assert store.get_state("media_player.tv")["state"] == "off"
        store.apply_service("media_player", "media_play",
                            {"entity_id": "media_player.tv"})
        assert store.get_state("media_player.tv")["state"] == "playing"
