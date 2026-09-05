#!/usr/bin/env python3
"""AutoFlow WebUI 文案 v2 批量替换脚本 - index.html"""
import re

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\index.html"

with open(FILE, "r", encoding="utf-8") as f:
    content = f.read()

replacements = [
    # === 顶栏 ===
    # 🧪 title
    ('title="DSL 验证任务池开关（关闭后 agent 调用将被拒绝）"',
     'title="DSL 验证池开关（关闭后 Agent 的 DSL 编译请求将被拒绝）"'),
    # 🧬 逃逸 → 原生节点
    ('<span class="btn-emoji">🧬</span><span class="btn-text">逃逸</span>',
     '<span class="btn-emoji">🧬</span><span class="btn-text">原生节点</span>'),
    # 🧬 title
    ('title="原生节点逃逸开关（中风险逃生舱，可随时关闭）"',
     'title="允许 Agent 提交原生节点（含 Function 节点，中风险，可随时关闭）"'),
    # ⚖ title
    ('title="部署策略（compiler_auto 下编译器产物标「可信」可自动部署，raw 仍须人审）"',
     'title="部署策略（自动生成的 flow 可免审直装，手写 raw flow 仍须人工审核）"'),
    # ♻️ title
    ('title="自愈闭环：agent 自主调试已部署 flow 的最大重试次数（三次机会失效保护，防自动修复死循环）"',
     'title="自动修复：Agent 自主调试已部署 flow 的最大重试次数（默认 3，防死循环）"'),

    # === 侧边导航 ===
    ('>Agents</button>', '>Agent 管理</button>'),
    ('>LLM 设置</button>', '>大模型设置</button>'),
    ('>LLM 助手</button>', '>AI 对话</button>'),
    ('>更新</button>', '>系统更新</button>'),

    # === 底部导航 ===
    ('<span>LLM助手</span>', '<span>AI对话</span>'),

    # === 帮助页术语统一 ===
    ('配置 MCP（把 agent 接进来）', '接入 Agent（MCP 配置）'),
    ('用 agent 编写 flow', '让 Agent 编写 flow'),
    ('在网关审核 / 撤销 agent 写的 flow', '审核 / 撤回 Agent 编写的 flow'),
    ('flow 跑不通？跟 agent 沟通 + 自愈闭环', 'flow 不工作？跟 Agent 沟通 + 自动修复'),
    ('inject 节点在自愈闭环里是必须的吗？（重点答疑）', 'inject 节点是自动修复必须的吗？（重点答疑）'),
    ('补充：身份码安全 / 顶部开关 / 子流程 / 审计', '补充：接入令牌安全 / 顶部开关 / 子流程 / 操作日志'),

    # 帮助页正文 - agent → Agent（首字母大写统一）
    ('让你用自然语言指挥 AI agent 去编写', '让你用自然语言指挥 AI Agent 去编写'),
    ('agent 编写 flow', 'Agent 编写 flow'),
    ('agent 提交场景 DSL', 'Agent 提交场景 DSL'),
    ('agent 可经 MCP 调用', 'Agent 可经 MCP 调用'),
    ('agent 永不自批准', 'Agent 永不自批准'),
    ('跟 agent 沟通', '跟 Agent 沟通'),
    ('agent 会按下面循环', 'Agent 会按下面循环'),
    ('agent 陷入', 'Agent 陷入'),
    ('agent 会如实告诉你', 'Agent 会如实告诉你'),
    ('agent 应当这样处置', 'Agent 应当这样处置'),
    ('agent 应说', 'Agent 应说'),
    ('agent 都能读到', 'Agent 都能读到'),
    ('agent 提交后', 'Agent 提交后'),
    ('agent 的 MCP 配置', 'Agent 的 MCP 配置'),
    ('agent 拿到一张', 'Agent 拿到一张'),
    ('agent / MCP 客户端', 'Agent / MCP 客户端'),
    ('agent 无法再连网关', 'Agent 无法再连网关'),
    ('agent 写的 flow', 'Agent 写的 flow'),
    ('agent 抛出的选择题', 'Agent 抛出的选择题'),

    # 身份识别码 → 接入令牌
    ('身份识别码', '接入令牌'),
    ('身份码', '接入令牌'),

    # 自愈闭环 → 自动修复
    ('自愈闭环', '自动修复'),

    # 审计日志 → 操作日志
    ('审计日志', '操作日志'),

    # 首次运行
    ('首次运行 · 风险须知', '首次使用 · 风险须知'),
    ('AutoFlow 网关可控制 <strong>真实电器与物理设备</strong>', 'AutoFlow 可控制 <strong>真实电器与设备</strong>'),
]

count = 0
for old, new in replacements:
    if old in content:
        content = content.replace(old, new)
        count += 1
    else:
        print(f"WARNING: not found: {old[:60]}...")

with open(FILE, "w", encoding="utf-8") as f:
    f.write(content)

print(f"\nDone: {count}/{len(replacements)} replacements applied to index.html")
