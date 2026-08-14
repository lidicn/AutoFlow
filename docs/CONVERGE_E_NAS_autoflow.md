# AutoFlow 本地工作区收敛执行 runbook

> 本文件由 PM 在沙箱中准备；**git 收敛步骤必须在你本机真实终端（非 WorkBuddy 沙箱）执行**，
> 因为沙箱 git 写不落盘（2026-08-13 / 2026-08-14 两次实证：commit/ref 写入后跨调用即丢）。

## 背景（为什么 NAS 总领先 E:/autoflow）
`E:/autoflow` 的 `.git` 在 8-13 `git stash` 事故里对象库损坏（HEAD 无提交），此后我们改在
recovery 仓（`E:/af_recov*`）改码 + scp 到 NAS，从未回填 `E:/autoflow` → 它一直停在损坏态。
收敛纠正这件事：让 `E:/NAS/autoflow`（含全部修复的最全快照）重新成为带 git 的唯一定源。

## 当前事实（已核实）
- `E:/NAS/autoflow` = 从 NAS 活树（8-14 部署态）全量拷贝，**无 .git**，但代码最全：
  ACP(webui acp=7/app.js acp=3) + Help 页(index help_tab=2) + LLM 气泡(uifix=8) +
  `/api/llm/test` + `_resolve_key` + 裸500根因修复(httpx.HTTPError=4) 全在。
- 安全网：`E:/NAS/autoflow_snapshot_20260814`（含 data/ secrets 完整拷贝）已备。
- `E:/autoflow` = 损坏空仓（HEAD 无提交，git 不可用），收敛后归档。
- GitHub `origin/main` 已关联（`git@github.com:lidicn/AutoFlow.git`），但可能落后于 NAS 活树
  （llm-ui 修复还没推过）。

## ⚠️ 用户数据防泄漏（最高优先级）
`.gitignore` 已排除：`/data/`、`**/.webui_token`、`.env`（`.env.example` 保留）、`*.json`(全局)、
`.cbcache/`、`src_backup_*`、`skills_backup*`、`*.bak-*`。
`git add -A` 前务必 `git status` 确认以下**绝不出现**：`.webui_token`、`.env`(非 example)、
`data/llm_config.json`、`data/.webui_token`、任何 `*.secret`。

## 步骤（本机终端逐条执行）
```bat
REM 0. 进入仓库
cd /d E:/NAS/autoflow

REM 1. 初始化 + 关联远端（.git 已 init、origin 已加；若重跑先确认）
git rev-parse --is-inside-work-tree
git remote -v

REM 2. 拉取 GitHub 历史（真实网络）
git fetch origin

REM 3. 关键：reset --mixed 把 HEAD/index 指向 origin/main，但【保留工作树=我们的修复】
REM    这样我们的修复显示为变更，叠加在 origin/main 之上，不丢历史、不丢修复
git reset --mixed origin/main

REM 4. 防泄漏核验（必须看到 data/、.webui_token、.env 不在待提交列表）
git status

REM 5. 暂存 + 提交
git add -A
git status --short | findstr /i "webui_token .env data/ .secret" && echo "❌ 发现 secrets，中止！" || echo "✅ 无 secrets 泄漏"
git commit -m "reconcile: 本地 NAS 修复收敛到 main (ACP + LLM UI 气泡/账号池/测试按钮 + agent身份预留)"

REM 6. 推送
git push origin main

REM 7. 归档旧损坏仓（仅重命名，不删；可随时恢复）
cd /d E:/
move E:/autoflow E:/autoflow.bak-20260814

REM 8. 验证远程权威（不要信本地 packed-refs）
git ls-remote origin main
```

## 回滚预案
若 step 3/5 出错：旧文件全在 `E:/NAS/autoflow_snapshot_20260814`，`E:/autoflow.bak-20260814` 是
损坏仓归档，随时 `move` 回来。收敛失败不影响 NAS 生产（NAS 活树独立运行）。

## 收敛后的标准工作流（恢复约定）
改码在 `E:/NAS/autoflow` → `git commit`（本机终端）→ `git push origin main` →
NAS 同步（autoflow-nas-deploy 技能：scp + restart + 运行时验收）。
