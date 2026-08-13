#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TikTok username availability checker (roblox.py-style).

Self-contained: generate usernames from patterns, check them against TikTok's
public profile page (``https://www.tiktok.com/@<user>``) with a threaded worker
pool, and write results to available.txt / taken.txt / invalid.txt.

Usage:
    python tiktok_checker.py            # interactive menu
    python tiktok_checker.py 100 LLLDD  # 100 random names, pattern LLLDD
    python tiktok_checker.py --check name # check one handle
"""

import itertools
import os
import random
import string
import sys
import threading
from queue import Queue

import requests
from colorama import Fore, Style, init

init(autoreset=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common import ProxyPool, WebhookNotifier, proxies_dict, load_proxies

USE_PROXIES = True
ENABLE_WEBHOOK = True
PROXY_POOL = ProxyPool(load_proxies())
NOTIFIER = WebhookNotifier("tiktok")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
PROFILE_URL = "https://www.tiktok.com/@{username}"
HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": UA,
}

BANNER = r"""
 _____ _ _  ___  _  __
|_   _(_) |/ _ \| |/ /
  | | | | | (_) | ' /
  | | | | |\__, | . \
  |_| |_|_|  /_/|_|\_\
"""

CHARSETS = {
    "C": "bcdfghjklmnpqrstvwxyz",
    "V": "aeiou",
    "D": "0123456789",
    "L": "abcdefghijklmnopqrstuvwxyz",
    "Q": "abcdefghijklmnopqrstuvwxyz0123456789",
    "_": "_",
}


def parse_pattern(fmt):
    tokens = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "[":
            end = fmt.index("]", i)
            inner = fmt[i + 1 : end]
            if len(inner) == 1:
                tokens.append(("lit", inner))
            else:
                tokens.append(("custom", inner))
            i = end + 1
        else:
            ch = fmt[i].upper()
            if ch in CHARSETS:
                tokens.append(("key", ch))
            else:
                tokens.append(("lit", fmt[i]))
            i += 1
    return tokens


def resolve_token(token):
    kind, val = token
    if kind == "key":
        return random.choice(CHARSETS[val])
    if kind == "custom":
        return random.choice(val)
    return val


def gen_from_pattern(fmt):
    return "".join(resolve_token(t) for t in parse_pattern(fmt))


def make_examples(fmt, n=2):
    return ", ".join(gen_from_pattern(fmt) for _ in range(n))


def all_combinations(fmt):
    tokens = parse_pattern(fmt)
    pools = []
    for kind, val in tokens:
        if kind == "key":
            pools.append(list(CHARSETS[val]))
        elif kind == "custom":
            pools.append(list(val))
        else:
            pools.append([val])
    for combo in itertools.product(*pools):
        yield "".join(combo)


def combo_count(fmt):
    tokens = parse_pattern(fmt)
    total = 1
    for kind, val in tokens:
        if kind == "key":
            total *= len(CHARSETS[val])
        elif kind == "custom":
            total *= len(val)
    return total


BUILTIN = [
    ("CVCVC", "CVCVC", "Consonant-vowel pattern"),
    ("LL_LL", "LL_LL", "Letters underscore letters"),
    ("LLLDD", "LLLDD", "3 letters + 2 digits"),
    ("DDLLL", "DDLLL", "2 digits + 3 letters"),
    ("LLDLL", "LLDLL", "Letters-digit-letters"),
    ("QQQQQ", "QQQQQ", "5 alphanumeric chars"),
    ("CVDCV", "CVDCV", "Vowel-consonant with digit"),
]

print_lock = threading.Lock()
file_lock = threading.Lock()

STATUS_FILES = {0: "available.txt", 1: "taken.txt", 2: "invalid.txt"}


def save_and_print(name, code):
    with print_lock:
        if code == 0:
            print(Fore.GREEN + f"  [AVAILABLE] {name}")
        elif code == 1:
            print(Fore.WHITE + f"  [TAKEN]     {name}")
        elif code == 2:
            print(Fore.RED + f"  [INVALID]   {name}")
        else:
            print(f"  [UNKNOWN]   {name}")

    with file_lock:
        dest = STATUS_FILES.get(code)
        if dest:
            with open(os.path.join(SCRIPT_DIR, dest), "a") as f:
                f.write(name + "\n")

    if code == 0 and ENABLE_WEBHOOK:
        NOTIFIER.notify(name, "available")


def clear_output_files():
    for name in STATUS_FILES.values():
        open(os.path.join(SCRIPT_DIR, name), "a").close()


def check_username(username, proxy=None):
    """Return 0 = available, 1 = taken, 2 = invalid, None = unknown/error."""
    url = PROFILE_URL.format(username=username)
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, proxies=proxies_dict(proxy))
    except Exception as exc:
        with print_lock:
            print(f"  error checking {username}: {exc}")
        return None

    status = r.status_code
    if status == 200:
        low = r.text.lower()
        # TikTok embeds `"statusCode":10221` when the account does not exist,
        # and a `"userInfo"` block when it does.
        if '"statuscode":10221' in low or "user page not found" in low:
            return 0
        if '"userinfo"' in low:
            return 1
        # Ambiguous page (e.g. bot challenge) - leave as unknown.
        return None
    if status == 404:
        return 0
    return None


def worker(q):
    while True:
        name = q.get()
        if name is None:
            break
        proxy = PROXY_POOL.next() if USE_PROXIES else None
        save_and_print(name, check_username(name, proxy))
        q.task_done()


def run_with_threads(usernames, num_threads):
    q = Queue(maxsize=num_threads * 4)
    threads = []
    for _ in range(num_threads):
        t = threading.Thread(target=worker, args=(q,), daemon=True)
        t.start()
        threads.append(t)

    for name in usernames:
        q.put(name)

    q.join()

    for _ in threads:
        q.put(None)
    for t in threads:
        t.join()


def ask_generation_mode(fmt):
    print("\nGeneration mode:")
    print("  1. Random sample (pick N names)")
    print("  2. All combinations (exhaustive)\n")
    mode = input("Mode: ").strip()

    if mode == "2":
        total = combo_count(fmt)
        print(f"\n  This pattern has {total:,} possible combinations.")
        confirm = input("  Proceed? (y/n): ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            sys.exit(0)
        usernames = list(all_combinations(fmt))
    else:
        count = int(input("\nHow many names? "))
        usernames = [gen_from_pattern(fmt) for _ in range(count)]

    return apply_ordering(usernames)


def apply_ordering(usernames):
    print("\nOrdering:")
    print("  1. Totally random")
    print("  2. Random, no duplicates")
    print("  3. Alphabetical order\n")
    order = input("Order: ").strip()

    if order == "2":
        usernames = list(dict.fromkeys(usernames))
        random.shuffle(usernames)
    elif order == "3":
        usernames = sorted(set(usernames))
    else:
        random.shuffle(usernames)

    return usernames


def ask_threads():
    print("\nThreads (more = faster, but may get rate-limited):")
    try:
        n = int(input("  How many threads? [1-50]: ").strip())
        return max(1, min(n, 50))
    except ValueError:
        return 1


def show_menu():
    print(BANNER)
    print("TikTok Username Checker\n")
    print("Pattern key:")
    print("  C = consonant  V = vowel  D = digit  L = letter  Q = alphanumeric")
    print("  _ = underscore")
    print("  [X]   = literal  (e.g. [M]  always outputs M)")
    print("  [XY...] = custom table - picks one of the listed chars  (e.g. [PQ] outputs P or Q)\n")
    print("Built-in patterns:\n")
    for i, (label, fmt, desc) in enumerate(BUILTIN, 1):
        print(f"  {i}. {label:<10} {desc:<30} e.g. {make_examples(fmt)}")
    print()
    print("  c. Custom pattern  (e.g. [M][S][PQ]DD)")
    print("  f. Load from .txt file")
    print("  q. Quit\n")


def run():
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        username = sys.argv[2].strip().lstrip("@")
        proxy = PROXY_POOL.next() if USE_PROXIES else None
        save_and_print(username, check_username(username, proxy))
        return

    if len(sys.argv) == 3:
        try:
            fmt = sys.argv[2]
            count = int(sys.argv[1])
            usernames = apply_ordering([gen_from_pattern(fmt) for _ in range(count)])
            threads = ask_threads()
            clear_output_files()
            print(f"\nChecking {len(usernames)} usernames with {threads} thread(s)...\n")
            run_with_threads(usernames, threads)
            print("\nSaved to available.txt / taken.txt / invalid.txt")
            return
        except Exception as exc:
            print(f"Bad args: {exc}")
            sys.exit(1)

    show_menu()
    choice = input("Choice: ").strip().lower()

    usernames = []

    if choice == "q":
        sys.exit(0)

    elif choice == "f":
        path = input("File path: ").strip()
        if not os.path.exists(path):
            print("File not found.")
            sys.exit(1)
        with open(path) as f:
            usernames = [line.strip() for line in f if line.strip()]
        usernames = apply_ordering(usernames)

    elif choice == "c":
        fmt = input("\nPattern (e.g. [M][S][PQ]DD): ").strip()
        usernames = ask_generation_mode(fmt)

    elif choice.isdigit() and 1 <= int(choice) <= len(BUILTIN):
        _, fmt, _ = BUILTIN[int(choice) - 1]
        usernames = ask_generation_mode(fmt)

    else:
        print("Invalid choice.")
        sys.exit(1)

    threads = ask_threads()
    clear_output_files()
    print(f"\nChecking {len(usernames)} usernames with {threads} thread(s)...\n")
    run_with_threads(usernames, threads)
    print("\nDone. Results in available.txt / taken.txt / invalid.txt")


if __name__ == "__main__":
    run()
