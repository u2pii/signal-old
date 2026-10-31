# -*- coding: utf-8 -*-
"""山寨币风险扫描
   输出 alt_watch.json：风险名单（别碰）+ 观察名单（有异动）

   依据实证结论（1142 个暴涨事件）：
   - 崩盘组的共同点：暴涨日涨幅高、前 20 日已累计涨很多、但量能倍数低
   - 做空这些币无正期望（PF≈1.0），所以只做预警，不给交易信号
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import statistics
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "alt_watch.json")
CACHE = os.path.join(HERE, "cache_scan")
os.makedirs(CACHE, exist_ok=True)
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
BASE = "https://api.gateio.ws/api/v4"

BAD_SUFFIX = ("5L", "5S", "4L", "4S", "3L", "3S", "2L", "2S")
BAD_NAMES = {"USDC", "USDT", "FDUSD", "TUSD", "DAI", "USD1", "BUSD", "PYUSD",
             "XAUT", "PAXG", "EURT", "USDD", "USDE", "RLUSD", "USDY", "USTC",
             "GYEN", "XSGD", "BUIDL", "USDH", "USDX", "EURC", "AEUR"}

# 已上线的主流币，单独处理，不进山寨名单
MAJOR = {"BTC", "ETH", "SOL", "BNB", "DOGE", "ADA", "XRP", "TRX", "AVAX",
         "LINK", "DOT", "MATIC", "LTC", "BCH", "SHIB", "UNI", "ATOM", "ETC"}


def get(url, retries=3, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(4 if e.code == 429 else 1.2)
        except Exception as e:
            last = e
            time.sleep(1.2)
    raise last


def daily(pair, limit=60):
    fn = os.path.join(CACHE, pair.replace("/", "_") + ".json")
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 3600 * 6:
        try:
            with open(fn, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    raw = get(f"{BASE}/spot/candlesticks?currency_pair={pair}&interval=1d&limit={limit}")
    bars = [{"t": int(r[0]), "c": float(r[2]), "h": float(r[3]),
             "l": float(r[4]), "o": float(r[5]), "v": float(r[1])} for r in raw]
    bars.sort(key=lambda x: x["t"])
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(bars, f)
    return bars


def ok_pair(pair):
    # Gate 的 currency_pair 形如 NEAR5S_USDT（下划线，不是斜杠）
    base = pair.replace("_USDT", "")
    if base in BAD_NAMES or base in MAJOR:
        return False
    for suf in BAD_SUFFIX:
        if base.endswith(suf):
            return False
    # 排除形如 XXX3L / XXX5S 的杠杆代币（含数字+字母结尾）
    if len(base) > 2 and base[-2].isdigit() and base[-1] in "LS":
        return False
    return True


def analyze(bars):
    """返回指标 dict，或 None（数据不足）"""
    if len(bars) < 40:
        return None
    cur = bars[-1]["c"]
    if cur <= 0:
        return None

    # 最近 5 天里最大的单日涨幅
    recent = bars[-6:]
    max_day = 0
    max_day_idx = None
    for i in range(1, len(recent)):
        prev = recent[i - 1]["c"]
        if prev > 0:
            r = recent[i]["c"] / prev - 1
            if r > max_day:
                max_day = r
                max_day_idx = len(bars) - len(recent) + i

    # 20 日累计涨幅
    c20 = bars[-21]["c"] if len(bars) >= 21 else bars[0]["c"]
    prior20 = cur / c20 - 1 if c20 > 0 else 0

    # 暴涨日量能倍数（相对暴涨前 20 日均量）
    vol_ratio = 0
    if max_day_idx and max_day_idx >= 21:
        base_v = statistics.mean([b["v"] for b in bars[max_day_idx - 20:max_day_idx]]) or 1
        vol_ratio = bars[max_day_idx]["v"] / base_v

    # 当前量能（最近 3 天 / 前 20 天）
    cur_vol = statistics.mean([b["v"] for b in bars[-3:]]) or 0
    avg_vol = statistics.mean([b["v"] for b in bars[-23:-3]]) or 1
    now_vol_ratio = cur_vol / avg_vol

    # 波动率（20 日日均振幅）
    amp = statistics.mean([(b["h"] - b["l"]) / b["c"] if b["c"] > 0 else 0 for b in bars[-20:]])

    # 距 20 日高/低
    hh = max(b["h"] for b in bars[-21:-1]) if len(bars) >= 22 else max(b["h"] for b in bars[:-1])
    ll = min(b["l"] for b in bars[-21:-1]) if len(bars) >= 22 else min(b["l"] for b in bars[:-1])
    to_high = cur / hh - 1 if hh > 0 else 0

    # 5 天内从高点回落多少
    high5 = max(b["h"] for b in bars[-5:])
    drawdown5 = cur / high5 - 1 if high5 > 0 else 0

    return {
        "cur": cur, "max_day": max_day, "prior20": prior20,
        "vol_ratio": vol_ratio, "now_vol_ratio": now_vol_ratio,
        "amp": amp, "to_high": to_high, "drawdown5": drawdown5,
        "vol24h": bars[-1]["v"] * cur,
    }


def risk_score(m):
    """风险评分：越高越危险（0~100）"""
    s = 0
    # 涨得猛
    if m["max_day"] >= 0.50: s += 30
    elif m["max_day"] >= 0.30: s += 20
    elif m["max_day"] >= 0.15: s += 8
    # 之前已涨很多
    if m["prior20"] >= 1.00: s += 30
    elif m["prior20"] >= 0.50: s += 20
    elif m["prior20"] >= 0.20: s += 10
    # 量能不足（关键：崩盘组量能 1.32 vs 1.94）
    if m["vol_ratio"] > 0 and m["vol_ratio"] < 1.5: s += 25
    elif m["vol_ratio"] < 2.5: s += 15
    elif m["vol_ratio"] < 5: s += 5
    # 已经从高点回落（说明开始崩了）
    if m["drawdown5"] <= -0.30: s += 15
    elif m["drawdown5"] <= -0.15: s += 8
    return min(s, 100)


def watch_score(m):
    """观察评分：量能异动但还没拉，可能突破"""
    s = 0
    if m["now_vol_ratio"] >= 5: s += 40
    elif m["now_vol_ratio"] >= 3: s += 28
    elif m["now_vol_ratio"] >= 2: s += 15
    # 还没大涨
    if abs(m["prior20"]) < 0.25: s += 20
    # 靠近 20 日高点
    if m["to_high"] > -0.03: s += 25
    elif m["to_high"] > -0.08: s += 15
    # 振幅够大说明有资金
    if m["amp"] >= 0.08: s += 15
    elif m["amp"] >= 0.05: s += 8
    # 风险太高就不算观察对象
    return min(s, 100)


def short_setups(scanned, bars_map):
    """可做空信号：最近 2 天内单日涨 ≥30%，次日做空
       回测：胜率 69%，中位 +20.6%，t=4.55（持有20天，1x，不设止损）
    """
    out = []
    for m in scanned:
        bars = bars_map.get(m["pair"])
        if not bars or len(bars) < 30:
            continue
        # 检查最近 2 根日线是否有暴涨
        hit = None
        for k in (1, 2):
            idx = len(bars) - k
            if idx < 1:
                continue
            prev = bars[idx - 1]["c"]
            if prev <= 0:
                continue
            r = bars[idx]["c"] / prev - 1
            if r >= 0.30:
                hit = {"days_ago": k - 1, "pct": r * 100,
                       "date": datetime.fromtimestamp(bars[idx]["t"], TZ).strftime("%m-%d")}
                break
        if not hit:
            continue
        if m["amp"] < 0.015:
            continue
        out.append({
            "sym": m["sym"], "price": m["cur"],
            "pump_pct": round(hit["pct"], 1),
            "pump_date": hit["date"],
            "days_ago": hit["days_ago"],
            "prior20": round(m["prior20"] * 100, 1),
            "vol_ratio": round(m["vol_ratio"], 2),
            "qv": round(m["qv"] / 10000, 0),
            "risk": m["risk"],
        })
    out.sort(key=lambda x: -x["pump_pct"])
    return out


def main():
    print("拉取交易对...", flush=True)
    tickers = get(f"{BASE}/spot/tickers")
    rows = []
    for t in tickers:
        p = t.get("currency_pair", "")
        if not p.endswith("_USDT") or not ok_pair(p):
            continue
        try:
            qv = float(t.get("quote_volume") or 0)
        except Exception:
            qv = 0
        if qv >= 300000:          # 24h 成交额 ≥ 30 万 U
            rows.append({"pair": p, "qv": qv})
    rows.sort(key=lambda x: -x["qv"])
    rows = rows[:200]
    print(f"扫描 {len(rows)} 个山寨币（24h成交额>30万U）\n", flush=True)

    scanned = []
    bars_map = {}
    for i, r in enumerate(rows):
        pair = r["pair"]
        try:
            bars = daily(pair)
            bars_map[pair] = bars
            m = analyze(bars)
            if not m:
                continue
            m["pair"] = pair
            m["sym"] = pair.replace("_USDT", "")
            m["qv"] = r["qv"]
            # 稳定币/低波动币排除（20日平均振幅 < 1.5%）
            if m["amp"] < 0.015:
                continue
            m["risk"] = risk_score(m)
            m["watch"] = watch_score(m)
            scanned.append(m)
        except Exception:
            pass
        if (i + 1) % 50 == 0:
            print(f"  已处理 {i+1}/{len(rows)}", flush=True)
        time.sleep(0.08)

    print(f"有效 {len(scanned)} 个\n")

    # 风险名单：风险分 ≥ 55，且最近 10 天内有过大涨
    risky = [m for m in scanned if m["risk"] >= 55 and m["max_day"] >= 0.20]
    risky.sort(key=lambda x: -x["risk"])

    # 观察名单：观察分 ≥ 45，风险分 < 50，且确实靠近 20 日高点（-10% 内）
    watch = [m for m in scanned if m["watch"] >= 45 and m["risk"] < 50 and m["to_high"] > -0.10]
    watch.sort(key=lambda x: -x["watch"])

    def slim(m):
        return {
            "sym": m["sym"], "price": m["cur"],
            "max_day": round(m["max_day"] * 100, 1),
            "prior20": round(m["prior20"] * 100, 1),
            "vol_ratio": round(m["vol_ratio"], 2),
            "now_vol": round(m["now_vol_ratio"], 2),
            "to_high": round(m["to_high"] * 100, 1),
            "drawdown5": round(m["drawdown5"] * 100, 1),
            "amp": round(m["amp"] * 100, 1),
            "qv": round(m["qv"] / 10000, 0),
            "risk": m["risk"], "watch": m["watch"],
        }

    payload = {
        "updated": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "scanned": len(scanned),
        "short": short_setups(scanned, bars_map)[:10],
        "risky": [slim(m) for m in risky[:15]],
        "watch": [slim(m) for m in watch[:15]],
        "note": "风险分高 = 涨得猛+前期已涨+量能不足。观察分高 = 放量但未大涨。做空信号 = 最近2天单日涨≥30%，回测胜率69%/中位+20.6%/t=4.55（1x、不设止损、持20天）。",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"风险名单 {len(risky)} 个，观察名单 {len(watch)} 个")
    print("\n--- 风险最高的 8 个 ---")
    for m in risky[:8]:
        print(f"  {m['sym']:<10} 风险{m['risk']:>3}  单日最大涨 {m['max_day']*100:>6.1f}%  "
              f"20日涨 {m['prior20']*100:>7.1f}%  量能 {m['vol_ratio']:>5.2f}x  "
              f"距5日高点 {m['drawdown5']*100:>6.1f}%")
    print("\n--- 观察名单前 8 个 ---")
    for m in watch[:8]:
        print(f"  {m['sym']:<10} 观察{m['watch']:>3}  当前量能 {m['now_vol_ratio']:>5.2f}x  "
              f"距20日高 {m['to_high']*100:>6.1f}%  振幅 {m['amp']*100:>5.1f}%  "
              f"20日涨 {m['prior20']*100:>7.1f}%")
    print(f"\n已写入 {OUT}")


if __name__ == "__main__":
    main()
