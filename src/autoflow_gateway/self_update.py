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

# 中国大陆网络兜底镜像：主远端（github.com）不可达时自动尝试。
# 仅 ghproxy.net 经 NAS 实测可达；其余镜像（ghproxy.com / mirror.ghproxy.com /
# gitclone.com / kkgithub.com / hub.gitmirror.com）实测从 NAS 不可达，不列入自动兜底。
# 自动兜底仅对「默认主远端」生效——用户显式设置的 AF_GIT_REMOTE 视为有意指定，
# 失败不自动跳镜像（尊重其配置，也避免测试误用）。
FALLBACK_MIRRORS = ["https://ghproxy.net/https://github.com/lidicn/AutoFlow.git"]


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


def _run_git(repo: str, args: List[str], check: bool = True,
              timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    # safe.directory=*：容器内以 root 运行，/repo 属主为 lidicn，git 默认拒访；
    # 自更新本就需要写入该仓库，放宽属主检查（仅对本仓操作，不波及其他）。
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", repo] + list(args),
        capture_output=True, text=True, env=_git_env(), check=check,
        timeout=timeout,
    )


def _env_flag(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _repo_state(repo: str) -> Dict:
    """活树可升级性体检。

    ★ 事故教训（2026-09-28）：NAS 活树长期靠 scp 手工部署，git 侧停在
      「detached HEAD（v1.4.5 时代）+ 27 个已跟踪文件被本地改写」的状态。
      此时 perform_update 的 `git checkout -f <tag>` 会把这些本地改动（含
      docker-compose.yml 的局域网端口绑定等 NAS 特有配置）整片冲掉，
      py_compile 失败时还会回滚到一个更旧的提交 —— 等于拿升级按钮当格式化键。

    只统计「已跟踪文件的改动」：checkout -f 不会删除未跟踪文件（data/、*.bak 等安全）。
    """
    st: Dict = {"detached": False, "dirty": False, "changed_files": 0, "branch": ""}
    if not repo:
        return st
    try:
        r = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        br = (r.stdout or "").strip()
        st["branch"] = br
        st["detached"] = (br == "HEAD")  # detached HEAD 时 abbrev-ref 恒为 "HEAD"
    except Exception:
        pass
    try:
        r = _run_git(repo, ["status", "--porcelain"], check=False)
        tracked = [l for l in (r.stdout or "").splitlines()
                   if l.strip() and not l.startswith("??")]
        st["dirty"] = bool(tracked)
        st["changed_files"] = len(tracked)
    except Exception:
        pass
    return st


def _dirty_reason(st: Dict) -> str:
    why = []
    if st.get("detached"):
        why.append("HEAD 处于分离状态（活树不是任何分支的尖端）")
    if st.get("dirty"):
        why.append(f"{st.get('changed_files', 0)} 个已跟踪文件有本地改动")
    return "；".join(why) or "活树状态未知"


def _probe_url(url: str) -> str:
    """探测单个远端可达性。返回空串表示可达；否则返回人类可读原因。

    用于区分「网络不通」与「远端真的没发版」——两者在旧实现里都退化成
    「远程无可用版本 tag」，把网络故障伪装成产品状态，误导排查方向。
    """
    try:
        subprocess.run(
            ["git", "-c", "safe.directory=*", "ls-remote", "--heads", url],
            capture_output=True, text=True, env=_git_env(), check=True, timeout=30,
        )
        return ""
    except subprocess.TimeoutExpired:
        return (f"远端 {url} 不可达（ls-remote 超时 30 秒）。"
                f"请检查网络或切换其他镜像。")
    except Exception as e:
        return f"远端 {url} 不可达：{e}"


def _remote_probe() -> str:
    """探测默认主远端可达性（见 _probe_url）。"""
    return _probe_url(_remote_url())


def _allow_shas() -> List[str]:
    raw = os.environ.get("AF_UPDATE_ALLOW_REFS") or ""
    return [s.strip().lower() for s in raw.split(",") if SHA_RE.match(s.strip().lower())]


def _ver_key(tag: str) -> List[int]:
    nums = re.findall(r"\d+", tag)
    return [int(x) for x in nums] if nums else [0]


def list_remote_tags(repo: str, remote_url: Optional[str] = None) -> List[Dict[str, str]]:
    """返回远程版本 tag 列表 [{tag, commit}]（按版本倒序）。

    无法联网 / git 缺失时返回空列表（调用方据此判定「无可用更新」而非崩溃）。
    remote_url 可显式指定远端（用于主远端失败后的兜底镜像探测）。
    """
    if not _git_present():
        return []
    url = remote_url or _remote_url()
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "ls-remote", "--tags", url],
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


def _version_file() -> str:
    repo = _repo_dir()
    return os.path.join(repo, "VERSION") if repo else ""


def read_version() -> str:
    """读取仓库根 VERSION 文件的版本号（网关发布版本，如 1.0.0）。

    该文件随自更新 checkout 一并更新，故反映「实际运行版本」；缺失时回退到提交比对。
    """
    p = _version_file()
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def update_check(ref: Optional[str] = None) -> Dict:
    """只读检查：当前提交 / 可用目标 / 是否可更新。失败不抛异常，返回 ok=True 且 available=False。

    可用判定优先级：
      · 显式 ref → 以「目标提交 != 当前提交」为准（用户明确指定来源）。
      · 未传 ref 且能读到运行版本号 → 以「最新远程 tag 版本 > 运行版本」为准（语义化）。
      · 否则 → 以「最新 tag 提交 != 当前提交」为准（兜底）。
    """
    repo = _repo_dir()
    if not repo or not _git_present():
        return {"ok": True, "git_present": False, "repo_dir": repo,
                "available": False, "reason": "git 不可用或仓库未初始化（需重建含 git 的镜像）",
                "current": current_commit(repo) if repo else "",
                "current_version": "", "latest_tag": None, "target_ref": None,
                "target_commit": None, "tags": [],
                "repo_state": _repo_state(repo), "safe_to_update": False}
    cur = current_commit(repo)
    cur_ver = read_version()
    tags = list_remote_tags(repo)
    remote_used = _remote_url()
    # 主远端（任一 github.com 地址，http/ssh 皆可）不可达 → 自动尝试兜底镜像，
    # 别把网络故障伪装成「无发版」。非 github 的自定义远端（如用户自建）不自动跳镜像。
    auto_fallback = ("github.com" in remote_used)
    if not tags and auto_fallback:
        for fm in FALLBACK_MIRRORS:
            if fm == remote_used:
                continue
            ft = list_remote_tags(repo, fm)
            if ft:
                tags = ft
                remote_used = fm
                break
    target_ref, target_commit, err = _resolve_target(ref, tags)
    if err:
        reason = err
        if not tags:
            # 区分「网络不通」与「远端没发版」，别把网络故障伪装成产品状态。
            reason = _remote_probe() or "远程无可用版本 tag（远端没有 v* 形式的发布标签）"
        return {"ok": True, "git_present": True, "repo_dir": repo, "available": False,
                "reason": reason, "current": cur, "current_version": cur_ver,
                "latest_tag": (tags[0]["tag"] if tags else None),
                "target_ref": target_ref, "target_commit": target_commit, "tags": tags,
                "remote_used": remote_used,
                "repo_state": _repo_state(repo), "safe_to_update": False}
    latest_tag = tags[0]["tag"] if tags else None
    if ref:
        available = bool(target_commit) and target_commit != cur
    elif cur_ver and latest_tag:
        # 语义化比对：v1.1.0 > 1.0.0
        available = _ver_key(latest_tag) > _ver_key(cur_ver)
    else:
        available = bool(target_commit) and target_commit != cur
    st = _repo_state(repo)
    # 安全判定只认「是否有未提交的本地改动（dirty）」：clean 的 detached tag 也允许升级
    # （否则自更新后停在 tag 上会再也升不了）。AF_UPDATE_ALLOW_DIRTY 仍用于强制升级
    # 那些确属脏改动、用户确认要丢弃的场景。
    safe = (not st["dirty"]) or _env_flag("AF_UPDATE_ALLOW_DIRTY")
    reason = ("已是最新" if not available else f"可更新到 {target_ref}")
    if not safe:
        reason = ("活树不干净，在线升级已禁用（会把本地改动整片冲掉）：" + _dirty_reason(st))
    return {"ok": True, "git_present": True, "repo_dir": repo, "current": cur,
            "current_version": cur_ver, "latest_tag": latest_tag,
            "target_ref": target_ref, "target_commit": target_commit,
            "available": bool(available) and safe,
            "reason": reason, "tags": tags, "remote_used": remote_used,
            "repo_state": st, "safe_to_update": safe}


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
                   data_dir: Optional[str] = None,
                   mirror: Optional[str] = None) -> Dict:
    """执行受控自更新。仅在全部前置校验 + 备份 + 语法校验通过后才切代码并触发重启。
    mirror: 国内镜像 URL（如 https://ghproxy.com/https://github.com/lidicn/AutoFlow.git），
            传入后 fetch 阶段使用镜像地址，完成后恢复原 remote。"""
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

    # ★ 0) 活树体检：detached HEAD / 工作树脏 一律先拒绝，再谈备份与 fetch。
    #   否则 `checkout -f <tag>` 会把活树的本地改动（NAS 特有配置、手工部署的新代码）
    #   整片冲掉，且失败回滚会退到一个更旧的提交 —— 升级按钮变格式化键。
    st = _repo_state(repo)
    # ★ 只拦截「已跟踪文件有本地改动（dirty）」——这才是 checkout -f 会整片冲掉的风险。
    #   单独 detached（如自更新后停在 tag 上）不算危险：clean 的 detached tag 上执行
    #   checkout -f 只是移动 HEAD，不会冲掉任何本地改动；若在此拦截，会导致「自更新一次后
    #   再也无法自更新」的死锁。本地改动才是真正的炸弹。
    if st["detached"] and not st["dirty"]:
        # 仅提示，不拦截：clean 分离头可正常升级
        pass
    if st["dirty"] and not _env_flag("AF_UPDATE_ALLOW_DIRTY"):
        return {"ok": False, "error": "活树不干净，已拒绝在线升级：" + _dirty_reason(st)
                + "。请先把活树改动纳入 git 管理（或设置 AF_UPDATE_ALLOW_DIRTY=1 强制升级）。",
                "current": cur, "repo_state": st, "blocked": "dirty_worktree"}

    # 1) 备份（不含 .git / data）
    backup_dir = data_dir or os.environ.get("AUTOFLLOW_DATA_DIR", "/data")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(backup_dir, f"autoflow-update-{ts}.tar.gz")
    try:
        _backup(repo, backup_path)
    except Exception as e:
        return {"ok": False, "error": f"备份失败：{e}", "current": cur}

    # 2) fetch（支持国内镜像 + 自动兜底）
    # ★ 候选远端：显式 mirror 优先；否则「主远端 + 兜底镜像」依次尝试，第一个成功即用。
    #   自动兜底仅对 github.com 主远端生效（含 ssh git@github.com 与 https://github.com），
    #   失败后自动跳 ghproxy.net 兜底镜像；用户显式选了镜像则以其为准。无论成败，
    #   origin 最终都恢复原值。
    auto_fallback = ("github.com" in _remote_url())
    explicit = [mirror] if mirror else []
    auto = [_remote_url()] + (FALLBACK_MIRRORS if auto_fallback else [])
    seen: set = set()
    candidates: List[str] = []
    for c in explicit + auto:
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)
    original_remote = None
    try:
        r = _run_git(repo, ["remote", "get-url", "origin"], check=False)
        original_remote = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        original_remote = None
    fetch_error = None
    used_remote = None
    for cand in candidates:
        try:
            _run_git(repo, ["remote", "set-url", "origin", cand], check=True)
            # fetch 增加 60 秒超时，避免网络问题时无限等待
            _run_git(repo, ["fetch", "--tags", "origin"], check=True, timeout=60)
            used_remote = cand
            break
        except subprocess.TimeoutExpired:
            fetch_error = f"fetch 超时（60秒），远端 {cand} 不可达，请检查网络或切换其他镜像"
        except Exception as e:
            detail = ""
            if hasattr(e, "stderr") and e.stderr:
                detail = f"（{e.stderr.strip()[:200]}）"
            elif hasattr(e, "output") and e.output:
                detail = f"（{str(e.output).strip()[:200]}）"
            fetch_error = f"fetch 失败：{e}{detail}（远端 {cand}）"
    # ★ 无论成功失败，都恢复原 remote
    if original_remote:
        try:
            _run_git(repo, ["remote", "set-url", "origin", original_remote], check=False)
        except Exception:
            pass
    if not used_remote:
        return {"ok": False, "error": fetch_error, "current": cur,
                "backup": backup_path, "mirror_used": (mirror or "")}

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
