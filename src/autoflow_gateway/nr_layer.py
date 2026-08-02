#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — NR 访问层

只暴露细粒度、单 flow 的安全写操作。结构上**绝不暴露** deploy_all / replace-all。
所有写经由 gateway 的防御层 + 确认闸。提供 diff 预览。
"""
import os
import sys
from typing import List, Dict, Any, Optional

from .config import get_config


def _load_nr_client():
    """延迟导入 nr_client。优先 vendored 副本，其次 NR_CLIENT_PATH，最后 skills 目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("NR_CLIENT_PATH")
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(here, "lib"))
    candidates.append(os.path.expanduser("~/.workbuddy/skills/node-red-Kai-Dai/scripts"))
    for c in candidates:
        if c and os.path.isdir(c):
            if c not in sys.path:
                sys.path.insert(0, c)
            try:
                import nr_client  # type: ignore
                return nr_client
            except Exception:
                continue
    return None


class NRLayer:
    def __init__(self, config=None, backend=None):
        self.cfg = config or get_config()
        self._backend = backend
        self._client = None
        self._client_rev = None

    @property
    def client(self):
        # 连接设置在 WebUI 改动后 cfg.connection_revision 会递增（#45）：
        # 代数变化即丢弃缓存 client，用新凭据重建，免重启网关。注入的测试 backend 不受影响。
        if self._backend is None and self._client is not None:
            rev = getattr(self.cfg, "connection_revision", 0)
            if rev != self._client_rev:
                self._client = None
        if self._client is None:
            if self._backend is not None:
                self._client = self._backend
            else:
                nc = _load_nr_client()
                if nc is None:
                    raise RuntimeError("无法加载 nr_client。")
                self._client = nc.NodeRedClient(
                    url=self.cfg.nr_url, username=self.cfg.nr_user, password=self.cfg.nr_pass
                )
                self._client_rev = getattr(self.cfg, "connection_revision", 0)
        return self._client

    # ── 读（开放）──
    def list_flows(self) -> List[Dict]:
        return self.client.list_flows()

    def get_flow(self, flow_id: str) -> Dict:
        return self.client.get_flow(flow_id)

    def find_flow_by_name(self, name: str) -> Optional[Dict]:
        return self.client.find_flow_by_name(name)

    # ── 安全写（gateway 在调用前已完成防御+确认）──
    def update_flow_nodes(self, flow_id: str, flow_data: Dict, force: bool = False) -> Dict:
        """单 flow PUT 更新（1880 实例可用路径）。不暴露 deploy_all。"""
        return self.client.update_flow(flow_id, flow_data, force=force)

    def create_or_update_flow(self, flow_id: str, flow_data: Dict, force: bool = False,
                              allow_prod: bool = False) -> Dict:
        """创建或更新 flow（部署新场景时先用此入口）。

        不存在则 POST /flow 创建；已存在则 PUT /flow/:id 更新。
        返回 {'id', 'created', 'raw'} —— 调用方应以返回的真实 id 登记 flow_catalog。
        allow_prod：透传 prod 写授权（人手动部署默认 True，agent 部署默认受守卫保护）。
        """
        return self.client.create_or_update_flow(flow_id, flow_data, force=force,
                                                 allow_prod=allow_prod)

    def add_nodes(self, flow_id: str, new_nodes: List[Dict]) -> Dict:
        return self.client.add_nodes(flow_id, new_nodes)

    def create_subflow(self, subflow_id: str, name: str,
                      in_ports: List[Dict], out_ports: List[Dict],
                      nodes: List[Dict], info: str = "",
                      category: str = "subflows", env: List[Dict] = None,
                      allow_prod: bool = False) -> Dict:
        """原子创建 NR 子流程（def + 内部节点），经权威 nr_client 的【增量 append】路径，
        不整实例替换、不触发 drop 熔断。仅由 deploy_proposal 的子流程注册分支调用（已通过人审）。

        allow_prod=False 与 seed_managed_subflows 一致（仅写 staging 实例，prod 环境需显式 allow_prod）。
        """
        return self.client.create_subflow(
            subflow_id, name, in_ports, out_ports, nodes,
            info=info, category=category, env=env, allow_prod=allow_prod)

    def modify_node_field(self, flow_id: str, node_id: str, fields: Dict) -> Dict:
        return self.client.modify_node_field(flow_id, node_id, fields)

    def modify_function_code(self, flow_id: str, node_id: str, code: str, name: str = None) -> Dict:
        return self.client.modify_function_code(flow_id, node_id, code, name=name)

    def delete_flow(self, flow_id: str, force: bool = False) -> Dict:
        """谨慎删除（gateway 已确认非受保护 + 经批准）。force 透传给底层 nr_client。"""
        return self.client.delete_flow(flow_id, force=force)

    def put_flow_raw(self, flow_id: str, flow_data: Dict) -> Dict:
        """直写 PUT /flow/:id（绕过 _normalize_flow/护栏）。

        仅经人审确认闸的『切 tab.disabled』场景使用（set_tab_state_execute），
        须原样回写节点内容（AC9 节点字节不变）。其余写路径走 update_flow_nodes
        / create_or_update_flow，勿调此方法。
        """
        return self.client.put_flow_raw(flow_id, flow_data)

    # ── 端到端执行追踪（E2E）透传 ──
    def inject_flow(self, flow_id: str) -> None:
        """触发 flow 中所有 inject 节点（P5 E2E 用）。"""
        return self.client.inject_flow(flow_id)

    def trigger_inject(self, node_id: str) -> int:
        """真实触发单个 inject 节点（P5 E2E 验证用）。返回 HTTP 状态码。"""
        return self.client.trigger_inject(node_id)

    def get_context(self, store: str, key: str) -> Any:
        """读取 NR context store（P5 E2E 读回 flow/global 上的 trace）。"""
        return self.client.get_context(store, key)

    def set_context(self, store: str, key: str, value: Any):
        """清理 NR context store（P5 E2E 用完清 trace）。

        注意：NR Admin API 无 POST 写端点；value 为空时底层走 DELETE。
        """
        return self.client.set_context(store, key, value)

    def delete_context(self, store: str, key: str):
        """删除 NR context key（P5 E2E 清理 trace，DELETE → 204）。"""
        return self.client.delete_context(store, key)

    def get_default_server_id(self) -> str:
        """返回 NR 中第一个 HA server 节点 id。"""
        return self.client.get_default_server_id()

    def validate_flow(self, flow_data: Dict) -> List[str]:
        return self.client.validate_flow(flow_data)

    def dump_all_flows(self, outdir: str) -> int:
        return self.client.dump_all_flows(outdir)

    # ── 节点构建器透传（供 build_scene 使用，保持 v6/v3 schema 正确）──
    def build(self, node_type: str, *a, **kw):
        fn = getattr(self.client, f"build_{node_type}", None)
        if fn is None:
            raise ValueError(f"未知节点类型: {node_type}")
        return fn(*a, **kw)
