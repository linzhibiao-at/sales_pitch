#!/usr/bin/env python3
"""校验统一 ETL 核心文件是否存在且可解析。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
PREVIEW = ROOT / "data" / "preview"


def main() -> int:
    need = [
        PROC / "skus.jsonl",
        PROC / "spu_to_skus.json",
        PREVIEW / "fila_outfits.json",
    ]
    err = 0
    for p in need:
        if not p.is_file():
            print(f"MISSING {p}")
            err += 1
            continue
        if p.suffix == ".json":
            with p.open(encoding="utf-8") as f:
                json.load(f)
        else:
            with p.open(encoding="utf-8") as f:
                line = f.readline()
                if line.strip():
                    json.loads(line)
        print(f"OK {p}")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
