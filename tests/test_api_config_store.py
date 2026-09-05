# -*- coding: utf-8 -*-
"""A0（#166）api_config_store 单元测试：表存在、get/set/list 往返、upsert 覆盖。

不依赖真机 / 真网关配置：用临时 data_dir 注入 ApiConfigStore，避免触碰 autoflow.db。
"""
import sqlite3
from types import SimpleNamespace

from autoflow_gateway.api_config_store import ApiConfigStore


def _store(tmp_path):
    # 只用到 cfg.data_dir，用最小桩对象注入，不触发全局 get_config()。
    return ApiConfigStore(config=SimpleNamespace(data_dir=str(tmp_path)))


def test_table_created(tmp_path):
    store = _store(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "autoflow.db"))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        assert "api_configs" in tables
    finally:
        conn.close()
    store.close()


def test_get_missing_returns_empty(tmp_path):
    store = _store(tmp_path)
    assert store.get_api_config("llm_caiyun_weather") == {}


def test_set_then_get_roundtrip(tmp_path):
    store = _store(tmp_path)
    cfg = {"CAIYUN_TOKEN": "Y2FpeXVu...", "CAIYUN_LON": "113.869565",
           "CAIYUN_LAT": "22.666851"}
    store.set_api_config("llm_caiyun_weather", cfg)
    assert store.get_api_config("llm_caiyun_weather") == cfg


def test_upsert_overwrites(tmp_path):
    store = _store(tmp_path)
    store.set_api_config("anysearch_batch", {"ANYSEARCH_API_KEY": "as_sk_a"})
    store.set_api_config("anysearch_batch", {"ANYSEARCH_API_KEY": "as_sk_b"})
    assert store.get_api_config("anysearch_batch") == {"ANYSEARCH_API_KEY": "as_sk_b"}


def test_list_api_configs(tmp_path):
    store = _store(tmp_path)
    store.set_api_config("llm_caiyun_weather", {"CAIYUN_TOKEN": "x"})
    store.set_api_config("anysearch_batch", {"ANYSEARCH_API_KEY": "y"})
    allcfg = store.list_api_configs()
    assert set(allcfg.keys()) == {"llm_caiyun_weather", "anysearch_batch"}
    assert allcfg["llm_caiyun_weather"] == {"CAIYUN_TOKEN": "x"}


def test_set_empty_name_raises(tmp_path):
    store = _store(tmp_path)
    try:
        import pytest
        with pytest.raises(ValueError):
            store.set_api_config("", {"X": "1"})
    finally:
        store.close()
