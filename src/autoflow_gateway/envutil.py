#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境变量双读工具 —— 审计报告 BUG-3 修复。

历史上网关的 env 前缀不统一：既用 ``AUTOFLOW_`` 也用 ``AUTOFLLOW_``（审计报告 #14 / BUG-3）。
两套前缀在活代码中都被真实读取，NAS/HA 部署历史按 ``AUTOFLLOW_`` 配置。

问题：若运维按新约定设了 ``AUTOFLOW_X``，旧代码只读 ``AUTOFLLOW_X`` 会静默忽略该值，
形成安全缺口（例如 prod 护栏 / LLM 接入被错误的默认值覆盖，却毫无报错）。

本模块提供 ``get_env``：优先读取 ``AUTOFLOW_`` 前缀（新约定），回退 ``AUTOFLLOW_`` 前缀
（遗留），最后用调用方默认值。两种前缀都生效且新前缀优先，零破坏既有 ``AUTOFLLOW_`` 部署。

用法：把原来的 ``os.environ.get("AUTOFLLOW_X", default)`` 整体替换为
``get_env("AUTOFLOW_X", default)`` 即可；``get_env`` 会自动派生 ``AUTOFLLOW_X`` 作为回退。
"""
import os


def get_env(name: str, default=None):
    """双读环境变量，AUTOFLOW_ 优先，AUTOFLLOW_ 回退。

    ``name`` 必须以 ``AUTOFLOW_`` 或 ``AUTOFLLOW_`` 开头；函数自动推导另一套前缀作为回退。
    例如 ``get_env("AUTOFLOW_ENV", "staging")`` 会先读 ``AUTOFLOW_ENV``，再读 ``AUTOFLLOW_ENV``，
    都没有才返回 ``"staging"``。

    返回规则：
    - 主前缀（name 本身）命中且非 None → 返回值；
    - 否则回退前缀命中且非 None → 返回值；
    - 否则返回 default。

    非双轨前缀变量（如 ``HASS_SERVER``）直接单读，保持原语义，避免误伤。
    """
    if name.startswith("AUTOFLOW_"):
        fallback = "AUTOFLLOW_" + name[len("AUTOFLOW_"):]
    elif name.startswith("AUTOFLLOW_"):
        fallback = "AUTOFLOW_" + name[len("AUTOFLLOW_"):]
    else:
        return os.environ.get(name, default)
    primary = os.environ.get(name)
    if primary is not None:
        return primary
    return os.environ.get(fallback, default)
