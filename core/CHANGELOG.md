# AutoFlow Core Changelog

## 1.0.0 (2026-08-30) — nr_client.py v3.0.0
- 首发：nr_client.py + 专用 SKILL.md + 一句话安装。
- nr_client.py 新增：doctor（环境自检）、inventory（全 tab 只读概览+归属标注）、
  write_flow（快照→PUT→回读校验一键写入）、inject_and_read（自愈闭环 context 桥）、
  compact（get --compact 剔渲染坐标省 token）。
- 安全脱敏（发行版）：移除内置默认 NR 地址/账密/HA 长期令牌；配置改
  env > ~/.autoflow-core/config.json；HA 断言改 HASS_SERVER/HASS_TOKEN 环境变量，
  未配置时优雅禁用；权威源默认位改 skill 安装目录。
