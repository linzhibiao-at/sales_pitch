"""scene_domain 场景域冲突治理单元测试。

覆盖：
  - extract_scene_domain：occasion_tags → domain 映射，含多值/sport 优先/配件豁免/
    鞋类细化/236xxx 码解码
  - build_scene_domain_milvus_expr：有向允许表 scene_allow 驱动正向允许集
    （daily→仅 daily+中性；sport→仅 allow 集+中性；中性→None）
  - build_attr_es_filter：ES must_not 镜像 Milvus expr（4 条结构化规则，
    scene_domain 不在此处）
  - build_scene_domain_es_filter：ES 正向隔离 filter（镜像 Milvus expr，
    只放行 allow 集+中性）
  - check_companion_conflict：仅同域可搭、跨 sport 互斥、daily×sport 双向拒绝、
    中性豁免；有向非对称语义见 TestSceneAllowDirected
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.intent.sku_attributes import extract_scene_domain
from backend.intent.role_slots import role_has_explicit_positive
from backend.models import UserIntent
from backend.ranking.outfit_conflict import (
    build_attr_es_filter,
    build_attr_milvus_expr,
    build_scene_domain_es_filter,
    build_scene_domain_milvus_expr,
    check_companion_conflict,
    check_outfit_conflict,
)


class TestExtractSceneDomain(unittest.TestCase):
    def test_daily_occasion(self):
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["生活"]), "daily"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖POLO", "top", ["商务通勤"]), "daily"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["时尚运动"]), "daily"
        )

    def test_sport_occasion_split(self):
        """运动侧按项目细分：健身→gym、运动→gym、骑行→cycling、滑雪→ski。"""
        self.assertEqual(
            extract_scene_domain("服装", "运动内衣", "top", ["健身"]), "gym"
        )
        self.assertEqual(
            extract_scene_domain("服装", "紧身裤", "bottoms", ["运动"]), "gym"
        )
        self.assertEqual(
            extract_scene_domain("服装", "骑行裤", "bottoms", ["骑行"]), "cycling"
        )
        self.assertEqual(
            extract_scene_domain("服装", "滑雪服", "top", ["滑雪"]), "ski"
        )

    def test_golf_tennis_outdoor(self):
        self.assertEqual(
            extract_scene_domain("服装", "短袖POLO", "top", ["高球"]), "golf"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["网球"]), "tennis"
        )
        self.assertEqual(
            extract_scene_domain("服装", "冲锋衣", "top", ["户外"]), "outdoor"
        )

    def test_occasion_code_decoding(self):
        """236xxx 上游场景码解码（修复此前掉中性 "" 的泄漏）。"""
        # 236016 基础生活 → daily（此前误判中性）
        self.assertEqual(
            extract_scene_domain("服装", "针织长裤", "bottoms", ["236016"]), "daily"
        )
        # 236019 场下健身 → gym
        self.assertEqual(
            extract_scene_domain("服装", "针织长裤", "bottoms", ["236019"]), "gym"
        )
        # 236023 骑行 → cycling
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["236023"]), "cycling"
        )
        # 236022 滑雪 → ski
        self.assertEqual(
            extract_scene_domain("服装", "滑雪服", "top", ["236022"]), "ski"
        )
        # 236020 高尔夫 → golf
        self.assertEqual(
            extract_scene_domain("服装", "短袖POLO", "top", ["236020"]), "golf"
        )
        # 236013 童凉鞋 → 服装/鞋无信号兜底 daily（配件才保持中性）
        self.assertEqual(
            extract_scene_domain("鞋类", "凉鞋", "shoes", ["236013"]), "daily"
        )

    def test_sport_priority_over_daily(self):
        """多值 occasion_tags 含 sport 类 → sport 域优先于 daily。"""
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["生活", "健身"]), "gym"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖POLO", "top", ["日常生活", "高球"]), "golf"
        )

    def test_accessory_exempt(self):
        """配件类（category_l1 ∈ _NEUTRAL_L1 或 role=accessory）→ 中性 ""。"""
        self.assertEqual(
            extract_scene_domain("配件", "包", "accessory", ["高球"]), ""
        )
        self.assertEqual(
            extract_scene_domain("装备", "帽子", "accessory", ["健身"]), ""
        )

    def test_shoes_refinement(self):
        """鞋类按 category_l2 精确映射；未命中的休闲鞋/板鞋 → daily（无信号兜底）。"""
        self.assertEqual(
            extract_scene_domain("鞋类", "高尔夫鞋", "shoes", []), "golf"
        )
        self.assertEqual(
            extract_scene_domain("鞋类", "网球鞋", "shoes", []), "tennis"
        )
        self.assertEqual(
            extract_scene_domain("鞋类", "跑鞋", "shoes", []), "running"
        )
        self.assertEqual(
            extract_scene_domain("鞋类", "训练鞋", "shoes", []), "gym"
        )
        self.assertEqual(
            extract_scene_domain("鞋类", "户外鞋", "shoes", []), "outdoor"
        )
        self.assertEqual(
            extract_scene_domain("鞋类", "休闲鞋", "shoes", []), "daily"
        )
        self.assertEqual(
            extract_scene_domain("鞋类", "板鞋", "shoes", []), "daily"
        )

    def test_text_fallback_sport_split(self):
        """occasion_tags 缺失时，文本兜底按项目关键词派生。"""
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [], "FILA CYCLING男子专业运动干爽短袖T恤"), "cycling"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [], "男子场下跑步梭织短裤"), "running"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [], "男士泳裤吸湿速干"), "swim"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [], "男子场下健身背心"), "gym"
        )

    def test_garment_cat2_overrides_brand_line_tag(self):
        """cat2 是功能型品类时，优先于 occasion_tags 的产品线标签：
        连体泳衣虽属 FITNESS 线（occasion=健身），仍应进 swim 而非 gym。"""
        self.assertEqual(
            extract_scene_domain("装备", "连体泳衣", "dress", ["健身"]), "swim"
        )
        self.assertEqual(
            extract_scene_domain("服装", "滑雪服", "top", ["健身"]), "ski"
        )

    def test_specific_sport_overrides_occasion_brand_code(self):
        """项目专用 sport 关键词（标题/series/sub_series）优先于 occasion 的
        236xxx 品牌线码：网球POLO(occasion=236001 健身份证) 须由标题「网球」/
        sub_series「TENNIS」定域为 tennis，不被误判 gym。"""
        # 标题含「网球」+ occasion 健身份证
        self.assertEqual(
            extract_scene_domain("服装", "短袖POLO", "top", ["236001"],
                                 "男子场下网球短袖POLO"), "tennis"
        )
        # 仅 sub_series=TENNIS（标题无项目词）也能定域
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["236001"],
                                 "男子潮流运动基础简约短袖T裢",
                                 series="INLINE", sub_series="BASKETBALL"), "basketball"
        )
        # sub_series=TENNIS 标题无项目词 → tennis
        self.assertEqual(
            extract_scene_domain("服装", "短袖POLO", "top", ["236001"],
                                 "男子基础短袖POLO",
                                 series="ATHLETICS", sub_series="TENNIS"), "tennis"
        )

    def test_fashion_sport_not_regressed_to_gym(self):
        """时尚运动 occasion 项保持 daily，不因标题含泛化「运动」被误判 gym。"""
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["时尚运动"],
                                 "女子时尚运动短袖T恤"), "daily"
        )

    def test_athleisure_overrides_brand_code_mis_tag(self):
        """athleisure（时尚休闲…）优先于 236xxx 品牌线码：236050「灵动裤防晒凉感」
        被错配到「时尚休闲短袖POLO」时，须由 title 的 athleisure 信号归 daily，
        而非误判 running。复现 F11M523108FNV。"""
        self.assertEqual(
            extract_scene_domain("服装", "短T类", "top", ["236050"],
                                 "男子时尚休闲短袖POLO",
                                 extra_text="FILAWHITE男士基础短袖POLO-F11M523108F",
                                 series="WHITE", sub_series="F.C."), "daily"
        )
        # 真跑步裤（灵动裤，无 athleisure 词）→ 仍 running（码不被覆盖）
        self.assertEqual(
            extract_scene_domain("服装", "针织长裤", "bottoms", ["236050"],
                                 "FILA灵动裤男子防晒凉感直口长裤"), "running"
        )
        # 中文 occasion sport 词（健身）不被 athleisure 覆盖——仍 gym
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["健身"],
                                 "男子时尚休闲健身短袖T恤"), "gym"
        )

    def test_equipment_garment_not_neutralized(self):
        """装备 L1 里的服装（garment role）不放行到中性，落场景检测；
        非服装装备（保温杯 role=accessory/unknown）仍中性。"""
        # 连体泳衣 role=dress → 落 swim（不被 _NEUTRAL_L1 强制中性）
        self.assertEqual(
            extract_scene_domain("装备", "连体泳衣", "dress", ["健身"]), "swim"
        )
        # role=accessory 的非运动装备 → 中性
        self.assertEqual(
            extract_scene_domain("装备", "保温杯", "accessory", ["健身"]), ""
        )

    def test_snow_l1_to_ski(self):
        """雪具 L1 整体定 ski：双板雪鞋 role=shoes 但 cat1≠鞋类漏过鞋类细化，
        title「ATOMIC 双板…」不含 ski 关键词也漏过文本兜底，故按 L1 兜底。"""
        self.assertEqual(
            extract_scene_domain("雪具", "双板雪鞋", "shoes", []), "ski"
        )
        self.assertEqual(
            extract_scene_domain("雪具", "雪杖", "unknown", []), "ski"
        )
        self.assertEqual(
            extract_scene_domain("雪具", "双板", "unknown",
                                 "ATOMIC 双板 REDSTER S9 FIS M"), "ski"
        )

    def test_sport_specific_accessory_not_neutralized(self):
        """title/cat2 命中项目专用 sport 的配饰放行归对应 sport 域，
        而非一刀切中性（GOLF手套/网球头带/泳帽 等功能型配饰）。
        「潮流运动/场下健身」等泛化运动线配饰仍保持中性。"""
        self.assertEqual(
            extract_scene_domain("配件", "护具", "accessory", [], "GOLF手套"), "golf"
        )
        self.assertEqual(
            extract_scene_domain("配件", "护具", "accessory", [],
                                 "FILA X MSGM 中性场下网球头带"), "tennis"
        )
        self.assertEqual(
            extract_scene_domain("装备", "泳帽", "accessory", ["健身"]), "swim"
        )
        # 泛化「运动/健身」配饰不染 gym（时尚运动款，非功能 gym）
        self.assertEqual(
            extract_scene_domain("配件", "帽类", "accessory", [],
                                 "男女同款潮流运动棒球帽"), ""
        )

    def test_athleisure_to_daily(self):
        """athleisure（时尚休闲/运动休闲/时尚运动/潮流运动）非专业运动 → daily。
        复现 bug：女子时尚休闲中长羽绒服（中性）误搭专业骑行裤；归 daily 后
        daily×cycling 冲突可挡。"""
        # 羽绒服 bug（F11W543908FBK）
        self.assertEqual(
            extract_scene_domain("服装", "中长羽绒服", "top", [],
                                 "女子时尚休闲中长羽绒服"), "daily"
        )
        # 时尚休闲短袖T（原中性）
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [],
                                 "男子时尚休闲短袖T"), "daily"
        )
        # 潮流运动短袖T（原 gym，泛化「运动」误判）→ daily
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [],
                                 "男女同款潮流运动宽松短袖T恤"), "daily"
        )
        # 潮流运动连衣裙（原 gym）→ daily
        self.assertEqual(
            extract_scene_domain("服装", "连衣裙", "dress", [],
                                 "女子潮流运动宽松连衣裙"), "daily"
        )

    def test_athleisure_yields_to_functional_sport(self):
        """athleisure 让位给功能性 sport 关键词（防晒服/健身/紧身/网球…）。"""
        # 潮流运动防晒服 → outdoor（防晒服功能优先，不归 daily）
        self.assertEqual(
            extract_scene_domain("服装", "防晒服", "top", [],
                                 "女子潮流运动宽松拼色防晒服"), "outdoor"
        )
        # 时尚休闲健身T → gym（健身功能优先）
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [],
                                 "男子时尚休闲健身短袖T恤"), "gym"
        )
        # 网球老钱T 含「时尚休闲」→ tennis（specific sport step4 优先）
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", [],
                                 "网球老钱T女子时尚休闲短袖T恤"), "tennis"
        )

    def test_athleisure_not_applied_to_accessory_shoes(self):
        """配饰/鞋不染 athleisure-daily：配饰保持中性跨场景复用；
        鞋类无 athleisure 也无 sport 信号时由步骤 8 兜底 daily。"""
        self.assertEqual(
            extract_scene_domain("配件", "包", "accessory", [],
                                 "女子时尚休闲挎包"), ""
        )
        self.assertEqual(
            extract_scene_domain("配件", "帽类", "accessory", [],
                                 "男女同款潮流运动棒球帽"), ""
        )
        # 鞋跳过 6.5（非 garment），「时尚休闲板鞋」无功能/运动词 → 步骤 8 兜底 daily
        self.assertEqual(
            extract_scene_domain("鞋类", "板鞋", "shoes", [],
                                 "女子时尚休闲板鞋"), "daily"
        )

    def test_no_occasion_tags(self):
        """无 occasion_tags 且文本无命中 → 服装兜底 daily（配件才中性）。"""
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", []), "daily"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", None), "daily"
        )

    def test_neutral_tags_only(self):
        """仅含品类吧/季节等非场景标签 → 服装兜底 daily。"""
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["品类吧"]), "daily"
        )
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", ["季节"]), "daily"
        )

    def test_string_occasion_tags(self):
        """occasion_tags 以逗号分隔字符串传入也应正确解析。"""
        self.assertEqual(
            extract_scene_domain("服装", "短袖T恤", "top", "生活,健身"), "gym"
        )


class TestBuildSceneDomainMilvusExpr(unittest.TestCase):
    def test_daily_anchor_allows_daily_and_neutral(self):
        anchor = {"scene_domain": "daily"}
        expr = build_scene_domain_milvus_expr(anchor, "bottoms")
        self.assertIsNotNone(expr)
        self.assertIn('"daily"', expr)
        for d in ("golf", "tennis", "gym", "running", "outdoor", "ski", "swim", "cycling", "basketball"):
            self.assertNotIn(f'"{d}"', expr)

    def test_sport_anchor_allows_self_and_neutral(self):
        # self-only 默认：golf 锚点允许 golf + 中性 ""，排除 daily + 其它 sport
        expr = build_scene_domain_milvus_expr({"scene_domain": "golf"}, "top")
        self.assertIn('"golf"', expr)
        self.assertNotIn('"daily"', expr)
        self.assertNotIn('"tennis"', expr)
        self.assertNotIn('"swim"', expr)
        self.assertNotIn('"gym"', expr)

    def test_standalone_camp_allows_self_and_neutral(self):
        # self-only 默认：swim 锚点允许 swim + 中性 ""
        expr = build_scene_domain_milvus_expr({"scene_domain": "swim"}, "top")
        self.assertIn('"swim"', expr)
        self.assertNotIn('"daily"', expr)
        self.assertNotIn('"golf"', expr)
        self.assertNotIn('"ski"', expr)

    def test_neutral_anchor_returns_none(self):
        self.assertIsNone(build_scene_domain_milvus_expr({"scene_domain": ""}, "top"))

    def test_no_anchor_returns_none(self):
        self.assertIsNone(build_scene_domain_milvus_expr(None, "top"))


class TestBuildAttrEsFilter(unittest.TestCase):
    def _must_not_terms(self, filter_d: dict, field: str) -> list[str] | None:
        """从 must_not 中提取指定 field 的 terms/term 值列表。"""
        clauses = filter_d.get("must_not", [])
        for c in clauses:
            if "terms" in c and field in c["terms"]:
                return list(c["terms"][field])
            if "term" in c and field in c["term"]:
                return [c["term"][field]]
        return None

    def test_intimate_always_excluded(self):
        """无 anchor 时 must_not 仍常驻 is_intimate==True 排除。"""
        f = build_attr_es_filter(None, "top")
        self.assertIsNotNone(f)
        self.assertIn({"term": {"is_intimate": True}}, f["must_not"])

    def test_scene_domain_not_in_attr_filter(self):
        """scene_domain 正向隔离已迁出 build_attr_es_filter，must_not 不再含 scene_domain。"""
        anchor = {"scene_domain": "daily", "role": "top", "is_intimate": False}
        f = build_attr_es_filter(anchor, "bottoms")
        self.assertIsNone(self._must_not_terms(f, "scene_domain"))

    def test_long_top_excludes_short_bottoms(self):
        anchor = {
            "role": "top", "length_class": "long",
            "scene_domain": "", "is_intimate": False,
        }
        f = build_attr_es_filter(anchor, "bottoms")
        self.assertEqual(self._must_not_terms(f, "length_class"), ["short"])

    def test_full_coverage_excludes_full(self):
        anchor = {
            "role": "dress", "coverage": "full",
            "scene_domain": "", "is_intimate": False,
        }
        f = build_attr_es_filter(anchor, "top")
        self.assertEqual(self._must_not_terms(f, "coverage"), ["full"])

    def test_base_layer_top_excludes_base(self):
        anchor = {
            "role": "top", "layer": "base",
            "scene_domain": "", "is_intimate": False,
        }
        f = build_attr_es_filter(anchor, "top")
        self.assertEqual(self._must_not_terms(f, "layer"), ["base"])


class TestExplicitIntentBypass(unittest.TestCase):
    """用户对某 target_role 有任一显式 positive → 该 role 所有锚点驱动预过滤/安全网一律让路。

    复现三例：
      - scene：F11W528108FWT（daily 短袖T恤）×「网球裤」（bottoms.scene_domain=tennis）
      - length：F11W619219FPK（长袖T恤）×「白色短裤」（bottoms.category=短裤）
      - scene+data稀疏：F11M625212FLK（daily 套头卫衣）×「白色裤子」
        （bottoms.category=长裤；唯一匹配是 golf 长裤，daily 隔离会清零）
    正向约束（ES/Milvus positive + _item_violates_intent）已保证候选符合用户值，
    故锚点结构/冲突规则让路不会放行不符用户意图的单品。is_intimate 与 category/color
    pairing 同属锚点驱动预过滤，bypass 时一并让路（用户明确意图优先）；gender_conflict/
    season_conflict/age_conflict 由用户自报 intent 字段驱动，不让路（本就是意图体现）。
    """

    def _long_top_anchor(self, gender="女"):
        return {
            "role": "top", "length_class": "long", "gender": [gender],
            "scene_domain": "daily", "is_intimate": False,
        }

    def _short_bottoms(self):
        return {
            "role": "bottoms", "length_class": "short", "category_l2": "梭织短裤",
            "scene_domain": "daily", "is_intimate": False,
        }

    def _must_not_terms(self, filter_d: dict, field: str) -> list[str] | None:
        clauses = filter_d.get("must_not", [])
        for c in clauses:
            if "terms" in c and field in c["terms"]:
                return list(c["terms"][field])
            if "term" in c and field in c["term"]:
                return [c["term"][field]]
        return None

    # ── role_has_explicit_positive 检测 ─────────────────────────
    def test_explicit_positive_any_slot(self):
        """任一 positive 槽位非空即视为显式意图（color/color_series/category/
        scene_domain/length_class/coverage/modeling/budget 各一例）。"""
        cases = [
            ({"color": ["白色"]}, True),
            ({"color_series": ["白色系"]}, True),
            ({"category": ["梭织长裤", "针织长裤"]}, True),
            ({"scene_domain": "tennis"}, True),
            ({"length_class": "short"}, True),
            ({"coverage": "lower"}, True),
            ({"modeling": "宽松"}, True),
            ({"budget_max": 500}, True),
            ({}, False),                       # 空 positive
            ({"color": []}, False),            # 空数组
            ({"color": ""}, False),            # 空串
            ({"color": None}, False),          # None
        ]
        for pos, expected in cases:
            intent = UserIntent(
                anchor_role="top", target_roles=["bottoms"],
                gender="男", season=["夏"], category=["短袖T恤"],
                target_slots={"bottoms": {"positive": pos, "negative": {}}},
            )
            self.assertEqual(
                role_has_explicit_positive(intent, "bottoms"), expected,
                f"positive={pos} 期望 {expected}",
            )

    def test_explicit_positive_no_target_slots(self):
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            gender="男", season=["夏"], category=["短袖T恤"],
        )
        self.assertFalse(role_has_explicit_positive(intent, "bottoms"))
        self.assertFalse(role_has_explicit_positive(intent, "shoes"))

    # ── 预过滤 bypass_all：跳过全部锚点驱动子句（含 is_intimate，一律让路）────
    def test_es_filter_bypass_all_skips_anchor_clauses(self):
        anchor = self._long_top_anchor()
        f = build_attr_es_filter(anchor, "bottoms")
        self.assertEqual(self._must_not_terms(f, "length_class"), ["short"])
        # bypass_all=True → 整个 attr filter 让路（含 is_intimate），返回 None
        self.assertIsNone(build_attr_es_filter(anchor, "bottoms", bypass_all=True))

    def test_es_filter_bypass_all_drops_is_intimate(self):
        """bypass_all 一并让路 is_intimate（用户明确意图优先于内衣安全过滤）。
        正向约束已保证候选符合用户值，安全过滤让路不会放行不符意图的内衣。"""
        anchor = self._long_top_anchor()
        self.assertIsNone(build_attr_es_filter(anchor, "bottoms", bypass_all=True))

    def test_milvus_expr_bypass_all_skips_anchor_clauses(self):
        anchor = self._long_top_anchor()
        self.assertIn('length_class != "short"', build_attr_milvus_expr(anchor, "bottoms") or "")
        bypassed = build_attr_milvus_expr(anchor, "bottoms", bypass_all=True) or ""
        self.assertNotIn('length_class != "short"', bypassed)
        # is_intimate 同样让路（不再常驻）
        self.assertNotIn('is_intimate == "false"', bypassed)

    # ── 安全网 bypass_all：跳过全部冲突规则 ────────────────────
    def test_safety_net_default_conflicts_still_fire(self):
        """无 bypass 时，长袖×短裤、daily×tennis、full×full 均冲突。"""
        self.assertTrue(check_companion_conflict(self._long_top_anchor("男"), self._short_bottoms()))
        self.assertTrue(check_companion_conflict({"scene_domain": "daily"}, {"scene_domain": "tennis"}))
        anchor = {"coverage": "full", "scene_domain": "daily", "is_intimate": False}
        companion = {"coverage": "full", "scene_domain": "daily", "role": "dress", "is_intimate": False}
        self.assertTrue(check_companion_conflict(anchor, companion))

    def test_safety_net_bypass_all_skips_every_rule(self):
        """有显式意图（bypass_all）时，三类规则一律不再触发。"""
        self.assertFalse(check_companion_conflict(
            self._long_top_anchor("男"), self._short_bottoms(), bypass_all=True))
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "daily"}, {"scene_domain": "tennis"}, bypass_all=True))
        anchor = {"coverage": "full", "scene_domain": "daily", "is_intimate": False}
        companion = {"coverage": "full", "scene_domain": "daily", "role": "dress", "is_intimate": False}
        self.assertFalse(check_companion_conflict(anchor, companion, bypass_all=True))

    # ── check_outfit_conflict 按 role 下传 bypass ──────────────
    def test_outfit_conflict_respects_role_bypass(self):
        anchor = self._long_top_anchor("男")
        items = [{"sku_id": "A", "role": "bottoms", "length_class": "short",
                  "scene_domain": "daily", "is_intimate": False}]
        self.assertTrue(check_outfit_conflict(anchor, items, anchor_id=""))
        self.assertFalse(check_outfit_conflict(
            anchor, items, anchor_id="", role_bypass_all={"bottoms"}))

    def test_outfit_conflict_other_role_still_checked(self):
        """bypass 只对有显式意图的 role 生效；其它 role 的冲突仍检查。"""
        # 锚点 base 层上装 × top(base) 同伴 → 同层叠穿冲突（layer 规则）
        anchor = {"role": "top", "layer": "base", "length_class": "long",
                  "scene_domain": "daily", "is_intimate": False}
        items = [{"sku_id": "A", "role": "top", "layer": "base",
                  "scene_domain": "daily", "is_intimate": False}]
        # bottoms bypass 不影响 top item 的冲突判定
        self.assertTrue(check_outfit_conflict(
            anchor, items, anchor_id="", role_bypass_all={"bottoms"}))


class TestBuildSceneDomainEsFilter(unittest.TestCase):
    def _allow_terms(self, filter_d: dict) -> list[str] | None:
        """从正向 filter 中提取 scene_domain terms 允许集。"""
        clauses = filter_d.get("terms", {}).get("scene_domain")
        return list(clauses) if clauses else None

    def test_daily_anchor_allows_daily_and_neutral(self):
        anchor = {"scene_domain": "daily", "role": "top", "is_intimate": False}
        f = build_scene_domain_es_filter(anchor, "bottoms")
        self.assertIsNotNone(f)
        terms = self._allow_terms(f)
        self.assertEqual(sorted(terms), ["", "daily"])

    def test_sport_anchor_allows_self_and_neutral(self):
        # self-only 默认：golf 锚点允许 golf + 中性 ""
        anchor = {"scene_domain": "golf", "role": "top", "is_intimate": False}
        f = build_scene_domain_es_filter(anchor, "top")
        terms = self._allow_terms(f)
        self.assertEqual(sorted(terms), ["", "golf"])

    def test_swim_anchor_allows_swim_and_neutral(self):
        anchor = {"scene_domain": "swim", "role": "top", "is_intimate": False}
        f = build_scene_domain_es_filter(anchor, "top")
        self.assertEqual(sorted(self._allow_terms(f)), ["", "swim"])

    def test_neutral_anchor_returns_none(self):
        self.assertIsNone(build_scene_domain_es_filter({"scene_domain": ""}, "top"))

    def test_no_anchor_returns_none(self):
        self.assertIsNone(build_scene_domain_es_filter(None, "top"))


class TestCheckCompanionConflictSceneDomain(unittest.TestCase):
    def test_daily_x_sport_conflict(self):
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "daily"}, {"scene_domain": "gym"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "daily"}, {"scene_domain": "swim"}))

    def test_sport_x_daily_conflict(self):
        """双向拒绝。"""
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "golf"}, {"scene_domain": "daily"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "cycling"}, {"scene_domain": "daily"}))

    def test_daily_x_daily_no_conflict(self):
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "daily"}, {"scene_domain": "daily"}))

    def test_daily_x_neutral_no_conflict(self):
        """中性 "" 自动豁免。"""
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "daily"}, {"scene_domain": ""}))

    def test_sport_only_matches_same_sport(self):
        """self-only 默认：仅同域可搭，跨 sport 一律互斥（双向）。"""
        # 同域可搭
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "outdoor"}, {"scene_domain": "outdoor"}))
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "golf"}, {"scene_domain": "golf"}))
        # 跨 sport 互斥（双向）—— 含原同营对
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "golf"}, {"scene_domain": "tennis"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "tennis"}, {"scene_domain": "golf"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "gym"}, {"scene_domain": "running"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "swim"}, {"scene_domain": "golf"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "cycling"}, {"scene_domain": "tennis"}))
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "basketball"}, {"scene_domain": "gym"}))

    def test_neutral_anchor_no_conflict(self):
        self.assertFalse(check_companion_conflict(
            {"scene_domain": ""}, {"scene_domain": "daily"}))
        self.assertFalse(check_companion_conflict(
            {"scene_domain": ""}, {"scene_domain": "gym"}))


class TestSceneAllowDirected(unittest.TestCase):
    """有向/非对称语义：scene_allow 写 gym:[tennis] 不自动镜像 tennis:[gym]。

    通过 patch _load_scene_config 注入自定义有向表，并清缓存避免污染其它用例。
    """

    _custom = {"gym": ["gym", "tennis"], "tennis": ["tennis"]}

    @staticmethod
    def _allow_terms(filter_d: dict) -> list[str] | None:
        clauses = filter_d.get("terms", {}).get("scene_domain")
        return list(clauses) if clauses else None

    def setUp(self):
        from backend.ranking import outfit_conflict as oc
        self._oc = oc
        self._patch = patch(
            "backend.ranking.outfit_conflict._load_scene_config",
            return_value=self._custom,
        )
        self._patch.start()
        # lru_cache 清缓存，使 _load_rules / _scene_domain_allow_set 重读自定义表
        oc._load_scene_config.cache_clear()
        oc._load_rules.cache_clear()

    def tearDown(self):
        self._patch.stop()
        self._oc._load_scene_config.cache_clear()
        self._oc._load_rules.cache_clear()

    def test_gym_anchor_allows_tennis(self):
        f = build_scene_domain_es_filter({"scene_domain": "gym"}, "top")
        self.assertIn("tennis", self._allow_terms(f))

    def test_tennis_anchor_does_not_allow_gym(self):
        f = build_scene_domain_es_filter({"scene_domain": "tennis"}, "top")
        self.assertNotIn("gym", self._allow_terms(f))

    def test_gym_pushes_tennis_no_conflict(self):
        """gym 锚点推 tennis 候选 → 安全网放行。"""
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "gym"}, {"scene_domain": "tennis"}))

    def test_tennis_does_not_push_gym_conflict(self):
        """tennis 锚点不推 gym → 安全网拒绝（非对称关键点）。"""
        self.assertTrue(check_companion_conflict(
            {"scene_domain": "tennis"}, {"scene_domain": "gym"}))

    def test_neutral_companion_still_allowed(self):
        """中性配件 "" 始终放行，不受有向表约束。"""
        self.assertFalse(check_companion_conflict(
            {"scene_domain": "gym"}, {"scene_domain": ""}))

    def test_milvus_expr_directed(self):
        """Milvus expr 同样反映有向允许集。"""
        expr_gym = build_scene_domain_milvus_expr({"scene_domain": "gym"}, "top")
        self.assertIn('"tennis"', expr_gym)
        expr_tennis = build_scene_domain_milvus_expr({"scene_domain": "tennis"}, "top")
        self.assertNotIn('"gym"', expr_tennis)


if __name__ == "__main__":
    unittest.main()
