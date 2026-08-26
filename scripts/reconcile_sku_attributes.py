#!/usr/bin/env python3
"""就地重算 skus.jsonl 的 layer / coverage / length_class / is_intimate，并修正误判 role。

背景
----
``backend/intent/sku_attributes.py`` 的 layer 白名单补全后（短T/针织运动上衣/
梭织薄外套/滑雪服/马甲 等简称/异名），需要让当前 skus.jsonl 立即带上更完整的
layer，而不必重跑全量 ETL。同时修正一批 role 误判：网球连衣裙被源表标 up_down=
"上装" 导致 role=top（应 dress），棒球帽/围巾被标 top（应 accessory）。

本脚本对每条 SKU：
  1. 修正误判 role（仅 top→dress / top→accessory 的明确子集，其余 role 不动）；
  2. 用修正后的 role + category_l2 + title 重算四个属性字段。
length_class 走 ``resolve_length_class``（规则 n/a 时对 top/bottoms 回退 VLM CSV，
连衣裙等非 top/bottoms 保持 n/a，不套用误判期跑出的 VLM 值）。

ETL 构建侧（``etl_common.py: infer_role`` / ``resolve_length_class``）已做同样修正，
未来重跑 ETL 重建 skus.jsonl 时结果一致；本脚本只是一次性桥接。

用法
----
    cd fila_agent_html && export PYTHONPATH="$(pwd)"
    python scripts/reconcile_sku_attributes.py            # 重算并写回
    python scripts/reconcile_sku_attributes.py --dry-run  # 只统计不改文件
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.intent.sku_attributes import (  # noqa: E402
    extract_coverage,
    extract_is_intimate,
    extract_layer,
)
from scripts.etl_common import resolve_length_class  # noqa: E402

DEFAULT_SKUS_JSONL = ROOT / "data" / "processed" / "skus.jsonl"


_DRESS_CAT2 = {"连衣裙", "裙装", "连体装"}
# 帽类用 cat2 精确匹配 + 围巾/手套关键词；注意不能只判 "帽" 子串——会误中"连帽卫衣"
_HAT_TITLE_KEYWORDS = ("棒球帽", "鸭舌帽", "遮阳帽", "空顶帽", "渔夫帽", "针织帽", "毛线帽", "网球帽")


def _fix_role(role: str, cat2: str, title: str) -> tuple[str, str]:
    """修正误判 role，返回 (new_role, reason)。仅处理 top 被误标的明确子集。"""
    r = (role or "").strip().lower()
    if r != "top":
        return role, ""
    c2 = cat2 or ""
    t = title or ""
    if c2 in _DRESS_CAT2 or "连衣裙" in t or "连体" in t:
        return "dress", "top→dress"
    if c2 == "帽类" or "围巾" in c2 or "手套" in c2 or "围巾" in t or any(h in t for h in _HAT_TITLE_KEYWORDS):
        return "accessory", "top→accessory"
    return role, ""


def reconcile(path: Path, dry_run: bool) -> int:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    before_layer = Counter(str(r.get("layer") or "") or "<missing>" for r in rows)
    before_cov = Counter(str(r.get("coverage") or "") or "<missing>" for r in rows)
    before_len = Counter(str(r.get("length_class") or "") or "<missing>" for r in rows)
    before_role = Counter(str(r.get("role") or "") or "<missing>" for r in rows)
    before_int = Counter(str(r.get("is_intimate") or "") or "<missing>" for r in rows)

    role_fixed = Counter()
    layer_changed = 0
    cov_changed = 0
    len_changed = 0
    int_changed = 0
    for r in rows:
        cat2 = str(r.get("category_l2") or "")
        cat3 = str(r.get("category_l3") or "")
        title = str(r.get("title") or "")
        sid = str(r.get("sku_id") or "")
        old_role = str(r.get("role") or "")
        new_role, why = _fix_role(old_role, cat2, title)
        if why:
            r["role"] = new_role
            role_fixed[why] += 1

        new_layer = extract_layer(cat2, title)
        new_cov = extract_coverage(new_role, cat2, title)
        new_len = resolve_length_class(new_role, cat2, title, sid, "", cat3)
        new_int = extract_is_intimate(cat2, title)

        if str(r.get("layer") or "") != new_layer:
            layer_changed += 1
        r["layer"] = new_layer
        if str(r.get("coverage") or "") != new_cov:
            cov_changed += 1
        r["coverage"] = new_cov
        if str(r.get("length_class") or "") != new_len:
            len_changed += 1
        r["length_class"] = new_len
        # is_intimate 存储为布尔；保持与 ETL 一致
        if bool(r.get("is_intimate")) != new_int:
            int_changed += 1
        r["is_intimate"] = new_int

    after_layer = Counter(str(r.get("layer") or "") or "<missing>" for r in rows)
    after_cov = Counter(str(r.get("coverage") or "") or "<missing>" for r in rows)
    after_len = Counter(str(r.get("length_class") or "") or "<missing>" for r in rows)
    after_role = Counter(str(r.get("role") or "") or "<missing>" for r in rows)
    after_int = Counter(str(r.get("is_intimate") or "") or "<missing>" for r in rows)

    print(f"总 SKU: {len(rows)}")
    print(f"role 修正: {dict(role_fixed)} (合计 {sum(role_fixed.values())})")
    print(f"layer  变更行数: {layer_changed}")
    print(f"coverage 变更行数: {cov_changed}")
    print(f"length_class 变更行数: {len_changed}")
    print(f"is_intimate 变更行数: {int_changed}")
    print(f"--- role:       {dict(before_role)} → {dict(after_role)}")
    print(f"--- layer:      {dict(before_layer)} → {dict(after_layer)}")
    print(f"--- coverage:   {dict(before_cov)} → {dict(after_cov)}")
    print(f"--- length:     {dict(before_len)} → {dict(after_len)}")
    print(f"--- is_intimate:{dict(before_int)} → {dict(after_int)}")

    if dry_run:
        print("--dry-run：未写回文件。")
    else:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(path)
        print(f"已写回 {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="重算 skus.jsonl 的 layer/coverage/length_class/is_intimate，修正误判 role",
    )
    parser.add_argument("--skus-jsonl", type=Path, default=DEFAULT_SKUS_JSONL)
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写回")
    args = parser.parse_args()
    if not args.skus_jsonl.is_file():
        print(f"缺少 {args.skus_jsonl}", file=sys.stderr)
        return 1
    return reconcile(args.skus_jsonl, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
