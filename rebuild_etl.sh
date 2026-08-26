# python3 scripts/build_catalog.py --no-up-time-filter
# python3 scripts/select_images.py
#python3 scripts/build_fila_guide_outfits_fast.py --workers 8
#python3 scripts/build_fila_es_index.py --reset
#python3 scripts/build_dphs_outfits_es.py --reset
#python3 scripts/build_outfits_unique_es.py --reset
#python3 scripts/validate_data.py
#python3 scripts/extract_category_l2_pairing_rules.py
#python3 scripts/gen_category_l2_cartesian_pairing.py
#python3 scripts/fill_missing_pairing.py
python3 scripts/build_fila_milvus_multimodal_index.py --reset
python3 scripts/build_hybrid_index.py --reset  # fila_sku_hybrid_vectors（BM25+dense），需 export ARK_API_KEY
python3 scripts/build_complementary_vectors.py
