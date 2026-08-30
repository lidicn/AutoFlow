# AutoFlow Core — 一句话安装

## 给 agent 的一句话（复制整段发给你的 AI 助手即可）

```text
请帮我安装 AutoFlow Core（Node-RED 智能体编程核心版），全程自主完成：
1. 下载 https://raw.githubusercontent.com/lidicn/AutoFlow/master/core/skill/SKILL.md
   保存为 ~/.workbuddy/skills/autoflow-core/SKILL.md（目录不存在就创建；
   非 WorkBuddy 环境保存到你的 skill 目录，或任意目录并在下一步记下路径）
2. 下载 https://raw.githubusercontent.com/lidicn/AutoFlow/master/core/skill/scripts/nr_client.py
   保存为 ~/.workbuddy/skills/autoflow-core/scripts/nr_client.py
3. 问我要 Node-RED 的地址、用户名、密码，写入 ~/.autoflow-core/config.json：
   {"url":"http://<主机>:<端口>","username":"...","password":"..."}
   （文件含明文密码，提醒我该目录仅本机使用）
4. 运行 python ~/.workbuddy/skills/autoflow-core/scripts/nr_client.py doctor 自检，
   login 与 nr_reachable 为 true 且无致命 issues 即安装成功；有问题按提示修好后重跑。
5. 完成后用一句话告诉我：装好了什么、装在哪、下一步能干什么。
```

> WorkBuddy 用户可把上面整段直接粘贴到对话；其他 agent（Claude Code / Cursor 等）
> 同样适用，只要它能下载文件、写文件、跑命令。

## 安装后 agent 获得的能力

- `doctor`：环境自检（配置 → 登录 → 实例概览 → 护栏状态）
- `inventory`：全实例 tab 只读概览（自动标注 af_* 归属与可写性）
- `write-flow`：一键安全写入（快照 → lint → 节点数熔断 → PUT → 回读校验）
- `inject-read`：自愈闭环（触发 inject → 轮询读 context 捕获 → 断言）
- 完整 CLI 与黄金法则见 SKILL.md

## 手动安装（不经过 agent）

```bash
mkdir -p ~/.workbuddy/skills/autoflow-core/scripts ~/.autoflow-core
curl -o ~/.workbuddy/skills/autoflow-core/SKILL.md \
  https://raw.githubusercontent.com/lidicn/AutoFlow/master/core/skill/SKILL.md
curl -o ~/.workbuddy/skills/autoflow-core/scripts/nr_client.py \
  https://raw.githubusercontent.com/lidicn/AutoFlow/master/core/skill/scripts/nr_client.py
cat > ~/.autoflow-core/config.json <<'EOF'
{"url": "http://<主机>:<端口>", "username": "你的用户名", "password": "你的密码"}
EOF
python ~/.workbuddy/skills/autoflow-core/scripts/nr_client.py doctor
```

## 可选：HA 断言

`verify` 命令的实体状态断言需要环境变量：

```bash
export HASS_SERVER=http://<ha-host>:8123
export HASS_TOKEN=<长期访问令牌>
```

不配置也能用（NR 侧验证照常，仅跳过 HA 断言）。

## 升级

nr_client.py 内置权威源自动同步：重装时重新执行一句话安装即可覆盖为最新版；
也可手动重新下载第 2 步的文件。版本查询：`python scripts/nr_client.py version`。

## 安全说明

- 本核心版不内置任何默认地址/账密/令牌；配置只存本机 `~/.autoflow-core/`。
- 黄金法则：`af_*` 前缀 flow 才可写，用户手工 flow 一律只读；prod 实例默认禁写。
- 全部写入留痕：`~/.autoflow-core/logs/nr_operations.log`；写前自动快照可回滚。
