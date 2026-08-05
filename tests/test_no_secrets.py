# -*- coding: utf-8 -*-
"""发布门禁：版本库内不得出现真实凭据 / 内网标识（R3）。

为什么要有这道测试：
    此前多次靠「人工扫一遍」确认无泄密——人工扫描不可重复、随时间失效，
    只要有人新加一个 fixture、一份 flow 导出或一段调试常量就会破防，
    而且**改动者不会收到任何提示**。这里把扫描固化成门禁：
    每次跑测试都在真实的「已跟踪文件集」（= 实际会被 push / 打包的那批）上重扫。

扫描对象刻意选 `git ls-files` 而非磁盘遍历：
    磁盘上有大量本地产物（真实 flow 导出、.env、日志）本来就含密钥，
    它们靠 .gitignore 拦着、不进版本库；对它们报警只会制造噪音、逼人加豁免，
    最终把门禁调成永远绿的摆设。只扫「已经/即将进版本库」的文件才是真风险面。
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (名称, 正则, 说明)。正则写成转义形式，天然不会匹配到本文件自身的模式源码。
_PATTERNS = [
    ("HA/JWT token", re.compile(r"eyJ[A-Za-z0-9_-]{20,}"),
     "Home Assistant 长效令牌 / JWT"),
    ("MCP 身份码", re.compile(r"af_[A-Za-z0-9]{24,}"),
     "网关三码（黑箱/白箱/管理员）"),
    ("内网 IP", re.compile(r"192\.168\.\d{1,3}\.\d{1,3}|100\.(?:112|76)\.\d{1,3}\.\d{1,3}"),
     "家庭内网 / tailscale 地址"),
    ("Bearer/sk- 密钥", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}|sk-[A-Za-z0-9]{20,}"),
     "第三方 API 密钥"),
    # ★值必须「无空白且 ≥16 字符」，否则误报会淹没真信号：
    #   - `"?token=" + token`（URL 拼接）匹配到的值是 ` + token) : `，含空格 → 排除
    #   - `cfg.hass_token = "staging-token"`（测试 fixture）13 字符 → 排除
    #   代价是长度 <16 的短口令扫不到；这类由上面 4 条格式化模式兜底，
    #   宁可漏掉边缘情况，也不要一个天天误报、最后被人整条删掉的规则。
    ("硬编码凭据赋值", re.compile(
        r"(?i)(password|passwd|api_key|apikey|secret|token)\s*[=:]\s*[\"'][^\"'\s]{16,}[\"']"),
     "形如 password=\"真实值\" 的赋值"),
]

# 占位符 / 教学模板 / 环境变量读取 —— 命中这些视为安全写法，不报警。
_SAFE_MARKERS = re.compile(
    r"<[A-Z_]{3,}>|your_|example|placeholder|dummy|fake|"
    r"os\.getenv|os\.environ|getenv\(|\$\{|\{\{|xxx+|\.\.\.",
    re.IGNORECASE,
)

_TEXT_EXT = {
    ".py", ".md", ".json", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml",
    ".js", ".css", ".html", ".sh", ".bat", ".ps1", ".template", ".example", "",
}


def _is_git_repo() -> bool:
    return os.path.isdir(os.path.join(_REPO_ROOT, ".git"))


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT,
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return []
    return [p for p in out.stdout.splitlines() if p.strip()]


def _mask(s: str) -> str:
    """只回显头尾，避免把真实密钥原样打进 CI 日志（那等于又泄一次）。"""
    s = s.strip()
    return s if len(s) <= 16 else f"{s[:8]}…{s[-4:]}"


@pytest.mark.skipif(not _is_git_repo(), reason="非 git 工作副本，跳过泄密扫描")
def test_no_secrets_in_tracked_files():
    files = _tracked_files()
    assert files, "git ls-files 返回空——扫描范围为 0 等于门禁失效，请检查仓库状态"

    hits: list[str] = []
    for rel in files:
        if os.path.splitext(rel)[1].lower() not in _TEXT_EXT:
            continue
        path = os.path.join(_REPO_ROOT, rel.replace("/", os.sep))
        try:
            with open(path, "r", encoding="utf-8", errors="strict") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue  # 二进制或读不到，跳过（非文本不在风险面）
        for lineno, line in enumerate(lines, 1):
            if _SAFE_MARKERS.search(line):
                continue
            for name, pat, _desc in _PATTERNS:
                m = pat.search(line)
                if m:
                    hits.append(f"  {rel}:{lineno}  [{name}]  {_mask(m.group(0))}")
                    break

    assert not hits, (
        f"版本库内发现 {len(hits)} 处疑似真实凭据 / 内网标识：\n"
        + "\n".join(hits[:30])
        + ("\n  …" if len(hits) > 30 else "")
        + "\n\n处理方式（按优先级）：\n"
          "  1. 真密钥 → 立刻换成占位符 <XXX_TOKEN> 或改读环境变量，并【轮换该密钥】\n"
          "     （已 push 过就等于已泄露，删掉文件不等于删掉历史）\n"
          "  2. 本地产物误入库 → git rm --cached <文件> 并补 .gitignore 规则\n"
          "  3. 确属教学示例 → 在同一行使用占位符写法（<TOKEN> / your_xxx / os.getenv）"
    )


@pytest.mark.skipif(not _is_git_repo(), reason="非 git 工作副本，跳过泄密扫描")
def test_secret_scanner_actually_detects():
    """自检：确保扫描器不是「永远绿」的摆设。

    #708 的教训是防线可能因为路径/豁免写错而静默失效。这里用合成样本正向验证
    每一类模式都真能命中，并验证占位符写法确实被放行（否则大家会为了消噪
    把模式删光，门禁一样报废）。
    """
    samples = {
        "HA/JWT token": "token = " + '"eyJ' + "A" * 30 + '"',
        "MCP 身份码": "code = " + '"af_' + "B" * 30 + '"',
        "内网 IP": "NR_URL = http://192.168.0.10:1880  # example",
        "Bearer/sk- 密钥": "Authorization: Bearer " + "C" * 30,
        "硬编码凭据赋值": 'api_key = "' + "D" * 32 + '"',
    }
    for name, sample in samples.items():
        pat = next(p for n, p, _ in _PATTERNS if n == name)
        assert pat.search(sample), f"模式 [{name}] 未能命中合成样本，扫描器已失效"

    # 反向：已知误报形态必须【不】命中，否则门禁会因噪音被人为削弱
    cred_pat = next(p for n, p, _ in _PATTERNS if n == "硬编码凭据赋值")
    for benign in [
        'function qs(token) { return token ? "?token=" + token : ""; }',
        'cfg.hass_token = "staging-token"',
        'document.cookie = "af_ui_token=" + encodeURIComponent(v);',
    ]:
        assert not cred_pat.search(benign), f"误报：{benign}"

    safe_lines = [
        'CAIYUN_URL = "https://api.caiyunapp.com/v2.7/<CAIYUN_TOKEN>/"',
        'token = os.getenv("HA_TOKEN")',
        'api_key = "your_api_key_here"',
    ]
    for line in safe_lines:
        assert _SAFE_MARKERS.search(line), f"占位符写法被误判为泄密：{line}"
