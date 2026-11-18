
import os
import time
import requests
from datetime import datetime, timezone

API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]
TIMEFRAMES = {"H1": "1h", "M15": "15min"}
OUTPUTSIZE = 120
RR = 2.0
MIN_ENTRY_SCORE = 75

BASE_URL = "https://api.twelvedata.com/time_series"


def fetch_candles(symbol, interval):
    if not API_KEY:
        raise RuntimeError("Missing Twelve Data API key")

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
    data = response.json()

    if "values" not in data:
        raise RuntimeError(data.get("message", str(data)))

    candles = []

    for row in reversed(data["values"]):
        candles.append({
            "datetime": row.get("datetime"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    if len(candles) < 40:
        raise RuntimeError(f"Insufficient candle data: {len(candles)}")

    return candles


def calculate_atr(candles, period=14):
    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))

    recent = true_ranges[-period:]

    return sum(recent) / len(recent) if recent else 0.0


def find_swings(candles, left=2, right=2):
    highs, lows = [], []

    for i in range(left, len(candles) - right):
        high = candles[i]["high"]
        low = candles[i]["low"]

        if all(
            high > candles[j]["high"]
            for j in range(i-left, i+right+1)
            if j != i
        ):
            highs.append({"index": i, "price": high})

        if all(
            low < candles[j]["low"]
            for j in range(i-left, i+right+1)
            if j != i
        ):
            lows.append({"index": i, "price": low})

    return highs, lows


def direction_from_swings(highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return "RANGING"

    higher_high = highs[-1]["price"] > highs[-2]["price"]
    higher_low = lows[-1]["price"] > lows[-2]["price"]

    lower_high = highs[-1]["price"] < highs[-2]["price"]
    lower_low = lows[-1]["price"] < lows[-2]["price"]

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    return "RANGING"


def analyse_structure(candles):
    highs, lows = find_swings(candles)

    current_direction = direction_from_swings(highs, lows)

    result = {
        "direction": current_direction,
        "bos": "NONE",
        "choch": "NONE",
        "range_break": "NONE",
        "event": "NONE",
        "event_index": None,
        "event_level": None,
        "swing_high": highs[-1]["price"] if highs else None,
        "swing_low": lows[-1]["price"] if lows else None,
        "highs": highs,
        "lows": lows,
    }

    # Only inspect recent breaks of confirmed swing levels.
    start = max(3, len(candles) - 14)

    for i in range(start, len(candles)):
        previous_close = candles[i - 1]["close"]
        current_close = candles[i]["close"]

        previous_highs = [x for x in highs if x["index"] < i]
        previous_lows = [x for x in lows if x["index"] < i]

        broken_side = None
        broken_level = None

        if previous_highs:
            level = previous_highs[-1]["price"]

            if previous_close <= level and current_close > level:
                broken_side = "BULLISH"
                broken_level = level

        if previous_lows:
            level = previous_lows[-1]["price"]

            if previous_close >= level and current_close < level:
                broken_side = "BEARISH"
                broken_level = level

        if broken_side is None:
            continue

        # Determine market direction immediately before the break.
        prior_highs = [x for x in highs if x["index"] < i - 2]
        prior_lows = [x for x in lows if x["index"] < i - 2]
        prior_direction = direction_from_swings(prior_highs, prior_lows)

        result["event"] = broken_side
        result["event_index"] = i
        result["event_level"] = broken_level
        result["bos"] = "NONE"
        result["choch"] = "NONE"
        result["range_break"] = "NONE"

        if prior_direction == broken_side:
            result["bos"] = broken_side
        elif prior_direction in ("BULLISH", "BEARISH"):
            result["choch"] = broken_side
        else:
            result["range_break"] = broken_side

    return result


def cluster_levels(levels, tolerance):
    if not levels:
        return []

    sorted_levels = sorted(levels)
    clusters = []

    for price in sorted_levels:
        if not clusters:
            clusters.append([price])
            continue

        average = sum(clusters[-1]) / len(clusters[-1])

        if abs(price - average) <= tolerance:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    output = []

    for cluster in clusters:
        if len(cluster) >= 2:
            output.append({
                "level": sum(cluster) / len(cluster),
                "touches": len(cluster),
                "low": min(cluster),
                "high": max(cluster),
            })

    return output


def find_liquidity(candles):
    highs, lows = find_swings(candles)
    volatility = calculate_atr(candles)
    price = candles[-1]["close"]

    tolerance = max(volatility * 0.12, price * 0.00005)

    buy_levels = cluster_levels(
        [x["price"] for x in highs],
        tolerance,
    )

    sell_levels = cluster_levels(
        [x["price"] for x in lows],
        tolerance,
    )

    buy_levels.sort(key=lambda x: abs(x["level"] - price))
    sell_levels.sort(key=lambda x: abs(x["level"] - price))

    return buy_levels[:3], sell_levels[:3]


def detect_zones(candles, structure):
    zones = []
    current_index = len(candles) - 1
    volatility = calculate_atr(candles)

    # Fair Value Gaps
    for i in range(2, len(candles)):
        first = candles[i - 2]
        middle = candles[i - 1]
        third = candles[i]

        middle_body = abs(middle["close"] - middle["open"])

        if middle_body < volatility * 0.35:
            continue

        if third["low"] > first["high"]:
            low = first["high"]
            high = third["low"]
            side = "BULLISH"

        elif third["high"] < first["low"]:
            low = third["high"]
            high = first["low"]
            side = "BEARISH"

        else:
            continue

        if high - low < volatility * 0.05:
            continue

        # A previous candle touching the zone makes it used.
        previous_candles = candles[i + 1:current_index]

        previously_touched = any(
            candle["low"] <= high and candle["high"] >= low
            for candle in previous_candles
        )

        current_touches = (
            candles[-1]["low"] <= high
            and candles[-1]["high"] >= low
        )

        if not previously_touched:
            zones.append({
                "type": "FVG",
                "side": side,
                "low": low,
                "high": high,
                "index": i,
                "retest_now": current_touches,
            })

    # Order Block: last opposing candle before the latest structural break.
    event_index = structure.get("event_index")
    event_side = structure.get("event")

    if event_index is not None and event_side in ("BULLISH", "BEARISH"):
        search_start = max(0, event_index - 10)

        for i in range(event_index - 1, search_start - 1, -1):
            candle = candles[i]

            opposing = (
                event_side == "BULLISH"
                and candle["close"] < candle["open"]
            ) or (
                event_side == "BEARISH"
                and candle["close"] > candle["open"]
            )

            if not opposing:
                continue

            low = candle["low"]
            high = candle["high"]

            previous_candles = candles[i + 1:current_index]

            previously_touched = any(
                x["low"] <= high and x["high"] >= low
                for x in previous_candles
            )

            current_touches = (
                candles[-1]["low"] <= high
                and candles[-1]["high"] >= low
            )

            if not previously_touched:
                zones.append({
                    "type": "OB",
                    "side": event_side,
                    "low": low,
                    "high": high,
                    "index": i,
                    "retest_now": current_touches,
                })

            break

    price = candles[-1]["close"]

    for zone in zones:
        midpoint = (zone["low"] + zone["high"]) / 2
        distance = abs(price - midpoint) / max(volatility, 1e-9)
        age = current_index - zone["index"]

        zone["distance_atr"] = distance
        zone["age"] = age

        zone["quality"] = max(
            0,
            100
            - min(distance, 5) * 12
            - min(age, 30) * 1.2,
        )

    zones.sort(
        key=lambda z: (
            z["retest_now"],
            z["quality"],
        ),
        reverse=True,
    )

    return zones


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
                or candle["close"] > zone["high"]
            )
        )

    if side == "BEARISH":
        return (
            candle["close"] < candle["open"]
            and (
                upper_wick >= body * 0.5
                or candle["close"] < zone["low"]
            )
        )

    return False


def format_price(symbol, value):
    if value is None:
        return "n/a"

    if symbol == "XAU/USD":
        digits = 2
    elif "JPY" in symbol:
        digits = 3
    else:
        digits = 5

    return f"{value:.{digits}f}"


def evaluate_market(symbol, h1, m15):
    h1s = analyse_structure(h1)
    m15s = analyse_structure(m15)

    price = m15[-1]["close"]
    zones = detect_zones(m15, m15s)
    buy_liquidity, sell_liquidity = find_liquidity(m15)

    aligned = (
        h1s["direction"] in ("BULLISH", "BEARISH")
        and h1s["direction"] == m15s["direction"]
    )

    side = m15s["direction"] if m15s["direction"] in (
        "BULLISH", "BEARISH"
    ) else None

    matching_zones = [
        zone for zone in zones
        if side and zone["side"] == side
    ]

    selected_zone = matching_zones[0] if matching_zones else None

    trigger = (
        m15s["bos"] == side
        or m15s["choch"] == side
    )

    rejection = bool(
        selected_zone
        and selected_zone["retest_now"]
        and rejection_confirmed(m15, side, selected_zone)
    )

    score = 0

    if aligned:
        score += 30

    if trigger:
        score += 20

    if selected_zone:
        score += 15

    if selected_zone and selected_zone["quality"] >= 60:
        score += 10

    if selected_zone and selected_zone["retest_now"]:
        score += 10

    if rejection:
        score += 15

    status = "NO VALID SETUP"
    reason = "Waiting for directional confirmation"

    entry = sl = tp = None

    if not aligned:
        developing = (
            m15s["event"] in ("BULLISH", "BEARISH")
            or m15s["range_break"] in ("BULLISH", "BEARISH")
            or m15s["choch"] in ("BULLISH", "BEARISH")
            or m15s["bos"] in ("BULLISH", "BEARISH")
        )

        if developing:
            status = "WATCHLIST"
            reason = (
                "M15 has a developing structural event, "
                "but H1/M15 alignment is not confirmed"
            )

    else:
        status = "EARLY WARNING"
        reason = "H1 and M15 direction agree"

        if trigger:
            status = "STRUCTURE ALIGNED"
            reason += "; M15 structure trigger detected"

        if not selected_zone:
            reason += "; waiting for a fresh matching zone"

        elif not selected_zone["retest_now"]:
            reason += "; waiting for price to revisit the zone"

        elif not rejection:
            reason += "; retest detected but rejection not confirmed"

        elif score >= MIN_ENTRY_SCORE:
            status = "ENTRY CONDITIONS MET"
            reason = (
                "Directional alignment, matching zone retest "
                "and M15 rejection conditions detected"
            )

            entry = price
            volatility = calculate_atr(m15)
            buffer = max(
                volatility * 0.25,
                price * 0.00015,
            )

            if side == "BULLISH":
                sl = selected_zone["low"] - buffer
                tp = entry + (entry - sl) * RR

            elif side == "BEARISH":
                sl = selected_zone["high"] + buffer
                tp = entry - (sl - entry) * RR

    return {
        "symbol": symbol,
        "status": status,
        "reason": reason,
        "h1": h1s,
        "m15": m15s,
        "side": side,
        "price": price,
        "zone": selected_zone,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "buy_liquidity": buy_liquidity,
        "sell_liquidity": sell_liquidity,
    }


def report_market(result):
    symbol = result["symbol"]
    h1 = result["h1"]
    m15 = result["m15"]
    zone = result["zone"]

    lines = [
        f"📊 {symbol} — {result['status']}",
        f"H1: {h1['direction']} | M15: {m15['direction']}",
        (
            f"BOS: {m15['bos']} | "
            f"CHOCH: {m15['choch']} | "
            f"RANGE BREAK: {m15['range_break']}"
        ),
        (
            f"Price: {format_price(symbol, result['price'])} | "
            f"Score: {result['score']}/100"
        ),
        f"Reason: {result['reason']}",
    ]

    if zone:
        lines.append(
            f"Zone: {zone['type']} {zone['side']} "
            f"{format_price(symbol, zone['low'])}–"
            f"{format_price(symbol, zone['high'])}"
        )

        lines.append(
            f"Zone quality: {zone['quality']:.0f}/100 | "
            f"{'RETEST NOW' if zone['retest_now'] else 'WAITING FOR RETEST'}"
        )
    else:
        lines.append("Zone: No fresh matching zone")

    if result["buy_liquidity"]:
        values = [
            f"{format_price(symbol, x['level'])} ({x['touches']} touches)"
            for x in result["buy_liquidity"]
        ]
        lines.append("Potential buy-side liquidity: " + ", ".join(values))

    if result["sell_liquidity"]:
        values = [
            f"{format_price(symbol, x['level'])} ({x['touches']} touches)"
            for x in result["sell_liquidity"]
        ]
        lines.append("Potential sell-side liquidity: " + ", ".join(values))

    if result["status"] == "ENTRY CONDITIONS MET":
        lines.extend([
            f"Conditional entry: {format_price(symbol, result['entry'])}",
            f"Suggested SL: {format_price(symbol, result['sl'])}",
            f"Suggested TP (1:{RR:g}): {format_price(symbol, result['tp'])}",
            "Check spread and candle close before considering any trade.",
        ])

    return "\n".join(lines)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram bot token or chat ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=25,
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram delivery failed: {data}")


def main():
    if not API_KEY:
        raise RuntimeError("Missing Twelve Data API key")

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram credentials")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    reports = [
        "🤖 ODJ AI SMART TRADER V6",
        f"Scan: {stamp}",
        "Mode: ALERT ONLY",
        "Timeframes: H1 + M15",
    ]

    errors = []

    for symbol in SYMBOLS:
        try:
            h1 = fetch_candles(symbol, TIMEFRAMES["H1"])
            time.sleep(1)

            m15 = fetch_candles(symbol, TIMEFRAMES["M15"])

            result = evaluate_market(symbol, h1, m15)
            reports.append(report_market(result))

        except Exception as error:
            errors.append(f"{symbol}: {error}")

        time.sleep(1)

    if errors:
        reports.append(
            "⚠️ DATA/API ERRORS\n" + "\n".join(errors)
        )

    reports.append(
        "WATCHLIST is not an entry signal. "
        "Structure, liquidity and zones are estimates. "
        "No trades are placed automatically."
    )

    message = "\n\n".join(reports)

    # Keep each Telegram message below the message size limit.
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

    if len(errors) == len(SYMBOLS):
        raise RuntimeError("All four markets failed to return data")


if __name__ == "__main__":
    main()
