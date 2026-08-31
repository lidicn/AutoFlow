#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow Gateway — WebUI 账号密码登录与会话（账号登录改造）

★ 三套令牌隔离铁律（改动前必读）★
本模块**只管 WebUI**。MCP 的 `af_` 身份码（mcp_server.py 中间件）与 ACP 的 `acp_` 对等令牌
（/acp 端点）完全不受本模块影响，三者互不相认。任何改动都不得把 WebUI 会话当成 MCP 身份。

设计要点：
  · 密码：hashlib.pbkdf2_hmac('sha256', 600k, 16B 随机盐)；**0 新增依赖**（仓库无 bcrypt/passlib）。
    迭代次数写进哈希串，未来提标无需一次性迁移，登录成功时若低于当前标准自动 rehash。
  · 会话：服务端有状态（SQLite webui_sessions 表），**不用 JWT** —— JWT 无法即时吊销，
    而「登出立刻失效」是本次改造的核心诉求（旧令牌正是死在这里）。
  · 会话明文只存在于 Cookie；库里只存 sha256。
  · 多角色 RBAC：viewer / admin / owner，按「路径前缀 + 方法」声明式授权，
    未登记的写路径一律 fail-closed 要求 admin。

安全不变量（对应 docs/PLAN_webui_password_login.md 第 5.4 节）：
  I-1 密码绝不明文落盘/落日志   I-2 三套令牌隔离   I-3 Cookie: HttpOnly+SameSite=Lax
  I-4 登出=服务端删除           I-5 失败恒定响应   I-6 失败计数+锁定
  I-7 S-4 不退化               I-8 常量时间比对   I-9 CSRF 三层防御
  I-10 审计留痕                I-11 P-2 不破（无默认密码/内网 IP 入库）
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config

# ───────────────────────── 常量 ─────────────────────────

PBKDF2_ALGO = "sha256"
# OWASP 2023 建议值（sha256）。实测本机 0.6s/次；NAS 等弱 CPU 可用
# AUTOFLOW_PBKDF2_ITERS 下调（不得低于 100_000）。迭代数写进哈希串，
# 调高后旧哈希登录成功会自动 rehash 升级，无需一次性迁移。
PBKDF2_ITERS = max(100_000, int(os.environ.get("AUTOFLOW_PBKDF2_ITERS") or 600_000))
SALT_BYTES = 16

SESSION_COOKIE = "af_session"
LEGACY_COOKIE = "af_ui_token"   # 旧令牌 cookie（兼容通道，M1 并存期）
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "autoflow"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

SESSION_TTL_HOURS = 12          # D2：默认 12 小时（滑动）
REMEMBER_TTL_DAYS = 7           # D2：「记住我」7 天绝对上限
SLIDING_REFRESH_AFTER = 30 * 60  # 滑动续期：距上次活跃超过 30 分钟才回写，减低压库

MAX_FAILED_USER = 5             # I-6：同一用户名连错 5 次
LOCK_SECONDS_USER = 15 * 60
MAX_FAILED_IP = 20              # I-6：同一 IP（15 分钟窗口）连错 20 次
LOCK_SECONDS_IP = 30 * 60
FAILED_WINDOW_SECONDS = 15 * 60

MIN_PASSWORD_LEN = 8

ROLES = ("viewer", "admin", "owner")
ROLE_RANK = {"viewer": 0, "admin": 1, "owner": 2}

# 认证模式：password_only（默认，D4 直接关令牌）/ both（并存）/ token_only（回滚）
VALID_AUTH_MODES = ("password_only", "both", "token_only")

# 免鉴权白名单（登录/注册/探测必须匿名可达，否则登不进来）
PUBLIC_PATHS = (
    "/api/auth/state",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/logout",
    "/health",
)

# ── RBAC 权限矩阵（D1：一步到位多用户）──
# 规则按 (methods, path_prefix, role) 登记；methods=None 表示所有方法。
# 解析：最长前缀优先；未命中任何规则时走 _DEFAULT_ROLE（fail-closed 要求 admin）。
PERM_RULES: Tuple[Tuple[Optional[Tuple[str, ...]], str, str], ...] = (
    # ── 账号自助区：登录即可（viewer 起）──
    (None, "/api/auth/me", "viewer"),
    (None, "/api/auth/change-password", "viewer"),
    (None, "/api/auth/sessions", "viewer"),
    # ── 用户管理：仅 owner（含改他人密码/角色/删号）──
    (None, "/api/auth/users", "owner"),
    # ── 只读业务：viewer 可看 ──
    (("GET",), "/api/pending", "viewer"),
    (("GET",), "/api/proposals", "viewer"),
    (("GET",), "/api/deployed", "viewer"),
    (("GET",), "/api/subflows", "viewer"),
    (("GET",), "/api/notes", "viewer"),          # 笔记是协作性质，登录即可写
    (("GET",), "/api/settings/connections", "viewer"),
    (("GET",), "/api/link-apis", "viewer"),
    (("GET",), "/api/agents", "viewer"),
    (("GET",), "/api/acp", "viewer"),
    (("GET",), "/api/llm/config", "viewer"),
    (("GET",), "/api/audit", "viewer"),
    (("GET",), "/api/diagnostics", "viewer"),
    (("GET",), "/api/catalog", "viewer"),
    (("GET",), "/api/entities", "viewer"),
    (("GET",), "/api/config", "viewer"),
    (("GET",), "/api/debug", "viewer"),
    (("GET",), "/api/first-run", "viewer"),
    # ── 高危写操作：admin 起（未登记路径的写操作默认也是 admin）──
    (None, "/api/pending", "admin"),             # 批准闸
    (None, "/api/proposals", "admin"),           # 部署/升格/删除
    (None, "/api/deployed", "admin"),            # 上线/下线
    (None, "/api/flows", "admin"),               # 触发 flow
    (None, "/api/settings/connections", "admin"),
    (None, "/api/settings", "admin"),
    (None, "/api/link-apis", "admin"),
    (None, "/api/subflows", "admin"),
    (None, "/api/agents", "admin"),              # 签发/吊销 agent 身份码
    (None, "/api/acp", "admin"),                 # ACP 对等令牌
    (None, "/api/llm/config", "admin"),
    (None, "/api/catalog/import", "admin"),
    (None, "/api/device-guard", "admin"),
    (None, "/api/first-run", "admin"),
)

_DEFAULT_ROLE_BY_METHOD = {"GET": "viewer", "HEAD": "viewer", "OPTIONS": "viewer"}
_DEFAULT_ROLE = "admin"   # fail-closed：未登记的写路径一律要求 admin


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def session_hash(sid: str) -> str:
    """会话明文 → 库里存的哈希（供「改密后保留当前会话」使用）。"""
    return _sha256(sid or "")


def role_rank(role: str) -> int:
    return ROLE_RANK.get(str(role or "").lower(), -1)


# ───────────────────────── 密码哈希 ─────────────────────────

def hash_password(pw: str, *, iterations: int = PBKDF2_ITERS,
                  salt: Optional[bytes] = None) -> str:
    """返回 `pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`。"""
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALGO, pw.encode("utf-8"), salt, int(iterations))
    return "pbkdf2_%s$%d$%s$%s" % (
        PBKDF2_ALGO, int(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def _split_hash(stored: str):
    """解析哈希串 → (iters, salt, dk)；格式不对返回 None。"""
    try:
        algo, iters, salt_b64, dk_b64 = str(stored or "").split("$", 3)
        if not algo.startswith("pbkdf2_"):
            return None
        return (int(iters), base64.b64decode(salt_b64), base64.b64decode(dk_b64),
                algo.split("_", 1)[1])
    except Exception:
        return None


_DUMMY_LOCK = threading.Lock()
_DUMMY_HASH: Optional[str] = None


def _dummy_hash() -> str:
    """用户不存在时用于「等时比对」的哑哈希（I-5：不泄露用户是否存在）。"""
    global _DUMMY_HASH
    with _DUMMY_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
        return _DUMMY_HASH


def verify_password(pw: str, stored: str) -> Tuple[bool, bool]:
    """校验密码。返回 (是否通过, 是否需要 rehash)。

    无论成功失败都跑满一次 PBKDF2（用户不存在时跑哑哈希），保证响应耗时恒定。
    比对用 hmac.compare_digest（I-8 常量时间）。
    """
    parsed = _split_hash(stored)
    if parsed is None:
        # 哈希串损坏/空 → 视为不匹配，但仍跑一次哑计算保持等时
        hash(pw)
        hashlib.pbkdf2_hmac(PBKDF2_ALGO, pw.encode("utf-8"), b"x" * SALT_BYTES, 1)
        return False, False
    iters, salt, dk, algo = parsed
    calc = hashlib.pbkdf2_hmac(algo, pw.encode("utf-8"), salt, iters)
    ok = hmac.compare_digest(calc, dk)
    need_rehash = ok and iters < PBKDF2_ITERS
    return ok, need_rehash


def password_policy_error(pw: str, username: str = "") -> Optional[str]:
    """密码强度校验（返回 None 表示通过）。"""
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if pw.strip() != pw or not pw.strip():
        return "密码首尾不能是空白字符"
    if username and pw.lower() == str(username).lower():
        return "密码不能与用户名相同"
    if len(set(pw)) < 4:
        return "密码太简单：至少包含 4 种不同字符"
    return None


# ───────────────────────── Cookie / Header 解析 ─────────────────────────

def _cookie_value(headers: Dict[bytes, bytes], name: str) -> str:
    raw = headers.get(b"cookie")
    if not raw:
        return ""
    from urllib.parse import unquote
    try:
        text = raw.decode("latin-1")
    except Exception:
        return ""
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == name:
            return unquote(v.strip())
    return ""


def session_id_from_scope(scope: dict) -> str:
    return _cookie_value(dict(scope.get("headers", [])), SESSION_COOKIE)


def build_session_cookie(sid: str, max_age: int, secure: bool = False) -> str:
    """I-3：HttpOnly + SameSite=Lax + Path=/；生产 https 加 Secure。"""
    parts = [
        f"{SESSION_COOKIE}={sid}",
        "Path=/",
        f"Max-Age={int(max_age)}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie(secure: bool = False) -> str:
    parts = [f"{SESSION_COOKIE}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def legacy_token_from_scope(scope: dict) -> str:
    """旧令牌的三处携带点（向后兼容 M1 并存期）。"""
    headers = dict(scope.get("headers", []))
    auth = headers.get(b"authorization")
    if auth:
        try:
            v = auth.decode("latin-1").strip()
        except Exception:
            v = ""
        if v[:7].lower() == "bearer ":
            return v[7:].strip()
        return v
    from urllib.parse import parse_qs
    qs = parse_qs(scope.get("query_string", b"").decode("latin-1", "ignore"))
    got = (qs.get("token") or [""])[0]
    if got:
        return got
    return _cookie_value(headers, LEGACY_COOKIE)


# ───────────────────────── RBAC ─────────────────────────

def required_role_for(method: str, path: str) -> Optional[str]:
    """返回该请求所需的最低角色；返回 None 表示免鉴权（PUBLIC_PATHS）。"""
    p = path or ""
    if p in PUBLIC_PATHS:
        return None
    m = (method or "GET").upper()
    best: Optional[str] = None
    best_len = -1
    for methods, prefix, role in PERM_RULES:
        if not p.startswith(prefix):
            continue
        if methods is not None and m not in methods:
            continue
        if len(prefix) > best_len:
            best, best_len = role, len(prefix)
    if best is not None:
        return best
    return _DEFAULT_ROLE_BY_METHOD.get(m, _DEFAULT_ROLE)


def is_public_path(path: str) -> bool:
    return (path or "") in PUBLIC_PATHS


def needs_csrf(method: str) -> bool:
    return (method or "GET").upper() not in SAFE_METHODS


def csrf_ok(scope: dict) -> bool:
    """I-9 第二层：Cookie 认证的写请求必须带自定义头 `X-Requested-With: autoflow`。

    跨站脚本无法自定义请求头（会触发 CORS 预检并被拒），因此这条能拦住 CSRF。
    第三层见 WebUIAuth：Bearer / ?token= 的写请求豁免（非环境权限，保 CI 兼容）。
    """
    headers = dict(scope.get("headers", []))
    for k, v in headers.items():
        if k.decode("latin-1").lower() == CSRF_HEADER:
            try:
                if v.decode("latin-1").strip().lower() == CSRF_VALUE:
                    return True
            except Exception:
                return False
            return False
    return False


# ───────────────────────── 主存储 ─────────────────────────

class WebUIAuth:
    """WebUI 账号、会话与授权的单一入口（SQLite，与 agents/acp_tokens 同库不同表）。"""

    _lock = threading.RLock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.cfg.data_dir, "autoflow.db")
        self.audit_path = os.path.join(self.cfg.data_dir, "webui_auth_audit.log")
        self.auth_mode = (os.environ.get("AF_WEBUI_TOKEN_MODE") or "password_only").lower()
        if self.auth_mode not in VALID_AUTH_MODES:
            self.auth_mode = "password_only"
        self.open_register = os.environ.get(
            "AF_WEBUI_OPEN_REGISTER", "1").lower() in ("1", "true", "yes")
        self.secure_cookie = os.environ.get(
            "AF_WEBUI_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
        # 进程内限流（IP 维度）；用户名维度落库持久化，重启不失效
        self._ip_attempts: Dict[str, List[float]] = {}
        self._ip_locked_until: Dict[str, float] = {}
        self._init_db()

    # ── 连接 ──
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS webui_users (
                        user_id       TEXT PRIMARY KEY,
                        username      TEXT NOT NULL UNIQUE,
                        pw_hash       TEXT NOT NULL,
                        role          TEXT NOT NULL DEFAULT 'owner',
                        status        TEXT NOT NULL DEFAULT 'active',
                        must_change   INTEGER NOT NULL DEFAULT 0,
                        failed_count  INTEGER NOT NULL DEFAULT 0,
                        locked_until  TEXT,
                        created_at    TEXT NOT NULL,
                        last_login_at TEXT,
                        notes         TEXT DEFAULT ''
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS webui_sessions (
                        session_id      TEXT PRIMARY KEY,
                        sess_hash       TEXT NOT NULL UNIQUE,
                        user_id         TEXT NOT NULL,
                        created_at      TEXT NOT NULL,
                        expires_at      TEXT NOT NULL,
                        hard_expires_at TEXT NOT NULL,
                        last_seen_at    TEXT,
                        user_agent      TEXT DEFAULT '',
                        ip              TEXT DEFAULT ''
                    )"""
                )
                conn.commit()
            finally:
                conn.close()

    # ── 审计（I-10）──
    def audit(self, action: str, *, username: str = "", ip: str = "",
              ok: bool = True, note: str = "") -> None:
        """追加一行 JSONL。★ 绝不记录密码（I-1）。"""
        try:
            rec = {
                "ts": _now().isoformat(),
                "action": action,
                "username": username or "",
                "ip": ip or "",
                "ok": bool(ok),
                "note": note or "",
            }
            line = json.dumps(rec, ensure_ascii=False)
            with self._lock:
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass  # 审计失败绝不影响主流程

    def read_audit(self, limit: int = 200) -> List[dict]:
        try:
            with open(self.audit_path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines[-int(limit):]:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out

    # ── 用户 ──
    @staticmethod
    def _row_to_user(r) -> dict:
        return {
            "user_id": r["user_id"],
            "username": r["username"],
            "role": r["role"],
            "status": r["status"],
            "must_change": int(r["must_change"] or 0),
            "created_at": r["created_at"],
            "last_login_at": r["last_login_at"],
            "notes": r["notes"] or "",
            "locked": bool(r["locked_until"] and _parse_iso(r["locked_until"])
                           and _parse_iso(r["locked_until"]) > _now()),
        }

    def has_users(self) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                n = conn.execute("SELECT COUNT(*) c FROM webui_users").fetchone()["c"]
            finally:
                conn.close()
        return n > 0

    def get_user(self, user_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM webui_users WHERE user_id=?", (user_id,)).fetchone()
            finally:
                conn.close()
        return self._row_to_user(r) if r else None

    def get_user_by_name(self, username: str) -> Optional[dict]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM webui_users WHERE username=?",
                    (str(username or "").strip(),)).fetchone()
            finally:
                conn.close()
        return self._row_to_user(r) if r else None

    def list_users(self) -> List[dict]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM webui_users ORDER BY created_at").fetchall()
            finally:
                conn.close()
        return [self._row_to_user(r) for r in rows]

    def create_user(self, username: str, password: str, role: str = "viewer",
                    notes: str = "", must_change: int = 0) -> dict:
        """建号。第一个账号强制 owner 且不允许降级。"""
        username = str(username or "").strip()
        err = password_policy_error(password, username)
        if err:
            raise ValueError(err)
        if not username or len(username) > 64:
            raise ValueError("用户名不能为空且不超过 64 字符")
        if role not in ROLES:
            raise ValueError(f"非法角色: {role}")
        uid = "usr_" + uuid.uuid4().hex[:12]
        now = _iso(_now())
        first = not self.has_users()
        eff_role = "owner" if first else role
        if first:
            must_change = 0
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO webui_users "
                    "(user_id,username,pw_hash,role,status,must_change,failed_count,created_at,notes) "
                    "VALUES (?,?,?,?,?,?,0,?,?)",
                    (uid, username, hash_password(password), eff_role, "active",
                     int(must_change), now, notes or ""),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"用户名已存在: {username}")
            finally:
                conn.close()
        return self.get_user(uid)

    def update_user(self, user_id: str, *, role: Optional[str] = None,
                    status: Optional[str] = None, notes: Optional[str] = None,
                    must_change: Optional[int] = None) -> bool:
        fields, vals = [], []
        if role is not None:
            if role not in ROLES:
                raise ValueError(f"非法角色: {role}")
            fields.append("role=?"); vals.append(role)
        if status is not None:
            if status not in ("active", "disabled"):
                raise ValueError(f"非法状态: {status}")
            fields.append("status=?"); vals.append(status)
        if notes is not None:
            fields.append("notes=?"); vals.append(notes or "")
        if must_change is not None:
            fields.append("must_change=?"); vals.append(int(bool(must_change)))
        if not fields:
            return True
        vals.append(user_id)
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    f"UPDATE webui_users SET {', '.join(fields)} WHERE user_id=?", vals)
                conn.commit()
                if cur.rowcount == 0:
                    return False
            finally:
                conn.close()
        # 禁用/改角色 → 立即踢掉该用户所有会话（防止权限变更滞后生效）
        if status == "disabled" or role is not None:
            self.revoke_user_sessions(user_id)
        return True

    def set_password(self, user_id: str, new_password: str, *, username: str = "") -> None:
        err = password_policy_error(new_password, username)
        if err:
            raise ValueError(err)
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE webui_users SET pw_hash=?, must_change=0, failed_count=0, "
                    "locked_until=NULL WHERE user_id=?",
                    (hash_password(new_password), user_id))
                conn.commit()
                if cur.rowcount == 0:
                    raise ValueError("用户不存在")
            finally:
                conn.close()

    def delete_user(self, user_id: str) -> bool:
        self.revoke_user_sessions(user_id)
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM webui_users WHERE user_id=?", (user_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # ── 登录限流（I-6）──
    def _ip_locked(self, ip: str) -> bool:
        if not ip:
            return False
        until = self._ip_locked_until.get(ip, 0)
        return until > _now().timestamp()

    def _ip_note_fail(self, ip: str) -> bool:
        """记录一次 IP 失败，返回是否触发锁定。"""
        if not ip:
            return False
        now_ts = _now().timestamp()
        window = [t for t in self._ip_attempts.get(ip, []) if now_ts - t < FAILED_WINDOW_SECONDS]
        window.append(now_ts)
        self._ip_attempts[ip] = window
        if len(window) >= MAX_FAILED_IP:
            self._ip_locked_until[ip] = now_ts + LOCK_SECONDS_IP
            self._ip_attempts[ip] = []
            return True
        return False

    # ── 认证 ──
    def authenticate(self, username: str, password: str, ip: str = "") -> Tuple[Optional[dict], str]:
        """返回 (user, reason)。reason: ok | locked | ip_locked | bad_creds | disabled。

        ★ I-5：用户不存在 / 密码错 / 已禁用 一律返回 bad_creds，前端与审计都不区分，
        防用户枚举。响应耗时通过哑哈希保持一致。
        """
        if self._ip_locked(ip):
            return None, "ip_locked"
        user = self.get_user_by_name(username)
        if user is None:
            verify_password(password or "", _dummy_hash())   # 等时
            self._ip_note_fail(ip)
            self.audit("login_fail", username=username, ip=ip, ok=False, note="bad_creds")
            return None, "bad_creds"
        locked_until = None
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT locked_until FROM webui_users WHERE user_id=?",
                    (user["user_id"],)).fetchone()
                if r:
                    locked_until = _parse_iso(r["locked_until"])
            finally:
                conn.close()
        if locked_until and locked_until > _now():
            self.audit("login_fail", username=user["username"], ip=ip, ok=False, note="locked")
            return None, "locked"
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT pw_hash FROM webui_users WHERE user_id=?",
                    (user["user_id"],)).fetchone()
                stored = r["pw_hash"] if r else ""
            finally:
                conn.close()
        ok, need_rehash = verify_password(password or "", stored)
        if ok and user["status"] != "active":
            ok = False
        if not ok:
            # 失败计数落库（持久化，重启不失效）
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(
                        "UPDATE webui_users SET failed_count=failed_count+1 WHERE user_id=?",
                        (user["user_id"],))
                    conn.commit()
                    c = conn.execute(
                        "SELECT failed_count FROM webui_users WHERE user_id=?",
                        (user["user_id"],)).fetchone()
                    cnt = int(c["failed_count"]) if c else 0
                finally:
                    conn.close()
            if cnt >= MAX_FAILED_USER:
                until = _iso(_now() + timedelta(seconds=LOCK_SECONDS_USER))
                with self._lock:
                    conn = self._conn()
                    try:
                        conn.execute(
                            "UPDATE webui_users SET locked_until=? WHERE user_id=?",
                            (until, user["user_id"]))
                        conn.commit()
                    finally:
                        conn.close()
                self.audit("lockout", username=user["username"], ip=ip, ok=False,
                           note=f"failed={cnt}")
            self._ip_note_fail(ip)
            self.audit("login_fail", username=user["username"], ip=ip, ok=False, note="bad_creds")
            return None, "bad_creds"
        if need_rehash:
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(
                        "UPDATE webui_users SET pw_hash=? WHERE user_id=?",
                        (hash_password(password), user["user_id"]))
                    conn.commit()
                finally:
                    conn.close()
        # 成功：清零失败计数 + 写 last_login
        now = _iso(_now())
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE webui_users SET failed_count=0, locked_until=NULL, last_login_at=? "
                    "WHERE user_id=?", (now, user["user_id"]))
                conn.commit()
            finally:
                conn.close()
        self.audit("login_ok", username=user["username"], ip=ip, ok=True)
        return self.get_user(user["user_id"]), "ok"

    # ── 会话 ──
    def create_session(self, user_id: str, *, remember: bool = False,
                       ip: str = "", user_agent: str = "") -> Tuple[str, int]:
        """返回 (明文会话 ID, Max-Age 秒数)。库里只存 sha256，明文只回给 Cookie。"""
        sid = secrets.token_urlsafe(32)
        now = _now()
        ttl = timedelta(days=REMEMBER_TTL_DAYS) if remember else timedelta(hours=SESSION_TTL_HOURS)
        hard = now + ttl
        idle = now + (timedelta(hours=SESSION_TTL_HOURS) if remember else ttl)
        if idle > hard:
            idle = hard
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO webui_sessions (session_id,sess_hash,user_id,created_at,"
                    "expires_at,hard_expires_at,last_seen_at,user_agent,ip) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("ses_" + uuid.uuid4().hex[:16], _sha256(sid), user_id,
                     _iso(now), _iso(idle), _iso(hard), _iso(now),
                     (user_agent or "")[:300], ip or ""),
                )
                conn.commit()
            finally:
                conn.close()
        return sid, int((hard - now).total_seconds())

    def resolve_session(self, sid: str) -> Optional[dict]:
        """用明文会话 ID 解析 {user_id,username,role,status,session_id}；失效返回 None。"""
        if not sid:
            return None
        h = _sha256(sid)
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM webui_sessions WHERE sess_hash=?", (h,)).fetchone()
                if not r:
                    return None
                expires = _parse_iso(r["expires_at"])
                hard = _parse_iso(r["hard_expires_at"])
                if (expires and expires <= now) or (hard and hard <= now):
                    conn.execute(
                        "DELETE FROM webui_sessions WHERE sess_hash=?", (h,))
                    conn.commit()
                    return None
                # 滑动续期（D2）：距上次活跃 > 30 分钟才回写，且不超过 hard 上限
                last = _parse_iso(r["last_seen_at"]) or now
                if (now - last).total_seconds() >= SLIDING_REFRESH_AFTER:
                    new_exp = now + timedelta(hours=SESSION_TTL_HOURS)
                    if hard and new_exp > hard:
                        new_exp = hard
                    conn.execute(
                        "UPDATE webui_sessions SET last_seen_at=?, expires_at=? WHERE sess_hash=?",
                        (_iso(now), _iso(new_exp), h))
                else:
                    conn.execute(
                        "UPDATE webui_sessions SET last_seen_at=? WHERE sess_hash=?",
                        (_iso(now), h))
                conn.commit()
                u = conn.execute(
                    "SELECT * FROM webui_users WHERE user_id=?", (r["user_id"],)).fetchone()
                if not u:
                    return None
            finally:
                conn.close()
        if str(u["status"]) != "active":
            return None
        return {
            "session_id": r["session_id"],
            "user_id": r["user_id"],
            "username": u["username"],
            "role": u["role"],
            "status": u["status"],
            "must_change": int(u["must_change"] or 0),
            "last_login_at": u["last_login_at"],
            "expires_at": r["expires_at"],
        }

    def revoke_session(self, sid: str, *, user_id: Optional[str] = None) -> bool:
        """吊销单个会话。传 user_id 时只吊销属于该用户的（防越权踢人）。"""
        if not sid:
            return False
        h = _sha256(sid)
        with self._lock:
            conn = self._conn()
            try:
                if user_id:
                    cur = conn.execute(
                        "DELETE FROM webui_sessions WHERE sess_hash=? AND user_id=?", (h, user_id))
                else:
                    cur = conn.execute(
                        "DELETE FROM webui_sessions WHERE sess_hash=?", (h,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def revoke_session_by_id(self, session_id: str, *, user_id: Optional[str] = None) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                if user_id:
                    cur = conn.execute(
                        "DELETE FROM webui_sessions WHERE session_id=? AND user_id=?",
                        (session_id, user_id))
                else:
                    cur = conn.execute(
                        "DELETE FROM webui_sessions WHERE session_id=?", (session_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def revoke_user_sessions(self, user_id: str, *, keep_hash: str = "") -> int:
        """吊销该用户所有会话（改密/禁用/改角色/删号时调用）。"""
        with self._lock:
            conn = self._conn()
            try:
                if keep_hash:
                    cur = conn.execute(
                        "DELETE FROM webui_sessions WHERE user_id=? AND sess_hash<>?",
                        (user_id, keep_hash))
                else:
                    cur = conn.execute(
                        "DELETE FROM webui_sessions WHERE user_id=?", (user_id,))
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    def list_sessions(self, user_id: Optional[str] = None) -> List[dict]:
        with self._lock:
            conn = self._conn()
            try:
                if user_id:
                    rows = conn.execute(
                        "SELECT * FROM webui_sessions WHERE user_id=? ORDER BY created_at DESC",
                        (user_id,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM webui_sessions ORDER BY created_at DESC").fetchall()
            finally:
                conn.close()
        out = []
        for r in rows:
            out.append({
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "hard_expires_at": r["hard_expires_at"],
                "last_seen_at": r["last_seen_at"],
                "ip": r["ip"] or "",
                "user_agent": r["user_agent"] or "",
            })
        return out

    def purge_expired(self) -> int:
        now = _iso(_now())
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM webui_sessions WHERE expires_at<=? OR hard_expires_at<=?",
                    (now, now))
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    # ── 旧令牌兼容通道（M1 并存 / 回滚）──
    def resolve_legacy_token(self, scope: dict, token: Optional[str] = None) -> bool:
        """旧令牌校验沿用 webui.py 既有语义（env AF_WEBUI_TOKEN → data_dir/.webui_token）。

        命中旧令牌的请求视为 **owner** 权限且 **豁免 CSRF**（令牌不是环境权限，
        跨站拿不到，天然免疫 CSRF；也给脚本/CI 留活路）。
        """
        if self.auth_mode == "password_only":
            return False
        provided = token if token is not None else legacy_token_from_scope(scope)
        expected = self._resolve_webui_token()
        if not expected:
            return False
        return hmac.compare_digest(
            (provided or "").encode("utf-8"), (expected or "").encode("utf-8"))

    def _resolve_webui_token(self) -> Optional[str]:
        env_tok = os.environ.get("AF_WEBUI_TOKEN")
        if env_tok:
            return env_tok
        try:
            p = os.path.join(self.cfg.data_dir, ".webui_token")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    return f.read().strip() or None
        except Exception:
            pass
        return None

    # ── 首开注册窗口 ──
    def registration_open(self) -> bool:
        """首账号注册窗口：仅在「零账号 + 开关未关」时开放，建号后永久关闭。"""
        return bool(self.open_register) and not self.has_users()


__all__ = [
    "WebUIAuth", "hash_password", "verify_password", "password_policy_error",
    "required_role_for", "is_public_path", "needs_csrf", "csrf_ok",
    "session_id_from_scope", "legacy_token_from_scope",
    "build_session_cookie", "clear_session_cookie", "role_rank", "session_hash",
    "SESSION_COOKIE", "CSRF_HEADER", "CSRF_VALUE", "ROLES", "ROLE_RANK",
]
