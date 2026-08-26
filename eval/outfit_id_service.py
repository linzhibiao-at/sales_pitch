"""单独服务：测试所有自动生成的 outfit_id 生成逻辑。

覆盖三种 ID 格式：
  - batch_eval_{hash}   — 批量评测
  - synth_rel_{hash}    — 关系召回合成搭配
  - synth_txt_{hash}    — 文本向量合成搭配

启动:
    cd fila_agent_html
    python -m eval.outfit_id_service

访问:
    http://localhost:8090/docs        — Swagger UI
    http://localhost:8090/generate    — POST 生成 outfit_id
    http://localhost:8090/test        — GET  运行内置测试用例
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from eval.batch_eval_outfit_es import (
    BATCH_EVAL_SOURCE_PREFIX,
    batch_eval_outfit_id,
)
from backend.services.synthetic_outfit import (
    pair_outfit_from_anchor_and_target,
    compose_outfit_from_items,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("outfit_id_service")

app = FastAPI(title="Outfit ID Generator Test Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ──

class GenerateRequest(BaseModel):
    """batch_eval 类型 ID 生成请求。"""
    id_type: str = "batch_eval"  # batch_eval | synth_rel | synth_txt
    input_sku_id: str = ""
    original_outfit_id: str = ""
    rank_order: int = 1
    # synth_rel 专用
    anchor_sku_id: str = ""
    target_sku_id: str = ""
    # synth_txt 专用
    sku_ids: List[str] = []


class GenerateResponse(BaseModel):
    outfit_id: str
    id_type: str
    raw_string: str
    detail: dict[str, Any] = {}


class TestResult(BaseModel):
    name: str
    passed: bool
    outfit_id: str
    detail: str


# ── Endpoints ──

@app.get("/")
def root():
    return {
        "service": "Outfit ID Generator Test",
        "id_types": {
            "batch_eval": "批量评测 ID: batch_eval_{hash8}",
            "synth_rel": "关系召回合成搭配 ID: synth_rel_{hash8}",
            "synth_txt": "文本向量合成搭配 ID: synth_txt_{hash8}",
        },
        "endpoints": {
            "generate": "POST /generate — 生成 outfit_id",
            "test": "GET /test — 运行内置测试用例",
            "docs": "GET /docs — Swagger UI",
        },
    }


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    """根据 id_type 生成对应的 outfit_id。"""
    if req.id_type == "synth_rel":
        raw = f"synth_rel_{req.anchor_sku_id}_{req.target_sku_id}"
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        oid = f"synth_rel_{h}"
        return GenerateResponse(
            outfit_id=oid, id_type="synth_rel", raw_string=raw,
            detail={"anchor_sku_id": req.anchor_sku_id, "target_sku_id": req.target_sku_id},
        )
    elif req.id_type == "synth_txt":
        sku_part = "_".join(sorted(req.sku_ids)[:6])
        raw = f"synth_txt_{sku_part}"
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        oid = f"synth_txt_{h}"
        return GenerateResponse(
            outfit_id=oid, id_type="synth_txt", raw_string=raw,
            detail={"sku_ids": req.sku_ids, "sku_part": sku_part},
        )
    else:
        raw = f"{req.input_sku_id}__{req.rank_order:02d}__{req.original_outfit_id}"
        oid = batch_eval_outfit_id(
            req.input_sku_id, req.original_outfit_id, req.rank_order,
        )
        return GenerateResponse(
            outfit_id=oid, id_type="batch_eval", raw_string=raw,
            detail={
                "input_sku_id": req.input_sku_id,
                "original_outfit_id": req.original_outfit_id,
                "rank_order": req.rank_order,
            },
        )


@app.get("/test", response_model=list[TestResult])
def run_tests():
    """运行内置测试用例，验证三种 outfit_id 生成逻辑。"""
    results: list[TestResult] = []

    # ═══ batch_eval 系列 ═══

    # 1) batch_eval: 前缀正确
    oid = batch_eval_outfit_id("SKU001", "outfit_abc_123", 1)
    results.append(TestResult(
        name="[batch_eval] prefix correct",
        passed=oid.startswith("batch_eval_"),
        outfit_id=oid,
        detail=f"前缀应为 'batch_eval_', 实际: '{oid}'",
    ))

    # 2) batch_eval: 幂等性
    oid1 = batch_eval_outfit_id("SKU001", "outfit_abc_123", 1)
    oid2 = batch_eval_outfit_id("SKU001", "outfit_abc_123", 1)
    results.append(TestResult(
        name="[batch_eval] idempotency",
        passed=oid1 == oid2,
        outfit_id=oid1,
        detail=f"两次调用结果应一致: {oid1} vs {oid2}",
    ))

    # 3) batch_eval: 不同 rank → 不同 ID
    oid_r1 = batch_eval_outfit_id("SKU001", "outfit_abc_123", 1)
    oid_r2 = batch_eval_outfit_id("SKU001", "outfit_abc_123", 2)
    results.append(TestResult(
        name="[batch_eval] different rank_order → different id",
        passed=oid_r1 != oid_r2,
        outfit_id=f"{oid_r1} / {oid_r2}",
        detail=f"rank_order 不同应产生不同 ID",
    ))

    # 4) batch_eval: 固定长度 19 字符
    oid_long = batch_eval_outfit_id("1234567890", "very_long_outfit_id_with_many_segments", 10)
    results.append(TestResult(
        name="[batch_eval] fixed length = 19",
        passed=len(oid_long) == 19,
        outfit_id=oid_long,
        detail=f"batch_eval_xxxxxxxx = 19 chars, actual: {len(oid_long)}",
    ))

    # ═══ synth_rel 系列 ═══

    def _make_anchor(sku_id: str) -> dict[str, Any]:
        return {"sku_id": sku_id, "spu_id": f"spu_{sku_id}", "role": "上装",
                "title": f"Anchor {sku_id}", "price": 100.0, "gender": "男",
                "season": "春夏", "display_image": "", "tryon_image": ""}

    def _make_target(sku_id: str) -> dict[str, Any]:
        return {"sku_id": sku_id, "spu_id": f"spu_{sku_id}", "role": "下装",
                "title": f"Target {sku_id}", "price": 200.0, "gender": "男",
                "season": "春夏", "display_image": "", "tryon_image": ""}

    # 5) synth_rel: 前缀正确
    outfit = pair_outfit_from_anchor_and_target(
        _make_anchor("A001"), _make_target("T001"), relation_ids=["rel_1"],
    )
    oid = outfit["outfit_id"]
    results.append(TestResult(
        name="[synth_rel] prefix correct",
        passed=oid.startswith("synth_rel_"),
        outfit_id=oid,
        detail=f"前缀应为 'synth_rel_', 实际: '{oid}'",
    ))

    # 6) synth_rel: 固定长度 18 字符 (synth_rel_ = 10 + 8 hash)
    results.append(TestResult(
        name="[synth_rel] fixed length = 18",
        passed=len(oid) == 18,
        outfit_id=oid,
        detail=f"synth_rel_xxxxxxxx = 18 chars, actual: {len(oid)}",
    ))

    # 7) synth_rel: 幂等性
    outfit2 = pair_outfit_from_anchor_and_target(
        _make_anchor("A001"), _make_target("T001"), relation_ids=["rel_2"],
    )
    results.append(TestResult(
        name="[synth_rel] idempotency (same anchor+target)",
        passed=outfit["outfit_id"] == outfit2["outfit_id"],
        outfit_id=f"{outfit['outfit_id']} / {outfit2['outfit_id']}",
        detail="相同 anchor+target 应生成相同 ID",
    ))

    # 8) synth_rel: 不同 target → 不同 ID
    outfit3 = pair_outfit_from_anchor_and_target(
        _make_anchor("A001"), _make_target("T999"), relation_ids=["rel_1"],
    )
    results.append(TestResult(
        name="[synth_rel] different target → different id",
        passed=outfit["outfit_id"] != outfit3["outfit_id"],
        outfit_id=f"{outfit['outfit_id']} / {outfit3['outfit_id']}",
        detail="不同 target 应产生不同 ID",
    ))

    # ═══ synth_txt 系列 ═══

    def _make_item(sku_id: str, role: str = "上装") -> dict[str, Any]:
        return {"sku_id": sku_id, "spu_id": f"spu_{sku_id}", "role": role,
                "title": f"Item {sku_id}", "price": 150.0, "gender": "女",
                "season": "秋冬", "display_image": "", "tryon_image": ""}

    # 9) synth_txt: 前缀正确
    txt_outfit = compose_outfit_from_items(
        [_make_item("S1", "上装"), _make_item("S2", "下装")],
        anchor_sku_id="S1", source="text_vector_compose",
    )
    oid_txt = txt_outfit["outfit_id"]
    results.append(TestResult(
        name="[synth_txt] prefix correct",
        passed=oid_txt.startswith("synth_txt_"),
        outfit_id=oid_txt,
        detail=f"前缀应为 'synth_txt_', 实际: '{oid_txt}'",
    ))

    # 10) synth_txt: 固定长度 18 字符 (synth_txt_ = 10 + 8 hash)
    results.append(TestResult(
        name="[synth_txt] fixed length = 18",
        passed=len(oid_txt) == 18,
        outfit_id=oid_txt,
        detail=f"synth_txt_xxxxxxxx = 18 chars, actual: {len(oid_txt)}",
    ))

    # 11) synth_txt: 幂等性
    txt_outfit2 = compose_outfit_from_items(
        [_make_item("S1", "上装"), _make_item("S2", "下装")],
        anchor_sku_id="S1", source="text_vector_compose",
    )
    results.append(TestResult(
        name="[synth_txt] idempotency",
        passed=txt_outfit["outfit_id"] == txt_outfit2["outfit_id"],
        outfit_id=f"{txt_outfit['outfit_id']} / {txt_outfit2['outfit_id']}",
        detail="相同 SKU 组合应生成相同 ID",
    ))

    # 12) synth_txt: 不同 SKU → 不同 ID
    txt_outfit3 = compose_outfit_from_items(
        [_make_item("S1", "上装"), _make_item("S99", "下装")],
        anchor_sku_id="S1", source="text_vector_compose",
    )
    results.append(TestResult(
        name="[synth_txt] different SKUs → different id",
        passed=txt_outfit["outfit_id"] != txt_outfit3["outfit_id"],
        outfit_id=f"{txt_outfit['outfit_id']} / {txt_outfit3['outfit_id']}",
        detail="不同 SKU 组合应产生不同 ID",
    ))

    # ═══ 跨类型区分 ═══

    # 13) synth_rel 和 synth_txt 前缀不同
    results.append(TestResult(
        name="[cross-type] synth_rel ≠ synth_txt prefix",
        passed=outfit["outfit_id"].startswith("synth_rel_")
               and txt_outfit["outfit_id"].startswith("synth_txt_"),
        outfit_id=f"{outfit['outfit_id']} / {txt_outfit['outfit_id']}",
        detail="关系召回与文本向量应使用不同前缀以区分召回通路",
    ))

    passed_count = sum(1 for r in results if r.passed)
    total = len(results)
    logger.info("测试完成: %d/%d 通过", passed_count, total)

    return results


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
