#!/usr/bin/env python3
"""一次性批量分类色系：规则匹配不上的颜色名调 LLM 分类，结果写入缓存。

用法::

  cd fila_agent_html  # 或 descente_agent_html
  export PYTHONPATH=.
  export ARK_API_KEY=...
  python3 scripts/classify_color_series.py [--dry-run] [--batch-size 50]

新增颜色名时可增量运行：仅分类缓存中没有的。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

VALID_SERIES = [
    "黑色系", "白色系", "灰色系", "红色系", "粉色系",
    "橙色系", "黄色系", "绿色系", "蓝色系", "紫色系", "棕色系",
    "米色系",
]

LLM_SYSTEM_PROMPT = f"""你是一个颜色分类专家。给定一批颜色名称，将每个颜色分类到以下色系。
如果颜色名含多种色相（如 "蓝丝带/雪白" 含蓝+白），请返回所有命中的色系数组。

可选色系：
{{', '.join(VALID_SERIES)}}

如果颜色名无法确定色系（如纯图案名、编码等），请分到最接近的色系。
对于明显是多色/印花的名称（迷彩、花色、拼色等），返回 ["多色系"]。
对于米/燕麦/卡其/奶咖/拿铁等中性浅色调，返回 ["米色系"]。

请以 JSON 对象格式返回，key 为颜色名，value 为色系数组。例如：
{{"Arctic Ice": ["蓝色系"], "Blue Ribbon/Snow White": ["蓝色系","白色系"], "Camo Green": ["多色系"]}}

只返回 JSON，不要其他内容。"""


def _load_all_color_names() -> list[str]:
    """从 skus.jsonl 提取所有唯一颜色名。"""
    proc = ROOT / "data" / "processed" / "skus.jsonl"
    if not proc.is_file():
        logger.error("缺少文件: %s", proc)
        return []
    names: set[str] = set()
    with proc.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            for field in ("attr_name", "color_name", "color_family"):
                val = str(row.get(field) or "").strip()
                if not val:
                    continue
                # 处理 "颜色:XXX;尺码:YYY" 格式
                if "颜色:" in val:
                    val = val.split("颜色:")[1].split(";")[0].strip()
                if val:
                    names.add(val)
    return sorted(names)


def _call_llm(color_names: list[str]) -> dict[str, list[str]]:
    """调用 LLM 对颜色名批量分类，返回 dict[str, list[str]]。"""
    from backend.llm_client import _chat_block

    user_msg = "请将以下颜色名分类到对应色系（多色名返回数组）：\n" + "\n".join(color_names)
    messages = [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    raw = _chat_block("intent_llm", messages, temperature=0.1)
    if not raw:
        return {}
    # 提取 JSON
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return {}
    try:
        result = json.loads(match.group())
        # 校验值：str → [str]，list → 过滤无效
        out: dict[str, list[str]] = {}
        for k, v in result.items():
            key = str(k).strip()
            if isinstance(v, list):
                vals = [str(x).strip() for x in v if str(x).strip() in VALID_SERIES]
            else:
                vs = str(v).strip()
                vals = [vs] if vs in VALID_SERIES else []
            if vals:
                out[key] = vals
            else:
                logger.warning("LLM 返回无效色系 %r → %r，跳过", k, v)
        return out
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM 返回 JSON 解析失败")
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="批量分类颜色名到色系")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不调 LLM")
    parser.add_argument("--batch-size", type=int, default=50, help="每批调 LLM 的颜色数")
    args = parser.parse_args()

    from backend.intent.color_series_mapper import (
        _load_cache,
        collect_unmatched,
        save_cache,
    )

    all_names = _load_all_color_names()
    logger.info("共 %d 个唯一颜色名", len(all_names))

    unmatched = collect_unmatched(all_names)
    logger.info("规则匹配不上: %d 个", len(unmatched))

    if args.dry_run:
        for name in unmatched:
            print(f"  未匹配: {name}")
        return

    if not unmatched:
        logger.info("所有颜色名均已匹配或在缓存中，无需调 LLM")
        return

    cache = _load_cache()
    total_classified = 0
    for i in range(0, len(unmatched), args.batch_size):
        batch = unmatched[i : i + args.batch_size]
        logger.info("LLM 分类批次 %d/%d (%d 个)", i // args.batch_size + 1,
                     (len(unmatched) + args.batch_size - 1) // args.batch_size,
                     len(batch))
        result = _call_llm(batch)
        if result:
            cache.update(result)
            total_classified += len(result)
            logger.info("  本批分类成功: %d 个", len(result))
        else:
            logger.warning("  本批分类失败")

    save_cache(cache)
    logger.info("完成。共新增分类 %d 个，缓存总计 %d 个", total_classified, len(cache))


if __name__ == "__main__":
    main()
