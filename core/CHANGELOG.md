# AutoFlow Core Changelog

## 1.0.1 (2026-08-30) — nr_client.py v3.0.1（T011 回单修复）
- **[P0] 修复 `restore_snapshot()` 全实例数据丢失**：旧实现逐条遍历扁平 GET /flows，
  把 tab 条目与 node 条目都当独立 flow PUT —— tab 被清空、node 孤儿化，会把整个实例写崩
  （T011 §6 实测触发，全用户 tab 节点归零）。改为经 deploy_all 整包 `POST /flows` 原子还原；
  新增「空快照拒绝还原」保护；返回 {restored_items, result}。
- **[A档] 用户手工流只读升级为代码层硬拦截**：write_flow / create_tab 对非 `af_*` 目标
  默认抛 NRGuardError；归属以**线上 label** 为准（防伪造前缀）；显式 `--allow-user-flow`
  仍可写（照常快照留底）。
- **[P2] 修复 lint 对末端节点误报**：真机实证（1234+ 节点）后从 _SINGLE_OUTPUT_TYPES 移除
  `debug`(175/175 零出边，误报 174) 与 `link out`(50/51 零出边，误报 50)，
  合计消除 224 个节点的假阳性。
- 文档：补连接排障（HTTPS/TLS 裸 IP 问题）、回滚须知（restore_snapshot 是整实例还原）、
  推荐用环境变量替代明文 config.json。
- 守卫测试：tests/test_core_v1.py 增至 17 项（新增 P0 原子还原、红线硬拦截×3、lint 误报）。

## 1.0.0 (2026-08-30) — nr_client.py v3.0.0
- 首发：nr_client.py + 专用 SKILL.md + 一句话安装。
- nr_client.py 新增：doctor（环境自检）、inventory（全 tab 只读概览+归属标注）、
  write_flow（快照→PUT→回读校验一键写入）、inject_and_read（自愈闭环 context 桥）、
  compact（get --compact 剔渲染坐标省 token）。
- 安全脱敏（发行版）：移除内置默认 NR 地址/账密/HA 长期令牌；配置改
  env > ~/.autoflow-core/config.json；HA 断言改 HASS_SERVER/HASS_TOKEN 环境变量，
  未配置时优雅禁用；权威源默认位改 skill 安装目录。
