#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BUG-3 双读元守卫：验证 AUTOFLOW_ 优先、AUTOFLLOW_ 回退、默认兜底。

审计报告 BUG-3：历史上 env 前缀不统一（AUTOFLOW_ / AUTOFLLOW_ 并存），
若运维按新约定设了 AUTOFLOW_X，旧代码只读 AUTOFLLOW_X 会静默忽略 → 安全缺口。
本守卫锁定双读语义，并同时覆盖 autoflow_gateway.envutil（6 个文件的共享实现）
与 lib/nr_client.py 的本地等价副本（nr_client 作为独立脚本被 node-red-kai-dai 技能直接运行）。
"""
import importlib.util
import os

import pytest

from autoflow_gateway.envutil import get_env as pkg_get_env

# nr_client 是独立脚本（lib/ 无 __init__），用 importlib 直接加载文件以测试其本地双读副本，
# 不依赖包导入机制。
_nr_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "autoflow_gateway", "lib", "nr_client.py")
)
_spec = importlib.util.spec_from_file_location("nr_client_standalone", _nr_path)
_nr_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nr_mod)
nr_get_env = _nr_mod.get_env

_DUAL_KEYS = ["AUTOFLOW_FOO", "AUTOFLLOW_FOO"]


@pytest.fixture
def env_clean():
    saved = {k: os.environ[k] for k in _DUAL_KEYS if k in os.environ}
    for k in _DUAL_KEYS:
        os.environ.pop(k, None)
    yield
    for k in _DUAL_KEYS:
        os.environ.pop(k, None)
    os.environ.update(saved)


@pytest.mark.parametrize("fn", [pkg_get_env, nr_get_env], ids=["pkg", "nr_client"])
def test_prefers_autoflow_prefix(fn, env_clean):
    """新约定 AUTOFLOW_ 优先于遗留 AUTOFLLOW_。"""
    os.environ["AUTOFLOW_FOO"] = "new"
    os.environ["AUTOFLLOW_FOO"] = "legacy"
    assert fn("AUTOFLOW_FOO", "def") == "new"


@pytest.mark.parametrize("fn", [pkg_get_env, nr_get_env], ids=["pkg", "nr_client"])
def test_falls_back_to_legacy(fn, env_clean):
    """未设 AUTOFLOW_ 时回退到 AUTOFLLOW_（既有部署不动）。"""
    os.environ["AUTOFLLOW_FOO"] = "legacy"
    assert fn("AUTOFLOW_FOO", "def") == "legacy"


@pytest.mark.parametrize("fn", [pkg_get_env, nr_get_env], ids=["pkg", "nr_client"])
def test_default_when_unset(fn, env_clean):
    """两者都未设时返回调用方默认值。"""
    assert fn("AUTOFLOW_FOO", "def") == "def"


def test_non_dual_name_single_read(env_clean):
    """非双轨前缀变量（如 HASS_SERVER）保持原单读语义，不被误伤。"""
    os.environ["HASS_SERVER"] = "http://example:8123"
    try:
        assert pkg_get_env("HASS_SERVER", "d") == "http://example:8123"
    finally:
        os.environ.pop("HASS_SERVER", None)
    assert pkg_get_env("HASS_SERVER", "d") == "d"


def test_legacy_name_argument_primary_is_passed_name(env_clean):
    """若调用方传 AUTOFLLOW_ 原名，则原名为主、AUTOFLOW_ 派生为回退（对称规则）。

    实践中所有调用点都已改为传 AUTOFLOW_ 名（见 grep 守卫），此用例仅文档化对称语义，
    防止未来维护者误解「primary = 传入名」这一核心约定。
    """
    os.environ["AUTOFLOW_FOO"] = "new"
    os.environ["AUTOFLLOW_FOO"] = "legacy"
    assert pkg_get_env("AUTOFLLOW_FOO", "def") == "legacy"
