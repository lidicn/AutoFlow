#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控自更新（方案 C）—— 从 GitHub 拉取更新并原地应用到 NAS 活树。

安全约束（改动前必读）：
  · 仅 developer/owner 角色可触发（WebUI RBAC 层把关，见 webui_auth.PERM_RULES）。
  · 只接受 allowlist 内的 ref：远程版本 tag（v*）或显式 SHA 白名单（AF_UPDATE_ALLOW_REFS）。
    默认（不传 ref）回退到「最新的 v* tag」。绝不接受任意分支/SHA 以外的来源。
  · 永远不跑 `git clean -f` / `git reset --hard`；不动未跟踪文件（data/ 等本地产物安全）。
  · 状态机：校验 ref → 备份 tar（不含 .git / data）→ fetch → checkout -f <pinned>
    → py_compile 全量语法校验 → 失败则回滚到上一提交并中止（不重启）→ 成功则触发重启。
  · 重启：容器内（/.dockerenv 存在）向 PID 1 发 SIGTERM，由 docker `restart: unless-stopped`
    拉起新容器（重读 /app/src 绑定挂载，新代码即生效）；非容器环境返回 manual 由外部重启。
  · 网络：尊重 HTTPS_PROXY/HTTP_PROXY；可选 AF_GIT_PROXY 单独覆盖 git 代理（中国大陆网络）。
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile  # noqa: F401  (保留，便于未来扩展)
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

# 只认版本 tag（v1.0 / v1.2.3 ...）；非版本 tag 一律不纳入自动更新。
TAG_RE = re.compile(r"^v\d+\.\d+")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_REMOTE = "https://github.com/lidicn/AutoFlow.git"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_present() -> bool:
    return shutil.which("git") is not None


def _git_env() -> Dict[str, str]:
    """返回带 git 代理的环境副本（AF_GIT_PROXY 单独覆盖；否则沿用系统 HTTPS_PROXY）。"""
    env = dict(os.environ)
    p = env.get("AF_GIT_PROXY")
    if p:
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = p
    return env


def _repo_dir() -> str:
    """定位 git 仓库根（容器内由 AF_REPO_DIR=/repo 指定）。"""
    for cand in (os.environ.get("AF_REPO_DIR"), "/repo"):
        if cand and os.path.isdir(os.path.join(cand, ".git")):
            return cand
    d = os.path.dirname(os.path.abspath(__file__))
    while d and d != os.path.dirname(d):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return ""


def _remote_url() -> str:
    return os.environ.get("AF_GIT_REMOTE") or DEFAULT_REMOTE


def _run_git(repo: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    # safe.directory=*：容器内以 root 运行，/repo 属主为 lidicn，git 默认拒访；
    # 自更新本就需要写入该仓库，放宽属主检查（仅对本仓操作，不波及其他）。
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", repo] + list(args),
        capture_output=True, text=True, env=_git_env(), check=check,
    )


def _allow_shas() -> List[str]:
    raw = os.environ.get("AF_UPDATE_ALLOW_REFS") or ""
    return [s.strip().lower() for s in raw.split(",") if SHA_RE.match(s.strip().lower())]


def _ver_key(tag: str) -> List[int]:
    nums = re.findall(r"\d+", tag)
    return [int(x) for x in nums] if nums else [0]


def list_remote_tags(repo: str) -> List[Dict[str, str]]:
    """返回远程版本 tag 列表 [{tag, commit}]（按版本倒序）。

    无法联网 / git 缺失时返回空列表（调用方据此判定「无可用更新」而非崩溃）。
    """
    if not _git_present():
        return []
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "ls-remote", "--tags", _remote_url()],
            capture_output=True, text=True, env=_git_env(), check=True, timeout=30,
        )
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.endswith("^{}"):
            continue
        m = re.match(r"^([0-9a-f]{40})\trefs/tags/([^\s]+)$", line)
        if not m:
            continue
        commit, tag = m.group(1), m.group(2)
        if not TAG_RE.match(tag):
            continue
        out.append({"tag": tag, "commit": commit})
    out.sort(key=lambda x: _ver_key(x["tag"]), reverse=True)
    return out


def current_commit(repo: str) -> str:
    try:
        return _run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()
    except Exception:
        return ""


def update_check(ref: Optional[str] = None) -> Dict:
    """只读检查：当前提交 / 可用目标 / 是否可更新。失败不抛异常，返回 ok=True 且 available=False。"""
    repo = _repo_dir()
    if not repo or not _git_present():
        return {"ok": True, "git_present": False, "repo_dir": repo,
                "available": False, "reason": "git 不可用或仓库未初始化（需重建含 git 的镜像）",
                "current": current_commit(repo) if repo else "",
                "target_ref": None, "target_commit": None, "tags": []}
    cur = current_commit(repo)
    tags = list_remote_tags(repo)
    target_ref, target_commit, err = _resolve_target(ref, tags)
    if err:
        return {"ok": True, "git_present": True, "repo_dir": repo, "available": False,
                "reason": err, "current": cur,
                "target_ref": target_ref, "target_commit": target_commit, "tags": tags}
    available = bool(target_commit) and target_commit != cur
    return {"ok": True, "git_present": True, "repo_dir": repo, "current": cur,
            "target_ref": target_ref, "target_commit": target_commit,
            "available": available,
            "reason": ("已是最新" if not available else f"可更新到 {target_ref}"),
            "tags": tags}


def _resolve_target(ref: Optional[str], tags: List[Dict[str, str]]):
    """返回 (target_ref, target_commit, error)。error 非空表示非法目标。"""
    allow = set(_allow_shas())
    if not ref:
        if not tags:
            return None, None, "远程无可用版本 tag"
        t = tags[0]  # 已倒序，最新
        return t["tag"], t["commit"], None
    ref = ref.strip()
    # 1) 显式 SHA（仅白名单）
    if SHA_RE.match(ref.lower()):
        if ref.lower() not in allow:
            return ref, None, "该提交 SHA 不在 AF_UPDATE_ALLOW_REFS 白名单内"
        try:
            _run_git(_repo_dir(), ["cat-file", "-e", ref], check=True)
        except Exception:
            return ref, None, "该提交在本地不可达（可能需要先 fetch）"
        return ref, ref, None
    # 2) 版本 tag 名（须命中远程已知 tag，防伪造随意 ref）
    for t in tags:
        if t["tag"] == ref:
            return t["tag"], t["commit"], None
    return ref, None, f"ref 不是允许的版本 tag 或白名单 SHA：{ref}"


def perform_update(ref: Optional[str] = None, *,
                   repo_dir: Optional[str] = None,
                   data_dir: Optional[str] = None) -> Dict:
    """执行受控自更新。仅在全部前置校验 + 备份 + 语法校验通过后才切代码并触发重启。"""
    repo = repo_dir or _repo_dir()
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return {"ok": False, "error": "仓库未初始化（AF_REPO_DIR 未指向含 .git 的目录）"}
    if not _git_present():
        return {"ok": False, "error": "容器内未安装 git（请重建镜像以包含 git）"}

    cur = current_commit(repo)
    chk = update_check(ref)
    if not chk.get("target_commit"):
        return {"ok": False, "error": chk.get("reason") or "未找到合法更新目标", "current": cur}
    target_ref = chk["target_ref"]
    target_commit = chk["target_commit"]
    if target_commit == cur:
        return {"ok": True, "already_latest": True, "current": cur,
                "target_ref": target_ref, "restart": "none"}

    # 1) 备份（不含 .git / data）
    backup_dir = data_dir or os.environ.get("AUTOFLLOW_DATA_DIR", "/data")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(backup_dir, f"autoflow-update-{ts}.tar.gz")
    try:
        _backup(repo, backup_path)
    except Exception as e:
        return {"ok": False, "error": f"备份失败：{e}", "current": cur}

    # 2) fetch
    try:
        _run_git(repo, ["fetch", "--tags", _remote_url()], check=True)
    except Exception as e:
        return {"ok": False, "error": f"fetch 失败：{e}", "current": cur,
                "backup": backup_path}

    # 3) checkout -f（丢弃已跟踪改动，但不删未跟踪文件）
    try:
        _run_git(repo, ["checkout", "-f", target_commit], check=True)
    except Exception as e:
        _rollback(repo, cur)
        return {"ok": False, "error": f"checkout 失败已回滚：{e}", "current": cur,
                "backup": backup_path}

    # 4) py_compile 全量语法校验（失败则回滚，不重启）
    compiled = _py_compile_check(repo)
    if not compiled["ok"]:
        _rollback(repo, cur)
        return {"ok": False, "error": "新代码语法校验失败，已回滚：" + compiled["error"],
                "current": cur, "backup": backup_path}

    # 5) 校验通过 → 触发重启（延后，先让 HTTP 响应送达）
    restart = _schedule_restart()
    return {"ok": True, "previous": cur, "target_ref": target_ref,
            "target_commit": target_commit, "backup": backup_path,
            "restart": restart, "restarting": True}


def _backup(repo: str, path: str) -> None:
    with tarfile.open(path, "w:gz") as tar:
        tar.add(repo, arcname=".", filter=_backup_filter)


def _backup_filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    parts = ti.name.split("/")
    if parts and parts[0] == ".":
        parts = parts[1:]
    if ".git" in parts:          # 不备份 git 内部（体积大且无必要）
        return None
    if parts and parts[0] == "data":  # 不备份本地产物卷（另挂，体积大）
        return None
    return ti


def _rollback(repo: str, commit: str) -> None:
    try:
        _run_git(repo, ["checkout", "-f", commit], check=True)
    except Exception:
        pass


def _py_compile_check(repo: str) -> Dict:
    targets: List[str] = []
    src_pkg = os.path.join(repo, "src", "autoflow_gateway")
    if os.path.isdir(src_pkg):
        for f in os.listdir(src_pkg):
            if f.endswith(".py"):
                targets.append(os.path.join(src_pkg, f))
    run_py = os.path.join(repo, "run.py")
    if os.path.isfile(run_py):
        targets.append(run_py)
    if not targets:
        return {"ok": True, "error": ""}
    try:
        r = subprocess.run([sys.executable, "-m", "py_compile", *targets],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout).strip()[:500]}
        return {"ok": True, "error": ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _schedule_restart() -> str:
    """容器内（/.dockerenv）向 PID 1 发 SIGTERM，由 docker restart 策略拉起新容器。"""
    if os.path.exists("/.dockerenv"):
        def _t() -> None:
            time.sleep(1.2)
            try:
                os.kill(1, signal.SIGTERM)
            except Exception:
                pass
        threading.Thread(target=_t, daemon=True).start()
        return "container-restart"
    return "manual"


__all__ = ["update_check", "perform_update", "list_remote_tags"]
