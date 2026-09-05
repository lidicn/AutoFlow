#!/usr/bin/env python3
"""修复在线更新 remote 恢复逻辑 + fetch 超时 + 详细错误信息"""

SU = r"E:\NAS\autoflow\src\autoflow_gateway\self_update.py"
with open(SU, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修改 _run_git 函数，增加 timeout 参数支持
old_run_git = '''def _run_git(repo: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    # safe.directory=*：容器内以 root 运行，/repo 属主为 lidicn，git 默认拒访；
    # 自更新本就需要写入该仓库，放宽属主检查（仅对本仓操作，不波及其他）。
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", repo] + list(args),
        capture_output=True, text=True, env=_git_env(), check=check,
    )'''

new_run_git = '''def _run_git(repo: str, args: List[str], check: bool = True,
              timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    # safe.directory=*：容器内以 root 运行，/repo 属主为 lidicn，git 默认拒访；
    # 自更新本就需要写入该仓库，放宽属主检查（仅对本仓操作，不波及其他）。
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", repo] + list(args),
        capture_output=True, text=True, env=_git_env(), check=check,
        timeout=timeout,
    )'''

if old_run_git in content:
    content = content.replace(old_run_git, new_run_git, 1)
    print("1. _run_git 增加 timeout 参数: OK")
else:
    print("1. _run_git 增加 timeout 参数: NOT FOUND")

# 2. 重写 fetch 阶段，用 try-finally 确保 remote 恢复
old_fetch = '''    # 2) fetch（支持国内镜像）
    fetch_url = mirror or _remote_url()
    original_remote = None
    if mirror:
        # 临时切换 remote 到镜像，fetch 后恢复
        try:
            r = _run_git(repo, ["remote", "get-url", "origin"], check=False)
            original_remote = r.stdout.strip() if r.returncode == 0 else None
            _run_git(repo, ["remote", "set-url", "origin", mirror], check=True)
        except Exception:
            original_remote = None
    try:
        _run_git(repo, ["fetch", "--tags", "origin"], check=True)
    except Exception as e:
        # 恢复原 remote
        if original_remote:
            try:
                _run_git(repo, ["remote", "set-url", "origin", original_remote], check=False)
            except Exception:
                pass
        return {"ok": False, "error": f"fetch 失败：{e}", "current": cur,
                "backup": backup_path}
    # 恢复原 remote
    if original_remote:
        try:
            _run_git(repo, ["remote", "set-url", "origin", original_remote], check=False)
        except Exception:
            pass'''

new_fetch = '''    # 2) fetch（支持国内镜像）
    # ★ 修复：用 try-finally 确保 remote 一定被恢复，避免 fetch 失败后
    #   origin 永久指向失效镜像，导致后续所有更新都失败。
    fetch_url = mirror or _remote_url()
    original_remote = None
    mirror_switched = False
    if mirror:
        # 临时切换 remote 到镜像，fetch 后恢复
        try:
            r = _run_git(repo, ["remote", "get-url", "origin"], check=False)
            original_remote = r.stdout.strip() if r.returncode == 0 else None
            _run_git(repo, ["remote", "set-url", "origin", mirror], check=True)
            mirror_switched = True
        except Exception as e:
            mirror_switched = False
            return {"ok": False, "error": f"切换镜像失败：{e}", "current": cur,
                    "backup": backup_path}
    fetch_error = None
    try:
        # fetch 增加 60 秒超时，避免网络问题时无限等待
        _run_git(repo, ["fetch", "--tags", "origin"], check=True, timeout=60)
    except subprocess.TimeoutExpired:
        fetch_error = "fetch 超时（60秒），请检查网络或切换其他镜像"
    except Exception as e:
        # 提取 git 的详细错误信息（stderr）
        detail = ""
        if hasattr(e, "stderr") and e.stderr:
            detail = f"（{e.stderr.strip()[:200]}）"
        elif hasattr(e, "output") and e.output:
            detail = f"（{str(e.output).strip()[:200]}）"
        fetch_error = f"fetch 失败：{e}{detail}"
    finally:
        # ★ 无论 fetch 成功还是失败，都恢复原 remote
        if mirror_switched and original_remote:
            try:
                _run_git(repo, ["remote", "set-url", "origin", original_remote], check=False)
            except Exception:
                pass
    if fetch_error:
        return {"ok": False, "error": fetch_error, "current": cur,
                "backup": backup_path, "mirror_used": mirror}'''

if old_fetch in content:
    content = content.replace(old_fetch, new_fetch, 1)
    print("2. fetch 阶段 try-finally + 超时 + 详细错误: OK")
else:
    print("2. fetch 阶段 try-finally + 超时 + 详细错误: NOT FOUND")

with open(SU, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
