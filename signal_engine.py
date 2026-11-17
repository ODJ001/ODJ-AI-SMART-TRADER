
import os
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = [
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD"
]

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials are not configured.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )
    response.raise_for_status()

def main():
    print("ODJ AI SMART TRADER started.")
    print("Markets configured:", ", ".join(SYMBOLS))

    message = (
        "ODJ AI SMART TRADER\n\n"
        "Signal engine deployment test.\n"
        "Market analysis is not active yet.\n"
        "Mode: DEMO"
    )

    send_telegram(message)

if __name__ == "__main__":
    main()
