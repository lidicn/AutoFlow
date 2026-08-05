#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 配置层

集中所有可调参数（环境变量优先）。网关暴露给 agent 的是「受控操作」，
所有凭证只在网关进程内使用，agent 永不直连 HA/NR。
"""
import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set, Dict, Any


def _load_local_env():
    """读取仓库根/.env 注入环境变量（python-dotenv 缺失时也能用）。

    不覆盖已存在的环境变量（含 shell 导出的）；忽略注释(#)与空行；
    支持 ``key=value`` 与引号包裹的值。
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))  # .../src/autoflow_gateway
        candidates = [
            os.path.join(here, "..", "..", ".env"),       # autoflow_gateway/.env
            os.path.join(here, "..", "..", "..", ".env"),  # 仓库根/.env
            os.path.join(os.getcwd(), ".env"),
        ]
        env_path = next((p for p in candidates if os.path.isfile(p)), None)
        if not env_path:
            return
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


# 优先用 python-dotenv（若存在），再用自实现兜底，确保 .env 一定生效
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
_load_local_env()


@dataclass
class GatewayConfig:
    # ── 运行时数据目录（共享态 / 待确认 / 备份 持久化）──
    data_dir: str = field(default_factory=lambda: os.environ.get(
        "AUTOFLLOW_DATA_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "data")
    ))

    # ── skill 指导文档目录（autoflow_get_skill 只读返回，供 agent 自愈陈旧的 skill 指导）──
    skills_dir: str = field(default_factory=lambda: os.environ.get(
        "AUTOFLOW_SKILLS_DIR",
        str(Path(__file__).resolve().parents[2] / "skills")
    ))

    # ── 环境分级：staging / prod（仅用于 data/ 子目录隔离，避免两套状态混淆）──
    env: str = field(default_factory=lambda: os.environ.get("AUTOFLLOW_ENV", "staging"))

    # ── HA 连接（网关独占，agent 不可见）──
    hass_server: str = field(default_factory=lambda: os.environ.get("HASS_SERVER", "http://<NAS_IP>:8123"))
    hass_token: str = field(default_factory=lambda: os.environ.get("HASS_TOKEN", ""))

    # ── NR 连接（网关独占）。默认 http://localhost:1880，可用 NR_URL 覆盖 ──
    nr_url: str = field(default_factory=lambda: os.environ.get("NR_URL", "http://localhost:1880"))
    nr_user: str = field(default_factory=lambda: os.environ.get("NR_USER", ""))
    nr_pass: str = field(default_factory=lambda: os.environ.get("NR_PASS", ""))
    # NR 中配置的 Home Assistant server id（server-state-changed 节点绑定用）。
    # 部署 DSL 提案时把 dsl_engine 的 HA_SERVER_ID 占位符替换为该值；为空则保留占位符。
    nr_ha_server_id: str = field(default_factory=lambda: os.environ.get("NR_HA_SERVER_ID", ""))
    # ── debug 回读桥（#644）：后台线程旁路订阅 NR5.0.1 原生 ws://<nr>/comms debug 事件流 ──
    # 默认开启；设 0 关闭（纯增量只读功能，fail-open，不影响任何热路径）。
    # 缓冲容量/TTL 等内部调参走 debug_bridge.py 内的 AUTOFLLOW_DEBUG_* env，不动此处。
    debug_bridge_enabled: bool = field(default_factory=lambda: os.environ.get("AUTOFLLOW_DEBUG_BRIDGE", "1").lower() in ("1", "true", "yes"))

    # ── ds_bridge 地址（golden 评测『点燃』chrome deepseek++ 用）──
    # 桌面机 <DESKTOP_LAN_IP> 永久跑 ds_bridge 控 Chrome；网关现同机用 localhost:9090，
    # 未来迁 NAS 改 http://<DESKTOP_LAN_IP>:9090 即可，业务代码零改动。
    ds_bridge_url: str = field(default_factory=lambda: os.environ.get("DS_BRIDGE_URL", "http://localhost:9090"))

    # ── 防御层默认值 ──
    blast_radius_max_flows: int = field(default_factory=lambda: int(os.environ.get("AF_BLAST_RADIUS", "1")))
    protected_flow_labels: Set[str] = field(default_factory=lambda: set(
        s for s in os.environ.get("AF_PROTECTED_LABELS", "core,system,AutoFlow").split(",") if s
    ))
    # 受保护 flow id 前缀（核心流 agent 永不能动）
    protected_flow_id_prefixes: Set[str] = field(default_factory=lambda: set(
        s for s in os.environ.get("AF_PROTECTED_ID_PREFIXES", "core_").split(",") if s
    ))
    # 默认允许写的安全 domain（HA call_service 白名单）
    safe_domains: Set[str] = field(default_factory=lambda: set(
        s for s in os.environ.get("AF_SAFE_DOMAINS", "light,switch,script,scene,notify,input_boolean,input_number,input_select,automation,cover,fan,climate").split(",") if s
    ))
    # 高危 domain：必须升级确认级别（锁/水阀/加热等现实危害）
    elevated_domains: Set[str] = field(default_factory=lambda: set(
        s for s in os.environ.get("AF_ELEVATED_DOMAINS", "lock,valve,water_heater,alarm_control_panel,garage_door").split(",") if s
    ))

    # ── 确认闸：写必人工确认（默认零信任）──
    auto_approve_low_risk: bool = field(default_factory=lambda: os.environ.get("AF_AUTO_APPROVE", "false").lower() in ("1", "true", "yes"))
    # 单 agent 待确认上限（熔断/速率）
    max_pending_per_agent: int = field(default_factory=lambda: int(os.environ.get("AF_MAX_PENDING", "20")))
    # ── 部署策略雏形（按提案 source 分流）──
    # review_all   : 所有提案（含编译器产物）都需人类在 WebUI 审核后部署（默认，行为不变）。
    # compiler_auto: 来源=compiler 且闸门通过的提案可自动部署；来源=raw 永远需人审。
    deploy_policy: str = field(default_factory=lambda: os.environ.get("AUTOFLOW_DEPLOY_POLICY", "review_all"))

    # ── MCP 服务 ──
    # C13 网络硬化: 默认 127.0.0.1 (暴露需显式 AF_MCP_HOST=0.0.0.0 才对外 + 启动打印警告)
    mcp_host: str = field(default_factory=lambda: os.environ.get("AF_MCP_HOST", "127.0.0.1"))
    mcp_port: int = field(default_factory=lambda: int(os.environ.get("AF_MCP_PORT", "8000")))
    mcp_path: str = field(default_factory=lambda: os.environ.get("AF_MCP_PATH", "/mcp"))
    mcp_white_path: str = field(default_factory=lambda: os.environ.get("AF_MCP_WHITE_PATH", "/mcp-white"))
    mcp_admin_path: str = field(default_factory=lambda: os.environ.get("AF_MCP_ADMIN_PATH", "/mcp-admin"))

    # ── 连接设置代数（#45）──
    # WebUI 保存 HA/NR/Bark 连接后由 connections.bump_revision 递增；
    # NR/HA 层在 client property 里比对代数，变了就丢弃缓存 client 用新凭据重建，
    # 从而实现「界面改完即时生效、无需重启网关」。
    connection_revision: int = 0

    def env_subdir(self) -> str:
        """共享态按环境隔离，避免 staging/prod 混淆。"""
        return self.env

    def feature_flags_path(self) -> str:
        """特性开关落盘位置（按环境隔离，运行时可改、无需重启网关）。"""
        return os.path.join(self.data_dir, self.env, "feature_flags.json")

    def deploy_policy_path(self) -> str:
        """部署策略落盘位置（按环境隔离，运行时可改、无需重启网关）。"""
        return os.path.join(self.data_dir, self.env, "deploy_policy.json")

    def make_dirs(self):
        for sub in ("state", "pending", "backup", "experience"):
            os.makedirs(os.path.join(self.data_dir, self.env, sub), exist_ok=True)


# 全局单例（进程内共享）
_CONFIG = None


def get_config() -> GatewayConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = GatewayConfig()
        _CONFIG.make_dirs()
        # WebUI 里保存过的连接设置（HA/NR/Bark）注入进程环境并同步字段。
        # 延迟 import 避免 config <-> connections 循环；失败一律忽略（fail-open，
        # 连接设置只是便利层，缺了仍可用 env/.env 跑）。
        try:
            from .connections import apply_saved_to_env
            apply_saved_to_env(_CONFIG)
        except Exception:
            pass
    return _CONFIG


def reset_config():
    global _CONFIG
    _CONFIG = None


# ── 特性开关（运行时由 WebUI 落盘，免重启读取）──
def load_feature_flags(cfg: "GatewayConfig") -> Dict[str, Any]:
    """读取特性开关（实时读文件，缺省返回空 dict 表示全部走默认值）。"""
    p = cfg.feature_flags_path()
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                o = json.load(f)
                if isinstance(o, dict):
                    return o
        except Exception:
            pass
    return {}


def is_task_pool_enabled(cfg: "GatewayConfig") -> bool:
    """DSL 验证任务池是否启用（默认启用；开关置 false 即关闭）。"""
    return bool(load_feature_flags(cfg).get("task_pool_enabled", True))


def is_raw_node_escape_enabled(cfg: "GatewayConfig") -> bool:
    """原生节点逃逸（Phase 4，中风险）是否启用。

    默认**关闭**：这是绕过 DSL 编译器、直接嵌入手写 NR 节点的逃生舱，
    安全面比纯 DSL 大，故默认关，由 WebUI 开关主动开启、可随时关闭。"""
    return bool(load_feature_flags(cfg).get("raw_node_escape_enabled", False))


def is_submit_gate_enabled(cfg: "GatewayConfig") -> bool:
    """任务池提交（autoflow_submit_result）时是否跑 staging 闸门（branch-aware vhass 重放断言）。

    默认**关闭**：与现状一致，提交时仅做 解析→编译→lint(+requires_branch 加固)。
    置 True 后，每次提交会额外跑 run_staging_gate —— 缺分支/逻辑错误的浅版 DSL
    会被判 gate_fail 拦截（防蒸馏污染），正确版得 gate_pass。
    运行时经 feature_flags.json 切换，免重启。"""
    return bool(load_feature_flags(cfg).get("submit_run_gate", False))


def set_feature_flag(cfg: "GatewayConfig", key: str, value: Any) -> Dict[str, Any]:
    """写入单个特性开关并落盘，返回更新后的完整 flags。"""
    p = cfg.feature_flags_path()
    flags = load_feature_flags(cfg)
    flags[key] = value
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(flags, f, ensure_ascii=False, indent=2)
    return flags


# ── 部署策略（运行时由 WebUI 落盘，免重启读取）──
# review_all   : 所有提案（含编译器产物）都需人类在 WebUI 审核后部署（默认）。
# compiler_auto: 来源=compiler 且闸门通过的提案标「可信」可自动部署；
#               来源=raw 永远需人审。未知值一律 fail-safe 回退需人审。
_VALID_DEPLOY_POLICIES = ("review_all", "compiler_auto")


def get_deploy_policy(cfg: "GatewayConfig") -> str:
    """读取当前部署策略：优先运行时文件，回退 env 配置，再回退默认 review_all。"""
    p = cfg.deploy_policy_path()
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                o = json.load(f)
                if isinstance(o, dict) and o.get("deploy_policy") in _VALID_DEPLOY_POLICIES:
                    return o["deploy_policy"]
        except Exception:
            pass
    return getattr(cfg, "deploy_policy", "review_all")


def set_deploy_policy(cfg: "GatewayConfig", policy: str) -> str:
    """写入部署策略（仅接受白名单值，未知值抛 ValueError 触发 fail-safe 拒绝）。"""
    if policy not in _VALID_DEPLOY_POLICIES:
        raise ValueError(f"未知部署策略: {policy!r}（允许: {', '.join(_VALID_DEPLOY_POLICIES)}）")
    p = cfg.deploy_policy_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"deploy_policy": policy}, f, ensure_ascii=False, indent=2)
    return policy
