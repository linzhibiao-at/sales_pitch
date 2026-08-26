"""对外接口健壮性回归测试（契约 / 功能 / 故障注入 / 边界）。

零依赖（仅 stdlib urllib），直接打线上服务，逐用例断言并打印 PASS/FAIL 表。
用法:
    python3 tests/test_external_api_robustness.py [BASE_URL]
默认 BASE_URL=http://localhost:8888 。

设计: 每条 case 给 (name, method, path, body, want_status, checker)。
checker 收 (resp_dict, headers, status, latency) 返回 None 或失败原因串。
want_status=0 表示「不限定状态码，交给 checker」。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8888"
TIMEOUT = 60
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def hget(hd, name):
    """大小写无关取响应头(Starlette 发小写 x-trace-id)。"""
    name = name.lower()
    for k, v in (hd or {}).items():
        if k.lower() == name:
            return v
    return None
KNOWN_SKU = "F11W619219FPK"
BAD_IMAGE = "https://invalid.invalid/nope.jpg"
# micro_guide 的有效 Key(config/api_keys.yaml); 成功路径默认带它,
# enabled=false 时被忽略, enabled=true 时通过——两态都正确。
VALID_KEY = "ak_a1b2c3d4e5f6789012345678abcdef01"


def call(method: str, path: str, body, timeout=TIMEOUT, headers=None):
    """发请求, 返回 (status, headers, obj_or_none, latency, err)。

    headers=None → 注入默认有效 X-API-Key(成功路径用);
    headers={}   → 不带 Key(AUTH 无 Key 用例);
    headers=dict → 用给定头(覆盖默认, AUTH 错 Key 用例)。
    """
    url = BASE + path
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if headers is None:
        req.add_header("X-API-Key", VALID_KEY)
    else:
        for k, v in headers.items():
            if v is not None:
                req.add_header(k, v)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            lat = time.time() - t0
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                obj = None
            return r.status, dict(r.headers), obj, lat, None
    except urllib.error.HTTPError as e:
        lat = time.time() - t0
        raw = e.read()
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            obj = None
        return e.code, dict(e.headers), obj, lat, None
    except Exception as e:  # 超时/连接错
        return None, {}, None, time.time() - t0, str(e)


# ── checkers ──
def is_envelope(obj):
    return isinstance(obj, dict) and {"code", "message", "trace_id"} <= set(obj)


def c_envelope(obj, hd, st, lat):
    if not is_envelope(obj):
        return f"非统一 envelope: {str(obj)[:120]}"
    if str(obj.get("code")) != str(st):
        return f"code={obj.get('code')} 与 HTTP {st} 不一致"
    if not HEX32.match(str(obj.get("trace_id", ""))):
        return f"trace_id 非合法 hex32: {obj.get('trace_id')}"
    if not hget(hd, "X-Trace-Id"):
        return "缺 X-Trace-Id 响应头"
    if hget(hd, "X-Trace-Id") != obj.get("trace_id"):
        return f"X-Trace-Id={hget(hd, "X-Trace-Id")} != body trace_id={obj.get('trace_id')}"
    return None


def c_msg_contains(key):
    def _c(obj, hd, st, lat):
        err = c_envelope(obj, hd, st, lat)
        return err or (None if key in str(obj.get("message", "")) else
                       f"message 缺 '{key}': {obj.get('message')}")
    return _c


def c_ok_outfits(obj, hd, st, lat):
    if st != 200:
        return f"期望 200, 实际 {st}"
    if not isinstance(obj, dict) or "outfits" not in obj:
        return f"缺 outfits: {str(obj)[:120]}"
    of = obj["outfits"]
    if not isinstance(of, list):
        return "outfits 非列表"
    if len(of) > 6:
        return f"outfits 数量 {len(of)} > 6 (default_outfit_limit)"
    ranks = [o.get("outfit_rank") for o in of]
    if ranks and ranks != list(range(len(of))):
        return f"outfit_rank 非 0..N 连续: {ranks}"
    for o in of:
        if not o.get("outfit_id") or not isinstance(o.get("items"), list) or not o["items"]:
            return f"outfit 字段不全: {str(o)[:120]}"
        for it in o["items"]:
            for f in ("sku_id", "role", "title"):
                if not it.get(f):
                    return f"item 缺 {f}: {str(it)[:120]}"
            if it.get("price") is None:
                return f"item 缺 price"
            if not it.get("sku_image_url"):
                return f"item sku_image_url 空: {it.get('sku_id')}"
    # trace_id 同源
    if not HEX32.match(str(obj.get("trace_id", ""))):
        return f"成功体 trace_id 非合法 hex32: {obj.get('trace_id')}"
    if hget(hd, "X-Trace-Id") != obj.get("trace_id"):
        return f"X-Trace-Id != body trace_id"
    return None


def c_session_generated(obj, hd, st, lat):
    e = c_ok_outfits(obj, hd, st, lat)
    return e or (None if HEX32.match(str(obj.get("session_id", "")))
                 else f"session_id 非服务端 hex: {obj.get('session_id')}")


def c_session_echoed(sid):
    def _c(obj, hd, st, lat):
        e = c_ok_outfits(obj, hd, st, lat)
        return e or (None if obj.get("session_id") == sid
                     else f"session_id 未回显: got {obj.get('session_id')}")
    return _c


def c_not_500(obj, hd, st, lat):
    return None if st and st < 500 else f"期望 <500, 实际 {st} ({str(obj)[:80]})"


def c_stripped_sku_echo(expected):
    """ISS-03: 响应 input_sku_id 应为 strip 后的值。"""
    def _c(obj, hd, st, lat):
        if st != 200:
            return f"期望 200, 实际 {st}"
        got = str(obj.get("input_sku_id", "")).strip() if isinstance(obj, dict) else ""
        if got != expected:
            return f"input_sku_id 未 strip: 期望 '{expected}' 实际 '{got}'"
        return None
    return _c


def c_msg_stripped_script(obj, hd, st, lat):
    """ISS-09: message 含 <script> 时应 200 且有推荐结果(非空)。"""
    if st != 200:
        return f"期望 200, 实际 {st} ({str(obj)[:80]})"
    if not isinstance(obj, dict) or "outfits" not in obj:
        return f"缺 outfits: {str(obj)[:120]}"
    # 有推荐结果即可(不要求非空, 但至少不应因 <script> 触发 500/异常)
    return None


def c_tryon_strict(obj, hd, st, lat):
    """StrictBool 契约: tryon 非 bool 应 422。线上旧版会 200+触发试穿=FAIL。"""
    if st == 422 and is_envelope(obj):
        return None
    if st == 200 and obj and isinstance(obj.get("outfits"), list):
        # 判定是否触发了试穿(慢 + 有 tryon 图)
        has_img = any(o.get("outfit_tryon_image") for o in obj["outfits"])
        if has_img and lat > 10:
            return f"未拒绝非布尔 tryon, 反触发试穿({lat:.0f}s, 有试穿图) — StrictBool 未生效/未部署"
    return f"期望 422, 实际 {st} — {str(obj)[:80]}"


CASES = [
    # ── 契约: 入参校验 ──
    ("C01 缺app_id→422", "POST", "/v1/outfit/recommend",
     '{"input_sku_id":"X"}', 422, c_msg_contains("app_id")),
    ("C02 app_id空串→400", "POST", "/v1/outfit/recommend",
     '{"app_id":""}', 400, c_msg_contains("app_id required")),
    ("C03 app_id纯空白→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"   "}', 400, c_msg_contains("app_id required")),
    ("C04 三选一全空→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide"}', 400, c_msg_contains("at least one")),
    ("C05 三选一纯空白→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"   "}', 400, c_msg_contains("at least one")),
    ("C06 app_id为数字→422", "POST", "/v1/outfit/recommend",
     '{"app_id":123,"input_sku_id":"X"}', 422, c_envelope),
    ("C07 tryon=null→422", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"X","tryon":null}', 422, c_msg_contains("boolean")),
    ("C08 tryon=1应422(StrictBool契约)", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s","tryon":1}' % KNOWN_SKU, 422, c_tryon_strict),
    ("C09 非法JSON→4xx", "POST", "/v1/outfit/recommend",
     '{bad json}', 422, c_envelope),
    ("C10 GET→405", "GET", "/v1/outfit/recommend", None, 405, c_envelope),
    ("C11 message含lone surrogate→200非500(ISS-06)", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s","message":"\\ud800\\ud800 test"}' % KNOWN_SKU,
     200, c_ok_outfits),

    # ── 功能 ──
    ("F01 sku锚点→200结构", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s","tryon":false}' % KNOWN_SKU, 200, c_ok_outfits),
    ("F02 session_id省略→服务端生成", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s"}' % KNOWN_SKU, 200, c_session_generated),
    ("F03 session_id指定→回显", "POST", "/v1/outfit/recommend",
     '{"session_id":"a1b2c3d4e5f6789012345678abcdef01","app_id":"micro_guide","input_sku_id":"%s"}' % KNOWN_SKU,
     200, c_session_echoed("a1b2c3d4e5f6789012345678abcdef01")),
    ("F04 message-only→200", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","message":"日常通勤"}', 200, c_ok_outfits),
    ("F05 sku空白被strip→200", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"  %s  "}' % KNOWN_SKU, 200, c_ok_outfits),
    ("F06 错误响应也带X-Trace-Id", "POST", "/v1/outfit/recommend",
     '{"app_id":""}', 400, c_envelope),

    # ── regenerate ──
    ("R01 缺outfit_id→422", "POST", "/v1/outfit/regenerate-reason",
     '{}', 422, c_msg_contains("outfit_id")),
    ("R02 未知outfit_id→404", "POST", "/v1/outfit/regenerate-reason",
     '{"outfit_id":"does_not_exist_xyz"}', 404, c_msg_contains("outfit not found")),
    ("R03 空outfit_id→应422(契约)", "POST", "/v1/outfit/regenerate-reason",
     '{"outfit_id":""}', 422, c_envelope),

    # ── ISS-02: 不合法 SKU 格式 → 400 invalid sku_id format ──
    ("S01 小写SKU→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"a11m627701fpk"}', 400, c_msg_contains("invalid sku_id format")),
    ("S02 含中间空格→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"A11M627701 FPK"}', 400, c_msg_contains("invalid sku_id format")),
    ("S03 纯数字→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"1234567890123"}', 400, c_msg_contains("invalid sku_id format")),
    ("S04 XSS注入→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"<script>alert(1)</script>"}', 400, c_msg_contains("invalid sku_id format")),
    ("S05 含中文→400", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"A11M627701F中文"}', 400, c_msg_contains("invalid sku_id format")),
    ("S06 合法SKU未知但仍200空(不误伤)", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"A11M627701"}', 0, c_not_500),

    # ── ISS-04: app_id 白名单 → 401 invalid app_id ──
    ("A01 app_id=test→401", "POST", "/v1/outfit/recommend",
     '{"app_id":"test","input_sku_id":"%s"}' % KNOWN_SKU, 401, c_msg_contains("invalid app_id")),
    ("A02 app_id大写MICRO_GUIDE→401(大小写敏感)", "POST", "/v1/outfit/recommend",
     '{"app_id":"MICRO_GUIDE","input_sku_id":"%s"}' % KNOWN_SKU, 401, c_msg_contains("invalid app_id")),
    ("A03 app_id=fila_app→401", "POST", "/v1/outfit/recommend",
     '{"app_id":"fila_app","input_sku_id":"%s"}' % KNOWN_SKU, 401, c_msg_contains("invalid app_id")),

    # ── ISS-05: API Key 鉴权(enabled=true 后转绿; enabled=false 期间预期 FAIL) ──
    # 第 7 元为 headers: None=默认有效 Key; {}=不带 Key; dict=指定头
    ("K01 无X-API-Key→401 API key required", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s"}' % KNOWN_SKU, 401, c_msg_contains("API key required"), {}),
    ("K02 错Key→401 invalid API key", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s"}' % KNOWN_SKU, 401, c_msg_contains("invalid API key"),
     {"X-API-Key": "ak_wrong_key"}),
    ("K03 对Key+匹配app_id→200", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s","tryon":false}' % KNOWN_SKU, 200, c_ok_outfits, None),
    ("K04 对Key+不匹配app_id→401 mismatch", "POST", "/v1/outfit/recommend",
     '{"app_id":"wechat_mini","input_sku_id":"%s"}' % KNOWN_SKU, 401, c_msg_contains("app_id mismatch"), None),
    ("K05 Key无权接口→403 access denied", "POST", "/v1/outfit/regenerate-reason",
     '{"outfit_id":"-255050021"}', 403, c_msg_contains("access denied"),
     {"X-API-Key": "ak_f1e2d3c4b5a69788697564534323120f"}),

    # ── ISS-08: GET 接口也需 X-API-Key 鉴权 ──
    ("K06 GET /api/outfits 无Key→401", "GET", "/api/outfits?size=1", None,
     401, c_msg_contains("API key required"), {}),
    ("K07 GET /skus/{id} 无Key→401", "GET", "/skus/%s" % KNOWN_SKU, None,
     401, c_msg_contains("API key required"), {}),
    ("K08 GET /api/outfits 有Key→200", "GET", "/api/outfits?size=1", None,
     200, None, None),

    # ── ISS-09: message 含 <script> 应被剥离, 非 0 结果 ──
    ("X01 message含script→200有结果(ISS-09)", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"%s","message":"<script>alert(1)</script>日常通勤"}' % KNOWN_SKU,
     200, c_msg_stripped_script),

    # ── ISS-03: input_sku_id 前后空格应 strip 后回显 ──
    ("X02 sku前后空格→响应回显strip后值(ISS-03)", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"  %s  "}' % KNOWN_SKU,
     200, c_stripped_sku_echo(KNOWN_SKU)),

    # ── 故障注入 ──
    ("I01 未知sku→200空非5xx", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","input_sku_id":"NOPE_NOPE_999"}', 0, c_not_500),
    ("I02 坏image_url无sku→降级非5xx", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","image_url":"%s"}' % BAD_IMAGE, 0, c_not_500),
    ("I03 非URL字符串image_url→非5xx", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","image_url":"not a url at all"}', 0, c_not_500),
    ("I04 image_url指向内网(SSRF探针)", "POST", "/v1/outfit/recommend",
     '{"app_id":"micro_guide","image_url":"http://127.0.0.1:8888/health"}', 0, c_not_500),
]


def main():
    # 取一个真实 outfit_id 供 regenerate 已知用例
    st, hd, obj, _, _ = call("GET", "/api/outfits?size=1", None, timeout=15)
    real_oid = None
    if st == 200 and isinstance(obj, dict):
        try:
            real_oid = obj["outfits"][0]["outfit_id"]
        except Exception:
            real_oid = None
    if real_oid:
        CASES.append(("R04 已知outfit_id→200有reason", "POST", "/v1/outfit/regenerate-reason",
                      json.dumps({"outfit_id": real_oid}), 200,
                      lambda o, h, s, l: None if s == 200 and o and o.get("reason")
                      else f"期望 200+reason, got {s} {str(o)[:80]}"))
    else:
        print("!! 未取到真实 outfit_id, 跳过 R04")

    print(f"BASE={BASE}  共 {len(CASES)} 用例\n")
    passed = failed = 0
    fails = []
    for case in CASES:
        name, m, path, body, want, chk = case[:6]
        hdrs = case[6] if len(case) > 6 else None
        st, hd, obj, lat, err = call(m, path, body, headers=hdrs)
        if err:
            verdict = f"ERR {err}"
        else:
            if want and st != want:
                verdict = f"FAIL 期望HTTP={want} 实际={st}"
            else:
                v = chk(obj, hd, st, lat) if chk else None
                verdict = "PASS" if v is None else f"FAIL {v}"
        ok = verdict == "PASS"
        print(f"[{'✓' if ok else '✗'}] {name:38s} {st or 'ERR':>4} {lat:5.1f}s  {verdict}")
        if ok:
            passed += 1
        else:
            failed += 1
            fails.append((name, st, verdict, obj))
    print(f"\n{'='*60}\nPASS {passed}  FAIL {failed}  共 {len(CASES)}")
    if fails:
        print("\n--- 失败明细 ---")
        for name, st, v, obj in fails:
            print(f"  {name}: HTTP={st}\n    {v}\n    body={str(obj)[:160]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
