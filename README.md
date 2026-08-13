# Username Availability Checkers

A collection of Python tools that check username/handle availability across
several platforms.

> **Use responsibly.** These are *availability checkers*, not auto-claim bots —
> they report whether a name is free at a point in time and never reserve or
> claim it. Respect each platform's Terms of Service and rate limits. Only use
> proxies you own or are authorized to use.

## Platforms

| Folder | What it checks |
| --- | --- |
| `discord/` | Discord username availability |
| `Roblox/` | Roblox username validity (valid / taken / censored) |
| `instagram/` | Instagram handle availability |
| `tiktok/` | TikTok username availability |
| `snapchat/` | Snapchat username availability |
| `steam/` | Steam **custom URL** availability |

The `instagram/`, `tiktok/`, `snapchat/`, and `steam/` checkers are
self-contained, `roblox.py`-style scripts: they generate usernames from
patterns, check them with a threaded worker pool, and write results to
`available.txt` / `taken.txt` / `invalid.txt`. On top of that menu, they also
rotate the shared proxy list and post webhook alerts (see below).

## Requirements

- Python 3.8+
- `requests` and `colorama`

```bash
python -m pip install -r requirements.txt
```

## Quick start

Launch the interactive menu that gives access to every tool:

```bash
python menu.py
```

Check one name on every platform at once:

```bash
python run_all.py someusername
python run_all.py --file names.txt
```

Or run a single checker (each has an interactive menu):

```bash
python instagram/instagram_checker.py
python tiktok/tiktok_checker.py
python snapchat/snapchat_checker.py
python steam/steam_checker.py
python Roblox/roblox.py
python discord/discord_checker.py
```

The four simple checkers also accept `--check NAME` (single check) and
`COUNT PATTERN` (bulk) on the command line:

```bash
python tiktok/tiktok_checker.py --check someuser
python tiktok/tiktok_checker.py 100 LLLDD
```

## Shared proxies + webhook

Every checker reads the same two files at the repo root:

- **`proxy.txt`** — HTTP proxies, `user:pass@host:port`, one per line. Used for
  round-robin rotation across the worker threads.
- **`webhook.txt`** — a Discord webhook URL (one line). When a name is found
  available, each checker posts a message in the form
  `instagram "izqkds" (available)`.

Toggle them per checker by editing `USE_PROXIES` / `ENABLE_WEBHOOK` at the top
of each script (both default to `True`).
