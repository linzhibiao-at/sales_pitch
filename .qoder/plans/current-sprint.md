---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 当前迭代计划

## 迭代目标

建立 sales_pitch 的 harness engineering 规范体系（`.qoder/`），并以可本地执行的 guardrails 检查验证规范与代码一致，为后续迭代提供可验证的边界。

## 当前任务

| 状态 | 任务 | 说明 |
| --- | --- | --- |
| 已完成 | `.qoder` 文档体系 | AGENTS.md 入口 + architecture 三篇 + conventions 六篇 + plans 两篇 + reference 两篇 |
| 已完成 | guardrails 落地 | GR-01~GR-08 硬门禁 + advisory 两项，一键脚本对当前代码全绿（分层无违规） |
| 已完成 | 棘轮基线登记 | `generate` 60 行白名单 + pytest 基线写入登记表（已由 88 逐步收紧至 95） |
| 已完成 | 审计写异步化 | `audit_worker.py` 内存队列 + 后台批量写（executemany ≤50 条/批），MySQL 慢/断连不再阻塞事件循环；含断连重连与 atexit drain |
| 进行中 | 安全债处置 | config.yaml 内嵌密钥轮换、api_keys.yaml 移出 Git 跟踪（见 backlog 安全项） |
| 待开始 | CI 接入 | GitHub Actions 复跑 guardrails 一键脚本 |
| 待开始 | generate 拆分 | 退出棘轮白名单（≤50 有效行） |

## 验收标准

- `.qoder/AGENTS.md` 快速导航与实际文件一一对应。
- guardrails 一键脚本退出码 0（GR-01~GR-08 全过）。
- pytest 用例全绿（基线已随审计异步化与重连死锁修复增至 95），文档描述与代码现状零矛盾。
- 规范文档描述与代码现状零矛盾（含静默降级、错误信封、装配根例外）。

## 风险

- config.yaml 已提交真实 DashScope 密钥到远端仓库：即使本地移除，历史仍可见，**必须轮换密钥**。
- 限流为进程内近似，规范文档已如实标注，多 worker 部署下实际限额放大（治理项在 backlog）。
- CI 未接入前，guardrails 依赖提交者自觉执行；合入主干前需人工跑一键脚本。
