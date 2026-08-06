# -*- coding: utf-8 -*-
"""Node-RED admin API 极简客户端（REG 回归套件专用）。

为什么不直接用 ``lib/nr_client.py``：
    nr_client.login() 以 **JSON** 体请求 ``/auth/token``，而 Node-RED 的该端点是
    OAuth2 密码模式，只接受 **application/x-www-form-urlencoded**，故对启用了鉴权的
    实例（1990）必然返回 ``400 Missing username``。本模块只做表单登录 + 单 flow 增量
    读写，供回归套件自举使用；生产写入仍应走 nr_client 的护栏路径。

安全：凭据一律来自环境变量，**不得**在源码内写任何默认口令（`tests/test_no_secrets.py`
会扫描仓库）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class NRAdminError(RuntimeError):
    """Node-RED admin API 调用失败。"""


class NRAdmin:
    """Node-RED admin REST 客户端（表单鉴权 + 单 flow 增量更新）。"""

    def __init__(self, url: str | None = None, user: str | None = None,
                 password: str | None = None, timeout: int = 30) -> None:
        """初始化客户端。

        Args:
            url: Node-RED 基地址，缺省读环境变量 ``NR_URL``。
            user: 用户名，缺省读 ``NR_USER``。
            password: 口令，缺省读 ``NR_PASS``。
            timeout: 单次请求超时（秒）。

        Raises:
            NRAdminError: 未提供 URL 或凭据不全。
        """
        self.url = (url or os.getenv("NR_URL") or "").rstrip("/")
        self.user = user or os.getenv("NR_USER") or ""
        self.password = password or os.getenv("NR_PASS") or ""
        self.timeout = timeout
        self._token: str | None = None
        if not self.url:
            raise NRAdminError("缺少 NR_URL（形如 http://host:1990）")

    # ── 鉴权 ──────────────────────────────────────────────────────────
    def login(self) -> str:
        """表单方式换取 bearer token；实例未开鉴权时返回空串。

        Returns:
            access_token 字符串；无鉴权实例返回 ""。

        Raises:
            NRAdminError: 鉴权被拒或凭据缺失。
        """
        if self._token is not None:
            return self._token
        try:
            probe = urllib.request.urlopen(self.url + "/auth/login", timeout=self.timeout)
            scheme = json.loads(probe.read() or b"{}")
        except urllib.error.URLError as exc:  # pragma: no cover - 网络态
            raise NRAdminError("无法连接 %s: %s" % (self.url, exc)) from exc
        if not scheme:  # {} 表示未启用鉴权
            self._token = ""
            return self._token
        if not (self.user and self.password):
            raise NRAdminError("实例启用了鉴权，请设置环境变量 NR_USER / NR_PASS")
        body = urllib.parse.urlencode({
            "client_id": "node-red-admin",
            "grant_type": "password",
            "scope": "*",
            "username": self.user,
            "password": self.password,
        }).encode()
        req = urllib.request.Request(
            self.url + "/auth/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._token = json.loads(resp.read())["access_token"]
        except urllib.error.HTTPError as exc:
            raise NRAdminError("鉴权失败 HTTP %s: %s"
                               % (exc.code, exc.read().decode("utf-8", "replace"))) from exc
        return self._token or ""

    # ── 底层请求 ──────────────────────────────────────────────────────
    def _request(self, method: str, path: str, payload: Any = None) -> Any:
        """发起一次 admin API 请求。

        Args:
            method: HTTP 方法。
            path: 以 ``/`` 开头的路径。
            payload: 可选 JSON 体。

        Returns:
            解析后的 JSON（响应体为空时返回 None）。

        Raises:
            NRAdminError: 非 2xx 响应。
        """
        token = self.login()
        headers = {"Node-RED-API-Version": "v2"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise NRAdminError("%s %s → HTTP %s: %s"
                               % (method, path, exc.code,
                                  exc.read().decode("utf-8", "replace")[:400])) from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    # ── 业务方法 ──────────────────────────────────────────────────────
    def get_flows(self) -> list[dict]:
        """拉取全部节点（含 tab / subflow 定义）。"""
        data = self._request("GET", "/flows")
        return data["flows"] if isinstance(data, dict) else data

    def get_flow(self, flow_id: str) -> dict:
        """按 tab id 拉取单个 flow。

        Raises:
            NRAdminError: tab 不存在（HTTP 404）。
        """
        return self._request("GET", "/flow/" + flow_id)

    def create_flow(self, flow: dict) -> str:
        """新建 tab。

        ⚠️ Node-RED 会**自行分配 tab id 并忽略 body 里的 id**，故必须以返回值为准
        登记台账，否则下次探测恒 404 → 重复建 tab。

        Returns:
            Node-RED 实际分配的 tab id。
        """
        res = self._request("POST", "/flow", flow)
        return (res or {}).get("id", "")

    def update_flow(self, flow_id: str, flow: dict) -> None:
        """整体替换单个 tab 的节点集（只影响该 tab，不触碰其它 tab）。"""
        self._request("PUT", "/flow/" + flow_id, flow)

    def find_tab_by_label(self, label: str) -> str:
        """按 label 查找 tab id；找不到返回空串。"""
        for node in self.get_flows():
            if node.get("type") == "tab" and node.get("label") == label:
                return node["id"]
        return ""

    def inject(self, node_id: str) -> None:
        """触发一个 inject 节点。"""
        self._request("POST", "/inject/" + node_id)
