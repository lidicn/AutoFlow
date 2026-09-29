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
        # 让活树停在一个「旧提交」上：存在更新的版本 tag 即表示「可更新」。
        # 自更新后 HEAD 必停在 detached tag 上，因此活树体检只拦「脏树（dirty）」，
        # 不放行 detached —— 否则会陷入「自更新一次后再也无法自更新」的死锁。
        # 这里先挂一个 prod 分支作为基线（detached 场景由下面的专项测试覆盖）。
        _git(self.repo, "checkout", "-b", "prod", self.base_commit)

        os.environ["AF_REPO_DIR"] = self.repo
        os.environ["AF_GIT_REMOTE"] = self.repo
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AF_REPO_DIR", None)
        os.environ.pop("AF_GIT_REMOTE", None)
        os.environ.pop("AF_UPDATE_ALLOW_REFS", None)
        os.environ.pop("AF_UPDATE_ALLOW_DIRTY", None)

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

    # ── 活树体检守卫（NAS 2026-09-28 事故：升级险些冲掉手工部署的活树）──

    def test_clean_tree_is_safe(self):
        chk = self_update.update_check()
        self.assertTrue(chk["safe_to_update"], chk)
        self.assertFalse(chk["repo_state"]["detached"])
        self.assertFalse(chk["repo_state"]["dirty"])

    def test_dirty_worktree_blocks_update(self):
        # 模拟 NAS 活树：已跟踪文件被本地改写（端口绑定、NAS 特有配置等）
        with open(os.path.join(self.repo, "run.py"), "w", encoding="utf-8") as f:
            f.write("# local hack\nprint('local')\n")
        head_before = _git_out(self.repo, "rev-parse", "HEAD").strip()
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("blocked"), "dirty_worktree")
        self.assertIn("活树不干净", res.get("error", ""))
        # 关键：绝不能动代码，也绝不能走到备份/重启
        self.assertEqual(_git_out(self.repo, "rev-parse", "HEAD").strip(), head_before)
        self.assertEqual(os.listdir(self.data), [])
        # update_check 也应如实标记为不可升级
        chk = self_update.update_check()
        self.assertFalse(chk["safe_to_update"])
        self.assertFalse(chk["available"])
        self.assertEqual(chk["repo_state"]["changed_files"], 1)

    def test_clean_detached_tag_is_safe(self):
        # ★ 回归测试：自更新后 HEAD 必停在 detached tag 上。clean 的 detached 必须允许
        #   升级，否则会陷入「自更新一次后再也无法自更新」的死锁（2026-09-29 守卫修复）。
        _git(self.repo, "checkout", "--detach", self.base_commit)
        chk = self_update.update_check()
        self.assertTrue(chk["repo_state"]["detached"])
        self.assertTrue(chk["safe_to_update"], chk)  # clean detached 不再拦截
        # 执行更新应成功切到目标 tag：clean detached 上 checkout -f 仅移动 HEAD，
        # 不会冲掉任何本地改动（与 dirty 拦截互为对照）。
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertTrue(res.get("ok"), res)
        head = _git_out(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual(head, self.tag_commit)

    def test_dirty_detached_still_blocks(self):
        # detached + 脏树：危险的是「脏」而非「detached」。守卫必须按 dirty 拦截，
        # 证明修复后守卫的判据是本地改动，而不是简单的 detached 状态。
        _git(self.repo, "checkout", "--detach", self.base_commit)
        with open(os.path.join(self.repo, "run.py"), "w", encoding="utf-8") as f:
            f.write("# local hack on detached\n")
        chk = self_update.update_check()
        self.assertTrue(chk["repo_state"]["detached"])
        self.assertTrue(chk["repo_state"]["dirty"])
        self.assertFalse(chk["safe_to_update"], chk)
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("blocked"), "dirty_worktree")

    def test_untracked_files_do_not_block(self):
        # 未跟踪文件（data/、*.bak 等）不会被 checkout -f 删除 → 不该算脏
        with open(os.path.join(self.repo, "docker-compose.yml.bak"), "w", encoding="utf-8") as f:
            f.write("backup\n")
        chk = self_update.update_check()
        self.assertTrue(chk["safe_to_update"], chk)
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertTrue(res.get("ok"), res)

    def test_allow_dirty_env_overrides(self):
        with open(os.path.join(self.repo, "run.py"), "w", encoding="utf-8") as f:
            f.write("# local hack\n")
        os.environ["AF_UPDATE_ALLOW_DIRTY"] = "1"
        res = self_update.perform_update(ref="v1.0.1", repo_dir=self.repo, data_dir=self.data)
        self.assertTrue(res.get("ok"), res)

    def test_unreachable_remote_reports_network_not_missing_tag(self):
        # 远端不可达时，不能退化成「远程无可用版本 tag」（会把网络故障伪装成产品状态）
        os.environ["AF_GIT_REMOTE"] = os.path.join(self.tmp, "no-such-repo")
        chk = self_update.update_check(ref="v1.0.1")
        self.assertFalse(chk["available"])
        self.assertIn("不可达", chk.get("reason", ""))
        self.assertNotEqual(chk.get("reason"), "远程无可用版本 tag")

    def test_fallback_to_mirror_when_primary_empty(self):
        # 主远端（默认 github.com）不可达返回空时，update_check 应自动尝试
        # ghproxy.net 兜底镜像并列出版本，而不是退化成「无可用版本 tag」。
        # 自动兜底只对默认主远端生效——用户显式设置的 AF_GIT_REMOTE 不自动跳镜像。
        real = self_update.list_remote_tags

        def fake(repo, remote_url=None):
            if remote_url is None or remote_url == self_update.DEFAULT_REMOTE:
                return []  # 主远端不可达
            return [{"tag": "v1.0.1", "commit": self.tag_commit}]  # 兜底镜像可达

        self_update.list_remote_tags = fake
        os.environ.pop("AF_GIT_REMOTE", None)  # 模拟默认主远端（github.com）
        try:
            chk = self_update.update_check()
            self.assertTrue(chk["available"], chk)
            self.assertEqual(chk["target_ref"], "v1.0.1")
            self.assertEqual(chk["remote_used"], self_update.FALLBACK_MIRRORS[0])
        finally:
            self_update.list_remote_tags = real

    def test_no_auto_fallback_for_custom_remote(self):
        # 用户显式设置 AF_GIT_REMOTE（即便是坏的）时，不自动跳 ghproxy 兜底
        real = self_update.list_remote_tags

        def fake(repo, remote_url=None):
            # 任一远端都返回空：自定义远端不可达，且不应触发兜底
            return []

        self_update.list_remote_tags = fake
        os.environ["AF_GIT_REMOTE"] = os.path.join(self.tmp, "no-such-repo")
        try:
            chk = self_update.update_check()
            self.assertFalse(chk["available"])
            self.assertEqual(chk["remote_used"], os.path.join(self.tmp, "no-such-repo"))
        finally:
            self_update.list_remote_tags = real


if __name__ == "__main__":
    unittest.main()
