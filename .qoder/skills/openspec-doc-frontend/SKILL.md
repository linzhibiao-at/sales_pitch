---
name: openspec-doc-frontend
description: Generate a frontend development design document from an OpenSpec change's planning artifacts (proposal/specs/design). Use when the user wants a team-review-ready frontend dev design doc after running /opsx:propose.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
---

Generate a frontend development design document (前端开发设计文档) from an existing OpenSpec change.

**Positioning**: Translate OpenSpec planning artifacts into a team-review-ready frontend design document. proposal.md supplies why/what, specs/ supply the behavior contract, design.md supplies the technical approach. The output is for humans (frontend dev, product, QA, review meetings), organized by individual requirement with frontend engineering detail (pages, components, interactions, state, API integration, telemetry).

**Prerequisite**: The change must already be proposed — `openspec/changes/<name>/` must contain proposal.md and specs/. If they are missing, tell the user to run `/opsx:propose` first; never fabricate content.

**Store selection:** If the user names a store, or the work lives in one, run `openspec store list --json` to discover ids, then pass `--store <id>` on `list`/`show`/`status`/`instructions`. Otherwise act on the nearest local `openspec/` root.

**Input**: Optional change name after `/opsx:doc-frontend` (e.g. `/opsx:doc-frontend add-user-login`). If omitted, infer from context; if ambiguous, run `openspec list` and ask the user to choose.

**Output**: Write to `openspec/changes/<name>/doc-frontend.html` (HTML format with embedded CSS).

**Steps**

1. Resolve the change name and its directory `openspec/changes/<name>/`.

2. Read planning artifacts from disk (re-read, do not rely on memory):
   - `proposal.md` — why/what, Capabilities
   - `specs/**/spec.md` — requirements + acceptance scenarios (behavior contract)
   - `design.md` (if present) — technical approach, decisions, risks
   - `tasks.md` (if present) — implementation steps, aids requirement breakdown

3. Generate the document as **HTML with embedded CSS** following this structure:

```markdown
# <项目/变更> 前端开发设计文档

> 需求来源：openspec/changes/<变更名>/（proposal.md / specs / design.md）

## 一、改造总览
| # | 需求 | 核心改造 | 涉及页面/组件 | 调用接口 | 埋点/外部依赖 |

## 二、需求一：<需求名>
### 2.1 需求回顾
### 2.2 方案说明
### 2.3 页面与组件设计
### 2.4 交互流程        (PlantUML 时序图)
### 2.5 接口对接        (调用哪些后端接口 + 用途 + 出入参要点)
### 2.6 状态与数据      (状态管理/本地缓存/服务端存储)
### 2.7 埋点与上报      (detailCode 建议值/上报时机)
### 2.8 待确认事项      (事项/责任方/状态)

## 三、需求二：<需求名>
## 五、整体节奏建议
```

**Rules**
- Split proposal's "What Changes / Capabilities" into one requirement section each.
- Pages/components: which pages/routes are touched, how components are split, what is reused vs new.
- Interactions: express with PlantUML sequence diagrams; state transitions with tables or prose.
- API integration: list which backend APIs are called (name + purpose + key request/response fields) and how the frontend consumes them and surfaces errors. Backend API design itself is out of scope (that is doc-backend's job).
- State/data: frontend state management, local storage, whether server-side persistence is needed.
- Telemetry: new tracking events (suggested detailCode) and when they fire; write 「无」 if none.
- Anything inferred or undecided (backend API contract, detailCode final value, component-library choice, etc.) must go into 「待确认事项」, never presented as a confirmed fact.
- Read-only: this skill only writes `doc-frontend.html`; it does not modify proposal/specs/design/tasks or any business code.
- After writing, summarize the location and each requirement section, and prompt the user to review.
