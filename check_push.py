# -*- coding: utf-8 -*-
"""交易信号每日检查 + 微信推送
   只用公开行情（币安 data-api），不涉及任何账户接口。
   有新信号才推，不重复打扰。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "alert_state.json")
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

CFG = {
    "N": 20,
    "K_SL": 1.5,
    "M_TRAIL": 3.0,
    "RISK": 0.08,
    "CAPITAL": 97.66,
    "MAX_LEV": 10,
    "MAX_POS": 2,
    "COINS": ["SOL", "DOGE", "BNB", "ADA", "BTC", "ETH"],
}

STEP = {"BTC": 0.001, "ETH": 0.001, "SOL": 0.01, "BNB": 0.01, "DOGE": 1, "ADA": 1}
MIN_NOTIONAL = 5


def get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def klines(sym):
    raw = get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}USDT&interval=1d&limit=200")
    b = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in raw]
    b.sort(key=lambda x: x["t"])
    return b


def atr14(bars, n=14):
    trs, a = [], None
    for i, b in enumerate(bars):
        if i == 0:
            v = b["h"] - b["l"]
        else:
            v = max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        trs.append(v)
        if i == n - 1:
            a = sum(trs[:n]) / n
        elif i >= n:
            a = (a * (n - 1) + v) / n
    return a


def scan():
    rows = []
    for sym in CFG["COINS"]:
        try:
            bars = klines(sym)
            closed = bars[:-1]
            cur = bars[-1]["c"]
            seg = closed[-CFG["N"] - 1:-1]          # 关键：排除当前根
            hh = max(b["h"] for b in seg)
            ll = min(b["l"] for b in seg)
            atr = atr14(closed)
            sig = "L" if cur > hh else ("S" if cur < ll else None)
            rows.append({"sym": sym, "cur": cur, "hh": hh, "ll": ll, "atr": atr, "sig": sig})
        except Exception as e:
            rows.append({"sym": sym, "err": str(e)[:40]})
    return rows


def size(s):
    s["stop"] = s["cur"] - CFG["K_SL"] * s["atr"] if s["sig"] == "L" else s["cur"] + CFG["K_SL"] * s["atr"]
    dist = abs(s["cur"] - s["stop"]) / s["cur"]
    cap = CFG["CAPITAL"]
    target = cap * CFG["RISK"]
    notional = min(target / dist, cap * CFG["MAX_LEV"])
    lev = max(1, min(CFG["MAX_LEV"], int(-(-notional // cap))))
    step = STEP.get(s["sym"], 0.001)
    dec = len(str(step).split(".")[1]) if "." in str(step) else 0
    qty = round(int((notional / s["cur"]) / step) * step, dec)
    notional = qty * s["cur"]
    s.update({"lev": lev, "qty": qty, "notional": notional,
              "risk": notional * dist, "dist": dist * 100,
              "margin": notional / lev, "tooSmall": notional < MIN_NOTIONAL})
    return s


def fmt(p):
    if p >= 1000: return f"{p:.1f}"
    if p >= 10: return f"{p:.2f}"
    if p >= 1: return f"{p:.3f}"
    return f"{p:.5f}"


def qstr(s):
    step = STEP.get(s["sym"], 0.001)
    dec = len(str(step).split(".")[1]) if "." in str(step) else 0
    return f"{s['qty']:.{dec}f}"


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def push(token, title, content):
    body = json.dumps({"token": token, "title": title, "content": content,
                       "template": "html"}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def main():
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    force = "--force" in sys.argv
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    rows = scan()
    sigs = [size(r) for r in rows if r.get("sig") and not r.get("err")]
    sigs.sort(key=lambda x: CFG["COINS"].index(x["sym"]))
    sigs = sigs[:CFG["MAX_POS"]]

    # 保证金检查
    keep, dropped, used = [], [], 0.0
    for s in sigs:
        if used + s["margin"] <= CFG["CAPITAL"] * 0.95 and not s["tooSmall"]:
            used += s["margin"]
            keep.append(s)
        else:
            dropped.append(s)

    old = load_state()
    new_state = {}
    fresh = []
    for s in keep:
        new_state[s["sym"]] = {"sig": s["sig"], "date": today, "entry": round(s["cur"], 6)}
        prev = old.get(s["sym"])
        if force or not prev or prev.get("sig") != s["sig"]:
            fresh.append(s)

    gone = [k for k in old if k not in new_state]

    print(f"扫描完成 {today}：信号 {len(keep)} 个，新增 {len(fresh)} 个，失效 {len(gone)} 个")

    if not fresh and not gone and not force:
        print("无新变化，不推送")
        save_state(new_state)
        return

    # 组装推送
    parts = []
    if fresh:
        cards = []
        for s in fresh:
            d = "做多" if s["sig"] == "L" else "做空"
            arrow = "↗" if s["sig"] == "L" else "↘"
            cards.append(
                f"<div style='border-left:3px solid #19c07a;background:#0d1a14;padding:10px 13px;"
                f"border-radius:8px;margin-bottom:10px'>"
                f"<div style='font-size:17px;font-weight:700;color:#fff'>{s['sym']} {d} {arrow}</div>"
                f"<div style='font-size:13px;color:#9aa3b5;line-height:1.9;margin-top:4px'>"
                f"入场 <b style='color:#fff'>{fmt(s['cur'])}</b><br>"
                f"止损 <b style='color:#ffc94d'>{fmt(s['stop'])}</b><br>"
                f"杠杆 <b style='color:#8ab4ff'>{s['lev']}x</b>　"
                f"数量 <b style='color:#fff'>{qstr(s)}</b><br>"
                f"名义 {s['notional']:.1f}U　风险 {s['risk']:.2f}U"
                f"</div></div>")
        parts.append("<h3 style='margin:6px 0 10px'>🔔 新信号</h3>" + "".join(cards))
    if gone:
        parts.append("<p style='color:#9aa3b5'>信号失效：" + "、".join(gone) + "（已回到区间内）</p>")
    if dropped:
        parts.append("<p style='color:#9aa3b5'>保证金不足跳过：" + "、".join(s["sym"] for s in dropped) + "</p>")

    title = "交易信号 " + today
    if fresh:
        uniq = sorted(set(s["sym"] for s in fresh), key=lambda x: CFG["COINS"].index(x))
        kind = "做多" if fresh[0]["sig"] == "L" else "做空"
        title = "、".join(uniq) + " " + kind + "信号 · " + today[5:].replace("-", "/")
    elif gone and not fresh:
        title = "信号失效 · " + today[5:].replace("-", "/")

    content = "".join(parts)
    if token:
        try:
            r = push(token, title, content)
            print("推送结果:", r)
        except Exception as e:
            print("推送失败:", e)
    else:
        print("未配置 PUSHPLUS_TOKEN，跳过推送")
        print(content)

    save_state(new_state)


if __name__ == "__main__":
    main()
