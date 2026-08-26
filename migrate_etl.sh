python3 scripts/build_catalog.py
python3 scripts/select_images.py
python3 scripts/build_fila_guide_outfits_fast.py --workers 64
python3 scripts/build_fila_es_index.py --reset
python3 scripts/build_dphs_outfits_es.py --reset
python3 scripts/build_outfits_unique_es.py --reset
python3 scripts/migrate_milvus_add_attr_fields.py --dump
python3 scripts/migrate_milvus_add_attr_fields.py --apply
#python3 scripts/build_hybrid_index.py --reset  # fila_sku_hybrid_vectors（BM25+dense），需 export ARK_API_KEY
