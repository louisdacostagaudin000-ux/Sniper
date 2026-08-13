# TikTok Username Availability Checker

A self-contained, multi-threaded utility for checking TikTok username
availability. It fetches the public profile URL
`https://www.tiktok.com/@{username}` and treats a real profile (`"userInfo"`
present) as taken and a missing account (`"statusCode":10221` or a 404) as
available.

> **Use responsibly.** This is an unofficial, status-code-based method and can
> occasionally be inaccurate or rate-limited. Respect TikTok's Terms of Service.
> Availability is a point-in-time result and does not reserve a username — this
> is a checker, not an auto-claim tool.

## Requirements

- Python 3.8+
- `requests` and `colorama`

```bash
python -m pip install -r requirements.txt
```

## Quick start

Run it from the repo root:

```bash
python tiktok/tiktok_checker.py
```

Pick a pattern (or load a `.txt` file) and a thread count, and it prints results
as it goes, writing confirmed names to `available.txt` (also `taken.txt` /
`invalid.txt`).

You can skip the menu:

```bash
python tiktok/tiktok_checker.py --check someuser
python tiktok/tiktok_checker.py 100 LLLDD
```

## Pattern key

- `C` consonant, `V` vowel, `D` digit, `L` letter, `Q` alphanumeric, `_` underscore
- `[X]` literal, `[XY…]` custom table (picks one of the listed chars)

## Proxies & webhook

By default the checker rotates the shared `proxy.txt` at the repo root and
posts `tiktok "name" (available)` to the shared `webhook.txt` when it finds an
available name. Flip `USE_PROXIES` / `ENABLE_WEBHOOK` at the top of the script
to disable either.

## Notes

- TikTok usernames are 2-24 characters using letters, numbers, underscores and
  periods.
- Because this checks the profile page rather than a dedicated API, a name that
  returns `available` could still be blocked from registration for other reasons.
