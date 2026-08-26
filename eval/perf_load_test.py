"""搭配推荐接口压测：在不同并发下测量响应时间，输出 p50/p95/p99 性能报告。

服务端为 FastAPI (backend.main:app)，默认 8 个 uvicorn worker。支持两种模式：

- --mode chat (默认): POST /chat SSE，请求体 {selected_sku_id, enable_llm_rank_reason=True,
  enable_tryon=False}。走与 eval.batch_eval 相同的完整 pipeline —— 召回 + LLM 排序 +
  LLM 生成理由。延迟计到 SSE `done` 事件为止。单请求 ~16s，故默认小并发(1/2/4/8)、
  少请求(15)、长超时(180s)。
- --mode outfits: POST /recommend/outfits，请求体 {query: <sku_id>, limit: N}。
  recommend_outfits 内部用 find_sku_token(query) 解析 sku_id 作为锚点；仅召回 + 规则排序，
  不生成 LLM reason。单请求 ~2s，默认并发 1/4/8/16/32、每档 50。

压测输入 SKU 复用 eval.batch_eval.sample_skus 的分层采样（按 up_down/category_l2/
gender 分组，每组 n_per_group 个，有可用 tryon_image），与批量评测同源同口径。

用法:
    cd fila_agent_html
    # 默认: chat 全链路, 并发 1/2/4/8, 每档 15 请求
    python -m eval.perf_load_test

    # 压轻量路径 /recommend/outfits
    python -m eval.perf_load_test --mode outfits

    # 自定义并发与每档请求数
    python -m eval.perf_load_test --mode chat --concurrency 1,2,4,8 --requests 20

    # 分层采样每组取更多 SKU（增大 sku 池）
    python -m eval.perf_load_test --n-per-group 5

JSON 报告写入 eval/results/{YYYYMMDDHH}/perf_report.json，Markdown 报告写入
docs/perf_report_{YYYYMMDDHHMM}.md，多次运行不覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

# 让 backend 包可被导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_root
from backend.local_data_store import LocalDataStore
from backend.retrieval.es_client import EsClient
from eval.batch_eval import sample_skus

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("perf_load_test")
logger.setLevel(logging.INFO)


def percentile(sorted_data: list[float], p: float) -> float:
    """线性插值百分位（与 numpy 默认一致）。sorted_data 需已升序排序。"""
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return sorted_data[0]
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def sample_sku_pool(
    n_per_group: int,
    seed: int,
) -> list[str]:
    """复用 batch_eval.sample_skus 的分层采样：按 (up_down, category_l2, gender)
    分组，每组取 n_per_group 个有可用 tryon_image 的 SKU，保证压测输入与评测同源、
    同策略、同口径（性别/品类/上鞋下均衡覆盖）。
    """
    es = EsClient()
    if not es.available:
        raise RuntimeError("ES 不可用，无法采样 sku 池")
    rows = es.scan_skus()
    if not rows:
        raise RuntimeError("ES SKU 扫描结果为空")
    store = LocalDataStore()
    # LocalDataStore.load 已是 no-op；这里直接灌入 ES 扫描结果供 sample_skus 分组。
    store.skus = {str(r.get("sku_id") or ""): r for r in rows if r.get("sku_id")}
    sampled_rows = sample_skus(store, n_per_group=n_per_group, seed=seed)
    pool: list[str] = []
    seen: set[str] = set()
    for row in sampled_rows:
        sid = str(row.get("sku_id") or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            pool.append(sid)
    if not pool:
        raise RuntimeError("分层采样结果为空，无法构造压测请求")
    logger.info(
        "分层采样 sku 池: n_per_group=%d, 共 %d 个 sku",
        n_per_group, len(pool),
    )
    return pool


def _build_payload(mode: str, sku_id: str, limit: int) -> dict[str, Any]:
    """按模式构造请求体。
    - chat: POST /chat SSE，传 selected_sku_id + enable_llm_rank_reason=True，
      走与 batch_eval 相同的完整 pipeline（召回 + LLM 排序 + LLM 生成理由）。
    - outfits: POST /recommend/outfits，传 query=sku_id，仅召回 + 规则排序（无 LLM reason）。
    """
    if mode == "chat":
        return {
            "selected_sku_id": sku_id,
            "enable_llm_rank_reason": True,
            "enable_tryon": False,
            "message": "",
        }
    return {"query": sku_id, "limit": limit}


def _endpoint_for(mode: str) -> str:
    return "/chat" if mode == "chat" else "/recommend/outfits"


async def send_one(
    client: httpx.AsyncClient,
    base_url: str,
    mode: str,
    sku_id: str,
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    """发单个请求，返回 {ok, latency_ms, outfits, with_reason, status,
    server_total_ms, error}。chat 模式测到 SSE done 事件为止（完整 pipeline）。
    """
    endpoint = base_url + _endpoint_for(mode)
    payload = _build_payload(mode, sku_id, limit)
    t0 = perf_counter()
    try:
        if mode == "chat":
            outfits = 0
            with_reason = 0
            server_total_ms = 0
            got_done = False
            status = 0
            async with client.stream(
                "POST", endpoint, json=payload, timeout=timeout,
            ) as resp:
                status = resp.status_code
                if status != 200:
                    async for _ in resp.aiter_lines():
                        pass
                    return {
                        "ok": False,
                        "latency_ms": (perf_counter() - t0) * 1000.0,
                        "outfits": 0, "with_reason": 0,
                        "status": status, "server_total_ms": 0,
                        "error": f"http_{status}",
                    }
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except (json.JSONDecodeError, ValueError):
                        continue
                    et = ev.get("type")
                    if et == "outfit_results":
                        ofs = ev.get("outfits") or []
                        outfits = len(ofs)
                        with_reason = sum(
                            1 for o in ofs if (o.get("reason") or "").strip()
                        )
                    elif et == "done":
                        server_total_ms = ev.get("total_ms") or 0
                        got_done = True
            latency = (perf_counter() - t0) * 1000.0
            return {
                "ok": got_done,
                "latency_ms": latency,
                "outfits": outfits,
                "with_reason": with_reason,
                "status": status,
                "server_total_ms": server_total_ms,
                "error": "" if got_done else "no_done_event",
            }
        # outfits 模式
        resp = await client.post(endpoint, json=payload, timeout=timeout)
        latency = (perf_counter() - t0) * 1000.0
        if resp.status_code != 200:
            return {
                "ok": False, "latency_ms": latency, "outfits": 0,
                "with_reason": 0, "status": resp.status_code,
                "server_total_ms": 0, "error": f"http_{resp.status_code}",
            }
        d = resp.json()
        ofs = d.get("outfits") or []
        return {
            "ok": True,
            "latency_ms": latency,
            "outfits": len(ofs),
            "with_reason": sum(1 for o in ofs if (o.get("reason") or "").strip()),
            "status": 200,
            "server_total_ms": 0,
            "error": "",
        }
    except httpx.TimeoutException:
        return {
            "ok": False, "latency_ms": (perf_counter() - t0) * 1000.0,
            "outfits": 0, "with_reason": 0, "status": 0,
            "server_total_ms": 0, "error": "timeout",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "latency_ms": (perf_counter() - t0) * 1000.0,
            "outfits": 0, "with_reason": 0, "status": 0,
            "server_total_ms": 0, "error": type(exc).__name__,
        }


async def _run_level(
    client: httpx.AsyncClient,
    base_url: str,
    mode: str,
    sku_pool: list[str],
    *,
    concurrency: int,
    total_requests: int,
    limit: int,
    timeout: float,
    seed: int,
) -> dict[str, Any]:
    """对单一并发档位发压，返回该档的指标汇总。

    采用闭合模型：维持恰好 concurrency 个并发 worker，共享请求预算
    total_requests，每个 worker 从 sku 池中循环取 sku_id 发请求，预算耗尽即止。
    """
    # 预生成请求序列（循环 sku 池），保证可复现
    rng = random.Random(seed + concurrency)
    seq = [sku_pool[i % len(sku_pool)] for i in range(total_requests)]
    rng.shuffle(seq)

    latencies_ms: list[float] = []
    server_total_ms_list: list[float] = []  # chat 模式下服务端自报耗时
    status_counts: Counter[int] = Counter()
    error_details: Counter[str] = Counter()
    outfits_total = 0
    with_reason_total = 0
    success = 0
    idx = 0
    idx_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal idx, success, outfits_total, with_reason_total
        while True:
            async with idx_lock:
                if idx >= total_requests:
                    return
                sku_id = seq[idx]
                idx += 1
            res = await send_one(
                client, base_url, mode, sku_id, limit, timeout,
            )
            status_counts[res["status"]] += 1
            if res["ok"]:
                success += 1
                latencies_ms.append(res["latency_ms"])
                outfits_total += res["outfits"]
                with_reason_total += res["with_reason"]
                if res["server_total_ms"]:
                    server_total_ms_list.append(res["server_total_ms"])
            else:
                error_details[res["error"] or "unknown"] += 1

    t_start = perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    total_duration = perf_counter() - t_start

    latencies_ms.sort()
    server_total_ms_list.sort()
    err_total = sum(error_details.values())
    total_done = success + err_total

    def pct(data: list[float], p: float) -> float:
        return round(percentile(data, p), 2)

    result: dict[str, Any] = {
        "concurrency": concurrency,
        "total_requests": total_done,
        "success": success,
        "errors": err_total,
        "success_rate": round(success / total_done, 4) if total_done else 0.0,
        "latency_ms": {
            "avg": round(sum(latencies_ms) / len(latencies_ms), 2) if latencies_ms else 0.0,
            "min": round(latencies_ms[0], 2) if latencies_ms else 0.0,
            "p50": pct(latencies_ms, 50),
            "p95": pct(latencies_ms, 95),
            "p99": pct(latencies_ms, 99),
            "max": round(latencies_ms[-1], 2) if latencies_ms else 0.0,
        },
        "throughput_qps": round(success / total_duration, 2) if total_duration else 0.0,
        "total_duration_s": round(total_duration, 3),
        "outfits_per_request": round(outfits_total / success, 2) if success else 0.0,
        "reasons_per_request": round(with_reason_total / success, 2) if success else 0.0,
        "status_codes": dict(status_counts),
        "error_breakdown": dict(error_details),
    }
    # chat 模式额外报告服务端自报 total_ms（剔除网络/排队外的纯处理耗时）
    if server_total_ms_list:
        result["server_total_ms"] = {
            "avg": round(sum(server_total_ms_list) / len(server_total_ms_list), 2),
            "p50": pct(server_total_ms_list, 50),
            "p95": pct(server_total_ms_list, 95),
            "p99": pct(server_total_ms_list, 99),
        }
    return result


async def warmup(
    client: httpx.AsyncClient,
    base_url: str,
    mode: str,
    sku_pool: list[str],
    limit: int,
    n: int,
) -> None:
    """发 n 个请求预热服务（触发模型/ES 连接池/缓存加载），不计入指标。"""
    if n <= 0:
        return
    logger.info("预热请求: %d 个 (不计入指标)", n)

    async def one(sku_id: str) -> None:
        res = await send_one(client, base_url, mode, sku_id, limit, 180.0)
        if not res["ok"]:
            logger.warning("预热请求失败 (%s): %s", sku_id, res["error"])

    await asyncio.gather(*(one(sku_pool[i % len(sku_pool)]) for i in range(n)))


async def run_load_test(args: argparse.Namespace) -> dict[str, Any]:
    base_url = f"http://{args.host}:{args.port}"
    mode = args.mode
    endpoint_path = _endpoint_for(mode)
    endpoint_url = base_url + endpoint_path
    sku_pool = sample_sku_pool(args.n_per_group, args.seed)
    if not sku_pool:
        raise RuntimeError("sku 池为空")

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    logger.info(
        "压测目标: %s (mode=%s) | 并发档位: %s | 每档请求: %d | sku 池: %d",
        endpoint_url, mode, levels, args.requests, len(sku_pool),
    )

    # 单 AsyncClient + 连接池上限拉到与最高并发档匹配，避免连接池成为瓶颈
    max_concurrency = max(levels) if levels else 1
    limits = httpx.Limits(
        max_connections=max(max_concurrency * 2, 64),
        max_keepalive_connections=max(max_concurrency, 32),
    )
    timeout_cfg = httpx.Timeout(timeout=args.timeout)
    async with httpx.AsyncClient(
        limits=limits, timeout=timeout_cfg,
        headers={"Accept": "application/json" if mode != "chat" else "text/event-stream"},
    ) as client:
        await warmup(client, base_url, mode, sku_pool, args.limit, args.warmup)
        results: list[dict[str, Any]] = []
        for c in levels:
            logger.info(">>> 并发 %d, 发送 %d 个请求 ...", c, args.requests)
            t0 = perf_counter()
            stat = await _run_level(
                client,
                base_url,
                mode,
                sku_pool,
                concurrency=c,
                total_requests=args.requests,
                limit=args.limit,
                timeout=args.timeout,
                seed=args.seed,
            )
            extra = ""
            if "server_total_ms" in stat:
                extra = (
                    f", server_total p50={stat['server_total_ms']['p50']:.0f}ms"
                    f" p95={stat['server_total_ms']['p95']:.0f}ms"
                )
            logger.info(
                "<<< 并发 %d 完成: 成功 %d/%d, 搭配/req=%.1f, 理由/req=%.1f, "
                "p50=%.0fms, p95=%.0fms, p99=%.0fms, qps=%.2f (%.1fs)%s",
                c, stat["success"], stat["total_requests"],
                stat["outfits_per_request"], stat["reasons_per_request"],
                stat["latency_ms"]["p50"], stat["latency_ms"]["p95"],
                stat["latency_ms"]["p99"], stat["throughput_qps"],
                perf_counter() - t0, extra,
            )
            results.append(stat)

    payload_tpl = (
        {"selected_sku_id": "<sku_id>", "enable_llm_rank_reason": True,
         "enable_tryon": False, "message": ""}
        if mode == "chat"
        else {"query": "<sku_id>", "limit": args.limit}
    )
    return {
        "test_time": datetime.now(timezone.utc).isoformat(),
        "target": {
            "base_url": base_url,
            "endpoint": endpoint_path,
            "mode": mode,
            "method": "POST",
            "payload_template": payload_tpl,
            "pipeline": "召回 + LLM 排序 + LLM 生成理由" if mode == "chat"
            else "召回 + 规则排序（无 LLM reason）",
        },
        "config": {
            "concurrency_levels": levels,
            "requests_per_level": args.requests,
            "warmup_requests": args.warmup,
            "timeout_s": args.timeout,
            "n_per_group": args.n_per_group,
            "pool_size": len(sku_pool),
            "seed": args.seed,
        },
        "levels": results,
    }


def _format_report_markdown(report: dict[str, Any]) -> str:
    """把 JSON 报告渲染成 Markdown 表格，便于人读。"""
    tgt = report["target"]
    cfg = report["config"]
    lines: list[str] = []
    lines.append("# 搭配推荐接口压测报告")
    lines.append("")
    lines.append(f"- 测试时间: {report['test_time']}")
    lines.append(f"- 目标: `{tgt['method']} {tgt['base_url']}{tgt['endpoint']}` (mode={tgt.get('mode')})")
    lines.append(f"- pipeline: {tgt.get('pipeline', '')}")
    lines.append(
        f"- 请求体: `{json.dumps(tgt['payload_template'], ensure_ascii=False)}`"
    )
    lines.append(
        f"- 并发档位: {cfg['concurrency_levels']} | 每档请求数: {cfg['requests_per_level']} | "
        f"预热请求数: {cfg['warmup_requests']} | 超时: {cfg['timeout_s']}s | "
        f"分层采样 n_per_group={cfg['n_per_group']} → sku 池 {cfg['pool_size']}"
    )
    lines.append("")
    lines.append("## 响应时间与吞吐")
    lines.append("")

    def s(ms: float) -> str:
        """毫秒 → 秒，保留 1 位小数。"""
        return f"{(ms or 0) / 1000.0:.1f}"

    is_chat = tgt.get("mode") == "chat"
    if is_chat:
        lines.append(
            "| 并发 | 请求数 | 成功 | 失败 | 成功率 | 搭配/req | 理由/req | "
            "avg(s) | min | p50 | p95 | p99 | max | 服务端p50 | 服务端p95 | QPS | 耗时(s) |"
        )
        lines.append(
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
    else:
        lines.append(
            "| 并发 | 请求数 | 成功 | 失败 | 成功率 | 搭配/req | "
            "avg(s) | min | p50 | p95 | p99 | max | QPS | 耗时(s) |"
        )
        lines.append(
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
    for lv in report["levels"]:
        lat = lv["latency_ms"]
        if is_chat:
            st = lv.get("server_total_ms") or {}
            lines.append(
                f"| {lv['concurrency']} | {lv['total_requests']} | {lv['success']} | "
                f"{lv['errors']} | {lv['success_rate']*100:.1f}% | "
                f"{lv['outfits_per_request']} | {lv['reasons_per_request']} | "
                f"{s(lat['avg'])} | {s(lat['min'])} | {s(lat['p50'])} | {s(lat['p95'])} | {s(lat['p99'])} | {s(lat['max'])} | "
                f"{s(st.get('p50', 0))} | {s(st.get('p95', 0))} | "
                f"{lv['throughput_qps']} | {lv['total_duration_s']:.1f} |"
            )
        else:
            lines.append(
                f"| {lv['concurrency']} | {lv['total_requests']} | {lv['success']} | "
                f"{lv['errors']} | {lv['success_rate']*100:.1f}% | {lv['outfits_per_request']} | "
                f"{s(lat['avg'])} | {s(lat['min'])} | {s(lat['p50'])} | {s(lat['p95'])} | {s(lat['p99'])} | {s(lat['max'])} | "
                f"{lv['throughput_qps']} | {lv['total_duration_s']:.1f} |"
            )
    lines.append("")
    # 错误明细（仅当存在失败时）
    any_err = any(lv["error_breakdown"] or
                  {k: v for k, v in lv["status_codes"].items() if k != 200}
                  for lv in report["levels"])
    if any_err:
        lines.append("## 错误明细")
        lines.append("")
        lines.append("| 并发 | 状态码分布 | 错误归类 |")
        lines.append("|---:|---|---|")
        for lv in report["levels"]:
            sc = ", ".join(f"{k}:{v}" for k, v in sorted(lv["status_codes"].items())) or "-"
            eb = ", ".join(f"{k}:{v}" for k, v in sorted(lv["error_breakdown"].items())) or "-"
            lines.append(f"| {lv['concurrency']} | {sc} | {eb} |")
        lines.append("")
    lines.append("## 说明")
    lines.append("")
    if is_chat:
        lines.append("- 延迟 = 客户端发起到收到 SSE `done` 事件的端到端耗时（含网络+排队），即完整 pipeline（召回+LLM 排序+LLM 理由）耗时。")
        lines.append("- 服务端 p50/p95 = SSE `done` 事件里 `total_ms` 的百分位，剔除网络与排队前的纯服务端处理耗时。")
        lines.append("- 搭配/req、理由/req 用于校验结果完整性：chat 模式下理由/req 应 > 0，证明 LLM reason 确实生成。")
    else:
        lines.append("- 延迟 = 客户端发起到收到完整 JSON 响应的端到端耗时（含网络）。")
        lines.append("- outfits 模式仅召回+规则排序，不生成 LLM reason，故理由/req=0。")
    lines.append("- 闭合模型：每档维持固定并发 worker，共享请求预算，预算耗尽即止。")
    lines.append("- QPS = 成功请求数 / 该档总耗时。")
    lines.append("- 服务端默认 8 个 uvicorn worker，并发超过 worker 数时排队竞争加剧。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="搭配推荐接口压测 (/chat 全链路 or /recommend/outfits)")
    parser.add_argument("--host", default="127.0.0.1", help="服务主机 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8888, help="服务端口 (默认 8888)")
    parser.add_argument(
        "--mode", choices=("chat", "outfits"), default="chat",
        help="chat=POST /chat SSE 全链路(召回+LLM排序+LLM理由, 与 batch_eval 同); "
        "outfits=POST /recommend/outfits 轻量路径(仅召回+规则排序) (默认 chat)",
    )
    parser.add_argument(
        "--concurrency", default=None,
        help="并发档位，逗号分隔 (chat 默认 1,2,4,8; outfits 默认 1,4,8,16,32)",
    )
    parser.add_argument(
        "--requests", type=int, default=None,
        help="每档总请求数 (chat 默认 15; outfits 默认 50)",
    )
    parser.add_argument(
        "--n-per-group", type=int, default=2,
        help="分层采样每组 (up_down,category_l2,gender) 的 SKU 数，与 batch_eval 同口径 (默认 2)",
    )
    parser.add_argument("--seed", type=int, default=42, help="随机种子 (默认 42)")
    parser.add_argument(
        "--limit", type=int, default=6,
        help="recommend_outfits 的 limit 参数，即每请求返回搭配数 (默认 6)",
    )
    parser.add_argument(
        "--warmup", type=int, default=3,
        help="预热请求数，不计入指标 (默认 3，0=不预热)",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="单请求超时秒数 (chat 默认 180; outfits 默认 60)",
    )
    parser.add_argument(
        "--output-dir", default="",
        help="JSON 报告输出目录 (默认 eval/results/{YYYYMMDDHH}/)",
    )
    parser.add_argument(
        "--docs-dir", default="docs",
        help="Markdown 报告输出目录 (默认 docs/，设为空串则不写 docs)",
    )
    args = parser.parse_args()

    # 按模式填默认值（chat 全链路单请求 ~16s，需更小并发/更长超时）
    is_chat = args.mode == "chat"
    if args.concurrency is None:
        args.concurrency = "1,2,4,8" if is_chat else "1,4,8,16,32"
    if args.requests is None:
        args.requests = 15 if is_chat else 50
    if args.timeout is None:
        args.timeout = 180.0 if is_chat else 60.0

    report = asyncio.run(run_load_test(args))

    root = get_root()
    ts = datetime.now().strftime("%Y%m%d%H%M")
    hour = ts[:10]  # YYYYMMDDHH，按小时分桶
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = root / "eval" / "results" / hour
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "perf_report.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    md_text = _format_report_markdown(report)

    # Markdown 报告写到 docs/，文件名带时间戳，多次运行不覆盖
    md_paths: list[Path] = []
    if args.docs_dir:
        docs_dir = Path(args.docs_dir)
        if not docs_dir.is_absolute():
            docs_dir = root / docs_dir
        docs_dir.mkdir(parents=True, exist_ok=True)
        docs_md = docs_dir / f"perf_report_{ts}.md"
        docs_md.write_text(md_text, encoding="utf-8")
        md_paths.append(docs_md)
    # eval/results 也留一份同内容 md
    results_md = out_dir / "perf_report.md"
    results_md.write_text(md_text, encoding="utf-8")
    md_paths.append(results_md)

    # 同时把 Markdown 表格打到 stdout，方便直接查看
    print(md_text)
    logger.info("JSON 报告: %s", json_path)
    logger.info("Markdown 报告: %s", " , ".join(str(p) for p in md_paths))


if __name__ == "__main__":
    main()
