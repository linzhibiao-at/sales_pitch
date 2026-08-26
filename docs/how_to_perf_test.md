# 如何压测

本项目已自带压测脚本 `eval/perf_load_test.py`,无需 locust/wrk。它对 `/chat` 或 `/recommend/outfits` 在不同并发档位下测量响应时间,输出 p50/p95/p99/QPS/成功率报告。

## 前提

服务在跑:`uvicorn :8888`,8 workers。压测前先确认活着:

```bash
curl -s http://127.0.0.1:8888/health || echo "服务未启动"
```

## 两种模式

| 模式 | 路径 | 链路 | 单请求耗时 | 默认并发 | 默认每档请求数 | 默认超时 |
|---|---|---|---|---|---|---|
| `chat`(默认) | `POST /chat` SSE | 召回+LLM排序+LLM理由,与 batch_eval 同口径 | ~16s | 1,2,4,8 | 15 | 180s |
| `outfits` | `POST /recommend/outfits` | 仅召回+规则排序 | ~2s | 1,4,8,16,32 | 50 | 60s |

- **chat**:请求体 `{selected_sku_id, enable_llm_rank_reason=True, enable_tryon=False}`。延迟计到 SSE `done` 事件为止。
- **outfits**:请求体 `{query: <sku_id>, limit: N}`,`recommend_outfits` 内部用 `find_sku_token(query)` 解析 sku_id 作为锚点。

压测输入 SKU 复用 `eval.batch_eval.sample_skus` 的分层采样(按 `up_down/category_l2/gender` 分组,每组取 `n_per_group` 个,有可用 tryon_image),与批量评测同源同口径。

## 常用命令

```bash
cd /home/jovyan/outfit_rec/fila_agent_html

# 1. 默认 chat 全链路压测 (并发 1/2/4/8, 每档 15 请求)
python -m eval.perf_load_test 2>&1 | tee perf_load_test_$(date +%m%d).log

# 2. 压轻量路径 /recommend/outfits
python -m eval.perf_load_test --mode outfits

# 3. 自定义并发+每档请求数
python -m eval.perf_load_test --mode chat --concurrency 1,2,4,8,16,32 --requests 30

# 4. 增大 SKU 池(分层采样每组多取几个,样本更具代表性)
python -m eval.perf_load_test --n-per-group 5

# 5. 看完整参数
python -m eval.perf_load_test --help
```

## 参数说明

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 服务主机 |
| `--port` | `8888` | 服务端口 |
| `--mode` | `chat` | `chat` 或 `outfits` |
| `--concurrency` | 按模式 | 并发档位,逗号分隔 |
| `--requests` | 按模式 | 每档总请求数 |
| `--n-per-group` | `2` | 分层采样每组 SKU 数 |
| `--seed` | `42` | 随机种子(可复现) |
| `--limit` | `6` | outfits 模式每请求返回搭配数 |
| `--warmup` | `3` | 预热请求数,不计入指标(0=不预热) |
| `--timeout` | 按模式 | 单请求超时秒数 |
| `--output-dir` | `eval/results/{YYYYMMDDHH}/` | JSON 报告输出目录 |
| `--docs-dir` | `docs` | Markdown 报告输出目录(空串则不写) |

## 输出

- JSON:`eval/results/{YYYYMMDDHH}/perf_report.json`
- Markdown:`docs/perf_report_{YYYYMMDDHHMM}.md`(多次运行不覆盖)
- stdout 直接打印 p50/p95/p99/QPS/成功率表格

## 读结果的要点

- **延迟** = 客户端发起到收到 SSE `done` 事件的端到端耗时(含网络+排队),即完整 pipeline 耗时。
- **服务端 p50/p95** = SSE `done` 事件里 `total_ms` 的百分位,剔除网络与排队的纯服务端处理耗时。
- **搭配/req、理由/req** 用于校验结果完整性:chat 模式下理由/req 应 > 0,证明 LLM reason 确实生成。
- 闭合模型:每档维持固定并发 worker,共享请求预算,预算耗尽即止。
- **QPS** = 成功请求数 / 该档总耗时。
- 服务端默认 8 个 uvicorn worker,**并发超过 worker 数时排队竞争加剧**,QPS 不会线性涨。

## 历史结果参考

- 0707 chat 全链路(`docs/perf_report_202607071433.md`):并发 1→8 时 QPS 0.11→0.87,p99 从 18s 降到 10s;并发 16/32 超过 worker 数后 QPS 反而掉到 0.94/0.83,p99 飙到 30s/35s,并发 32 出现 1 个 ReadError 失败。
