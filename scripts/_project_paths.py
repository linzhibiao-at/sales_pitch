"""fila_agent_html 脚本共用路径（读取 config.yaml）。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_paths(config_path: Path | None = None) -> dict[str, Path]:
    cfg_path = config_path or (ROOT / "config.yaml")
    with cfg_path.open(encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    paths = cfg.get("paths") or {}
    product = ROOT / str(paths.get("product_dir", "data/tables"))
    processed = ROOT / str(paths.get("processed_dir", "data/processed"))
    preview = ROOT / "data" / "preview"
    preview.mkdir(parents=True, exist_ok=True)
    return {
        "root": ROOT,
        "repo_root": REPO_ROOT,
        "product_dir": product,
        "processed_dir": processed,
        "preview_dir": preview,
        "outfits_json": preview / "fila_outfits.json",
        "tools_dir": ROOT / "tools",
    }
