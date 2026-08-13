# Snapchat Username Availability Checker

A self-contained, multi-threaded utility for checking Snapchat username
availability. It queries Snapchat's signup suggestion endpoint
(`accounts.snapchat.com/accounts/get_username_suggestions`), which reports an
`OK`, `TAKEN`, `DELETED`, or `INVALID_*` status for the requested name.

> **Use responsibly.** This is an unofficial endpoint that requires an
> `xsrf_token` (fetched from the signup page with a static fallback) and can
> change without notice. Respect Snapchat's Terms of Service. Availability is a
> point-in-time result and does not reserve a username — this is a checker, not
> an auto-claim tool.

## Requirements

- Python 3.8+
- `requests` and `colorama`

```bash
python -m pip install -r requirements.txt
```

## Quick start

Run it from the repo root:

```bash
python snapchat/snapchat_checker.py
```

Pick a pattern (or load a `.txt` file) and a thread count, and it prints results
as it goes, writing confirmed names to `available.txt` (also `taken.txt` /
`invalid.txt`).

You can skip the menu:

```bash
python snapchat/snapchat_checker.py --check someuser
python snapchat/snapchat_checker.py 100 LLLDD
```

## Pattern key

- `C` consonant, `V` vowel, `D` digit, `L` letter, `Q` alphanumeric, `_` underscore
- `[X]` literal, `[XY…]` custom table (picks one of the listed chars)

## Proxies & webhook

By default the checker rotates the shared `proxy.txt` at the repo root and
posts `snapchat "name" (available)` to the shared `webhook.txt` when it finds
an available name. Flip `USE_PROXIES` / `ENABLE_WEBHOOK` at the top of the
script to disable either.

## Notes

- Snapchat usernames are 3-15 characters, must start with a letter, and may
  contain letters, numbers, and the characters `-`, `_`, or `.`.
- The endpoint is rate sensitive. If you see many `unknown` results, the
  endpoint or `xsrf_token` may have changed — update the constants at the top of
  the script.
