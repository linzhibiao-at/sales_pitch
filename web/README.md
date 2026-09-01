# FILA 营销话术助手 — 前端架构文档

> 版本：v1.0  
> 更新日期：2026-08-31  
> 技术栈：Vue 3 + Vite 5 + Vue Router 4

---

## 1. 项目概述

本前端是 FILA 营销话术生成服务的调试与演示界面，提供：

- **话术生成**：填写顾客信息 + 商品信息，调用后端 `/v1/sales-pitch/generate` 接口生成个性化导购话术
- **多轮对话**：同一会话内保留 `session_id`，支持连续多次生成/调整话术
- **审计查询**：调用 `/v1/audit/requests` 查看历史请求记录，支持多条件筛选
- **接口配置**：在页面上配置 API Key / App ID / 后端地址，保存至 localStorage

---

## 2. 目录结构

```
web/
├── index.html                  # HTML 入口
├── package.json                # 项目依赖
├── vite.config.js              # Vite 构建配置（含开发代理）
├── README.md                   # 本文档
└── src/
    ├── main.js                 # 应用入口，挂载 Vue App + Router
    ├── App.vue                 # 根组件（左侧导航框架）
    ├── assets/
    │   └── main.css            # 全局样式（按钮/卡片/表单/徽章/动画）
    ├── router/
    │   └── index.js            # Vue Router 路由表
    ├── api/
    │   └── index.js            # API 客户端（封装 fetch，读取 localStorage 配置）
    └── views/
        ├── GenerateView.vue    # 话术生成页（核心页面）
        ├── AuditView.vue       # 审计记录查询页
        └── SettingsView.vue    # 接口配置页
```

---

## 3. 架构说明

### 3.1 整体架构

```
浏览器 (http://localhost:5173)
    │
    ├── Vue Router
    │     ├── /generate  → GenerateView.vue
    │     ├── /audit     → AuditView.vue
    │     └── /settings  → SettingsView.vue
    │
    └── API 层 (src/api/index.js)
          │  读取 localStorage 中的 API Key / App ID / Base URL
          │
          ├── generatePitch()   → POST /v1/sales-pitch/generate
          ├── queryAudit()      → GET  /v1/audit/requests
          └── getAuditDetail()  → GET  /v1/audit/requests/{trace_id}
```

### 3.2 开发代理（跨域解决方案）

开发环境下，Vite Dev Server 将所有 `/v1/*` 请求透明转发到后端，避免浏览器跨域拦截：

```
浏览器 → Vite(:5173) /v1/... → 转发 → FastAPI(:8000) /v1/...
```

配置位于 `vite.config.js`：

```js
server: {
  proxy: {
    '/v1': {
      target: 'http://localhost:8000',
      changeOrigin: true,
    },
  },
},
```

> **生产部署**：Base URL 留空依赖代理仅适用于开发环境。生产部署时在「设置」页填写真实后端地址（如 `https://api.example.com`），或配置 Nginx 反向代理。

### 3.3 配置管理（localStorage）

所有敏感配置通过「设置」页写入 localStorage，无需修改代码：

| localStorage Key | 用途 | 默认值 |
|---|---|---|
| `sp_base_url` | 后端 Base URL | 空（使用 Vite 代理） |
| `sp_api_key` | X-API-Key 请求头 | 空 |
| `sp_app_id` | 话术请求的 app_id | `micro_guide` |
| `sp_session_id` | 当前多轮对话会话 ID | 自动从响应写入 |

### 3.4 多轮对话机制

```
首次生成
  → 后端返回 { session_id, pitch, ... }
  → 前端将 session_id 存入 localStorage
  → 后续请求携带同一 session_id
  → 后端通过 LangGraph thread_id 恢复对话历史
  → Agent 基于历史上下文生成连贯的话术调整

点击「新建会话」
  → 清除 localStorage 中的 session_id
  → 清空页面对话历史
  → 下次请求将开启新 thread
```

---

## 4. 页面功能说明

### 4.1 话术生成页（`/generate`）

| 区块 | 说明 |
|---|---|
| 顾客信息 | 称呼、性别、年龄、尺码、风格偏好、场景、预算、备注（全部选填） |
| 商品信息 | 支持 1-10 个商品，必填商品名称，可选 SKU/价格/类目/颜色/材质/卖点 |
| 话术要求 | 风格（warm/professional/concise）、渠道（微信/线下/电话）、字数上限 |
| 对话记录 | 每次请求以气泡方式展示请求摘要 + AI 话术，支持一键复制 |
| 会话控制 | 顶部显示当前 session_id，点击「新建会话」清除并重置 |

### 4.2 审计记录页（`/audit`）

| 功能 | 说明 |
|---|---|
| 筛选条件 | App ID / Session ID / Trace ID / 时间范围 |
| 列表展示 | 时间、App ID、话术风格、话术截取、Trace ID |
| 详情弹窗 | 点击行或「详情」按钮，弹出完整顾客信息/商品信息/生成话术 |
| 分页 | 每页 20 条，支持翻页 |

### 4.3 设置页（`/settings`）

| 配置项 | 说明 |
|---|---|
| Base URL | 后端地址，开发时留空 |
| API Key | X-API-Key 鉴权头，`auth.enabled=false` 时可不填 |
| App ID | 必须在后端 `allowed_app_ids` 白名单内 |

---

## 5. 快速启动

### 5.1 前置条件

| 依赖 | 版本要求 |
|---|---|
| Node.js | ≥ 18 |
| npm | ≥ 9 |
| 后端服务 | FastAPI 运行在 `:8000`（见后端文档） |

### 5.2 开发模式启动

**步骤一：启动后端**

```bash
# 在项目根目录
cd /path/to/sales_pitch
source .venv/bin/activate

# 设置必要环境变量
export DASHSCOPE_API_KEY=your_key
export ANTA_LLM_API_KEY=your_key

uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

**步骤二：启动前端**

```bash
# 新开一个终端，进入 web 目录
cd /path/to/sales_pitch/web

# 首次运行需安装依赖
npm install

# 启动开发服务器
npm run dev
```

**步骤三：访问页面**

打开浏览器访问：**http://localhost:5173**

如后端开启了鉴权（`auth.enabled: true`），先进入「⚙️ 设置」页配置 API Key。

### 5.3 生产构建

```bash
cd web
npm run build
# 产物在 web/dist/ 目录，部署到任意静态文件服务器
```

Nginx 参考配置（需同时反向代理后端）：

```nginx
server {
    listen 80;

    # 前端静态资源
    location / {
        root /app/web/dist;
        try_files $uri $uri/ /index.html;
    }

    # 后端 API 代理
    location /v1/ {
        proxy_pass http://127.0.0.1:8080/v1/;
        proxy_set_header Host $host;
    }
}
```

---

## 6. 开发脚本

| 命令 | 说明 |
|---|---|
| `npm run dev` | 启动开发服务器（http://localhost:5173，支持热更新） |
| `npm run build` | 生产构建，产物在 `dist/` |
| `npm run preview` | 本地预览生产构建结果 |

---

## 7. 技术依赖

| 包 | 版本 | 用途 |
|---|---|---|
| `vue` | ^3.4.0 | 前端框架 |
| `vue-router` | ^4.3.0 | 客户端路由 |
| `vite` | ^5.2.0 | 构建工具 + 开发服务器 |
| `@vitejs/plugin-vue` | ^5.0.0 | Vite Vue SFC 支持 |

无额外 UI 框架依赖，样式全部自定义（`src/assets/main.css`）。
