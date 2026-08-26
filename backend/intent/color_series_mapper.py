"""颜色名 → 色系 映射：规则匹配 + YAML 缓存兜底。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_DICT_DIR = Path(__file__).resolve().parent / "dictionaries"
_CACHE_PATH = _DICT_DIR / "color_name_to_series_cache.yaml"

# ---------- 中文关键词规则（按优先级排列，先匹配先命中） ----------
_CHINESE_RULES: list[tuple[list[str], str]] = [
    (["黑"], "黑色系"),
    (["白", "雪", "象牙"], "白色系"),
    (["灰", "银"], "灰色系"),
    (["红", "丹", "朱", "绯", "赤", "酒红", "枣"], "红色系"),
    (["粉", "桃", "玫瑰", "樱"], "粉色系"),
    (["橙", "橘", "杏"], "橙色系"),
    (["黄", "金", "鹅", "姜", "柠檬"], "黄色系"),
    (["绿", "草", "抹茶", "薄荷", "橄榄", "翠", "苔", "军"], "绿色系"),
    (["蓝", "靛", "藏青", "湖", "天", "海军", "雾霾", "牛仔", "藍"], "蓝色系"),
    (["紫", "薰衣草", "兰花", "丁香"], "紫色系"),
    (["棕", "褐", "咖啡", "驼", "卡其", "焦糖", "可可", "栗", "巧克力", "土", "沙", "泥", "麻", "赭", "赭石"], "棕色系"),
    # 米色系：中性浅色调（米/燕麦/卡其/咖/奶咖等）。仅用多字关键词，避免单字
    # （米/咖/卡/砂/麦/糖）被 _rightmost_series 从「朱砂色(红)/浅棕卡(棕)/
    # 咖啡色(棕)/焦糖色(棕)」等名尾字盗走；单字根的具体米色名走 cache 精确兜底。
    ([
        "燕麦", "拿铁", "奶咖", "米咖", "米绸", "蕉咖", "贡米", "燕麦米",
        "薏米", "燕乳", "麦麸", "麦秆", "麦穗", "麦乳", "风吹麦浪", "薶麦",
        "流砂", "雨朦", "深冬咖", "秋芦", "春荞", "葛藤", "浅牙", "浅缃",
        "浅米", "砂米", "明中卡", "石头卡", "牡蛎卡", "乔其卡", "晨浅卡",
        "梵浅卡", "燕麦卡", "树屋", "榛蘑", "石乳", "啡酒", "油青", "奶啡",
        "焦香咖", "太妃糖", "椿糖", "硬糖", "奶油麦", "淀乳", "胭脂米",
    ], "米色系"),
]

# 多色/花色/印花关键词 → 多色系
_MULTICOLOR_KEYWORDS: list[str] = [
    "迷彩", "花", "印花", "扎染", "渐变", "拼色", "拼接",
    "撞色", "条纹", "格纹", "豹纹", "斑马", "碎花", "图案",
    "涂鸦", "满印", "提花", "织花", "彩虹", "混色", "多色",
    "tie-dye", "camo", "floral", "print", "stripe", "plaid",
    "pattern", "multi", "rainbow",
]

# ---------- 英文关键词规则（小写匹配） ----------
_ENGLISH_RULES: list[tuple[list[str], str]] = [
    (["black", "onyx", "ebony", "charcoal", "anthracite", "asphalt", "tap shoe",
      "stretch limo", "midnight", "raven", "salute", "peacoat"], "黑色系"),
    (["white", "snow", "ivory", "cream", "egret", "pristine", "marshmallow",
      "blanc", "bright white", "brilliant white", "coconut milk", "sugar swizzle",
      "no dye white"], "白色系"),
    (["gray", "grey", "silver", "ash", "fog", "smoke", "stone", "titanium",
      "cement", "heather", "pewter", "slate", "volcanic ash", "glacier",
      "pavement", "pumice", "pelican", "wind chime", "overcast",
      "harbor mist", "ultimate gray", "iron gate"], "灰色系"),
    (["red", "cherry", "scarlet", "crimson", "ruby", "racing red",
      "barbados", "cabernet", "sangria", "zinfandel", "lava",
      "rhododendron"], "红色系"),
    (["pink", "rose", "blush", "coral", "ballet", "ballerina",
      "prism pink", "primrose pink", "strawberry", "nostalgia rose",
      "pinkesque"], "粉色系"),
    (["orange", "tangerine", "apricot", "amber", "pumpkin",
      "papaya", "mango", "tiger"], "橙色系"),
    (["yellow", "gold", "golden", "lemon", "banana", "dandelion",
      "mustard", "canary", "sunshine", "sunny", "buttercup",
      "elfin yellow", "hay", "oil yellow", "transparent yellow"], "黄色系"),
    (["green", "olive", "mint", "sage", "moss", "forest", "alfalfa",
      "basil", "pistachio", "jadeite", "jasmine green", "leek",
      "piquant", "plantation", "bok choy", "green ash", "yucca",
      "margarita", "lime"], "绿色系"),
    (["blue", "navy", "cobalt", "azure", "indigo", "denim",
      "sapphire", "aqua", "teal", "ocean", "marine", "sea",
      "niagara", "bosphorus", "classic blue", "skipper",
      "windward", "zen blue", "moonbeam"], "蓝色系"),
    (["purple", "violet", "plum", "lavender", "orchid", "lilac",
      "mauve", "amethyst", "eggplant", "grape", "wisteria",
      "aster purple"], "紫色系"),
    (["brown", "beige", "khaki", "tan", "camel", "taupe",
      "chocolate", "coffee", "mocha", "cocoa", "walnut", "umber",
      "brindle", "humus", "fudge", "sepia", "tannin", "otter",
      "roan", "turtledove", "oatmeal"], "棕色系"),
]

# ---------- 英文缩写（精确匹配，小写） ----------
_ENGLISH_ABBR: dict[str, str] = {
    "bk": "黑色系",
    "wh": "白色系",
    "wt": "白色系",
    "gr": "灰色系",
    "cg": "灰色系",
    "sl": "灰色系",
    "rd": "红色系",
    "cr": "红色系",
    "pk": "粉色系",
    "lp": "粉色系",
    "ye": "黄色系",
    "yl": "黄色系",
    "gd": "黄色系",
    "gn": "绿色系",
    "bl": "蓝色系",
    "bu": "蓝色系",
    "nv": "蓝色系",
    "sb": "蓝色系",
    "lb": "蓝色系",
    "pp": "紫色系",
    "lv": "紫色系",
    "br": "棕色系",
    "be": "棕色系",
    "kh": "棕色系",
    "iv": "白色系",
    "db": "蓝色系",
    "dv": "紫色系",
    "lg": "绿色系",
    "fg": "灰色系",
    "mg": "灰色系",
    "gy": "灰色系",
    "wg": "灰色系",
    "em": "绿色系",
    "dr-g": "红色系",
    "bk-1-g": "黑色系",
    "kk": "棕色系",
    "kk-27": "棕色系",
    "hp": "粉色系",
    "mu": "紫色系",
    "mt": "棕色系",
    "pt": "棕色系",
    "bw": "黑色系",
}

# ---------- 缓存 ----------
# 值为 list[str]；旧式 str 值在 _load_cache / 查询时自动包装为 [v]，兼容期无需显式迁移。
_cache: dict[str, list[str]] | None = None


def _coerce_to_list(v: object) -> list[str]:
    """把缓存条目归一为 list[str]。旧式 str → [str]；空值 → []。"""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _load_cache() -> dict[str, list[str]]:
    global _cache
    if _cache is not None:
        return _cache
    if _CACHE_PATH.is_file():
        try:
            with _CACHE_PATH.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _cache = {str(k): _coerce_to_list(v) for k, v in data.items()}
        except Exception:
            logger.warning("加载色系缓存失败: %s", _CACHE_PATH)
            _cache = {}
    else:
        _cache = {}
    return _cache


def save_cache(cache: dict[str, list[str]]) -> None:
    global _cache
    _cache = cache
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(
            cache,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=True,
        )
    logger.info("色系缓存已写入: %s (%d 条)", _CACHE_PATH, len(cache))


def _rightmost_series(
    name: str, rules: list[tuple[list[str], str]]
) -> str | None:
    """在 name 中扫描所有规则关键词，返回位置最靠后（rfind 最大）的关键词所属色系。

    并列时取更长关键词（更具体）。无命中返回 None。
    单子串多色相如 "水蓝灰" 只取最后的 "灰" → 灰色系，而非 [蓝色系, 灰色系]。
    """
    best_pos = -1
    best_len = 0
    best_series: str | None = None
    for keywords, series in rules:
        for kw in keywords:
            pos = name.rfind(kw)
            if pos == -1:
                continue
            if pos > best_pos or (pos == best_pos and len(kw) > best_len):
                best_pos = pos
                best_len = len(kw)
                best_series = series
    return best_series


def _match_chinese(name: str) -> list[str]:
    """返回 name 中最靠后的颜色关键词所属色系（单值列表）。

    "水蓝灰" → ["灰色系"]；"蓝白银" → ["灰色系"]（银最靠后）；无命中 → []。
    """
    series = _rightmost_series(name, _CHINESE_RULES)
    return [series] if series else []


def _match_multicolor(name: str) -> bool:
    """检测多色/花色/印花关键词。命中则整名视为图案，由上层返回 ["多色系"]。"""
    low = name.lower()
    for kw in _MULTICOLOR_KEYWORDS:
        if kw in name or kw in low:
            return True
    return False


def _match_english(name: str) -> list[str]:
    """返回英文规则命中的最靠后色系（缩写精确匹配 + 关键词子串匹配，单值）。

    多个英文色相出现时取位置最靠后者，如 "stone blue" → ["蓝色系"]。
    """
    low = name.lower().strip()
    best_pos = -1
    best_len = 0
    best_series: str | None = None
    # 精确缩写匹配（整串命中，位置 0）
    if low in _ENGLISH_ABBR:
        best_pos = 0
        best_len = len(low)
        best_series = _ENGLISH_ABBR[low]
    # 关键词子串匹配：取 rfind 最靠后者
    for keywords, series in _ENGLISH_RULES:
        for kw in keywords:
            pos = low.rfind(kw)
            if pos == -1:
                continue
            if pos > best_pos or (pos == best_pos and len(kw) > best_len):
                best_pos = pos
                best_len = len(kw)
                best_series = series
    return [best_series] if best_series else []


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


# 多色名分隔符：英文/中文标点 + 空格 + 连词
_COLOR_SPLIT_RE = re.compile(r"[\/、，,&]| and |\s+")


def _split_color_name(name: str) -> list[str]:
    """把多色名拆成子串：'蓝丝带/雪白' → ['蓝丝带','雪白']。"""
    parts = [p.strip() for p in _COLOR_SPLIT_RE.split(name) if p.strip()]
    return parts or [name]


def map_color_to_series_list(color_name: str) -> list[str]:
    """将颜色名映射到色系列表（去重保序）。

    单段颜色名内含多个色相时，以最靠后的色相为准（最后一个字优先）：
      '水蓝灰'   → ['灰色系']          （灰最靠后，非 [蓝色系, 灰色系]）
      '淡灰紫'   → ['紫色系']
    含分隔符的多色名按分隔符拆分后逐段取最靠后色相，再合并去重：
      '白中白/白沙灰' → ['白色系','灰色系']
      '淡灰紫/白中白' → ['紫色系','白色系']
      '迷彩绿'        → ['多色系']（印花关键词命中，整名作图案）
      '正黑色'        → ['黑色系']
    优先查缓存 → 印花检测 → 分词 + 逐段最靠后规则扫描 → 返回空 list。
    """
    name = (color_name or "").strip()
    if not name:
        return []

    # 预处理：去掉 "颜色:" 前缀和 ";尺码:..." 后缀
    if "颜色:" in name:
        name = name.split("颜色:")[1].split(";")[0].strip()
    if not name:
        return []

    # 查缓存（缓存值为 list）
    cache = _load_cache()
    if name in cache:
        return list(cache[name])

    # 印花/多色关键词：整名作图案，返回 ["多色系"]
    if _match_multicolor(name):
        return ["多色系"]

    # 分词 + 全规则扫描
    found: list[str] = []
    seen: set[str] = set()
    for part in _split_color_name(name):
        hits = _match_chinese(part) if _has_chinese(part) else _match_english(part)
        for s in hits:
            if s not in seen:
                seen.add(s)
                found.append(s)
    return found


def map_color_to_series(color_name: str) -> str:
    """将颜色名映射到色系（单值，向后兼容）。

    返回 list 的第一个元素；空 list 返回空串。新代码应直接用 map_color_to_series_list。
    """
    lst = map_color_to_series_list(color_name)
    return lst[0] if lst else ""


def collect_unmatched(color_names: list[str]) -> list[str]:
    """收集规则匹配不上且不在缓存中的颜色名。"""
    unmatched: list[str] = []
    for name in color_names:
        if not name or not name.strip():
            continue
        result = map_color_to_series_list(name)
        if not result:
            unmatched.append(name.strip())
    return sorted(set(unmatched))
