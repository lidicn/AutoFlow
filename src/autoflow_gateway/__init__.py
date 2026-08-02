#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow Gateway — 让 agent 经统一网关在 HA 发现设备、在 NR 构建自动化流。"""
from .config import get_config, GatewayConfig
from .gateway import Gateway

__all__ = ["get_config", "GatewayConfig", "Gateway"]
__version__ = "0.1.0"
