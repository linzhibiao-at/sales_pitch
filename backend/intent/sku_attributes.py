"""SKU 结构化属性提取：从 category_l2 + title 推导正交维度属性。

四个属性维度，每个解决 role + category_l2 无法覆盖的问题：

  layer:       base(内搭) / mid(中间层) / outer(外套) / n/a
    └─ 解决：内搭 vs 外套同属 top，系统推荐两件内搭

  coverage:    upper(上身) / lower(下身) / full(全身) / feet / head / n/a
    └─ 解决：连体裤/连衣裙占满全身，不应再配 top 或 bottoms

  length_class: short(短款) / long(长款) / n/a
    └─ 解决：长袖×短裤/短裙季节冲突（替代 title 关键词补丁）

  is_intimate: true / false
    └─ 解决：内裤/文胸/运动内衣不应参与搭配推荐

提取方法：category_l2 + title 的确定性规则映射，不需要 LLM。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.intent.color_series_mapper import map_color_to_series_list

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# layer（穿着层次）
# ──────────────────────────────────────────────────────────────

_OUTER_CATS: frozenset[str] = frozenset({
    "梭织外套", "梭织薄外套", "梭织厚外套", "外套类",
    "羽绒服", "中长羽绒服", "长羽绒服", "羽绒马甲",
    "单层冲锋衣", "冲锋衣两件套", "梭织马甲", "针织马甲",
    "马甲", "棉马甲", "针织马甲两件套",
    "棉服", "毛呢上衣", "防晒服",
    "滑雪服", "棒球服",
})

_OUTER_TITLE_KEYWORDS: tuple[str, ...] = ("外套", "夹克", "风衣", "大衣", "羽绒服", "棉服", "冲锋衣")

_MID_CATS: frozenset[str] = frozenset({
    "套头卫衣", "连帽卫衣", "毛衣", "编织开衫", "毛织开衫", "编织衫",
    "针织套头衫",
})

_MID_TITLE_KEYWORDS: tuple[str, ...] = ("卫衣", "毛衣", "开衫")

_BASE_CATS: frozenset[str] = frozenset({
    "短袖T", "短袖T恤", "长袖T", "长袖T恤",
    "短袖POLO", "长袖POLO", "编织衫POLO",
    "短袖衬衫", "长袖衬衫",
    "短袖编织衫", "短袖梭织上衣", "短袖针织上衣",
    "梭织上衣", "针织上衣", "针织短袖衫",
    "背心",
    # 简称 / 异名（cat2 用了缩写，title 也无关键词兜底，须显式收录）
    "短T", "短T类", "长T", "针织运动上衣",
    "内搭", "内搭类", "内衣类", "BRA",
    # 泳装（贴身穿着，按内层 base 处理；长袖泳衣等上装 role=top）
    "泳装",
})

_BASE_TITLE_KEYWORDS: tuple[str, ...] = ("T恤", "POLO", "衬衫", "背心")


def extract_layer(category_l2: str, title: str) -> str:
    """提取穿着层次：base / mid / outer / n/a。"""
    cat2 = (category_l2 or "").strip()
    t = title or ""

    if cat2 in _OUTER_CATS or any(kw in t for kw in _OUTER_TITLE_KEYWORDS):
        return "outer"
    if cat2 in _MID_CATS or any(kw in t for kw in _MID_TITLE_KEYWORDS):
        return "mid"
    if cat2 in _BASE_CATS or any(kw in t for kw in _BASE_TITLE_KEYWORDS):
        return "base"
    return "n/a"


# ──────────────────────────────────────────────────────────────
# coverage（身体覆盖范围）
# ──────────────────────────────────────────────────────────────

_FULL_BODY_CATS: frozenset[str] = frozenset({
    "连衣裙", "针织连衣裙", "梭织连衣裙", "两件套连衣裙", "连衣裙两件套",
})

_FULL_BODY_TITLE_KEYWORDS: tuple[str, ...] = ("连体裤", "背带裤", "背带裙", "连衣裙")


def extract_coverage(role: str, category_l2: str, title: str) -> str:
    """提取身体覆盖范围：upper / lower / full / feet / head / n/a。"""
    cat2 = (category_l2 or "").strip()
    t = title or ""
    r = (role or "").strip().lower()

    if cat2 in _FULL_BODY_CATS or any(kw in t for kw in _FULL_BODY_TITLE_KEYWORDS):
        return "full"
    if r == "top":
        return "upper"
    if r == "bottoms":
        return "lower"
    if r == "shoes":
        return "feet"
    if r == "accessory":
        return "head"
    return "n/a"


# ──────────────────────────────────────────────────────────────
# length_class（长度等级）
# ──────────────────────────────────────────────────────────────

# 上装侧：长袖 / 保暖类 → long
# 「服」类（防晒服/滑雪服/棒球服）与外套类按业务定 long：防晒服为长袖防晒，
# 滑雪服/棒球服/外套为保暖/外穿长款。显式枚举数据中实际存在的「服」中类，
# 不用裸子串 '服' in cat2，避免将来某个含「服」的新中类被误判 long。
_LONG_TOP_CATS: frozenset[str] = frozenset({
    "长袖T", "长袖T恤", "长袖POLO", "长袖衬衫",
    "套头卫衣", "连帽卫衣",
    "毛衣", "编织开衫", "毛织开衫",
    "梭织外套", "针织马甲", "梭织马甲",
    "羽绒服", "中长羽绒服", "长羽绒服", "羽绒马甲",
    "单层冲锋衣", "冲锋衣两件套",
    "棉服", "毛呢上衣",
    "防晒服", "滑雪服", "棒球服", "外套类",
})

_LONG_TOP_TITLE_KEYWORDS: tuple[str, ...] = ("长袖", "卫衣", "毛衣", "外套", "羽绒服", "棉服", "冲锋衣", "开衫")

# 上装侧：短袖 / 无袖 → short
# 短T类→short（无袖长信号即短袖）；内衣类/内搭类/内搭 多为背心/短款打底→short。
_SHORT_TOP_CATS: frozenset[str] = frozenset({
    "短袖T", "短袖T恤", "短袖POLO", "短袖衬衫",
    "短袖编织衫", "短袖梭织上衣", "短袖针织上衣",
    "针织短袖衫", "背心",
    "短T类", "内衣类", "内搭类", "内搭",
})

_SHORT_TOP_TITLE_KEYWORDS: tuple[str, ...] = ("短袖", "无袖", "背心")

# 下装侧：短裤 / 五分 / 七分 / 短裙 → short
_SHORT_BOTTOM_CATS: frozenset[str] = frozenset({
    "梭织短裤", "针织短裤",
    "梭织五分裤", "针织五分裤",
    "梭织七分裤", "针织七分裤",
    "平角裤",
})

_SHORT_BOTTOM_TITLE_KEYWORDS: tuple[str, ...] = ("短裤", "五分", "七分裤", "短裙")

# 下装侧：长裤 / 长裙 → long
# 滑雪裤→long：滑雪为冬季长裤，length 作 season 代理时与 long 一致。
_LONG_BOTTOM_CATS: frozenset[str] = frozenset({
    "梭织长裤", "针织长裤", "梭织九分裤",
    "针织打底裤",
    "针织裤",
    "滑雪裤",
})

_LONG_BOTTOM_TITLE_KEYWORDS: tuple[str, ...] = ("长裤", "九分裤", "长裙", "针织裙裤", "针织打底裤")

# ── 用户指定的优先级规则（在上述显式中类/关键词之前判定）──────────────
# top rule1：标题含 长袖/长 → long，短袖/短 → short（标题优先于中类默认）
_TOP_LEN_LONG_KW: tuple[str, ...] = ("长袖", "长")
_TOP_LEN_SHORT_KW: tuple[str, ...] = ("短袖", "短")
# top rule2：泛称上装中类（标题/中类无袖长信号时）默认 long
_GENERIC_LONG_TOP_CATS: frozenset[str] = frozenset({
    "梭织上衣", "针织上衣", "编织衫",
    "梭织两件套", "针织两件套", "针织套头衫",
})
# bottoms rule1：title/中类/小类 含下列词 → short
_BOTTOM_SHORT_KW: tuple[str, ...] = ("短", "五分", "七分", "半裙", "半身裙")
# bottoms rule2：裤裙/背带裙 → short；背带裤/背带装 → long（cat_l2/小类/标题命中均可）
_BOTTOM_SHORT2_KW: tuple[str, ...] = ("裤裙", "背带裙")
_BOTTOM_LONG2_KW: tuple[str, ...] = ("背带裤", "背带装")

# 泳装类（沙滩/泳装域）：length 不作为 season 代理——长款防晒开衫（开衫泳衣）
# × 短款泳裤（短裤泳装）是正确搭配，不应被「长袖上装×短款下装季节冲突」误杀。
# cat2 命中或标题含泳装关键词即归 n/a，退出季节冲突规则。
_SWIM_CATS: frozenset[str] = frozenset({
    "泳装", "连体泳衣", "分体泳衣", "儿童连体泳衣",
    "泳衣", "开衫泳衣", "泳裤",
})
_SWIM_TITLE_KEYWORDS: tuple[str, ...] = ("泳衣", "泳裤", "泳装")


def is_swimwear(category_l2: str, title: str) -> bool:
    """是否为泳装类 garment（沙滩/泳装域）。

    泳装 length 不作 season 代理（避免误杀长款开衫×短款泳裤），且可参与泳-泳搭配
    推荐同伴（不判 is_intimate）。供 ETL 全链路（extract_length_class /
    resolve_length_class VLM 回退 / VLM 抽取脚本过滤）共用，保证泳装 length=n/a
    的不变量在 VLM 回补后仍成立。
    """
    cat2 = (category_l2 or "").strip()
    t = title or ""
    return cat2 in _SWIM_CATS or (bool(t) and any(kw in t for kw in _SWIM_TITLE_KEYWORDS))


def extract_length_class(
    role: str, category_l2: str, title: str, extra_text: str = "", cat_l3: str = "",
) -> str:
    """提取长度等级：short / long / n/a。

    上装侧（优先级从高到低）：
      1. 标题含 长袖/长 → long；短袖/短 → short
      2. 泛称上装中类（梭织上衣/针织上衣/编织衫/梭织两件套/针织两件套/针织套头衫）→ long
      3. 既有显式中类 / 标题关键词兜底
    下装侧（优先级从高到低）：
      1. title/中类/小类 含 短/五分/七分/半裙/半身裙 → short
      2. 裤裙/背带裙 → short；背带裤/背带装 → long
      3. 既有显式中类 / 标题关键词兜底
    泳装类：一律 n/a（length 在沙滩域不作季节代理，避免误杀长款开衫×短款泳裤）
    其余（鞋、配饰、连衣裙等）→ n/a

    extra_text（如 search_keywords）作为 title 之外的兜底文本参与长短关键词扫描，
    但仅作用于上述第 3 步兜底与下装 rule2 之外的关键词判定；用户指定的 rule1
    「title 里有」「title/中类/小类 含」按字面只扫 title(+cat_l2+cat_l3)，不含
    extra_text，避免 search_keywords 里的泛词越权覆盖中类默认。
    cat_l3（小类）参与下装 rule1/rule2 的「中类/小类」扫描。
    """
    cat2 = (category_l2 or "").strip()
    t = title or ""
    et = extra_text or ""
    c3 = (cat_l3 or "").strip()
    r = (role or "").strip().lower()

    def _hit(keywords: tuple[str, ...]) -> bool:
        """title + extra_text（legacy 兜底语义）。"""
        return any(kw in t or kw in et for kw in keywords)

    def _in_title(keywords: tuple[str, ...]) -> bool:
        return any(kw in t for kw in keywords)

    def _in_tcat(keywords: tuple[str, ...]) -> bool:
        """title + 中类(cat_l2) + 小类(cat_l3)。"""
        return any(kw in t or kw in cat2 or kw in c3 for kw in keywords)

    # 泳装类优先：退出 length → season 代理，避免季节冲突规则误杀沙滩搭配
    if is_swimwear(cat2, t):
        return "n/a"

    if r == "top":
        # rule1：标题长短关键词优先
        if _in_title(_TOP_LEN_LONG_KW):
            return "long"
        if _in_title(_TOP_LEN_SHORT_KW):
            return "short"
        # rule2：泛称上装中类默认 long
        if cat2 in _GENERIC_LONG_TOP_CATS:
            return "long"
        # rule3：既有显式中类 / 标题关键词兜底
        if cat2 in _LONG_TOP_CATS or _hit(_LONG_TOP_TITLE_KEYWORDS):
            return "long"
        if cat2 in _SHORT_TOP_CATS or _hit(_SHORT_TOP_TITLE_KEYWORDS):
            return "short"
        return "n/a"

    if r == "bottoms":
        # rule1：title/中类/小类 含 短款信号 → short
        if _in_tcat(_BOTTOM_SHORT_KW):
            return "short"
        # rule2：裤裙/背带裙 → short；背带裤/背带装 → long
        if _in_tcat(_BOTTOM_SHORT2_KW):
            return "short"
        if _in_tcat(_BOTTOM_LONG2_KW):
            return "long"
        # rule3：既有显式中类 / 标题关键词兜底（含 search_keywords）
        if cat2 in _SHORT_BOTTOM_CATS or _hit(_SHORT_BOTTOM_TITLE_KEYWORDS):
            return "short"
        if cat2 in _LONG_BOTTOM_CATS or _hit(_LONG_BOTTOM_TITLE_KEYWORDS):
            return "long"
        return "n/a"

    return "n/a"


# ──────────────────────────────────────────────────────────────
# is_intimate（是否贴身内衣）
# ──────────────────────────────────────────────────────────────

_INTIMATE_CATS: frozenset[str] = frozenset({
    "内裤", "内裤套装", "平角裤", "平角裤2件装",
    "运动内衣", "一阶段文胸", "二阶段文胸", "儿童成长文胸",
    "BRA", "运动BRA",
    # 注：泳裤/泳衣/开衫泳衣 原列于此，但泳装是可外穿的整套（沙滩域），
    # 应参与泳-泳搭配推荐；已移出，仅保留真正贴身内衣。详见 test_swimwear_attributes。
})

# 注：「泳裤」「泳衣」原作 intimate 标题关键词，会使连体泳衣/分体泳衣等被误判
# is_intimate=True 而挡在常驻 is_intimate==false 过滤之外。已移除，泳装不再判 intimate。
_INTIMATE_TITLE_KEYWORDS: tuple[str, ...] = ("内裤", "文胸", "运动内衣", "运动BRA")


def extract_is_intimate(category_l2: str, title: str) -> bool:
    """判断是否为贴身内衣类商品。"""
    cat2 = (category_l2 or "").strip()
    t = title or ""
    if cat2 in _INTIMATE_CATS:
        return True
    if t and any(kw in t for kw in _INTIMATE_TITLE_KEYWORDS):
        return True
    return False


# ──────────────────────────────────────────────────────────────
# scene_domain（场景域：日常 / 专业运动）
# ──────────────────────────────────────────────────────────────

# occasion_tags → scene_domain 映射；sport 类优先于 daily（"生活+健身"→gym）
# 运动侧按项目细分：骑行→cycling、滑雪→ski、高球→golf、网球→tennis、
# 户外→outdoor；泛化「运动/健身/运动场景」→gym（默认训练桶，比中性更安全）。
# 跑步/游泳/篮球一般不出现在 occasion_tags，由文本兜底关键词派生。
_OCCASION_DOMAIN: dict[str, str] = {
    "生活": "daily", "时尚运动": "daily", "日常生活": "daily",
    "优雅生活": "daily", "商务通勤": "daily",
    "运动": "gym", "健身": "gym", "运动场景": "gym",
    "骑行": "cycling", "滑雪": "ski",
    "高球": "golf", "网球": "tennis", "户外": "outdoor",
}

# 上游 occasion_tags 里混入的结构化场景码（236xxx）→ scene_domain 解码表。
# 此前这些码未解码，大量生活类 SKU 误判为中性 ""（如 236016 基础生活 235 条）。
# 显式中性码（如 236013 童凉鞋）映射为 ""，自动豁免冲突。
_OCCASION_CODE_DOMAIN: dict[str, str] = {
    "236001": "gym",      # 场下健身短T
    "236002": "daily",    # 经典商务短T
    "236003": "daily",    # 男士宽松短T（生活）
    "236011": "daily",    # 新年/龘龍生活时尚系列
    "236012": "daily",    # 男小童经典生活
    "236013": "",         # 童凉鞋（中性）
    "236014": "daily",    # 基础POLO（生活）
    "236015": "daily",    # 时尚休闲圆领T
    "236016": "daily",    # 基础针织长裤/生活
    "236018": "outdoor",  # 专业运动防风外套
    "236019": "gym",      # 场下健身针织长裤/背心
    "236020": "golf",     # 高尔夫POLO
    "236021": "tennis",   # 修身POLO/场下网球
    "236022": "ski",      # 滑雪服/雪峰羽绒
    "236023": "cycling",  # FILA CYCLING 骑行服
    "236050": "running",  # 灵动裤防晒凉感
}


def _tag_to_domain(tag: str) -> str | None:
    """occasion_tag（中文场景词或 236xxx 码）→ scene_domain；无映射返回 None。

    中文词优先于码（同一 tag 不会既在词表又在码表，二者互斥）；
    显式中性码返回 ""。
    """
    if tag in _OCCASION_DOMAIN:
        return _OCCASION_DOMAIN[tag]
    if tag in _OCCASION_CODE_DOMAIN:
        return _OCCASION_CODE_DOMAIN[tag]
    return None


# 鞋类按 category_l2 细化（休闲鞋/板鞋/老爹鞋/帆布鞋/凉鞋/拖鞋/潮鞋/儿童鞋 → ""）
_SHOE_CAT2_DOMAIN: dict[str, str] = {
    "高尔夫鞋": "golf", "网球鞋": "tennis", "跑鞋": "running",
    "训练鞋": "gym", "户外鞋": "outdoor",
    "骑行鞋": "cycling", "篮球鞋": "basketball",
}

# 服装 category_l2 对 sport 域的 definitive 映射（优先于 occasion_tags，
# 避免 FILA 产品线标签如「健身」盖过功能型品类如「连体泳衣」：连体泳衣
# 虽属 FITNESS 线，但本质是泳装，应进 swim 域而非 gym）。
_GARMENT_CAT2_DOMAIN: dict[str, str] = {
    "连体泳衣": "swim", "分体泳衣": "swim", "泳装": "swim", "泳裤": "swim",
    "儿童连体泳衣": "swim",
    "滑雪服": "ski", "滑雪裤": "ski",
}

# 配件/装备等中性品类强制中性（不参与 scene_domain 冲突）
_NEUTRAL_L1: frozenset[str] = frozenset({"配件", "装备", "礼品", "广宣用品", "其它"})
_SPORT_DOMAINS: frozenset[str] = frozenset({
    "golf", "tennis", "gym", "running", "outdoor", "ski", "swim", "cycling", "basketball",
})

# ──────────────────────────────────────────────────────────────
# 文本兜底：occasion_tags 缺失时，从 title / search_keywords /
# functional_tag / applicable_scenario 等文本推断 scene_domain。
# 与 _OCCASION_DOMAIN 保持同义：sport 类优先于 daily。
# 仅收录高置信关键词，避免把"休闲鞋/板鞋"等中性款误判为 daily。
# ──────────────────────────────────────────────────────────────

# sport 域关键词（按优先级排序：golf > tennis > ski > swim > cycling >
# basketball > outdoor > gym > running）。先匹配的项目域优先，
# 避免泛化「运动」关键词把项目专用款（骑行/滑雪/泳装）吞进 gym。
_SPORT_TEXT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("golf",       ("高尔夫", "高球", "golf")),
    ("tennis",     ("网球", "tennis")),
    ("ski",        ("滑雪", "雪服", "雪峰", "ski")),
    ("swim",       ("游泳", "泳", "沙滩", "swim", "surf")),
    ("cycling",    ("骑行", "CYCLING", "自行车")),
    ("basketball", ("篮球", "basketball")),
    ("outdoor",    ("户外", "徒步", "登山", "露营", "冲锋衣", "抓绒",
                    "防晒服", "防风", "outdoor")),
    ("gym",        ("健身", "训练", "场下健身", "瑜伽", "运动内衣",
                    "运动BRA", "紧身", "速干", "吸汗", "运动裤", "运动裙",
                    "运动")),
    ("running",    ("跑步", "跑鞋", "灵动裤", "running")),
)

# daily 域关键词（社交/通勤/度假等明确日常场景）
_DAILY_TEXT_KEYWORDS: tuple[str, ...] = (
    "通勤", "商务", "办公", "上班", "约会", "聚会", "派对",
    "度假", "旅行", "旅游", "出差", "晚宴", "宴会", "婚礼",
)

# 项目专用 sport 关键词（网球/高尔夫/篮球/骑行/滑雪/泳）。这些是功能型、
# 无歧义的项目，标题或 series/sub_series 出现即认定该域，优先于 occasion_tags
# 的品牌线噪声（236xxx 码如 236001 会跨运动复用，把网球POLO误判 gym）。
# gym/running/outdoor 关键词较泛化（「运动」也出现在「时尚运动」daily 项里），
# 不进此组，保留 occasion→text 兜底顺序，避免 daily 误归 gym。
_SPECIFIC_SPORT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("golf",       ("高尔夫", "高球", "golf")),
    ("tennis",     ("网球", "tennis")),
    ("ski",        ("滑雪", "雪服", "雪峰", "ski")),
    ("swim",       ("游泳", "泳", "沙滩", "swim", "surf")),
    ("cycling",    ("骑行", "CYCLING", "自行车")),
    ("basketball", ("篮球", "basketball")),
)

# athleisure 日常关键词：潮流运动/时尚运动/运动休闲/时尚休闲 不是专业运动，
# 归 daily（FILA FUSION 潮流运动线、LIFESTYLE 时尚休闲线均为日常时尚款）。
# 优先于泛化「运动」gym 关键词，避免把 athleisure T恤/连衣裙/羽绒服误判 gym、
# 进而与专业骑行裤/滑雪裤等跨场景误搭（daily×sport 冲突可挡）。
_ATHLEISURE_DAILY_KEYWORDS: tuple[str, ...] = (
    "时尚休闲", "运动休闲", "时尚运动", "潮流运动", "休闲运动",
)

# 功能性 sport 关键词 = _SPORT_TEXT_KEYWORDS 去掉泛化「运动」「运动场景」。
# athleisure 信号须让位给这些真功能型词（防晒服/冲锋衣/健身/训练/紧身/速干/
# 网球/滑雪…），否则「潮流运动防晒服」会被误归 daily 而非 outdoor。
_FUNCTIONAL_SPORT_KEYWORDS: frozenset[str] = frozenset(
    kw.lower()
    for _d, kws in _SPORT_TEXT_KEYWORDS
    for kw in kws
    if kw not in ("运动", "运动场景")
)


def _blob_has_athleisure(*texts: str) -> bool:
    """文本是否含 athleisure 日常信号（时尚休闲/运动休闲/…）。大小写不敏感。"""
    blob = "".join(str(t or "") for t in texts).lower()
    return any(kw and kw.lower() in blob for kw in _ATHLEISURE_DAILY_KEYWORDS)


def _blob_has_functional_sport(*texts: str) -> bool:
    """文本是否含功能性 sport 关键词（防晒服/健身/网球/…），athleisure 须让位。"""
    blob = "".join(str(t or "") for t in texts).lower()
    return any(kw in blob for kw in _FUNCTIONAL_SPORT_KEYWORDS)


def _infer_specific_sport(*texts: str) -> str:
    """扫描项目专用 sport 关键词；无命中返回 ""。优先级 golf>tennis>ski>swim>cycling>basketball。

    大小写不敏感（series/sub_series 常为大写英文如 TENNIS/BASKETBALL）。
    """
    blob = "".join(str(t or "") for t in texts).lower()
    if not blob.strip():
        return ""
    for _domain, keywords in _SPECIFIC_SPORT_KEYWORDS:
        for kw in keywords:
            if kw and kw.lower() in blob:
                return _domain
    return ""


def _infer_domain_from_text(*texts: str) -> str:
    """从若干文本字段扫描关键词推断 scene_domain；无命中返回 ""。

    sport 优先于 daily；多 sport 关键字按 _SPORT_TEXT_KEYWORDS 顺序取首个。
    大小写不敏感（series/sub_series 常为大写英文）。
    """
    blob = "".join(str(t or "") for t in texts).lower()
    if not blob.strip():
        return ""
    # 1) sport 域优先
    for _domain, keywords in _SPORT_TEXT_KEYWORDS:
        for kw in keywords:
            if kw and kw.lower() in blob:
                return _domain
    # 2) daily 域
    for kw in _DAILY_TEXT_KEYWORDS:
        if kw in blob:
            return "daily"
    return ""


def _parse_occasion_tags(occasion_tags: Any) -> list[str]:
    """把 occasion_tags 字段（可能是 list 或逗号分隔字符串）归一为 list[str]。"""
    if not occasion_tags:
        return []
    if isinstance(occasion_tags, (list, tuple, set)):
        return [str(t).strip() for t in occasion_tags if str(t).strip()]
    s = str(occasion_tags).strip()
    if not s:
        return []
    # 兼容 "生活,健身" / "生活/健身" / "生活|健身" 分隔符
    for sep in (",", "/", "|", "、", ";", "；"):
        if sep in s:
            return [t.strip() for t in s.split(sep) if t.strip()]
    return [s]


def extract_scene_domain(
    category_l1: str,
    category_l2: str,
    role: str,
    occasion_tags: Any,
    title: str = "",
    extra_text: str = "",
    series: str = "",
    sub_series: str = "",
) -> str:
    """提取场景域：daily / golf / tennis / gym / running / outdoor / ski /
    swim / cycling / basketball / ""(中性)。

    运动侧按项目细分，跨项目互斥（由 outfit_conflict 的 scene_allow 有向表驱动）。
    信号优先级（高→低）：
      1) 雪具 L1 → ski（双板雪鞋/雪杖/雪镜等全套滑雪硬装备）
      2) 配件/非服装 L1 → 中性（role 把关：装备 L1 里的 garment 放行；
         项目专用 sport 命中的配饰 GOLF手套/网球头带 等放行归对应 sport 域）
      3) 鞋类 cat2 / 服装 cat2 definitive 映射（连体泳衣/滑雪服 等）
      4) 项目专用 sport 关键词（标题 / series / sub_series）——优先于 occasion
         噪声：occasion 的 236xxx 码是品牌线码会跨运动复用（如 236001 同时用于
         健身T 与 网球POLO），故网球POLO 须由标题「网球」/ sub_series「TENNIS」定域
      5) occasion_tags sport（干净中文词 高球/网球/… + 码）
      6) occasion_tags daily（生活/商务通勤/…）
      6.5) athleisure 日常：garment 含「时尚休闲/运动休闲/时尚运动/潮流运动」→ daily
        （非专业运动；让位给功能性 sport 关键词如防晒服/健身/网球）
      7) 文本兜底（gym/running/outdoor 泛化关键词 + daily 词）
      8) 服装/鞋无任何场景信号 → daily（原中性 "" 默认归日常；配件仍返回 ""）
    """
    cat1 = (category_l1 or "").strip()
    cat2 = (category_l2 or "").strip()
    r = (role or "").strip().lower()

    # 1) 雪具 L1 → ski：双板雪鞋/雪杖/雪镜/头盔/雪板等全套滑雪硬装备。
    #    雪鞋 role=shoes 但 cat1≠鞋类，漏过 _SHOE_CAT2_DOMAIN；title「ATOMIC 双板…」
    #    不含 ski 关键词（滑雪/雪服/雪峰/ski），文本兜底也漏。整 L1 直接定 ski。
    if cat1 == "雪具":
        return "ski"

    # 2) 配件/中性品类：role=accessory 或非服装类 L1（礼品/广宣等）→ 中性。
    #    装备 L1 含少量服装（连体泳衣/泳裤等 garment role），需放行到场景检测，
    #    否则全身泳装会被误判中性、与滑雪服等跨营误搭。
    #    但 title/series 命中项目专用 sport（高尔夫/网球/滑雪/泳/骑行/篮球）的
    #    配饰放行：GOLF手套/网球头带/高尔夫腰带 等功能型配饰应归对应 sport 域，
    #    而非一刀切中性。「潮流运动/场下健身」等泛化运动线配饰仍保持中性
    #    （时尚运动款，非功能 gym，误染 gym 会污染训练营）。
    if r == "accessory" or (cat1 in _NEUTRAL_L1 and r not in ("top", "bottoms", "dress")):
        spec = _infer_specific_sport(cat2, title, extra_text, series, sub_series)
        if spec:
            return spec
        return ""

    # 3) 鞋类细化：cat_l2 命中直接返回；未命中下放到文本兜底（如"跑步鞋"标题）
    if cat1 == "鞋类" or r == "shoes":
        if cat2 in _SHOE_CAT2_DOMAIN:
            return _SHOE_CAT2_DOMAIN[cat2]

    # 3.5) 服装 cat2 definitive 映射（优先于 occasion_tags 品牌线噪声）
    if cat2 in _GARMENT_CAT2_DOMAIN:
        return _GARMENT_CAT2_DOMAIN[cat2]

    # 4) 项目专用 sport（标题 / series / sub_series）优先于 occasion 噪声
    spec = _infer_specific_sport(cat2, title, extra_text, series, sub_series)
    if spec:
        return spec

    # 5) occasion_tags sport
    tags = _parse_occasion_tags(occasion_tags)
    # 5a) 干净中文场景词（健身/骑行/滑雪/高球/网球/户外/运动…）——可靠信号，
    #     直接返回 sport。仅 sport 域返回；daily 词（生活/时尚运动…）下放到 6。
    for tag in tags:
        if tag in _OCCASION_DOMAIN and _OCCASION_DOMAIN[tag] in _SPORT_DOMAINS:
            return _OCCASION_DOMAIN[tag]
    # 5b) athleisure 日常优先于 236xxx 品牌线码：236xxx 码跨运动复用且常被错配
    #     （如 236050「灵动裤防晒凉感」被贴到「时尚休闲短袖POLO」→误判 running），
    #     故 garment 含 athleisure 信号（时尚休闲/运动休闲/时尚运动/潮流运动）且
    #     无功能性 sport 关键词时归 daily，不被 236xxx 码盖过。仅覆盖码、不覆盖
    #     5a 的中文 sport 词；项目专用 sport(4) 已在上一步命中返回。
    if r in ("top", "bottoms", "dress") and _blob_has_athleisure(
        cat2, title, extra_text, series, sub_series,
    ) and not _blob_has_functional_sport(cat2, title, extra_text, series, sub_series):
        return "daily"
    # 5c) 236xxx 结构化场景码 → sport（被 5b athleisure 让位后仍未命中才到此）
    for tag in tags:
        if tag in _OCCASION_CODE_DOMAIN and _OCCASION_CODE_DOMAIN[tag] in _SPORT_DOMAINS:
            return _OCCASION_CODE_DOMAIN[tag]

    # 6) occasion_tags daily
    for tag in tags:
        if _tag_to_domain(tag) == "daily":
            return "daily"

    # 6.5) athleisure 日常：garment 角色（top/bottoms/dress）含「时尚休闲/运动休闲/
    #      时尚运动/潮流运动」→ daily。athleisure 非专业运动，归日常，避免潮流运动T恤/
    #      时尚休闲羽绒服等与专业骑行裤/滑雪裤跨场景误搭。
    #      让位给功能性 sport 关键词（防晒服/冲锋衣/健身/紧身/网球…）——真功能型，
    #      athleisure 不能覆盖；配饰/鞋不染（保持中性跨场景复用）。
    #      前置 specific sport(4) 与 occasion(5/6) 已命中返回，此处不会覆盖它们。
    if r in ("top", "bottoms", "dress") and _blob_has_athleisure(
        cat2, title, extra_text, series, sub_series,
    ) and not _blob_has_functional_sport(cat2, title, extra_text, series, sub_series):
        return "daily"

    # 7) 文本兜底：gym/running/outdoor 泛化关键词 + daily 词
    #    纳入 cat2：虚拟图锚点 title 为空时，仍可由中类「冲锋衣」等关键词派生 outdoor。
    d = _infer_domain_from_text(cat2, title, extra_text, series, sub_series)
    if d:
        return d

    # 8) 无任何场景信号的服装/鞋 → daily（原中性 "" 默认归日常）。
    #    配件已在步骤 2 返回 ""（跨场景复用，由 pre-filter allow 集放行 "" 兜底）。
    return "daily"


# ──────────────────────────────────────────────────────────────
# 字段枚举与归一（供意图模块 LLM 输出校验）
# ──────────────────────────────────────────────────────────────

LENGTH_CLASS_VALUES: frozenset[str] = frozenset({"short", "long", "n/a"})
COVERAGE_VALUES: frozenset[str] = frozenset({"upper", "lower", "full", "feet", "head", "n/a"})
SCENE_DOMAIN_VALUES: frozenset[str] = frozenset({
    "", "daily", "golf", "tennis", "gym", "running", "outdoor",
    "ski", "swim", "cycling", "basketball",
})
# 版型（产品款型）：源 product_master_ext.modeling 列枚举（约 30% 为空）。
# ETL build_sku_record 落盘时 normalize_modeling 校验；意图侧 target_slots 同源校验。
MODELING_VALUES: frozenset[str] = frozenset({
    "宽松", "基础", "舒适", "修身", "紧身", "超宽松", "ACTIVE",
})
# 基础色系（intent_extract.md 第十三节枚举），用于校验 target_slots/negative_slots 的 color_series 取值
COLOR_SERIES_BASE_VALUES: frozenset[str] = frozenset({
    "黑色系", "白色系", "灰色系", "红色系", "粉色系", "橙色系",
    "黄色系", "绿色系", "蓝色系", "紫色系", "棕色系", "米色系",
})

_ATTR_ENUMS: dict[str, frozenset[str]] = {
    "length_class": LENGTH_CLASS_VALUES,
    "coverage": COVERAGE_VALUES,
    "scene_domain": SCENE_DOMAIN_VALUES,
    "color_series": COLOR_SERIES_BASE_VALUES,
    "modeling": MODELING_VALUES,
}
# 非法/缺失时退回的中性默认值
_ATTR_NEUTRAL: dict[str, str] = {
    "length_class": "n/a",
    "coverage": "n/a",
    "scene_domain": "",
    "color_series": "",
    "modeling": "",
}


def normalize_attr_enum(key: str, raw: Any) -> str:
    """把 LLM/外部输入归一到字段枚举；非法或缺失 → 该字段中性默认。

    镜像 ``normalize_gender`` / ``_normalize_categories`` 的「枚举校验 + 降级」模式，
    确保 length_class/coverage/scene_domain 取值始终限定在现有字典内。
    """
    valid = _ATTR_ENUMS.get(key)
    if valid is None:
        return str(raw or "").strip()
    s = str(raw or "").strip()
    return s if s in valid else _ATTR_NEUTRAL[key]


# ──────────────────────────────────────────────────────────────
# 版型（modeling）归一与同义词归并
# ──────────────────────────────────────────────────────────────

def normalize_modeling(raw: Any) -> str:
    """归一 product_master_ext.modeling 原值到枚举；非法/空 → ""。

    供 ETL ``build_sku_record`` 落盘与意图侧共用，单一真相源。
    源 modeling 是结构化枚举列，不做 title 兜底（标题里「宽松版型」是自由文本，
    混入会污染枚举域）。
    """
    return normalize_attr_enum("modeling", raw)


# 用户口语版型词 → 命中的源枚举值集合（同义词归并）。
# 「宽松」含「超宽松」、「修身」含「紧身」：用户说宽松时超宽松款也应召回，
# 说修身时紧身款也应召回。空值 SKU 不在此表，有约束时由 terms/in 过滤自然排除。
MODELING_SYNONYMS: dict[str, list[str]] = {
    "宽松": ["宽松", "超宽松"],
    "超宽松": ["超宽松"],
    "修身": ["修身", "紧身"],
    "紧身": ["紧身"],
    "舒适": ["舒适"],
    "基础": ["基础"],
    "ACTIVE": ["ACTIVE"],
}


def expand_modeling(value: str) -> list[str]:
    """用户版型词 → 命中的源枚举值集合（同义词归并）。

    非枚举值返回空列表（调用方据此跳过过滤，避免误杀全量）。
    """
    s = str(value or "").strip()
    if not s:
        return []
    if s in MODELING_SYNONYMS:
        return MODELING_SYNONYMS[s]
    # 兜底：枚举内但未列入同义词表（如未来新增值）→ 精确自身
    return [s] if s in MODELING_VALUES else []


# ──────────────────────────────────────────────────────────────
# 统一提取入口
# ──────────────────────────────────────────────────────────────

def enrich_sku_attributes(sku: dict[str, Any]) -> dict[str, Any]:
    """为单个 SKU dict 补充 layer / coverage / length_class / is_intimate / scene_domain 字段。

    原地修改并返回。如果 SKU 已有这些字段则跳过（不覆盖）。
    """
    if "layer" not in sku:
        sku["layer"] = extract_layer(
            sku.get("category_l2") or "",
            sku.get("title") or "",
        )
    if "coverage" not in sku:
        sku["coverage"] = extract_coverage(
            sku.get("role") or "",
            sku.get("category_l2") or "",
            sku.get("title") or "",
        )
    if "length_class" not in sku:
        sku["length_class"] = extract_length_class(
            sku.get("role") or "",
            sku.get("category_l2") or "",
            sku.get("title") or "",
        )
    if "is_intimate" not in sku:
        sku["is_intimate"] = extract_is_intimate(
            sku.get("category_l2") or "",
            sku.get("title") or "",
        )
    if "scene_domain" not in sku:
        sku["scene_domain"] = extract_scene_domain(
            sku.get("category_l1") or "",
            sku.get("category_l2") or "",
            sku.get("role") or "",
            sku.get("occasion_tags") or "",
            sku.get("title") or "",
            sku.get("search_keywords") or "",
            sku.get("series") or "",
            sku.get("sub_series") or "",
        )
    # color_series 不由 build_sku_record 落盘时为空（旧集合/增量未重跑 build_catalog），
    # 此处从 color_name/attr_name 现场派生，与 ES 构建同源。用 not get() 而非 not in，
    # 以同时覆盖「键缺失」与「键存在但为空串」两种旧数据形态。
    if not sku.get("color_series"):
        sku["color_series"] = map_color_to_series_list(
            str(sku.get("attr_name") or sku.get("color_name") or ""),
        )
    return sku


def get_attr(sku: Optional[dict[str, Any]], key: str, default: str = "") -> str:
    """安全读取 SKU 属性，缺失时实时推导（兼容未 enriched 的 SKU）。"""
    if not sku:
        return default
    val = sku.get(key)
    if val is not None:
        return str(val)
    # 实时推导（兼容旧数据）
    if key == "layer":
        return extract_layer(sku.get("category_l2") or "", sku.get("title") or "")
    if key == "coverage":
        return extract_coverage(sku.get("role") or "", sku.get("category_l2") or "", sku.get("title") or "")
    if key == "length_class":
        return extract_length_class(sku.get("role") or "", sku.get("category_l2") or "", sku.get("title") or "")
    if key == "is_intimate":
        return str(extract_is_intimate(sku.get("category_l2") or "", sku.get("title") or ""))
    if key == "scene_domain":
        return extract_scene_domain(
            sku.get("category_l1") or "",
            sku.get("category_l2") or "",
            sku.get("role") or "",
            sku.get("occasion_tags") or "",
            sku.get("title") or "",
            sku.get("search_keywords") or "",
            sku.get("series") or "",
            sku.get("sub_series") or "",
        )
    if key == "color_series":
        return map_color_to_series_list(
            str(sku.get("attr_name") or sku.get("color_name") or ""),
        )
    return default


# ──────────────────────────────────────────────────────────────
# series（子品牌线/联名胶囊）归一
# ──────────────────────────────────────────────────────────────

from functools import lru_cache
from pathlib import Path

_SKUS_JSONL = (
    Path(__file__).resolve().parents[2] / "data" / "processed" / "skus.jsonl"
)


@lru_cache(maxsize=1)
def load_known_series() -> frozenset[str]:
    """从 skus.jsonl 加载去重后的合法 series 值集合（开放枚举的真相源）。

    用于意图侧 normalize_series 校验 LLM 输出的 series，避免非规范值
    （如 ``FILA FUSION`` 漏写 ``LIFE``）下推到 ``series == "..."`` 致 0 召回。
    文件缺失/读取失败时返回空集——normalize_series 据此 fail-open（仅 strip），
    不阻断意图提取（生产环境 skus.jsonl 存在则严格校验）。
    """
    if not _SKUS_JSONL.is_file():
        return frozenset()
    import json

    out: set[str] = set()
    try:
        with _SKUS_JSONL.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = str(json.loads(line).get("series") or "").strip()
                except (ValueError, TypeError):
                    continue
                if s:
                    out.add(s)
    except Exception:
        logger.warning("failed to load known series from %s", _SKUS_JSONL)
        return frozenset()
    return frozenset(out)


def normalize_series(raw: Any) -> str:
    """归一 LLM/外部输入的 series：strip + 折叠空白；已知集合非空时校验，非法→空。

    与 ``normalize_attr_enum`` 的「枚举校验 + 降级」模式一致，但 series 是开放枚举
    （来自数据），合法集由 ``load_known_series`` 提供。已知集为空（无数据文件）时
    fail-open 仅返回 strip 后的值，不阻断。
    """
    s = " ".join(str(raw or "").strip().split())
    if not s:
        return ""
    # 容错 LLM 常见后缀：「HERITAGE系列」「FILA X NEMEN 联名」→ 剥离后逐字匹配白名单。
    for suf in ("系列", "联名"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    if not s:
        return ""
    known = load_known_series()
    if not known:
        return s
    return s if s in known else ""


# 系列「系列/联名/的」后缀或紧跟 CJK 品类词时，认定是显式系列信号——
# 用于过滤误报：裸英文词命中易把无关词当系列，需系列后缀/品类词锚定。
_SERIES_SUFFIX_TOKENS = ("系列", "联名", "的")


def _has_series_signal(text: str, start: int, end: int) -> bool:
    """canonical series 在 text[start:end] 命中后，判断后续字符是否为显式系列信号。

    允许：可选空白 + 系列/联名/的；或紧跟一个 CJK 字符（如「HERITAGE裤子」）。
    """
    j = end
    while j < len(text) and text[j].isspace():
        j += 1
    if j < len(text):
        for tok in _SERIES_SUFFIX_TOKENS:
            if text.startswith(tok, j):
                return True
        # 紧跟 CJK 字符（产品品类词，如 裤/鞋/裙/外套）
        if "一" <= text[j] <= "鿿":
            return True
    return False


def extract_series_from_text(text: str) -> str:
    """从自由文本里扫描命中的 canonical series（确定性规则回填，LLM 漏抽时的安全网）。

    遍历 ``load_known_series`` 白名单，按值长度降序匹配（保证 ``MODERN HERITAGE``
    优先于 ``HERITAGE``、``FILA FUSION LIFE`` 优先于其子串）。命中后还需后续字符
    为显式系列信号（系列/联名/的 或 CJK 品类词）才认定，降低误报致 0 召回的风险。

    返回首个（最长）命中的 canonical 值；无命中返回空串。白名单空时 fail-open 返回空。
    """
    if not text:
        return ""
    known = load_known_series()
    if not known:
        return ""
    low = text.lower()
    # 长度降序：先长后短，避免子系列盗匹配（HERITAGE 抢 MODERN HERITAGE）。
    for s in sorted(known, key=len, reverse=True):
        if not s:
            continue
        needle = s.lower()
        idx = low.find(needle)
        if idx == -1:
            continue
        # 在原始 text 上判定后续信号（保留原大小写无关，只看后续字符类别）。
        if _has_series_signal(text, idx, idx + len(s)):
            return s
    return ""


