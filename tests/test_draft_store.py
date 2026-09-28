#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C20/C19 单元测试：草稿暂存（mimo 亮点①）与结构化错误 fix/candidates（mimo 亮点②）。

零依赖运行：python tests/test_draft_store.py
"""
import os
import sys

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

from autoflow_gateway.draft_store import DraftStore, stage_draft, get_draft
from autoflow_gateway.errors import AutoFlowError, ErrCode, fix_patch, _compile_error_envelope


def test_stage_and_get():
    s = DraftStore()
    ref = s.put({"flow": {"id": "x", "nodes": []}, "dsl": "a"})
    assert ref.startswith("af:")
    assert len(ref) == 11  # "af:" + 8 hex
    got = s.get(ref)
    assert got is not None and got["dsl"] == "a"


def test_ref_stable_for_same_content():
    s = DraftStore()
    p = {"flow": {"id": "x", "nodes": [{"id": "n1"}]}, "dsl": "same"}
    assert s.put(p) == s.put(p)  # 内容幂等 → 同 ref


def test_ref_differs_for_diff_content():
    s = DraftStore()
    r1 = s.put({"flow": {"nodes": []}, "dsl": "a"})
    r2 = s.put({"flow": {"nodes": []}, "dsl": "b"})
    assert r1 != r2


def test_ttl_expiry():
    s = DraftStore(ttl=0.0)
    ref = s.put({"flow": {}, "dsl": "z"})
    assert s.get(ref) is None  # 立即过期


def test_process_singleton_roundtrip():
    r = stage_draft({"flow": {"nodes": []}, "dsl": "hi"})
    assert get_draft(r)["dsl"] == "hi"


def test_error_carries_fix_and_candidates():
    e = AutoFlowError(ErrCode.NOT_FOUND, "x", fix=[{"op": "resolve", "entity": "y"}],
                      candidates=["a", "b"], hint="do")
    d = e.to_dict()
    assert d["code"] == "NOT_FOUND"
    assert d["fix"] == [{"op": "resolve", "entity": "y"}]
    assert d["candidates"] == ["a", "b"]
    assert d["hint"] == "do"


def test_compile_envelope_fix():
    class FakeE:
        code = "C_X"
        line = 3
        hint = "建议"
    env = _compile_error_envelope(FakeE(), fix=[{"op": "get_help"}], candidates=["c1"])
    assert env["fix"] == [{"op": "get_help"}]
    assert env["candidates"] == ["c1"]
    assert env["hint"] == "建议"


def test_fix_patch_factory():
    assert fix_patch("replace", path="p", value=1) == {"op": "replace", "path": "p", "value": 1}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa
            fail += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if fail else 0)
