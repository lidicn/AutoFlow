#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 自助编写支持测试：dsl_help 自助指南 / propose_scene 弃用重定向 / discover 区域兜底。
运行：python tests/test_authoring_help.py   （环境无 pytest，内置零依赖运行器）
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_author_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()


def _write_catalog():
    """写一份含『书房』实体的合成 catalog：实体的 area 字段存的是 area_id(shu_fang)，
    用以验证 discover 区域兜底（之前会因此返回空）。同时填充 entity_mapping 的区域索引/别名，
    模拟 refresh_catalog 后的共享态。"""
    cat = {
        "freshness": "test",
        "areas": {"shu_fang": "书房", "living_room": "客厅"},
        "entities": {
            "light.philips_cn_249518489_rwread_s_2_light": {
                "friendly_name": "书房台灯 灯", "area": "shu_fang",
                "domain": "light", "state": "off",
            },
            "binary_sensor.0x00158d0001a2520d_motion": {
                "friendly_name": "书房人体传感器 Motion", "area": "shu_fang",
                "domain": "binary_sensor", "state": "off",
            },
            "light.living_room_main": {
                "friendly_name": "客厅主灯", "area": "living_room",
                "domain": "light", "state": "off",
            },
        },
    }
    GW.state.set_device_catalog(cat)
    GW.state.save_entity_mapping({
        "mappings": {},
        "areas": {"shu_fang": "书房", "living_room": "客厅"},
        "room_aliases": {"书房": "书房", "客厅": "客厅", "全屋": "__all__"},
    })


def test_dsl_help_structure():
    h = GW.dsl_help()
    assert "workflow" in h and "grammar" in h and "subflows" in h
    assert "example" in h and "submit" in h
    names = [s["name"] for s in h["subflows"]]
    assert "demo_notify" in names
    # 约束里必须指明唯一提交入口
    assert any("autoflow_propose_dsl" in c for c in h["constraints"])
    # 语法必须覆盖核心关键字
    for kw in ("场景", "触发", "动作", "调用子流程", "并行", "分支"):
        assert kw in h["grammar"]
    # WB4 #509：『条件:』是场景级前置条件（无否则），文档必须明确提示改用 分支: 实现二选一
    cond_doc = h["grammar"].get("条件", "")
    assert "分支" in cond_doc, "dsl_help 的『条件』条目必须说明无否则、需二选一改用 分支:"
    assert "否则" in cond_doc, "dsl_help 的『条件』条目应提及否则分支的替代语法"
    print("[ok] dsl_help 结构完整，含 demo_notify 子流程与提交入口说明")


def test_propose_scene_deprecated_redirect():
    r = GW.propose_scene_redirect()
    assert r.get("deprecated") is True
    assert "autoflow_propose_dsl" in r.get("how", {}).get("tool", "")
    print("[ok] autoflow_propose_scene 已改为弃用重定向，不再崩旧 schema")


def test_discover_area_fallback():
    _write_catalog()
    r = GW.discover(area="书房")
    eids = [e["entity_id"] for e in r["entities"]]
    # 之前 area 存 area_id → 返回空；修复后 friendly_name 兜底应能命中两个书房实体
    assert "light.philips_cn_249518489_rwread_s_2_light" in eids
    assert "binary_sensor.0x00158d0001a2520d_motion" in eids
    # 客厅实体不应混入
    assert "light.living_room_main" not in eids
    print(f"[ok] discover(area='书房') 兜底命中 {len(eids)} 个书房实体（修复前为空）")


def _main():
    tests = [test_dsl_help_structure, test_propose_scene_deprecated_redirect,
             test_discover_area_fallback]
    fail = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            fail += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa
            fail += 1
            print(f"[ERR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n=== {len(tests)-fail}/{len(tests)} passed ===")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    _main()
