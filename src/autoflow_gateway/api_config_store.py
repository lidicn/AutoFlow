# -*- coding: utf-8 -*-
"""Link API 运行时配置存储（方案 B：独立 SQLite 表）。

持久化用户在 WebUI 填写的 Link API 配置（token / 坐标 / key），与网关既有
commands / decisions / proposals 同库（autoflow.db），复用 WAL + busy_timeout 约定。
真实密钥不进 git：api_configs 表落在 data/（gitignored），api_specs.json 只保留
<ENV_NAME> 占位符，安装时由本 store 的取值替换。

访问器：
- get_api_config(name) -> dict
- set_api_config(name, config_dict)
- delete_api_config(name) -> bool
- list_api_configs() -> dict[name -> config_dict]
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import get_config


class ApiConfigStore:
    """api_configs 表的读写访问器（与 proposals 同库同约定）。"""

    def __init__(self, config=None):
        # config 允许外部注入（测试用临时 data_dir）；缺省走全局 get_config()。
        self.cfg = config or get_config()
        self.db_path = os.path.join(self.cfg.data_dir, "autoflow.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            # WAL：读写互不阻塞（WebUI 读取不再被写锁拖卡）；
            # busy_timeout：偶发锁竞争自动重试而非立即报 database is locked。
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS api_configs (
                    spec_name   TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    updated_at  TEXT
                )"""
            )

    def get_api_config(self, name: str) -> Dict[str, Any]:
        """读取某 spec 的配置 dict；未配置返回 {}（不抛异常）。

        Args:
            name: spec 名称（对应 ApiSpec.name）。
        Returns:
            配置字典 {ENV_VAR: value}；不存在时返回空 dict。
        """
        row = self._conn.execute(
            "SELECT config_json FROM api_configs WHERE spec_name=?",
            (name,),
        ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["config_json"])
        except (ValueError, TypeError):
            return {}

    def set_api_config(self, name: str, config: Dict[str, Any]) -> None:
        """写入/更新某 spec 的配置（upsert by spec_name）。

        Args:
            name: spec 名称。
            config: 配置字典 {ENV_VAR: value}。
        Raises:
            ValueError: name 为空。
        """
        if not name:
            raise ValueError("spec_name 必填")
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO api_configs (spec_name, config_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(spec_name) DO UPDATE SET
                 config_json = excluded.config_json,
                 updated_at  = excluded.updated_at""",
            (name, json.dumps(config, ensure_ascii=False), now),
        )
        self._conn.commit()

    def delete_api_config(self, name: str) -> bool:
        """删除某 spec 的配置行（#182 Link API 删除）。

        幂等：不存在时返回 False 而非抛异常 —— 调用方（DELETE 端点）需要区分
        「真的删掉了配置」与「本来就没配过」，但两种情况都不该报错。

        Args:
            name: spec 名称（对应 ApiSpec.name）。
        Returns:
            True=确实删掉了一行；False=该 spec 本来就没有配置行。
        Raises:
            ValueError: name 为空。
        """
        if not name:
            raise ValueError("spec_name 必填")
        cur = self._conn.execute(
            "DELETE FROM api_configs WHERE spec_name=?", (name,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_api_configs(self) -> Dict[str, Dict[str, Any]]:
        """列出所有已配置项 -> {spec_name: config_dict}。"""
        rows = self._conn.execute(
            "SELECT spec_name, config_json FROM api_configs"
        ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                out[r["spec_name"]] = json.loads(r["config_json"])
            except (ValueError, TypeError):
                continue
        return out

    def close(self) -> None:
        """关闭底层连接（尽力而为，忽略异常）。"""
        try:
            self._conn.close()
        except Exception:
            pass
