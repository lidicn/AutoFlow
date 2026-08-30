"""WB93 专项：c4_replay_semantics 终裁确认。

决议（gateway.py:_replay_zero_policy docstring，2026-08-15 终裁）：G2 默认 fail_closed，
经 env AUTOFLOW_REPLAY_ZERO_POLICY=warn_only 可切换。本测试锁定该默认值与切换。
"""
import os, sys
import pytest

sys.path.insert(0, r"E:\NAS\autoflow\src")
from autoflow_gateway import gateway as G


class TestC4ReplayZeroPolicy:
    def test_default_is_fail_closed(self, monkeypatch):
        monkeypatch.delenv("AUTOFLOW_REPLAY_ZERO_POLICY", raising=False)
        assert G._replay_zero_policy() == "fail_closed"

    def test_warn_only_via_env(self, monkeypatch):
        monkeypatch.setenv("AUTOFLOW_REPLAY_ZERO_POLICY", "warn_only")
        assert G._replay_zero_policy() == "warn_only"

    def test_unknown_env_value_falls_back_to_fail_closed(self, monkeypatch):
        monkeypatch.setenv("AUTOFLOW_REPLAY_ZERO_POLICY", "bogus")
        assert G._replay_zero_policy() == "fail_closed"
