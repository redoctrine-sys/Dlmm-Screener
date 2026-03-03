"""
Vercel Serverless Function — DLMM + DAMM Ghost Pool Screener
Endpoint: GET /api/screener?type=dlmm|damm|steal|all
"""

import json
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from collections import defaultdict

SOL_MINT = "So11111111111111111111111111111111111111112"

CONFIG = {
    "tvl_min": 5_000,
    "tvl_max": 150_000,
    "vol_tvl_min": 0.08,
    "vol_tvl_max": 1.50,
    "vol_mcap_min": 0.5,
    "vol_mcap_max": 15.0,
    "mcap_tvl_min": 30,
    "mcap_tvl_max": 1000,
    "ghost_score_min": 38,
    "steal_threshold_pct": 30,
    "max_dlmm": 300,
    "max_damm": 200,
}

# ─── HTTP helper (no external deps) ─────────────────────────────
def http_get(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DLMM-Screener/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

# ─── Fetch APIs ──────────────────────────────────────────────────
def fetch_dlmm():
    all_pairs = []
    page = 0
    while len(all_pairs) < CONFIG["max_dlmm"]:
        data = http_get(
            f"https://dlmm-api.meteora.ag/pair/all_with_pagination?page={page}&limit=100&sort_key=volume&order_by=desc"
        )
        if not data:
            break
        pairs = data.get("pairs", data) if isinstance(data, dict) else data
        if not pairs:
            break
        all_pairs.extend(pairs)
        if len(pairs) < 100:
            break
        page += 1
    return all_pairs

def fetch_damm_v2():
    all_pools = []
    page = 1
    while len(all_pools) < CONFIG["max_damm"]:
        data = http_get(
            f"https://damm-v2.datapi.meteora.ag/pools?page={page}&limit=100&sort_key=volume&order_by=desc"
        )
        if not data:
            break
        pools = data.get("data", []) if isinstance(data, dict) else data
        if not pools:
            break
        for p in pools:
            p["_pool_type"] = "DAMM_V2"
        all_pools.extend(pools)
        total_pages = data.get("pages", 1) if isinstance(data, dict) else 1
        if page >= total_pages or len(pools) < 100:
            break
        page += 1
    return all_pools

def fetch_token_data(mint):
    result = {"price": 0, "high_24h": 0, "low_24h": 0, "mcap": 0,
              "volume_24h_usd": 0, "symbol": "", "price_change_24h": 0}
    data = http_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if data:
        pairs = data.get("pairs", [])
        if pairs:
            best = max(pairs, key=lambda x: float((x.get("volume") or {}).get("h24", 0) or 0))
            result["symbol"] = (best.get("baseToken") or {}).get("symbol", "")
            result["price"] = float(best.get("priceUsd", 0) or 0)
            result["mcap"] = float(best.get("marketCap", 0) or 0)
            result["volume_24h_usd"] = float((best.get("volume") or {}).get("h24", 0) or 0)
            result["price_change_24h"] = float((best.get("priceChange") or {}).get("h24", 0) or 0)
            if result["price"] > 0 and result["price_change_24h"] != 0:
                chg = result["price_change_24h"] / 100
                prev = result["price"] / (1 + chg) if chg != -1 else result["price"]
                result["high_24h"] = result["price"] if chg > 0 else prev
                result["low_24h"] = prev if chg > 0 else result["price"]
    if not result["price"]:
        jup = http_get(f"https://price.jup.ag/v6/price?ids={mint}")
        if jup:
            td = (jup.get("data") or {}).get(mint, {})
            result["price"] = float(td.get("price", 0) or 0)
    return result

# ─── Normalize ───────────────────────────────────────────────────
def normalize_dlmm(p):
    mx, my = p.get("mint_x",""), p.get("mint_y","")
    token = my if mx == SOL_MINT else mx
    return {
        "_pool_type": "DLMM",
        "address": p.get("address",""),
        "name": p.get("name",""),
        "token_mint": token,
        "mint_x": mx, "mint_y": my,
        "tvl": float(p.get("liquidity", 0) or 0),
        "volume_24h": float(p.get("trade_volume_24h", 0) or 0),
        "fee_tier_pct": float(p.get("base_fee_percentage", 0.3) or 0.3),
        "bin_step": int(p.get("bin_step", 0) or 0),
        "is_sol_pair": mx == SOL_MINT or my == SOL_MINT,
    }

def normalize_damm(p):
    ptype = p.get("_pool_type", "DAMM_V2")
    ma = p.get("token_a_mint") or p.get("mint_x") or (p.get("token_a") or {}).get("address","")
    mb = p.get("token_b_mint") or p.get("mint_y") or (p.get("token_b") or {}).get("address","")
    token = mb if ma == SOL_MINT else ma
    tvl = float(p.get("liquidity") or p.get("total_liquidity") or p.get("tvl") or 0)
    vol_raw = p.get("volume", {})
    vol = float(vol_raw.get("24h") or vol_raw.get("h24") or 0) if isinstance(vol_raw, dict) else float(vol_raw or 0)
    cfg = p.get("pool_config") or p.get("config") or {}
    fee = float((cfg.get("base_fee_pct") or cfg.get("fee_pct") or 0.25) if isinstance(cfg, dict) else 0.25)
    name_a = ((p.get("token_a") or {}).get("symbol") or ma[:6])
    name_b = ((p.get("token_b") or {}).get("symbol") or mb[:6])
    return {
        "_pool_type": ptype,
        "address": p.get("address") or p.get("pool_address") or "",
        "name": p.get("name") or f"{name_a}/{name_b}",
        "token_mint": token, "mint_x": ma, "mint_y": mb,
        "tvl": tvl, "volume_24h": vol,
        "fee_tier_pct": fee, "bin_step": 0,
        "is_sol_pair": ma == SOL_MINT or mb == SOL_MINT,
    }

# ─── Ghost Score ─────────────────────────────────────────────────
def ghost_score(vt, vm, mt, vol, apr, ptype):
    s1 = 25 if 0.15<=vt<=0.60 else 18 if 0.60<vt<=1.0 else 12 if 0.08<=vt<0.15 else 10 if vt>1.0 else 0
    s2 = 20 if 2<=vm<=6 else 14 if 1<=vm<2 else 12 if 6<vm<=10 else 7 if 0.5<=vm<1 else 6 if 10<vm<=15 else 0
    s3 = 20 if 80<=mt<=300 else 14 if 50<=mt<80 else 12 if 300<mt<=600 else 8 if 30<=mt<50 else 7 if 600<mt<=1000 else 0
    s4 = 20 if 10<=vol<=20 else 16 if 8<=vol<10 else 14 if 20<vol<=28 else 10 if 6<=vol<8 else 8 if 28<vol<=35 else 0
    s5 = 15 if apr>=300 else 12 if apr>=150 else 9 if apr>=80 else 6 if apr>=50 else 3 if apr>=30 else 0
    total = min(100, s1+s2+s3+s4+s5 + (5 if ptype.startswith("DAMM") and s1+s2+s3+s4+s5>=40 else 0))
    label = "✅ MASUK" if total>=70 else "✅ BUY" if total>=55 else "👀 WATCH" if total>=40 else "❌ SKIP"
    bin_r = "100 bps" if vol>25 else "50 bps" if vol>15 else "25 bps ⭐" if vol>8 else "10 bps"
    return {"total": total, "label": label, "bin_rec": bin_r,
            "scores": {"vol_tvl": s1, "vol_mcap": s2, "mcap_tvl": s3, "volatility": s4, "fee_apr": s5}}

# ─── Analyze ─────────────────────────────────────────────────────
def analyze(pool_norm, token_cache):
    if not pool_norm["is_sol_pair"]:
        return None
    tvl = pool_norm["tvl"]
    vol = pool_norm["volume_24h"]
    if tvl < CONFIG["tvl_min"] or tvl > CONFIG["tvl_max"]:
        return None
    vt = vol / tvl if tvl > 0 else 0
    if vt < CONFIG["vol_tvl_min"] or vt > CONFIG["vol_tvl_max"]:
        return None

    tmint = pool_norm["token_mint"]
    if tmint and tmint not in token_cache:
        token_cache[tmint] = fetch_token_data(tmint)

    td = token_cache.get(tmint, {})
    price = td.get("price", 0)
    high = td.get("high_24h", 0)
    low = td.get("low_24h", 0)
    mcap = td.get("mcap", 0)
    symbol = td.get("symbol", "") or pool_norm["name"]

    dex_vol = td.get("volume_24h_usd", 0)
    if dex_vol > vol:
        vol = dex_vol

    vt   = vol / tvl if tvl > 0 else 0
    vm   = (vol / mcap * 100) if mcap > 0 else 0
    mt   = mcap / tvl if tvl > 0 else 0
    volat = ((high - low) / price * 100) if price > 0 and high > 0 else 0
    fee_pct = pool_norm["fee_tier_pct"] / 100
    apr  = (vol * fee_pct / tvl) * 365 * 100 if tvl > 0 else 0

    if mcap > 0:
        if not (CONFIG["vol_mcap_min"] <= vm <= CONFIG["vol_mcap_max"]): return None
        if not (CONFIG["mcap_tvl_min"] <= mt <= CONFIG["mcap_tvl_max"]): return None

    sc = ghost_score(vt, vm, mt, volat, apr, pool_norm["_pool_type"])
    if sc["total"] < CONFIG["ghost_score_min"]:
        return None

    return {
        "pool_type": pool_norm["_pool_type"],
        "pool_address": pool_norm["address"],
        "name": symbol,
        "pair_name": pool_norm["name"],
        "token_mint": tmint,
        "bin_step": pool_norm["bin_step"],
        "fee_tier_pct": pool_norm["fee_tier_pct"],
        "tvl": round(tvl, 2),
        "volume_24h": round(vol, 2),
        "mcap": round(mcap, 2),
        "price": price,
        "price_change_24h": td.get("price_change_24h", 0),
        "vol_tvl": round(vt, 4),
        "vol_mcap_pct": round(vm, 2),
        "mcap_tvl": round(mt, 1),
        "volatility_idx": round(volat, 1),
        "fee_apr_pct": round(apr, 1),
        "est_daily_fee": round(vol * fee_pct, 2),
        "ghost_score": sc["total"],
        "score_detail": sc["scores"],
        "action": sc["label"],
        "bin_rec": sc["bin_rec"],
        "steal_opp": "",
    }

# ─── Steal finder ─────────────────────────────────────────────────
def find_steal(results):
    by_token = defaultdict(lambda: {"DLMM": [], "DAMM": []})
    for r in results:
        key = "DLMM" if r["pool_type"] == "DLMM" else "DAMM"
        by_token[r["token_mint"]][key].append(r)

    opps = []
    for tmint, pools in by_token.items():
        damm = pools["DAMM"]
        dlmm = pools["DLMM"]
        if not damm:
            continue
        damm_tvl = sum(p["tvl"] for p in damm)
        dlmm_tvl = sum(p["tvl"] for p in dlmm)
        if damm_tvl < 10_000:
            continue
        ratio = (dlmm_tvl / damm_tvl * 100) if damm_tvl > 0 else 0
        if ratio <= CONFIG["steal_threshold_pct"]:
            best = max(damm, key=lambda x: x["tvl"])
            if ratio == 0:   strength = "🔥🔥 NO DLMM — ambil semua!"
            elif ratio <= 10: strength = "🔥🔥 DLMM sangat tipis"
            elif ratio <= 20: strength = "🔥 Steal opportunity kuat"
            else:             strength = "⚡ Worth dipertimbangkan"
            vol = best.get("volatility_idx", 0)
            range_r = "±15-20%" if vol>20 else "±8-12%" if vol>10 else "±5-8%"
            for p in damm:
                p["steal_opp"] = strength
            opps.append({
                "token_mint": tmint,
                "token_name": best["name"],
                "pair_name": best["pair_name"],
                "damm_tvl": round(damm_tvl, 2),
                "dlmm_tvl": round(dlmm_tvl, 2),
                "ratio_pct": round(ratio, 1),
                "volume_24h": best["volume_24h"],
                "est_daily_fee": round(best["volume_24h"] * best["fee_tier_pct"] / 100, 2),
                "fee_apr_pct": best["fee_apr_pct"],
                "ghost_score": best["ghost_score"],
                "price": best["price"],
                "price_change_24h": best.get("price_change_24h", 0),
                "volatility": best["volatility_idx"],
                "best_damm_addr": best["pool_address"],
                "damm_type": best["pool_type"],
                "steal_strength": strength,
                "bin_rec": best["bin_rec"],
                "range_rec": range_r,
            })
    opps.sort(key=lambda x: x["ratio_pct"])
    return opps

# ─── Main handler ────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        # CORS headers — penting agar frontend bisa akses
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=300")  # cache 5 menit di Vercel
        self.end_headers()

        try:
            # Parse query param ?type=
            query = ""
            if "?" in self.path:
                query = self.path.split("?")[1]
            params = {}
            for part in query.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v
            req_type = params.get("type", "all")

            # Fetch & normalize
            token_cache = {}
            results = []

            if req_type in ("dlmm", "all", "steal"):
                raw_dlmm = fetch_dlmm()
                for p in raw_dlmm:
                    norm = normalize_dlmm(p)
                    r = analyze(norm, token_cache)
                    if r:
                        results.append(r)

            if req_type in ("damm", "all", "steal"):
                raw_damm = fetch_damm_v2()
                for p in raw_damm:
                    norm = normalize_damm(p)
                    r = analyze(norm, token_cache)
                    if r:
                        results.append(r)

            results.sort(key=lambda x: x["ghost_score"], reverse=True)
            steal_opps = find_steal(results) if req_type in ("all", "steal") else []

            response = {
                "status": "ok",
                "timestamp": int(time.time()),
                "total": len(results),
                "dlmm_count": sum(1 for r in results if r["pool_type"] == "DLMM"),
                "damm_count": sum(1 for r in results if r["pool_type"] != "DLMM"),
                "steal_count": len(steal_opps),
                "pools": results,
                "steal_opps": steal_opps,
            }
        except Exception as e:
            response = {"status": "error", "message": str(e), "pools": [], "steal_opps": []}

        self.wfile.write(json.dumps(response).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, *args):
        pass  # Suppress default logs
