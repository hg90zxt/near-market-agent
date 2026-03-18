#!/usr/bin/env python3
"""Interactive launcher for NEAR headless bot."""

import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path.home() / ".near-market-bot"
CREDS_FILE = APP_DIR / "credentials.json"
BOT_DIR = Path(__file__).resolve().parent


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def mask(value):
    if not value:
        return "<not set>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}"


def print_header(title="NEAR Market Bot Launcher"):
    clear_screen()
    print("+----------------------------------------------------------+")
    print(f"| {title:<56} |")
    print("+----------------------------------------------------------+")


def load_credentials():
    if CREDS_FILE.exists():
        try:
            return json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_credentials(creds):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")


def ask_value(label, key, creds, secret=False):
    current = creds.get(key, "")
    shown = mask(current)
    prompt = f"{label} [{shown}] (Enter to keep): "
    raw = getpass.getpass(prompt) if secret else input(prompt)
    value = raw.strip()
    return value if value else current


def configure_credentials(creds):
    print_header("Configure API Keys")
    print("Enter values once and they will be saved locally.")
    print("File:", CREDS_FILE)
    print()

    updated = dict(creds)
    updated["NEAR_KEY"] = ask_value("NEAR API key", "NEAR_KEY", updated, secret=True)
    updated["CLAUDE_KEY"] = ask_value("Claude API key", "CLAUDE_KEY", updated, secret=True)
    updated["TG_BOT_TOKEN"] = ask_value("Telegram bot token", "TG_BOT_TOKEN", updated, secret=True)
    updated["TG_CHAT_ID"] = ask_value("Telegram user/chat id", "TG_CHAT_ID", updated, secret=False)

    save_credentials(updated)
    print("\nSaved.")
    input("Press Enter to continue...")
    return updated


def credentials_ok(creds):
    required = ["NEAR_KEY", "CLAUDE_KEY"]
    return all(bool(creds.get(k)) for k in required)


def build_env(creds):
    env = os.environ.copy()
    for k in ("NEAR_KEY", "CLAUDE_KEY", "TG_BOT_TOKEN", "TG_CHAT_ID"):
        if creds.get(k):
            env[k] = str(creds[k])
    return env


def run_single(script_name, creds):
    env = build_env(creds)
    script_path = BOT_DIR / script_name
    print_header(f"Running {script_name}")
    print("Stop with Ctrl+C")
    print("-" * 58)
    try:
        subprocess.run([sys.executable, str(script_path)], env=env, check=False)
    except KeyboardInterrupt:
        print("\nStopped.")
        time.sleep(1)


def run_both(creds):
    env = build_env(creds)
    auto_path = BOT_DIR / "autobot.py"
    mon_path = BOT_DIR / "monitor.py"

    print_header("Running AutoBot + Monitor")
    print("Both processes are running. Stop both with Ctrl+C")
    print("-" * 58)

    p1 = subprocess.Popen([sys.executable, str(auto_path)], env=env)
    p2 = subprocess.Popen([sys.executable, str(mon_path)], env=env)

    try:
        while True:
            if p1.poll() is not None or p2.poll() is not None:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (p1, p2):
            if p.poll() is None:
                p.terminate()
        for p in (p1, p2):
            try:
                p.wait(timeout=5)
            except Exception:
                pass

    print("\nProcesses stopped.")
    input("Press Enter to continue...")


def show_status(creds):
    print_header("Current Configuration")
    print(f"NEAR_KEY      : {mask(creds.get('NEAR_KEY', ''))}")
    print(f"CLAUDE_KEY    : {mask(creds.get('CLAUDE_KEY', ''))}")
    print(f"TG_BOT_TOKEN  : {mask(creds.get('TG_BOT_TOKEN', ''))}")
    print(f"TG_CHAT_ID    : {creds.get('TG_CHAT_ID', '<not set>')}")
    print(f"Storage file  : {CREDS_FILE}")
    print()
    input("Press Enter to continue...")


def main_menu():
    creds = load_credentials()

    while True:
        print_header()
        print("1) Configure / update keys")
        print("2) Run AutoBot")
        print("3) Run Monitor")
        print("4) Run AutoBot + Monitor")
        print("5) Show saved key status")
        print("0) Exit")
        print("-" * 58)

        if not credentials_ok(creds):
            print("Warning: some required keys are missing.")
        else:
            print("Keys are configured.")

        choice = input("Select action: ").strip()

        if choice == "1":
            creds = configure_credentials(creds)
        elif choice == "2":
            if not credentials_ok(creds):
                creds = configure_credentials(creds)
            run_single("autobot.py", creds)
        elif choice == "3":
            if not credentials_ok(creds):
                creds = configure_credentials(creds)
            run_single("monitor.py", creds)
        elif choice == "4":
            if not credentials_ok(creds):
                creds = configure_credentials(creds)
            run_both(creds)
        elif choice == "5":
            show_status(creds)
        elif choice == "0":
            break
        else:
            input("Unknown option. Press Enter to continue...")


if __name__ == "__main__":
    main_menu()
