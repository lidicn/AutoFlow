#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控自更新（方案 C）单元测试：用本地临时 git 仓库模拟 NAS 活树，验证
update_check / perform_update 的 ref allowlist、备份、checkout、语法校验与回滚。

不依赖网络（AF_GIT_REMOTE 指向本地临时仓库），不在容器内运行故 restart=manual。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "src"))

from autoflow_gateway import self_update  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=check,
    )


def _git_out(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True,
    ).stdout


class SelfUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        # 远程指向自身（离线 ls-remote / fetch 可达）
        _git(self.repo, "remote", "add", "origin", self.repo)
        # 初始提交
        os.makedirs(os.path.join(self.repo, "src", "autoflow_gateway"))
        with open(os.path.join(self.repo, "src", "autoflow_gateway", "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(self.repo, "src", "autoflow_gateway", "x.py"), "w") as f:
            f.write("def f():\n    return 1\n")
        with open(os.path.join(self.repo, "run.py"), "w") as f:
            f.write("# v1\nprint('hi')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        self.base_commit = _git_out(self.repo, "rev-parse", "HEAD").strip()
        # 新提交 + 版本 tag（模拟一次发布）
        with open(os.path.join(self.repo, "run.py"), "w") as f:
            f.write("# v2\nprint('hi2')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "v2")
        _git(self.repo, "tag", "v1.0.1")
        self.tag_commit = _git_out(self.repo, "rev-parse", "v1.0.1").strip()
        # 一个非版本 tag（不应被自动更新纳入）
        _git(self.repo, "tag", "nightly")
        # 让活树停在一个「旧提交」上：存在更新的版本 tag 即表示「可更新」
        _git(self.repo, "checkout", "-f", self.base_commit)

        os.environ["AF_REPO_DIR"] = self.repo
        os.environ["AF_GIT_REMOTE"] = self.repo
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AF_REPO_DIR", None)
        os.environ.pop("AF_GIT_REMOTE", None)
        os.environ.pop("AF_UPDATE_ALLOW_REFS", None)

    def test_update_check_finds_latest_tag(self):
        chk = self_update.update_check()
        self.assertTrue(chk["git_present"])
        self.assertTrue(chk["available"])
        self.assertEqual(chk["target_ref"], "v1.0.1")
        self.assertEqual(chk["target_commit"], self.tag_commit)
        # 非版本 tag nightly 不应出现在候选里
        self.assertNotIn("nightly", [t["tag"] for t in chk["tags"]])

    def test_perform_update_applies_and_backs_up(self):
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["restart"], "manual")  # 测试环境无 /.dockerenv
        # 工作树应已切到 tag 提交
        head = _git_out(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual(head, self.tag_commit)
        # 备份文件应存在且为 gzip
        self.assertTrue(os.path.exists(res["backup"]))
        self.assertTrue(res["backup"].endswith(".tar.gz"))

    def test_reject_unknown_ref(self):
        chk = self_update.update_check(ref="not-a-real-ref")
        self.assertIsNone(chk["target_commit"])
        self.assertFalse(chk["available"])

    def test_explicit_sha_requires_allowlist(self):
        # 传 tag 的 commit SHA 作为 ref：走 SHA 白名单路径，未列入白名单应被拒
        chk = self_update.update_check(ref=self.tag_commit)
        self.assertIsNone(chk["target_commit"])
        self.assertFalse(chk["available"])
        # 列入白名单后应被接受
        os.environ["AF_UPDATE_ALLOW_REFS"] = self.tag_commit
        chk2 = self_update.update_check(ref=self.tag_commit)
        self.assertEqual(chk2["target_commit"], self.tag_commit)

    def _write_version(self, ver: str):
        with open(os.path.join(self.repo, "VERSION"), "w", encoding="utf-8") as f:
            f.write(ver + "\n")

    def test_version_based_available(self):
        # 活树 VERSION=1.0.0，最新远程 tag=v1.0.1 → 应判定「可更新」（语义化比对）
        self._write_version("1.0.0")
        chk = self_update.update_check()
        self.assertTrue(chk["available"], chk)
        self.assertEqual(chk["current_version"], "1.0.0")
        self.assertEqual(chk["latest_tag"], "v1.0.1")
        self.assertEqual(chk["target_ref"], "v1.0.1")

    def test_version_equal_already_latest(self):
        # 活树 VERSION=1.0.1，与最新 tag 同版本 → 已是最新，不触发更新
        self._write_version("1.0.1")
        chk = self_update.update_check()
        self.assertFalse(chk["available"], chk)
        self.assertEqual(chk["reason"], "已是最新")
        self.assertEqual(chk["current_version"], "1.0.1")

    def test_already_latest_noop(self):
        # 先把工作树切到 tag 提交，再更新到同一 tag → 应判已最新，不触发 restart
        _git(self.repo, "checkout", "-f", self.tag_commit)
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertTrue(res.get("ok"))
        self.assertTrue(res.get("already_latest"))


if __name__ == "__main__":
    unittest.main()
