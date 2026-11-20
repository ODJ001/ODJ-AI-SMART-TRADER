
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from statistics import median

# ============================================================
# ODJ AI SMART TRADER V9
# STRICT ENTRY VALIDATION | ALERT ONLY
# ============================================================

API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

TIMEFRAMES = {
    "H1": {"interval": "1h", "minutes": 60},
    "M15": {"interval": "15min", "minutes": 15},
}

OUTPUTSIZE = 120
REQUEST_DELAY = 8

MAX_ZONE_AGE = 32
MAX_ZONE_DISTANCE_ATR = 2.5
MIN_ZONE_QUALITY = 45
MIN_ENTRY_SCORE = 80


# ============================================================
# DATA FETCHING
# ============================================================

def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_candles(symbol, timeframe):
    if not API_KEY:
        raise RuntimeError("Missing Twelve Data API key")

    settings = TIMEFRAMES[timeframe]

    response = requests.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "interval": settings["interval"],
            "outputsize": OUTPUTSIZE,
            "timezone": "UTC",
            "apikey": API_KEY,
        },
        timeout=25,
    )
    response.raise_for_status()

    data = response.json()

    if "values" not in data:
        raise RuntimeError(data.get("message", str(data)))

    now = datetime.now(timezone.utc)
    duration = timedelta(minutes=settings["minutes"])
    candles = []

    for item in data["values"]:
        timestamp = parse_time(item.get("datetime"))
        if timestamp is None:
            continue

        # Only use candles whose full duration has elapsed.
        if timestamp + duration > now:
            continue

        try:
            candles.append({
                "time": timestamp,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            })
        except (ValueError, TypeError, KeyError):
            continue

    candles.sort(key=lambda x: x["time"])

    if len(candles) < 40:
        raise RuntimeError(
            f"Insufficient completed candles: {symbol} {timeframe}"
        )

    return candles


# ============================================================
# ATR AND SWINGS
# ============================================================

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

    if len(ranges) < period:
        return 0

    return sum(ranges[-period:]) / period


def find_swings(candles, left=2, right=2):
    highs = []
    lows = []

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


def determine_trend(highs, lows):
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


# ============================================================
# STRUCTURE: BOS / CHOCH
# ============================================================

def analyse_structure(candles):
    highs, lows = find_swings(candles)
    trend = determine_trend(highs, lows)

    result = {
        "direction": trend,
        "bos": "NONE",
        "choch": "NONE",
        "range_break": "NONE",
        "event_index": None,
        "event_level": None,
        "highs": highs,
        "lows": lows,
    }

    if len(highs) < 1 or len(lows) < 1:
        return result

    # Examine recent completed candles for a fresh break.
    start = max(2, len(candles) - 9)

    for i in range(start, len(candles)):
        previous_close = candles[i - 1]["close"]
        close = candles[i]["close"]

        prior_highs = [
            x["price"] for x in highs if x["index"] < i
        ]
        prior_lows = [
            x["price"] for x in lows if x["index"] < i
        ]

        bullish_level = max(prior_highs) if prior_highs else None
        bearish_level = min(prior_lows) if prior_lows else None

        bullish_break = (
            bullish_level is not None
            and previous_close <= bullish_level
            and close > bullish_level
        )

        bearish_break = (
            bearish_level is not None
            and previous_close >= bearish_level
            and close < bearish_level
        )

        if bullish_break:
            result["event_index"] = i
            result["event_level"] = bullish_level

            if trend == "BEARISH":
                result["choch"] = "BULLISH"
            elif trend == "RANGING":
                result["range_break"] = "BULLISH"
            else:
                result["bos"] = "BULLISH"

        elif bearish_break:
            result["event_index"] = i
            result["event_level"] = bearish_level

            if trend == "BULLISH":
                result["choch"] = "BEARISH"
            elif trend == "RANGING":
                result["range_break"] = "BEARISH"
            else:
                result["bos"] = "BEARISH"

    return result


def confirmed_structure_direction(structure):
    if structure["bos"] != "NONE":
        return structure["bos"]

    if structure["choch"] != "NONE":
        return structure["choch"]

    return None


# ============================================================
# LIQUIDITY CLUSTERS
# ============================================================

def cluster_levels(levels, tolerance):
    if not levels:
        return []

    levels = sorted(levels, key=lambda x: x["price"])
    groups = []

    for level in levels:
        if not groups:
            groups.append([level])
            continue

        center = median([x["price"] for x in groups[-1]])

        if abs(level["price"] - center) <= tolerance:
            groups[-1].append(level)
        else:
            groups.append([level])

    output = []

    for group in groups:
        if len(group) >= 2:
            prices = [x["price"] for x in group]
            output.append({
                "price": median(prices),
                "touches": len(group),
                "low": min(prices),
                "high": max(prices),
            })

    return output


def detect_liquidity(candles, atr):
    highs, lows = find_swings(candles)
    tolerance = max(atr * 0.12, 0.00001)

    buy = cluster_levels(highs, tolerance)
    sell = cluster_levels(lows, tolerance)

    price = candles[-1]["close"]

    buy.sort(key=lambda x: abs(x["price"] - price))
    sell.sort(key=lambda x: abs(x["price"] - price))

    return buy[:3], sell[:3]


# ============================================================
# FVG DETECTION
# ============================================================

def detect_fvg(candles, atr):
    zones = []

    if atr <= 0:
        return zones

    current_price = candles[-1]["close"]

    # The last candle cannot create a usable zone by itself:
    # a later completed candle must be available for retesting.
    for i in range(2, len(candles) - 1):
        age = len(candles) - 1 - i

        if age < 1 or age > MAX_ZONE_AGE:
            continue

        first = candles[i - 2]
        third = candles[i]

        if third["low"] > first["high"]:
            low = first["high"]
            high = third["low"]
            direction = "BULLISH"

        elif third["high"] < first["low"]:
            low = third["high"]
            high = first["low"]
            direction = "BEARISH"

        else:
            continue

        midpoint = (low + high) / 2
        distance = abs(current_price - midpoint) / atr
        gap_atr = (high - low) / atr

        if distance > MAX_ZONE_DISTANCE_ATR:
            continue

        if gap_atr < 0.08:
            continue

        # Reject any zone touched after formation.
        mitigated = any(
            candle["low"] <= high and candle["high"] >= low
            for candle in candles[i + 1:]
        )

        if mitigated:
            continue

        distance_score = max(0, 35 - distance * 12)
        size_score = min(30, gap_atr * 35)
        freshness_score = max(0, 35 - age)

        quality = int(min(
            100,
            distance_score + size_score + freshness_score
        ))

        if quality < MIN_ZONE_QUALITY:
            continue

        zones.append({
            "type": "FVG",
            "direction": direction,
            "low": low,
            "high": high,
            "midpoint": midpoint,
            "index": i,
            "age": age,
            "distance": distance,
            "quality": quality,
        })

    return zones


# ============================================================
# ORDER BLOCK DETECTION
# ============================================================

def detect_order_block(candles, structure, atr):
    direction = confirmed_structure_direction(structure)

    if direction is None or atr <= 0:
        return None

    current_price = candles[-1]["close"]
    start = max(1, len(candles) - 14)

    for i in range(len(candles) - 2, start - 1, -1):
        candle = candles[i]

        opposing = (
            direction == "BULLISH" and candle["close"] < candle["open"]
        ) or (
            direction == "BEARISH" and candle["close"] > candle["open"]
        )

        if not opposing:
            continue

        low = candle["low"]
        high = candle["high"]
        midpoint = (low + high) / 2
        distance = abs(current_price - midpoint) / atr
        age = len(candles) - 1 - i

        if age < 1 or age > MAX_ZONE_AGE:
            continue

        if distance > MAX_ZONE_DISTANCE_ATR:
            continue

        mitigated = any(
            x["low"] <= high and x["high"] >= low
            for x in candles[i + 1:]
        )

        if mitigated:
            continue

        quality = int(max(0, 80 - distance * 15 - age))

        if quality < MIN_ZONE_QUALITY:
            continue

        return {
            "type": "OB",
            "direction": direction,
            "low": low,
            "high": high,
            "midpoint": midpoint,
            "index": i,
            "age": age,
            "distance": distance,
            "quality": quality,
        }

    return None


def select_zone(candles, structure, atr):
    zones = detect_fvg(candles, atr)

    ob = detect_order_block(candles, structure, atr)
    if ob:
        zones.append(ob)

    if not zones:
        return None

    zones.sort(
        key=lambda z: (
            z["quality"],
            -z["distance"],
            -z["age"],
        ),
        reverse=True,
    )

    return zones[0]


# ============================================================
# REAL RETEST AND REJECTION VALIDATION
# ============================================================

def validate_retest_rejection(candles, zone):
    if zone is None:
        return {
            "retest": False,
            "rejection": False,
            "valid": False,
        }

    zone_index = zone["index"]
    current_index = len(candles) - 1

    # A later candle must exist after the zone was created.
    if current_index <= zone_index:
        return {
            "retest": False,
            "rejection": False,
            "valid": False,
        }

    later_candles = candles[zone_index + 1:]

    # Price must actually return to the zone after formation.
    touched = [
        c for c in later_candles
        if c["low"] <= zone["high"]
        and c["high"] >= zone["low"]
    ]

    if not touched:
        return {
            "retest": False,
            "rejection": False,
            "valid": False,
        }

    last = candles[-1]
    candle_range = last["high"] - last["low"]

    if candle_range <= 0:
        return {
            "retest": True,
            "rejection": False,
            "valid": False,
        }

    body_ratio = abs(last["close"] - last["open"]) / candle_range

    close_position = (
        last["close"] - last["low"]
    ) / candle_range

    # The current candle must be the retest rejection candle.
    current_touches = (
        last["low"] <= zone["high"]
        and last["high"] >= zone["low"]
    )

    if not current_touches:
        return {
            "retest": True,
            "rejection": False,
            "valid": False,
        }

    if zone["direction"] == "BULLISH":
        lower_wick = min(last["open"], last["close"]) - last["low"]

        rejection = (
            last["close"] > last["open"]
            and close_position >= 0.65
            and body_ratio >= 0.25
            and lower_wick > candle_range * 0.20
            and last["close"] > zone["midpoint"]
        )

    else:
        upper_wick = last["high"] - max(last["open"], last["close"])

        rejection = (
            last["close"] < last["open"]
            and close_position <= 0.35
            and body_ratio >= 0.25
            and upper_wick > candle_range * 0.20
            and last["close"] < zone["midpoint"]
        )

    return {
        "retest": True,
        "rejection": rejection,
        "valid": rejection,
    }


# ============================================================
# CONFIRMED LIQUIDITY SWEEP
# ============================================================

def detect_confirmed_sweep(candles, structure):
    highs = structure["highs"]
    lows = structure["lows"]

    start = max(3, len(candles) - 12)

    for i in range(len(candles) - 2, start - 1, -1):
        candle = candles[i]
        follow = candles[i + 1]

        prior_highs = [
            x["price"] for x in highs if x["index"] < i
        ]
        prior_lows = [
            x["price"] for x in lows if x["index"] < i
        ]

        if prior_highs:
            level = max(prior_highs[-3:])

            swept = (
                candle["high"] > level
                and candle["close"] < level
            )

            follow_through = (
                follow["close"] < level
                and follow["close"] < follow["open"]
            )

            if swept and follow_through:
                return {
                    "type": "BUY-SIDE SWEEP",
                    "direction": "BEARISH",
                    "level": level,
                    "index": i,
                    "confirmed": True,
                }

        if prior_lows:
            level = min(prior_lows[-3:])

            swept = (
                candle["low"] < level
                and candle["close"] > level
            )

            follow_through = (
                follow["close"] > level
                and follow["close"] > follow["open"]
            )

            if swept and follow_through:
                return {
                    "type": "SELL-SIDE SWEEP",
                    "direction": "BULLISH",
                    "level": level,
                    "index": i,
                    "confirmed": True,
                }

    return None


# ============================================================
# ENTRY LEVELS
# ============================================================

def calculate_levels(candles, zone, atr):
    if not zone or atr <= 0:
        return None

    entry = candles[-1]["close"]

    if zone["direction"] == "BULLISH":
        stop = min(zone["low"], candles[-1]["low"]) - atr * 0.25
        risk = entry - stop

        if risk <= 0:
            return None

        target = entry + risk * 2

    else:
        stop = max(zone["high"], candles[-1]["high"]) + atr * 0.25
        risk = stop - entry

        if risk <= 0:
            return None

        target = entry - risk * 2

    return {
        "entry": entry,
        "stop": stop,
        "target": target,
    }


# ============================================================
# EVALUATION
# ============================================================

def evaluate(symbol, h1, m15):
    h1_structure = analyse_structure(h1)
    m15_structure = analyse_structure(m15)

    atr = calculate_atr(m15)
    price = m15[-1]["close"]

    zone = select_zone(m15, m15_structure, atr)

    retest = validate_retest_rejection(m15, zone)

    sweep = detect_confirmed_sweep(m15, m15_structure)

    h1_direction = h1_structure["direction"]
    m15_direction = m15_structure["direction"]

    aligned = (
        h1_direction in ("BULLISH", "BEARISH")
        and h1_direction == m15_direction
    )

    structure_direction = confirmed_structure_direction(
        m15_structure
    )

    structure_confirmed = (
        structure_direction is not None
        and structure_direction == h1_direction
        and aligned
    )

    zone_confirmed = (
        zone is not None
        and zone["direction"] == h1_direction
    )

    sweep_confirmed = (
        sweep is not None
        and sweep["direction"] == h1_direction
    )

    entry_score = 0

    if aligned:
        entry_score += 20

    if structure_confirmed:
        entry_score += 20

    if zone_confirmed:
        entry_score += 15

    if retest["retest"]:
        entry_score += 10

    if retest["rejection"]:
        entry_score += 15

    if sweep_confirmed:
        entry_score += 20

    # Strict entry gate: every condition is mandatory.
    entry_valid = all([
        aligned,
        structure_confirmed,
        zone_confirmed,
        retest["retest"],
        retest["rejection"],
        sweep_confirmed,
        entry_score >= MIN_ENTRY_SCORE,
    ])

    levels = None

    if entry_valid:
        levels = calculate_levels(m15, zone, atr)

        if levels is None:
            entry_valid = False

    watch_score = 0

    if h1_direction != "RANGING":
        watch_score += 15

    if m15_direction != "RANGING":
        watch_score += 15

    if zone:
        watch_score += min(25, int(zone["quality"] * 0.25))

    if sweep:
        watch_score += 10

    if structure_direction:
        watch_score += 20

    if aligned:
        watch_score += 15

    if entry_valid:
        status = "CONFIRMED ENTRY CONDITIONS"
    elif watch_score >= 20:
        status = "WATCHLIST"
    else:
        status = "NO SETUP"

    buy_liquidity, sell_liquidity = detect_liquidity(m15, atr)

    return {
        "symbol": symbol,
        "price": price,
        "status": status,
        "h1": h1_structure,
        "m15": m15_structure,
        "zone": zone,
        "retest": retest,
        "sweep": sweep,
        "aligned": aligned,
        "watch_score": min(100, watch_score),
        "entry_score": entry_score,
        "entry_valid": entry_valid,
        "levels": levels,
        "buy_liquidity": buy_liquidity,
        "sell_liquidity": sell_liquidity,
    }


# ============================================================
# REPORT
# ============================================================

def fmt(value, symbol):
    if value is None:
        return "N/A"

    digits = 3 if "JPY" in symbol else (2 if "XAU" in symbol else 5)
    return f"{value:.{digits}f}"


def liquidity_text(levels, symbol):
    if not levels:
        return "None"

    return ", ".join(
        f"{fmt(x['price'], symbol)} ({x['touches']} touches)"
        for x in levels
    )


def format_result(r):
    symbol = r["symbol"]
    h1 = r["h1"]
    m15 = r["m15"]
    zone = r["zone"]
    sweep = r["sweep"]

    lines = [
        f"{symbol} — {r['status']}",
        f"H1 {h1['direction']} | M15 {m15['direction']}",
        (
            f"BOS {m15['bos']} | CHOCH {m15['choch']} | "
            f"RANGE BREAK {m15['range_break']}"
        ),
        f"Price {fmt(r['price'], symbol)}",
        (
            f"Watchlist {r['watch_score']}/100 | "
            f"Entry {r['entry_score']}/100"
        ),
    ]

    if zone:
        lines.append(
            f"Zone {zone['type']} {zone['direction']} "
            f"{fmt(zone['low'], symbol)}–{fmt(zone['high'], symbol)} | "
            f"Quality {zone['quality']}/100 | "
            f"Age {zone['age']} | Distance {zone['distance']:.2f} ATR"
        )
        lines.append(
            f"Retest: {'YES' if r['retest']['retest'] else 'NO'} | "
            f"Rejection: {'YES' if r['retest']['rejection'] else 'NO'}"
        )
    else:
        lines.append("Zone: No fresh qualifying zone")

    if sweep:
        lines.append(
            f"Sweep: {sweep['type']} at {fmt(sweep['level'], symbol)} "
            f"— CONFIRMED"
        )
    else:
        lines.append("Confirmed liquidity sweep: None")

    lines.append(
        "Potential buy-side liquidity: "
        + liquidity_text(r["buy_liquidity"], symbol)
    )
    lines.append(
        "Potential sell-side liquidity: "
        + liquidity_text(r["sell_liquidity"], symbol)
    )

    if r["entry_valid"] and r["levels"]:
        lv = r["levels"]
        lines.extend([
            "ENTRY CONDITIONS CONFIRMED — ALERT ONLY",
            f"Direction: {r['h1']['direction']}",
            f"Entry reference: {fmt(lv['entry'], symbol)}",
            f"Stop loss: {fmt(lv['stop'], symbol)}",
            f"Take profit: {fmt(lv['target'], symbol)}",
            "Risk/reward reference: 1:2",
        ])
    else:
        lines.append("No entry alert. Wait for every condition to confirm.")

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram configuration")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=25,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {result}")


def send_long_message(message):
    chunks = []
    current = ""

    for line in message.splitlines():
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = ""

        current += line + "\n"

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_KEY:
        raise RuntimeError("Twelve Data API key missing")

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram credentials missing")

    scan_time = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    results = []

    for symbol in SYMBOLS:
        try:
            print(f"Scanning {symbol}")

            h1 = fetch_candles(symbol, "H1")
            time.sleep(REQUEST_DELAY)

            m15 = fetch_candles(symbol, "M15")
            time.sleep(REQUEST_DELAY)

            result = evaluate(symbol, h1, m15)
            results.append(result)

            print(
                f"{symbol}: {result['status']} | "
                f"Entry score {result['entry_score']}"
            )

        except Exception as error:
            print(f"ERROR {symbol}: {error}")
            results.append({
                "symbol": symbol,
                "error": str(error),
            })

    report = [
        "🤖 ODJ AI SMART TRADER V9",
        f"Scan: {scan_time}",
        "Mode: ALERT ONLY — NO AUTOMATIC TRADING",
        "Strict H1 + M15 entry validation",
        "",
    ]

    for result in results:
        if "error" in result:
            report.append(
                f"{result['symbol']} — SCAN ERROR\n{result['error']}"
            )
        else:
            report.append(format_result(result))

        report.append("------------------------")

    report.append(
        "Entry alerts require alignment, BOS/CHOCH, fresh zone, "
        "post-formation retest, rejection and confirmed sweep. "
        "These are conditional signals, not guaranteed outcomes."
    )

    # Regular report every workflow run.
    send_long_message("\n".join(report))

    # Separate Telegram alert only when every entry condition passes.
    for result in results:
        if result.get("entry_valid"):
            alert = (
                "🚨 ODJ AI SMART TRADER — ENTRY CONDITIONS CONFIRMED\n\n"
                + format_result(result)
                + "\n\nALERT ONLY. Verify spread, execution and risk "
                  "before considering any trade."
            )
            send_long_message(alert)

    print("V9 scan completed.")


if __name__ == "__main__":
    main()
