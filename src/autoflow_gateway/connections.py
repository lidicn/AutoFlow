#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 连接设置（Home Assistant / Node-RED / Bark 推送）

背景（开源卫生 · #45）：这三类连接凭据全是**用户私有**的（HA 长期令牌、NR
登录密码、Bark 推送 key）。此前只能靠环境变量 / `.env` 注入，WebUI 里无处可填，
用户很容易把密钥硬编码进脚本、进而随仓库泄漏（本仓开源前的隐私审计正是被这一
点扎到）。本模块补齐「有地方填」这一环：

- **落盘**：`<data_dir>/<env>/connections.json`，只存用户在 WebUI 显式填写的
  字段。`data/` 已在 `.gitignore` 中，绝不入库。
- **生效**：进程启动时注入 `os.environ`（UI 值优先于 env/.env，语义单一，
  避免「我在界面改了却不生效」）；运行时保存后即时热更新——同步 GatewayConfig
  字段并递增 `connection_revision`，NR/HA 层据此重建 client，无需重启网关。
- **回显**：secret 类字段**永不明文回传**给前端，只回 `configured` + 掩码尾号。

本模块刻意**不 import config**（避免循环导入），`cfg` 一律由调用方传入。
"""
import os
import json
import logging
import threading
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ── 字段规格 ────────────────────────────────────────────────────────────────
# kind:
#   url    — 地址，明文回显
#   text   — 普通文本（用户名 / id），明文回显
#   secret — 令牌 / 密码 / 推送 key，只回掩码


@dataclass(frozen=True)
class FieldSpec:
    group: str
    key: str                      # 环境变量名（同时是持久化 key）
    label: str
    kind: str                     # url | text | secret
    cfg_attr: Optional[str] = None  # 对应 GatewayConfig 属性（无则仅走 env）
    placeholder: str = ""
    hint: str = ""


GROUP_SPECS: List[Dict[str, str]] = [
    {"id": "ha", "label": "Home Assistant",
     "desc": "网关读取实体状态、下发 call_service 的目标。令牌请用 HA 用户资料页生成的「长期访问令牌」。"},
    {"id": "nr", "label": "Node-RED",
     "desc": "自动化流程的落地实例，默认指向 1880；启用了登录鉴权才需要填用户名/密码。"},
    {"id": "bark", "label": "Bark 推送（可选）",
     "desc": "网关需要你人工审核时的手机通知通道。不填则相关推送静默跳过，不影响其它功能。"},
]

FIELD_SPECS: List[FieldSpec] = [
    # ── Home Assistant ──
    FieldSpec("ha", "HASS_SERVER", "HA 地址", "url", "hass_server",
              "http://127.0.0.1:8123",
              "容器内访问宿主 HA 请用 http://host.docker.internal:8123，不要写 localhost。"),
    FieldSpec("ha", "HASS_TOKEN", "长期访问令牌", "secret", "hass_token",
              "eyJhbGciOi…", "HA → 个人资料 → 安全 → 长期访问令牌。"),
    # ── Node-RED ──
    FieldSpec("nr", "NR_URL", "Node-RED 地址", "url", "nr_url",
              "http://127.0.0.1:1880", ""),
    FieldSpec("nr", "NR_USER", "用户名", "text", "nr_user", "", "未开启 adminAuth 时留空即可。"),
    FieldSpec("nr", "NR_PASS", "密码", "secret", "nr_pass", "", "未开启 adminAuth 时留空即可。"),
    FieldSpec("nr", "NR_HA_SERVER_ID", "HA server 节点 id", "text", "nr_ha_server_id", "",
              "Node-RED 里 home-assistant 配置节点的 id；留空则部署时保留占位符。"),
    # ── Bark ──
    FieldSpec("bark", "BARK_SERVER", "Bark 服务地址", "url", None,
              "https://api.day.app", "自建服务填自己的地址，官方服务填 https://api.day.app。"),
    FieldSpec("bark", "BARK_KEY", "设备 Key", "secret", None, "",
              "Bark App 首页那串设备 key，等同于你的推送地址凭据。"),
]

_SPEC_BY_KEY: Dict[str, FieldSpec] = {f.key: f for f in FIELD_SPECS}
_KEYS_BY_GROUP: Dict[str, List[str]] = {}
for _f in FIELD_SPECS:
    _KEYS_BY_GROUP.setdefault(_f.group, []).append(_f.key)

VALID_GROUPS = tuple(g["id"] for g in GROUP_SPECS)


# ── 持久化 ──────────────────────────────────────────────────────────────────
def connections_path(cfg) -> str:
    """连接设置落盘位置（按 env 隔离，与 feature_flags / deploy_policy 同级）。"""
    return os.path.join(cfg.data_dir, cfg.env, "connections.json")


def load_saved(cfg) -> Dict[str, str]:
    """读取用户经 WebUI 保存过的字段（缺文件/坏文件一律返回空 dict，fail-open）。"""
    p = connections_path(cfg)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            o = json.load(f)
    except Exception:
        return {}
    if not isinstance(o, dict):
        return {}
    return {k: str(v) for k, v in o.items() if k in _SPEC_BY_KEY and isinstance(v, (str, int))}


def _write_saved(cfg, values: Dict[str, str]) -> None:
    """原子落盘（先写临时文件再替换，避免半截文件）+ 尽力收紧权限。"""
    p = connections_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".connections-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(values, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # POSIX 生效；Windows 上是 no-op，不影响功能
        os.chmod(p, 0o600)
    except OSError:
        pass


# ── 生效（env 注入 + cfg 同步）────────────────────────────────────────────────
def _sync_cfg(cfg, key: str, value: str) -> None:
    spec = _SPEC_BY_KEY.get(key)
    if spec and spec.cfg_attr and hasattr(cfg, spec.cfg_attr):
        setattr(cfg, spec.cfg_attr, value)


def bump_revision(cfg) -> int:
    """递增连接代数——NR/HA 层看到代数变化即丢弃缓存 client，用新配置重建。

    用「代数」而不是直接去戳某个 Gateway 实例，是因为同进程内可能有多个
    Gateway（WebUI 一个、MCP 一个），而 cfg 是全局单例，代数放这里所有实例都能自愈。
    """
    rev = int(getattr(cfg, "connection_revision", 0)) + 1
    try:
        cfg.connection_revision = rev
    except Exception:
        pass
    return rev


def apply_saved_to_env(cfg) -> List[str]:
    """把已保存的连接设置注入进程环境 + 同步 cfg 字段。返回生效的 key 列表。

    进程启动时调用一次（见 config.get_config）。**UI 值覆盖 env/.env**：
    界面上改过就以界面为准，否则用户会遇到「改了没反应」的幽灵问题。
    """
    saved = load_saved(cfg)
    applied = []
    for k, v in saved.items():
        if v == "":
            continue
        os.environ[k] = v
        _sync_cfg(cfg, k, v)
        applied.append(k)
    return applied


# ── 回显 ────────────────────────────────────────────────────────────────────
def _mask(value: str) -> str:
    """secret 掩码：**一个字符都不露**。

    刻意不采用「露末 4 位」的常见做法——WebUI 会被截图、演示、投屏，而 Bark key /
    NR 密码这类短凭据露 4 位就少 4 位熵。要确认填没填对，看 `length` 就够了。
    """
    return "••••••••" if value else ""


def _effective(cfg, spec: FieldSpec, saved: Dict[str, str]) -> str:
    if spec.key in saved:
        return saved[spec.key]
    env_v = os.environ.get(spec.key)
    if env_v:
        return env_v
    if spec.cfg_attr:
        return str(getattr(cfg, spec.cfg_attr, "") or "")
    return ""


def _source(cfg, spec: FieldSpec, saved: Dict[str, str]) -> str:
    if spec.key in saved:
        return "ui"
    if os.environ.get(spec.key):
        return "env"
    if spec.cfg_attr and getattr(cfg, spec.cfg_attr, ""):
        return "default"
    return "unset"


def describe(cfg) -> Dict[str, Any]:
    """给 WebUI 的只读视图：分组 + 每字段当前值/掩码/来源。secret 绝不明文外传。"""
    saved = load_saved(cfg)
    groups = []
    for g in GROUP_SPECS:
        fields = []
        for key in _KEYS_BY_GROUP.get(g["id"], []):
            spec = _SPEC_BY_KEY[key]
            val = _effective(cfg, spec, saved)
            item: Dict[str, Any] = {
                "key": spec.key,
                "label": spec.label,
                "kind": spec.kind,
                "placeholder": spec.placeholder,
                "hint": spec.hint,
                "configured": bool(val),
                "source": _source(cfg, spec, saved),
            }
            if spec.kind == "secret":
                item["masked"] = _mask(val)
                item["length"] = len(val)   # 只给长度，够核对「填没填对」又不泄漏字符
            else:
                item["value"] = val
            fields.append(item)
        groups.append({"id": g["id"], "label": g["label"], "desc": g["desc"], "fields": fields})
    return {"groups": groups, "path": connections_path(cfg), "revision": int(getattr(cfg, "connection_revision", 0))}


# ── 写入 ────────────────────────────────────────────────────────────────────
def update(cfg, patch: Dict[str, Any]) -> Dict[str, Any]:
    """保存连接设置。

    patch 形如 ``{"ha": {"HASS_SERVER": "...", "HASS_TOKEN": "..."}, ...}``，
    也接受扁平的 ``{"HASS_SERVER": "..."}``。取值语义：

    - 字段**未出现** → 不改
    - ``null``       → 清除该字段（回退 env / 默认值）
    - ``""``（空串） → secret 视为「不改」（前端回显的是掩码，不该被空串覆盖掉）；
                       非 secret 视为清除
    - 其它字符串     → 写入

    返回 ``{"changed": [...], "cleared": [...], "revision": n}``；
    调用方拿到后应重新 describe 以刷新界面。校验失败抛 ValueError。
    """
    flat: Dict[str, Any] = {}
    for k, v in (patch or {}).items():
        if k in VALID_GROUPS and isinstance(v, dict):
            for kk, vv in v.items():
                flat[kk] = vv
        else:
            flat[k] = v

    unknown = [k for k in flat if k not in _SPEC_BY_KEY]
    if unknown:
        raise ValueError(f"未知的连接字段: {', '.join(sorted(unknown))}")

    saved = load_saved(cfg)
    changed: List[str] = []
    cleared: List[str] = []

    for key, raw in flat.items():
        spec = _SPEC_BY_KEY[key]
        if raw is None:
            if key in saved:
                saved.pop(key, None)
                cleared.append(key)
            continue
        if not isinstance(raw, (str, int)):
            raise ValueError(f"{spec.label}（{key}）必须是字符串")
        val = str(raw).strip()
        if val == "":
            if spec.kind == "secret":
                continue  # 掩码回显不覆盖已存密钥
            if key in saved:
                saved.pop(key, None)
                cleared.append(key)
            continue
        if spec.kind == "url" and not (val.startswith("http://") or val.startswith("https://")):
            raise ValueError(f"{spec.label}（{key}）必须以 http:// 或 https:// 开头")
        if "\n" in val or "\r" in val:
            raise ValueError(f"{spec.label}（{key}）不能包含换行")
        if saved.get(key) != val:
            saved[key] = val
            changed.append(key)

    if not changed and not cleared:
        return {"changed": [], "cleared": [], "revision": int(getattr(cfg, "connection_revision", 0))}

    _write_saved(cfg, saved)

    # 生效：新值注入 env + cfg；被清除的字段从 env 摘掉，回退到 .env/默认
    for key in changed:
        os.environ[key] = saved[key]
        _sync_cfg(cfg, key, saved[key])
    for key in cleared:
        os.environ.pop(key, None)
        spec = _SPEC_BY_KEY[key]
        if spec.cfg_attr:
            _sync_cfg(cfg, key, "")
    rev = bump_revision(cfg)
    return {"changed": changed, "cleared": cleared, "revision": rev}


# ── 连通性测试 ───────────────────────────────────────────────────────────────
def _http_probe(url: str, timeout: float = 5.0, headers: Optional[Dict[str, str]] = None,
                data: Optional[bytes] = None) -> Dict[str, Any]:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(512)
            return {"ok": True, "status": r.status, "body": body.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "http_error": True,
                "body": (e.read(256) or b"").decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _maybe_refresh_catalog(gateway) -> None:
    """HA 连接成功后后台拉取一次设备目录（非阻塞），稍后 resolve/list 只读缓存。"""
    def _pull():
        try:
            from .gateway import Gateway
            gw = gateway or Gateway()
            gw.refresh_catalog()
            try:
                total = len(gw.state.get_device_catalog().get("entities", {}))
                logging.getLogger(__name__).info("设备目录已刷新：共 %d 个设备", total)
            except Exception:
                pass
        except Exception as e:
            logging.getLogger(__name__).warning("设备目录后台刷新失败：%s", e)
    try:
        threading.Thread(target=_pull, daemon=True).start()
    except Exception:
        pass


def _test_ha(cfg, gateway=None) -> Dict[str, Any]:
    server = (os.environ.get("HASS_SERVER") or getattr(cfg, "hass_server", "") or "").rstrip("/")
    token = os.environ.get("HASS_TOKEN") or getattr(cfg, "hass_token", "") or ""
    if not server or "<" in server:
        return {"ok": False, "error": "未配置 HA 地址"}
    r = _http_probe(server + "/api/", headers={"Authorization": f"Bearer {token}"} if token else None)
    if r.get("ok"):
        # safe-gate-ui: 测试连接仅做连通性探针，不触发设备目录刷新；
        # 导入全屋设备目录由「安全闸 / 连接配置 → 导入全部设备」显式触发（与保存/测试解耦）。
        detail = ("已连接（令牌有效）。设备目录不会自动拉取，请到「安全闸」或「连接配置」点"
                  "「导入全部设备」以启用 autoflow_resolve_entity / autoflow_list_entities"
                  if token else "服务可达（未配令牌）")
        return {"ok": True, "status": r["status"], "detail": detail}
    if r.get("status") in (401, 403):
        return {"ok": False, "status": r["status"],
                "error": "服务可达，但令牌无效或缺失" if token else "服务可达，但需要长期访问令牌"}
    return {"ok": False, "error": r.get("error") or f"HTTP {r.get('status')}"}


def _test_nr(cfg, gateway=None) -> Dict[str, Any]:
    url = (os.environ.get("NR_URL") or getattr(cfg, "nr_url", "") or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "未配置 Node-RED 地址"}
    if gateway is not None:
        try:
            flows = gateway.nr.list_flows()
            return {"ok": True, "detail": f"已连接，读到 {len(flows)} 个顶层节点/流"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    r = _http_probe(url + "/settings")
    if r.get("ok"):
        return {"ok": True, "status": r["status"], "detail": "服务可达"}
    if r.get("status") in (401, 403):
        return {"ok": False, "status": r["status"], "error": "服务可达，但用户名/密码无效"}
    return {"ok": False, "error": r.get("error") or f"HTTP {r.get('status')}"}


def _test_bark(cfg, send: bool = False) -> Dict[str, Any]:
    server = (os.environ.get("BARK_SERVER") or "").rstrip("/")
    key = os.environ.get("BARK_KEY") or ""
    if not server or not key or "<" in server or "<" in key:
        return {"ok": False, "error": "未配置 Bark 服务地址或设备 Key（不影响其它功能，仅无法推送审核提醒）"}
    if not send:
        return {"ok": True, "detail": "配置完整（勾选「发送测试推送」可实测送达）", "sent": False}
    payload = json.dumps({"title": "AutoFlow", "body": "连接测试推送 ✅", "device_key": key}).encode("utf-8")
    r = _http_probe(server + "/push", headers={"Content-Type": "application/json"}, data=payload)
    if r.get("ok"):
        return {"ok": True, "detail": "测试推送已发送，请查看手机", "sent": True}
    return {"ok": False, "error": r.get("error") or f"HTTP {r.get('status')}", "sent": False}


def test_connections(cfg, targets: Optional[List[str]] = None, gateway=None,
                     send_bark: bool = False) -> Dict[str, Any]:
    """按目标做只读连通性探测（bark 默认只校验配置，send_bark=True 才真发）。"""
    targets = [t for t in (targets or list(VALID_GROUPS)) if t in VALID_GROUPS]
    out: Dict[str, Any] = {}
    for t in targets:
        if t == "ha":
            out["ha"] = _test_ha(cfg, gateway)
        elif t == "nr":
            out["nr"] = _test_nr(cfg, gateway)
        elif t == "bark":
            out["bark"] = _test_bark(cfg, send_bark)
    return out
