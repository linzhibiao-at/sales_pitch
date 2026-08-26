# FILA 穿搭推荐系统架构

## 1. 系统概述

FILA 穿搭推荐系统是一个**品牌隔离**的图文穿搭推荐服务，采用 FastAPI 架构，提供推荐 API、SSE 对话、搭配预览与商品详情功能。系统基于已有固定搭配和向量匹配和文本匹配拼套进行推荐。

---

## 2. 架构图

### 2.1 推荐流程

![截屏2026-06-15 17.14.05.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/YdgOk2bjWL9pXq4B/img/a1e45135-7f65-449a-86f0-f3ff5611b193.png)

### 2.2 架构流程图

```mermaid
flowchart TD
    A[👤 用户输入<br/>文字 + 图片] --> B

    subgraph Stage1["① 意图解析"]
        B[parse_user_intent] --> B1["Trie 词典匹配<br/>性别/季节/风格/场合"]
        B --> B2["图片向量相似度<br/>Milvus 图搜 Top-K"]
        B --> B3["LLM<br/>意图提取"]
        B1 --> C[UserIntent]
        B2 --> C
        B3 --> C
    end

    C --> D

    subgraph Stage2["② 搭配召回（三路并行）"]
        D[multi_path_recall] --> E1["通路1: 锚点图召回<br/>Milvus 向量 → 固定搭配库"]
        D --> E2["通路2: 文本向量拼套<br/>关键词向量召回"]
        D --> E3["通路3: Query2ES 拼套<br/>LLM/规则生成 ES Query → 检索组合"]
    end

    E1 --> F[merge_and_dedupe<br/>RRF 融合 + 去重]
    E2 --> F
    E3 --> F

    subgraph Stage3["③ 粗排"]
        F --> G[coarse_rank_outfits<br/>规则打分截断<br/>性别/季节/标签/预算]
    end

    subgraph Stage4["④ LLM 排序 + 推荐理由"]
        G --> H[rank_deduped_outfits]
        H --> H1["LLM 美学打分排序<br/>batch / parallel 模式"]
        H --> H2["LLM 生成推荐理由<br/>搭配级 + 单品级"]
    end

    subgraph Stage5["⑤ 虚拟试穿"]
        H1 --> I[batch_tryon_outfits<br/>生成试穿效果图]
        H2 --> I
    end

    I --> J[📦 返回搭配卡片<br/>outfit_cards + 理由 + 试穿图]

    style Stage1 fill:#e1f5fe,stroke:#0288d1
    style Stage2 fill:#fff3e0,stroke:#f57c00
    style Stage3 fill:#fce4ec,stroke:#c62828
    style Stage4 fill:#e8f5e9,stroke:#2e7d32
    style Stage5 fill:#f3e5f5,stroke:#7b1fa2
```

## 3. 部署架构图

```mermaid
graph TB
    subgraph 客户端
        Browser[浏览器]
    end

    subgraph FastAPI 单进程服务
        UVicorn[uvicorn 应用服务]
        API[REST / SSE API<br/>对话推荐 / 单品互补 / 搭配召回 / 数据查询]
    end

    subgraph 外部服务
        ES_Cluster[Elasticsearch 集群]
        Milvus_Cloud[阿里云托管 Milvus]
        LLM_Gateway[LLM Gateway]
    end

    Browser -->|HTTP| UVicorn
    UVicorn --> API

    API -->|ES REST| ES_Cluster
    API -->|gRPC| Milvus_Cloud
    API -->|OpenAI Compatible| LLM_Gateway
```
---

## 4. 推荐管线流程

```mermaid
sequenceDiagram
    participant UI as 前端
    participant API as FastAPI
    participant SVC as 推荐服务
    participant INT as 意图解析
    participant RET as 检索层
    participant RNK as 排序层
    participant LLM as Qwen3.6
    participant EMB as Qwen3-VL-Embedding
    participant TRY as 虚拟试穿

    UI->>API: 对话请求 (SSE)
    API->>SVC: 启动推荐流

    SVC->>INT: 解析意图（Trie 词典 + Qwen3.6）
    INT->>LLM: 意图提取
    LLM-->>INT: 意图槽位
    INT-->>SVC: 意图槽位（性别/季节/角色等）
    SVC-->>UI: 意图结果

    SVC->>RET: 锚点 SKU 检索（图向量/文本）
    RET->>EMB: 图片/文本向量化
    EMB-->>RET: embedding 向量
    RET-->>SVC: 锚点候选
    SVC-->>UI: 锚点 SKU

    SVC->>RET: 多路召回
    Note over RET: 1. 图向量召回（Milvus → 固定搭配）<br/>2. ES 属性召回（属性 + 文本拼套）<br/>3. 文本向量召回（Milvus 语义拼套）
    RET->>EMB: 召回向量化
    EMB-->>RET: embedding 向量
    RET-->>SVC: 召回候选
    SVC-->>UI: 召回完成

    SVC->>RNK: 排序打分（规则 / LLM）
    RNK->>LLM: （可选）Qwen3.6 打分 + 推荐理由
    LLM-->>RNK: 分数 + 理由
    RNK-->>SVC: 排序结果
    SVC-->>UI: 搭配卡片 + 总结文案

    SVC->>TRY: （可选）虚拟试穿
    Note over TRY: 拼接 top/pants/shoes tryon_image<br/>生成穿搭效果图
    TRY-->>SVC: 试穿效果图
    SVC-->>UI: 试穿进度 + 效果图

    SVC-->>UI: 流结束
```

## 5. 数据索引与存储位置

### 5.1 Elasticsearch 索引

| 索引 | 用途 | 存储内容 |
| --- | --- | --- |
| **fila-skus** | SKU 单品索引 | 商品属性、搜索文本、图片 URL、价格、中类、色系等 |
| **fila-outfits** | 搭配索引 | 固定搭配组合（搭配 ID、商品列表、来源标记） |

*   部署在内网 ES 集群（ES 7.9.3），通过环境变量配置连接与认证
    
*   支持全量重建和增量更新
    

### 5.2 Milvus 向量索引

| Collection | 用途 | 向量维度 | 距离度量 |
| --- | --- | --- | --- |
| **fila**_sku_vectors | SKU 图文向量 | 1024 | COSINE |
| **fila**_sku_text\_vectors | SKU 文本向量 | 1024 | COSINE |

*   **云端模式**（生产环境）：阿里云托管 Milvus，HNSW 索引
    
*   **本地模式**（开发调试）：Milvus Lite 本地文件，IVF\_FLAT 索引
    

### 5.3 本地文件存储

| 目录 | 说明 |
| --- | --- |
| `data/tables/` | Hive 日更商品原始表 CSV |
| `data/processed/` | 离线 ETL 产出的单品目录与款号映射 |
| `data/preview/` | 微导购固定搭配预览 JSON |
| `data/logs/` | 索引同步状态、在线推荐日志、会话回放 |

### 5.4 数据流向

```mermaid
graph LR
    subgraph 数据源
        Hive[Hive 商品表<br/>日更 CSV]
        CC[搭配素材表]
        Guide[微导购搭配表]
    end

    subgraph ETL 处理
        DL[日更下载]
        BC[商品目录构建]
        BO[搭配 JSON 构建]
        SI[图片选型]
        VE[数据校验]
    end

    subgraph 索引构建
        BE[ES 索引写入]
        BM[向量索引写入]
    end

    subgraph 在线存储
        ES_SKU[ES 单品索引]
        ES_OF[ES 搭配索引]
        MV_IMG[Milvus 图文向量]
        MV_TXT[Milvus 文本向量]
    end

    Hive --> DL
    DL --> BC
    Guide --> BO
    CC --> BO
    BC --> SI
    BO --> SI
    SI --> VE
    VE --> BE
    VE --> BM

    BE --> ES_SKU
    BE --> ES_OF
    BM --> MV_IMG
    BM --> MV_TXT
```

---

## 5. 核心模块说明

### 5.1 后端模块

| 模块 | 职责 |
| --- | --- |
| **应用入口** | FastAPI 路由、静态资源挂载、SSE 流 |
| **配置管理** | 配置加载（yaml + 环境变量覆盖） |
| **推荐服务** | 推荐全链路编排（意图 → 召回 → 排序 → 理由） |
| **意图解析** | Trie 词典意图解析 + LLM fallback |
| **ES 检索** | Elasticsearch 查询封装 |
| **Milvus 检索** | 向量检索（cloud / local 双模式） |
| **搭配召回** | 固定搭配召回 + 向量拼套 |
| **单品检索** | 单品查询与互补召回 |
| **排序打分** | 规则打分 / LLM 打分排序 |
| **卡片构建** | 搭配结果卡片组装 |
| **虚拟试穿** | 试穿图片生成服务 |
| **LLM 客户端** | OpenAI Compatible LLM 调用封装 |
| **Embedding 客户端** | 图文向量生成 |

---

## 6. ETL 与日更流程

数据日更流程依次执行以下步骤：

1.  **日更下载** — 从 Hive 拉取最新商品表 CSV
    
2.  **商品目录构建** — 清洗生成单品目录与款号映射
    
3.  **搭配 JSON 构建** — 从微导购搭配表构建统一搭配数据
    
4.  **图片选型** — 为每个 SKU 选定展示图、索引图、试穿图
    
5.  **数据校验** — 检查数据完整性
    
6.  **ES 索引写入** — 写入 ES 单品索引和搭配索引（支持增量）
    
7.  **向量索引写入** — 写入 Milvus 图文向量和文本向量（支持增量）
    

增量索引同步状态由专用脚本管理，确保日更时仅写入变更数据。