#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Top-level launcher menu for every checker in this repo.

Run it from anywhere - it resolves every tool by absolute path and launches it
in its own folder, so relative output files (e.g. Roblox's valid.txt) land in
the right place.
"""

import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from colorama import Fore, Style, init

from common import load_proxies, load_webhook_url

init(autoreset=True)

RAINBOW_COLORS = (
    "\033[95m",  # light magenta
    "\033[35m",  # magenta
    "\033[97m",  # bright white
    "\033[37m",  # white
)


def rainbow(text):
    return "".join(
        RAINBOW_COLORS[i % len(RAINBOW_COLORS)] + ch for i, ch in enumerate(text)
    ) + "\033[0m"


def rainbow_lines(text):
    return "\n".join(rainbow(line) for line in text.split("\n"))

BANNER = r"""
 _   _                                                     _ ____  ____
| | | |___  ___ _ __ ___  __ _ _ __   ___   ___ _ __   ___(_) ___||  _ \
| | | / __|/ _ \ '_ ` _ \/ _` | '_ \ / _ \ / __| '_ \ / _ \ |___ \| |_) |
| |_| \__ \  __/ | | | | | (_| | | | |  __/| (__| | | |  __/ |___) |  __/
 \___/|___/\___|_| |_| |_|\__,_|_| |_|\___| \___|_| |_|\___|_|____/|_|
"""

# (menu label, folder, script)
TOOLS = [
    ("Discord username checker", "discord", "discord_checker.py"),
    ("Roblox username checker", "Roblox", "roblox.py"),
    ("Instagram username checker", "instagram", "instagram_checker.py"),
    ("TikTok username checker", "tiktok", "tiktok_checker.py"),
    ("Snapchat username checker", "snapchat", "snapchat_checker.py"),
    ("Steam custom URL checker", "steam", "steam_checker.py"),
]

ITEM_COLORS = (
    Fore.MAGENTA,
    Fore.LIGHTMAGENTA_EX,
    Fore.WHITE,
    Fore.LIGHTWHITE_EX,
)


def spinner(seconds: float = 0.8, message: str = "Loading") -> None:
    """Show a short animated spinner (skipped when not a TTY)."""
    try:
        if not sys.stdout.isatty():
            return
    except (AttributeError, OSError):
        return
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        sys.stdout.write(
            f"\r  {Fore.LIGHTWHITE_EX}{message} {frames[i % len(frames)]}{Style.RESET_ALL}"
        )
        sys.stdout.flush()
        time.sleep(0.06)
        i += 1
    sys.stdout.write("\r" + " " * 70 + "\r")
    sys.stdout.flush()


def run_script(folder, script, label, extra_args=None):
    folder_abs = os.path.join(REPO_ROOT, folder)
    script_abs = os.path.join(folder_abs, script)
    cmd = [sys.executable, script_abs] + (extra_args or [])
    print()
    spinner(0.8, f"Launching {label}...")
    return subprocess.call(cmd, cwd=folder_abs)


def check_all():
    try:
        name = input("  Username to check on every platform: ").strip()
    except EOFError:
        return
    if not name:
        return
    print()
    spinner(0.8, "Checking across all platforms...")
    subprocess.call(
        [sys.executable, os.path.join(REPO_ROOT, "run_all.py"), name],
        cwd=REPO_ROOT,
    )


def check_all_file():
    try:
        path = input("  Path to a file of usernames (one per line): ").strip()
    except EOFError:
        return
    if not path:
        return
    print()
    spinner(0.8, "Checking file across all platforms...")
    subprocess.call(
        [sys.executable, os.path.join(REPO_ROOT, "run_all.py"), "--file", path],
        cwd=REPO_ROOT,
    )


def show_menu():
    print()
    print(f"{Style.BRIGHT}{rainbow_lines(BANNER)}{Style.RESET_ALL}")
    print(f"{rainbow('  Username Checker Suite - pick a tool')}")

    proxy_count = len(load_proxies())
    webhook_url = load_webhook_url()
    webhook_state = "on" if webhook_url else "off"
    webhook_color = Fore.GREEN if webhook_url else Fore.RED
    print(
        f"  {Fore.LIGHTWHITE_EX}proxies: {Style.BRIGHT}{proxy_count}{Style.RESET_ALL}"
        f"  {Fore.LIGHTWHITE_EX}/  webhook: {webhook_color}{webhook_state}{Style.RESET_ALL}"
    )

    print(f"{Fore.LIGHTBLACK_EX}{'-' * 58}{Style.RESET_ALL}")
    for i, (label, _, _) in enumerate(TOOLS):
        color = ITEM_COLORS[i % len(ITEM_COLORS)]
        print(f"  {color}[{i + 1}]{Style.RESET_ALL}  {label}")
    print(f"  {Fore.MAGENTA}[7]{Style.RESET_ALL}  Check a username across all platforms")
    print(f"  {Fore.MAGENTA}[8]{Style.RESET_ALL}  Check a file across all platforms")
    print(f"  {Fore.LIGHTBLACK_EX}[0]{Style.RESET_ALL}  Quit")
    print(f"{Fore.LIGHTBLACK_EX}{'-' * 58}{Style.RESET_ALL}")


def main():
    while True:
        show_menu()
        try:
            choice = input("  Select an option [0-8]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("", "0", "q", "Q"):
            break
        if choice in ("1", "2", "3", "4", "5", "6"):
            idx = int(choice) - 1
            label, folder, script = TOOLS[idx]
            print(f"{Fore.MAGENTA}\n  Launching: {label}{Style.RESET_ALL}")
            run_script(folder, script, label)
            continue
        if choice == "7":
            check_all()
            continue
        if choice == "8":
            check_all_file()
            continue
        print(f"{Fore.RED}  Invalid option - choose 0-8.{Style.RESET_ALL}")

    print(f"{Fore.MAGENTA}  Goodbye!{Style.RESET_ALL}")


if __name__ == "__main__":
    main()
