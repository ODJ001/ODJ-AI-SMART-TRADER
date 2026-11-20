
import os
import time
import json
import base64
import requests

from datetime import datetime, timedelta, timezone
from statistics import median

# ============================================================
# ODJ AI SMART TRADER V11
# COMPLETE TRADE SETUP | VERIFIED CANDLES | ALERT ONLY
# ============================================================

VERSION = "V11"

API_KEY = (
    os.getenv("TWELVE_DATA_API_KEY")
    or os.getenv("TWELVEDATA_API_KEY")
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GH_ALERT_TOKEN = os.getenv("GH_ALERT_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")

STATE_FILE = ".odj_alert_state.json"
BASE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

TIMEFRAMES = {
    "H1": {
        "interval": "1h",
        "minutes": 60,
        "max_stale_minutes": 180,
    },
    "M15": {
        "interval": "15min",
        "minutes": 15,
        "max_stale_minutes": 45,
    },
}

OUTPUTSIZE = 120
REQUEST_DELAY = 8

MAX_ZONE_AGE = 32
MAX_ZONE_DISTANCE_ATR = 2.5
MIN_ZONE_QUALITY = 45
MIN_ENTRY_SCORE = 80

MAX_SWEEP_AGE = 4
NEARBY_LIQUIDITY_ATR = 3.0

# Trade risk controls
STOP_BUFFER_ATR = 0.25
MIN_STOP_ATR = 0.15
MAX_STOP_ATR = 2.5

# Profit targets expressed as multiples of initial risk
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.0

ALERT_STATE_DAYS = 30
MAX_STORED_ALERTS = 100


# ============================================================
# TIME AND DATA VERIFICATION
# ============================================================

def parse_time(value):
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

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

        expected_close = timestamp + duration

        # Ignore incomplete candles.
        if expected_close > now:
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

    latest = candles[-1]
    latest_close_time = latest["time"] + duration

    age_minutes = (
        now - latest_close_time
    ).total_seconds() / 60

    maximum_age = settings["max_stale_minutes"]

    if age_minutes > maximum_age:
        raise RuntimeError(
            f"STALE DATA: {symbol} {timeframe}. "
            f"Latest candle ended "
            f"{latest_close_time.strftime('%Y-%m-%d %H:%M UTC')}; "
            f"age {age_minutes:.1f} minutes."
        )

    metadata = {
        "latest_candle_start": latest["time"],
        "latest_candle_close": latest_close_time,
        "data_age_minutes": age_minutes,
        "candles_count": len(candles),
    }

    return candles, metadata


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
            highs.append({
                "index": i,
                "price": high,
            })

        if all(
            low < candles[j]["low"]
            for j in range(i-left, i+right+1)
            if j != i
        ):
            lows.append({
                "index": i,
                "price": low,
            })

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
        "event_index": None,
        "event_level": None,
        "highs": highs,
        "lows": lows,
    }

    if not highs or not lows:
        return result

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
                result["bos"] = "NONE"
                result["range_break"] = "NONE"

            elif trend == "RANGING":
                result["range_break"] = "BULLISH"
                result["bos"] = "NONE"
                result["choch"] = "NONE"

            else:
                result["bos"] = "BULLISH"
                result["choch"] = "NONE"
                result["range_break"] = "NONE"

        elif bearish_break:
            result["event_index"] = i
            result["event_level"] = bearish_level

            if trend == "BULLISH":
                result["choch"] = "BEARISH"
                result["bos"] = "NONE"
                result["range_break"] = "NONE"

            elif trend == "RANGING":
                result["range_break"] = "BEARISH"
                result["bos"] = "NONE"
                result["choch"] = "NONE"

            else:
                result["bos"] = "BEARISH"
                result["choch"] = "NONE"
                result["range_break"] = "NONE"

    return result


def confirmed_structure_direction(structure):
    if structure["bos"] != "NONE":
        return structure["bos"]

    if structure["choch"] != "NONE":
        return structure["choch"]

    return None


# ============================================================
# LIQUIDITY
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

        center = median([
            x["price"] for x in groups[-1]
        ])

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
    price = candles[-1]["close"]

    buy = cluster_levels(highs, tolerance)
    sell = cluster_levels(lows, tolerance)

    def separate(levels):
        nearby = []
        distant = []

        for level in levels:
            distance = (
                abs(level["price"] - price) / atr
                if atr > 0 else float("inf")
            )

            item = dict(level)
            item["distance_atr"] = distance

            if distance <= NEARBY_LIQUIDITY_ATR:
                nearby.append(item)
            else:
                distant.append(item)

        nearby.sort(key=lambda x: x["distance_atr"])
        distant.sort(key=lambda x: x["distance_atr"])

        return nearby[:3], distant[:3]

    buy_near, buy_far = separate(buy)
    sell_near, sell_far = separate(sell)

    return buy_near, buy_far, sell_near, sell_far


# ============================================================
# FAIR VALUE GAPS
# ============================================================

def detect_fvg(candles, atr):
    zones = []

    if atr <= 0:
        return zones

    current_price = candles[-1]["close"]

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

        previously_mitigated = any(
            candle["low"] <= high
            and candle["high"] >= low
            for candle in candles[i + 1:-1]
        )

        if previously_mitigated:
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
# ORDER BLOCK
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
            direction == "BULLISH"
            and candle["close"] < candle["open"]
        ) or (
            direction == "BEARISH"
            and candle["close"] > candle["open"]
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

        previously_mitigated = any(
            x["low"] <= high and x["high"] >= low
            for x in candles[i + 1:-1]
        )

        if previously_mitigated:
            continue

        quality = int(max(
            0,
            80 - distance * 15 - age
        ))

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


# ============================================================
# DIRECTION-ALIGNED ZONE
# ============================================================

def select_zone(candles, structure, atr, allowed_direction):
    if allowed_direction not in ("BULLISH", "BEARISH"):
        return None

    zones = detect_fvg(candles, atr)

    ob = detect_order_block(candles, structure, atr)

    if ob:
        zones.append(ob)

    zones = [
        zone for zone in zones
        if zone["direction"] == allowed_direction
    ]

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
# RETEST AND REJECTION
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

    if current_index <= zone_index:
        return {
            "retest": False,
            "rejection": False,
            "valid": False,
        }

    last = candles[-1]

    current_touches = (
        last["low"] <= zone["high"]
        and last["high"] >= zone["low"]
    )

    if not current_touches:
        return {
            "retest": False,
            "rejection": False,
            "valid": False,
        }

    candle_range = last["high"] - last["low"]

    if candle_range <= 0:
        return {
            "retest": True,
            "rejection": False,
            "valid": False,
        }

    body_ratio = (
        abs(last["close"] - last["open"]) / candle_range
    )

    close_position = (
        last["close"] - last["low"]
    ) / candle_range

    if zone["direction"] == "BULLISH":
        lower_wick = (
            min(last["open"], last["close"]) - last["low"]
        )

        rejection = (
            last["close"] > last["open"]
            and close_position >= 0.65
            and body_ratio >= 0.25
            and lower_wick > candle_range * 0.20
            and last["close"] > zone["midpoint"]
        )

    else:
        upper_wick = (
            last["high"] - max(last["open"], last["close"])
        )

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

    latest_index = len(candles) - 1
    start = max(3, latest_index - MAX_SWEEP_AGE)

    for i in range(latest_index - 1, start - 1, -1):
        candle = candles[i]
        follow = candles[i + 1]

        prior_highs = [
            x["price"] for x in highs
            if x["index"] <= i - 2
        ]

        prior_lows = [
            x["price"] for x in lows
            if x["index"] <= i - 2
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
                    "age": latest_index - i,
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
                    "age": latest_index - i,
                    "confirmed": True,
                }

    return None


# ============================================================
# V11 COMPLETE TRADE LEVELS
# ============================================================

def calculate_levels(candles, zone, atr):
    if not zone or atr <= 0:
        return None

    last = candles[-1]

    # Entry is the close of the completed rejection candle.
    entry = last["close"]

    if zone["direction"] == "BULLISH":
        stop = min(
            zone["low"],
            last["low"]
        ) - atr * STOP_BUFFER_ATR

        risk = entry - stop

        if risk <= 0:
            return None

        direction = "BUY"

        tp1 = entry + risk * TP1_R
        tp2 = entry + risk * TP2_R
        tp3 = entry + risk * TP3_R

    elif zone["direction"] == "BEARISH":
        stop = max(
            zone["high"],
            last["high"]
        ) + atr * STOP_BUFFER_ATR

        risk = stop - entry

        if risk <= 0:
            return None

        direction = "SELL"

        tp1 = entry - risk * TP1_R
        tp2 = entry - risk * TP2_R
        tp3 = entry - risk * TP3_R

    else:
        return None

    risk_atr = risk / atr

    # Reject stops that are too tight or excessively wide.
    if risk_atr < MIN_STOP_ATR:
        return None

    if risk_atr > MAX_STOP_ATR:
        return None

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "risk": risk,
        "risk_atr": risk_atr,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr1": TP1_R,
        "rr2": TP2_R,
        "rr3": TP3_R,
        "entry_candle_time": last["time"],
    }


# ============================================================
# EVALUATION
# ============================================================

def evaluate(symbol, h1, m15, h1_meta, m15_meta):
    h1_structure = analyse_structure(h1)
    m15_structure = analyse_structure(m15)

    atr = calculate_atr(m15)
    price = m15[-1]["close"]

    h1_direction = h1_structure["direction"]
    m15_direction = m15_structure["direction"]

    aligned = (
        h1_direction in ("BULLISH", "BEARISH")
        and h1_direction == m15_direction
    )

    zone = select_zone(
        m15,
        m15_structure,
        atr,
        h1_direction,
    )

    retest = validate_retest_rejection(m15, zone)

    sweep = detect_confirmed_sweep(
        m15,
        m15_structure
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

    entry_conditions = all([
        aligned,
        structure_confirmed,
        zone_confirmed,
        retest["retest"],
        retest["rejection"],
        sweep_confirmed,
        entry_score >= MIN_ENTRY_SCORE,
    ])

    levels = None
    entry_valid = False
    level_rejection_reason = None

    if entry_conditions:
        levels = calculate_levels(m15, zone, atr)

        if levels is not None:
            entry_valid = True
        else:
            level_rejection_reason = (
                "Trade rejected: invalid or excessive stop distance."
            )

    watch_score = 0

    if h1_direction != "RANGING":
        watch_score += 15

    if m15_direction != "RANGING":
        watch_score += 15

    if zone:
        watch_score += min(
            25,
            int(zone["quality"] * 0.25)
        )

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

    (
        buy_near,
        buy_far,
        sell_near,
        sell_far,
    ) = detect_liquidity(m15, atr)

    return {
        "symbol": symbol,
        "price": price,
        "atr": atr,
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
        "level_rejection_reason": level_rejection_reason,
        "buy_near": buy_near,
        "buy_far": buy_far,
        "sell_near": sell_near,
        "sell_far": sell_far,
        "h1_meta": h1_meta,
        "m15_meta": m15_meta,
    }


# ============================================================
# PRICE FORMATTING
# ============================================================

def fmt(value, symbol):
    if value is None:
        return "N/A"

    digits = (
        3 if "JPY" in symbol
        else 2 if "XAU" in symbol
        else 5
    )

    return f"{value:.{digits}f}"


def liquidity_text(levels, symbol):
    if not levels:
        return "None"

    return ", ".join(
        f"{fmt(x['price'], symbol)} "
        f"({x['touches']} touches)"
        for x in levels
    )


# ============================================================
# V11 TRADE PLAN FORMAT
# ============================================================

def format_trade_plan(result):
    symbol = result["symbol"]
    lv = result["levels"]

    direction_emoji = (
        "🟢" if lv["direction"] == "BUY" else "🔴"
    )

    return "\n".join([
        "━━━━━━━━━━━━━━━━━━",
        f"{direction_emoji} {lv['direction']} {symbol}",
        "🚨 ENTRY CONDITIONS CONFIRMED",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"Entry reference: {fmt(lv['entry'], symbol)}",
        f"Stop Loss: {fmt(lv['stop'], symbol)}",
        "",
        f"Take Profit 1: {fmt(lv['tp1'], symbol)}  (1:1)",
        f"Take Profit 2: {fmt(lv['tp2'], symbol)}  (1:2)",
        f"Take Profit 3: {fmt(lv['tp3'], symbol)}  (1:3)",
        "",
        f"Stop distance: {fmt(lv['risk'], symbol)}",
        f"Stop distance: {lv['risk_atr']:.2f} ATR",
        "",
        f"H1 direction: {result['h1']['direction']}",
        f"M15 direction: {result['m15']['direction']}",
        f"BOS: {result['m15']['bos']}",
        f"CHOCH: {result['m15']['choch']}",
        f"Zone: {result['zone']['type']} "
        f"{result['zone']['direction']}",
        f"Zone quality: {result['zone']['quality']}/100",
        f"Retest: {'YES' if result['retest']['retest'] else 'NO'}",
        f"Rejection: {'YES' if result['retest']['rejection'] else 'NO'}",
        f"Sweep: {result['sweep']['type']}",
        f"Sweep age: {result['sweep']['age']} M15 candles",
        "",
        "Entry reference is based on the completed M15 candle close.",
        "Check the live price before execution. Do not chase a missed entry.",
        "Alert only — no automatic trading.",
        "━━━━━━━━━━━━━━━━━━",
    ])


# ============================================================
# SCAN REPORT FORMAT
# ============================================================

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
        (
            "H1 candle ended: "
            f"{r['h1_meta']['latest_candle_close'].strftime('%Y-%m-%d %H:%M UTC')} "
            f"| Data age: {r['h1_meta']['data_age_minutes']:.1f} min"
        ),
        (
            "M15 candle ended: "
            f"{r['m15_meta']['latest_candle_close'].strftime('%Y-%m-%d %H:%M UTC')} "
            f"| Data age: {r['m15_meta']['data_age_minutes']:.1f} min"
        ),
    ]

    if zone:
        lines.append(
            f"Aligned zone {zone['type']} {zone['direction']} "
            f"{fmt(zone['low'], symbol)}–"
            f"{fmt(zone['high'], symbol)} | "
            f"Quality {zone['quality']}/100 | "
            f"Age {zone['age']} | "
            f"Distance {zone['distance']:.2f} ATR"
        )

        lines.append(
            f"Retest: {'YES' if r['retest']['retest'] else 'NO'} | "
            f"Rejection: {'YES' if r['retest']['rejection'] else 'NO'}"
        )

    else:
        lines.append(
            "Zone: No fresh qualifying zone aligned with H1 direction"
        )

    if sweep:
        lines.append(
            f"Sweep: {sweep['type']} at "
            f"{fmt(sweep['level'], symbol)} "
            f"— CONFIRMED | Age {sweep['age']} M15 candles"
        )

    else:
        lines.append("Confirmed fresh liquidity sweep: None")

    lines.append(
        "Nearby buy-side liquidity (within 3 ATR): "
        + liquidity_text(r["buy_near"], symbol)
    )

    lines.append(
        "Distant buy-side liquidity (over 3 ATR): "
        + liquidity_text(r["buy_far"], symbol)
    )

    lines.append(
        "Nearby sell-side liquidity (within 3 ATR): "
        + liquidity_text(r["sell_near"], symbol)
    )

    lines.append(
        "Distant sell-side liquidity (over 3 ATR): "
        + liquidity_text(r["sell_far"], symbol)
    )

    if r["entry_valid"] and r["levels"]:
        lv = r["levels"]

        lines.extend([
            "",
            "ENTRY CONDITIONS CONFIRMED",
            f"Direction: {lv['direction']}",
            f"Entry: {fmt(lv['entry'], symbol)}",
            f"SL: {fmt(lv['stop'], symbol)}",
            f"TP1: {fmt(lv['tp1'], symbol)}",
            f"TP2: {fmt(lv['tp2'], symbol)}",
            f"TP3: {fmt(lv['tp3'], symbol)}",
            "Risk/reward: 1:1 | 1:2 | 1:3",
        ])

    else:
        lines.append(
            "No entry alert. Wait for every condition to confirm."
        )

        if r.get("level_rejection_reason"):
            lines.append(r["level_rejection_reason"])

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram configuration")

    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/sendMessage"
    )

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
        raise RuntimeError(
            f"Telegram rejected message: {result}"
        )


def send_long_message(message):
    chunks = []
    current = ""

    for line in message.splitlines():
        if len(current) + len(line) + 1 > 3900:
            if current:
                chunks.append(current)

            current = ""

        current += line + "\n"

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)


# ============================================================
# PERSISTENT DUPLICATE ALERT PROTECTION
# ============================================================

def github_headers():
    return {
        "Authorization": f"Bearer {GH_ALERT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_state_url():
    if not GITHUB_REPOSITORY:
        return None

    return (
        "https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
    )


def load_alert_state():
    if not GH_ALERT_TOKEN or not GITHUB_REPOSITORY:
        print(
            "WARNING: GH_ALERT_TOKEN not configured. "
            "Persistent duplicate protection is disabled."
        )

        return {"alerts": {}}, None, False

    try:
        response = requests.get(
            github_state_url(),
            headers=github_headers(),
            timeout=20,
        )

        if response.status_code == 404:
            return {"alerts": {}}, None, True

        response.raise_for_status()

        data = response.json()

        raw = base64.b64decode(
            data["content"].replace("\n", "")
        ).decode("utf-8")

        state = json.loads(raw)

        if not isinstance(state, dict):
            state = {}

        if not isinstance(state.get("alerts"), dict):
            state["alerts"] = {}

        return state, data.get("sha"), True

    except Exception as error:
        print(
            "WARNING: Could not load GitHub alert state: "
            f"{error}"
        )

        return {"alerts": {}}, None, False


def save_alert_state(state, sha):
    if not GH_ALERT_TOKEN or not GITHUB_REPOSITORY:
        return False

    try:
        raw = json.dumps(
            state,
            indent=2,
            sort_keys=True,
        )

        encoded = base64.b64encode(
            raw.encode("utf-8")
        ).decode("utf-8")

        payload = {
            "message": "Update ODJ Smart Trader V11 alert state",
            "content": encoded,
        }

        if sha:
            payload["sha"] = sha

        response = requests.put(
            github_state_url(),
            headers=github_headers(),
            json=payload,
            timeout=25,
        )

        response.raise_for_status()

        print("Persistent alert state saved to GitHub.")
        return True

    except Exception as error:
        print(
            "WARNING: Could not save GitHub alert state: "
            f"{error}"
        )

        return False


def alert_signature(result):
    zone = result["zone"]
    sweep = result["sweep"]
    symbol = result["symbol"]

    digits = (
        3 if "JPY" in symbol
        else 2 if "XAU" in symbol
        else 5
    )

    return "|".join([
        symbol,
        zone["direction"],
        zone["type"],
        str(round(zone["low"], digits)),
        str(round(zone["high"], digits)),
        str(round(sweep["level"], digits)),
    ])


def prepare_entry_alerts(results, state, persistence_available):
    alerts = []
    stored = state.get("alerts", {})

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ALERT_STATE_DAYS)

    cleaned = {}

    for signature, timestamp in stored.items():
        alert_time = parse_time(timestamp)

        if alert_time and alert_time >= cutoff:
            cleaned[signature] = timestamp

    stored = cleaned

    for result in results:
        if not result.get("entry_valid"):
            continue

        signature = alert_signature(result)

        if persistence_available and signature in stored:
            print(
                f"Duplicate alert suppressed: {result['symbol']}"
            )
            continue

        alert = (
            f"🤖 ODJ AI SMART TRADER {VERSION}\n"
            + format_trade_plan(result)
        )

        alerts.append({
            "signature": signature,
            "message": alert,
        })

    return alerts, stored


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

            h1, h1_meta = fetch_candles(symbol, "H1")
            time.sleep(REQUEST_DELAY)

            m15, m15_meta = fetch_candles(symbol, "M15")
            time.sleep(REQUEST_DELAY)

            result = evaluate(
                symbol,
                h1,
                m15,
                h1_meta,
                m15_meta,
            )

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
        f"🤖 ODJ AI SMART TRADER {VERSION}",
        f"Scan: {scan_time}",
        "Mode: ALERT ONLY — NO AUTOMATIC TRADING",
        "Verified completed H1 and M15 candles",
        "H1 maximum data age: 180 minutes",
        "M15 maximum data age: 45 minutes",
        "Strict direction-aligned zone validation",
        "Fresh sweep limit: 4 completed M15 candles",
        "Nearby / distant liquidity classification",
        "V11 complete trade levels enabled",
        "TP1 = 1:1 | TP2 = 1:2 | TP3 = 1:3",
        f"Maximum stop distance: {MAX_STOP_ATR} ATR",
        "",
    ]

    for result in results:
        if "error" in result:
            report.append(
                f"{result['symbol']} — SCAN ERROR\n"
                f"{result['error']}"
            )

        else:
            report.append(format_result(result))

        report.append("------------------------")

    report.append(
        "Entry alerts require H1/M15 alignment, "
        "BOS/CHOCH, a fresh direction-aligned zone, "
        "current-candle retest, rejection and a fresh "
        "confirmed liquidity sweep. "
        "Trade levels are calculated references, not guaranteed fills."
    )

    # Send regular market report.
    send_long_message("\n".join(report))

    # Duplicate-alert protection.
    state, sha, persistence_available = load_alert_state()

    alerts, stored = prepare_entry_alerts(
        results,
        state,
        persistence_available,
    )

    successfully_sent = []

    for item in alerts:
        try:
            send_long_message(item["message"])

            successfully_sent.append(item["signature"])

            print(
                f"Entry alert sent: {item['signature']}"
            )

        except Exception as error:
            print(f"Entry alert failed: {error}")

    if persistence_available and successfully_sent:
        now_text = datetime.now(timezone.utc).isoformat()

        for signature in successfully_sent:
            stored[signature] = now_text

        ordered = sorted(
            stored.items(),
            key=lambda item: item[1],
            reverse=True,
        )

        stored = dict(ordered[:MAX_STORED_ALERTS])

        state["alerts"] = stored

        save_alert_state(state, sha)

    elif not persistence_available:
        print(
            "Persistent duplicate protection unavailable. "
            "Entry alerts may repeat on later scans."
        )

    print(f"{VERSION} scan completed.")


if __name__ == "__main__":
    main()
