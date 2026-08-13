# Discord Username Availability Checker

A multi-threaded Python utility for checking Discord username availability.
It supports direct requests or optional rotating HTTP proxies, retries transient
failures, records confirmed available names, and can notify a Discord webhook.

> **Use responsibly.** Respect Discord's Terms of Service and rate limits. Only
> use proxy servers you own or are authorized to use. Availability is a
> point-in-time result and does not reserve or claim a username.

## Features

- Interactive menu and non-interactive CLI modes.
- Checks names from `list.txt` or generates random names using supported
  alphabets and lengths.
- Optional round-robin HTTP proxy rotation from the shared `proxy.txt` at the repo root.
- Proxy health tracking with cooldowns, failure streaks, and permanent bans.
- Per-proxy pacing through `per_proxy_max_rps` plus a global request throttle.
- Retry handling for network errors, blocked responses, server errors, and
  rate limits.
- Proxy testing with `--test-proxies`; dead and blocked proxies are removed
  only after the original file is backed up to `proxy_backup.txt`.
- Colorized output and a short terminal startup animation.
- Confirmed available names are appended to `available.txt` and optionally
  sent to a Discord webhook.
- Graceful `Ctrl+C` handling and a per-run summary with proxy health stats.

## Requirements

- Python 3.8 or newer
- Internet access to Discord
- Dependencies listed in `requirements.txt`

Install the dependencies from the project directory:

```bash
python -m pip install -r requirements.txt
```

## Quick start

1. Review `config.json` and replace the webhook placeholder with your own
   webhook URL, or set `enable_webhook` to `false`.
2. Put one username per line in `list.txt`.
3. Run the checker:

   ```bash
   python discord_checker.py
   ```

4. Choose a menu option. Confirmed available names are written to
   `available.txt`.

For a quick connectivity check before a larger run:

```bash
python discord_checker.py --selftest
```

The startup animation appears in an interactive terminal. It automatically
skips itself when output is redirected or captured, so scripts and tests do not
receive carriage-return animation output.

## Interactive menu

```text
[1]  Load usernames from list.txt
[2]  Random 4-character usernames  (a-z, 0-9, _)
[3]  Random 4-letter usernames     (a-z)
[4]  Random 3-character usernames  (a-z, 0-9, _)
[5]  Generate random usernames into list.txt (no checking)
[0]  Quit
```

Menu option **5** only generates names; it does not send requests to Discord.
It skips names already present in `list.txt`.

## Command-line usage

Check one name and exit:

```bash
python discord_checker.py --check example_name
```

Run a random mode without opening the menu:

```bash
python discord_checker.py --run 2 --count 100
python discord_checker.py --run 3 --count 100 --threads 16
python discord_checker.py --run 4 --count 100 --quiet
```

Generate names into `list.txt` without checking them:

```bash
python discord_checker.py --genlist 200
python discord_checker.py --genlist 50 --length 6 --letters
python discord_checker.py --genlist 100 --length 3
```

Other options:

| Option | Purpose |
| --- | --- |
| `--check USERNAME` | Check one username and exit. |
| `--run 1-4` | Run a menu mode non-interactively and exit. |
| `--count N` | Set the random-name count for the current run. |
| `--threads N` | Override the configured worker count. |
| `--quiet` | Hide routine taken/invalid/retry output; keep available names, errors, and stats. |
| `--config PATH` | Load a different JSON configuration file. |
| `--selftest` | Send one direct request and print the raw result. |
| `--test-proxies` | Test every proxy and keep only healthy, non-blocked entries. |
| `--no-auto-test` | Skip `auto_test_proxies` for this invocation. |
| `--genlist N` | Append `N` unique generated names to `list.txt`. |
| `--length L` | Set the `--genlist` username length, from 2 to 32. |
| `--letters` | Make `--genlist` use letters only. |

## Optional proxy rotation

Proxy use is controlled by `use_proxies` in `config.json`. The built-in default
is `false`; the sample `config.json` in this repository may enable it, so check
your local file before running.

Set this in `config.json`:

```json
{
  "use_proxies": true,
  "auto_test_proxies": true
}
```

Add one proxy per line to the shared `proxy.txt` at the repo root. Both authenticated and unauthenticated
HTTP proxies are accepted:

```text
user:password@host:port
host:port
http://user:password@host:port
```

Blank lines and lines beginning with `#` are ignored. Duplicate `host:port`
entries are ignored. When `auto_test_proxies` is enabled, the checker tests and
prunes dead or blocked proxies before a normal checking run. Use
`--no-auto-test` to bypass that startup step.

## Webhook notifications

Only names classified as **available** are saved and queued for webhook
notification. Set a real webhook URL in `config.json`:

```json
{
  "enable_webhook": true,
  "webhook_url": "https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN"
}
```

Set `enable_webhook` to `false` to disable notifications. Never publish webhook
URLs, proxy passwords, or other credentials in source control. If credentials
have been exposed, rotate them before using the project again.

## Configuration

The checker merges `config.json` over its built-in defaults. The main options
are listed below; values in your local `config.json` take precedence.

| Key | Default | Description |
| --- | ---: | --- |
| `webhook_url` | placeholder | Webhook receiving available names. |
| `enable_webhook` | `true` | Enable webhook notifications. |
| `use_proxies` | `false` | Load and rotate proxies from the shared root `proxy.txt`. |
| `concurrency` | `32` | Number of worker threads. |
| `timeout_seconds` | `10` | HTTP read timeout in seconds. |
| `request_interval_seconds` | `0.01` | Minimum gap between all requests. |
| `per_proxy_max_rps` | `2.0` | Maximum request rate for one proxy IP. |
| `max_retries_per_username` | `3` | Attempts made for transient failures. |
| `retry_backoff_base` | `1.5` | Exponential retry-backoff base. |
| `retry_backoff_max` | `10.0` | Maximum retry-backoff delay. |
| `proxy_failure_threshold` | `3` | Consecutive failures before cooldown. |
| `proxy_cooldown_seconds` | `30` | Failure cooldown duration. |
| `proxy_ban_after_failures` | `10` | Consecutive failures before permanent ban. |
| `proxy_wait_timeout_seconds` | `10` | Wait time for a usable proxy. |
| `proxy_summary_limit` | `0` | Proxies shown in the summary; `0` means all. |
| `auto_test_proxies` | `false` | Test proxies automatically before checking. |
| `max_proxy_cooldown_seconds` | `600` | Maximum cooldown after a proxy 429. |
| `max_global_pause_seconds` | `60` | Maximum pause after a global 429. |
| `proxy_recovery_timeout_seconds` | `1800` | Maximum time a name waits for proxy recovery. |
| `default_random_count` | `100` | Default number of generated names. |
| `username_min_length` | `2` | Local minimum username length. |
| `username_max_length` | `32` | Local maximum username length. |
| `verbose_results` | `true` | Print routine result lines. |
| `verbose_rate_limits` | `false` | Print each non-global 429 line. |
| `verbose_proxy_waiting` | `false` | Print when all proxies are cooling. |
| `available_file` | `available.txt` | File where available names are appended. |
| `webhook_retries` | `3` | Webhook delivery attempts. |
| `webhook_timeout_seconds` | `10` | Webhook request timeout. |

## How checking works

1. Names are normalized to lowercase and checked locally for the configured
   length range.
2. The checker sends a POST request containing `{"username": "..."}` to
   Discord's unauthenticated username-attempt endpoint.
3. Responses are classified as follows:
   - `200` with `{"taken": false}` → **available**
   - `200` with `{"taken": true}` → **taken**
   - `400` → invalid username
   - `429` → rate limited; the `Retry-After` value is honored within configured caps
   - `401` or `403` → blocked; the proxy is treated as unhealthy
   - network errors and `5xx` responses → retried
4. Workers rotate proxies between attempts. A normal 429 cools only the proxy
   that received it; a response marked `X-RateLimit-Global: true` pauses the
   fleet for the configured capped duration.
5. Available names are appended to `available.txt` and sent to the webhook
   queue. Taken names are never saved or posted.

## Testing

The test suite uses Python's built-in `unittest` framework. Network requests are
mocked, so it does not contact Discord:

```bash
python -m unittest test_discord_checker -v
```

The tests cover proxy health and rotation, rate limiting, retry logging,
quiet-mode output, summaries, webhook delivery, menu and CLI flows, list
creation, self-tests, and proxy-file rewriting.

## Troubleshooting

### The checker appears stuck

Run the self-test first:

```bash
python discord_checker.py --selftest
```

During a normal run, a progress heartbeat is printed periodically when verbose
output is enabled. Slow or blocked proxies can also take time to cool down;
use `--test-proxies` to remove dead and blocked entries.

### Many requests are rate limited

Reduce `concurrency`, increase `request_interval_seconds`, and keep
`per_proxy_max_rps` conservative. A larger proxy list does not guarantee that
Discord will allow unlimited checking.

### Webhook messages do not arrive

Confirm that `enable_webhook` is true and that `webhook_url` is a valid current
Discord webhook. The program logs webhook failures and retries them according
to `webhook_retries`.

### Every proxy fails

Run:

```bash
python discord_checker.py --test-proxies
```

The command keeps the original `proxy.txt` if no healthy proxies are found. If
some are removed, the previous file is saved as `proxy_backup.txt`.

## Project files

| File | Purpose |
| --- | --- |
| `discord_checker.py` | Main checker and CLI. |
| `config.json` | Runtime configuration. |
| `requirements.txt` | Python dependencies. |
| `list.txt` | Input usernames and generated names. |
| `proxy.txt` (repo root) | Shared HTTP proxy list for all checkers. |
| `available.txt` | Confirmed available output. |
| `test_discord_checker.py` | Offline unit and integration tests. |
