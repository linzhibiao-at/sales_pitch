from __future__ import annotations

import unittest
from pathlib import Path

import yaml

DICT_DIR = Path(__file__).resolve().parent.parent / "backend" / "intent" / "dictionaries"


def _load_yaml(name: str) -> dict:
    with (DICT_DIR / name).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def _cartesian_keys() -> set[str]:
    data = _load_yaml("category_l2_cartesian_pairing.yaml")
    rules = (data.get("pairing_rules") or {})
    return set(rules.keys())


def _excluded_canonicals() -> set[str]:
    """non_clothing_exclusion.yaml 中声明的不参与搭配的中类，
    允许出现在 categories.yaml 但不出现在 cartesian pairing 中。"""
    data = _load_yaml("non_clothing_exclusion.yaml")
    excluded: set[str] = set()
    for key in ("non_clothing", "intimate_swimwear"):
        excluded.update(data.get(key) or [])
    return excluded


class CategoryL2CanonicalConsistencyTest(unittest.TestCase):
    """守护 category_l2 canonical 值域的一致性。

    canonical 的唯一来源是 category_l2_cartesian_pairing.yaml 的 key 集。
    categories.yaml（trie 别名）与 category_l2_merge.yaml（ETL 归并）的值
    都必须对齐到这个集合，否则会出现「意图侧抽不到 / ETL 侧写不进」的漂移。
    """

    def test_categories_yaml_values_are_subset_of_cartesian(self) -> None:
        """categories.yaml 的 canonical 目标必须是 cartesian 合法中类，
        或在 non_clothing_exclusion.yaml 中显式排除。"""
        cat = _load_yaml("categories.yaml")
        allowed = _cartesian_keys() | _excluded_canonicals()
        invalid = {v for v in cat.values() if v not in allowed}
        self.assertFalse(
            invalid,
            f"categories.yaml 含非法 canonical（不在 cartesian pairing 也未声明排除）: {sorted(invalid)}",
        )

    def test_categories_yaml_covers_all_cartesian_canonicals(self) -> None:
        """trie 路径必须能产出全部 cartesian canonical，否则用户 query 命中不到 ETL SKU。"""
        cat = _load_yaml("categories.yaml")
        missing = _cartesian_keys() - set(cat.values())
        self.assertFalse(
            missing,
            f"trie 缺失 canonical（categories.yaml 未覆盖）: {sorted(missing)}",
        )

    def test_merge_yaml_values_are_subset_of_cartesian(self) -> None:
        """category_l2_merge.yaml 的归并目标必须是 cartesian 合法中类。"""
        merge = _load_yaml("category_l2_merge.yaml")
        invalid = {v for v in merge.values() if v not in _cartesian_keys()}
        self.assertFalse(
            invalid,
            f"category_l2_merge.yaml 含非法 canonical（不在 cartesian pairing 中）: {sorted(invalid)}",
        )


if __name__ == "__main__":
    unittest.main()
