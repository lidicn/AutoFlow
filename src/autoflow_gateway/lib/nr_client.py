#!/usr/bin/env python3
"""
Node-RED Admin API 客户端
认证: POST /auth/token → JWT Bearer token

核心特性:
  - 安全守卫: 节点数量保护，防止误删
  - diff 预览: 修改前预览差异
  - 操作日志: 所有写入操作记录到 ~/.qclaw/logs/nr_operations.log

用法:
  from nr_client import NodeRedClient
  nr = NodeRedClient()

常用操作:
  nr.list_flows()              # 列出所有 flows（含节点数）
  nr.get_flow("id")            # 获取单个 flow（缓存节点数）
  nr.modify_function_code(...) # 修改 function 代码并部署
  nr.fix_api_current_state_nodes("id")  # 批量修复 api-current-state（NR 5.0 兼容）
"""

import os, sys, json, uuid, re, shutil, time
import urllib.request
import urllib.error
from urllib.parse import quote
from typing import Optional, Dict, List, Any, Callable
from datetime import datetime

# ── 配置 ─────────────────────────────────────────────

# 默认实例 = 1880。日常测试与编写都在 1880；prod 环境写操作需 allow_prod opt-in。
# 需要操作 1880 时显式设 NR_URL=http://<NAS_IP>:1880（或按用户发来的 URL 端口为准）。
NR_URL    = os.getenv("NR_URL",    "http://localhost:1880")
NR_USER   = os.getenv("NR_USER",   "")
# ★S-2 安全修复：占位符凭据默认值改为空字符串，未配置时不再发送假密码登录 NR
NR_PASS   = os.getenv("NR_PASS",   "")

# 日志目录（WorkBuddy 用户空间）
LOG_DIR = os.path.expanduser("~/.workbuddy/logs")
LOG_FILE = os.path.join(LOG_DIR, "nr_operations.log")

# ── 日志工具 ─────────────────────────────────────────

def _log_operation(action: str, details: str):
    """记录操作日志"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} | {action} | {details}\n")
    except Exception:
        pass  # 日志失败不影响主流程


# ── 权威源 / 版本 / 自动同步 ──────────────────────────
# 单一权威源：以本文件（autoflow lib fork）为准。其他副本 import 时自动对比版本并拉取最新。
# 覆盖权威源位置：NR_CLIENT_AUTHORITY=<绝对路径>
# 关闭自动同步：NR_CLIENT_DISABLE_AUTOSYNC=1

NR_CLIENT_VERSION = "2.1.7"

# 默认权威源位置（可被 NR_CLIENT_AUTHORITY 环境变量或运行时注册表覆盖）。
# 指向本文件自身（vendored 副本即权威源），不再硬编码个人路径。
NR_CLIENT_AUTHORITY_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nr_client.py")

# 运行期注册表：权威源被 import 时登记自身路径+版本，供其他副本解析。
NR_CLIENT_REGISTRY = os.path.join(os.path.expanduser("~"), ".workbuddy", "nr_client_authority.json")


def _read_version_from_file(path: str) -> str:
    """从文件中解析 NR_CLIENT_VERSION 常量（不 import，避免副作用）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
        m = re.search(r'NR_CLIENT_VERSION\s*=\s*["\']([^"\']+)["\']', txt)
        return m.group(1) if m else "0.0.0"
    except Exception:
        return "0.0.0"


def _parse_version(v) -> tuple:
    parts = []
    for x in re.split(r"[.\-+]", str(v).lstrip("vV")):
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _version_lt(a, b) -> bool:
    return _parse_version(a) < _parse_version(b)


def _resolve_authoritative() -> Optional[str]:
    """解析权威源绝对路径：env > 注册表 > 默认位置。"""
    env = os.environ.get("NR_CLIENT_AUTHORITY")
    if env and os.path.exists(env):
        return os.path.abspath(env)
    try:
        with open(NR_CLIENT_REGISTRY, "r", encoding="utf-8") as f:
            reg = json.load(f)
        p = reg.get("path")
        if p and os.path.exists(p):
            return os.path.abspath(p)
    except Exception:
        pass
    if os.path.exists(NR_CLIENT_AUTHORITY_DEFAULT):
        return os.path.abspath(NR_CLIENT_AUTHORITY_DEFAULT)
    return None


def _is_authoritative() -> bool:
    auth = _resolve_authoritative()
    if not auth:
        return False
    return os.path.abspath(auth) == os.path.abspath(__file__)


def _register_authority() -> None:
    """权威源在被 import 时登记自身路径+版本到注册表。"""
    try:
        os.makedirs(os.path.dirname(NR_CLIENT_REGISTRY), exist_ok=True)
        with open(NR_CLIENT_REGISTRY, "w", encoding="utf-8") as f:
            json.dump({
                "path": os.path.abspath(__file__),
                "version": NR_CLIENT_VERSION,
                "updated_at": datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def ensure_latest(verbose: bool = True) -> Optional[bool]:
    """若本文件落后于权威源，则拉取最新版覆盖自身（仅在 import 时、且本文件非权威源时触发）。

    返回:
      False -> 已是最新（或本就是权威源）
      True  -> 已拉取更新
      None  -> 无法解析权威源 / 同步被禁用 / 拉取失败
    """
    if os.environ.get("NR_CLIENT_DISABLE_AUTOSYNC") == "1":
        return None
    if _is_authoritative():
        _register_authority()   # 权威源：保持注册表最新
        return False
    src = _resolve_authoritative()
    if not src or not os.path.exists(src):
        return None
    if os.path.abspath(src) == os.path.abspath(__file__):
        return False
    src_ver = _read_version_from_file(src)
    if not _version_lt(NR_CLIENT_VERSION, src_ver):
        return False  # 已是同等或更新版本
    try:
        shutil.copyfile(src, __file__)
        if verbose:
            print(f"[nr_client] 自动拉取权威版 v{src_ver}（覆盖本文件 v{NR_CLIENT_VERSION}）from {src}")
        return True
    except Exception as e:
        if verbose:
            print(f"[nr_client] 自动拉取失败: {e}")
        return None


# ── 破坏性操作护栏（写前快照 + 大规模删除熔断）──────────
# 关闭护栏：NR_CLIENT_DISABLE_GUARD=1
# 调参：NR_CLIENT_SNAPSHOT_DIR / NR_CLIENT_DROP_THRESHOLD(0.5) / NR_CLIENT_DELETE_NODES(20)

NR_GUARD = os.environ.get("NR_CLIENT_DISABLE_GUARD") != "1"
NR_SNAPSHOT_DIR = os.environ.get(
    "NR_CLIENT_SNAPSHOT_DIR",
    os.path.join(os.path.expanduser("~"), ".workbuddy", "nr_snapshots"),
)
NR_DEPLOY_DROP_THRESHOLD = float(os.environ.get("NR_CLIENT_DROP_THRESHOLD", "0.5"))
NR_DELETE_NODE_THRESHOLD = int(os.environ.get("NR_CLIENT_DELETE_NODES", "20"))

# prod(1880 正式实例) 显式护栏：默认禁止写 prod，需显式 opt-in
#   开全局：NR_ALLOW_PROD=1
#   或调用时 allow_prod=True
# 判定 prod：URL 含 :1880，或显式设 NR_PROD=1
NR_ALLOW_PROD = os.getenv("NR_ALLOW_PROD") == "1"


class NRGuardError(RuntimeError):
    """破坏性操作被熔断时抛出"""
    pass


class NRRollbackError(NRGuardError):
    """写操作失败、已回滚到 last-good 时抛出（含快照路径）。

    语义：部署/更新在写前已捕获 last-good 快照，写过程异常则回滚，
    故抛出本异常时 NR 侧已回到部署前状态（不会残留半成品）。
    """
    def __init__(self, msg: str, snapshot_path: Optional[str] = None):
        super().__init__(msg)
        self.snapshot_path = snapshot_path


class _Resp:
    """轻量响应包装，兼容 requests 的 status_code / text / json() 用法"""
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text

    def json(self):
        return json.loads(self.text)


class NodeRedClient:
    """Node-RED Admin API 客户端（安全增强版，纯标准库实现）"""

    def __init__(self, url: str = None, username: str = None, password: str = None):
        self.base_url  = (url or NR_URL).rstrip("/")
        self.username  = username or NR_USER
        self.password  = password or NR_PASS
        self._token: Optional[str] = None
        self._session_headers: Dict[str, str] = {}  # 复用 Authorization 等头
        self._flow_cache: Dict[str, Dict] = {}  # flow_id -> {nodes_count, timestamp}

    # ── 认证 ──────────────────────────────────────────

    def login(self) -> str:
        """POST /auth/token → JWT Bearer token（纯标准库 urllib，无外部依赖）"""
        req = urllib.request.Request(
            f"{self.base_url}/auth/token",
            data=json.dumps({
                "client_id": "node-red-admin",
                "grant_type": "password",
                "scope": "*",
                "username": self.username,
                "password": self.password,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Login failed ({e.code}): {e.read().decode('utf-8','replace')[:200]}")
        data = json.loads(body)
        self._token = data["access_token"]
        self._session_headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        return self._token

    def _ensure_auth(self):
        if not self._token:
            self.login()

    # ── 底层请求 ──────────────────────────────────────

    def _build_url(self, endpoint: str) -> str:
        """拼完整 URL，并对路径做 percent-encoding。

        flow ID / 路径里可能含中文等非 ASCII 字符 → 必须编码，
        否则 urllib.request 拼 URL 时会抛 'ascii' codec can't encode。
        """
        enc_ep = quote(endpoint, safe="/:?=&")
        return f"{self.base_url}{enc_ep}"

    def _request(self, method: str, endpoint: str, **kwargs) -> "_Resp":
        """纯标准库实现：自动处理 401 重试一次"""
        self._ensure_auth()
        url = self._build_url(endpoint)
        raw = kwargs.get("json")
        data = json.dumps(raw).encode("utf-8") if raw is not None else None
        headers = dict(self._session_headers)
        if data is not None:
            headers["Content-Type"] = "application/json"

        def _do() -> "_Resp":
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return _Resp(resp.status, resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                return _Resp(e.code, e.read().decode("utf-8", "replace"))

        resp = _do()
        if resp.status_code == 401:          # token 过期，重试一次
            self._token = None
            self._session_headers.pop("Authorization", None)
            self._ensure_auth()
            resp = _do()
        return resp

    def _json(self, method: str, endpoint: str, **kwargs) -> Any:
        """自动处理 204/400+/JSON 解析"""
        resp = self._request(method, endpoint, **kwargs)
        if resp.status_code == 204:
            return {}
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {endpoint} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ── Flows ─────────────────────────────────────────

    def list_flows(self) -> List[Dict]:
        """列出所有 flows"""
        return self._json("GET", "/flows")

    def get_flow(self, flow_id: str, use_cache: bool = True) -> Dict:
        """
        获取单个 flow（含所有节点）
        
        use_cache: 是否使用缓存的节点数量（用于后续安全检查）
        """
        flow = self._json("GET", f"/flow/{flow_id}")
        
        # 缓存节点数量
        if use_cache:
            nodes_count = len(flow.get("nodes", []))
            self._flow_cache[flow_id] = {
                "nodes_count": nodes_count,
                "timestamp": datetime.now().isoformat()
            }
        
        return flow

    def update_flow(self, flow_id: str, flow_data: Dict, 
                    force: bool = False, dry_run: bool = False,
                    allow_prod: bool = False) -> Dict:
        """
        PUT /flow/:id 更新并部署
        
        安全检查:
        1. 节点数量保护（减少超过 10% 会拒绝，除非 force=True）
        2. 标准化高风险字段（inject crontab, api-current-state）
        
        force: 强制更新，跳过节点数量检查
        dry_run: 仅预览，不实际更新
        """
        # 标准化
        self._normalize_flow(flow_data)
        
        # 护栏（NR_GUARD）：结构 lint + prod 护栏（即便 force 也拦 lint）
        if NR_GUARD:
            lp = self._lint_flows([flow_data])
            if lp:
                raise NRGuardError(
                    f"⚠️ update_flow 含节点级结构问题（flow={flow_id}）：\n"
                    + "\n".join(f"  - {p}" for p in lp)
                )
            self._guard_prod(allow_prod, f"update_flow {flow_id}")
        
        # 安全检查
        new_count = len(flow_data.get("nodes", []))
        if not force:
            cached = self._flow_cache.get(flow_id, {})
            old_count = cached.get("nodes_count", 0)
            
            if old_count > 0 and new_count < old_count * 0.9:
                raise RuntimeError(
                    f"⚠️ 节点数量异常减少: {old_count} → {new_count}，拒绝更新。\n"
                    f"如确认要删除节点，请使用 force=True 或手动在 Node-RED UI 操作。"
                )
        
        if dry_run:
            return {"dry_run": True, "flow_id": flow_id, "nodes": new_count}
        
        # 更新
        result = self._json("PUT", f"/flow/{flow_id}", json=flow_data)
        
        # 更新缓存
        nodes_count = len(flow_data.get("nodes", []))
        self._flow_cache[flow_id] = {
            "nodes_count": nodes_count,
            "timestamp": datetime.now().isoformat()
        }
        
        # 记录日志
        _log_operation("UPDATE_FLOW", f"flow={flow_id} | nodes={nodes_count}")
        
        return result

    def put_flow_raw(self, flow_id: str, flow_data: Dict) -> Dict:
        """直写 PUT /flow/:id —— 绕过 _normalize_flow 与全部护栏。

        仅用于已是人审确认闸批准的「切 tab.disabled」场景（set_tab_state_execute），
        必须原样回写节点内容以满足 AC9（节点 payload 字节不变）。其余写路径一律走
        update_flow / create_or_update_flow（带标准化+护栏），勿调此直写方法。
        """
        result = self._json("PUT", f"/flow/{flow_id}", json=flow_data)
        _log_operation("PUT_FLOW_RAW", f"flow={flow_id} | 直写(跳过标准化/护栏)")
        return {"id": flow_id, "raw": result}

    # ── 破坏性操作护栏（写前快照 + 大规模删除熔断）──────────

    def _snapshot_raw(self, label: str) -> Optional[str]:
        """GET /flows 全量快照到磁盘（熔断前先留底）。返回快照路径或 None。"""
        try:
            flows = self._json("GET", "/flows")
        except Exception as e:
            _log_operation("GUARD_SNAPSHOT_FAIL", f"label={label} | {e}")
            return None
        try:
            os.makedirs(NR_SNAPSHOT_DIR, exist_ok=True)
        except Exception:
            pass
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", label)[:40]
        path = os.path.join(NR_SNAPSHOT_DIR, f"nr_{safe}_{ts}.json")
        data = {
            "_meta": {
                "saved_at": datetime.now().isoformat(),
                "label": label,
                "nr_url": self.base_url,
                "node_count": len(flows),
                "auto_snapshot": True,
            },
            "flows": flows,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            _log_operation("GUARD_SNAPSHOT", f"label={label} | nodes={len(flows)} | {path}")
            return path
        except Exception as e:
            _log_operation("GUARD_SNAPSHOT_WRITE_FAIL", f"{e}")
            return None

    def restore_snapshot(self, path: str, allow_prod: bool = False) -> int:
        """把 _snapshot_raw 生成的快照回滚（重放）到 NR。

        行为级回滚的核心逃逸口：deploy_all / create_tab 写前已拍全量快照，
        任一写操作失败时可调用本方法把整实例恢复到部署前 last-good。
        逐 flow 走 create_or_update_flow（force=True 跳过节点数熔断，
        lint 仍拦数据损坏；prod 需 allow_prod 显式 opt-in）。
        返回成功恢复的 flow 数。
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"快照不存在：{path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        flows = data.get("flows", [])
        restored = 0
        for fl in flows:
            if not isinstance(fl, dict) or "id" not in fl:
                continue
            try:
                self.create_or_update_flow(
                    fl["id"], fl, force=True, allow_prod=allow_prod)
                restored += 1
            except Exception as e:  # pragma: no cover - 回滚自身失败兜底
                _log_operation("RESTORE_FAIL",
                               f"flow={fl.get('id')} | {e}")
        _log_operation("RESTORE", f"path={path} | restored={restored}/{len(flows)}")
        return restored

    def _live_counts(self):
        """返回 (tab_count, node_count, flows_list)。

        注意: NR GET /flows 返回扁平数组——type=="tab" 是分页对象，
        其余节点（含 HA server 等 config 节点）是独立顶层条目，用 z 指向所属 tab。
        因此节点总数 = 非 tab 条目数。
        """
        flows = self._json("GET", "/flows")
        tabs = [f for f in flows if f.get("type") == "tab"]
        nodes = [f for f in flows if f.get("type") != "tab"]
        return len(tabs), len(nodes), flows

    def deploy_all(self, flows: List[Dict], force: bool = False,
                   dry_run: bool = False, allow_prod: bool = False,
                   allow_partial: bool = False) -> Dict:
        """POST /flows 全量部署。

        护栏（NR_GUARD）:
          0. 【防部分 payload 整实例替换】若提案缺失线上已有的 tab/subflow（即提案是线上
             严格子集），即便 force=True 也拒绝，除非 allow_partial=True。这直接封堵「仅把
             少数 flow 当 payload 整实例替换 → 清场」的根因模式（本次事故即此类）。
          1. 写前自动全量快照到 NR_SNAPSHOT_DIR；
          2. 熔断：若提案节点数 < 线上节点数 ×(1-阈值)，拒绝（防误清空整实例）；
          3. 子流程端口完整性：任一子流程定义 out/in 非 `[{...,wires}]`/`[]`（如写成
             `[1]`）会致编辑器加载 `out[0].wires.forEach` 崩溃，即便 force 也拒绝部署。
        force=True：仍留快照，但跳过熔断(2)放行（护栏(0)(3)仍拦，因属数据损坏/清场级）。
        allow_partial=True：显式允许「提案不含全部线上 flow」的整实例替换（慎用，会删差集）。
        dry_run=True：不实际写，仅返回与线上的 diff 预览 + 快照路径。
        关闭护栏：NR_CLIENT_DISABLE_GUARD=1。
        """
        snap = None
        if NR_GUARD:
            snap = self._snapshot_raw("deploy_all_before")
            # 护栏(0)：防「部分 payload 整实例替换」根因模式（清场）
            if not allow_partial:
                _, _, live_list = self._live_counts()
                live_ids = {f.get("id") for f in live_list if f.get("id")}
                prop_ids = {f.get("id") for f in flows if f.get("id")}
                missing = live_ids - prop_ids
                if missing:
                    raise NRGuardError(
                        f"⚠️ 熔断：deploy_all 提案缺失线上已有的 {len(missing)} 个 tab/subflow，"
                        f"属「部分 payload 整实例替换」根因模式（会清场）。\n"
                        f"已存快照: {snap}\n如需真实整实例替换，请传 allow_partial=True。"
                    )
            if not force:
                live_tabs, live_nodes, _ = self._live_counts()
                # 提案节点数：gateway 风格({nodes:[...]}) 求和；标准扁平导出 数非 tab 条目
                if flows and isinstance(flows[0], dict) and "nodes" in flows[0]:
                    prop_nodes = sum(len(f.get("nodes", [])) for f in flows)
                else:
                    prop_nodes = len([f for f in flows if f.get("type") != "tab"])
                if live_nodes > 0 and prop_nodes < live_nodes * (1 - NR_DEPLOY_DROP_THRESHOLD):
                    drop = 100 * (1 - prop_nodes / live_nodes)
                    raise NRGuardError(
                        f"⚠️ 熔断：deploy_all 将把线上节点从 {live_nodes} 降到 {prop_nodes} "
                        f"（跌幅 {drop:.0f}% > 阈值 {int(NR_DEPLOY_DROP_THRESHOLD*100)}%）。\n"
                        f"已存快照: {snap}\n"
                        f"如确认要执行，请调用 deploy_all(flows, force=True)。"
                    )
            # 子流程端口完整性（防 forEach 崩溃）—— 即便 force 也拦
            sp = self._validate_subflow_ports(flows)
            if sp:
                raise NRGuardError(
                    "⚠️ 部署内含畸形子流程定义（会导致编辑器加载整页崩溃 "
                    "'Cannot read properties of undefined (reading \\'forEach\\')'）：\n"
                    + "\n".join(f"  - {p}" for p in sp)
                    + f"\n已存快照: {snap}\n请修正子流程 out/in 为端口对象数组后再部署。"
                )
            # 节点级结构 lint（R10 类 wires 畸形等）—— 即便 force 也拦
            lp = self._lint_flows(flows)
            if lp:
                raise NRGuardError(
                    "⚠️ 部署内含节点级结构问题（会导致编辑器/运行时异常）：\n"
                    + "\n".join(f"  - {p}" for p in lp)
                    + f"\n已存快照: {snap}\n请修正后重试。"
                )
            # prod 实例护栏
            self._guard_prod(allow_prod, "deploy_all 全量部署")
        if dry_run:
            live = self._json("GET", "/flows")
            diff = self._diff_flows(live, flows)
            return {"dry_run": True, "snapshot": snap, "diff": diff}
        for f in flows:
            self._normalize_flow(f)
        result = self._json("POST", "/flows", json=flows)
        _log_operation("DEPLOY_ALL", f"flows={len(flows)}")
        return result

    def _validate_subflow_ports(self, flows: List[Dict]) -> List[str]:
        """校验所有子流程定义的 out/in 为合法端口数组。返回问题列表（空=OK）。"""
        problems: List[str] = []
        for n in flows:
            if n.get("type") != "subflow":
                continue
            sid = n.get("id", "?")
            for key in ("out", "in"):
                v = n.get(key)
                if v is None:
                    continue
                if not isinstance(v, list):
                    problems.append(f"子流程 {sid} 的 {key} 不是数组（实得 {type(v).__name__}）")
                    continue
                for idx, port in enumerate(v):
                    if not isinstance(port, dict) or "wires" not in port:
                        problems.append(
                            f"子流程 {sid} 的 {key}[{idx}] 不是合法端口对象（实得 {port!r}）；"
                            f"应为 {{x,y,wires:[...]}} 或空数组 []"
                        )
                        continue
                    # 版本无关的最小检查：wires 必须是数组（NR 5.x 元素是
                    # {{"id":"nodeId"}} / {{"id":"nodeId","port":0}} 对象；旧版可能是
                    # 字符串列表的列表）。只要它是数组，forEach 就不会崩。
                    wires = port.get("wires")
                    if not isinstance(wires, list):
                        problems.append(
                            f"子流程 {sid} 的 {key}[{idx}].wires 不是数组（实得 {type(wires).__name__}）；"
                            f"应为端口连线数组，如 [{{\"id\":\"nodeId\"}}] 或 [[\"nodeId\"]]"
                        )
        return problems

    # ── 节点级结构 lint（纵深防御）────────────────────
    # 仅拦截「数据损坏级」结构问题（含历史 R10 崩溃）：
    #   - wires 不是数组 / 数组元素不是数组 / 目标不是非空 str id
    #   - 单 output 节点出现 >1 个 wires 数组（R10：第 2 数组目标永不触发）
    #   - 子流程 out/in 端口畸形（编辑器 forEach 崩溃）
    # 注意：仅检查「破坏性」问题，不拦截合理的空 wires（link out/debug 等终节点）。

    # 已知单 output 节点类型（declared outputs 缺失时回退用）。
    # 多 output 节点（switch/api-current-state 等）靠节点自身 outputs 字段判断。
    _SINGLE_OUTPUT_TYPES = {
        "inject": 1, "delay": 1, "api-current-state": 1, "change": 1,
        "http request": 1,
        # link out / debug / api-call-service 是 0 输出的终节点（wires:[] 完全合法）。
        # 标成 1 会误触发 R10「期望 1 个 output 却得 0 个」并拦死部署（Bug A / RW-R10）。
        # api-call-service 是 HA websocket 的终端调用节点，不向下游发 msg，故 outputs=0。
        "link out": 0, "link call": 1, "debug": 0, "api-call-service": 0,
        "function": 1, "event-state": 1,
    }

    def _lint_flows(self, flows: List[Dict]) -> List[str]:
        """节点级结构 lint。返回阻断级问题列表（空=OK）。

        即便是 force=True 也应在部署前调用（与 _validate_subflow_ports 同级别，
        属数据损坏预防，而非「节点数减少」类可放行的熔断）。
        """
        problems: List[str] = []
        for f in flows:
            if not isinstance(f, dict):
                continue
            nodes = f.get("nodes", [])
            if not isinstance(nodes, list):
                # 扁平导出（单 flow 顶层节点列表）
                if "type" in f:
                    nodes = [f]
                else:
                    continue
            for n in nodes:
                nid = n.get("id", "?")
                t = n.get("type", "")
                if t in ("tab", "subflow"):
                    continue
                wires = n.get("wires")
                if wires is None:
                    continue  # 无输出字段（comment 等）允许
                if not isinstance(wires, list):
                    problems.append(
                        f"node[{nid[:8]}] type={t} wires 不是数组（实得 {type(wires).__name__}）")
                    continue
                # 期望 output 数：优先用节点自身 outputs 字段，否则回退映射
                declared = n.get("outputs")
                expected = declared if isinstance(declared, int) else \
                    NodeRedClient._SINGLE_OUTPUT_TYPES.get(t, None)
                # R10 类：期望 1 个 output，却出现 ≠1 个 wires 数组
                if isinstance(expected, int) and expected == 1 and len(wires) != 1:
                    problems.append(
                        f"node[{nid[:8]}] type={t} 期望 1 个 output，却得 {len(wires)} 个 "
                        f"wires 数组（历史 R10：多数组导致第 2 数组目标永不触发）")
                for wi, w in enumerate(wires):
                    if not isinstance(w, list):
                        problems.append(
                            f"node[{nid[:8]}] wires[{wi}] 不是数组（实得 {type(w).__name__}）")
                        continue
                    for dst in w:
                        if not isinstance(dst, str) or not dst:
                            problems.append(
                                f"node[{nid[:8]}] wires[{wi}] 含非法目标 {dst!r}（应为非空 str id）")
        # 子流程端口完整性（编辑器 forEach 崩溃级）
        problems.extend(self._validate_subflow_ports(flows))
        return problems

    def is_prod(self) -> bool:
        """目标实例是否为 prod 环境（受保护，写需 allow_prod opt-in）。

        以 AUTOFLLOW_ENV 为准（不再按端口 1880 判定），使单实例 1880 部署也能正常写；
        只有显式 AUTOFLLOW_ENV=prod（或 NR_PROD=1）才进入受保护模式。
        """
        if os.getenv("AUTOFLLOW_ENV", "staging").lower() == "prod":
            return True
        if os.getenv("NR_PROD") == "1":
            return True
        return False

    def _guard_prod(self, allow_prod: bool, action: str) -> None:
        """写 prod 环境前护栏；prod 且未显式 opt-in 则熔断。"""
        if self.is_prod() and not (allow_prod or NR_ALLOW_PROD):
            raise NRGuardError(
                f"⚠️ 操作目标为 PROD 环境(AUTOFLLOW_ENV=prod): {action}\n"
                f"默认禁止写 prod（防误操作整实例替换）。\n"
                f"如确认要执行，请设 NR_ALLOW_PROD=1 或调用时 allow_prod=True。"
            )

    def _diff_flows(self, live: List[Dict], proposed: List[Dict]) -> Dict[str, Any]:
        """对比线上(live)与提案(proposed)的节点增减改，返回 diff 摘要。"""
        live_ids = {n.get("id"): n for n in live}
        prop_ids = {n.get("id"): n for n in proposed}
        added = [i for i in prop_ids if i not in live_ids]
        removed = [i for i in live_ids if i not in prop_ids]
        modified = []
        for i in prop_ids:
            if i in live_ids:
                ln, pn = live_ids[i], prop_ids[i]
                if (ln.get("type") != pn.get("type")
                        or json.dumps(ln, sort_keys=True, ensure_ascii=False)
                        != json.dumps(pn, sort_keys=True, ensure_ascii=False)):
                    modified.append(i)
        return {
            "added": added, "removed": removed, "modified": modified,
            "added_count": len(added), "removed_count": len(removed),
            "modified_count": len(modified),
        }

    def delete_flow(self, flow_id: str, force: bool = False,
                    allow_prod: bool = False) -> Dict:
        """删除 flow（tab 或子流程）。

        护栏（NR_GUARD 且未 force）:
          1. 写前自动全量快照；
          2. 熔断：若目标含节点数 ≥ NR_DELETE_NODE_THRESHOLD(默认20)，拒绝（防误删大 flow）。
        force=True：仍留快照，但跳过熔断放行。gateway 等已确认的操作请传 force=True。

        子流程特例：NR 的 DELETE /flow/:id 对子流程返回 404（子流程须经全量集合去除），
        故 subflow 走「GET 全量 → 过滤掉该 subflow def 及其内部节点(z=id) →
        deploy_all(allow_partial=True)」的定向移除，其余 tab/subflow 全部保留，不会清场。
        """
        if NR_GUARD:
            snap = self._snapshot_raw(f"delete_{str(flow_id)[:12]}")
            self._guard_prod(allow_prod, f"delete_flow {flow_id}")
        live = self._json("GET", "/flows")
        target = next((f for f in live if f.get("id") == flow_id), None)
        if target is None:
            return {"deleted": False, "reason": "not_found"}
        # ── 子流程：全量定向移除 ──
        if target.get("type") == "subflow":
            kept = [f for f in live
                    if f.get("id") != flow_id and f.get("z") != flow_id]
            removed = len(live) - len(kept)
            if NR_GUARD and not force and removed >= NR_DELETE_NODE_THRESHOLD:
                raise NRGuardError(
                    f"⚠️ 熔断：删除子流程 {flow_id} 将移除 {removed} 个节点"
                    f"（≥ 阈值 {NR_DELETE_NODE_THRESHOLD}）。\n"
                    f"已存快照: {snap}\n如确认要删除，请传 force=True。"
                )
            # allow_partial=True：显式定向移除（其余 flow 全部保留）
            self.deploy_all(kept, force=force, allow_partial=True, allow_prod=allow_prod)
            _log_operation("DELETE_SUBFLOW", f"flow={flow_id} | removed={removed}")
            return {"deleted": True, "type": "subflow", "removed": removed}
        # ── 普通 tab：原 DELETE 路径（带节点数熔断）──
        if NR_GUARD and not force:
            target_nodes = sum(1 for n in live if n.get("z") == flow_id)
            if target_nodes >= NR_DELETE_NODE_THRESHOLD:
                raise NRGuardError(
                    f"⚠️ 熔断：delete_flow 将删除含 {target_nodes} 个节点的 flow "
                    f"（≥ 阈值 {NR_DELETE_NODE_THRESHOLD}）。\n"
                    f"已存快照: {snap}\n"
                    f"如确认要删除，请调用 delete_flow(flow_id, force=True)。"
                )
        result = self._json("DELETE", f"/flow/{flow_id}")
        _log_operation("DELETE_FLOW", f"flow={flow_id}")
        return result

    def get_default_server_id(self) -> str:
        """返回 NR 中第一个 HA server 节点 id（部署时填补触发器 server 字段）。"""
        return self._get_default_server()

    def create_tab(self, label: str, allow_prod: bool = False) -> Dict:
        """创建新 flow tab（安全增量路径，绝不整实例替换）。

        安全模型：走 create_or_update_flow 的「POST /flow 建壳 → PUT /flow/:id 补节点」
        单 flow 路径（1880 验证稳定），仅新增 1 个 tab，绝不触碰其它 tab / 子流程。
        不再使用 POST /flows 整实例替换（该路径一旦 payload 仅含少数 flow 即清空全实例，
        是历史事故的根因模式：当年某次调用只把单个子流程当作 payload 整体替换，
        清场后整实例只剩那一个子流程）。
        返回 {'id': <真实 tab id>, 'label': label, 'nodes': []}。
        """
        tab_id = str(uuid.uuid4()).replace('-', '')[:16]
        new_flow = {
            "id": tab_id,
            "type": "tab",
            "label": label,
            "nodes": []
        }
        res = self.create_or_update_flow(tab_id, new_flow, allow_prod=allow_prod)
        _log_operation("CREATE_TAB", f"flow={res['id']} | label={label}")
        return {"id": res["id"], "label": label, "nodes": []}

    # ── 子流程生成（网关可程序化产出 subflow，免去用户手搓）────────

    @staticmethod
    def _norm_subflow_wire_item(item, is_output):
        """把子流程端口的单个连线目标归一化为 NR 5.x 对象格式。

        调用方（尤其是经 MCP autoflow_create_subflow 提交的 agent）常把子流程端口
        误写成「流节点语法」——把端口当成普通节点连线，例如 wires 写成
        "fn1" / ["fn1"] / [["fn1"]]，而 NR 子流程端口要求
        {"id": <node>, "port": <n>}（输出端口必带 port）。此函数把这些误写
        归一化为标准对象，避免部署后端口实际未连接（#670 真问题）。
        """
        if isinstance(item, dict):
            if "id" not in item:
                return item
            if is_output and "port" not in item:
                return {"id": item["id"], "port": 0}
            return item
        if isinstance(item, str):
            return {"id": item, "port": 0} if is_output else {"id": item}
        if isinstance(item, list):
            if not item:
                return item
            if isinstance(item[0], dict):
                return NodeRedClient._norm_subflow_wire_item(item[0], is_output)
            # ["fn1"] 或 ["fn1", 0]
            nid = item[0]
            port = item[1] if len(item) > 1 else None
            if port is None:
                return {"id": nid, "port": 0} if is_output else {"id": nid}
            return {"id": nid, "port": port}
        return item

    @staticmethod
    def _normalize_subflow_ports(in_ports, out_ports):
        """归一化子流程 in/out 端口的 wires 为对象数组 [{id, port?}]。

        输入端口（is_output=False）：目标规范为 {"id": node}（port 可省）。
        输出端口（is_output=True）：目标必须带 port，缺失补 0。
        个别调用方可能把 wires 写成单对象 / 单字符串而非数组，此处一并兜底。
        返回新的 (in_ports, out_ports) 列表（不就地改入参）。
        """
        def norm_ports(ports, is_output):
            if not isinstance(ports, list):
                return ports
            out = []
            for port in ports:
                if not isinstance(port, dict):
                    out.append(port)
                    continue
                p = dict(port)
                w = p.get("wires")
                if isinstance(w, list):
                    p["wires"] = [NodeRedClient._norm_subflow_wire_item(x, is_output) for x in w]
                elif isinstance(w, (dict, str)):
                    p["wires"] = [NodeRedClient._norm_subflow_wire_item(w, is_output)]
                out.append(p)
            return out
        return norm_ports(in_ports, False), norm_ports(out_ports, True)

    def build_subflow_entries(self, subflow_id: str, name: str,
                              in_ports: List[Dict], out_ports: List[Dict],
                              nodes: List[Dict], info: str = "",
                              category: str = "subflows",
                              env: List[Dict] = None) -> List[Dict]:
        """把「子流程定义 + 内部节点」组装成 NR 扁平条目数组（def 在前，内部节点 z 指向 subflow_id）。

        返回可直接喂给 create_subflow / deploy_all（增量 append）。
        in_ports/out_ports 形如（NR 5.x 真实格式）：
            in_ports  = [{"x":40,"y":40,"wires":[{"id":"<node_id>"}]}]
            out_ports = [{"x":40,"y":120,"wires":[{"id":"<node_id>","port":0}]}]
        即每个端口的 wires 是「连线对象数组」：输入端口用 {"id": 内部节点}，
        输出端口用 {"id": 内部节点, "port": 输出序号}。内部节点 wires 用子流程内真实节点 id。

        ⚠️ 端口 wires 归一化（#670）：调用方可能用流节点语法误写端口线
        （"fn1" / ["fn1"] / [["fn1"]]），此处统一归一化为标准对象格式，
        否则 NR 5.x 不认该端口、子流程输入/输出实际悬空。
        """
        in_ports, out_ports = self._normalize_subflow_ports(in_ports, out_ports)
        def_entry = {
            "id": subflow_id,
            "type": "subflow",
            "name": name,
            "info": info,
            "category": category,
            "in": in_ports or [],
            "out": out_ports or [],
            "status": {"x": 0, "y": 0, "wires": []},
            "env": env or [],
            "meta": {},
        }
        internals = []
        for n in nodes:
            nn = dict(n)
            nn["z"] = subflow_id
            internals.append(nn)
        return [def_entry] + internals

    def create_subflow(self, subflow_id: str, name: str,
                       in_ports: List[Dict], out_ports: List[Dict],
                       nodes: List[Dict], info: str = "",
                       category: str = "subflows", env: List[Dict] = None,
                       allow_prod: bool = False) -> Dict:
        """程序化创建子流程到 NR（def + 内部节点），不触碰现有 flow / 不整实例替换。

        安全模型：经 deploy_all 的【增量 append】路径（GET 全量 → 追加本子流程条目 →
        带护栏回写）。写前自动全量快照；熔断仅拦「节点数下降 > 阈值」，而 append 使节点数
        上升、永不触发；子流程端口完整性 + 节点级 lint 仍拦数据损坏。故不会清空实例。
        返回 {'id': subflow_id, 'created': True}。
        """
        entries = self.build_subflow_entries(
            subflow_id, name, in_ports, out_ports, nodes, info, category, env)
        live = self._json("GET", "/flows")
        combined = list(live) + entries
        # force=False：append 不会触发 drop 熔断；护栏（快照/lint/端口）仍生效
        self.deploy_all(combined, force=False, allow_prod=allow_prod)
        _log_operation("CREATE_SUBFLOW",
                       f"id={subflow_id} | name={name} | nodes={len(nodes)}")
        return {"id": subflow_id, "created": True}

    def generate_subflow_from_spec(self, spec: Dict, allow_prod: bool = False) -> Dict:
        """从声明式 spec 生成子流程并部署。

        spec 例：
          {"id":"abc","name":"Bark 推送","in_ports":[{"x":40,"y":40,"wires":[["n1"]]}],
           "out_ports":[],"nodes":[{"id":"n1","type":"function","func":"...","wires":[]}],
           "info":"","category":"subflows","env":[]}
        节点可引用 build_* 构造器产出。返回 create_subflow 的结果。
        """
        return self.create_subflow(
            spec["id"], spec["name"], spec.get("in_ports", []),
            spec.get("out_ports", []), spec.get("nodes", []),
            info=spec.get("info", ""), category=spec.get("category", "subflows"),
            env=spec.get("env"), allow_prod=allow_prod)

    def create_flow(self, flow_data: Dict, force: bool = False,
                    allow_prod: bool = False) -> Dict:
        """
        POST /flow 创建新 flow。body 含 id 时 NR 使用该 id（须唯一）。
        返回 {'id': <真实flow id>, 'created': True, 'raw': <NR 响应>}。
        """
        self._normalize_flow(flow_data)
        if NR_GUARD:
            lp = self._lint_flows([flow_data])
            if lp:
                raise NRGuardError(
                    "⚠️ create_flow 含节点级结构问题：\n"
                    + "\n".join(f"  - {p}" for p in lp)
                )
            self._guard_prod(allow_prod, "create_flow")
        result = self._json("POST", "/flow", json=flow_data)
        new_id = result.get("id") or flow_data.get("id")
        nodes_count = len(flow_data.get("nodes", []))
        self._flow_cache[new_id] = {
            "nodes_count": nodes_count,
            "timestamp": datetime.now().isoformat()
        }
        _log_operation("CREATE_FLOW", f"flow={new_id} | nodes={nodes_count}")
        return {"id": new_id, "created": True, "raw": result}

    def create_or_update_flow(self, flow_id: str, flow_data: Dict,
                              force: bool = False,
                              allow_prod: bool = False) -> Dict:
        """
        创建或更新 flow（部署新场景/更新已有场景的统一入口）：
          - 先 GET /flow/:id 探存；不存在（404/异常）→ POST /flow 创建
          - 已存在 → PUT /flow/:id 更新
        返回 {'id': <真实flow id>, 'created': bool, 'raw': <NR 响应>}。
        护栏：结构 lint + prod 护栏（force 仅跳过节点数熔断，不跳过 lint/prod）。
        """
        self._normalize_flow(flow_data)
        if NR_GUARD:
            lp = self._lint_flows([flow_data])
            if lp:
                raise NRGuardError(
                    f"⚠️ create_or_update_flow 含节点级结构问题（flow={flow_id}）：\n"
                    + "\n".join(f"  - {p}" for p in lp)
                )
            self._guard_prod(allow_prod, f"create_or_update_flow {flow_id}")
        # 写前捕获 last-good（existing 即部署前该 flow 的当前态，回滚源）
        existing = None
        try:
            existing = self._json("GET", f"/flow/{flow_id}")
        except Exception:
            existing = None
        try:
            if existing is None:
                # 行为：POST /flow 时 NR 会重新生成 flow id（不采纳我们传的 id），
                # 且 POST 不持久化节点；PUT /flow/:id 能落盘却要求 flow 已存在。
                # 故三步：① POST 建壳拿真实 id R ② 把节点 z 改写为 R ③ PUT /flow/R 补节点。
                try:
                    created = self.create_flow(flow_data, force=force, allow_prod=allow_prod)
                except Exception as _ce:
                    # RW-DUP：NR 对「id 已存在」偶发返回 400 duplicate id（尤其中文 flow id /
                    # 既有同名节点 id 冲突，导致 GET 探存失效而误走 POST）。此时该 flow / 其节点
                    # 实际已在 NR 中存在，降级为 PUT 整体替换（必要时 DELETE 后重建），避免二次
                    # 部署（同一 DSL 场景名→稳定中文 slug→节点 id 全局重复）必然失败。
                    if "duplicate" in str(_ce).lower():
                        try:
                            result = self._json("PUT", f"/flow/{flow_id}", json=flow_data)
                            nodes_count = len(flow_data.get("nodes", []))
                            self._flow_cache[flow_id] = {
                                "nodes_count": nodes_count,
                                "timestamp": datetime.now().isoformat(),
                            }
                            _log_operation("UPDATE_FLOW", f"flow={flow_id} | nodes={nodes_count} (dup-fallback)")
                            return {"id": flow_id, "created": False, "raw": result}
                        except Exception:
                            try:
                                self._json("DELETE", f"/flow/{flow_id}")
                            except Exception:
                                pass
                            created = self.create_flow(flow_data, force=force, allow_prod=allow_prod)
                    else:
                        raise
                real_id = created.get("id") or flow_id
                for n in flow_data.get("nodes", []):
                    n["z"] = real_id
                self.update_flow(real_id, flow_data, force=force, allow_prod=allow_prod)
                return {"id": real_id, "created": True, "raw": created.get("raw")}
            # 已存在 → 走更新路径
            result = self._json("PUT", f"/flow/{flow_id}", json=flow_data)
            nodes_count = len(flow_data.get("nodes", []))
            self._flow_cache[flow_id] = {
                "nodes_count": nodes_count,
                "timestamp": datetime.now().isoformat()
            }
            _log_operation("UPDATE_FLOW", f"flow={flow_id} | nodes={nodes_count}")
            return {"id": flow_id, "created": False, "raw": result}
        except Exception as e:
            # 行为级回滚：写失败 → 恢复到部署前 last-good（existing），
            # 避免残留半成品。create 路径无 preexisting 则尽力删半壳。
            if existing is not None:
                try:
                    self.update_flow(flow_id, existing, force=True,
                                    allow_prod=allow_prod)
                    _log_operation("ROLLBACK",
                                    f"flow={flow_id} | restored to last-good")
                except Exception as re:  # pragma: no cover
                    _log_operation("ROLLBACK_FAIL",
                                    f"flow={flow_id} | {re}")
            else:
                try:
                    self._json("DELETE", f"/flow/{flow_id}")
                except Exception:
                    pass
            raise NRRollbackError(
                f"create_or_update_flow 写失败已回滚到 last-good：{e}",
                snapshot_path=None)

    # ── Nodes ─────────────────────────────────────────

    def list_nodes(self) -> List[Dict]:
        """列出已安装节点（NR 5.x 兼容）"""
        node_types = set()
        try:
            flows = self._json("GET", "/flows")
            for f in flows:
                for n in f.get("nodes", []):
                    t = n.get("type")
                    if t:
                        node_types.add(t)
        except Exception:
            pass
        return [{"id": t, "type": t, "name": t} for t in sorted(node_types)]

    def install_node(self, module: str) -> Dict:
        return self._json("POST", "/nodes", json={"module": module})

    # ── Settings / Diagnostics ─────────────────────────

    def get_settings(self) -> Dict:
        return self._json("GET", "/settings")

    def get_diagnostics(self) -> Dict:
        return self._json("GET", "/diagnostics")

    # ── Context ───────────────────────────────────────
    # 注意：Node-RED Admin Context API 是【只读 + 可删】——
    #   GET    /context/:scope/:key  → {"msg": "<值的字符串化>", "format": "..."}（值裹在 msg 里）
    #   DELETE /context/:scope/:key  → 204
    #   【没有 POST 写端点】（旧实现用 POST → 404）。
    # 故：读需解包 msg 并按 format 还原；清理用 DELETE；无法经 REST 写值（也无需——
    # 插桩 tap 内用 global.get(k)||[] 自初始化）。

    def get_context(self, store: str, key: str) -> Any:
        """读取 NR context 值，并解包 NR 的 {"msg","format"} 信封。

        NR 返回 {"msg": "<stringified value>", "format": "array[1]"/"boolean"/...}。
        本方法把 msg 解回原始 Python 值：
          - format 以 "array"/"object" 开头 或 msg 是 JSON → json.loads
          - "(undefined)" / undefined → None
          - 其余按字符串返回。
        解包失败时回退返回原始信封（调用方仍可自行处理）。
        """
        raw = self._json("GET", f"/context/{store}/{key}")
        if not isinstance(raw, dict) or "msg" not in raw:
            return raw  # 非预期形状，原样返回
        msg = raw.get("msg")
        fmt = str(raw.get("format") or "")
        if msg is None or msg == "(undefined)" or fmt == "undefined":
            return None
        if isinstance(msg, str):
            # array[...] / object / 明显的 JSON 字面量 → 尝试 JSON 还原
            if fmt.startswith("array") or fmt.startswith("object") \
                    or (msg[:1] in "[{" and msg[-1:] in "]}"):
                try:
                    return json.loads(msg)
                except Exception:
                    return msg
            if fmt == "boolean":
                return msg == "true"
            if fmt == "number":
                try:
                    return json.loads(msg)
                except Exception:
                    return msg
        return msg

    def delete_context(self, store: str, key: str):
        """删除 NR context key（清理 trace 用；DELETE → 204）。"""
        return self._json("DELETE", f"/context/{store}/{key}")

    # 已知核心 / HA 贡献节点类型（网关编译器会发射的全部词汇 + NR 核心）。
    # 作为注册表查不到时的兜底，避免误杀合法部署；新扩展节点类型时在此补充。
    KNOWN_NODE_TYPES = {
        # NR 核心
        "tab", "subflow", "subflow-instance", "group", "comment", "inject", "debug",
        "change", "catch", "switch", "delay", "function", "link in", "link out",
        "link call", "complete", "status", "range", "template", "rbe", "smooth",
        "trigger", "join", "split", "sort", "batch", "exec", "file", "file in",
        "fileinject", "watch", "tail", "ping", "markdown",
        "http in", "http request", "http response", "http proxy",
        "websocket in", "websocket out", "websocket-listener",
        "mqtt in", "mqtt out", "mqtt-broker", "tcp in", "tcp out", "udp in", "udp out",
        "tls-config", "flow", "unknown", "image viewer", "status",
        # HA 贡献（node-red-contrib-home-assistant-websocket 等）
        "server", "server-state-changed", "api-current-state", "api-call-service",
        "trigger-state", "events-state", "poll-state", "get-entities", "get-history",
        "ha-get-entities", "ha-fire-event", "api-render-template", "server-events",
        "xiaoai-tts-configurator", "axios-endpoint", "axios-request",
        "tcp request", "global-config",
        # 历史查询节点：/nodes 注册表报的别名是 "get-history"，
        # 但 flow 实例真实 type 是 "api-get-history"（已部署在用的节点）。
        # 两写进已知集，避免闸门因别名分歧误杀合法节点。
        "api-get-history",
        # 本网关扩展发射
        "time-range-switch",
    }

    def get_installed_node_types(self) -> set:
        """目标 NR 已安装（可运行）的节点类型集合（部署前注册表校验用）。

        优先级：
          1) 权威 /nodes（汇总每个模块声明的 types）；
          2) 退化：从 /flows 收集所有「在用」节点类型（网关已部署的
             类型必然出现在某条在用 flow 里，故等价于『NR 能跑的类型』）；
          3) 至少返回 KNOWN_NODE_TYPES（已知核心集），永不返回空集——
             空集会让上层『注册表取空则跳过』守卫把闸门变成空转。

        用途：编译产物若含目标 NR 装不了的类型（如旧版误发的
        `time-range`），部署即坏（陌生节点静默丢 msg），此闸门直接拦截。
        """
        types: set = set(self.KNOWN_NODE_TYPES)
        # 1) 权威 /nodes（模块声明 types）；失败则忽略，退化到 /flows
        try:
            mods = self._json("GET", "/nodes")
            if isinstance(mods, list):
                for m in mods:
                    if not isinstance(m, dict):
                        continue
                    for t in (m.get("types") or []):
                        if t:
                            types.add(t)
        except Exception:
            pass
        # 2) /flows 在用类型（含 subflow 内部节点，最可靠——
        #    社区/贡献节点如 api-get-history 不一定出现在 /nodes，但必在某条
        #    在用 flow 里。与 /nodes 取并集，避免漏判已装节点为未注册。
        try:
            flows = self._json("GET", "/flows")
        except Exception:
            return types  # 至少已知核心集，不空
        nodes = flows if isinstance(flows, list) else flows.get("flows", [])
        for f in nodes:
            if not isinstance(f, dict):
                continue
            # NR 5.x 的 /flows 是扁平节点数组：tab / subflow 定义 / 普通节点 /
            # subflow 实例都直接是顶层带 type 的节点配置（如 "subflow:b0bbc86..."）。
            # 旧版则是 {flows:[{nodes:[...]}]} 嵌套结构。
            # 两种都兼容：先取顶层节点自身 type，再兜底遍历嵌套 f.nodes。
            t = f.get("type")
            if t:
                types.add(t)
            for n in (f.get("nodes") or []):
                if isinstance(n, dict) and n.get("type"):
                    types.add(n["type"])
        return types

    def set_context(self, store: str, key: str, value: Any):
        """【已弃用】NR Admin API 无 POST 写端点。

        保留签名以兼容旧调用：value == [] / None 时按『清理』语义走 DELETE；
        其余写入请求无法经 REST 完成，抛错以免静默误导调用方。
        """
        if value in ([], None, {}):
            return self.delete_context(store, key)
        raise RuntimeError(
            "NR Admin Context API 不支持写入（无 POST 端点）；"
            "如需在 NR 内写 context，请用 function 节点的 global.set。")

    # ─────────────────────────────────────────────────
    #  实用工具方法
    # ─────────────────────────────────────────────────

    # ── 搜索 ─────────────────────────────────────────

    def find_flows(self, predicate: Callable[[Dict], bool]) -> List[Dict]:
        """按条件搜索 flow 元数据（不走 API）"""
        flows = self.list_flows()
        return [f for f in flows if predicate(f)]

    def find_flow_by_name(self, name: str) -> Optional[Dict]:
        """按 label 模糊匹配 flow"""
        flows = self.list_flows()
        kw = name.lower()
        for f in flows:
            label = (f.get("label") or "").lower()
            if kw in label or label in kw:
                return f
        return None

    def find_nodes_all(self, predicate: Callable[[Dict], bool]) -> List[tuple]:
        """
        在所有 flows 中全局搜索节点（仅搜索 type='tab' 的 flow）。
        返回: [(flow_id, flow_label, node_dict), ...]
        """
        results = []
        for f_summary in self.list_flows():
            if f_summary.get("type") != "tab":
                continue
            fid    = f_summary.get("id", "?")
            flabel = f_summary.get("label", fid)
            try:
                full_flow = self.get_flow(fid)
                for n in full_flow.get("nodes", []):
                    try:
                        if predicate(n):
                            results.append((fid, flabel, n))
                    except Exception:
                        pass
            except Exception:
                pass
        return results

    def find_nodes(self, flow_id: str, predicate: Callable[[Dict], bool]) -> List[Dict]:
        """在指定 flow 中搜索节点"""
        flow = self.get_flow(flow_id)
        return [n for n in flow.get("nodes", []) if predicate(n)]

    def get_node(self, flow_id: str, node_id: str) -> Optional[Dict]:
        """获取指定节点"""
        flow = self.get_flow(flow_id)
        for n in flow.get("nodes", []):
            if n["id"] == node_id:
                return n
        return None

    def find_node_all(self, node_id: str) -> Optional[tuple]:
        """在所有 flows 中查找指定节点 ID"""
        for fid, flabel, n in self.find_nodes_all(lambda x: x.get("id") == node_id):
            return (fid, flabel, n)
        return None

    def find_wires_to(self, target_node_id: str) -> List[Dict]:
        """查找所有指向 target_node_id 的连线（源节点列表）"""
        results = []
        for f_summary in self.list_flows():
            if f_summary.get("type") != "tab":
                continue
            fid = f_summary.get("id", "?")
            try:
                full_flow = self.get_flow(fid)
            except Exception:
                continue
            for n in full_flow.get("nodes", []):
                wires = n.get("wires", [])
                if not wires:
                    continue
                targets = wires[0] if isinstance(wires[0], list) else wires
                if target_node_id in targets:
                    results.append(n)
        return results

    def get_node_type_stats(self) -> Dict[str, int]:
        """统计各类型节点数量"""
        stats: Dict[str, int] = {}
        for _, _, n in self.find_nodes_all(lambda _: True):
            t = n.get("type", "?")
            stats[t] = stats.get(t, 0) + 1
        return dict(sorted(stats.items(), key=lambda x: -x[1]))

    # ── diff 工具 ─────────────────────────────────────

    @staticmethod
    def _diff_code(old: str, new: str) -> str:
        """生成代码差异（简单实现）"""
        old_lines = old.split('\n') if old else []
        new_lines = new.split('\n') if new else []
        
        result = []
        result.append(f"--- old ({len(old_lines)} lines)")
        result.append(f"+++ new ({len(new_lines)} lines)")
        
        # 简单的行级 diff
        max_lines = max(len(old_lines), len(new_lines))
        for i in range(max_lines):
            old_line = old_lines[i] if i < len(old_lines) else None
            new_line = new_lines[i] if i < len(new_lines) else None
            
            if old_line == new_line:
                continue
            elif old_line is None:
                result.append(f"+ {i+1}: {new_line}")
            elif new_line is None:
                result.append(f"- {i+1}: {old_line}")
            else:
                result.append(f"~ {i+1}:")
                result.append(f"  - {old_line}")
                result.append(f"  + {new_line}")
        
        return '\n'.join(result) if result else "No changes"

    # ── 批量修复 ─────────────────────────────────────

    def fix_nodes_by_type(self, flow_id: str, node_type: str,
                           fix_fn: Callable[[Dict], bool]) -> int:
        """按节点类型批量修复节点"""
        flow = self.get_flow(flow_id)
        count = 0
        for n in flow.get("nodes", []):
            if n.get("type") == node_type:
                try:
                    if fix_fn(n):
                        count += 1
                except Exception as e:
                    print(f"  ⚠  fix node {n.get('name','?')}[{n.get('id','?')[:12]}]: {e}")
        if count:
            self.update_flow(flow_id, flow)
            _log_operation("FIX_NODES", f"flow={flow_id} | type={node_type} | count={count}")
        return count

    def fix_api_current_state_nodes(self, flow_id: str,
                                     entity_state_type: str = "str") -> int:
        """修复 NR 5.0 下 api-current-state 节点的格式兼容问题"""
        CORRECT_OUT_PROPS = [
            {'property': 'payload', 'propertyType': 'msg', 'value': 'string', 'valueType': 'entityState'},
            {'property': 'data', 'propertyType': 'msg', 'value': 'string', 'valueType': 'entity'}
        ]

        def fix_api_node(n: dict) -> bool:
            out_props = n.get('outputProperties', [])
            has_es = any(p.get('valueType') == 'entityState' for p in out_props)
            if has_es:
                return False
            n['outputs'] = 1
            n['outputProperties'] = CORRECT_OUT_PROPS
            n['version'] = 3
            n['halt_if'] = ''
            n['halt_if_type'] = 'str'
            n['halt_if_compare'] = 'is'
            n['state_type'] = entity_state_type
            n['state_location'] = 'payload'
            n['override_payload'] = 'msg'
            n['override_topic'] = False
            n['blockInputOverrides'] = True
            n['for'] = '0'
            n['forType'] = 'num'
            n['forUnits'] = 'minutes'
            if 'entities' in n and 'entity_id' not in n:
                ents = n.get('entities', [])
                if ents and isinstance(ents, list) and len(ents) > 0:
                    e = ents[0]
                    n['entity_id'] = e.get('entity_id', '') if isinstance(e, dict) else str(e)
                del n['entities']
            return True

        return self.fix_nodes_by_type(flow_id, 'api-current-state', fix_api_node)

    # ── 节点构建器 (v6 schema 强制) ──────────────────────

    def build_server_state_changed(self, node_id: str, flow_id: str,
                                    entity_id: str, name: str = "",
                                    server: str = "",
                                    outputs: int = 1,
                                    wires: list = None) -> Dict:
        """构造 server-state-changed 节点 (v6 schema)"""
        if not server:
            server = self._get_default_server()
        return {
            "id": node_id,
            "type": "server-state-changed",
            "z": flow_id,
            "name": name or f"📡 {entity_id}",
            "server": server,
            "version": 6,
            "outputs": outputs,
            "exposeAsEntityConfig": "",
            "entities": {
                "entity": [entity_id],
                "substring": [],
                "regex": []
            },
            "outputInitially": False,
            "stateType": "str",
            "ifState": "",
            "ifStateType": "str",
            "ifStateOperator": "is",
            "outputOnlyOnStateChange": False,
            "for": "0",
            "forType": "num",
            "forUnits": "minutes",
            "ignorePrevStateNull": False,
            "ignorePrevStateUnknown": False,
            "ignorePrevStateUnavailable": False,
            "ignoreCurrentStateUnknown": False,
            "ignoreCurrentStateUnavailable": False,
            "outputProperties": [],
            "entityId": entity_id,
            "wires": wires or []
        }

    def build_api_current_state(self, node_id: str, flow_id: str,
                                 entity_id: str, name: str = "",
                                 server: str = "",
                                 halt_if: str = "",
                                 halt_if_type: str = "str",
                                 halt_if_compare: str = "is",
                                 wires: list = None) -> Dict:
        """构造 api-current-state 节点 (v3 schema)"""
        if not server:
            server = self._get_default_server()
        outputs = 2 if halt_if else 1
        return {
            "id": node_id,
            "type": "api-current-state",
            "z": flow_id,
            "name": name or f"🔍 {entity_id}",
            "server": server,
            "version": 3,
            "outputs": outputs,
            "halt_if": halt_if,
            "halt_if_type": halt_if_type,
            "halt_if_compare": halt_if_compare,
            "entity_id": entity_id,
            "state_type": "str",
            "blockInputOverrides": True,
            "outputProperties": [
                {"property": "payload", "propertyType": "msg",
                 "value": "", "valueType": "entityState"},
                {"property": "data", "propertyType": "msg",
                 "value": "", "valueType": "entity"}
            ],
            "for": "0",
            "forType": "num",
            "forUnits": "minutes",
            "override_topic": False,
            "state_location": "payload",
            "override_payload": "msg",
            "entity_location": "data",
            "override_data": "msg",
            "wires": wires or [[], []]
        }

    def build_link_out(self, node_id: str, flow_id: str,
                        link_in_id: str, name: str = "",
                        mode: str = "link") -> Dict:
        """构造 link out 节点"""
        return {
            "id": node_id,
            "type": "link out",
            "z": flow_id,
            "name": name or "→ " + link_in_id[:8],
            "mode": mode,
            "links": [link_in_id],
            "wires": []
        }

    def build_function(self, node_id: str, flow_id: str,
                        func_code: str, name: str = "",
                        outputs: int = 1,
                        wires: list = None) -> Dict:
        """构造 function 节点"""
        return {
            "id": node_id,
            "type": "function",
            "z": flow_id,
            "name": name,
            "func": func_code,
            "outputs": outputs,
            "timeout": "",
            "noerr": 0,
            "initialize": "",
            "finalize": "",
            "libs": [],
            "wires": wires or []
        }

    def build_debug(self, node_id: str, flow_id: str, name: str = "",
                     complete: bool = True) -> Dict:
        """构造 debug 节点"""
        return {
            "id": node_id,
            "type": "debug",
            "z": flow_id,
            "name": name or "debug",
            "active": True,
            "tosidebar": True,
            "console": False,
            "tostatus": False,
            "complete": "true" if complete else "false",
            "targetType": "full",
            "statusVal": "",
            "statusType": "auto",
            "wires": []
        }

    def build_inject(self, node_id: str, flow_id: str, name: str = "",
                      payload_type: str = "date",
                      repeat: str = "", crontab: str = "",
                      wires: list = None) -> Dict:
        """构造 inject 节点"""
        return {
            "id": node_id,
            "type": "inject",
            "z": flow_id,
            "name": name or "inject",
            "props": [
                {"p": "payload"},
                {"p": "topic", "vt": "str"}
            ],
            "repeat": repeat,
            "crontab": crontab,
            "once": False,
            "onceDelay": 0.1,
            "topic": "",
            "payload": "",
            "payloadType": payload_type,
            "wires": wires or []
        }

    def _get_default_server(self) -> str:
        """从 settings 推断默认 HA server 节点 ID。

        HA server 是顶层 config 节点（type=="server"），并不挂在任何 flow 的 nodes 下，
        因此既要检查顶层条目本身，也要检查各 flow 的子节点。
        """
        try:
            flows = self.list_flows()
            for f in flows:
                if f.get("type") == "server":
                    return f.get("id", "")
                for n in f.get("nodes", []):
                    if n.get("type") == "server":
                        return n.get("id", "")
        except Exception:
            pass
        return ""

    # ── 连线操作 ─────────────────────────────────────

    def get_wire_map(self, flow_id: str) -> Dict[str, List[str]]:
        """获取 flow 的完整连线映射"""
        flow = self.get_flow(flow_id)
        wire_map: Dict[str, List[str]] = {}
        for n in flow.get("nodes", []):
            wires = n.get("wires", [])
            if not wires:
                continue
            targets = wires[0] if isinstance(wires[0], list) else wires
            wire_map[n["id"]] = targets
        return wire_map

    def add_wire(self, flow_id: str, src_id: str, tgt_id: str, src_output: int = 0) -> Dict:
        """在现有 flow 中添加一条连线"""
        flow = self.get_flow(flow_id)
        for n in flow.get("nodes", []):
            if n["id"] == src_id:
                wires = n.get("wires", [])
                if not wires:
                    n["wires"] = [[]]
                if isinstance(n["wires"][0], list):
                    targets = n["wires"][0]
                else:
                    targets = []
                    n["wires"] = [targets]
                if tgt_id not in targets:
                    targets.append(tgt_id)
                break
        result = self.update_flow(flow_id, flow)
        _log_operation("ADD_WIRE", f"flow={flow_id} | {src_id} → {tgt_id}")
        return result

    def remove_wire(self, flow_id: str, src_id: str, tgt_id: str) -> Dict:
        """删除一条连线"""
        flow = self.get_flow(flow_id)
        for n in flow.get("nodes", []):
            if n["id"] == src_id:
                wires = n.get("wires", [])
                if wires:
                    targets = wires[0] if isinstance(wires[0], list) else wires
                    if tgt_id in targets:
                        targets.remove(tgt_id)
                break
        result = self.update_flow(flow_id, flow)
        _log_operation("REMOVE_WIRE", f"flow={flow_id} | {src_id} ✕ {tgt_id}")
        return result

    def reconnect_node(self, flow_id: str, node_id: str, new_wires: List[str]) -> Dict:
        """重连节点的所有输出线"""
        flow = self.get_flow(flow_id)
        for n in flow.get("nodes", []):
            if n["id"] == node_id:
                n["wires"] = [new_wires]
                break
        result = self.update_flow(flow_id, flow)
        _log_operation("RECONNECT_NODE", f"flow={flow_id} | node={node_id} → {new_wires}")
        return result

    # ── 修改节点 ─────────────────────────────────────

    def modify_function_code(self, flow_id: str, node_id: str,
                              code: str, name: str = None,
                              dry_run: bool = False) -> Dict:
        """
        修改 function 节点代码并部署
        
        dry_run: 仅预览 diff，不实际修改
        """
        flow = self.get_flow(flow_id)
        
        for n in flow.get("nodes", []):
            if n["id"] == node_id:
                old_code = n.get("func", "")
                old_name = n.get("name", "")
                
                # diff 预览
                diff = self._diff_code(old_code, code)
                
                if dry_run:
                    return {
                        "dry_run": True,
                        "node_id": node_id,
                        "old_name": old_name,
                        "diff": diff,
                        "old_lines": len(old_code.split('\n')) if old_code else 0,
                        "new_lines": len(code.split('\n')) if code else 0
                    }
                
                # 实际修改
                n["func"] = code
                if name:
                    n["name"] = name
                
                self.update_flow(flow_id, flow)
                
                # 记录日志
                lines_info = f"+{len(code.split(chr(10))) if code else 0}/-{len(old_code.split(chr(10))) if old_code else 0}"
                _log_operation("MODIFY_FUNCTION", f"flow={flow_id} | node={node_id} | lines={lines_info}")
                
                return {
                    "success": True,
                    "node_id": node_id,
                    "diff": diff
                }
        
        raise RuntimeError(f"Node {node_id} not found in flow {flow_id}")

    def modify_node_field(self, flow_id: str, node_id: str,
                           field_updates: Dict) -> Dict:
        """修改节点任意字段并部署"""
        flow = self.get_flow(flow_id)
        for n in flow.get("nodes", []):
            if n["id"] == node_id:
                n.update(field_updates)
                break
        else:
            raise RuntimeError(f"Node {node_id} not found in flow {flow_id}")
        
        result = self.update_flow(flow_id, flow)
        _log_operation("MODIFY_NODE", f"flow={flow_id} | node={node_id} | fields={list(field_updates.keys())}")
        return result

    def add_nodes(self, flow_id: str, new_nodes: List[Dict]) -> Dict:
        """向 flow 追加新节点"""
        flow = self.get_flow(flow_id)
        old_count = len(flow.get("nodes", []))
        flow.setdefault("nodes", []).extend(new_nodes)
        result = self.update_flow(flow_id, flow)
        _log_operation("ADD_NODES", f"flow={flow_id} | nodes +{len(new_nodes)} | total {old_count} → {old_count + len(new_nodes)}")
        return result

    def find_and_replace(self, flow_id: str, old_str: str, new_str: str,
                          fields: List[str] = None) -> int:
        """在 flow 的所有节点中搜索并替换字符串"""
        if fields is None:
            fields = ["name", "func", "topic"]
        flow = self.get_flow(flow_id)
        count = 0
        for n in flow.get("nodes", []):
            changed = False
            for f in fields:
                if f in n and isinstance(n[f], str) and old_str in n[f]:
                    n[f] = n[f].replace(old_str, new_str)
                    changed = True
            if changed:
                count += 1
        if count:
            self.update_flow(flow_id, flow)
            _log_operation("FIND_REPLACE", f"flow={flow_id} | '{old_str}' → '{new_str}' | count={count}")
        return count

    # ── 特殊入口查找 ─────────────────────────────────

    def get_tts_queue_flow(self) -> Optional[Dict]:
        """查找 TTS 队列 flow"""
        for f in self.find_flows(lambda x: "TTS" in (x.get("label") or "")):
            return f
        return None

    def get_link_in_flow(self) -> Optional[Dict]:
        """查找所有 link-in 节点所在的 flow"""
        results = self.find_nodes_all(lambda n: n.get("type") == "link in")
        if results:
            return results[0]
        return None

    # ── 注入 / 触发 ─────────────────────────────────

    def inject_flow(self, flow_id: str) -> None:
        """触发 flow 中所有 inject 节点（用于调试）"""
        flow = self.get_flow(flow_id)
        injects = [n for n in flow.get("nodes", []) if n.get("type") == "inject"]
        for n in injects:
            print(f"  inject: {n.get('name', n['id'][:8])}")

    def trigger_inject(self, node_id: str) -> int:
        """真实触发单个 inject 节点（端到端验证用）。返回 HTTP 状态码。

        注意：NR 触发成功时返回空 body，故用 _request 而非 _json（免 JSONDecode 报错）。
        """
        resp = self._request("POST", f"/inject/{node_id}")
        return resp.status_code

    # ── 端到端验证（e2e verify）─────────────────────────
    # HA 连接（与 homeassistant-kai-dai/ha_client.py 保持一致，零外部依赖）
    _HA_SERVER = os.getenv("HASS_SERVER") or "http://<NAS_IP>:8123"
    # ★S-3 安全修复：假 JWT 占位符默认值改为空字符串，未配置时拒绝鉴权请求（不泄漏 JWT 结构）
    _HA_TOKEN = os.getenv("HASS_TOKEN") or ""

    def _ha_req(self, path: str, method: str = "GET", payload: Any = None) -> str:
        url = f"{self._HA_SERVER}/api/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Bearer {self._HA_TOKEN}",
                     "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", "replace") or "{}"
        except Exception as e:
            return f"ERR:{e}"

    def ha_get_state(self, entity_id: str) -> str:
        """只读查询 HA 实体状态（验证断言用）。"""
        raw = self._ha_req(f"states/{entity_id}")
        try:
            return json.loads(raw).get("state", raw[:80])
        except Exception:
            return raw[:80]

    def verify_flow(self, flow_id: str, inject_node: Optional[str] = None,
                    wait: int = 5, expect: Optional[List[str]] = None,
                    cleanup: Optional[List[str]] = None) -> Dict:
        """端到端验证：触发 flow 的 inject 节点 → 等待 → 收集 NR 节点 error →
        可选 HA 断言 → 可选清理。

        flow_id   : flow 的 tab id 或 label
        inject_node : 仅触发该 inject 节点（id 或 name）；缺省触发全部
        expect    : list["entity_id:state"]，触发后对每个实体查 HA 状态做断言
        cleanup   : list[entity_id]，验证后调 light.turn_off 恢复
        返回结构化报告 dict（ok / triggered / node_errors / ha_assertions / cleanup）。
        """
        flows = self._json("GET", "/flows")
        tab = next((f for f in flows
                    if f.get("type") == "tab"
                    and (f.get("id") == flow_id or f.get("label") == flow_id)), None)
        if not tab:
            return {"ok": False, "error": f"flow 未找到: {flow_id}"}
        fid = tab["id"]
        nodes = [n for n in flows if n.get("z") == fid]
        injects = [n for n in nodes if n.get("type") == "inject"]
        if inject_node:
            targets = [n for n in injects
                       if n["id"] == inject_node or n.get("name") == inject_node]
            if not targets:
                return {"ok": False, "error": f"inject 节点未找到: {inject_node}"}
        else:
            targets = injects

        triggered = []
        for n in targets:
            code = self.trigger_inject(n["id"])
            triggered.append({"id": n["id"], "name": n.get("name"), "http": code})

        if wait:
            time.sleep(wait)

        flows2 = self._json("GET", "/flows")
        nodes2 = [n for n in flows2 if n.get("z") == fid]
        errors = []
        for n in nodes2:
            s = n.get("status")
            if s:
                txt = json.dumps(s, ensure_ascii=False) if isinstance(s, dict) else str(s)
                if any(k in txt.lower() for k in ("error", "red", "ring")):
                    errors.append({"id": n["id"], "name": n.get("name"), "status": s})

        ha_assertions = []
        for e in (expect or []):
            ent, _, st = e.partition(":")
            ent, st = ent.strip(), st.strip()
            cur = self.ha_get_state(ent)
            ha_assertions.append({"entity": ent, "expect": st,
                                  "actual": cur, "pass": cur == st})

        cleanup_res = []
        for ent in (cleanup or []):
            r = self._ha_req("services/light/turn_off", "POST", {"entity_id": ent})
            cleanup_res.append({"entity": ent, "result": r[:120]})

        return {
            "ok": True,
            "flow_id": fid,
            "label": tab.get("label"),
            "triggered": triggered,
            "node_errors": errors,
            "ha_assertions": ha_assertions,
            "cleanup": cleanup_res,
        }

    # ── 备份 ─────────────────────────────────────────

    def dump_flow(self, flow_id: str, outfile: str = None) -> Dict:
        """读取并可选保存到文件"""
        data = self.get_flow(flow_id)
        if outfile:
            with open(outfile, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        return data

    def dump_all_flows(self, outdir: str = ".") -> int:
        """备份所有 flows 到指定目录"""
        os.makedirs(outdir, exist_ok=True)
        count = 0
        for f in self.list_flows():
            fid = f.get("id", "?")
            label = f.get("label", fid).replace("/", "_").replace("\\", "_")
            out = os.path.join(outdir, f"{fid[:12]}_{label}.json")
            try:
                self.dump_flow(fid, out)
                count += 1
            except Exception as e:
                print(f"  ⚠ {fid}: {e}")
        _log_operation("BACKUP_ALL", f"count={count} | dir={outdir}")
        return count

    # ─────────────────────────────────────────────────
    #  内部标准化（部署前自动调用）
    # ─────────────────────────────────────────────────

    @staticmethod
    def _normalize_inject(n: dict):
        """标准化 inject 节点：crontab → str，repeat 清理"""
        ct = n.get("crontab")
        if ct is not None:
            if isinstance(ct, dict):
                n["crontab"] = str(ct.get("expression", ""))
            elif isinstance(ct, str):
                s = ct.strip()
                if s.startswith("{"):
                    try:
                        parsed = json.loads(s)
                        n["crontab"] = str(parsed.get("expression", ""))
                    except Exception:
                        pass
            else:
                n["crontab"] = str(ct)
            if n["crontab"]:
                n["repeat"] = ""
        rp = n.get("repeat")
        if rp is not None and rp != "" and not isinstance(rp, (str, bool)):
            n["repeat"] = ""

    @staticmethod
    def _extract_entity_id(n: dict):
        """从 api-* 节点抽取实体 id，兼容三种写法：

        - ``entityId``(camelCase 字符串/数组, NR5/v7 契约)
        - ``entity_id``(snake_case, 旧版 0.80.3 部分节点)
        - ``entities``(legacy 数组)

        返回单个实体字符串；找不到返回 ''。
        """
        eid = n.get("entityId")
        if isinstance(eid, list):
            eid = eid[0] if eid else ""
        if isinstance(eid, str) and eid.strip():
            return eid
        eid = n.get("entity_id")
        if isinstance(eid, str) and eid.strip():
            return eid
        ents = n.get("entities")
        if isinstance(ents, dict):
            # trigger-state / server-state-changed v5/v6 形态：
            # entities.entity 是字符串数组（或单字符串）
            ev = ents.get("entity")
            if isinstance(ev, list):
                for x in ev:
                    if isinstance(x, str) and x.strip():
                        return x
            elif isinstance(ev, str) and ev.strip():
                return ev
        elif isinstance(ents, list):
            for e in ents:
                v = e.get("entity_id") or e.get("entityId") if isinstance(e, dict) else e
                if isinstance(v, str) and v.strip():
                    return v
        return ""

    @staticmethod
    def _normalize_api_state(n: dict):
        """标准化 api-current-state 节点（兼容 NR5/v7 与 0.80.3 字段名错配）。

        关键修复（entityId 静默丢失 bug，详见
        docs/autoflow_study_scene_entityid_report.md）：原实现只从 legacy
        ``entities`` 数组抽 ``entity_id``，完全无视 ``entityId``(camelCase) 字符串；
        且写死 ``version=3``，而 v3 读 ``entity_id`` 而非 ``entityId`` → 白箱交
        ``{entityId:"x"}`` 归一到 v3 后绑定丢失（0.80.3 的旧版读法读不到）。

        现改为：无论写成 entityId / entity_id / entities，都把实体绑定落到
        **BOTH** 字段（entityId=camelCase + entity_id=snake_case），并归一到
        ``version=7``（与编译器契约一致），确保落 0.80.3 后绑定不丢，且不会因
        R19「同时含两字段」警告而阻断（R19 仅非阻塞告警）。

        门(gate)语义：outputs>=2（pass/fail 双输出）且 halt_if 非空 →
        这是路由门，必须保留双输出与 wires 结构，绝不改写为单输出。
        否则会触发 R10 结构 lint（_lint_flows）+ NR「第 2 数组目标永不触发」反模式。
        """
        eid = NodeRedClient._extract_entity_id(n)
        # 门节点：保留原结构，仅 setdefault 补全 halt 字段（不覆盖 compile 已设值）
        if int(n.get('outputs', 1)) >= 2 and n.get('halt_if') not in (None, ''):
            for k, v in [('halt_if_type', 'str'), ('halt_if_compare', 'is'),
                          ('state_type', 'str'), ('state_location', 'payload'),
                          ('override_payload', 'msg'), ('override_topic', False),
                          ('blockInputOverrides', True), ('for', '0'), ('forType', 'num'),
                          ('forUnits', 'minutes'), ('version', 7)]:
                n.setdefault(k, v)
            if eid:
                n['entityId'] = eid
                n['entity_id'] = eid
            n.pop('entities', None)
            return
        # 普通读取节点：单输出，确保 outputs=1 + 标准 outputProperties
        out_props = n.get('outputProperties', [])
        has_es = any(p.get('valueType') == 'entityState' for p in out_props)
        if has_es:
            n['outputs'] = 1
            if eid:
                n['entityId'] = eid
                n['entity_id'] = eid
            n.pop('entities', None)
            return
        n['outputs'] = 1
        n['outputProperties'] = [
            {'property': 'payload', 'propertyType': 'msg', 'value': 'string', 'valueType': 'entityState'},
            {'property': 'data', 'propertyType': 'msg', 'value': 'string', 'valueType': 'entity'}
        ]
        for k, v in [('halt_if', ''), ('halt_if_type', 'str'), ('halt_if_compare', 'is'),
                      ('state_type', 'str'), ('state_location', 'payload'),
                      ('override_payload', 'msg'), ('override_topic', False),
                      ('blockInputOverrides', True), ('for', '0'), ('forType', 'num'),
                      ('forUnits', 'minutes'), ('version', 7)]:
            n.setdefault(k, v)
        if eid:
            n['entityId'] = eid
            n['entity_id'] = eid
        n.pop('entities', None)

    @staticmethod
    def _normalize_api_call_service(n: dict):
        """标准化 api-call-service 节点（修复 entityId 静默丢失 bug）。

        ha-websocket v6+ 的 api-call-service 用 ``entityId`` **数组**；白箱 agent
        可能写老 v5 形态（entityId 为**字符串**）或漏写。原 ``_normalize_flow``
        完全不处理该类型，字符串 entityId 原样交给 0.80.3 迁移 → 被升 v7 但值
        丢进空数组。现归一成 ``version=7`` + 数组 ``entityId``（与编译器契约一致），
        确保绑定不丢。

        ★ action 契约补全（WB72 Bug#1 / #705，P0）：
          v5 形态用 ``domain`` + ``service`` 两字段表达调用；v7 改用单字段
          ``action="<domain>.<service>"``。此前本函数**只升版不补 action**，
          于是 `{version:5, domain:"light", service:"turn_on"}` 被升成 v7 却
          没有 action → NR 运行时 ValidationError（"action" is required），
          而网关 schema 校验（gateway.validate_flow_schema）只要求
          「action 或 domain 二选一」，恰好放行 → 静态全绿、运行必炸。
          **升版即补全**：能从 domain+service 推出 action 就补上；只给 action
          的 v7 写法则反向补 domain/service（保持两种读法都成立，与编译器
          dsl_engine.py 发射的节点形态一致）。推不出 action 时不强行升版，
          让 schema 校验去点名，避免制造新的「v7 无 action」非法节点。
        """
        eid = n.get("entityId")
        if isinstance(eid, str):
            n["entityId"] = [eid]
        # action ⇄ domain/service 补全（编译器契约 dsl_engine.py:2148）
        # 修复：只正向补全 action（v7 必需），不反向补全 domain/service。
        # 之前双向补全会改写用户手动添加的 api-call-service 节点（如「💾 存档」节点
        # 的空 domain/service 被补为 input_text/set_value），属部署副作用。
        # v7 格式只需要 action 字段，domain/service 是可选的，反向补全非必须。
        action = n.get("action")
        action = action.strip() if isinstance(action, str) else ""
        domain = n.get("domain")
        domain = domain.strip() if isinstance(domain, str) else ""
        service = n.get("service")
        service = service.strip() if isinstance(service, str) else ""
        if not action and domain and service:
            action = f"{domain}.{service}"
            n["action"] = action
        # 不再反向补全 domain/service：避免改写用户节点的原始字段
        # 版本归一到 v7（编译器契约 / 0.80.3 数组 entityId 形态）
        # 仅在 action 已确定时升版：v7 强依赖 action，缺 action 还升版 = 造非法节点。
        ver = n.get("version")
        if action and ver in (None, 0, 1, 2, 3, 4, 5, 6):
            n["version"] = 7

    @staticmethod
    def _normalize_entities_obj_node(n: dict, version: int):
        """trigger-state / server-state-changed 共用归一化。

        这两类节点最新版用 ``entities = {entity:[..], substring:[..], regex:[..]}``
        承载实体绑定；旧版(v1)用顶层 ``entityId``(字符串) + ``entityidfiltertype``。
        若网关不主动归一到 v5/v6，NR 导入时 v1→vN 迁移会把 ``entityId`` 写成
        ``entities.entity`` 却**不搬运值** → 变 ``[null]``（见
        docs/autoflow_entityid_probe_v4_reverify_report.md 的 G 节点回归）。
        这里主动归一到最新版 + entities.entity 填实，NR 无版本可升、无值可丢。
        """
        # 已是最新版且 entities.entity 已填实 → 不动（保留用户原结构）
        ents = n.get("entities")
        if isinstance(ents, dict) and isinstance(ents.get("entity"), list) \
                and any(isinstance(x, str) and x.strip() for x in ents["entity"]):
            n["version"] = version
            return
        eid = NodeRedClient._extract_entity_id(n)
        if not eid:
            return  # 真没实体：不动，交给校验层报空（防误改）
        filt = (n.get("entityidfiltertype") or "exact").lower()
        bucket = {"exact": "entity", "substring": "substring",
                  "regex": "regex"}.get(filt, "entity")
        entities = {"entity": [], "substring": [], "regex": []}
        entities[bucket] = [eid]
        n["entities"] = entities
        n["version"] = version
        # 清掉 v1 残留，避免歧义 / 被迁移再改写
        n.pop("entityId", None)
        n.pop("entityidfiltertype", None)
        n.pop("entity", None)
        # 补全 v5/v6 必备字段（setdefault，不覆盖已有值）
        for k, v in [("outputs", 1), ("exposeAsEntityConfig", ""),
                     ("constraints", []), ("customOutputs", []),
                     ("outputInitially", False), ("stateType", "str"),
                     ("enableInput", False), ("debugEnabled", True)]:
            n.setdefault(k, v)

    @staticmethod
    def _normalize_trigger_state(n: dict):
        """trigger-state 归一到 v5（entities.entity 填实）。"""
        NodeRedClient._normalize_entities_obj_node(n, 5)

    @staticmethod
    def _normalize_server_state_changed(n: dict):
        """server-state-changed 归一到 v6（entities.entity 填实）。"""
        NodeRedClient._normalize_entities_obj_node(n, 6)

    @staticmethod
    def _normalize_entity_id_str_node(n: dict):
        """api-get-history / poll-state / events-state / wait-until 共用保底。

        这些节点跨版本**字段名稳定**（始终用顶层 ``entityId`` 字符串），不会
        发生 entityId→entities 改名丢值；故只需保底：确保 ``entityId`` 是非空
        字符串、清掉 ``entity_id``/``entity``/``entities`` 等旧写法残留，
        **不强制改 version**（避免猜错最新版本号反而引入问题）。
        """
        eid = NodeRedClient._extract_entity_id(n)
        if not eid:
            return  # 真没实体：不动
        n["entityId"] = eid
        n.pop("entity_id", None)
        n.pop("entity", None)
        n.pop("entities", None)

    @staticmethod
    def _normalize_flow(flow_data: Dict) -> None:
        """标准化高风险字段，防止 NR 崩溃（NR 5.x 兼容）"""
        # legacy→NR5 形状对齐（Bug B 修复）：
        # deploy_raw 等网关路径产出 {id, label, nodes:[tab, children...]}（tab 嵌在 nodes 里），
        # 但 NR 5.x 的 POST/PUT /flow 只认 NR5 形状 {id, type:"tab", label, nodes:[children...]}
        # （tab 作为根对象）。不转换则 POST /flow → 400「invalid node type: tab」，
        # 或 PUT /flow/:id 返回 200 却静默丢弃全部子节点。这里把内嵌 tab 提升为根、
        # 其余子节点留在 nodes，与 NR5 契约对齐。
        if isinstance(flow_data, dict) and flow_data.get("type") != "tab":
            _nodes = flow_data.get("nodes")
            if isinstance(_nodes, list):
                _tabs = [n for n in _nodes
                         if isinstance(n, dict) and n.get("type") == "tab"]
                if _tabs:
                    _tab = _tabs[0]
                    flow_data["id"] = _tab.get("id", flow_data.get("id"))
                    flow_data["type"] = "tab"
                    if _tab.get("label"):
                        flow_data["label"] = _tab.get("label")
                    flow_data["nodes"] = [
                        n for n in _nodes
                        if not (isinstance(n, dict) and n.get("type") == "tab")
                    ]
        NodeRedClient._normalize_config_node(flow_data)
        for n in flow_data.get("nodes", []):
            if n.get("type") == "inject":
                NodeRedClient._normalize_inject(n)
            elif n.get("type") == "api-current-state":
                NodeRedClient._normalize_api_state(n)
            elif n.get("type") == "api-call-service":
                NodeRedClient._normalize_api_call_service(n)
            elif n.get("type") == "trigger-state":
                NodeRedClient._normalize_trigger_state(n)
            elif n.get("type") == "server-state-changed":
                NodeRedClient._normalize_server_state_changed(n)
            elif n.get("type") in ("api-get-history", "poll-state",
                                   "events-state", "wait-until"):
                NodeRedClient._normalize_entity_id_str_node(n)

    @staticmethod
    def _normalize_config_node(obj: Dict) -> None:
        """递归确保 obj 中所有 config 字段都是数组（NR 5.x config.forEach 兼容）"""
        cfg = obj.get("config")
        if cfg is not None and not isinstance(cfg, list):
            obj["config"] = [cfg]
        for n in obj.get("nodes", []):
            NodeRedClient._normalize_config_node(n)
        for k, v in list(obj.items()):
            if k in ("id", "type", "name", "config", "nodes", "wires", "label"):
                continue
            if isinstance(v, dict):
                NodeRedClient._normalize_config_node(v)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                for item in v:
                    if isinstance(item, dict):
                        NodeRedClient._normalize_config_node(item)

    def validate_flow(self, flow_data: Dict) -> List[str]:
        """校验 flow，返回警告列表（空=正常）"""
        warnings = []
        for n in flow_data.get("nodes", []):
            t   = n.get("type", "")
            nid = n.get("id", "?")
            if t == "inject":
                ct = n.get("crontab")
                if ct is not None and not isinstance(ct, str):
                    warnings.append(f"inject[{nid[:8]}] crontab 应为 str，实际为 {type(ct).__name__}")
            if t == "api-call-service":
                if not n.get("domain"):
                    warnings.append(f"api-call-service[{nid[:8]}] 缺少 domain")
                if not n.get("service"):
                    warnings.append(f"api-call-service[{nid[:8]}] 缺少 service")
            wires = n.get("wires", [])
            if not wires and n.get("type") not in ("comment", "unknown", "debug"):
                warnings.append(f"node[{nid[:8]}] type={t} 无输出 wires")
        return warnings


# ── 自动同步：import 时对齐权威源 ────────────────────
# 权威源（autoflow lib fork）被 import 时自登记；其余副本 import 时若落后则自动拉取最新版。
ensure_latest(verbose=True)

# ─── CLI ──────────────────────────────────────────────────

def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Node-RED Admin CLI (安全增强版)")
    sp = p.add_subparsers(dest="cmd")

    sp.add_parser("login", help="测试登录")
    sp.add_parser("flows", help="列出所有 flows")
    sp.add_parser("stats", help="节点类型统计")
    g = sp.add_parser("get", help="获取单个 flow")
    g.add_argument("flow_id")

    s = sp.add_parser("search", help="全局搜索节点")
    s.add_argument("keyword", nargs="?", default="")

    w = sp.add_parser("wires-to", help="查找指向某节点的连线")
    w.add_argument("node_id")

    sp.add_parser("inject", help="列出 flow 中的 inject 节点（不触发）")

    v = sp.add_parser("verify", help="端到端验证 flow：触发 inject + 观测节点 + 可选 HA 断言")
    v.add_argument("flow_id", help="flow id 或 label")
    v.add_argument("--inject", "-i", help="只触发指定 inject 节点 id 或 name")
    v.add_argument("--wait", "-w", type=int, default=5, help="触发后等待秒数")
    v.add_argument("--expect", "-e", action="append", default=[], help="HA 断言 entity_id:state（可重复）")
    v.add_argument("--cleanup", "-c", action="append", default=[], help="验证后 turn_off 的实体（可重复）")
    v.add_argument("--yes", action="store_true", help="确认真实触发（否则仅 dry-run 只读预览）")
    sp.add_parser("settings", help="运行时设置")
    sp.add_parser("nodes", help="已安装节点")
    sp.add_parser("backup", help="备份所有 flows 到 ./nr_backup/")

    sp.add_parser("version", help="打印 nr_client 版本与权威源信息")
    sp.add_parser("sync", help="立即拉取权威源最新版")
    sp.add_parser("check", help="审计各副本版本一致性")
    l = sp.add_parser("lint", help="离线 lint 一个 flow JSON（结构校验，不连 NR）")
    l.add_argument("flow_file", help="flow JSON 路径（{nodes:[...]} 或扁平 [..]）")

    u = sp.add_parser("update-code", help="修改 function 代码")
    u.add_argument("flow_id")
    u.add_argument("node_id")
    u.add_argument("--code", "-c")
    u.add_argument("--file", "-f")
    u.add_argument("--name", "-n")
    u.add_argument("--dry-run", action="store_true", help="仅预览 diff，不实际修改")

    fr = sp.add_parser("find-replace", help="批量替换字符串")
    fr.add_argument("flow_id")
    fr.add_argument("old")
    fr.add_argument("new")

    fix = sp.add_parser("fix-api-nodes", help="修复 api-current-state（NR 5.0 兼容）")
    fix.add_argument("flow_id")

    fix_generic = sp.add_parser("fix-nodes", help="通用批量修复节点")
    fix_generic.add_argument("flow_id")
    fix_generic.add_argument("node_type")
    fix_generic.add_argument("fix_expr", help="修复表达式，如 'n[\"outputs\"] = 1'")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return

    nr = NodeRedClient()

    try:
        if args.cmd == "login":
            t = nr.login()
            print(f"✅ 登录成功, token: {t[:20]}...")

        elif args.cmd == "flows":
            flows = nr.list_flows()
            for f in flows:
                label  = f.get("label") or f.get("id", "?")
                nnodes = len(f.get("nodes", []))
                print(f"  [{f.get('type', 'flow'):8s}] {label} ({f['id']}) — {nnodes} nodes")
            print(f"\nTotal: {len(flows)} flows")

        elif args.cmd == "stats":
            stats = nr.get_node_type_stats()
            for t, c in stats.items():
                print(f"  {c:4d}  {t}")

        elif args.cmd == "get":
            flow = nr.get_flow(args.flow_id)
            nodes = flow.get("nodes", [])
            print(f"Flow: {flow.get('label','?')} ({flow['id']}) — {len(nodes)} nodes")
            for n in nodes:
                name = n.get("name") or n.get("label") or ""
                print(f"  [{n['type']}] [{n['id'][:12]}] {name}")

        elif args.cmd == "search":
            kw = args.keyword.lower()
            results = nr.find_nodes_all(
                lambda n: kw in (n.get("name") or "").lower()
                or kw in (n.get("func") or "").lower()
                or kw in (n.get("type") or "").lower()
            )
            for fid, flabel, n in results:
                print(f"  {flabel} / {n.get('name') or n.get('type','?')} [{n['id'][:12]}]")
            print(f"\nTotal: {len(results)} matches")

        elif args.cmd == "wires-to":
            sources = nr.find_wires_to(args.node_id)
            if not sources:
                print(f"  No wires targeting {args.node_id[:12]}")
            for s in sources:
                print(f"  [{s['type']}] [{s['id'][:12]}] {s.get('name','?')}")

        elif args.cmd == "inject":
            results = nr.find_nodes_all(lambda n: n.get("type") == "inject")
            for fid, flabel, n in results:
                crontab = n.get("crontab") or ""
                print(f"  {flabel} / {n.get('name','?')} [{n['id'][:12]}] cron={repr(crontab)[:40]}")

        elif args.cmd == "verify":
            if not args.yes:
                # dry-run：只读预览，不触发
                flows = nr._json("GET", "/flows")
                tab = next((f for f in flows
                            if f.get("type") == "tab"
                            and (f.get("id") == args.flow_id or f.get("label") == args.flow_id)), None)
                if not tab:
                    print("flow 未找到:", args.flow_id); return
                nodes = [n for n in flows if n.get("z") == tab["id"]]
                injects = [n for n in nodes if n.get("type") == "inject"]
                targets = injects
                if args.inject:
                    targets = [n for n in injects
                               if n["id"] == args.inject or n.get("name") == args.inject]
                print(f"[dry-run] flow={tab.get('label')} ({tab['id']})")
                print(f"  将触发 inject 节点: {[n.get('name') or n['id'] for n in targets]}")
                if args.expect:
                    print("  HA 断言(当前快照):")
                    for e in args.expect:
                        ent, _, st = e.partition(":")
                        cur = nr.ha_get_state(ent.strip())
                        print(f"    {ent.strip()} expect={st.strip()} current={cur}")
                if args.cleanup:
                    print(f"  验证后 turn_off: {args.cleanup}")
                print("  ⚠️ 加 --yes 才真实触发（有副作用：开灯/TTS/微信等）")
            else:
                res = nr.verify_flow(args.flow_id, inject_node=args.inject,
                                     wait=args.wait, expect=args.expect, cleanup=args.cleanup)
                print(json.dumps(res, ensure_ascii=False, indent=2))

        elif args.cmd == "settings":
            s = nr.get_settings()
            print(json.dumps(s, indent=2, ensure_ascii=False))

        elif args.cmd == "nodes":
            for n in nr.list_nodes():
                print(f"  {n.get('module','?')} v{n.get('version','?')}")

        elif args.cmd == "backup":
            n = nr.dump_all_flows("./nr_backup")
            print(f"✅ 备份了 {n} 个 flows 到 ./nr_backup/")

        elif args.cmd == "update-code":
            code = open(args.file, "r", encoding="utf-8").read() if args.file else args.code
            if args.dry_run:
                result = nr.modify_function_code(args.flow_id, args.node_id, code, name=args.name, dry_run=True)
                print(f"--- diff 预览 ---")
                print(result["diff"])
                print(f"--- 旧: {result['old_lines']} 行, 新: {result['new_lines']} 行 ---")
            else:
                nr.modify_function_code(args.flow_id, args.node_id, code, name=args.name)
                print(f"✅ Function 更新成功")

        elif args.cmd == "find-replace":
            n = nr.find_and_replace(args.flow_id, args.old, args.new)
            print(f"✅ 修改了 {n} 个节点")

        elif args.cmd == "fix-api-nodes":
            n = nr.fix_api_current_state_nodes(args.flow_id)
            print(f"✅ 修复了 {n} 个 api-current-state 节点")

        elif args.cmd == "fix-nodes":
            fix_code = compile(args.fix_expr, '<string>', 'exec')
            def fix_fn(n):
                local_vars = {'n': n}
                exec(fix_code, {}, local_vars)
                return True
            count = nr.fix_nodes_by_type(args.flow_id, args.node_type, fix_fn)
            print(f"✅ 修复了 {count} 个 {args.node_type} 节点")

        elif args.cmd == "version":
            print(f"nr_client v{NR_CLIENT_VERSION}")
            print(f"  本文件: {os.path.abspath(__file__)}")
            auth = _resolve_authoritative()
            auth_v = _read_version_from_file(auth) if auth else "?"
            print(f"  权威源: {auth} (v{auth_v})")
            print(f"  是否权威源: {_is_authoritative()}")

        elif args.cmd == "sync":
            r = ensure_latest(verbose=True)
            if r is True:
                print("✅ 已拉取最新版")
            elif r is False:
                print("✅ 已是最新")
            else:
                print("⚠️ 无法解析权威源（请设置 NR_CLIENT_AUTHORITY 或确保默认路径存在）")

        elif args.cmd == "check":
            auth = _resolve_authoritative()
            auth_v = _read_version_from_file(auth) if auth else "0.0.0"
            print(f"权威源: {auth}  v{auth_v}")
            candidates = [auth]
            seen = set()
            for c in candidates:
                if not c or c in seen:
                    continue
                seen.add(c)
                if not os.path.exists(c):
                    print(f"  (缺失)  {c}")
                    continue
                v = _read_version_from_file(c)
                if auth and os.path.abspath(c) == os.path.abspath(auth):
                    flag = "★权威"
                elif _version_lt(v, auth_v):
                    flag = "⚠落后"
                else:
                    flag = "✓同步"
                print(f"  [{flag}] v{v}  {c}")
        elif args.cmd == "lint":
            with open(args.flow_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            flows = data if isinstance(data, list) else [data]
            problems = nr._lint_flows(flows)
            if problems:
                print(f"❌ 发现 {len(problems)} 个结构问题：")
                for p in problems:
                    print(f"  - {p}")
                sys.exit(1)
            else:
                print("✅ 结构 lint 通过（无阻断级问题）")

    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
