# Steam Custom URL (Vanity) Availability Checker

A self-contained, multi-threaded utility for checking Steam **custom URL**
availability. It fetches `https://steamcommunity.com/id/<name>`: a `200` (or a
redirect to a profile) means the vanity URL is taken and a `404` means it is
available.

> **Important:** Steam does **not** publicly expose a check for the account
> *login name*. This tool checks the *custom URL* (also called the vanity URL /
> profile name) only.
>
> **Use responsibly.** Respect Steam's Terms of Service and rate limits.
> Availability is a point-in-time result and does not reserve a name — this is a
> checker, not an auto-claim tool.

## Requirements

- Python 3.8+
- `requests` and `colorama`

```bash
python -m pip install -r requirements.txt
```

## Quick start

Run it from the repo root:

```bash
python steam/steam_checker.py
```

Pick a pattern (or load a `.txt` file) and a thread count, and it prints results
as it goes, writing confirmed names to `available.txt` (also `taken.txt` /
`invalid.txt`).

You can skip the menu:

```bash
python steam/steam_checker.py --check myvanity
python steam/steam_checker.py 100 LLLDD
```

## Pattern key

- `C` consonant, `V` vowel, `D` digit, `L` letter, `Q` alphanumeric, `_` underscore
- `[X]` literal, `[XY…]` custom table (picks one of the listed chars)

## Proxies & webhook

By default the checker rotates the shared `proxy.txt` at the repo root and
posts `steam "name" (available)` to the shared `webhook.txt` when it finds an
available name. Flip `USE_PROXIES` / `ENABLE_WEBHOOK` at the top of the script
to disable either.

## Notes

- Custom URLs are case-insensitive, 3-32 characters, using letters, numbers and
  underscores.
