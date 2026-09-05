#!/usr/bin/env python3
"""v1.2.3 综合修改：后端镜像支持 + 前端更新页面 + ACP文案 + 教程重构"""
import re

# ═══════════════════════════════════════════════════════════
# 1. self_update.py: 增加 mirror 参数支持
# ═══════════════════════════════════════════════════════════
SU = r"E:\NAS\autoflow\src\autoflow_gateway\self_update.py"
with open(SU, "r", encoding="utf-8") as f:
    su = f.read()

# perform_update 增加 mirror 参数
old_sig = '''def perform_update(ref: Optional[str] = None, *,
                   repo_dir: Optional[str] = None,
                   data_dir: Optional[str] = None) -> Dict:
    """执行受控自更新。仅在全部前置校验 + 备份 + 语法校验通过后才切代码并触发重启。"""'''
new_sig = '''def perform_update(ref: Optional[str] = None, *,
                   repo_dir: Optional[str] = None,
                   data_dir: Optional[str] = None,
                   mirror: Optional[str] = None) -> Dict:
    """执行受控自更新。仅在全部前置校验 + 备份 + 语法校验通过后才切代码并触发重启。
    mirror: 国内镜像 URL（如 https://ghproxy.com/https://github.com/lidicn/AutoFlow.git），
            传入后 fetch 阶段使用镜像地址，完成后恢复原 remote。"""'''
su = su.replace(old_sig, new_sig, 1)

# fetch 阶段使用 mirror
old_fetch = '''    # 2) fetch
    try:
        _run_git(repo, ["fetch", "--tags", _remote_url()], check=True)
    except Exception as e:
        return {"ok": False, "error": f"fetch 失败：{e}", "current": cur,
                "backup": backup_path}'''
new_fetch = '''    # 2) fetch（支持国内镜像）
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
su = su.replace(old_fetch, new_fetch, 1)

with open(SU, "w", encoding="utf-8") as f:
    f.write(su)
print("self_update.py: mirror support added")

# ═══════════════════════════════════════════════════════════
# 2. webui.py: self_update_endpoint 接收 mirror 参数
# ═══════════════════════════════════════════════════════════
WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    wu = f.read()

old_ep = '''    async def self_update_endpoint(request: Request):
        """触发受控自更新（备份→fetch→checkout→py_compile→重启）。仅 owner。"""
        b = await _body(request)
        ref = (b.get("ref") or "").strip() or None
        try:
            from . import self_update as _su
            res = _su.perform_update(ref=ref)
        except Exception as e:
            return _js({"ok": False, "error": f"更新失败: {e}"}, 500)
        return _js(res, status=200 if res.get("ok") else 500)'''
new_ep = '''    async def self_update_endpoint(request: Request):
        """触发受控自更新（备份→fetch→checkout→py_compile→重启）。仅 owner。"""
        b = await _body(request)
        ref = (b.get("ref") or "").strip() or None
        mirror = (b.get("mirror") or "").strip() or None
        try:
            from . import self_update as _su
            res = _su.perform_update(ref=ref, mirror=mirror)
        except Exception as e:
            return _js({"ok": False, "error": f"更新失败: {e}"}, 500)
        return _js(res, status=200 if res.get("ok") else 500)'''
wu = wu.replace(old_ep, new_ep, 1)

with open(WU, "w", encoding="utf-8") as f:
    f.write(wu)
print("webui.py: mirror parameter added")

print("\nBackend changes complete!")
