"""AC 自动机 Slot 提取器：基于 pyahocorasick 实现高效多模式匹配。"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

try:
    import ahocorasick
except ImportError:
    ahocorasick = None  # type: ignore

logger = logging.getLogger(__name__)

_NEGATION_PREFIXES = ("不要", "别", "不", "排除", "不喜欢", "拒", "不想", "不要了", "非", "除")
_NEGATION_EXCEPTIONS = ("除了", "除外")


def normalize_text_for_extraction(text: str) -> str:
    """文本预处理：全角→半角、去emoji、去中文间空格。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[a-zA-Z])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[a-zA-Z])", "", text)
    return text


def is_negated(text: str, position: int) -> bool:
    """检测命中位置前是否存在否定前缀。"""
    window_start = max(0, position - 6)
    prefix = text[window_start:position]
    if any(exc in prefix for exc in _NEGATION_EXCEPTIONS):
        return False
    return any(neg in prefix for neg in _NEGATION_PREFIXES)


class SlotExtractor:
    """单个 slot 维度的 AC 自动机提取器。"""

    def __init__(self, dictionary: dict[str, str], name: str = "") -> None:
        self.name = name
        self._dict = dictionary
        self._automaton: Any = None
        if ahocorasick and dictionary:
            self._automaton = ahocorasick.Automaton()
            for keyword, normalized in dictionary.items():
                kw = keyword.lower()
                self._automaton.add_word(kw, (kw, normalized))
            self._automaton.make_automaton()

    def extract(self, text: str) -> list[str]:
        """返回去重后的标准化中文值列表（不含被否定的值）。"""
        if not self._automaton or not text:
            return []
        t = normalize_text_for_extraction(text).lower()
        seen: set[str] = set()
        result: list[str] = []
        for _end_idx, (keyword, normalized) in self._automaton.iter(t):
            if normalized in seen:
                continue
            pos = _end_idx - len(keyword) + 1
            if is_negated(t, pos):
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def extract_with_hits(self, text: str) -> list[dict[str, str]]:
        """返回带命中词条的详细结果（含否定标记）。"""
        if not self._automaton or not text:
            return []
        t = normalize_text_for_extraction(text).lower()
        seen: set[str] = set()
        result: list[dict[str, str]] = []
        for end_idx, (keyword, normalized) in self._automaton.iter(t):
            if normalized in seen:
                continue
            pos = end_idx - len(keyword) + 1
            negated = is_negated(t, pos)
            seen.add(normalized)
            result.append({
                "keyword": keyword,
                "value": normalized,
                "position": pos,
                "negated": str(negated),
            })
        return result

    def extract_negated(self, text: str) -> list[str]:
        """返回被否定的值列表。"""
        if not self._automaton or not text:
            return []
        t = normalize_text_for_extraction(text).lower()
        seen: set[str] = set()
        result: list[str] = []
        for _end_idx, (keyword, normalized) in self._automaton.iter(t):
            if normalized in seen:
                continue
            pos = _end_idx - len(keyword) + 1
            if is_negated(t, pos):
                seen.add(normalized)
                result.append(normalized)
        return result


def _load_yaml_dict(path: Path) -> dict[str, str]:
    """加载 YAML 词典文件。"""
    if not path.is_file():
        logger.warning("词典文件不存在: %s", path)
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k is not None and v is not None}


class MultiSlotExtractor:
    """聚合所有 slot 维度的提取器。"""

    def __init__(self, dict_dir: str | Path) -> None:
        d = Path(dict_dir)
        self._extractors: dict[str, SlotExtractor] = {}
        slot_files = {
            "style_tags": "styles.yaml",
            "season": "seasons.yaml",
            "occasion_tags": "occasions.yaml",
            "color": "colors.yaml",
            "color_series": "color_series.yaml",
            "category": "categories.yaml",
            "gender": "genders.yaml",
            "age": "ages.yaml",
            "role": "roles.yaml",
        }
        for slot_name, filename in slot_files.items():
            dictionary = _load_yaml_dict(d / filename)
            self._extractors[slot_name] = SlotExtractor(dictionary, name=slot_name)
        if not ahocorasick:
            logger.warning(
                "pyahocorasick 未安装，Trie 提取将退化为空结果。"
                "请安装: pip install pyahocorasick"
            )

    def extract_all(self, text: str) -> dict[str, list[str]]:
        """返回 {slot_name: [中文值]}（不含被否定的值）。"""
        result: dict[str, list[str]] = {}
        for slot_name, extractor in self._extractors.items():
            values = extractor.extract(text)
            result[slot_name] = values
        return result

    def extract_all_with_hits(self, text: str) -> dict[str, list[dict[str, str]]]:
        """返回带命中详情的结果，用于调试面板。"""
        result: dict[str, list[dict[str, str]]] = {}
        for slot_name, extractor in self._extractors.items():
            hits = extractor.extract_with_hits(text)
            result[slot_name] = hits
        return result

    def extract_all_negated(self, text: str) -> dict[str, list[str]]:
        """返回所有 slot 中被否定的值。"""
        result: dict[str, list[str]] = {}
        for slot_name, extractor in self._extractors.items():
            negated = extractor.extract_negated(text)
            if negated:
                result[slot_name] = negated
        return result


# 全局单例（懒加载）
_global_extractor: MultiSlotExtractor | None = None


def get_multi_slot_extractor(dict_dir: str | Path | None = None) -> MultiSlotExtractor:
    """获取全局 MultiSlotExtractor 单例。"""
    global _global_extractor
    if _global_extractor is None:
        if dict_dir is None:
            from backend.config import get_root, load_config
            cfg = load_config()
            intent_cfg = cfg.get("intent") or {}
            rel = intent_cfg.get("dict_dir") or "backend/intent/dictionaries"
            dict_dir = get_root() / rel
        _global_extractor = MultiSlotExtractor(dict_dir)
    return _global_extractor


def reset_multi_slot_extractor() -> None:
    """重置全局单例（用于词典更新后重新加载）。"""
    global _global_extractor
    _global_extractor = None
