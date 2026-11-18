
import os
import time
import requests
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]
TIMEFRAMES = {"H1": ("1h", 60), "M15": ("15min", 15)}
OUTPUTSIZE = 140
RR = 2.0
MIN_ENTRY_SCORE = 80
BASE_URL = "https://api.twelvedata.com/time_series"


def fetch_candles(symbol, interval, minutes):
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
            "timezone": "UTC",
        },
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()

    if "values" not in payload:
        raise RuntimeError(payload.get("message", str(payload)))

    candles = []
    for row in reversed(payload["values"]):
        stamp = datetime.strptime(
            row["datetime"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)

        candles.append({
            "datetime": stamp,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    # Exclude the candle that is still forming.
    now = datetime.now(timezone.utc)
    duration = timedelta(minutes=minutes)
    candles = [c for c in candles if c["datetime"] + duration <= now]

    if len(candles) < 40:
        raise RuntimeError(
            f"Not enough completed candles: {len(candles)}"
        )

    return candles


def calculate_atr(candles, period=14):
    ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))

    recent = ranges[-period:]
    return sum(recent) / len(recent) if recent else 0.0


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


def direction_from_swings(highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return "RANGING"

    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "RANGING"


def analyse_structure(candles):
    highs, lows = find_swings(candles)
    direction = direction_from_swings(highs, lows)

    result = {
        "direction": direction,
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

    # Search recent completed candles for a structural break.
    start = max(3, len(candles) - 9)

    for i in range(start, len(candles)):
        previous_close = candles[i - 1]["close"]
        close = candles[i]["close"]

        eligible_highs = [x for x in highs if x["index"] < i]
        eligible_lows = [x for x in lows if x["index"] < i]

        broken_side = None
        broken_level = None

        if eligible_highs:
            level = eligible_highs[-1]["price"]
            if previous_close <= level and close > level:
                broken_side, broken_level = "BULLISH", level

        if eligible_lows:
            level = eligible_lows[-1]["price"]
            if previous_close >= level and close < level:
                broken_side, broken_level = "BEARISH", level

        if not broken_side:
            continue

        prior_highs = [x for x in highs if x["index"] < i - 2]
        prior_lows = [x for x in lows if x["index"] < i - 2]
        prior_direction = direction_from_swings(prior_highs, prior_lows)

        result.update({
            "event": broken_side,
            "event_index": i,
            "event_level": broken_level,
            "bos": "NONE",
            "choch": "NONE",
            "range_break": "NONE",
        })

        if prior_direction == broken_side:
            result["bos"] = broken_side
        elif prior_direction in ("BULLISH", "BEARISH"):
            result["choch"] = broken_side
        else:
            result["range_break"] = broken_side

    return result


def detect_liquidity_sweep(candles, lookback=8):
    highs, lows = find_swings(candles)
    start = max(3, len(candles) - lookback)

    # Check newest possible sweep first.
    for i in range(len(candles) - 1, start - 1, -1):
        previous_highs = [x for x in highs if x["index"] < i]
        previous_lows = [x for x in lows if x["index"] < i]
        candle = candles[i]

        if previous_highs:
            level = previous_highs[-1]["price"]

            if candle["high"] > level and candle["close"] < level:
                confirmed = (
                    i < len(candles) - 1
                    and candles[i + 1]["close"] < candle["close"]
                )

                return {
                    "side": "BEARISH",
                    "kind": "BUY-SIDE SWEEP",
                    "level": level,
                    "index": i,
                    "confirmed": confirmed,
                }

        if previous_lows:
            level = previous_lows[-1]["price"]

            if candle["low"] < level and candle["close"] > level:
                confirmed = (
                    i < len(candles) - 1
                    and candles[i + 1]["close"] > candle["close"]
                )

                return {
                    "side": "BULLISH",
                    "kind": "SELL-SIDE SWEEP",
                    "level": level,
                    "index": i,
                    "confirmed": confirmed,
                }

    return None


def cluster_levels(levels, tolerance):
    clusters = []

    for price in sorted(levels):
        if not clusters:
            clusters.append([price])
            continue

        average = sum(clusters[-1]) / len(clusters[-1])

        if abs(price - average) <= tolerance:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    return [
        {
            "level": sum(group) / len(group),
            "touches": len(group),
            "low": min(group),
            "high": max(group),
        }
        for group in clusters if len(group) >= 2
    ]


def find_liquidity(candles):
    highs, lows = find_swings(candles)
    volatility = calculate_atr(candles)
    price = candles[-1]["close"]
    tolerance = max(volatility * 0.12, price * 0.00005)

    buy = cluster_levels([x["price"] for x in highs], tolerance)
    sell = cluster_levels([x["price"] for x in lows], tolerance)

    buy.sort(key=lambda x: abs(x["level"] - price))
    sell.sort(key=lambda x: abs(x["level"] - price))

    return buy[:3], sell[:3]


def detect_zones(candles, structure):
    zones = []
    current_index = len(candles) - 1
    volatility = calculate_atr(candles)

    # Fair Value Gaps with a displacement-body filter.
    for i in range(2, len(candles)):
        first = candles[i - 2]
        middle = candles[i - 1]
        third = candles[i]

        body = abs(middle["close"] - middle["open"])

        if body < volatility * 0.35:
            continue

        if third["low"] > first["high"]:
            low, high, side = first["high"], third["low"], "BULLISH"
        elif third["high"] < first["low"]:
            low, high, side = third["high"], first["low"], "BEARISH"
        else:
            continue

        if high - low < volatility * 0.05:
            continue

        previous = candles[i + 1:current_index]

        touched_before = any(
            c["low"] <= high and c["high"] >= low
            for c in previous
        )

        touched_now = (
            candles[-1]["low"] <= high
            and candles[-1]["high"] >= low
        )

        if not touched_before:
            zones.append({
                "type": "FVG",
                "side": side,
                "low": low,
                "high": high,
                "index": i,
                "retest_now": touched_now,
            })

    # Last opposing candle before the latest structural break.
    event_index = structure.get("event_index")
    event_side = structure.get("event")

    if event_index is not None and event_side in ("BULLISH", "BEARISH"):
        for i in range(event_index - 1, max(-1, event_index - 11), -1):
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

            low, high = candle["low"], candle["high"]
            previous = candles[i + 1:current_index]

            touched_before = any(
                c["low"] <= high and c["high"] >= low
                for c in previous
            )

            touched_now = (
                candles[-1]["low"] <= high
                and candles[-1]["high"] >= low
            )

            if not touched_before:
                zones.append({
                    "type": "OB",
                    "side": event_side,
                    "low": low,
                    "high": high,
                    "index": i,
                    "retest_now": touched_now,
                })

            break

    price = candles[-1]["close"]

    for zone in zones:
        midpoint = (zone["low"] + zone["high"]) / 2
        distance = abs(price - midpoint) / max(volatility, 1e-9)
        age = current_index - zone["index"]

        zone["quality"] = max(
            0,
            100 - min(distance, 5) * 12 - min(age, 30) * 1.2,
        )
        zone["age"] = age

    zones.sort(
        key=lambda z: (z["retest_now"], z["quality"]),
        reverse=True,
    )

    return zones


def validate_rejection(candles, side, zone):
    candle = candles[-1]
    total_range = candle["high"] - candle["low"]

    if total_range <= 0:
        return False

    body = abs(candle["close"] - candle["open"])
    body_ratio = body / total_range

    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    upper_wick = candle["high"] - max(candle["open"], candle["close"])

    overlaps = (
        candle["low"] <= zone["high"]
        and candle["high"] >= zone["low"]
    )

    if not overlaps:
        return False

    if side == "BULLISH":
        close_position = (candle["close"] - candle["low"]) / total_range

        return (
            candle["close"] > candle["open"]
            and close_position >= 0.65
            and (body_ratio >= 0.25 or lower_wick >= body * 0.5)
        )

    if side == "BEARISH":
        close_position = (candle["high"] - candle["close"]) / total_range

        return (
            candle["close"] < candle["open"]
            and close_position >= 0.65
            and (body_ratio >= 0.25 or upper_wick >= body * 0.5)
        )

    return False


def format_price(symbol, value):
    if value is None:
        return "n/a"

    digits = 2 if symbol == "XAU/USD" else 3 if "JPY" in symbol else 5
    return f"{value:.{digits}f}"


def evaluate_market(symbol, h1, m15):
    h1s = analyse_structure(h1)
    m15s = analyse_structure(m15)
    sweep = detect_liquidity_sweep(m15)

    price = m15[-1]["close"]
    zones = detect_zones(m15, m15s)
    buy_liquidity, sell_liquidity = find_liquidity(m15)

    aligned = (
        h1s["direction"] in ("BULLISH", "BEARISH")
        and h1s["direction"] == m15s["direction"]
    )

    side = (
        m15s["direction"]
        if m15s["direction"] in ("BULLISH", "BEARISH")
        else None
    )

    matching = [
        z for z in zones
        if side and z["side"] == side
    ]

    zone = matching[0] if matching else None

    structural_trigger = bool(
        side and (
            m15s["bos"] == side
            or m15s["choch"] == side
        )
    )

    sweep_support = bool(
        sweep
        and sweep["side"] == side
        and sweep["confirmed"]
    ) if side else False

    rejection = bool(
        zone
        and zone["retest_now"]
        and validate_rejection(m15, side, zone)
    )

    # Watchlist score measures developing evidence, not entry readiness.
    watch_score = 0

    if m15s["direction"] in ("BULLISH", "BEARISH"):
        watch_score += 20

    if m15s["event"] in ("BULLISH", "BEARISH"):
        watch_score += 20

    if zone:
        watch_score += 20

        if zone["quality"] >= 60:
            watch_score += 10

        if zone["retest_now"]:
            watch_score += 10

    if sweep and sweep["confirmed"]:
        watch_score += 20

    watch_score = min(watch_score, 100)

    # Entry score is only awarded when H1 and M15 align.
    entry_score = 0

    if aligned:
        entry_score += 25

    if aligned and structural_trigger:
        entry_score += 20

    if aligned and zone:
        entry_score += 15

        if zone["quality"] >= 60:
            entry_score += 10

        if zone["retest_now"]:
            entry_score += 10

    if aligned and rejection:
        entry_score += 15

    if aligned and sweep_support:
        entry_score += 5

    status = "NO VALID SETUP"
    reason = "Waiting for a clear market direction"
    entry = sl = tp = None

    if not aligned:
        developing = (
            m15s["event"] in ("BULLISH", "BEARISH")
            or sweep is not None
            or zone is not None
        )

        if developing:
            status = "WATCHLIST"
            reason = (
                "Developing M15 evidence; "
                "H1/M15 alignment is not confirmed"
            )

    else:
        status = "EARLY WARNING"
        reason = "H1 and M15 directions agree"

        if structural_trigger:
            status = "STRUCTURE ALIGNED"
            reason += "; recent M15 BOS/CHOCH supports direction"

        if not zone:
            reason += "; waiting for a fresh matching zone"

        elif not zone["retest_now"]:
            reason += "; waiting for zone retest"

        elif not rejection:
            reason += "; retest found but rejection candle failed validation"

        elif entry_score >= MIN_ENTRY_SCORE:
            status = "ENTRY CONDITIONS MET"
            reason = (
                "H1/M15 aligned; fresh zone retested; "
                "closed M15 rejection candle validated"
            )

            entry = price
            volatility = calculate_atr(m15)
            buffer = max(volatility * 0.25, price * 0.00015)

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
        "h1": h1s,
        "m15": m15s,
        "side": side,
        "price": price,
        "zone": zone,
        "sweep": sweep,
        "watch_score": watch_score,
        "entry_score": entry_score,
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
            f"BOS: {m15['bos']} | CHOCH: {m15['choch']} | "
            f"RANGE BREAK: {m15['range_break']}"
        ),
        f"Price: {format_price(symbol, result['price'])}",
        (
            f"Watchlist score: {result['watch_score']}/100 | "
            f"Entry score: {result['entry_score']}/100"
        ),
        f"Reason: {result['reason']}",
    ]

    if zone:
        lines.extend([
            f"Zone: {zone['type']} {zone['side']} "
            f"{format_price(symbol, zone['low'])}–"
            f"{format_price(symbol, zone['high'])}",
            f"Zone quality: {zone['quality']:.0f}/100 | "
            f"{'RETEST' if zone['retest_now'] else 'WAITING'}",
        ])
    else:
        lines.append("Zone: No fresh matching zone")

    sweep = result["sweep"]

    if sweep:
        lines.append(
            f"Liquidity sweep: {sweep['kind']} at "
            f"{format_price(symbol, sweep['level'])} | "
            f"{'FOLLOW-THROUGH CONFIRMED' if sweep['confirmed'] else 'UNCONFIRMED'}"
        )
    else:
        lines.append("Liquidity sweep: None detected")

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
            "Verify spread, execution price and risk before considering a trade.",
        ])

    return "\n".join(lines)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram credentials")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=25,
    )

    response.raise_for_status()
    payload = response.json()

    if not payload.get("ok"):
        raise RuntimeError(f"Telegram delivery error: {payload}")


def main():
    if not API_KEY:
        raise RuntimeError("Missing Twelve Data API key")

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram credentials")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    reports = [
        "🤖 ODJ AI SMART TRADER V7",
        f"Scan: {stamp}",
        "Mode: ALERT ONLY",
        "Closed-candle analysis | H1 + M15",
    ]

    errors = []

    for symbol in SYMBOLS:
        try:
            h1 = fetch_candles(
                symbol,
                TIMEFRAMES["H1"][0],
                TIMEFRAMES["H1"][1],
            )

            time.sleep(1)

            m15 = fetch_candles(
                symbol,
                TIMEFRAMES["M15"][0],
                TIMEFRAMES["M15"][1],
            )

            result = evaluate_market(symbol, h1, m15)
            reports.append(report_market(result))

        except Exception as exc:
            errors.append(f"{symbol}: {exc}")

        time.sleep(1)

    if errors:
        reports.append(
            "⚠️ DATA/API ERRORS\n" + "\n".join(errors)
        )

    reports.append(
        "WATCHLIST is not an entry signal. Scores are rule-based, "
        "not probabilities. Liquidity levels are estimated from price swings. "
        "No trades are placed automatically."
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

    if len(errors) == len(SYMBOLS):
        raise RuntimeError("All markets failed; inspect API error report")


if __name__ == "__main__":
    main()
