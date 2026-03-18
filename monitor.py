#!/usr/bin/env python3
"""
NEAR Market Bid Monitor
Sends Telegram notifications when bid statuses change.
"""

import json
import os
import time

import requests

NEAR_API_KEY = os.environ.get("NEAR_KEY", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

NEAR_API = "https://market.near.ai/v1"
CHECK_INTERVAL = 60
STATUS_FILE = os.path.expanduser("~/near-market-bot/bids_status.json")


def load_known_statuses():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known_statuses(statuses):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(statuses, f, indent=2)


def get_bids():
    r = requests.get(
        f"{NEAR_API}/agents/me/bids",
        headers={"Authorization": f"Bearer {NEAR_API_KEY}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def send_telegram(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )


def main():
    print("NEAR Market Monitor started...")
    send_telegram("✅ NEAR Market Monitor started. Tracking bid status changes.")

    known = load_known_statuses()

    while True:
        try:
            bids = get_bids()
            for bid in bids:
                bid_id = bid["bid_id"]
                job_id = bid["job_id"]
                status = bid["status"]
                amount = bid.get("amount", "?")
                prev = known.get(bid_id)

                if prev and prev != status:
                    if status == "accepted":
                        send_telegram(
                            f"🎉 <b>BID ACCEPTED</b>\n\n"
                            f"💰 Amount: {amount} NEAR\n"
                            f"📋 Job ID: <code>{job_id}</code>\n"
                            f"🔑 Bid ID: <code>{bid_id}</code>\n\n"
                            f"Time to deliver."
                        )
                    elif status == "rejected":
                        send_telegram(
                            f"❌ Bid rejected\n"
                            f"Job: <code>{str(job_id)[:8]}</code> | {amount} NEAR"
                        )
                    elif status == "completed":
                        send_telegram(
                            f"✅ Bid completed\n"
                            f"Job: <code>{job_id}</code> | {amount} NEAR"
                        )

                known[bid_id] = status

            save_known_statuses(known)
        except Exception as e:
            print(f"Monitor error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
