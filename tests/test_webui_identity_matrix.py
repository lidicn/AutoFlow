# -*- coding: utf-8 -*-
"""身份通道矩阵守卫 —— 防 D-09 类「硬化一条通道 → 误杀邻通道」。

## 为什么有这条守卫（真实事故，2026-09-22）
38 项审计里的 D-09 本意是「API Key 校验失败要显式 401，别静默放过」。但实现写成
`if _auth.startswith("Bearer ")` —— 而 `Bearer` 这个头是**两条身份通道共用**的：
  ① `af_pro_<hex>` 才是 API Key；
  ② 旧令牌 `AF_WEBUI_TOKEN` 也以 Bearer 传（兼容通道，注释写着"给脚本/CI 留活路"）。
结果硬化 API Key 的同时**把旧令牌通道整条打死**，webui 系 15 个用例全红。若非跑全量，
该回归会直接上 prod。

## 本守卫的做法
把 `guarded` 中间的**全部身份通道**枚举成矩阵，逐格断言落点，
使「收紧某通道 → 邻通道被误杀」在 CI 立即显形，而不必等某个无关用例偶然变红。

## 通道（判定顺序，见 webui.py `build_webui_asgi` 的 `guarded`）
  P 公开白名单          → 免鉴权（不写 af_auth）
  K Bearer `af_pro_*`   → API Key 通道（af_auth.mode=api_key）
  S `af_session` cookie → 会话通道（mode=session）
  T 旧令牌              → 兼容通道（mode=token；仅 both/token_only）
  - 无身份              → 401（已初始化）；未初始化+回环则本机开放（S-4 止血）

## 不变量
  I-1 非 `af_pro_` 前缀的 Bearer **绝不**进 API Key 通道            ← D-09 根因
  I-2 `af_pro_` 无效/吊销 → 401，**不得**回落到会话或旧令牌通道      ← D-09 本意
  I-3 旧令牌通道在 password_only 模式下关闭
  I-4 会话优先于旧令牌（同时携带 → 会话胜出）
  I-5 会话 cookie 失效 → 401（不静默降级为匿名）
  I-6 已初始化后，无任何凭据 → 401
  I-7 未初始化 + 回环 → 本机开放（S-4 有意行为，显式钉住防误改）

运行：pytest tests/test_webui_identity_matrix.py -q
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    from starlette.testclient import TestClient

    from autoflow_gateway.api_keys import APIKeyStore
    from autoflow_gateway.config import GatewayConfig
    from autoflow_gateway.gateway import Gateway
    from autoflow_gateway.webui import build_webui_asgi
    from autoflow_gateway.webui_auth import WebUIAuth

    _HAVE_WEB_DEPS = True
    _WEB_DEP_MSG = ""
except ImportError as _e:          # pragma: no cover - 环境缺依赖时优雅 skip
    _HAVE_WEB_DEPS = False
    _WEB_DEP_MSG = str(_e)
    TestClient = build_webui_asgi = None

# 受保护的只读端点（GET，viewer 起）：用于观测通道落点，不产生副作用
PROBE_PATH = "/api/config"
PUBLIC_PATH = "/api/health"

LEGACY_TOKEN = "mx-legacy-token-abc123"
OWNER_PW = "Str0ngPass!234"


@unittest.skipUnless(
    _HAVE_WEB_DEPS,
    f"身份通道矩阵需要 starlette（缺失：{_WEB_DEP_MSG}）；用系统 Python 3.13 运行。",
)
class TestIdentityChannelMatrix(unittest.TestCase):
    """通道矩阵。每个用例只断言「这一格落在哪条通道」，互不耦合。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_mx_")
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("AF_WEBUI_TOKEN_MODE", "AF_WEBUI_TOKEN",
                      "AF_WEBUI_OPEN_REGISTER", "AUTOFLLOW_DATA_DIR")
        }
        os.environ["AUTOFLLOW_DATA_DIR"] = self.tmp
        os.environ["AF_WEBUI_OPEN_REGISTER"] = "1"
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self._clients = []

    def tearDown(self):
        for c in self._clients:
            try:
                c.__exit__(None, None, None)
            except Exception:
                pass
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 客户端工厂 ────────────────────────────────────────────────

    def _make_client(self, *, mode="both", token=LEGACY_TOKEN, initialized=True,
                     host=None):
        """造一个带「身份观测探针」的 TestClient。

        探针原理：`guarded` 是**就地**改写 `scope["af_auth"]` 后再调内层 app，
        因此在外层包一层 spy 即可在响应返回后读到本请求解析出的身份通道。

        host=None → 回环（starlette 默认 client 主机名 "testclient"，已在 webui.py
        `_LOOPBACK_HOSTS` 显式登记）；host="203.0.113.9" → 模拟远端，
        用于覆盖 S-4「未配置认证时远端一律拒」这一维。
        """
        os.environ["AF_WEBUI_TOKEN_MODE"] = mode
        os.environ["AF_WEBUI_TOKEN"] = token
        gw = Gateway(self.cfg)
        inner = build_webui_asgi(self.cfg, gateway=gw)
        seen = {}

        async def spy(scope, receive, send):
            await inner(scope, receive, send)
            if scope.get("type") == "http":
                seen["af_auth"] = scope.get("af_auth")

        client = TestClient(spy, client=(host, 51234)) if host else TestClient(spy)
        client.__enter__()
        self._clients.append(client)
        wa = WebUIAuth(self.cfg)
        if initialized and not wa.has_users():
            # 建号使 has_users()=True —— 否则会落进「未初始化+回环 → 本机开放」分支
            wa.create_user("mx_owner", OWNER_PW, role="owner")
        return client, seen, wa

    @staticmethod
    def _probe(client, seen, *, path=PROBE_PATH, headers=None):
        seen.clear()
        r = client.get(path, headers=headers or {})
        return r.status_code, dict(seen.get("af_auth") or {})

    # ── 矩阵本体 ──────────────────────────────────────────────────

    def test_matrix_every_channel_lands_where_expected(self):
        """通道矩阵：每种凭据组合 → (状态码, 解析出的通道)。"""
        client, seen, wa = self._make_client(mode="both")
        kstore = APIKeyStore(os.path.join(self.tmp, "api_keys"))
        good = kstore.create_key(name="mx", agent_id="agt_mx")

        # 会话 cookie
        owner = wa.get_user_by_name("mx_owner")
        sid, _ttl = wa.create_session(owner["user_id"])

        rows = [
            # label,                headers,                                          status, mode
            ("P 公开白名单",          {},                                                 200, None),
            ("K 有效 af_pro_ key",    {"Authorization": f"Bearer {good['key']}"},         200, "api_key"),
            ("K 无效 af_pro_ key",    {"Authorization": "Bearer af_pro_deadbeef"},        401, None),
            ("S 有效会话 cookie",     {"Cookie": f"af_session={sid}"},                    200, "session"),
            ("S 失效会话 cookie",     {"Cookie": "af_session=bogus"},                     401, None),
            ("T 旧令牌（Bearer）",    {"Authorization": f"Bearer {LEGACY_TOKEN}"},        200, "token"),
            ("T 旧令牌（?token=）",   {},                                                 200, "token"),
            ("- 无任何凭据",          {},                                                 401, None),
            ("S+T 同时携带",          {"Cookie": f"af_session={sid}; af_ui_token={LEGACY_TOKEN}"},
                                                                                          200, "session"),
        ]
        for label, headers, exp_status, exp_mode in rows:
            with self.subTest(label=label):
                path = PUBLIC_PATH if label.startswith("P ") else PROBE_PATH
                if label == "T 旧令牌（?token=）":
                    path = f"{PROBE_PATH}?token={LEGACY_TOKEN}"
                status, ident = self._probe(client, seen, path=path, headers=headers)
                self.assertEqual(status, exp_status,
                                 f"[{label}] 状态码不符：{status} != {exp_status}")
                self.assertEqual(ident.get("mode"), exp_mode,
                                 f"[{label}] 通道落点不符：{ident.get('mode')!r} != {exp_mode!r}")

        # 吊销后的 key 也必须 401（并落到「无身份」）
        kstore.revoke_key(good["key_id"])
        status, ident = self._probe(
            client, seen, headers={"Authorization": f"Bearer {good['key']}"})
        self.assertEqual(status, 401, "已吊销的 API Key 必须 401")
        self.assertIsNone(ident.get("mode"), "吊销后不得落入任何身份通道")

    # ── 逐条不变量（失败信息直指被破坏的通道）──────────────────────

    def test_I1_non_af_pro_bearer_never_enters_api_key_channel(self):
        """I-1 ★ D-09 根因：非 af_pro_ 的 Bearer 必须继续走后续通道。

        「任意 Bearer 都当 API Key」正是把旧令牌通道打死的写法。
        此处正向断言：旧令牌以 Bearer 传入 → 必须成功且落 token 通道。
        """
        client, seen, _ = self._make_client(mode="both")
        status, ident = self._probe(
            client, seen, headers={"Authorization": f"Bearer {LEGACY_TOKEN}"})
        self.assertEqual(status, 200,
                         "旧令牌 Bearer 被拒 → API Key 分支又一次吞掉了兼容通道（D-09 复发）")
        self.assertEqual(ident.get("mode"), "token",
                         f"旧令牌 Bearer 应落 token 通道，实际 {ident.get('mode')!r}")

    def test_I2_invalid_api_key_does_not_fall_back_to_legacy_token(self):
        """I-2 ★ D-09 本意：提供了 af_pro_ key 但无效 → 401，**不得**回落旧令牌。

        构造同时携带「无效 af_pro_ Bearer」+「有效旧令牌 cookie」的请求：
        若实现回落到旧令牌，就会拿到 200 —— 那等于凭据作废后仍能进（硬化失效）。
        """
        client, seen, wa = self._make_client(mode="both")
        wa.get_user_by_name("mx_owner")   # 确保已初始化
        status, ident = self._probe(
            client, seen,
            headers={"Authorization": "Bearer af_pro_deadbeef",
                     "Cookie": f"af_ui_token={LEGACY_TOKEN}"})
        self.assertEqual(status, 401,
                         "无效 API Key 回落到旧令牌通道 → D-09 的 fail-closed 硬化失效")
        self.assertIsNone(ident.get("mode"), "无效 API Key 请求不得持有任何身份")

    def test_I3_legacy_channel_closed_in_password_only(self):
        """I-3 旧令牌通道只在 both/token_only 开启；password_only 下必须拒绝。"""
        client, seen, _ = self._make_client(mode="password_only")
        status, ident = self._probe(
            client, seen, headers={"Authorization": f"Bearer {LEGACY_TOKEN}"})
        self.assertEqual(status, 401, "password_only 模式下旧令牌必须不可用")
        self.assertIsNone(ident.get("mode"))

    def test_I4_session_wins_over_legacy_token(self):
        """I-4 同时携带会话与旧令牌 → 会话优先（旧令牌是 owner 特权，不能反超）。"""
        client, seen, wa = self._make_client(mode="both")
        owner = wa.get_user_by_name("mx_owner")
        sid, _ = wa.create_session(owner["user_id"])
        _status, ident = self._probe(
            client, seen,
            headers={"Cookie": f"af_session={sid}; af_ui_token={LEGACY_TOKEN}"})
        self.assertEqual(ident.get("mode"), "session", "会话应优先于旧令牌")
        self.assertEqual(ident.get("username"), "mx_owner")

    def test_I5_bad_session_cookie_is_401(self):
        """I-5 会话失效绝不静默降级为匿名放行。"""
        client, seen, _ = self._make_client(mode="both")
        status, ident = self._probe(client, seen, headers={"Cookie": "af_session=deadbeef"})
        self.assertEqual(status, 401, "失效会话必须 401")
        self.assertIsNone(ident.get("mode"))

    def test_I6_initialized_without_credentials_is_401(self):
        """I-6 已初始化（存在账号）后，无凭据访问受保护端点必须 401。"""
        client, seen, _ = self._make_client(mode="both", initialized=True)
        status, ident = self._probe(client, seen)
        self.assertEqual(status, 401, "已初始化后匿名访问必须 401")
        self.assertIsNone(ident.get("mode"))

    def test_I7_uninitialized_loopback_open_but_remote_closed(self):
        """I-7 S-4 有意行为：未初始化时**回环**放行（首开注册窗口），**远端**一律 403。

        这是 S-4「止血但不锁死自己」的设计：本机浏览器要能完成首次注册，
        而 Docker 0.0.0.0 暴露到公网时绝不能让控制面裸奔。两半都显式钉住，
        若哪天被改动，本用例红 → 提醒同步前端首开流程与文档。
        """
        for mode in ("both", "token_only"):
            with self.subTest(mode=mode, where="loopback"):
                c, s, _ = self._make_client(mode=mode, initialized=False)
                status, _ = self._probe(c, s)
                self.assertEqual(status, 200, "未初始化 + 回环应放行（S-4 首开注册窗口）")
            with self.subTest(mode=mode, where="remote"):
                c, s, _ = self._make_client(mode=mode, initialized=False,
                                            host="203.0.113.9")
                status, _ = self._probe(c, s)
                self.assertEqual(status, 403,
                                 "未初始化时远端必须 403（S-4 止血：防 0.0.0.0 裸奔到公网）")

    def test_I8_remote_authenticated_ok_unauthenticated_401(self):
        """I-8 远端不是「一律拒」：凭据正确必须可用，仅无凭据才 401。

        与 I-7 的 403 区分：403 = 来源被拒（未配置认证）；401 = 有认证体系但你没带。
        前端据此在 403/401 上走不同提示，二者不可混淆。
        """
        c, s, wa = self._make_client(mode="both", host="203.0.113.9")
        owner = wa.get_user_by_name("mx_owner")
        sid, _ = wa.create_session(owner["user_id"])
        status, ident = self._probe(c, s, headers={"Cookie": f"af_session={sid}"})
        self.assertEqual(status, 200, "远端持有效会话应可访问")
        self.assertEqual(ident.get("mode"), "session")

        status2, _ = self._probe(c, s)
        self.assertEqual(status2, 401, "已初始化后远端无凭据应 401（非 403）")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
