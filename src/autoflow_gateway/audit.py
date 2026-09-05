#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计日志（C21）。

开放源码 MVP 在去人审（PIN 模型）后，审计日志是唯一可追溯性来源。
当前复用 gateway 的进程内结构化 trace 缓冲（_write_apply_trace 写入的 apply/audit 记录），
本模块提供统一读取入口；后续若需长周期落盘审计，可在此扩展（与 D11 备份体系对齐）。
"""
from typing import Any, Dict, List


class AuditStore:
    def __init__(self, gateway):
        self.gw = gateway

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            return self.gw.get_recent_traces(limit)
        except Exception:
            return []
