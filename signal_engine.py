
import os
import time
import requests
from datetime import datetime, timezone

# ==========================================
# ODJ AI SMART TRADER V3
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
ZONE_LOOKBACK = 40
OB_LOOKBACK = 30

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

    # Exclude the currently forming candle
    return candles[:-1]


# ==========================================
# SWING DETECTION
# ==========================================

def find_swings(candles, left=2, right=2):
    highs = []
    lows = []

    for i in range(left, len(candles) - right):
        current = candles[i]

        surrounding = (
            candles[i-left:i] +
            candles[i+1:i+right+1]
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
# MARKET STRUCTURE
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
        "break_index": None
    }

    if len(highs) < 2 or len(lows) < 2:
        return result

    latest = candles[-1]
    previous = candles[-2]

    swing_high = highs[-1]["price"]
    swing_low = lows[-1]["price"]

    result["swing_high"] = swing_high
    result["swing_low"] = swing_low

    higher_high = highs[-1]["price"] > highs[-2]["price"]
    higher_low = lows[-1]["price"] > lows[-2]["price"]

    lower_high = highs[-1]["price"] < highs[-2]["price"]
    lower_low = lows[-1]["price"] < lows[-2]["price"]

    previous_trend = "RANGING"

    if higher_high and higher_low:
        previous_trend = "BULLISH"
    elif lower_high and lower_low:
        previous_trend = "BEARISH"

    result["direction"] = previous_trend

    bullish_break = (
        previous["close"] <= swing_high
        and latest["close"] > swing_high
    )

    bearish_break = (
        previous["close"] >= swing_low
        and latest["close"] < swing_low
    )

    if bullish_break:
        result["direction"] = "BULLISH"
        result["break_index"] = len(candles) - 1

        if previous_trend == "BEARISH":
            result["choch"] = "BULLISH"
        else:
            result["bos"] = "BULLISH"

    elif bearish_break:
        result["direction"] = "BEARISH"
        result["break_index"] = len(candles) - 1

        if previous_trend == "BULLISH":
            result["choch"] = "BEARISH"
        else:
            result["bos"] = "BEARISH"

    if latest["low"] < swing_low and latest["close"] > swing_low:
        result["sweep"] = "SELL-SIDE LIQUIDITY"

    elif latest["high"] > swing_high and latest["close"] < swing_high:
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
# VALID FVG DETECTION
# ==========================================

def find_valid_fvg(candles, direction):
    zones = []
    start = max(2, len(candles) - ZONE_LOOKBACK)

    for i in range(start, len(candles)):
        first = candles[i-2]
        third = candles[i]

        zone = None

        if direction == "BULLISH" and third["low"] > first["high"]:
            zone = {
                "type": "BULLISH FVG",
                "low": first["high"],
                "high": third["low"],
                "index": i
            }

        elif direction == "BEARISH" and third["high"] < first["low"]:
            zone = {
                "type": "BEARISH FVG",
                "low": third["high"],
                "high": first["low"],
                "index": i
            }

        if zone is None:
            continue

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

        # Track whether price has already entered the zone.
        zone["mitigated"] = any(
            c["low"] <= zone["high"]
            and c["high"] >= zone["low"]
            for c in later_candles
        )

        zones.append(zone)

    return zones[-1] if zones else None


# ==========================================
# HISTORICAL ORDER BLOCK DETECTION
# ==========================================

def find_valid_order_block(candles, direction):
    start = max(20, len(candles) - OB_LOOKBACK)

    for break_index in range(len(candles) - 1, start - 1, -1):
        history = candles[:break_index]

        highs, lows = find_swings(history)

        if not highs or not lows:
            continue

        previous = candles[break_index - 1]
        current = candles[break_index]

        bullish_break = (
            previous["close"] <= highs[-1]["price"]
            and current["close"] > highs[-1]["price"]
        )

        bearish_break = (
            previous["close"] >= lows[-1]["price"]
            and current["close"] < lows[-1]["price"]
        )

        if direction == "BULLISH" and not bullish_break:
            continue

        if direction == "BEARISH" and not bearish_break:
            continue

        search_start = max(0, break_index - 8)

        for i in range(break_index - 1, search_start - 1, -1):
            candle = candles[i]

            if direction == "BULLISH" and candle["close"] < candle["open"]:
                zone = {
                    "type": "BULLISH ORDER BLOCK",
                    "low": candle["low"],
                    "high": candle["high"],
                    "index": i
                }

            elif direction == "BEARISH" and candle["close"] > candle["open"]:
                zone = {
                    "type": "BEARISH ORDER BLOCK",
                    "low": candle["low"],
                    "high": candle["high"],
                    "index": i
                }

            else:
                continue

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

            zone["mitigated"] = any(
                c["low"] <= zone["high"]
                and c["high"] >= zone["low"]
                for c in later_candles
            )

            return zone

    return None


# ==========================================
# M15 RETEST AND REJECTION
# ==========================================

def check_retest(candles, zone, direction):
    if zone is None:
        return False, "No valid zone found"

    candle = candles[-1]

    overlap = (
        candle["low"] <= zone["high"]
        and candle["high"] >= zone["low"]
    )

    if not overlap:
        return False, "Price has not revisited the zone"

    body = abs(candle["close"] - candle["open"])

    if body == 0:
        return False, "Confirmation candle has no body"

    midpoint = (zone["low"] + zone["high"]) / 2

    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    upper_wick = candle["high"] - max(candle["open"], candle["close"])

    if direction == "BULLISH":
        confirmed = (
            candle["close"] > candle["open"]
            and candle["close"] >= midpoint
            and lower_wick >= body * 0.5
        )

        if not confirmed:
            return False, "Bullish rejection candle not confirmed"

    else:
        confirmed = (
            candle["close"] < candle["open"]
            and candle["close"] <= midpoint
            and upper_wick >= body * 0.5
        )

        if not confirmed:
            return False, "Bearish rejection candle not confirmed"

    return True, "Directional M15 rejection confirmed"


# ==========================================
# ENTRY AND RISK MANAGEMENT
# ==========================================

def calculate_entry(candles, zone, direction):
    if zone is None:
        return None

    atr = calculate_atr(candles)

    if atr is None or atr <= 0:
        return None

    confirmed, reason = check_retest(candles, zone, direction)

    if not confirmed:
        return None

    entry = candles[-1]["close"]
    buffer = atr * 0.2

    if direction == "BULLISH":
        stop_loss = zone["low"] - buffer
        risk = entry - stop_loss

        if risk <= 0:
            return None

        take_profit = entry + (risk * RR_RATIO)

    else:
        stop_loss = zone["high"] + buffer
        risk = stop_loss - entry

        if risk <= 0:
            return None

        take_profit = entry - (risk * RR_RATIO)

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_distance": risk,
        "rr": RR_RATIO,
        "confirmation": reason
    }


# ==========================================
# SIGNAL CLASSIFICATION
# ==========================================

def classify_signal(h1, m15):
    direction = m15["direction"]

    if direction not in ("BULLISH", "BEARISH"):
        return "NO VALID SETUP"

    if h1["direction"] != direction:
        return "NO VALID SETUP"

    return "EARLY WARNING"


# ==========================================
# INSTRUMENT ANALYSIS
# ==========================================

def analyse_pair(symbol):
    h1_candles = get_candles(symbol, "1h")

    time.sleep(REQUEST_DELAY)

    m15_candles = get_candles(symbol, "15min")

    h1 = analyse_structure(h1_candles)
    m15 = analyse_structure(m15_candles)

    signal = classify_signal(h1, m15)
    direction = m15["direction"]

    fvg = None
    order_block = None
    selected_zone = None
    entry = None
    rejection_reason = "No aligned directional setup"

    if direction in ("BULLISH", "BEARISH"):
        fvg = find_valid_fvg(m15_candles, direction)

        order_block = find_valid_order_block(
            m15_candles,
            direction
        )

        candidates = [
            z for z in [fvg, order_block]
            if z is not None
        ]

        if candidates:
            # Prefer the most recently formed valid zone.
            selected_zone = max(
                candidates,
                key=lambda z: z["index"]
            )

        if h1["direction"] == direction:
            if selected_zone is None:
                rejection_reason = "No valid FVG or Order Block"

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
        action = "REVIEW RISK AND EXECUTION CONDITIONS"

    elif signal == "EARLY WARNING":
        status = "EARLY WARNING"
        action = "WAIT FOR VALID ZONE RETEST AND REJECTION"

    else:
        status = "NO VALID SETUP"
        action = "WAIT"

    def format_price(value):
        if value is None:
            return "NONE"
        return f"{value:.5f}"

    def zone_text(zone):
        if zone is None:
            return "NONE"

        mitigation = "MITIGATED" if zone["mitigated"] else "UNMITIGATED"

        return (
            f"{zone['type']} "
            f"{format_price(zone['low'])} - "
            f"{format_price(zone['high'])} "
            f"({mitigation})"
        )

    entry_text = "NOT CONFIRMED"

    if entry:
        entry_text = (
            f"ENTRY: {format_price(entry['entry'])}\n"
            f"STOP LOSS: {format_price(entry['stop_loss'])}\n"
            f"TAKE PROFIT: {format_price(entry['take_profit'])}\n"
            f"RISK DISTANCE: {format_price(entry['risk_distance'])}\n"
            f"RISK/REWARD: 1:{entry['rr']:.1f}\n"
            f"CONFIRMATION: {entry['confirmation']}"
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
        f"LIQUIDITY: {m15['sweep']}\n\n"
        f"FVG: {zone_text(fvg)}\n"
        f"ORDER BLOCK: {zone_text(order_block)}\n"
        f"SELECTED ZONE: {zone_text(selected_zone)}\n"
        f"REJECTION REASON: {rejection_reason}\n\n"
        f"{entry_text}\n\n"
        f"SWING HIGH: {format_price(m15['swing_high'])}\n"
        f"SWING LOW: {format_price(m15['swing_low'])}\n"
        f"LAST CLOSED PRICE: {format_price(m15_candles[-1]['close'])}\n"
        f"LAST CLOSED CANDLE: {m15_candles[-1]['datetime']}"
    )


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing Telegram bot token")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram chat ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

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
    print("ODJ AI SMART TRADER V3 STARTED", flush=True)

    report = [
        "ODJ AI SMART TRADER V3",
        "VALID FVG + ORDER BLOCK + ENTRY CONFIRMATION",
        "RISK MANAGEMENT REPORT",
        "--------------------------------"
    ]

    for symbol in SYMBOLS:
        try:
            print(f"Analysing {symbol}", flush=True)

            result = analyse_pair(symbol)
            report.append(result)

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

    print("ODJ AI SMART TRADER V3 FINISHED", flush=True)


if __name__ == "__main__":
    main()
