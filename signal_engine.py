
import os
import time
import requests
from datetime import datetime, timezone

# ==========================================
# ODJ AI SMART TRADER V4
# PRECISION FILTER
# ==========================================

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.twelvedata.com"

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

CANDLE_LIMIT = 100
REQUEST_DELAY = 9
RR_RATIO = 2.0
ATR_PERIOD = 14
SWING_LEFT = 2
SWING_RIGHT = 2
BREAK_LOOKBACK = 12
ZONE_LOOKBACK = 40

session = requests.Session()


# ==========================================
# MARKET DATA
# ==========================================

def get_candles(symbol, interval):
    if not API_KEY:
        raise RuntimeError("Missing Twelve Data API key")

    response = session.get(
        f"{BASE_URL}/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": CANDLE_LIMIT,
            "apikey": API_KEY,
            "timezone": "UTC"
        },
        timeout=30
    )

    response.raise_for_status()
    data = response.json()

    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Market data error"))

    if "values" not in data:
        raise RuntimeError("No candle data returned")

    candles = []

    for item in reversed(data["values"]):
        candles.append({
            "datetime": item["datetime"],
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"])
        })

    if len(candles) < 30:
        raise RuntimeError("Insufficient candle history")

    # Do not analyse the currently forming candle.
    return candles[:-1]


# ==========================================
# SWINGS
# ==========================================

def find_swings(candles):
    highs = []
    lows = []

    for i in range(SWING_LEFT, len(candles) - SWING_RIGHT):
        current = candles[i]

        surrounding = (
            candles[i-SWING_LEFT:i]
            + candles[i+1:i+SWING_RIGHT+1]
        )

        if all(current["high"] > c["high"] for c in surrounding):
            highs.append({
                "index": i,
                "price": current["high"]
            })

        if all(current["low"] < c["low"] for c in surrounding):
            lows.append({
                "index": i,
                "price": current["low"]
            })

    return highs, lows


# ==========================================
# STRUCTURE AND RECENT BREAK DETECTION
# ==========================================

def analyse_structure(candles):
    highs, lows = find_swings(candles)

    result = {
        "direction": "RANGING",
        "bos": "NONE",
        "choch": "NONE",
        "sweep": "NONE",
        "swing_high": None,
        "swing_low": None,
        "break_index": None,
        "break_type": "NONE"
    }

    if len(highs) < 2 or len(lows) < 2:
        return result

    last_highs = highs[-2:]
    last_lows = lows[-2:]

    higher_high = last_highs[-1]["price"] > last_highs[-2]["price"]
    higher_low = last_lows[-1]["price"] > last_lows[-2]["price"]

    lower_high = last_highs[-1]["price"] < last_highs[-2]["price"]
    lower_low = last_lows[-1]["price"] < last_lows[-2]["price"]

    if higher_high and higher_low:
        trend = "BULLISH"
    elif lower_high and lower_low:
        trend = "BEARISH"
    else:
        trend = "RANGING"

    result["direction"] = trend
    result["swing_high"] = last_highs[-1]["price"]
    result["swing_low"] = last_lows[-1]["price"]

    # Look for recent close-based breaks of confirmed swings.
    recent_start = max(1, len(candles) - BREAK_LOOKBACK)
    events = []

    for i in range(recent_start, len(candles)):
        previous = candles[i-1]
        current = candles[i]

        available_highs = [s for s in highs if s["index"] < i]
        available_lows = [s for s in lows if s["index"] < i]

        if available_highs:
            level = available_highs[-1]["price"]

            if previous["close"] <= level and current["close"] > level:
                events.append({
                    "index": i,
                    "direction": "BULLISH",
                    "level": level
                })

        if available_lows:
            level = available_lows[-1]["price"]

            if previous["close"] >= level and current["close"] < level:
                events.append({
                    "index": i,
                    "direction": "BEARISH",
                    "level": level
                })

    if events:
        event = events[-1]
        result["break_index"] = event["index"]
        result["break_type"] = event["direction"]

        if event["direction"] == "BULLISH":
            result["direction"] = "BULLISH"

            if trend == "BEARISH":
                result["choch"] = "BULLISH"
            else:
                result["bos"] = "BULLISH"

        else:
            result["direction"] = "BEARISH"

            if trend == "BULLISH":
                result["choch"] = "BEARISH"
            else:
                result["bos"] = "BEARISH"

    # Latest confirmed swing levels for liquidity checks.
    latest = candles[-1]

    swing_high = result["swing_high"]
    swing_low = result["swing_low"]

    if (
        latest["low"] < swing_low
        and latest["close"] > swing_low
    ):
        result["sweep"] = "SELL-SIDE LIQUIDITY"

    elif (
        latest["high"] > swing_high
        and latest["close"] < swing_high
    ):
        result["sweep"] = "BUY-SIDE LIQUIDITY"

    return result


# ==========================================
# ATR
# ==========================================

def calculate_atr(candles, period=ATR_PERIOD):
    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i-1]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        ranges.append(true_range)

    if len(ranges) < period:
        return None

    return sum(ranges[-period:]) / period


# ==========================================
# ZONE OVERLAP
# ==========================================

def candle_overlaps_zone(candle, zone):
    return (
        candle["low"] <= zone["high"]
        and candle["high"] >= zone["low"]
    )


# ==========================================
# FVG DETECTION
# ==========================================

def find_valid_fvg(candles, direction):
    zones = []
    start = max(2, len(candles) - ZONE_LOOKBACK)

    for i in range(start, len(candles)):
        first = candles[i-2]
        middle = candles[i-1]
        third = candles[i]

        zone = None

        if direction == "BULLISH":
            if third["low"] > first["high"]:
                zone = {
                    "type": "BULLISH FVG",
                    "low": first["high"],
                    "high": third["low"],
                    "index": i
                }

        elif direction == "BEARISH":
            if third["high"] < first["low"]:
                zone = {
                    "type": "BEARISH FVG",
                    "low": third["high"],
                    "high": first["low"],
                    "index": i
                }

        if zone is None:
            continue

        # Ignore a gap already revisited by any earlier
        # closed candle after its formation.
        earlier_after_formation = candles[i+1:-1]

        previously_touched = any(
            candle_overlaps_zone(c, zone)
            for c in earlier_after_formation
        )

        if previously_touched:
            continue

        # The latest closed candle may be the retest candle.
        zone["retested_now"] = candle_overlaps_zone(
            candles[-1], zone
        )

        zones.append(zone)

    return zones[-1] if zones else None


# ==========================================
# ORDER BLOCK DETECTION
# ==========================================

def find_valid_order_block(candles, direction, structure):
    break_index = structure["break_index"]

    if break_index is None:
        return None

    if structure["break_type"] != direction:
        return None

    search_start = max(0, break_index - 8)

    for i in range(break_index - 1, search_start - 1, -1):
        candle = candles[i]

        if direction == "BULLISH":
            if candle["close"] >= candle["open"]:
                continue
        else:
            if candle["close"] <= candle["open"]:
                continue

        zone = {
            "type": direction + " ORDER BLOCK",
            "low": candle["low"],
            "high": candle["high"],
            "index": i
        }

        # Exclude zones touched before the latest candle.
        earlier_after_formation = candles[i+1:-1]

        previously_touched = any(
            candle_overlaps_zone(c, zone)
            for c in earlier_after_formation
        )

        if previously_touched:
            continue

        # Reject invalidation by a close beyond the zone.
        later_candles = candles[i+1:]

        if direction == "BULLISH":
            invalidated = any(
                c["close"] < zone["low"]
                for c in later_candles
            )
        else:
            invalidated = any(
                c["close"] > zone["high"]
                for c in later_candles
            )

        if invalidated:
            continue

        zone["retested_now"] = candle_overlaps_zone(
            candles[-1], zone
        )

        return zone

    return None


# ==========================================
# M15 RETEST AND REJECTION
# ==========================================

def check_retest(candles, zone, direction):
    if zone is None:
        return False, "No fresh FVG or Order Block"

    candle = candles[-1]

    if not candle_overlaps_zone(candle, zone):
        return False, "Price has not revisited the fresh zone"

    body = abs(candle["close"] - candle["open"])

    if body == 0:
        return False, "Confirmation candle has no body"

    midpoint = (zone["low"] + zone["high"]) / 2

    lower_wick = min(
        candle["open"], candle["close"]
    ) - candle["low"]

    upper_wick = candle["high"] - max(
        candle["open"], candle["close"]
    )

    if direction == "BULLISH":
        valid = (
            candle["close"] > candle["open"]
            and candle["close"] >= midpoint
            and lower_wick >= body * 0.5
        )

        if not valid:
            return False, "Bullish M15 rejection not confirmed"

    else:
        valid = (
            candle["close"] < candle["open"]
            and candle["close"] <= midpoint
            and upper_wick >= body * 0.5
        )

        if not valid:
            return False, "Bearish M15 rejection not confirmed"

    return True, "Fresh-zone retest and rejection confirmed"


# ==========================================
# ENTRY AND RISK
# ==========================================

def calculate_entry(candles, zone, direction):
    if zone is None:
        return None

    confirmed, reason = check_retest(
        candles, zone, direction
    )

    if not confirmed:
        return None

    atr = calculate_atr(candles)

    if atr is None or atr <= 0:
        return None

    last = candles[-1]
    entry = last["close"]
    buffer = atr * 0.2

    if direction == "BULLISH":
        stop_loss = zone["low"] - buffer
        risk = entry - stop_loss

        if risk <= 0:
            return None

        take_profit = entry + risk * RR_RATIO

    else:
        stop_loss = zone["high"] + buffer
        risk = stop_loss - entry

        if risk <= 0:
            return None

        take_profit = entry - risk * RR_RATIO

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_distance": risk,
        "rr": RR_RATIO
    }


# ==========================================
# SIGNAL ANALYSIS
# ==========================================

def analyse_pair(symbol):
    print(f"Analysing {symbol}", flush=True)

    h1_candles = get_candles(symbol, "1h")

    time.sleep(REQUEST_DELAY)

    m15_candles = get_candles(symbol, "15min")

    h1 = analyse_structure(h1_candles)
    m15 = analyse_structure(m15_candles)

    direction = m15["direction"]

    aligned = (
        direction in ("BULLISH", "BEARISH")
        and h1["direction"] == direction
    )

    structural_trigger = False

    if direction == "BULLISH":
        structural_trigger = (
            m15["bos"] == "BULLISH"
            or m15["choch"] == "BULLISH"
            or m15["sweep"] == "SELL-SIDE LIQUIDITY"
        )

    elif direction == "BEARISH":
        structural_trigger = (
            m15["bos"] == "BEARISH"
            or m15["choch"] == "BEARISH"
            or m15["sweep"] == "BUY-SIDE LIQUIDITY"
        )

    fvg = None
    order_block = None
    selected_zone = None
    entry = None

    rejection_reason = "Waiting for directional alignment"

    if not aligned:
        status = "NO VALID SETUP"
        action = "WAIT FOR H1 AND M15 ALIGNMENT"

    else:
        fvg = find_valid_fvg(m15_candles, direction)

        order_block = find_valid_order_block(
            m15_candles,
            direction,
            m15
        )

        candidates = [
            z for z in [fvg, order_block]
            if z is not None
        ]

        if candidates:
            selected_zone = max(
                candidates,
                key=lambda z: z["index"]
            )

        if not structural_trigger:
            status = "EARLY WARNING"
            action = "WAIT FOR RECENT STRUCTURAL CONFIRMATION"
            rejection_reason = "No recent aligned BOS, CHOCH or liquidity sweep"

        elif selected_zone is None:
            status = "STRUCTURAL SETUP - NO FRESH ZONE"
            action = "WAIT FOR A NEW VALID FVG OR ORDER BLOCK"
            rejection_reason = "No fresh, unmitigated zone found"

        else:
            confirmed, rejection_reason = check_retest(
                m15_candles,
                selected_zone,
                direction
            )

            if confirmed:
                entry = calculate_entry(
                    m15_candles,
                    selected_zone,
                    direction
                )

            if entry:
                status = "ENTRY CONDITIONS MET - ALERT ONLY"
                action = "REVIEW RISK BEFORE ANY TRADE"
            else:
                status = "STRUCTURAL SETUP - WAIT"
                action = "WAIT FOR M15 RETEST AND REJECTION"

    def fmt(value):
        return "NONE" if value is None else f"{value:.5f}"

    def zone_text(zone):
        if zone is None:
            return "NONE"

        state = (
            "RETESTING NOW"
            if zone["retested_now"]
            else "FRESH / UNTOUCHED"
        )

        return (
            f"{zone['type']} "
            f"{fmt(zone['low'])} - {fmt(zone['high'])} "
            f"({state})"
        )

    entry_text = "NOT CONFIRMED"

    if entry:
        entry_text = (
            f"ENTRY: {fmt(entry['entry'])}\n"
            f"STOP LOSS: {fmt(entry['stop_loss'])}\n"
            f"TAKE PROFIT: {fmt(entry['take_profit'])}\n"
            f"RISK DISTANCE: {fmt(entry['risk_distance'])}\n"
            f"RISK/REWARD: 1:{entry['rr']:.1f}"
        )

    return (
        f"INSTRUMENT: {symbol}\n"
        f"STATUS: {status}\n"
        f"DIRECTION: {direction}\n"
        f"ACTION: {action}\n\n"
        f"H1 DIRECTION: {h1['direction']}\n"
        f"M15 DIRECTION: {m15['direction']}\n"
        f"BOS: {m15['bos']}\n"
        f"CHOCH: {m15['choch']}\n"
        f"LIQUIDITY: {m15['sweep']}\n"
        f"RECENT BREAK: {m15['break_type']}\n\n"
        f"FVG: {zone_text(fvg)}\n"
        f"ORDER BLOCK: {zone_text(order_block)}\n"
        f"SELECTED ZONE: {zone_text(selected_zone)}\n"
        f"REJECTION REASON: {rejection_reason}\n\n"
        f"{entry_text}\n\n"
        f"SWING HIGH: {fmt(m15['swing_high'])}\n"
        f"SWING LOW: {fmt(m15['swing_low'])}\n"
        f"LAST CLOSED PRICE: {fmt(m15_candles[-1]['close'])}\n"
        f"LAST CLOSED CANDLE: {m15_candles[-1]['datetime']}"
    )


# ==========================================
# TELEGRAM DELIVERY
# ==========================================

def send_telegram(message):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing Telegram bot token")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram chat ID")

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    chunks = [
        message[i:i+4000]
        for i in range(0, len(message), 4000)
    ]

    for chunk in chunks:
        response = session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk
            },
            timeout=30
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")

    print("TELEGRAM DELIVERY SUCCESSFUL", flush=True)


# ==========================================
# MAIN
# ==========================================

def main():
    print("ODJ AI SMART TRADER V4 STARTED", flush=True)

    report = [
        "ODJ AI SMART TRADER V4",
        "PRECISION FILTER REPORT",
        "H1 ALIGNMENT + FRESH ZONES + M15 CONFIRMATION",
        "--------------------------------"
    ]

    for symbol in SYMBOLS:
        try:
            report.append(analyse_pair(symbol))
            print(f"Completed: {symbol}", flush=True)

        except Exception as error:
            report.append(
                f"INSTRUMENT: {symbol}\n"
                f"STATUS: ANALYSIS ERROR\n"
                f"ERROR: {str(error)}"
            )

            print(f"Error on {symbol}: {error}", flush=True)

        time.sleep(REQUEST_DELAY)

    final_report = (
        "\n\n".join(report)
        + "\n\nREPORT TIME: "
        + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        + "\n\nALERT ONLY - NO AUTOMATIC TRADING"
    )

    print(final_report, flush=True)
    send_telegram(final_report)

    print("ODJ AI SMART TRADER V4 FINISHED", flush=True)


if __name__ == "__main__":
    main()
