#!/usr/bin/env python3
"""
FILA v8sws 穿搭评分客户端（裸 vLLM 直连，自包含）。

合作方只需 `pip install requests`，改 BASE_URL 即可调用：
  from v8sws_client import FilaScorer
  scorer = FilaScorer("http://10.215.6.10:32616")
  r = scorer.score([
      {"image_url":"https://.../1.jpg","title":"运动BRA","series":"FUSION","category":"BRA","sex":"女"},
      {"image_url":"https://.../2.jpg","title":"梭织运动长裤","series":"HERITAGE"},
  ])
  print(r["composite"], r["comment"])

返回（dict）：
  composite       综合分（默认外部 f 覆写，排序用，0-10）
  color/style/fashion/material/layering   五维子分（0-10）
  composite_raw   模型自生成综合分（仅参考，不用排序）
  comment         评语
  parse_ok        是否成功解析
  latency_ms      端到端耗时

也可命令行直接跑：python v8sws_client.py --items '[{...},{...}]'
"""
import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

# ── 服务配置 ──────────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "http://10.215.6.10:32616"
DEFAULT_MODEL = "fila-outfit-v8sws"
DEFAULT_TIMEOUT = 60
MAX_TOKENS = 700          # V8 输出（5维CoT+综合+评语）约 450-600 token，700 留余量
MAX_ITEMS = 5
MIN_ITEMS = 2

# ── V8 系统提示（与服务训练/评测逐字一致，必须原样使用，勿改）──────────────────
SYSTEM_PROMPT_V8 = """你是 FILA（斐乐）专业穿搭美学评审专家，从**美学与设计**角度评判搭配。
**评分顺序**：先逐维度观察打分（5维），最后基于以上维度推理给综合评分——综合评分必须反映各维度观察的具体情况，不得默认中位分，平庸档内部也要区分5.0/5.5/6.0/6.5，禁止扎堆5.5。

评分维度（5维，各0-10，0.5步进）：每维度先写1-2句**视觉观察**（具体到色相/廓形/面料/系列/层次），再给分。
- 色彩搭配：色调呼应/撞色/品牌配色。撞色（红+绿等高饱和对立）、>3种强色、与FILA配色不符→≤5。
- 款式协调：廓形匹配/设计语言/LOGO呼应。
- 时尚匹配：系列协调（同系列9-10；休闲系列间7-9；休闲与运动6-8；不同运动专项间5-7，FILA可时尚混搭）。
- 材质适配：面料质感/厚薄功能匹配（防水+防水、轻薄+轻薄好；厚薄混搭/功能矛盾→≤5）。
- 整体层次感：叠穿/结构章法（多层丰富9-10；有内外层次7-8；简洁呼应5-6；单调3-4）。

综合评分规则：若色彩≤5则综合≤6（色彩冲突）；FILA各运动系列间不设上限；其余基于各维度观察综合判断，不要机械加权。
评分锚点：9-10优秀（色彩精心设计+系列统一+层次丰富）；7-8.5良好；5-6.5平庸（随机混搭/色彩平平）；3-4.5不协调；1-2.5严重失调。相邻档差≥1.5。

输出格式（严格遵守：先5行维度观察+分，再综合评分，最后评语）：
色彩搭配：袖口蓝绿滚边与薄荷绿长裤呼应，但红鞋撞色突兀 6/10
款式协调：廓形统一复古风协调，LOGO细节呼应 7/10
时尚匹配：同属休闲系列混搭自然，设计语言相近 7/10
材质适配：防晒面料与棉质T恤质感冲突 4/10
整体层次感：两件简洁无叠穿层次单薄 5/10
综合评分：5.5/10
评语：色彩与材质冲突明显整体协调性一般

（上面是格式示例。请对每套搭配输出：5行维度（先视觉观察再给0-10分），然后1行综合评分（基于以上维度推理，0-10分），最后1行评语≤25字。分值用整数或0.5步进如6.5/10。不要输出格式之外的内容。）"""

OUTPUT_FORMAT = (
    "色彩搭配：[1-2句视觉观察] X/10\n"
    "款式协调：[观察] X/10\n"
    "时尚匹配：[观察] X/10\n"
    "材质适配：[观察] X/10\n"
    "整体层次感：[观察] X/10\n"
    "综合评分：X/10\n"
    "评语：..."
)

# ── 外部 f 权重：composite = intercept + Σ w·per-dim（68k 教师数据拟合，R²0.978）──
# 用外部 f 覆写模型自生成 composite，排序准确率 0.7393→0.7471（+0.05）
_F_INTERCEPT = -0.6684100917199061
_F_W = {
    "color": 0.2626135858739374,
    "style": 0.14683599083360675,
    "fashion": 0.2175721340082032,
    "material": 0.19416058180503382,
    "layering": 0.24777462828335445,
}
# 备选：material 降到 0.15（其余按 25c 重拟合），pairwise 0.7471→0.7482（+0.001，可选）
_F_W_MAT015 = {**_F_W, "material": 0.15, "color": 0.264, "style": 0.157,
               "fashion": 0.232, "layering": 0.260, "_intercept": -0.619}

DIM_KEYS = ["color", "style", "fashion", "material", "layering"]


def apply_f(dims: dict) -> Optional[float]:
    """composite = intercept + Σ w·per-dim。缺任一维返回 None。"""
    try:
        return round(_F_INTERCEPT + sum(_F_W[k] * dims[k] for k in DIM_KEYS), 2)
    except (KeyError, TypeError):
        return None


# ── user_text 构造（从 items 拼商品描述行）─────────────────────────────────────
def make_item_desc(item: dict, idx: int) -> str:
    up_down = (item.get("up_down") or "").strip()
    if up_down.lower() in ("nan", ""):
        up_down = ""
    cat = (item.get("category") or item.get("cat_alias") or "").strip()
    cat_str = f"{cat}（{up_down}）" if up_down and cat else (cat or "")
    parts = [
        (item.get("title") or "").strip() or cat_str or "-",
        f"系列：{(item.get('series') or '').strip() or '-'}",
        f"品类：{cat_str or '-'}",
        f"性别：{(item.get('sex') or '').strip() or '-'}",
        f"季节：{(item.get('season') or '').strip() or '-'}",
    ]
    return f"商品{idx}（图{idx}）：" + "｜".join(parts)


def build_user_text(items: list) -> str:
    n = len(items)
    intro = (f"请综合图片和商品属性，对以下 {n} 件 FILA（斐乐）商品组成的搭配进行多维度评分。\n\n"
             if n > 2 else "请综合图片和商品属性，对以下两件 FILA（斐乐）商品的搭配进行多维度评分。\n\n")
    body = "\n".join(make_item_desc(it, i + 1) for i, it in enumerate(items))
    return intro + body + "\n\n输出格式（严格遵守）：\n" + OUTPUT_FORMAT


# ── 输出解析 ───────────────────────────────────────────────────────────────────
def extract_scores(text: str) -> dict:
    res = {k: None for k in DIM_KEYS}
    res["composite_raw"] = None
    res["comment"] = ""
    m = re.search(r"综合评分[：:]\s*(\d+(?:\.\d+)?)\s*/\s*10", text)
    if m:
        res["composite_raw"] = float(m.group(1))
    dim_pat = {"色彩搭配": "color", "款式协调": "style", "时尚匹配": "fashion",
               "材质适配": "material", "整体层次感": "layering"}
    for zh, key in dim_pat.items():
        m = re.search(rf"{zh}[：:].*?(\d+(?:\.\d+)?)\s*/\s*10", text)
        if m:
            res[key] = float(m.group(1))
    m = re.search(r"评语[：:]\s*(.+?)(?:\n|$)", text)
    if m:
        res["comment"] = m.group(1).strip()
    all_dims = all(res[k] is not None for k in DIM_KEYS)
    res["parse_ok"] = all_dims and res["composite_raw"] is not None
    res["composite"] = apply_f(res) if all_dims else None  # 外部 f 覆写
    return res


# ── 客户端 ─────────────────────────────────────────────────────────────────────
class FilaScorer:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                 timeout: int = DEFAULT_TIMEOUT, use_external_f: bool = True,
                 api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.use_external_f = use_external_f
        self.api_key = api_key
        self.session = requests.Session()

    def _call(self, items: list) -> str:
        if not (MIN_ITEMS <= len(items) <= MAX_ITEMS):
            raise ValueError(f"items 数量须 {MIN_ITEMS}-{MAX_ITEMS}，实际 {len(items)}")
        for i, it in enumerate(items):
            if not (it.get("image_url") or "").strip():
                raise ValueError(f"第 {i+1} 件缺 image_url")
        user_content = [{"type": "image_url", "image_url": {"url": it["image_url"]}}
                        for it in items if it.get("image_url")]
        user_content.append({"type": "text", "text": build_user_text(items)})
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_V8},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.0,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        r = self.session.post(f"{self.base_url}/v1/chat/completions",
                              json=payload, timeout=self.timeout, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"vLLM HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    def score(self, items: list, outfit_id: Optional[str] = None) -> dict:
        """单套打分。返回 composite(外部f或raw) + 5维 + composite_raw + 评语 + parse_ok + latency。"""
        t0 = time.time()
        raw = self._call(items)
        sc = extract_scores(raw)
        composite = sc["composite"] if self.use_external_f else sc["composite_raw"]
        return {
            "outfit_id": outfit_id,
            "composite": composite,
            "color": sc["color"], "style": sc["style"], "fashion": sc["fashion"],
            "material": sc["material"], "layering": sc["layering"],
            "composite_raw": sc["composite_raw"],
            "comment": sc["comment"],
            "parse_ok": sc["parse_ok"],
            "latency_ms": int((time.time() - t0) * 1000),
        }

    def score_batch(self, outfits: list, concurrency: int = 8) -> list:
        """批量打分。outfits=[{"outfit_id":..,"items":[...]}, ...]。并发建议 ≤8。"""
        results = [None] * len(outfits)
        with ThreadPoolExecutor(max_workers=min(concurrency, 8)) as ex:
            fut = {ex.submit(self.score, o["items"], o.get("outfit_id")): i
                   for i, o in enumerate(outfits)}
            for f in as_completed(fut):
                i = fut[f]
                try:
                    results[i] = f.result()
                except Exception as e:
                    results[i] = {"outfit_id": outfits[i].get("outfit_id"),
                                  "parse_ok": False, "error": str(e)}
        return results


# ── CLI ────────────────────────────────────────────────────────────────────────
def _main():
    ap = argparse.ArgumentParser(description="FILA v8sws 穿搭评分客户端")
    ap.add_argument("--url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--items", help="items JSON 字符串，如 [{\"image_url\":..,\"title\":..},...]")
    ap.add_argument("--items_file", help="items JSON 文件（数组或 {outfits:[...]}）")
    ap.add_argument("--no_ext_f", action="store_true", help="不用外部 f，用模型自生成 composite")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    scorer = FilaScorer(args.url, args.model, use_external_f=not args.no_ext_f)

    if args.items_file:
        data = json.loads(open(args.items_file, encoding="utf-8").read())
        if isinstance(data, dict) and "outfits" in data:
            outfits = data["outfits"]
        elif isinstance(data, list) and data and isinstance(data[0], dict) and "items" in data[0]:
            outfits = data                       # [{outfit_id:.., items:[...]}, ...]
        elif isinstance(data, list) and data and isinstance(data[0], dict) and "outfits" in data[0]:
            outfits = [o for rec in data for o in rec.get("outfits", [])]  # [{outfits:[...]}]
        elif isinstance(data, list):
            outfits = [{"outfit_id": "items", "items": data}]   # 裸 items 数组
        else:
            raise ValueError("items_file 格式不识别：需 [{outfit_id,items}] 或 {outfits:[...]} 或 items 数组")
    elif args.items:
        items = json.loads(args.items)
        outfits = [{"outfit_id": "cli", "items": items}]
    else:
        # 自带示例（需要真实可达的图片 URL，否则会 422/取图失败）
        print("[示例] 未传 items，用内置示例（需真实 image_url）")
        outfits = [{"outfit_id": "demo", "items": [
            {"image_url": "https://img.fishfay.com/.../1.jpg", "title": "运动BRA", "series": "FUSION"},
            {"image_url": "https://img.fishfay.com/.../2.jpg", "title": "梭织运动长裤"},
        ]}]

    if len(outfits) == 1:
        r = scorer.score(outfits[0]["items"], outfits[0].get("outfit_id"))
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        rs = scorer.score_batch(outfits, concurrency=args.concurrency)
        ranked = sorted([r for r in rs if r and r.get("composite") is not None],
                        key=lambda x: -x["composite"])
        for i, r in enumerate(ranked):
            print(f"  #{i+1} composite={r['composite']} {r['outfit_id']} ({r.get('comment','')})")


if __name__ == "__main__":
    _main()
