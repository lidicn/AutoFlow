#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库完整性回归（#708）。

背景：2026-08-01 发现 `audit.py`(C21) / `device_guard.py`(C25) 落在磁盘但从未
`git add`，而 `webui.py` 顶部硬 import 它们 —— 从 master 全新 clone 后
`import autoflow_gateway.webui` 直接 ModuleNotFoundError，Web 控制面整个起不来。

全量 979 passed 完全没能发现，因为：
  1. 测试跑的是**工作树**（磁盘上文件在），不是 master tree；
  2. 这两个模块零测试覆盖。

本文件补上这道防线：**凡 src/ 下的 .py，要么已进 master tree，要么已在当前 index
被 add**；两者皆无 = 漏网，必须红。

判定用「master tree ∪ index」而非单一来源，是为了兼容特性分支开发：
  - CB 在分支上新建模块并 `git add` → 在 index → 放行；
  - WB1 用隔离 index 提交进 master → 在 master tree → 放行；
  - 真漏网（既没 add 也没进 master）→ 拦截。
"""
import os
import subprocess

import pytest

# ★2026-08-02 修复：原实现把仓库根写死为「本文件上溯两级」、src 写死为
# `autoflow_gateway/src/autoflow_gateway`（仅适配 <root>/autoflow_gateway/src 那种嵌套布局）。
# 在 <root>/src 扁平布局下，_REPO_ROOT 会算到盘符根（如 E:\），`.git` 找不到 →
# 两条用例被 skipif 静默跳过 → **这道 #708 防线形同虚设**（与 FEEDBACK #13 同源的虚假绿灯）。
# 改为运行时探测：向上找 .git 定位仓库根，再在根下探测实际 src 路径。
def _find_repo_root(start: str) -> str:
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:                      # 到盘符根仍未找到
            return os.path.abspath(os.path.join(start, "..", ".."))
        cur = parent


_REPO_ROOT = _find_repo_root(os.path.dirname(__file__))

# 两种已知布局，按序探测（相对仓库根，git 命令也用这个相对路径做 pathspec）
_SRC_REL_CANDIDATES = (
    "src/autoflow_gateway",                      # 扁平布局：<root>/src/autoflow_gateway
    "autoflow_gateway/src/autoflow_gateway",     # 嵌套布局：<root>/autoflow_gateway/src/...
)
_SRC_REL = next(
    (rel for rel in _SRC_REL_CANDIDATES
     if os.path.isdir(os.path.join(_REPO_ROOT, rel.replace("/", os.sep)))),
    _SRC_REL_CANDIDATES[0],
)
_SRC_ABS = os.path.join(_REPO_ROOT, _SRC_REL.replace("/", os.sep))

# 已知豁免：`nr_subflows/history/` 是构建产物与一次性脚本目录，
# 按设计被 .gitignore 忽略（改动后须 `git add -f` 指定四文件，见 MEMORY.md）。
_EXEMPT_DIR_PARTS = ("nr_subflows", "history")


def _git(*args: str):
    """跑 git 子命令；失败返回 None（用于优雅降级而非误报）。"""
    try:
        # 用 cwd 进仓库而非 `git -C <abs>`：在 Git Bash + 共享盘(UNC) 场景下
        # `-C`/`--git-dir` 的绝对路径解析会失败，cwd 形式通用。
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _is_git_repo() -> bool:
    return os.path.isdir(os.path.join(_REPO_ROOT, ".git"))


def _disk_py_files():
    """磁盘上 src/ 下所有 .py 的仓库相对路径（正斜杠），已排除豁免目录。"""
    found = []
    for dirpath, _dirnames, filenames in os.walk(_SRC_ABS):
        rel_dir = os.path.relpath(dirpath, _REPO_ROOT).replace(os.sep, "/")
        if all(p in rel_dir.split("/") for p in _EXEMPT_DIR_PARTS):
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if "__pycache__" in rel_dir:
                continue
            found.append(f"{rel_dir}/{fn}")
    return sorted(found)


def _tracked_paths():
    """master tree ∪ 当前 index 的已跟踪路径集合。"""
    tracked = set()
    # 主线分支名不固定（master/main/特性分支），master 取不到时回退 HEAD，
    # 避免因分支名不匹配而静默降级成「只看 index」。
    tree = (_git("ls-tree", "-r", "--name-only", "master", "--", _SRC_REL)
            or _git("ls-tree", "-r", "--name-only", "HEAD", "--", _SRC_REL))
    if tree:
        tracked.update(x.strip() for x in tree.splitlines() if x.strip())
    idx = _git("ls-files", "--", _SRC_REL)
    if idx:
        tracked.update(x.strip() for x in idx.splitlines() if x.strip())
    return tracked


@pytest.mark.skipif(not _is_git_repo(), reason="非 git 工作副本，跳过完整性校验")
def test_all_src_modules_are_tracked():
    """src/ 下每个 .py 都必须已进 master 或已被 add，否则 clone 出来就是残缺的。"""
    tracked = _tracked_paths()
    if not tracked:
        pytest.skip("无法从 git 读取跟踪列表（可能是 tarball 导出）")

    disk = _disk_py_files()
    assert disk, f"未在 {_SRC_REL} 找到任何 .py，路径推断可能有误"

    missing = [p for p in disk if p not in tracked]
    assert not missing, (
        "以下源码文件在磁盘上但既未进 master、也未被 git add —— "
        "从 master clone 后会缺失（#708 同类缺陷）：\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.skipif(not _is_git_repo(), reason="非 git 工作副本，跳过完整性校验")
def test_webui_hard_dependencies_are_tracked():
    """#708 定点防线：webui.py 顶部 import 的一级本地模块必须已跟踪。

    比通用扫描更早失败、错误信息更直指要害（webui 挂 = Web 控制面全挂）。
    """
    webui = os.path.join(_SRC_ABS, "webui.py")
    if not os.path.exists(webui):
        pytest.skip("webui.py 不存在")

    with open(webui, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # 收集形如 `from .xxx import ...` 的一级本地模块名
    local_mods = set()
    for ln in lines:
        s = ln.strip()
        if s.startswith("from .") and " import " in s:
            mod = s[len("from ."):].split(" import ")[0].strip()
            # 只取一级（跳过 `from .pkg.sub import`，那属包内子模块）
            if mod and "." not in mod:
                local_mods.add(mod)

    assert local_mods, "未能从 webui.py 解析出任何本地 import，解析逻辑可能失效"

    tracked = _tracked_paths()
    if not tracked:
        pytest.skip("无法从 git 读取跟踪列表")

    missing = []
    for mod in sorted(local_mods):
        rel = f"{_SRC_REL}/{mod}.py"
        # 模块也可能是包目录（如 webui/）——存在 __init__.py 即视为包
        pkg_init = f"{_SRC_REL}/{mod}/__init__.py"
        if rel in tracked or pkg_init in tracked:
            continue
        # 磁盘上根本没有 = 本就是坏 import，交给 import 测试报，不在此重复报
        if not os.path.exists(os.path.join(_SRC_ABS, f"{mod}.py")):
            continue
        missing.append(rel)

    assert not missing, (
        "webui.py 硬 import 了这些模块，但它们未纳入 git —— "
        "从 master clone 后 webui 直接 ModuleNotFoundError（#708）：\n  "
        + "\n  ".join(missing)
    )
