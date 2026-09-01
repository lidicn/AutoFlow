## 1.1.0 (2026-08-31)
- 版本管理落地后首个递增版：self_update 语义化「已是最新」判定 + WebUI 显示运行版本号

# AutoFlow 网关 Changelog

> 本文件记录**网关本体**（WebUI + MCP + 编译/校验/部署）的发布版本。
> 注意：`core/` 下的 `VERSION`/`CHANGELOG.md` 是 **AutoFlow Core（专家路径 skill）** 的独立版本轨道，与本文件无关。
> 发布流程见 `scripts/tag_release.py`；`v*` tag 即「方案 C 受控自更新」的可用更新来源。

## 1.0.0 (2026-08-31) — 网关首个发布 tag
- 受控自更新（方案 C）落地：WebUI「更新」页 + `POST /api/admin/self-update`
  + `GET /api/admin/update-check`（owner 专属，RBAC fail-closed 把关）。
- 自更新安全约束：仅接受 v* 版本 tag 或白名单 SHA；备份→fetch→checkout -f→
  py_compile 校验→失败回滚→成功 SIGTERM 重启；绝不 `git clean -f` / `reset --hard`。
- 版本管理：根 `VERSION` 文件随自更新一并 checkout，故「更新」页显示的「当前版本」
  即为实际运行版本；比对待比对保证「已是最新」判定准确。
- 中国网络适配：自更新 remote 默认走 SSH（`git@github.com:lidicn/AutoFlow.git`），
  容器内 `GIT_SSH_COMMAND` 直指私钥、跳过挂载的 `~/.ssh/config`（属主为 lidicn，root 拒访）；
  git 调用统一 `-c safe.directory=*` 绕过 dubious ownership。
