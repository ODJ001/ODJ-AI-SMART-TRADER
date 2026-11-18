
import os
import time
import requests
from datetime import datetime, timezone

# ==========================================
# ODJ AI SMART TRADER
# ==========================================

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.twelvedata.com"
SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

CANDLE_LIMIT = 100
REQUEST_DELAY = 9
RR_RATIO = 2.0

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

    if len(candles) < 25:
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

        left_candles = candles[i-left:i]
        right_candles = candles[i+1:i+right+1]

        if all(
            current["high"] > c["high"]
            for c in left_candles + right_candles
        ):
            highs.append({
                "index": i,
                "price": current["high"]
            })

        if all(
            current["low"] < c["low"]
            for c in left_candles + right_candles
        ):
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
        "direction": "UNCLEAR",
        "bos": "NONE",
        "choch": "NONE",
        "sweep": "NONE",
        "swing_high": None,
        "swing_low": None,
        "break_index": None
    }

    if len(highs) < 2 or len(lows) < 2:
        result["direction"] = "RANGING"
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

    # Liquidity sweeps
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
# FAIR VALUE GAP DETECTION
# ==========================================

def find_fvg(candles, direction):
    zones = []

    start = max(2, len(candles) - 30)

    for i in range(start, len(candles)):
        first = candles[i-2]
        third = candles[i]

        if direction == "BULLISH":
            if third["low"] > first["high"]:
                zones.append({
                    "type": "BULLISH FVG",
                    "low": first["high"],
                    "high": third["low"],
                    "index": i
                })

        elif direction == "BEARISH":
            if third["high"] < first["low"]:
                zones.append({
                    "type": "BEARISH FVG",
                    "low": third["high"],
                    "high": first["low"],
                    "index": i
                })

    return zones[-1] if zones else None


# ==========================================
# ORDER BLOCK DETECTION
# ==========================================

def find_order_block(candles, structure):
    direction = structure["direction"]
    break_index = structure["break_index"]

    if break_index is None:
        return None

    start = max(0, break_index - 8)

    for i in range(break_index - 1, start - 1, -1):
        candle = candles[i]

        if direction == "BULLISH" and candle["close"] < candle["open"]:
            return {
                "type": "BULLISH ORDER BLOCK",
                "low": candle["low"],
                "high": candle["high"],
                "index": i
            }

        if direction == "BEARISH" and candle["close"] > candle["open"]:
            return {
                "type": "BEARISH ORDER BLOCK",
                "low": candle["low"],
                "high": candle["high"],
                "index": i
            }

    return None


# ==========================================
# ATR CALCULATION
# ==========================================

def calculate_atr(candles, period=14):
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
# SIGNAL CLASSIFICATION
# ==========================================

def classify_signal(h1, m15):
    direction = m15["direction"]

    aligned = (
        direction in ("BULLISH", "BEARISH")
        and h1["direction"] == direction
    )

    if not aligned:
        return {
            "status": "NO VALID SETUP",
            "direction": direction
        }

    if direction == "BULLISH":
        trigger = (
            m15["bos"] == "BULLISH"
            or m15["choch"] == "BULLISH"
            or m15["sweep"] == "SELL-SIDE LIQUIDITY"
        )

    else:
        trigger = (
            m15["bos"] == "BEARISH"
            or m15["choch"] == "BEARISH"
            or m15["sweep"] == "BUY-SIDE LIQUIDITY"
        )

    if trigger:
        return {
            "status": "CONFIRMED STRUCTURAL SETUP",
            "direction": direction
        }

    return {
        "status": "EARLY WARNING",
        "direction": direction
    }


# ==========================================
# ENTRY AND RISK CALCULATION
# ==========================================

def calculate_entry(candles, direction, zone):
    if not zone:
        return None

    atr = calculate_atr(candles)

    if atr is None:
        return None

    last = candles[-1]

    # Price must revisit the candidate zone
    overlap = (
        last["low"] <= zone["high"]
        and last["high"] >= zone["low"]
    )

    if not overlap:
        return None

    entry = last["close"]

    # Require a directional candle closing within the zone
    if not (zone["low"] <= entry <= zone["high"]):
        return None

    if direction == "BULLISH":
        if last["close"] <= last["open"]:
            return None

        stop_loss = zone["low"] - (0.2 * atr)
        risk = entry - stop_loss

        if risk <= 0:
            return None

        take_profit = entry + (risk * RR_RATIO)

    else:
        if last["close"] >= last["open"]:
            return None

        stop_loss = zone["high"] + (0.2 * atr)
        risk = stop_loss - entry

        if risk <= 0:
            return None

        take_profit = entry - (risk * RR_RATIO)

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_distance": risk,
        "rr": RR_RATIO
    }


# ==========================================
# FULL INSTRUMENT ANALYSIS
# ==========================================

def analyse_pair(symbol):
    print(f"Analysing {symbol}", flush=True)

    h1_candles = get_candles(symbol, "1h")

    time.sleep(REQUEST_DELAY)

    m15_candles = get_candles(symbol, "15min")

    h1 = analyse_structure(h1_candles)
    m15 = analyse_structure(m15_candles)

    signal = classify_signal(h1, m15)

    direction = signal["direction"]

    fvg = None
    order_block = None
    selected_zone = None
    entry = None

    if direction in ("BULLISH", "BEARISH"):
        fvg = find_fvg(m15_candles, direction)
        order_block = find_order_block(m15_candles, m15)

        candidates = [
            zone for zone in [fvg, order_block]
            if zone is not None
        ]

        if candidates:
            selected_zone = max(
                candidates,
                key=lambda zone: zone["index"]
            )

        if signal["status"] == "CONFIRMED STRUCTURAL SETUP":
            entry = calculate_entry(
                m15_candles,
                direction,
                selected_zone
            )

    if entry:
        status = "ENTRY CONDITIONS MET - ALERT ONLY"
        action = "REVIEW SETUP AND RISK BEFORE TRADING"

    elif signal["status"] == "CONFIRMED STRUCTURAL SETUP":
        status = "CONFIRMED STRUCTURAL SETUP"
        action = "WAIT FOR FVG OR ORDER BLOCK RETEST"

    elif signal["status"] == "EARLY WARNING":
        status = "EARLY WARNING"
        action = "WATCH FOR STRUCTURAL CONFIRMATION"

    else:
        status = "NO VALID SETUP"
        action = "WAIT"

    zone_text = "NONE"

    if selected_zone:
        zone_text = (
            f"{selected_zone['type']}: "
            f"{selected_zone['low']:.5f} - "
            f"{selected_zone['high']:.5f}"
        )

    entry_text = "NOT CONFIRMED"

    if entry:
        entry_text = (
            f"ENTRY: {entry['entry']:.5f}\n"
            f"STOP LOSS: {entry['stop_loss']:.5f}\n"
            f"TAKE PROFIT: {entry['take_profit']:.5f}\n"
            f"RISK DISTANCE: {entry['risk_distance']:.5f}\n"
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
        f"LIQUIDITY: {m15['sweep']}\n\n"
        f"FVG: {fvg['type'] if fvg else 'NONE'}\n"
        f"ORDER BLOCK: "
        f"{order_block['type'] if order_block else 'NONE'}\n"
        f"SELECTED ZONE: {zone_text}\n\n"
        f"{entry_text}\n\n"
        f"SWING HIGH: {m15['swing_high']}\n"
        f"SWING LOW: {m15['swing_low']}\n"
        f"LAST CLOSED PRICE: {m15_candles[-1]['close']}\n"
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
# MAIN ENGINE
# ==========================================

def main():
    print("ODJ AI SMART TRADER STARTED", flush=True)

    report = [
        "ODJ AI SMART TRADER",
        "ADVANCED MARKET STRUCTURE REPORT",
        "FVG + ORDER BLOCK + RISK ANALYSIS",
        "--------------------------------"
    ]

    for symbol in SYMBOLS:
        try:
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
        + datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        + "\n\nALERT ONLY - NO AUTOMATIC TRADING"
    )

    print(final_report, flush=True)

    send_telegram(final_report)

    print("ODJ AI SMART TRADER FINISHED", flush=True)


if __name__ == "__main__":
    main()
