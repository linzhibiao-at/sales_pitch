#!/usr/bin/env python3
"""合作方直连裸 vLLM 调用示例（FILA 穿搭评分，V6）。

服务实际部署的是裸 vLLM（非评分封装层），没有 /v1/outfit/score。
合作方需自行：构造 V6 system prompt + 多图 image_url + user_text，调
/v1/chat/completions，并解析模型输出的多维度文本。

本文件自包含，无项目依赖。仅需 `pip install httpx`（或改用 requests）。

用法：
  python deploy/partner_call_example.py
"""
import json
import re
import time
import urllib.request

BASE_URL = "http://10.215.6.10:32207"   # NodePort，直连 vLLM
MODEL = "fila-outfit-v6_1"

# ── V6 系统 prompt（逐字复制自项目 scripts/prompt.py SYSTEM_PROMPT_V6）────────────
SYSTEM_PROMPT_V6 = """你是 FILA（斐乐）专业穿搭美学评审专家，只从**美学与设计**角度评判搭配的协调性与美感。
你评分极其严格、客观，绝不虚高，且严格拉开不同质量档次的分差。
核心原则：评分反映"现有商品的色彩、款式、材质、设计语言与层次的协调性与美学质量"，不因件数多寡而机械加减——2-3件若色彩与设计出色同样可得高分；件数多但杂乱无章也应低分。

评分维度（仅美学，5 维，各 0-10）：
- 色彩搭配：色调和谐度、配色方案一致性（同色系/互补色/撞色），FILA品牌配色语言延续性
- 款式协调：廓形匹配、设计语言统一、LOGO/细节呼应（不含颜色判断）
- 时尚匹配：FILA时尚运动美学统一度与设计语言一致性，含系列协调（同系列=9-10；ORIGINALE/HERITAGE/WHITE/MILANO等休闲系列间=7-9；休闲与FITNESS/TENNIS/GOLF等运动系列=6-8；不同运动专项系列间=5-7，FILA品牌特色可时尚混搭）
- 材质适配：面料质感协调、厚薄功能性匹配（防水+防水、轻薄+轻薄）
- 整体层次感：廓形/色彩/叠穿层次丰富度与结构章法（外套+内搭+下装+配件多层次=9-10；有内外层次=7-8；简洁两件有呼应=5-6；单调无层次=3-4）

色彩协调强化规则（必须遵守）：
- 颜色撞色（如红+绿、橙+紫等高饱和对立色相并存）、超过3种强色并存、或与FILA品牌配色语言明显不符 → 色彩搭配≤5，且综合评分≤6
- 全身色彩应控制在2-3个主色以内，色彩呼应合理；超过则扣分

综合评分规则（必须遵守）：
1. 若"色彩搭配"≤ 5，综合评分不超过 6 分（色彩冲突）
2. FILA各运动系列之间不设综合评分上限（时尚包容性）
3. 其余情况：综合评分是对现有商品整体美感与协调性的综合感知判断，不要机械加权

评分锚点（严格，避免虚高与撞分，按设计质量分档）：
- 9-10分：色彩精心设计呼应、系列协调统一、整体层次丰富的优秀搭配。
- 7-8.5分：协调良好、颜色基本和谐、有设计章法，但层次或色彩设计感略欠。
- 5-6.5分：件数合理但系列随机混搭或色彩平平，缺少设计协调感。
- 3-4.5分：明显色彩/风格不协调（撞色、色彩混乱）或设计语言冲突。
- 1-2.5分：严重撞色或美学严重失调。

相邻档次分差要求（确保可排序）：
- 色彩精心设计 > 色彩平平，分差≥1.0
- 有设计章法（T4，7-8.5）> 随机混搭无设计感（T3，5-6.5），分差≥1.5
- 优秀搭配（T5，9-10）须明显高于 T4，分差≥1.5

输出格式严格要求：第一行"综合评分：X/10"，然后5行"- 维度：X/10"（色彩搭配/款式协调/时尚匹配/材质适配/整体层次感），最后"评语：..."（≤25字）。
所有分值为整数或0.5步进（如8/10或8.5/10），不允许其他小数（如8.2/10）。
不要输出格式之外的任何内容。"""

# user_text 中的输出格式块（五维）
OUTPUT_FORMAT = (
    "综合评分：X/10\n"
    "- 色彩搭配：X/10\n"
    "- 款式协调：X/10\n"
    "- 时尚匹配：X/10\n"
    "- 材质适配：X/10\n"
    "- 整体层次感：X/10\n"
    "评语：..."
)

# V6.1 五维 + 综合评分（模型按此五维训练，严格解析这 6 项；任何多余维度行直接忽略）
DIM_PATTERN = {
    "综合评分": "composite",
    "色彩搭配": "color",
    "款式协调": "style",
    "时尚匹配": "fashion",
    "材质适配": "material",
    "整体层次感": "layering",
}
SCORE_KEYS = ["composite", "color", "style", "fashion", "material", "layering"]


def make_item_desc(item: dict, idx: int) -> str:
    """逐字对齐训练时 02_build_training_data.make_item_desc 的拼法。

    字段名与训练一致：品类用 cat_alias（rule_filter 归一化后的品类别名，
    非原始品类名），颜色用 color_name。合作方需传归一化后的品类，否则
    推理文本分布会与训练偏移。
    """
    up_down = item.get("up_down", "") or ""
    cat = item.get("cat_alias", "") or ""
    cat_str = f"{cat}（{up_down}）" if up_down and up_down.lower() not in ("nan", "") else cat
    parts = [
        item.get("title", "") or cat,
        f"系列：{item.get('series', '') or '-'}",
        f"品类：{cat_str or '-'}",
        f"性别：{item.get('sex', '') or '-'}",
        f"季节：{item.get('season', '') or '-'}",
    ]
    color = item.get("color_name", "") or ""
    if color:
        parts.append(f"颜色：{color}")
    price = item.get("price", "0")
    price = "0" if str(price).strip().lower() in ("nan", "none", "") else str(price)
    parts.append(f"价格：¥{price}")
    return f"商品{idx}（图{idx}）：" + "｜".join(parts)


def build_user_text(items: list) -> str:
    """逐字对齐训练时 02_build_training_data.make_sample 的 user_text 构造（无负样本约束）。"""
    n = len(items)
    intro = (
        f"请综合图片和商品属性，对以下 {n} 件 FILA（斐乐）商品组成的搭配进行多维度评分。\n\n"
        if n > 2 else
        "请综合图片和商品属性，对以下两件 FILA（斐乐）商品的搭配进行多维度评分。\n\n"
    )
    desc = "\n".join(make_item_desc(it, i + 1) for i, it in enumerate(items))
    return intro + desc + "\n\n" + "输出格式（严格遵守）：\n" + OUTPUT_FORMAT


def extract_scores(text: str) -> dict:
    """严格按 V6.1 五维解析。只取 composite+5 维+评语；多余行忽略。
    parse_ok = composite 与 5 维齐全。"""
    out = {key: None for key in DIM_PATTERN.values()}
    out["comment"] = ""
    out["parse_ok"] = False
    for zh, key in DIM_PATTERN.items():
        m = re.search(rf"{zh}[：:]\s*(\d+(?:\.\d+)?)\s*/\s*10", text)
        if m:
            out[key] = float(m.group(1))
    m = re.search(r"评语[：:]\s*(.+?)(?:\n|$)", text)
    if m:
        out["comment"] = m.group(1).strip()
    out["parse_ok"] = all(out[k] is not None for k in SCORE_KEYS)
    out["raw_output"] = text
    return out


def build_messages(items: list) -> list:
    content = [{"type": "image_url", "image_url": {"url": it["image_url"]}}
               for it in items]
    content.append({"type": "text", "text": build_user_text(items)})
    return [
        {"role": "system", "content": SYSTEM_PROMPT_V6},
        {"role": "user", "content": content},
    ]


def score_outfit(items: list, outfit_id: str = "", timeout: float = 60.0) -> dict:
    """对一套穿搭打分。items: 2-5 件，每件含 image_url（必填）+ 可选
    title/series/cat_alias/sex/season/price/color_name/up_down。"""
    assert 2 <= len(items) <= 5, "items 须 2-5 件"
    body = json.dumps({
        "model": MODEL,
        "messages": build_messages(items),
        "max_tokens": 600,
        "temperature": 0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    dt_ms = int((time.time() - t0) * 1000)
    text = resp["choices"][0]["message"]["content"]
    scores = extract_scores(text)
    return {
        "outfit_id": outfit_id,
        "model": MODEL,
        "scores": {k: scores[k] for k in SCORE_KEYS},
        "comment": scores["comment"],
        "parse_ok": scores["parse_ok"],
        "latency_ms": dt_ms,
        "raw_output": scores["raw_output"],
    }


if __name__ == "__main__":
    # 示例：两件 FILA 商品（图片须服务 Pod 可达的公网 URL）
    items = [
        {"sku_id": "T11M638801F", "title": "FILA FUSION 潦草小狗联名男士宽松梭织五分裤",
         "image_url": "https://img.fishfay.com/shopgoods/7/T11M638801F/T11M638801FLK/11/490d13509292d9365fb103ff5211791d.jpg",
         "series": "FILA X moonge", "cat_alias": "梭织五分裤", "up_down": "下装", "sex": "男士", "season": "秋季", "price": 740},
        {"sku_id": "T11U638105F", "title": "FILA FUSION 潦草小狗联名男女同款基础短袖T恤",
         "image_url": "https://img.fishfay.com/shopgoods/7/T11U638105F/T11U638105FWT/11/effe7caa83d12926680b0a068829da28.jpg",
         "series": "FILA X moonge", "cat_alias": "短袖T", "up_down": "上装", "sex": "中性", "season": "秋季", "price": 480},
    ]
    res = score_outfit(items, outfit_id="demo-001")
    print(json.dumps(res, ensure_ascii=False, indent=2))
