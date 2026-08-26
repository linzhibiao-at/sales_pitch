#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FILA：用 config.yaml ``models.vision_llm`` 配置的 VLM 从候选商品图中完成图片预处理：

1. **tryon_image**（white_front_url）：选出一张纯色背景（白底或浅色纯色底）、
   无较大说明文字的商品静物主图；优先无真人模特，若无任何无模特的纯色背景商品图，
   可接受模特少部分部位出现（如仅手脚、肩膀等）的纯色背景商品图；
   在符合以上条件的前提下，按 正面 > 侧面 > 背面 的优先级选择。
2. **index_images**：选出所有商品图（含模特图、各角度），仅排除非商品图
   （吊牌、尺码表等），并尽可能优先选择纯色背景商品图，用于构建 Milvus 多图向量索引。

数据来源：``build_catalog.py`` 产出的 ``skus.jsonl``（按 ``sku_id`` 过滤）、
``product_master.csv``、``product_image.csv``。

模型连接参数默认读取 ``fila_agent_html/config.yaml`` 的 ``models.vision_llm``（与
``backend/llm_client.py`` 中 ``understand_image_json`` 一致）；可通过命令行覆盖。

注意：服务端须支持视觉输入（OpenAI 兼容 multimodal chat/completions）。
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image
from tqdm import tqdm

DEFAULT_MAX_PIXELS = 1048576
DEFAULT_MIN_PIXELS = 5600
DEFAULT_IMAGE_QUALITY = 85

MAX_RETRIES = 3
RETRY_DELAY_SEC = 2.0

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import env_or_empty, load_config
from backend.empty_image_urls import is_empty_product_image_url
from scripts._project_paths import load_paths as _load_paths

_PATHS = _load_paths()
DEFAULT_PRODUCT_DIR = _PATHS["product_dir"]
DEFAULT_SKUS_JSONL = _PATHS["processed_dir"] / "skus.jsonl"


def resolve_vision_llm_settings(
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """从 config.yaml ``models.vision_llm`` 解析 VLM 连接参数。"""
    cfg = load_config(config_path or DEFAULT_CONFIG_PATH)
    mcfg = (cfg.get("models") or {}).get("vision_llm") or {}
    base = (mcfg.get("base_url") or "").strip().rstrip("/")
    key_env = str(mcfg.get("api_key_env") or "ANTA_LLM_API_KEY")
    api_key = env_or_empty(key_env) or os.environ.get("OPENAI_API_KEY", "")
    enable_thinking = mcfg.get("enable_thinking")
    if enable_thinking is not None:
        enable_thinking = bool(enable_thinking)
    return {
        "api_base": base,
        "api_key": api_key,
        "api_key_env": key_env,
        "model": str(mcfg.get("model") or "qwen3.5-flash"),
        "max_tokens": int(mcfg.get("max_tokens") or 1024),
        "timeout_sec": float(mcfg.get("timeout_sec") or 180),
        "enable_thinking": enable_thinking,
    }

SYSTEM_PROMPT_TRYON = (
    "你是电商商品图识别助手，只根据用户给出的多张商品图做判断。"
    "选图必须严格遵循：必须是纯色背景（白底或浅色纯色底）的商品静物图，"
    "不能有较大的说明文字；优先无真人模特，"
    "若候选中无任何无模特的纯色背景商品图，可接受模特少部分部位出现（如仅手脚、肩膀等）的纯色背景商品图。"
    "在符合以上条件的前提下按 正面 > 侧面 > 背面 的优先级选择。"
    "并严格按要求的 JSON 格式输出。"
)

USER_PROMPT_TRYON_TEMPLATE = """下面按顺序给出 {n} 张图片，编号从 1 到 {n}（第 1 张、第 2 张……）。

业务目标：为「货号 {attr_alias}」「款号 {style_no}」「颜色属性 id_pa={id_pa}」选出**一张**最合适的商品主图（只能选一张）。

**硬性排除**（满足任一条则直接排除，不参与选择）：
1. **非纯色背景**：背景不是白色或浅色纯色底（如外景、街道、赛场、生活场景、复杂图案背景、拼图宫格等），一律排除。
2. **有较大说明文字**：图中有较大的说明性文字、卖点标语、促销信息等（吊牌、水洗标上的小字不算），一律排除。
3. **非商品图**：图片内容是吊牌、水洗标、尺码表、成分说明、包装盒、赠品、配件细节特写等，而非商品本身的展示图。
4. **非单品**：明显是多件组合搭配图、拼图宫格主视觉，而非单件商品主体。
5. **商品展示不完整**：只展示了商品的局部（如只拍了领口、袖口、鞋底等细节特写），缺少商品整体轮廓。

**模特出镜规则**（按优先级降级，仅在上一档无候选时才进入下一档）：
- **档位 A（优先）**：无真人模特的纯色背景商品静物图。
- **档位 B（降级）**：仅有模特**少部分部位**出现（如仅露手脚、肩膀等局部，模特不作为主体）的纯色背景商品图；仅当候选池中没有任何一张档位 A 的图时，才可选档位 B。
- **排除**：模特作为主体展示商品（全身、半身上身图等，模特占据明显画面的），一律排除，不进入任何档位。

**选择流程**（必须严格按顺序执行，不可跳级）：

**步骤 1 — 限定纯色背景、无较大文字、商品完整展示的图**
- 在通过上述硬性排除后，剩下的候选均为「纯色背景 + 无较大文字 + 商品完整展示」的图；
- 若没有任何一张满足以上条件，则 chosen_index 填 0。

**步骤 2 — 在步骤 1 确定的候选池内按模特出镜规则筛选**
- 优先选档位 A（无模特）；仅当档位 A 为空时，才在档位 B 中选。

**步骤 3 — 在步骤 2 确定的候选池内按角度优先级选择**
- **优先正面**：商品以正面或微侧正面朝向镜头为主，且展示了商品的完整形态（整件衣服、整双鞋等）；
- **无正面则选侧面**：仅当候选池中没有任何正面图时，才选侧面或微侧图；
- **无侧面则选背面**：仅当候选池中既无正面图也无侧面图时，才选背面图；
- 明显俯视图仍可选，但优先级低于正面/侧面。

请只做选择，不要编造不存在的编号。

严格只输出一个 JSON 对象（不要用 markdown 代码围栏），格式如下：
{{"chosen_index": <整数，1~{n} 表示选中第几张；若没有任何一张符合则填 0>, "confidence": <0到1之间的小数>, "reason": "<简短中文理由>"}}
"""

SYSTEM_PROMPT_INDEX = (
    "你是电商商品图识别助手，只根据用户给出的多张商品图做判断。"
    "你需要选出所有商品展示图，并尽可能选择纯色背景（白底或浅色纯色底）的商品图；"
    "仅排除非商品图（吊牌、尺码表、成分说明、包装盒、赠品、配件细节特写等）。"
    "并严格按要求的 JSON 格式输出。"
)

USER_PROMPT_INDEX_TEMPLATE = """下面按顺序给出 {n} 张图片，编号从 1 到 {n}（第 1 张、第 2 张……）。

业务目标：为「货号 {attr_alias}」「款号 {style_no}」「颜色属性 id_pa={id_pa}」选出所有**商品展示图**，并尽可能以纯色背景商品图为主。

**入选标准**：只要图片展示的是商品本身即可，包括：
- 无模特的平铺图 / 静物图（**优先**，尤其是纯色背景的）
- 商品正面、侧面、背面等各角度图（**优先**纯色背景的）
- 有真人模特的上身图 / 穿搭图（可入选，但优先级低于纯色背景静物图）
- 不同背景的商品展示图（白底、场景图等均可，但**尽量优先**选择纯色背景图）

**排除标准**（满足任一条则排除）：
1. **非商品图**：吊牌、水洗标、尺码表、成分说明、包装盒、赠品、配件细节特写等。
2. **非单品且非穿搭展示**：纯拼图宫格主视觉、纯文字图。
3. **商品展示不完整**：只展示了商品局部（领口、袖口、鞋底等细节特写），缺少商品整体轮廓。

**选择建议**：
- 若同一商品既有纯色背景静物图，又有场景/模特图，应**优先选入**纯色背景静物图；
- 场景图、模特图仅在缺少纯色背景图时作为补充入选，避免选入过多相似场景图。

请选出所有符合入选标准、不满足排除标准的图片编号。

请只做选择，不要编造不存在的编号。

严格只输出一个 JSON 对象（不要用 markdown 代码围栏），格式如下：
{{"index_indices": [<整数数组，选中的所有图片编号，1~{n}>], "confidence": <0到1之间的小数>, "reason": "<简短中文理由>"}}
"""


def parse_sku_id_arg(raw: Optional[str]) -> set[str]:
    """解析 --sku-id：单个或多个货号，英文逗号分隔。"""
    if not raw or not raw.strip():
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _norm_id(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip().strip('"').lstrip("'").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def read_csv_dicts(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def encode_image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def resize_image_bytes_if_needed(
    raw: bytes,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    min_pixels: int = DEFAULT_MIN_PIXELS,
    quality: int = DEFAULT_IMAGE_QUALITY,
) -> str:
    """返回 JPEG base64，逻辑对齐 process_quality_judgment_v4.resize_image_if_needed。"""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            ow, oh = img.size
            op = ow * oh
            target = op
            need = False
            if op > max_pixels:
                need = True
                target = max_pixels
            elif op < min_pixels:
                need = True
                target = min_pixels
            if not need:
                return base64.b64encode(raw).decode("utf-8")
            scale = (target / op) ** 0.5
            nw = int(ow * scale)
            nh = int(oh * scale)
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img,
                    mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None,
                )
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img_r = img.resize((nw, nh), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img_r.save(buf, format="JPEG", quality=quality, optimize=True)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")
    except Exception:
        return encode_image_to_base64(raw)


def fetch_image_bytes(url: str, timeout: float = 30.0) -> bytes:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; outfit-rec/fila_images_preprocess)"
        ),
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.content


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text or not text.strip():
        return None
    text = text.strip()
    if "</think>" in text:
        text = text.split("</think>")[-1].strip()
    if "```json" in text:
        a = text.find("```json") + 7
        b = text.find("```", a)
        if b > a:
            text = text[a:b].strip()
    elif text.startswith("```"):
        c0 = text.find("{")
        c1 = text.rfind("}")
        if c0 >= 0 and c1 > c0:
            text = text[c0 : c1 + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    end = text.rfind("}")
    if end < 0:
        return None
    depth = 0
    start = -1
    for i in range(end, -1, -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            depth -= 1
            if depth == 0:
                start = i
                break
    if start >= 0:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _parse_index_indices(raw: Any, n: int) -> List[int]:
    """从 VLM 输出的 index_indices 字段解析出合法编号列表（1-based）。"""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        try:
            idx = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= n and idx not in out:
            out.append(idx)
    return out


def _build_image_content(
    image_urls: List[str],
    max_pixels: int,
    min_pixels: int,
    image_quality: int,
) -> List[Dict[str, Any]]:
    """下载并编码图片，返回 OpenAI 兼容的 content 列表。"""
    content: List[Dict[str, Any]] = []
    for u in image_urls:
        try:
            raw = fetch_image_bytes(u)
            b64 = resize_image_bytes_if_needed(
                raw,
                max_pixels=max_pixels,
                min_pixels=min_pixels,
                quality=image_quality,
            )
            uri = f"data:image/jpeg;base64,{b64}"
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": uri},
                }
            )
        except Exception as exc:
            content.append(
                {
                    "type": "text",
                    "text": f"[图片加载失败 url={u} err={exc}]",
                }
            )
    return content


def _create_chat_client(
    api_base: str,
    api_key: str,
    timeout_sec: float,
):
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=api_base,
        timeout=timeout_sec,
    )


def _call_vlm(
    *,
    client,
    model: str,
    system_prompt: str,
    user_text: str,
    image_content: List[Dict[str, Any]],
    max_tokens: int,
    enable_thinking: Optional[bool],
) -> str:
    """通用 VLM 调用，返回 raw_text。"""
    content = list(image_content)
    content.append({"type": "text", "text": user_text})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    kwargs: Dict[str, Any] = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.05,
        top_p=0.05,
    )
    extra_body: Dict[str, Any] = {}
    if enable_thinking is not None:
        extra_body["enable_thinking"] = enable_thinking
        if not enable_thinking:
            extra_body["chat_template_kwargs"] = {
                "enable_thinking": False,
            }
    if extra_body:
        kwargs["extra_body"] = extra_body

    response = client.chat.completions.create(**kwargs)
    if response.choices:
        return response.choices[0].message.content or ""
    return ""


def call_vlm_pick_tryon_image(
    *,
    image_urls: List[str],
    attr_alias: str,
    style_no: str,
    id_pa: int,
    model: str,
    api_base: str,
    api_key: str,
    max_tokens: int,
    timeout_sec: float,
    enable_thinking: Optional[bool],
    max_pixels: int,
    min_pixels: int,
    image_quality: int,
) -> Tuple[int, str, str]:
    """返回 (chosen_index 0..n, reason, raw_text)。"""
    n = len(image_urls)
    if n == 0:
        return 0, "无候选图", ""

    client = _create_chat_client(api_base, api_key, timeout_sec)
    image_content = _build_image_content(
        image_urls, max_pixels, min_pixels, image_quality
    )

    user_text = USER_PROMPT_TRYON_TEMPLATE.format(
        n=n,
        attr_alias=attr_alias,
        style_no=style_no,
        id_pa=id_pa,
    )

    raw_text = _call_vlm(
        client=client,
        model=model,
        system_prompt=SYSTEM_PROMPT_TRYON,
        user_text=user_text,
        image_content=image_content,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
    )

    parsed = parse_json_object(raw_text)
    if not parsed:
        return 0, "模型输出无法解析为 JSON", raw_text

    idx = parsed.get("chosen_index")
    try:
        idx_int = int(idx)
    except (TypeError, ValueError):
        idx_int = 0
    if idx_int < 0 or idx_int > n:
        idx_int = 0
    reason = str(parsed.get("reason", "") or "").strip()
    return idx_int, reason, raw_text


def call_vlm_pick_index_images(
    *,
    image_urls: List[str],
    attr_alias: str,
    style_no: str,
    id_pa: int,
    model: str,
    api_base: str,
    api_key: str,
    max_tokens: int,
    timeout_sec: float,
    enable_thinking: Optional[bool],
    max_pixels: int,
    min_pixels: int,
    image_quality: int,
) -> Tuple[List[int], str, str]:
    """返回 (index_indices [1..n], reason, raw_text)。"""
    n = len(image_urls)
    if n == 0:
        return [], "无候选图", ""

    client = _create_chat_client(api_base, api_key, timeout_sec)
    image_content = _build_image_content(
        image_urls, max_pixels, min_pixels, image_quality
    )

    user_text = USER_PROMPT_INDEX_TEMPLATE.format(
        n=n,
        attr_alias=attr_alias,
        style_no=style_no,
        id_pa=id_pa,
    )

    raw_text = _call_vlm(
        client=client,
        model=model,
        system_prompt=SYSTEM_PROMPT_INDEX,
        user_text=user_text,
        image_content=image_content,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
    )

    parsed = parse_json_object(raw_text)
    if not parsed:
        return [], "模型输出无法解析为 JSON", raw_text

    index_indices = _parse_index_indices(parsed.get("index_indices"), n)
    reason = str(parsed.get("reason", "") or "").strip()
    return index_indices, reason, raw_text


def load_masters(path: Path) -> Dict[int, str]:
    """id_goods -> 款号 id_alias"""
    out: Dict[int, str] = {}
    for r in read_csv_dicts(path):
        gid = _norm_id(r.get("id_goods"))
        if gid is None:
            continue
        alias = (r.get("id_alias") or "").strip()
        if alias:
            out[gid] = alias
    return out


def load_catalog_skus(skus_jsonl: Path) -> List[Dict[str, Any]]:
    """从 build_catalog.py 产出的 skus.jsonl 读取待选图 SKU（sku_id 货号）。"""
    rows: List[Dict[str, Any]] = []
    with skus_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sku_id = str(rec.get("sku_id") or "").strip()
            gid = _norm_id(rec.get("id_goods") or rec.get("goods_id"))
            id_pa = _norm_id(rec.get("id_pa"))
            if not sku_id or gid is None:
                continue
            if id_pa is None:
                id_pa = 0
            rows.append(
                {
                    "attr_alias": sku_id,
                    "id_goods": gid,
                    "id_pa": id_pa,
                }
            )
    return rows


# ── 全量在售 SKU 加载 ─────────────────────────────────────────────────────



def _is_onsell(raw: Any) -> bool:
    """product_master.onsell：1 表示在售。"""
    if raw is None:
        return False
    s = str(raw).strip()
    if not s:
        return False
    try:
        return int(float(s)) == 1
    except ValueError:
        return s == "1"


def is_legacy_sku_id(sku_id: str) -> bool:
    """识别老款/电商款 sku_id（数字开头，如 162217109-3、112128861-1、0028728）。

    新款 FILA 货号均以字母开头（A11M411206FBU、A1EU621231FWT、F1EU629038FLG）。
    用于过滤掉上游遗留的老款颜色货号，统一 sku_id 体系。
    """
    if not sku_id:
        return False
    return sku_id[0].isdigit()


def load_onsale_skus(product_dir: Path) -> List[Dict[str, Any]]:
    """从 product_master + product_attr 加载全量在售 SKU。

    过滤条件：
    - product_master: onsell=1（全量在售，不限上架时间）
    - product_attr:   id_pac=1（颜色维度）、status=0（有效）

    返回格式与 load_catalog_skus 一致：[{attr_alias, id_goods, id_pa}]。
    """
    # 1. 加载在售 id_goods 集合
    master_path = product_dir / "product_master.csv"
    onsell_gids: set[int] = set()
    for r in read_csv_dicts(master_path):
        gid = _norm_id(r.get("id_goods"))
        if gid is None:
            continue
        if _is_onsell(r.get("onsell")):
            onsell_gids.add(gid)

    # 2. 从 product_attr 收集颜色维度 SKU
    attr_path = product_dir / "product_attr.csv"
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for r in read_csv_dicts(attr_path):
        gid = _norm_id(r.get("id_goods"))
        if gid is None or gid not in onsell_gids:
            continue
        # 仅颜色维度行
        if str(r.get("id_pac", "")).strip() != "1":
            continue
        # status=0 表示有效
        if str(r.get("status", "0")).strip() != "0":
            continue
        alias = (r.get("attr_alias") or "").strip()
        if not alias or alias in seen:
            continue
        if is_legacy_sku_id(alias):
            continue
        seen.add(alias)
        id_pa = _norm_id(r.get("id_pa"))
        if id_pa is None:
            id_pa = 0
        rows.append(
            {
                "attr_alias": alias,
                "id_goods": gid,
                "id_pa": id_pa,
            }
        )
    return rows


def load_images_by_goods(product_dir: Path) -> Dict[int, List[Dict[str, Any]]]:
    by_gid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    img_path = product_dir / "product_image.csv"
    for r in read_csv_dicts(img_path):
        if str(r.get("status", "")).strip() != "1":
            continue
        path = (r.get("path") or "").strip()
        if not path:
            continue
        gid = _norm_id(r.get("id_goods"))
        if gid is None:
            continue
        id_pa = _norm_id(r.get("id_pa"))
        if id_pa is None:
            id_pa = 0
        oid = _norm_id(r.get("order_id"))
        if oid is None:
            oid = 0
        by_gid[gid].append(
            {
                "path": path,
                "id_pa": id_pa,
                "order_id": oid,
                "image_type": (r.get("image_type") or "").strip(),
            }
        )
    return by_gid


def collect_candidates(
    images_by_goods: Dict[int, List[Dict[str, Any]]],
    id_goods: int,
    id_pa: int,
    max_candidates: int,
) -> List[Dict[str, Any]]:
    """返回候选图行列表，每行含 path/id_pa/order_id/image_type。
    调用前已过滤 empty 占位图。"""
    rows = [
        x
        for x in images_by_goods.get(id_goods, [])
        if x["id_pa"] == id_pa
    ]
    rows.sort(key=lambda x: (x["order_id"], x["path"]))
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        p = row["path"]
        if p in seen:
            continue
        # 过滤 empty 占位图
        if is_empty_product_image_url(p):
            continue
        seen.add(p)
        out.append(row)
        if len(out) >= max_candidates:
            break
    return out


def process_one_sku(
    sku: Dict[str, Any],
    masters: Dict[int, str],
    images_by_goods: Dict[int, List[Dict[str, Any]]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    gid = sku["id_goods"]
    id_pa = sku["id_pa"]
    attr_alias = sku["attr_alias"]
    style = masters.get(gid, "")

    cand = collect_candidates(
        images_by_goods,
        gid,
        id_pa,
        args.max_candidates,
    )
    cand_urls = [r["path"] for r in cand]

    row_out: Dict[str, Any] = {
        "货号": attr_alias,
        "款号": style,
        "id_goods": gid,
        "id_pa": id_pa,
        "candidate_count": len(cand),
        "white_front_url": "",
        "index_images": "",
        "tryon_index": "",
        "chosen_id_pa": "",
        "chosen_order_id": "",
        "chosen_image_type": "",
        "tryon_reason": "",
        "index_reason": "",
        "tryon_error": "",
        "index_error": "",
        "tryon_raw_response_tail": "",
        "index_raw_response_tail": "",
    }

    if not cand:
        row_out["tryon_error"] = "无候选图（该 id_goods+id_pa 在 product_image 中无记录或全部为 empty 占位图）"
        row_out["index_error"] = "无候选图（该 id_goods+id_pa 在 product_image 中无记录或全部为 empty 占位图）"
        return row_out

    # ── 任务 1：选 tryon_image ──
    tryon_chosen = 0
    tryon_reason = ""
    tryon_raw = ""
    tryon_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            tryon_chosen, tryon_reason, tryon_raw = call_vlm_pick_tryon_image(
                image_urls=cand_urls,
                attr_alias=attr_alias,
                style_no=style,
                id_pa=id_pa,
                model=args.model,
                api_base=args.local_api_base,
                api_key=args.local_api_key,
                max_tokens=args.max_tokens,
                timeout_sec=args.timeout_sec,
                enable_thinking=args.enable_thinking,
                max_pixels=args.max_pixels,
                min_pixels=args.min_pixels,
                image_quality=args.image_quality,
            )
            break
        except Exception as exc:
            tryon_err = str(exc)
            time.sleep(RETRY_DELAY_SEC + random.random())

    if tryon_err and tryon_chosen == 0 and not tryon_raw:
        row_out["tryon_error"] = tryon_err
    else:
        row_out["tryon_index"] = tryon_chosen
        row_out["tryon_reason"] = tryon_reason
        if tryon_raw:
            row_out["tryon_raw_response_tail"] = tryon_raw[-1200:]

        # tryon_image (white_front_url)
        if 1 <= tryon_chosen <= len(cand):
            chosen_row = cand[tryon_chosen - 1]
            row_out["white_front_url"] = chosen_row["path"]
            row_out["chosen_id_pa"] = chosen_row["id_pa"]
            row_out["chosen_order_id"] = chosen_row["order_id"]
            row_out["chosen_image_type"] = chosen_row["image_type"]
        else:
            row_out["tryon_error"] = row_out["tryon_error"] or "模型未选中任何 tryon 图(tryon_index=0)"

    # ── 任务 2：选 index_images ──
    index_indices: list[int] = []
    index_reason = ""
    index_raw = ""
    index_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            index_indices, index_reason, index_raw = call_vlm_pick_index_images(
                image_urls=cand_urls,
                attr_alias=attr_alias,
                style_no=style,
                id_pa=id_pa,
                model=args.model,
                api_base=args.local_api_base,
                api_key=args.local_api_key,
                max_tokens=args.max_tokens,
                timeout_sec=args.timeout_sec,
                enable_thinking=args.enable_thinking,
                max_pixels=args.max_pixels,
                min_pixels=args.min_pixels,
                image_quality=args.image_quality,
            )
            break
        except Exception as exc:
            index_err = str(exc)
            time.sleep(RETRY_DELAY_SEC + random.random())

    if index_err and not index_indices and not index_raw:
        row_out["index_error"] = index_err
    else:
        row_out["index_reason"] = index_reason
        if index_raw:
            row_out["index_raw_response_tail"] = index_raw[-1200:]

        # index_images: 收集多张商品图 URL
        index_urls: list[str] = []
        for idx in index_indices:
            if 1 <= idx <= len(cand):
                url = cand[idx - 1]["path"]
                if url not in index_urls:
                    index_urls.append(url)
        row_out["index_images"] = json.dumps(index_urls, ensure_ascii=False)

    return row_out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "FILA：图片预处理 — 选取 tryon_image（纯色背景、无模特、无大文字的商品静物主图，"
            "按 正面>侧面>背面 优先级）"
            "和 index_images（所有商品图，尽量优先纯色背景）"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="config.yaml 路径，默认 fila_agent_html/config.yaml",
    )
    parser.add_argument(
        "--product-dir",
        type=Path,
        default=DEFAULT_PRODUCT_DIR,
        help="含 product_*.csv 的目录，默认 data/tables",
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=["catalog", "onsale"],
        default="catalog",
        help=(
            "SKU 来源：catalog=从 skus.jsonl（build_catalog 产出）读取，"
            "onsale=从 product_master+product_attr 读取全量在售 SKU（onsell=1）"
        ),
    )
    parser.add_argument(
        "--skus-jsonl",
        type=Path,
        default=DEFAULT_SKUS_JSONL,
        help="build_catalog.py 产出的 skus.jsonl，默认 config paths.processed_dir（仅 --source=catalog 时使用）",
    )
    parser.add_argument(
        "--local-api-base",
        type=str,
        default=None,
        help="OpenAI 兼容 API base_url，默认读 config models.vision_llm.base_url",
    )
    parser.add_argument(
        "--local-api-key",
        type=str,
        default=None,
        help="API Key，默认读 config models.vision_llm.api_key_env 对应环境变量",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="模型名，默认读 config models.vision_llm.model",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="max_tokens，默认读 config models.vision_llm.max_tokens",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=None,
        help="请求超时秒数，默认读 config models.vision_llm.timeout_sec",
    )
    parser.add_argument(
        "--enable-thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否开启 thinking，默认读 config models.vision_llm.enable_thinking",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_PRODUCT_DIR / "fila_sku_selected_images.csv",
        help="输出 CSV 路径（兼容旧名，含新增 index_images 列）",
    )
    parser.add_argument(
        "--sku-id",
        type=str,
        default=None,
        help="可选：仅处理指定 sku_id（货号），多个用英文逗号分隔，便于小批量测试",
    )
    parser.add_argument(
        "--articles",
        type=Path,
        default=None,
        help="可选：每行一个货号 attr_alias，仅处理这些 SKU",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="仅处理前 N 条 SKU（0 表示全部）",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=48,
        help="每个 SKU 参与推理的最大候选图张数（按 order_id 排序截断）",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=40,
        help="并发线程数（每条 SKU 会下载多张图并调 API，不宜过大）",
    )
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=DEFAULT_MAX_PIXELS,
    )
    parser.add_argument(
        "--min-pixels",
        type=int,
        default=DEFAULT_MIN_PIXELS,
    )
    parser.add_argument(
        "--image-quality",
        type=int,
        default=DEFAULT_IMAGE_QUALITY,
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        default=False,
        help="从已有输出 CSV 中读取出错行（tryon_error 或 index_error 非空），仅重试这些 SKU 并更新原 CSV",
    )

    args = parser.parse_args()
    vision = resolve_vision_llm_settings(args.config)
    args.local_api_base = args.local_api_base or vision["api_base"]
    args.local_api_key = args.local_api_key or vision["api_key"]
    args.model = args.model or vision["model"]
    args.max_tokens = (
        args.max_tokens if args.max_tokens is not None else vision["max_tokens"]
    )
    args.timeout_sec = (
        args.timeout_sec
        if args.timeout_sec is not None
        else vision["timeout_sec"]
    )
    if args.enable_thinking is None:
        args.enable_thinking = vision["enable_thinking"]

    product_dir = args.product_dir
    if not product_dir.is_dir():
        print(f"错误：目录不存在 {product_dir}", file=sys.stderr)
        return 1

    master_path = product_dir / "product_master.csv"
    if not master_path.is_file():
        print(f"错误：缺少 {master_path}", file=sys.stderr)
        return 1

    masters = load_masters(master_path)
    images_by_goods = load_images_by_goods(product_dir)

    if args.source == "onsale":
        skus = load_onsale_skus(product_dir)
        print(f"全量在售 SKU（product_master+product_attr）: {len(skus)}")
    else:
        skus_jsonl = args.skus_jsonl
        if not skus_jsonl.is_file():
            print(
                f"错误：缺少 {skus_jsonl}，请先运行 build_catalog.py",
                file=sys.stderr,
            )
            return 1
        skus = load_catalog_skus(skus_jsonl)
        print(f"catalog SKU（skus.jsonl）: {len(skus)}  来源: {skus_jsonl}")

    if not args.local_api_base:
        print(
            "错误：models.vision_llm.base_url 未配置（config 或 --local-api-base）",
            file=sys.stderr,
        )
        return 1
    if not args.local_api_key:
        print(
            f"错误：未设置 API Key，请 export {vision['api_key_env']} "
            "或在 config / --local-api-key 中提供",
            file=sys.stderr,
        )
        return 1

    venv_hint = (
        f"已激活虚拟环境: {os.environ.get('VIRTUAL_ENV')}"
        if os.environ.get("VIRTUAL_ENV")
        else "未检测到 VIRTUAL_ENV"
    )
    print(f"vision_llm 配置来源: {args.config}  ({venv_hint})")
    print(
        f"模型: {args.model}  API: {args.local_api_base}  "
        f"max_tokens: {args.max_tokens}  timeout: {args.timeout_sec}s  "
        f"enable_thinking: {args.enable_thinking}"
    )

    if args.sku_id:
        wanted_sku_ids = parse_sku_id_arg(args.sku_id)
        all_aliases = {s["attr_alias"] for s in skus}
        missing = wanted_sku_ids - all_aliases
        if missing:
            print(
                f"警告：以下 sku_id 在 catalog 中未找到: {sorted(missing)}",
                file=sys.stderr,
            )
        skus = [s for s in skus if s["attr_alias"] in wanted_sku_ids]
        print(f"按 --sku-id 过滤: {sorted(wanted_sku_ids)} -> {len(skus)} 条")

    if args.articles and args.articles.is_file():
        wanted = {
            x.strip()
            for x in args.articles.read_text(encoding="utf-8").splitlines()
            if x.strip()
        }
        skus = [s for s in skus if s["attr_alias"] in wanted]

    if args.limit and args.limit > 0:
        skus = skus[: args.limit]

    print(f"待处理 SKU 条数: {len(skus)}")

    # ── retry-errors 模式：从已有 CSV 读取出错行 ──
    original_csv_rows: List[Dict[str, str]] = []
    original_rows_by_alias: Dict[str, Dict[str, str]] = {}
    if args.retry_errors:
        if not args.output.is_file():
            print(f"错误：--retry-errors 需要已有输出文件 {args.output}", file=sys.stderr)
            return 1
        original_csv_rows = read_csv_dicts(args.output)
        error_aliases: set[str] = set()
        for row in original_csv_rows:
            alias = (row.get("货号") or "").strip()
            if not alias:
                continue
            tryon_err = (row.get("tryon_error") or "").strip()
            index_err = (row.get("index_error") or "").strip()
            if tryon_err or index_err:
                error_aliases.add(alias)
                original_rows_by_alias[alias] = row
        tryon_only = sum(1 for r in original_csv_rows if (r.get("tryon_error") or "").strip() and not (r.get("index_error") or "").strip())
        index_only = sum(1 for r in original_csv_rows if (r.get("index_error") or "").strip() and not (r.get("tryon_error") or "").strip())
        both_err = sum(1 for r in original_csv_rows if (r.get("tryon_error") or "").strip() and (r.get("index_error") or "").strip())
        print(
            f"已有 CSV 总行数: {len(original_csv_rows)}, "
            f"出错行数: {len(error_aliases)} "
            f"(仅 tryon 错: {tryon_only}, 仅 index 错: {index_only}, 两者都错: {both_err})"
        )
        # 过滤只保留出错 SKU
        skus = [s for s in skus if s["attr_alias"] in error_aliases]
        missing = error_aliases - {s["attr_alias"] for s in skus}
        if missing:
            print(
                f"警告：以下出错货号在当前 SKU 列表中未找到，将跳过: {sorted(missing)}",
                file=sys.stderr,
            )
        print(f"待重试 SKU 条数: {len(skus)}")

    fieldnames = [
        "货号",
        "款号",
        "id_goods",
        "id_pa",
        "candidate_count",
        "white_front_url",
        "index_images",
        "tryon_index",
        "chosen_id_pa",
        "chosen_order_id",
        "chosen_image_type",
        "tryon_reason",
        "index_reason",
        "tryon_error",
        "index_error",
        "tryon_raw_response_tail",
        "index_raw_response_tail",
    ]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    results: List[Optional[Dict[str, Any]]] = [None] * len(skus)

    def worker(ii: int, sku: Dict[str, Any]) -> None:
        r = process_one_sku(sku, masters, images_by_goods, args)
        with lock:
            results[ii] = r

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as ex:
        futs = {
            ex.submit(worker, i, s): i
            for i, s in enumerate(skus)
        }
        with tqdm(total=len(skus), unit="sku") as bar:
            for fut in as_completed(futs):
                fut.result()
                bar.update(1)

    # ── 合并 & 写出 ──
    if args.retry_errors and original_csv_rows:
        # 构建新结果查找表（按货号）
        new_by_alias: Dict[str, Dict[str, Any]] = {}
        for r in results:
            if r is None:
                continue
            alias = str(r.get("货号") or "").strip()
            if alias:
                new_by_alias[alias] = r

        # tryon 相关字段
        tryon_fields = [
            "white_front_url", "tryon_index", "chosen_id_pa",
            "chosen_order_id", "chosen_image_type",
            "tryon_reason", "tryon_error", "tryon_raw_response_tail",
        ]
        # index 相关字段
        index_fields = [
            "index_images", "index_reason", "index_error", "index_raw_response_tail",
        ]

        merged_rows: List[Dict[str, Any]] = []
        tryon_fixed = 0
        index_fixed = 0
        for row in original_csv_rows:
            alias = (row.get("货号") or "").strip()
            if alias in new_by_alias:
                new_row = new_by_alias[alias]
                orig_tryon_err = bool((row.get("tryon_error") or "").strip())
                orig_index_err = bool((row.get("index_error") or "").strip())
                new_tryon_ok = bool(str(new_row.get("white_front_url", "")).strip())
                new_index_ok = bool(
                    new_row.get("index_images")
                    and str(new_row["index_images"]).strip() not in ("", "[]")
                )

                merged = dict(row)  # 从原始行开始
                if orig_tryon_err:
                    if new_tryon_ok:
                        for f in tryon_fields:
                            if f in new_row:
                                merged[f] = str(new_row[f]) if new_row[f] is not None else ""
                        tryon_fixed += 1
                    else:
                        # 仍失败，更新错误信息
                        merged["tryon_error"] = str(new_row.get("tryon_error", ""))
                        merged["tryon_raw_response_tail"] = str(new_row.get("tryon_raw_response_tail", ""))

                if orig_index_err:
                    if new_index_ok:
                        for f in index_fields:
                            if f in new_row:
                                merged[f] = str(new_row[f]) if new_row[f] is not None else ""
                        index_fixed += 1
                    else:
                        merged["index_error"] = str(new_row.get("index_error", ""))
                        merged["index_raw_response_tail"] = str(new_row.get("index_raw_response_tail", ""))

                merged_rows.append(merged)
            else:
                merged_rows.append(row)

        # 统计最终结果
        final_tryon_ok = sum(
            1 for r in merged_rows
            if str(r.get("white_front_url", "")).strip()
        )
        final_index_ok = sum(
            1 for r in merged_rows
            if r.get("index_images") and str(r["index_images"]).strip() not in ("", "[]")
        )
        final_tryon_err = sum(
            1 for r in merged_rows
            if str(r.get("tryon_error", "")).strip()
        )
        final_index_err = sum(
            1 for r in merged_rows
            if str(r.get("index_error", "")).strip()
        )
        total = len(merged_rows)

        with args.output.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            for r in merged_rows:
                w.writerow(r)

        print(
            f"重试完成。输出: {args.output}  "
            f"总行数: {total}  "
            f"本次修复 tryon: {tryon_fixed}  index: {index_fixed}"
        )
        print(
            f"最终 tryon 选出: {final_tryon_ok}/{total} (错误: {final_tryon_err})  "
            f"最终 index_images 选出: {final_index_ok}/{total} (错误: {final_index_err})"
        )
    else:
        with args.output.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            for r in results:
                if r is not None:
                    w.writerow(r)

        ok_tryon = sum(
            1
            for r in results
            if r and str(r.get("white_front_url", "")).strip()
        )
        ok_index = sum(
            1
            for r in results
            if r and r.get("index_images") and r["index_images"] != "[]"
        )
        tryon_err_count = sum(
            1 for r in results if r and str(r.get("tryon_error", "")).strip()
        )
        index_err_count = sum(
            1 for r in results if r and str(r.get("index_error", "")).strip()
        )
        print(
            f"完成。输出: {args.output}  "
            f"tryon 选出: {ok_tryon}/{len(skus)} (错误: {tryon_err_count})  "
            f"index_images 选出: {ok_index}/{len(skus)} (错误: {index_err_count})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
