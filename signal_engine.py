
import os
import time
import requests
from datetime import datetime, timezone

API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]
INTERVALS = {"H1": "1h", "M15": "15min"}
OUTPUTSIZE = 100
RR = 2.0
MIN_SCORE = 70
BASE_URL = "https://api.twelvedata.com/time_series"


def api_candles(symbol, interval):
    if not API_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY secret")

    response = requests.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": OUTPUTSIZE,
            "apikey": API_KEY,
            "format": "JSON",
        },
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()

    if "values" not in payload:
        raise RuntimeError(payload.get("message", str(payload)))

    rows = list(reversed(payload["values"]))
    candles = []

    for row in rows:
        candles.append({
            "datetime": row.get("datetime"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    if len(candles) < 30:
        raise RuntimeError(f"Only {len(candles)} candles returned")

    return candles


def atr(candles, period=14):
    trs = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        trs.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))

    return sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0


def find_swings(candles, left=2, right=2):
    highs, lows = [], []

    for i in range(left, len(candles) - right):
        high = candles[i]["high"]
        low = candles[i]["low"]

        if all(
            high > candles[j]["high"]
            for j in range(i-left, i+right+1) if j != i
        ):
            highs.append({"index": i, "price": high})

        if all(
            low < candles[j]["low"]
            for j in range(i-left, i+right+1) if j != i
        ):
            lows.append({"index": i, "price": low})

    return highs, lows


def analyse_structure(candles):
    highs, lows = find_swings(candles)

    if len(highs) < 2 or len(lows) < 2:
        return {
            "direction": "RANGING",
            "bos": "NONE",
            "choch": "NONE",
            "event": "NONE",
            "swing_high": None,
            "swing_low": None,
            "highs": highs,
            "lows": lows,
        }

    h1, h2 = highs[-2]["price"], highs[-1]["price"]
    l1, l2 = lows[-2]["price"], lows[-1]["price"]

    if h2 > h1 and l2 > l1:
        direction = "BULLISH"
    elif h2 < h1 and l2 < l1:
        direction = "BEARISH"
    else:
        direction = "RANGING"

    event = "NONE"
    event_level = None
    event_index = None

    recent_start = max(2, len(candles) - 14)

    for i in range(recent_start, len(candles)):
        previous_close = candles[i-1]["close"]
        close = candles[i]["close"]

        eligible_highs = [x for x in highs if x["index"] < i]
        eligible_lows = [x for x in lows if x["index"] < i]

        if eligible_highs:
            level = eligible_highs[-1]["price"]

            if previous_close <= level and close > level:
                event = "BULLISH"
                event_level = level
                event_index = i

        if eligible_lows:
            level = eligible_lows[-1]["price"]

            if previous_close >= level and close < level:
                event = "BEARISH"
                event_level = level
                event_index = i

    bos = "NONE"
    choch = "NONE"

    if event != "NONE":
        if direction == event:
            bos = event
        else:
            choch = event

    return {
        "direction": direction,
        "bos": bos,
        "choch": choch,
        "event": event,
        "event_level": event_level,
        "event_index": event_index,
        "swing_high": highs[-1]["price"],
        "swing_low": lows[-1]["price"],
        "highs": highs,
        "lows": lows,
    }


def find_zones(candles, structure):
    zones = []
    current = len(candles) - 1
    event_i = structure.get("event_index")
    event = structure.get("event")
    volatility = atr(candles)

    # Fair Value Gaps
    for i in range(2, len(candles)):
        a = candles[i-2]
        c = candles[i]

        if c["low"] > a["high"]:
            low, high, side = a["high"], c["low"], "BULLISH"
        elif c["high"] < a["low"]:
            low, high, side = c["high"], a["low"], "BEARISH"
        else:
            continue

        if high - low < volatility * 0.08:
            continue

        later = candles[i+1:current]

        touched_before_now = any(
            x["low"] <= high and x["high"] >= low
            for x in later
        )

        now_touches = (
            candles[current]["low"] <= high
            and candles[current]["high"] >= low
        )

        if not touched_before_now:
            zones.append({
                "type": "FVG",
                "side": side,
                "low": low,
                "high": high,
                "index": i,
                "fresh": True,
                "retest_now": now_touches,
            })

    # Order Block: last opposing candle before the latest break
    if event_i is not None and event in ("BULLISH", "BEARISH"):
        start = max(0, event_i - 8)

        for i in range(event_i - 1, start - 1, -1):
            candle = candles[i]

            opposite = (
                event == "BULLISH" and candle["close"] < candle["open"]
            ) or (
                event == "BEARISH" and candle["close"] > candle["open"]
            )

            if not opposite:
                continue

            low, high = candle["low"], candle["high"]
            later = candles[i+1:current]

            touched_before_now = any(
                x["low"] <= high and x["high"] >= low
                for x in later
            )

            now_touches = (
                candles[current]["low"] <= high
                and candles[current]["high"] >= low
            )

            if not touched_before_now:
                zones.append({
                    "type": "OB",
                    "side": event,
                    "low": low,
                    "high": high,
                    "index": i,
                    "fresh": True,
                    "retest_now": now_touches,
                })

            break

    price = candles[-1]["close"]

    for zone in zones:
        midpoint = (zone["low"] + zone["high"]) / 2
        distance = abs(price - midpoint) / max(volatility, 1e-9)
        age = current - zone["index"]

        zone["distance_atr"] = distance
        zone["age"] = age
        zone["score"] = max(
            0,
            100 - min(distance, 5) * 12 - min(age, 30) * 1.2
        )

    zones.sort(
        key=lambda z: (z["retest_now"], z["score"]),
        reverse=True,
    )

    return zones


def liquidity_levels(candles):
    highs, lows = find_swings(candles)
    price = candles[-1]["close"]
    volatility = atr(candles)

    tolerance = max(volatility * 0.12, price * 0.00015)

    buy = []
    sell = []

    for i, item in enumerate(highs):
        if any(
            abs(item["price"] - other["price"]) <= tolerance
            for other in highs[i+1:]
        ):
            buy.append(item["price"])

    for i, item in enumerate(lows):
        if any(
            abs(item["price"] - other["price"]) <= tolerance
            for other in lows[i+1:]
        ):
            sell.append(item["price"])

    buy = sorted(set(buy), key=lambda x: abs(x-price))[:2]
    sell = sorted(set(sell), key=lambda x: abs(x-price))[:2]

    return buy, sell


def rejection_confirmed(candles, side, zone):
    candle = candles[-1]

    body = abs(candle["close"] - candle["open"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    upper_wick = candle["high"] - max(candle["open"], candle["close"])

    overlaps = (
        candle["low"] <= zone["high"]
        and candle["high"] >= zone["low"]
    )

    if not overlaps:
        return False

    if side == "BULLISH":
        return (
            candle["close"] > candle["open"]
            and (
                lower_wick >= body * 0.5
                or candle["close"] >= zone["high"]
            )
        )

    return (
        candle["close"] < candle["open"]
        and (
            upper_wick >= body * 0.5
            or candle["close"] <= zone["low"]
        )
    )


def fmt(symbol, value):
    if value is None:
        return "n/a"

    digits = (
        2 if symbol == "XAU/USD"
        else 3 if "JPY" in symbol
        else 5
    )

    return f"{value:.{digits}f}"


def evaluate(symbol, h1, m15):
    h1s = analyse_structure(h1)
    m15s = analyse_structure(m15)

    price = m15[-1]["close"]
    zones = find_zones(m15, m15s)
    buy_liq, sell_liq = liquidity_levels(m15)

    aligned = (
        h1s["direction"] in ("BULLISH", "BEARISH")
        and h1s["direction"] == m15s["direction"]
    )

    side = m15s["direction"] if aligned else None

    matching_zones = [
        z for z in zones if z["side"] == side
    ] if side else []

    zone = matching_zones[0] if matching_zones else None

    confirmed = bool(
        zone
        and zone["retest_now"]
        and rejection_confirmed(m15, side, zone)
    )

    trigger = (
        m15s["bos"] == side
        or m15s["choch"] == side
    )

    score = 0

    if aligned:
        score += 30
    if trigger:
        score += 20
    if zone:
        score += 15
    if zone and zone["score"] >= 60:
        score += 10
    if zone and zone["retest_now"]:
        score += 10
    if confirmed:
        score += 15

    status = "NO VALID SETUP"
    reason = "Waiting for H1/M15 directional alignment"

    entry = sl = tp = None

    if aligned:
        status = "EARLY WARNING"
        reason = (
            "H1 and M15 direction agree; waiting for a fresh zone and retest"
        )

        if trigger:
            status = "STRUCTURE ALIGNED"
            reason = "Recent M15 structure break agrees with H1 direction"

        if not zone:
            reason += "; no fresh matching FVG/OB"
        elif not zone["retest_now"]:
            reason += "; waiting for price to revisit the zone"
        elif not confirmed:
            reason += "; zone touched but rejection candle not confirmed"
        elif score >= MIN_SCORE:
            status = "ENTRY CONDITIONS MET"
            reason = (
                "Aligned trend, fresh zone retest and M15 rejection confirmed"
            )

            entry = price
            volatility = atr(m15)
            buffer = max(
                volatility * 0.25,
                price * 0.00015,
            )

            if side == "BULLISH":
                sl = zone["low"] - buffer
                tp = entry + (entry - sl) * RR
            else:
                sl = zone["high"] + buffer
                tp = entry - (sl - entry) * RR

    return {
        "symbol": symbol,
        "status": status,
        "reason": reason,
        "side": side,
        "h1": h1s,
        "m15": m15s,
        "price": price,
        "zone": zone,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "buy_liq": buy_liq,
        "sell_liq": sell_liq,
    }


def report_item(result):
    symbol = result["symbol"]
    h = result["h1"]
    m = result["m15"]
    zone = result["zone"]

    lines = [
        f"📊 {symbol} — {result['status']}",
        f"H1: {h['direction']} | M15: {m['direction']}",
        f"BOS: {m['bos']} | CHOCH: {m['choch']}",
        f"Price: {fmt(symbol, result['price'])} | Score: {result['score']}/100",
        f"Reason: {result['reason']}",
    ]

    if zone:
        lines.append(
            f"Zone: {zone['type']} {zone['side']} "
            f"{fmt(symbol, zone['low'])}–{fmt(symbol, zone['high'])} | "
            f"{'RETEST NOW' if zone['retest_now'] else 'WAITING'} | "
            f"quality {zone['score']:.0f}/100"
        )
    else:
        lines.append("Zone: none fresh and aligned")

    if result["buy_liq"]:
        lines.append(
            "Potential buy-side liquidity: "
            + ", ".join(fmt(symbol, x) for x in result["buy_liq"])
        )

    if result["sell_liq"]:
        lines.append(
            "Potential sell-side liquidity: "
            + ", ".join(fmt(symbol, x) for x in result["sell_liq"])
        )

    if result["status"] == "ENTRY CONDITIONS MET":
        lines.extend([
            f"Conditional entry: {fmt(symbol, result['entry'])}",
            f"Suggested SL: {fmt(symbol, result['sl'])}",
            f"Suggested TP (1:{RR:g}): {fmt(symbol, result['tp'])}",
            "Alert only — verify spread, candle close and risk before any trade.",
        ])

    return "\n".join(lines)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram secrets")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def main():
    if not API_KEY or not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("One or more required GitHub secrets are missing")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    reports = [
        f"🤖 ODJ AI SMART TRADER V5\n"
        f"Scan: {stamp}\n"
        f"Mode: ALERT ONLY\n"
    ]

    errors = []

    for symbol in SYMBOLS:
        try:
            h1 = api_candles(symbol, INTERVALS["H1"])
            time.sleep(1)

            m15 = api_candles(symbol, INTERVALS["M15"])

            result = evaluate(symbol, h1, m15)
            reports.append(report_item(result))

        except Exception as exc:
            errors.append(f"{symbol}: {exc}")

        time.sleep(1)

    if errors:
        reports.append(
            "⚠️ DATA/API ISSUES\n" + "\n".join(errors)
        )

    reports.append(
        "⚠️ Structure and zone rules are estimates, not guaranteed signals. "
        "No trade is placed automatically."
    )

    message = "\n\n".join(reports)
    chunks = []

    while len(message) > 3900:
        cut = message.rfind("\n\n", 0, 3900)

        if cut < 1:
            cut = 3900

        chunks.append(message[:cut])
        message = message[cut:].lstrip()

    if message:
        chunks.append(message)

    for chunk in chunks:
        send_telegram(chunk)

    if errors and len(errors) == len(SYMBOLS):
        raise RuntimeError("All symbols failed; check the Telegram error report")


if __name__ == "__main__":
    main()
