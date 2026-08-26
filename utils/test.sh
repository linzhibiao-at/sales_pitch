curl -s -u "${ES_USERNAME}:${ES_PASSWORD}" \
  'http://10.131.7.119:9200/umalog-q-maiamgs-index-fila-skus/_search?pretty' \
  -H 'Content-Type: application/json' \
  -d '{
  "size": 20,
  "query": {
    "bool": {
      "filter": [
        { "term": { "role": "bottoms" } },
        { "term": { "gender": "男" } },
        { "wildcard": { "season": "*冬*" } },
        { "terms": { "color_series": ["白色系", "多色系"] } }
      ],
      "should": [
        { "multi_match": { "query": "机能风", "fields": ["title^2", "search_text", "search_keywords"], "type": "best_fields", "lenient": true } },
        { "multi_match": { "query": "运动休闲", "fields": ["title^2", "search_text", "search_keywords"], "type": "best_fields", "lenient": true } },
        { "multi_match": { "query": "简约", "fields": ["title^2", "search_text", "search_keywords"], "type": "best_fields", "lenient": true } },
        { "multi_match": { "query": "户外", "fields": ["title^2", "search_text", "search_keywords"], "type": "best_fields", "lenient": true } },
        { "multi_match": { "query": "日常通勤", "fields": ["title^2", "search_text", "search_keywords"], "type": "best_fields", "lenient": true } },
        { "multi_match": { "query": "休闲", "fields": ["title^2", "search_text", "search_keywords"], "type": "best_fields", "lenient": true } },
        { "term": { "season": { "value": "冬", "boost": 2 } } }
      ]
    }
  }
}'

curl -s -u "${ES_USERNAME}:${ES_PASSWORD}" \
  'http://10.131.7.119:9200/umalog-q-maiamgs-index-fila-skus/_search?pretty' \
  -H 'Content-Type: application/json' \
  -d '{
  "size": 20,
  "query": {
    "bool": {
      "filter": [
        { "term": { "role": "bottoms" } },
        { "term": { "gender": "男童" } },
        { "wildcard": { "season": "*夏*" } },
        { "terms": { "color_series": ["绿色系", "多色系"] } }
      ]
    }
  }
}'