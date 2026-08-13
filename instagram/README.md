# Instagram Username Availability Checker

A self-contained, multi-threaded utility for checking Instagram handle
availability. It queries Instagram's public web signup endpoint
(`web_create_ajax/attempt`) and reports whether each handle is available, taken,
or invalid.

> **Use responsibly.** This uses an unofficial endpoint that can change or rate
> limit without notice. Respect Instagram's Terms of Service. Availability is a
> point-in-time result and does not reserve or claim a username — this is a
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
python instagram/instagram_checker.py
```

Pick a pattern (or load a `.txt` file) and a thread count, and it prints results
as it goes, writing confirmed names to `available.txt` (also `taken.txt` /
`invalid.txt`).

You can skip the menu:

```bash
python instagram/instagram_checker.py --check myhandle
python instagram/instagram_checker.py 100 LLLDD
```

This generates 100 random handles matching `LLLDD` and checks them.

## Pattern key

- `C` consonant, `V` vowel, `D` digit, `L` letter, `Q` alphanumeric, `_` underscore
- `[X]` literal, `[XY…]` custom table (picks one of the listed chars)

## Proxies & webhook

By default the checker rotates the shared `proxy.txt` at the repo root and
posts `instagram "name" (available)` to the shared `webhook.txt` when it finds
an available name. Flip `USE_PROXIES` / `ENABLE_WEBHOOK` at the top of the
script to disable either.

## Notes

- Instagram is aggressive about blocking unauthenticated traffic; results may
  come back rate-limited (HTTP 429) from datacenter/VPN IPs.
- Handles are 1-30 chars using `a-z`, `0-9`, `.` and `_`; they cannot start or
  end with a period or contain consecutive periods.
