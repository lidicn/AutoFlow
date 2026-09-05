#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow 网关受控发布助手（方案 C 自更新的版本 tag 来源）。

用法：
  python scripts/tag_release.py minor [--note "一句话发布说明"] [--date YYYY-MM-DD]
  python scripts/tag_release.py patch  [--note "..."]
  python scripts/tag_release.py major  [--note "..."]

行为：
  1. 读取仓库根 VERSION（如 1.0.0），按 major/minor/patch 递增（patch 默认 +1）。
  2. 写回 VERSION；若给了 --note，在根 CHANGELOG.md 顶部插入对应版本小节。
  3. git add VERSION [CHANGELOG.md] && commit "release: vX.Y.Z"。
  4. git tag -a vX.Y.Z -m "..."。
  5. 推送：HEAD:master、HEAD:main（镜像双推）+ 推送 tag refs/tags/vX.Y.Z。
     remote 取 `git config --get remote.origin.url`（本地/CI 已配好 SSH）。

注意：本脚本只负责「打 tag + 推送」，不部署。NAS 活树通过「更新」页自更新拉取。
不传 --note 时仍会打 tag，但 CHANGELOG 不自动加小节（留待手工补）。
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO, "VERSION")
CHANGELOG_FILE = os.path.join(REPO, "CHANGELOG.md")


def _run(args, check=True):
    print("+ " + " ".join(args))
    r = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"❌ 命令失败 {args}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def read_version() -> str:
    if not os.path.isfile(VERSION_FILE):
        sys.exit(f"❌ 未找到 VERSION 文件：{VERSION_FILE}")
    with open(VERSION_FILE, encoding="utf-8") as f:
        return f.read().strip()


def bump(ver: str, kind: str) -> str:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", ver)
    if not m:
        sys.exit(f"❌ VERSION 格式应为 X.Y.Z，当前：{ver!r}")
    maj, mino, pat = (int(x) for x in m.groups())
    if kind == "major":
        maj, mino, pat = maj + 1, 0, 0
    elif kind == "minor":
        mino, pat = mino + 1, 0
    else:  # patch
        pat += 1
    return f"{maj}.{mino}.{pat}"


def prepend_changelog(new_ver: str, note: str, day: str) -> None:
    head = f"## {new_ver} ({day})\n- {note}\n"
    existing = ""
    if os.path.isfile(CHANGELOG_FILE):
        with open(CHANGELOG_FILE, encoding="utf-8") as f:
            existing = f.read()
    # 跳过已存在的同级标题（避免重复打同一个版本）
    if re.search(rf"^##\s+{re.escape(new_ver)}\b", existing, re.M):
        print(f"⚠️ CHANGELOG 已有 {new_ver} 小节，跳过插入")
        return
    with open(CHANGELOG_FILE, "w", encoding="utf-8") as f:
        f.write(head + "\n" + existing)
    print(f"✓ 已更新 CHANGELOG.md 顶部：{new_ver}")


def main() -> None:
    ap = argparse.ArgumentParser(description="AutoFlow 网关版本 tag 发布助手")
    ap.add_argument("bump", choices=["major", "minor", "patch"], help="递增级别")
    ap.add_argument("--note", default="", help="发布说明（写入 CHANGELOG 顶部小节）")
    ap.add_argument("--date", default=date.today().isoformat(), help="发布日期 YYYY-MM-DD")
    ap.add_argument("--no-push", action="store_true", help="只打本地 tag，不推送")
    args = ap.parse_args()

    old = read_version()
    new = bump(old, args.bump)
    print(f"版本：{old} → {new}")

    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(new + "\n")
    print(f"✓ 已写回 VERSION：{new}")

    if args.note:
        prepend_changelog(new, args.note, args.date)
        _run(["git", "add", "VERSION", "CHANGELOG.md"])
    else:
        _run(["git", "add", "VERSION"])

    tag = f"v{new}"
    _run(["git", "commit", "-m", f"release: {tag}"])
    _run(["git", "tag", "-a", tag, "-m", f"AutoFlow 网关 {tag}（{args.date}）"])
    print(f"✓ 已提交并打 tag：{tag}")

    if args.no_push:
        print("（--no-push）未推送。手动推送：")
        print(f"  git push origin HEAD:master HEAD:main && git push origin {tag}")
        return

    _run(["git", "push", "origin", "HEAD:master", "HEAD:main"])
    _run(["git", "push", "origin", f"refs/tags/{tag}"])
    print(f"✓ 已推送 {tag} 到 origin（master + main 镜像）。NAS「更新」页稍后可见。")


if __name__ == "__main__":
    main()
