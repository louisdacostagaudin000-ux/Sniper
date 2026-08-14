# Instagram Username Availability Checker

A self-contained, multi-threaded utility for checking Instagram handle
availability. It queries Instagram's public profile lookup endpoint
(`i.instagram.com/api/v1/users/web_profile_info/`), which returns the profile
when a handle exists and a 404 otherwise, and reports whether each handle is
available, taken, or invalid.

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
python instagram/instagram_checker.py --no-proxy 100 LLLDD  # bulk run, skip proxies
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

- This uses the unauthenticated `web_profile_info` lookup instead of the signup
  `web_create_ajax/attempt` endpoint, which now rate-limits (HTTP 429) most
  unauthenticated clients. Results can still come back rate-limited from
  datacenter/VPN IPs.
- Handles are 1-30 chars using `a-z`, `0-9`, `.` and `_`; they cannot start or
  end with a period or contain consecutive periods. Clearly invalid handles are
  reported as `invalid` without hitting the API.
- If a check fails through a proxy, the checker retries that name directly, so
  dead or expired proxies won't turn every result into `unknown`.
