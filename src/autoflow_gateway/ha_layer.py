#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — HA 访问层

只暴露读操作 + 经确认闸的 call_service（写）。agent 永不直接拿到 HA 凭证。
读操作幂等、不改变设备状态；写操作一律进入确认闸。
"""
import os
import sys
from typing import List, Dict, Any, Optional

from .config import get_config


def _load_ha_client():
    """延迟导入 ha_client。优先用本包 vendored 副本（容器/本地自洽），
    其次环境变量 HA_CLIENT_PATH，最后 skills 目录。找不到返回 None（测试可注入 backend）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("HA_CLIENT_PATH")
    candidates = []
    if path:
        candidates.append(path)
    # vendored 副本优先（保证容器与本地一致，不依赖 skills 目录存在）
    candidates.append(os.path.join(here, "lib"))
    candidates.append(
        os.path.expanduser("~/.workbuddy/skills/homeassistant-kai-dai/scripts")
    )
    for c in candidates:
        if c and os.path.isdir(c):
            if c not in sys.path:
                sys.path.insert(0, c)
            try:
                import ha_client  # type: ignore
                return ha_client
            except Exception:
                continue
    return None


class HALayer:
    def __init__(self, config=None, backend=None):
        self.cfg = config or get_config()
        self._backend = backend  # 测试注入
        self._client = None
        self._client_rev = None

    @property
    def client(self):
        # 连接设置在 WebUI 改动后 cfg.connection_revision 会递增（#45）：
        # 代数变化即丢弃缓存 client，用新地址/令牌重建，免重启网关。注入的测试 backend 不受影响。
        if self._backend is None and self._client is not None:
            rev = getattr(self.cfg, "connection_revision", 0)
            if rev != self._client_rev:
                self._client = None
        if self._client is None:
            if self._backend is not None:
                self._client = self._backend
            else:
                hc = _load_ha_client()
                if hc is None:
                    raise RuntimeError(
                        "无法加载 ha_client（请检查 HA_CLIENT_PATH 或 skills 目录）。"
                    )
                self._client = hc.HAClient(
                    server=self.cfg.hass_server,
                    token=self.cfg.hass_token,
                    allow_write=True,
                )
                self._client_rev = getattr(self.cfg, "connection_revision", 0)
        return self._client

    # ── 读（开放）──
    def get_states(self, domain: Optional[str] = None) -> List[Dict]:
        return self.client.get_states(domain)

    def get_state(self, entity_id: str) -> Dict:
        return self.client.get_state(entity_id)

    def search_entities(self, keyword: str, domain: Optional[str] = None) -> List[Dict]:
        return self.client.search_entities(keyword, domain)

    def list_entities(self, domain: Optional[str] = None, area: Optional[str] = None) -> List[Dict]:
        return self.client.list_entities(domain, area)

    def get_areas(self) -> Dict[str, str]:
        return self.client.get_areas()

    def get_areas_http(self) -> Dict[str, str]:
        """兜底：GET /api/areas（虚拟 HA/vhass 支持；真实 HA 可能 404 返回 {}）。"""
        return self.client.get_areas_http()

    def domain_counts(self) -> Dict[str, int]:
        return self.client.domain_counts()

    def find_by_state(self, state_value: str, domain: Optional[str] = None) -> List[str]:
        return self.client.find_by_state(state_value, domain)

    # ── 写（经确认闸，由 gateway 调用）──
    def call_service(self, domain: str, service: str, service_data: Dict) -> Any:
        """实际执行 HA 服务调用（仅在确认批准后由 gateway 调用）。"""
        return self.client.call_service(domain, service, service_data)
