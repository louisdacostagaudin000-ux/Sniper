#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check one or more usernames across every platform at once.

Usage:
    python run_all.py someuser
    python run_all.py name1 name2 name3
    python run_all.py --file names.txt

Each name is checked once per platform via that checker's ``check_username()``
function. Results are printed as a compact table. This is a quick
multi-platform availability check only - it does not reserve or claim any
username.
"""

import argparse
import importlib.util
import os
import sys

from colorama import Fore, Style, init

init(autoreset=True)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

ADAPTERS = [
    ("instagram", "instagram_checker.py"),
    ("tiktok", "tiktok_checker.py"),
    ("snapchat", "snapchat_checker.py"),
    ("steam", "steam_checker.py"),
]

_LABELS = {0: "available", 1: "taken", 2: "invalid", None: "unknown"}
_COLORS = {
    0: Fore.GREEN,
    1: Fore.RED,
    2: Fore.YELLOW,
    None: Fore.LIGHTBLACK_EX,
}


def load_adapter(folder, script):
    path = os.path.join(REPO_ROOT, folder, script)
    spec = importlib.util.spec_from_file_location(f"{folder}_checker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_checkers():
    checkers = []
    for folder, script in ADAPTERS:
        mod = load_adapter(folder, script)
        checkers.append((folder, mod.check_username))
    return checkers


def read_names(path):
    names = []
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            name = line.strip()
            if name and not name.startswith("#"):
                names.append(name)
    return names


def render(status):
    label = _LABELS.get(status, "unknown")
    color = _COLORS.get(status, Fore.LIGHTBLACK_EX)
    return f"{color}{label:>14}{Style.RESET_ALL}"


def main():
    parser = argparse.ArgumentParser(
        description="Check usernames across all platforms at once"
    )
    parser.add_argument("names", nargs="*", help="username(s) to check")
    parser.add_argument("--file", help="file of usernames (one per line)")
    args = parser.parse_args()

    names = list(args.names)
    if args.file:
        names += read_names(args.file)
    if not names:
        parser.error("provide at least one username or --file FILE")

    checkers = build_checkers()
    header = f"  {'username':<20}" + "".join(f"{name:>14}" for name, _ in checkers)
    print(header)
    print("  " + "-" * (20 + 14 * len(checkers)))

    for name in names:
        row = f"  {name:<20}"
        for _platform, check in checkers:
            try:
                status = check(name)
            except Exception:
                status = None
            row += render(status)
        print(row)


if __name__ == "__main__":
    main()
