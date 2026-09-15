---
name: openspec-doc-backend
description: Generate a backend development design document from an OpenSpec change's planning artifacts (proposal/specs/design). Use when the user wants a team-review-ready backend dev design doc after running /opsx:propose.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
---

Generate a development design document (开发设计文档) from an existing OpenSpec change.

**Positioning**: Translate OpenSpec planning artifacts into a team-review-ready design document. proposal.md supplies why/what, specs/ supply the behavior contract, design.md supplies the technical approach. The output is for humans (dev, product, QA, review meetings), organized by individual requirement with engineering detail (interfaces, data storage, flows).

**Prerequisite**: The change must already be proposed — `openspec/changes/<name>/` must contain proposal.md and specs/. If they are missing, tell the user to run `/opsx:propose` first; never fabricate content.

**Store selection:** If the user names a store, or the work lives in one, run `openspec store list --json` to discover ids, then pass `--store <id>` on `list`/`show`/`status`/`instructions`. Otherwise act on the nearest local `openspec/` root.

**Input**: Optional change name after `/opsx:doc-backend` (e.g. `/opsx:doc-backend add-user-login`). If omitted, infer from context; if ambiguous, run `openspec list` and ask the user to choose.

**Output**: Write to `openspec/changes/<name>/doc-backend.html` (HTML format with embedded CSS).

**Steps**

1. Resolve the change name and its directory `openspec/changes/<name>/`.

2. Read planning artifacts from disk (re-read, do not rely on memory):
   - `proposal.md` — why/what, Capabilities
   - `specs/**/spec.md` — requirements + acceptance scenarios (behavior contract)
   - `design.md` (if present) — technical approach, decisions, risks
   - `tasks.md` (if present) — implementation steps, aids requirement breakdown

3. Generate the document as **HTML with embedded CSS** following this structure:

```markdown
# <项目/变更> 开发设计文档

> 需求来源：openspec/changes/<变更名>/（proposal.md / specs / design.md）

## 一、改造总览
| # | 需求 | 核心改造 | 前端接口 | 新增表/字段 | 外部依赖 |

## 二、需求一：<需求名>
### 2.1 需求回顾
### 2.2 方案说明
### 2.3 整体流程        (PlantUML 时序图)
### 2.4 接口设计        (字段表：字段/类型/必填/说明)
### 2.5 数据存储        (新增表/字段/缓存/复用)
### 2.6 待确认事项      (事项/责任方/状态)

## 三、需求二：<需求名>
## 五、整体节奏建议
```

**Rules**
- Split proposal's "What Changes / Capabilities" into one requirement section each.
- Interfaces: give field tables (field/type/required/description).
- Data: state which tables/fields are added, and what is reused.
- Flows: express with PlantUML sequence diagrams.
- Anything inferred or undecided (external API contract, table/field final names, auth method, etc.) must go into 「待确认事项」, never presented as a confirmed fact.
- Read-only: this skill only writes `doc-backend.html`; it does not modify proposal/specs/design/tasks or any business code.
- After writing, summarize the location and each requirement section, and prompt the user to review.
