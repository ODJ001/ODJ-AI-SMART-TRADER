
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from statistics import median

# ============================================================
# ODJ AI SMART TRADER V8
# Alert-only forex market structure scanner
# ============================================================

BASE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

TIMEFRAMES = {
    "H1": {"interval": "1h", "minutes": 60},
    "M15": {"interval": "15min", "minutes": 15},
}

CANDLE_LIMIT = 100
REQUEST_DELAY = 8

MAX_ZONE_AGE = 32          # M15 candles: maximum 8 hours
MAX_ZONE_DISTANCE_ATR = 2.5
MIN_ZONE_QUALITY = 45
MIN_ENTRY_SCORE = 80

TWELVE_DATA_API_KEY = (
    os.getenv("TWELVE_DATA_API_KEY")
    or os.getenv("TWELVEDATA_API_KEY")
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# DATA
# ============================================================

def parse_timestamp(value):
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_candles(symbol, timeframe):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY secret")

    settings = TIMEFRAMES[timeframe]

    params = {
        "symbol": symbol,
        "interval": settings["interval"],
        "outputsize": CANDLE_LIMIT,
        "order": "ASC",
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }

    response = requests.get(BASE_URL, params=params, timeout=25)
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(
            f"{symbol} {timeframe}: {data.get('message', data)}"
        )

    now = datetime.now(timezone.utc)
    duration = timedelta(minutes=settings["minutes"])
    candles = []

    for item in data["values"]:
        timestamp = parse_timestamp(item.get("datetime"))
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
                "volume": float(item.get("volume") or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue

    candles.sort(key=lambda candle: candle["time"])

    if len(candles) < 30:
        raise RuntimeError(
            f"Not enough completed candles for {symbol} {timeframe}"
        )

    return candles


# ============================================================
# TECHNICAL CALCULATIONS
# ============================================================

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        ranges.append(true_range)

    return sum(ranges[-period:]) / period


def find_swings(candles, left=2, right=2):
    highs = []
    lows = []

    for i in range(left, len(candles) - right):
        high = candles[i]["high"]
        low = candles[i]["low"]

        if all(
            high > candles[j]["high"]
            for j in range(i - left, i + right + 1)
            if j != i
        ):
            highs.append({
                "index": i,
                "price": high,
                "time": candles[i]["time"],
            })

        if all(
            low < candles[j]["low"]
            for j in range(i - left, i + right + 1)
            if j != i
        ):
            lows.append({
                "index": i,
                "price": low,
                "time": candles[i]["time"],
            })

    return highs, lows


def determine_trend(highs, lows):
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


# ============================================================
# MARKET STRUCTURE
# ============================================================

def analyse_structure(candles):
    highs, lows = find_swings(candles)
    trend = determine_trend(highs, lows)

    result = {
        "direction": trend,
        "bos": "NONE",
        "choch": "NONE",
        "range_break": "NONE",
        "swing_high": highs[-1]["price"] if highs else None,
        "swing_low": lows[-1]["price"] if lows else None,
        "highs": highs,
        "lows": lows,
    }

    if not highs or not lows:
        return result

    latest = candles[-1]
    previous_high = highs[-1]["price"]
    previous_low = lows[-1]["price"]

    # Do not classify a break using a swing formed by the
    # current candle itself.
    if highs[-1]["index"] >= len(candles) - 2:
        previous_high = highs[-2]["price"] if len(highs) > 1 else None

    if lows[-1]["index"] >= len(candles) - 2:
        previous_low = lows[-2]["price"] if len(lows) > 1 else None

    bullish_break = (
        previous_high is not None
        and latest["close"] > previous_high
        and candles[-2]["close"] <= previous_high
    )

    bearish_break = (
        previous_low is not None
        and latest["close"] < previous_low
        and candles[-2]["close"] >= previous_low
    )

    if bullish_break:
        if trend == "BEARISH":
            result["choch"] = "BULLISH"
        elif trend == "RANGING":
            result["range_break"] = "BULLISH"
        else:
            result["bos"] = "BULLISH"

    elif bearish_break:
        if trend == "BULLISH":
            result["choch"] = "BEARISH"
        elif trend == "RANGING":
            result["range_break"] = "BEARISH"
        else:
            result["bos"] = "BEARISH"

    return result


# ============================================================
# LIQUIDITY CLUSTERING
# ============================================================

def cluster_levels(levels, tolerance):
    """
    Group nearby swing levels without allowing unlimited
    chained clustering. These are potential swing-liquidity
    areas, not confirmed resting order-book liquidity.
    """
    if not levels:
        return []

    sorted_levels = sorted(levels, key=lambda x: x["price"])
    groups = []

    for level in sorted_levels:
        if not groups:
            groups.append([level])
            continue

        group = groups[-1]
        center = median([item["price"] for item in group])

        if abs(level["price"] - center) <= tolerance:
            group.append(level)
        else:
            groups.append([level])

    output = []

    for group in groups:
        if len(group) < 2:
            continue

        prices = [item["price"] for item in group]

        output.append({
            "price": median(prices),
            "low": min(prices),
            "high": max(prices),
            "touches": len(group),
        })

    return output


def detect_liquidity(candles, atr):
    highs, lows = find_swings(candles)

    tolerance = max(atr * 0.12, 0.00001)

    buy_levels = cluster_levels(highs, tolerance)
    sell_levels = cluster_levels(lows, tolerance)

    buy_levels.sort(key=lambda x: abs(x["price"] - candles[-1]["close"]))
    sell_levels.sort(key=lambda x: abs(x["price"] - candles[-1]["close"]))

    return buy_levels[:3], sell_levels[:3]


# ============================================================
# FAIR VALUE GAPS
# ============================================================

def detect_fvg(candles, atr):
    zones = []

    if len(candles) < 4 or atr <= 0:
        return zones

    current_price = candles[-1]["close"]

    for i in range(2, len(candles)):
        first = candles[i - 2]
        third = candles[i]

        age = len(candles) - 1 - i

        if age > MAX_ZONE_AGE:
            continue

        # Bullish FVG
        if third["low"] > first["high"]:
            low = first["high"]
            high = third["low"]
            direction = "BULLISH"

        # Bearish FVG
        elif third["high"] < first["low"]:
            low = third["high"]
            high = first["low"]
            direction = "BEARISH"

        else:
            continue

        midpoint = (low + high) / 2
        distance = abs(current_price - midpoint) / atr

        # Hard distance filter: distant zones are discarded.
        if distance > MAX_ZONE_DISTANCE_ATR:
            continue

        # Reject tiny gaps.
        gap_size_atr = (high - low) / atr
        if gap_size_atr < 0.08:
            continue

        # A later candle entering the zone means it is no
        # longer a fresh untouched zone.
        mitigated = False

        for candle in candles[i + 1:-1]:
            if candle["low"] <= high and candle["high"] >= low:
                mitigated = True
                break

        if mitigated:
            continue

        distance_score = max(0, 35 - distance * 12)
        size_score = min(30, gap_size_atr * 35)
        freshness_score = max(0, 35 - age)

        quality = int(
            min(100, distance_score + size_score + freshness_score)
        )

        if quality < MIN_ZONE_QUALITY:
            continue

        zones.append({
            "type": "FVG",
            "direction": direction,
            "low": low,
            "high": high,
            "midpoint": midpoint,
            "age": age,
            "distance_atr": round(distance, 2),
            "quality": quality,
            "index": i,
        })

    return zones


# ============================================================
# ORDER BLOCKS
# ============================================================

def detect_order_blocks(candles, structure, atr):
    zones = []

    if atr <= 0:
        return zones

    direction = None

    if structure["bos"] != "NONE":
        direction = structure["bos"]
    elif structure["choch"] != "NONE":
        direction = structure["choch"]

    if direction is None:
        return zones

    current_price = candles[-1]["close"]
    start = max(1, len(candles) - 12)

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

        if distance > MAX_ZONE_DISTANCE_ATR:
            continue

        mitigated = False

        for later in candles[i + 1:-1]:
            if later["low"] <= high and later["high"] >= low:
                mitigated = True
                break

        if mitigated:
            continue

        quality = int(max(0, 80 - distance * 15 - (len(candles) - 1 - i)))

        if quality < MIN_ZONE_QUALITY:
            continue

        zones.append({
            "type": "OB",
            "direction": direction,
            "low": low,
            "high": high,
            "midpoint": midpoint,
            "age": len(candles) - 1 - i,
            "distance_atr": round(distance, 2),
            "quality": quality,
            "index": i,
        })

        break

    return zones


def select_best_zone(candles, atr, structure):
    zones = detect_fvg(candles, atr)
    zones += detect_order_blocks(candles, structure, atr)

    if not zones:
        return None

    zones.sort(
        key=lambda z: (
            z["quality"],
            -z["distance_atr"],
            -z["age"],
        ),
        reverse=True,
    )

    return zones[0]


# ============================================================
# LIQUIDITY SWEEP AND FOLLOW-THROUGH
# ============================================================

def detect_sweep(candles, highs, lows, atr):
    if len(candles) < 12 or atr <= 0:
        return None

    # Examine recent swing levels. A sweep is only confirmed
    # when a later completed candle closes back through the level.
    recent_start = max(0, len(candles) - 12)

    for i in range(len(candles) - 2, recent_start - 1, -1):
        candle = candles[i]

        previous_highs = [
            item["price"] for item in highs
            if item["index"] < i
        ]
        previous_lows = [
            item["price"] for item in lows
            if item["index"] < i
        ]

        if previous_highs:
            level = max(previous_highs[-3:])

            swept_high = candle["high"] > level and candle["close"] < level

            if swept_high:
                follow = candles[i + 1]

                confirmed = (
                    follow["close"] < level
                    and follow["close"] < follow["open"]
                )

                return {
                    "type": "BUY-SIDE SWEEP",
                    "direction": "BEARISH",
                    "level": level,
                    "confirmed": confirmed,
                    "time": candle["time"],
                }

        if previous_lows:
            level = min(previous_lows[-3:])

            swept_low = candle["low"] < level and candle["close"] > level

            if swept_low:
                follow = candles[i + 1]

                confirmed = (
                    follow["close"] > level
                    and follow["close"] > follow["open"]
                )

                return {
                    "type": "SELL-SIDE SWEEP",
                    "direction": "BULLISH",
                    "level": level,
                    "confirmed": confirmed,
                    "time": candle["time"],
                }

    return None


# ============================================================
# RETEST AND REJECTION
# ============================================================

def validate_retest_rejection(candles, zone):
    if zone is None:
        return False

    candle = candles[-1]

    overlaps = (
        candle["low"] <= zone["high"]
        and candle["high"] >= zone["low"]
    )

    if not overlaps:
        return False

    candle_range = candle["high"] - candle["low"]

    if candle_range <= 0:
        return False

    body = abs(candle["close"] - candle["open"])
    close_position = (
        candle["close"] - candle["low"]
    ) / candle_range

    if zone["direction"] == "BULLISH":
        rejection = (
            candle["close"] > candle["open"]
            and close_position >= 0.65
            and body / candle_range >= 0.25
        )

    else:
        rejection = (
            candle["close"] < candle["open"]
            and close_position <= 0.35
            and body / candle_range >= 0.25
        )

    return rejection


# ============================================================
# RISK LEVELS
# ============================================================

def calculate_trade_levels(candles, zone, atr):
    if zone is None or atr <= 0:
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
        "risk": risk,
        "reward_risk": 2.0,
    }


# ============================================================
# MARKET EVALUATION
# ============================================================

def evaluate_market(symbol, h1, m15):
    h1_structure = analyse_structure(h1)
    m15_structure = analyse_structure(m15)

    atr = calculate_atr(m15)

    price = m15[-1]["close"]

    zone = select_best_zone(m15, atr, m15_structure)

    sweep = detect_sweep(
        m15,
        m15_structure["highs"],
        m15_structure["lows"],
        atr,
    )

    buy_liquidity, sell_liquidity = detect_liquidity(m15, atr)

    h1_direction = h1_structure["direction"]
    m15_direction = m15_structure["direction"]

    aligned = (
        h1_direction in ("BULLISH", "BEARISH")
        and h1_direction == m15_direction
    )

    retest = validate_retest_rejection(m15, zone)

    sweep_confirmed = bool(sweep and sweep["confirmed"])

    watch_score = 0
    entry_score = 0

    # Watchlist score only rewards valid, nearby, fresh zones.
    if h1_direction != "RANGING":
        watch_score += 15

    if m15_direction != "RANGING":
        watch_score += 15

    if zone:
        watch_score += min(25, int(zone["quality"] * 0.25))

    if sweep:
        watch_score += 10 if sweep_confirmed else 5

    if m15_structure["bos"] != "NONE" or m15_structure["choch"] != "NONE":
        watch_score += 20

    if aligned:
        entry_score += 25

    if zone:
        entry_score += 20

    if retest:
        entry_score += 15

    if sweep_confirmed:
        entry_score += 20

    if (
        m15_structure["bos"] != "NONE"
        or m15_structure["choch"] != "NONE"
    ):
        entry_score += 10

    if zone and zone["quality"] >= 70:
        entry_score += 10

    direction = None

    if aligned:
        direction = h1_direction

    elif (
        zone
        and sweep_confirmed
        and zone["direction"] == sweep["direction"]
    ):
        direction = zone["direction"]

    trade_levels = None

    valid_entry = (
        aligned
        and direction is not None
        and zone is not None
        and zone["direction"] == direction
        and retest
        and sweep_confirmed
        and sweep["direction"] == direction
        and entry_score >= MIN_ENTRY_SCORE
    )

    if valid_entry:
        trade_levels = calculate_trade_levels(m15, zone, atr)

        if trade_levels is None:
            valid_entry = False

    if valid_entry:
        status = f"CONFIRMED {direction} SETUP"
    elif watch_score >= 20:
        status = "WATCHLIST"
    else:
        status = "NO SETUP"

    return {
        "symbol": symbol,
        "status": status,
        "price": price,
        "h1": h1_structure,
        "m15": m15_structure,
        "aligned": aligned,
        "zone": zone,
        "sweep": sweep,
        "buy_liquidity": buy_liquidity,
        "sell_liquidity": sell_liquidity,
        "watch_score": min(100, watch_score),
        "entry_score": min(100, entry_score),
        "retest": retest,
        "trade_levels": trade_levels,
        "atr": atr,
    }


# ============================================================
# REPORT FORMATTING
# ============================================================

def format_price(value, symbol):
    if value is None:
        return "N/A"

    digits = 3 if "JPY" in symbol else (2 if "XAU" in symbol else 5)
    return f"{value:.{digits}f}"


def format_liquidity(levels, symbol):
    if not levels:
        return "None"

    return ", ".join(
        f"{format_price(item['price'], symbol)} ({item['touches']} touches)"
        for item in levels
    )


def format_result(result):
    symbol = result["symbol"]
    h1 = result["h1"]
    m15 = result["m15"]
    zone = result["zone"]
    sweep = result["sweep"]

    lines = [
        f"{symbol} — {result['status']}",
        f"H1 {h1['direction']} | M15 {m15['direction']}",
        (
            f"BOS {m15['bos']} | CHOCH {m15['choch']} | "
            f"RANGE BREAK {m15['range_break']}"
        ),
        f"Price {format_price(result['price'], symbol)}",
        (
            f"Watchlist score {result['watch_score']}/100 | "
            f"Entry score {result['entry_score']}/100"
        ),
    ]

    if zone:
        lines.append(
            f"Zone {zone['type']} {zone['direction']} "
            f"{format_price(zone['low'], symbol)}–"
            f"{format_price(zone['high'], symbol)} | "
            f"Quality {zone['quality']}/100 | "
            f"Age {zone['age']} candles | "
            f"Distance {zone['distance_atr']} ATR"
        )
        lines.append(
            "Retest and rejection: "
            + ("CONFIRMED" if result["retest"] else "WAITING")
        )
    else:
        lines.append("Zone: No fresh qualifying zone")

    if sweep:
        lines.append(
            f"Liquidity sweep: {sweep['type']} at "
            f"{format_price(sweep['level'], symbol)} — "
            f"{'CONFIRMED' if sweep['confirmed'] else 'UNCONFIRMED'}"
        )
    else:
        lines.append("Liquidity sweep: None")

    lines.append(
        "Potential buy-side liquidity: "
        + format_liquidity(result["buy_liquidity"], symbol)
    )

    lines.append(
        "Potential sell-side liquidity: "
        + format_liquidity(result["sell_liquidity"], symbol)
    )

    if result["trade_levels"]:
        levels = result["trade_levels"]

        lines.extend([
            "CONDITIONAL ENTRY — ALERT ONLY",
            f"Entry reference: {format_price(levels['entry'], symbol)}",
            f"Stop loss: {format_price(levels['stop'], symbol)}",
            f"Take profit: {format_price(levels['target'], symbol)}",
            "Risk/reward: 1:2",
        ])

    elif result["status"] == "WATCHLIST":
        lines.append("No entry signal. Wait for further confirmation.")

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram bot token or chat ID")

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=25,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


# ============================================================
# MAIN
# ============================================================

def main():
    print("Starting ODJ AI SMART TRADER V8")

    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Twelve Data API key is missing")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram configuration is missing")

    report_time = datetime.now(timezone.utc).strftime(
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

            result = evaluate_market(symbol, h1, m15)
            results.append(result)

            print(f"{symbol}: {result['status']}")

        except Exception as error:
            print(f"ERROR scanning {symbol}: {error}")
            results.append({
                "symbol": symbol,
                "error": str(error),
            })

    report = [
        "🤖 ODJ AI SMART TRADER V8",
        f"Scan: {report_time}",
        "Mode: ALERT ONLY — NO AUTOMATIC TRADING",
        "Closed-candle analysis | H1 + M15",
        "Fresh-zone and distance filters enabled",
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
        "Reminder: Potential liquidity levels are swing-based estimates. "
        "Alerts are conditional analysis, not guaranteed trade outcomes."
    )

    message = "\n".join(report)

    # Telegram message limit is 4096 characters.
    if len(message) <= 4000:
        send_telegram(message)
    else:
        chunks = []
        current = ""

        for line in message.split("\n"):
            if len(current) + len(line) + 1 > 3900:
                chunks.append(current)
                current = ""

            current += line + "\n"

        if current:
            chunks.append(current)

        for chunk in chunks:
            send_telegram(chunk)

    print("V8 scan completed and Telegram report sent.")


if __name__ == "__main__":
    main()
