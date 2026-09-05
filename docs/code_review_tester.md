# 测试覆盖率审查报告 — AutoFlow V2

**审查人**: tester  
**日期**: 2026-09-05  
**项目**: E:\NAS\autoflow-v2  

---

## 1. 测试资产概览

| 指标 | 数值 |
|------|------|
| 源模块数 (src/autoflow_gateway/) | 51 个 .py 文件 |
| 测试文件数 (tests/) | 177 个 test_*.py |
| 测试函数总数 | ~1,500 个 test_ 函数 |
| conftest.py | 1 个，含 FakeNR/FakeHA stub + 环境变量锁定 |
| 测试运行器 | run_tests.py (支持 --live / --smoke / 按文件名过滤) |

### 1.1 测试类型分布（按文件名分类）

| 类型 | 估算文件数 | 占比 |
|------|-----------|------|
| 单元测试 (unit) | ~60 | 34% |
| 缺陷回归 (defect/round/wb*) | ~65 | 37% |
| 集成测试 (integration: subflow/gate/lint) | ~30 | 17% |
| E2E 测试 (e2e_trace/apply/verify) | ~15 | 9% |
| 安全/门禁 (secrets/no_secrets/auth) | ~5 | 3% |
| 其他 (helper, compiler, conftest) | ~5 | 3% |

### 1.2 优秀实践

- **conftest.py 设计精良**: FakeNRClient/FakeHA 内存 stub，sys.path 优先级锁定，自动 monkeypatch 环境变量。
- **test_no_secrets.py**: 扫描 git ls-files（而非磁盘遍历），5 类正则模式，含自检用例（test_secret_scanner_actually_detects）确保门禁不会静默失效。设计注释详尽，体现了从 #708 事故中吸取的教训。
- **test_false_green_family.py**: 聚合 6 种"假绿"模式，每种独立断言 fully_verified 为 False，确保验证系统诚实降级而非伪造通过。
- **test_core_v1.py**: 内置 ThreadingHTTPServer fake NR，覆盖 4 道防线（脱敏、配置、行为、doctor），378 行测试覆盖完整。
- **缺陷驱动测试文化**: defect_*.py / round*.py / wb*.py 系列共 65+ 文件，每个严重缺陷都有对应的回归测试。

---

## 2. 模块覆盖率映射

### 2.1 已覆盖模块（有专门测试文件）

| 源模块 | 对应测试文件 | 覆盖质量 |
|--------|-------------|----------|
| dsl_engine.py | test_dsl_engine.py (955行), test_dsl_round4.py, test_nested_conditions.py 等 | ★★★★★ 非常充分 |
| gateway.py | test_gateway.py, test_unified_gate.py, test_gateway_logging.py 等 | ★★★★★ 非常充分 |
| flow_linter.py | test_flow_linter.py (808行), test_flow_linter_b1b2.py, test_flow_linter_jsonata.py | ★★★★★ 非常充分 |
| subflows.py | test_subflow_*.py 系列 (16+ 文件) | ★★★★★ 非常充分 |
| vhass.py | test_vhass.py, test_gate_vhass_deepen.py | ★★★★ 充分 |
| webui.py | test_webui.py, test_webui_settings.py, test_webui_password_login.py | ★★★★ 充分 |
| defense.py | test_defense.py | ★★★★ 充分 |
| device_guard.py | test_device_guard.py | ★★★★ 充分 |
| config.py | 被 30+ 测试文件间接覆盖 | ★★★★ 充分 |
| connections.py | test_connections_settings.py | ★★★ 良好 |
| llm_client.py | test_llm_client.py, test_llm_webui_agent.py | ★★★ 良好 |
| mcp_server.py | test_mcp_contract_drift.py, test_mcp_server_merge.py | ★★★ 良好 |
| identity.py | test_identity.py | ★★★ 良好 |
| audit.py | test_audit.py | ★★★ 良好 |
| sync.py | test_sync.py | ★★★ 良好 |
| telemetry.py | test_telemetry.py | ★★★ 良好 |
| template_lib.py | test_template_lib.py | ★★★ 良好 |
| templates.py | test_templates_brightness.py | ★★★ 良好 |
| proposals.py | test_proposals.py, test_proposals_w2.py | ★★★ 良好 |
| api_specs.py | test_api_specs.py | ★★★ 良好 |
| api_config_store.py | test_api_config_store.py | ★★★ 良好 |
| self_update.py | test_self_update.py | ★★★ 良好 |
| acp_client.py | test_acp_protocol_and_tokens.py, test_acp_server.py | ★★★ 良好 |
| confirm.py | test_confirm_gate_enh.py | ★★★ 良好 |
| debug_bridge.py | test_debug_bridge.py | ★★★ 良好 |

### 2.2 ⚠️ 无专门测试的模块（16 个 / 51 = 31%）

| 模块 | 严重程度 | 说明 |
|------|---------|------|
| **cli.py** | 🔴 P1 | CLI 入口点，命令行参数解析和错误处理无测试 |
| **flow_simulator.py** | 🔴 P1 | 流模拟器，核心仿真逻辑无测试 |
| **ha_layer.py** | 🔴 P1 | HA 层抽象，与 Home Assistant 交互的核心代码无测试 |
| **snapshot_manager.py** | 🟡 P2 | 快照管理，数据持久化相关 |
| **flow_diff.py** | 🟡 P2 | 流差异比较，变更追踪功能 |
| **build_scene.py** | 🟡 P2 | 场景构建，DSL→flow 编译的上游 |
| **command_store.py** | 🟡 P2 | 命令存储，状态持久化 |
| **plan_store.py** | 🟡 P2 | 计划存储 |
| **task_store.py** | 🟡 P2 | 任务存储 |
| **schemas.py** | 🟡 P2 | Pydantic schema 定义，数据校验 |
| **tab_organizer.py** | 🟡 P2 | Tab 组织逻辑 |
| **error_knowledge.py** | 🟢 P3 | 错误知识库 |
| **notes.py** | 🟢 P3 | 笔记系统 |
| **lib/affordance.py** | 🟢 P3 | 设备能力推断 |
| **lib/ha_client.py** | 🟢 P3 | HA HTTP 客户端 |
| **mock_docker_api.py** | 🟢 P3 | Docker API mock |

> 注：部分模块可能被其他测试间接覆盖（如 ha_layer 可能被 test_vhass.py 间接触碰），但无直接单元测试。

---

## 3. 测试质量分析

### 3.1 参数化测试（Parametrize）— 🔴 严重不足

```
全仓库 parametrize 使用次数: 5 处（分布在 3 个文件中）
```

| 文件 | parametrize 用例 |
|------|-----------------|
| test_acp_protocol_and_tokens.py | 1 处 |
| test_contracts_surface.py | 3 处 |
| test_import_hygiene.py | 1 处 |

**问题**: 1,500 个测试函数中仅 5 处使用了 pytest.mark.parametrize。大量测试可以受益于数据驱动测试：
- DSL 解析边界（各种特殊字符、嵌套深度、空参数）
- Flow linter 规则验证（每种规则的正反例）
- 配置项边界（各种非法值、空值、极值）
- WebUI API 端点测试（各种 HTTP 方法 + 状态码组合）

**建议**: 目标将 parametrize 使用提升至 50+ 处，覆盖所有可枚举的输入空间。

### 3.2 边界条件测试 — ★★ 部分覆盖

**已有覆盖**:
- 空 flows 数组、空快照、空 payload → 多处 assert
- 超时处理 (test_core_v1.py: test_inject_and_read_timeout_returns_none)
- 所有权红线拦截 (test_core_v1.py: test_write_flow_blocks_user_tab_by_default)
- 假绿检测 (test_false_green_family.py)
- 回滚机制 (test_core_v1.py: test_write_flow_readback_mismatch_rolls_back)

**缺口**:
- ❌ 大文件/大数据集边界（如 100+ 节点的 flow、1000+ 实体）
- ❌ 数值边界（极小/极大 brightness 值、temperature 范围外）
- ❌ 并发写入竞态条件（虽有 test_wb16_concurrency.py，但未覆盖所有并发场景）
- ❌ Unicode/特殊字符输入（CJK 实体名、emoji、null 字节）
- ❌ JSON 深度嵌套（10 层以上嵌套对象）
- ❌ 网络超时/部分失败（连接中断、503、429）

### 3.3 测试隔离性 — ★★★★ 良好

- conftest.py 提供 FakeNR/FakeHA stub，避免依赖真实服务
- test_core_v1.py 内置 ThreadingHTTPServer，每个用例前重置 fake state
- autouse fixture 锁定 AF_MCP_HOST=127.0.0.1 和 AF_DEPLOY_POLICY=review_all
- 部分测试仍使用手动 sys.path.insert（20+ 个文件），与 conftest 冗余

### 3.4 测试命名与组织 — ★★★ 良好但有历史包袱

**优点**:
- 测试命名语义化（test_write_flow_readback_mismatch_rolls_back）
- 缺陷测试按编号组织（defect_d1d4, wb93_f12 等）
- 中文注释详尽，说明每个测试的背景和动机

**问题**:
- 大量历史前缀测试（round2/3/4/20, wb4/16/24/25/84/88/90/91/92/93）命名碎片化
- 部分测试文件混用 unittest.TestCase 和 pytest 风格
- regression/reg_m/ 目录下的辅助脚本未统一命名规范

### 3.5 测试运行配置 — 🟡 无覆盖率工具

pyproject.toml 中 **无** `[tool.coverage]` 或 `[tool.pytest.ini_options]` 配置：
- 没有 coverage 插件配置
- 没有覆盖率阈值门禁
- 没有 pytest 配置（测试标记、超时、并行等）
- run_tests.py 手动管理测试执行，不依赖 pytest 的配置文件

**建议**: 添加 pytest.ini 或 pyproject.toml 配置：
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: requires real NR/HA", "slow: takes > 10s"]
addopts = "--strict-markers -p no:cacheprovider"

[tool.coverage.run]
source = ["src/autoflow_gateway"]
omit = ["*/__pycache__/*", "*_backup_*/*"]

[tool.coverage.report]
fail_under = 70
```

---

## 4. 关键覆盖缺口（按风险排序）

### 🔴 P0 — 必须补充

| 缺口 | 风险 | 建议 |
|------|------|------|
| **cli.py 无测试** | CLI 是用户/agent 的直接入口，参数解析错误会导致静默失败 | 为 main() 和所有子命令写单元测试 |
| **无覆盖率门禁** | 无法量化追踪覆盖率，回退无告警 | 添加 coverage + fail_under 门禁 |
| **无 parametrize 数据驱动测试** | 边界条件覆盖依赖人工记忆，易遗漏 | 对 DSL 解析、linter 规则、API 端点系统使用 parametrize |

### 🟡 P1 — 应该补充

| 缺口 | 风险 | 建议 |
|------|------|------|
| **ha_layer.py 无测试** | HA 层是与外部系统交互的关键抽象 | Mock HA API，覆盖 get/set/call_service |
| **flow_simulator.py 无测试** | 仿真器输出直接影响 gate 判定 | 覆盖正向/负向/边界仿真场景 |
| **build_scene.py 无测试** | 场景构建是 DSL→flow 的上游 | 覆盖各种 DSL 场景的编译输出 |
| **网络错误处理无测试** | 生产中 NR/HA 可能超时、断连、503 | 用 mock 模拟网络故障 |
| **并发/竞态测试不足** | 多 agent 并发操作同一 flow 可能冲突 | 扩展 test_wb16_concurrency.py |

### 🟢 P2 — 建议补充

| 缺口 | 风险 | 建议 |
|------|------|------|
| snapshot_manager/flow_diff/plan_store/task_store 无测试 | 数据持久化层缺乏保障 | 每个 store 至少覆盖 CRUD + 异常路径 |
| 数值边界测试缺失 | brightness=0/255、temperature=-50/100 等 | parametrize 覆盖数值范围 |
| Unicode/特殊字符无测试 | CJK 实体名、emoji 场景名 | 添加中文/emoji 实体名测试 |
| schemas.py 无测试 | Pydantic 校验逻辑可能有漏洞 | 覆盖合法/非法 schema 输入 |

---

## 5. 回归测试体系评估

### 5.1 回归测试覆盖范围

项目展现了优秀的缺陷驱动开发文化：

| 系列 | 文件数 | 覆盖范围 |
|------|--------|---------|
| defect_*.py | 16 | C1-C8 缺陷系列、D1-D19 DSL/Lint 缺陷 |
| round*.py | 5 | Round2-20 bug 修复 |
| wb*.py | 20+ | WB1-WB93 工作批次修复 |
| test_false_green_family.py | 1 | 6 种假绿模式 |
| test_compiler_regression_*.py | 2 | 编译器回归 |

**总计**: 45+ 个回归测试文件，覆盖历史缺陷的复现和修复验证。

### 5.2 回归测试问题

- 命名碎片化（round2/3/4/20, wb4/16/24/25/84/88/90/91/92/93）难以导航
- 部分回归测试可能是临时性（如 test_defect_round3_*.py），应整合到对应模块的测试中
- 无自动清理机制，随着版本迭代测试文件持续累积

---

## 6. 安全测试评估

### 6.1 已有安全措施

- **test_no_secrets.py**: 自动化密钥/内网 IP 扫描，含自检机制（7 类模式）
- **test_core_v1.py**: 脱敏守卫（禁止内网 IP、用户名、JWT）
- **test_repo_integrity.py**: 仓库完整性检查
- **test_import_hygiene.py**: 导入卫生检查
- **test_contracts_surface.py**: 契约表面检查（禁止特定导出）

### 6.2 安全测试缺口

- ❌ 输入验证测试（SQL 注入、XSS、路径遍历）
- ❌ 认证/授权绕过测试（token 过期、权限提升）
- ❌ 速率限制测试（已知有 rate_limit 相关修复脚本，但无测试）
- ❌ CSRF 测试（WebUI 是否有 CSRF 保护？）
- ❌ 加密/传输层测试（HTTPS 配置、证书验证）

---

## 7. 总结与建议

### 7.1 总体评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 测试数量 | ★★★★★ | 177 个测试文件、~1500 个测试函数，覆盖量大 |
| 测试质量 | ★★★★ | conftest 设计精良，注释详尽，但 parametrize 严重不足 |
| 回归文化 | ★★★★★ | 45+ 回归文件，每个缺陷都有对应测试 |
| 边界条件 | ★★ | 有覆盖但系统性不足，parametrize 仅 5 处 |
| 覆盖率工具 | ★ | 无 coverage 配置，无阈值门禁 |
| 安全测试 | ★★★ | 密钥扫描和脱敏守卫优秀，但缺认证/输入验证 |
| 并发测试 | ★★ | 仅有 1 个并发测试文件，覆盖不足 |
| 模块覆盖率 | ★★★ | 31% 模块无专门测试（16/51） |

### 7.2 优先行动项

1. **立即**: 添加 pyproject.toml 覆盖率配置 + fail_under 门禁
2. **本周**: 为 cli.py、ha_layer.py、flow_simulator.py 编写单元测试
3. **本迭代**: 将 DSL 解析和 flow_linter 测试改为 parametrize 数据驱动
4. **持续**: 将 regression/reg_m/ 下的临时回归测试整合到对应模块测试中

### 7.3 测试统计摘要

```
总测试文件:        177
总测试函数:        ~1,500
源模块总数:        51
已覆盖模块:        35 (69%)
未覆盖模块:        16 (31%)
parametrize 用例:  5 (严重不足)
conftest fixture:  4 (FakeNR, FakeHA, 环境变量锁定, err_base)
回归测试文件:      45+
安全测试文件:      5+
```
