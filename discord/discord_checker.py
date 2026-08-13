#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discord username availability checker.

The checker supports direct requests, optional rotating HTTP proxies, retry and
rate-limit handling, webhook notifications, an interactive menu, and an
offline-friendly CLI.  Available names are only saved after Discord confirms
that they are available.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import shutil
import string
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Optional

import requests
from colorama import Fore, Style, init

init(autoreset=True)

# ---------------------------------------------------------------------------
# Paths, API constants, and defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
# Shared proxy list at the repo root so every checker uses the same proxies.
PROXY_FILE = os.path.join(REPO_ROOT, "proxy.txt")
SHARED_WEBHOOK_FILE = os.path.join(REPO_ROOT, "webhook.txt")


def resolve_webhook_url(config: dict) -> str:
    """Return the shared webhook URL so every tool posts to the same webhook.

    A non-placeholder ``webhook_url`` in the tool's config wins; otherwise the
    shared ``webhook.txt`` at the repo root is used.
    """
    url = config.get("webhook_url", "")
    if url and "YOUR_ID" not in url and "REPLACE" not in url:
        return url
    if os.path.exists(SHARED_WEBHOOK_FILE):
        try:
            with open(SHARED_WEBHOOK_FILE, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except OSError:
            pass
    return url

API_BASE = "https://discord.com/api/v9"
USERNAME_ATTEMPT_URL = API_BASE + "/unique-username/username-attempt-unauthed"

X_SUPER_PROPERTIES = (
    "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6"
    "ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2lu"
    "NjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEwNy4wLjAu"
    "MCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTA3LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEw"
    "IiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJl"
    "ZmVycmVfc2VhcmNoX3Rlcm0iOiIiLCJsb2NhbGVfdGltZXpvbmUiOiJFdGMvVVRDIiwiYnVpbGRfaW5mbyI6"
    "W3siaWQiOiIxMDc3MDQiLCJwYXRoIjoiXC8iLCJuYW1lIjoiU3RhYmxlIn1dLCJjbGllbnRfYnVpbGRfbnVt"
    "YmVyIjoxNTQ3NTAsImNsaWVudF9ldmVudF9zb3VyY2UiOm51bGx9"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ALPHABET_ALL = string.ascii_lowercase + string.digits + "_"
ALPHABET_LETTERS = string.ascii_lowercase
MAX_GENERATED_NAMES = 10_000_000

ST_AVAILABLE = "available"
ST_TAKEN = "taken"
ST_INVALID = "invalid"
ST_RATE_LIMITED = "rate_limited"
ST_BLOCKED = "blocked"
ST_PROXY_ERROR = "proxy_error"
ST_NETWORK = "network_error"
ST_UNKNOWN = "unknown"

DEFAULT_CONFIG = {
    "webhook_url": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
    "enable_webhook": True,
    "webhook_platform": "discord",
    "webhook_footer": "Discord Username Checker",
    "use_proxies": False,
    "concurrency": 32,
    "timeout_seconds": 10,
    "request_interval_seconds": 0.01,
    "per_proxy_max_rps": 2.0,
    "max_retries_per_username": 3,
    "retry_backoff_base": 1.5,
    "retry_backoff_max": 10.0,
    "proxy_failure_threshold": 3,
    "proxy_cooldown_seconds": 30,
    "proxy_ban_after_failures": 10,
    "proxy_wait_timeout_seconds": 10,
    "proxy_summary_limit": 0,
    "hide_proxies": True,
    "auto_test_proxies": False,
    "max_proxy_cooldown_seconds": 600,
    "max_global_pause_seconds": 60,
    "proxy_recovery_timeout_seconds": 1800,
    "default_random_count": 100,
    "username_min_length": 2,
    "username_max_length": 32,
    "verbose_results": True,
    "verbose_rate_limits": False,
    "verbose_proxy_waiting": False,
    "available_file": "available.txt",
    "webhook_retries": 3,
    "webhook_timeout_seconds": 10,
}


# ---------------------------------------------------------------------------
# Thread-safe colored logging
# ---------------------------------------------------------------------------

_PRINT_LOCK = threading.Lock()


def _emit(text: str) -> None:
    """Print one complete line without interleaving worker output."""
    with _PRINT_LOCK:
        print(text, flush=True)


def log_info(message: str) -> None:
    _emit(f"{Fore.CYAN}[i] {message}{Style.RESET_ALL}")


def log_ok(message: str) -> None:
    _emit(f"{Fore.GREEN}[+] {message}{Style.RESET_ALL}")


def log_warn(message: str) -> None:
    _emit(f"{Fore.YELLOW}[!] {message}{Style.RESET_ALL}")


def log_error(message: str) -> None:
    _emit(f"{Fore.RED}[x] {message}{Style.RESET_ALL}")


def log_success(username: str) -> None:
    _emit(f"{Fore.GREEN}{Style.BRIGHT}AVAILABLE  {username}{Style.RESET_ALL}")


def log_taken(username: str) -> None:
    _emit(f"{Fore.RED}TAKEN      {username}{Style.RESET_ALL}")


def log_dim(message: str) -> None:
    _emit(f"{Fore.LIGHTBLACK_EX}{message}{Style.RESET_ALL}")


def log_rate_limit(message: str) -> None:
    _emit(f"{Fore.MAGENTA}[429] {message}{Style.RESET_ALL}")


def trim(value: Any, limit: int = 140) -> str:
    """Convert a value to a one-line, bounded display string."""
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(path: str = CONFIG_PATH) -> dict:
    """Load JSON configuration over defaults without crashing on bad input."""
    config = dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        log_warn(f"config.json not found at {path} - using built-in defaults.")
        return config

    try:
        with open(path, encoding="utf-8") as handle:
            user_config = json.load(handle)
        if not isinstance(user_config, dict):
            raise ValueError("config.json must contain a JSON object")
        config.update(user_config)
        log_ok(f"Loaded config from {path}")
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log_error(f"Could not parse config.json ({exc}) - using built-in defaults.")
    return config


# ---------------------------------------------------------------------------
# Proxy management
# ---------------------------------------------------------------------------

PROXY_RE = re.compile(
    r"^(?:(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@)?"
    r"(?P<host>[^:\s@]+):(?P<port>\d{1,5})$"
)


class Proxy:
    """A proxy entry plus its mutable health and run-stat state."""

    __slots__ = (
        "host", "port", "user", "password", "url", "label", "seq",
        "consecutive_failures", "cooldown_until", "banned", "last_request_at",
        "uses", "rate_limits", "cooled", "bans",
    )

    def __init__(self, user: str, password: str, host: str, port: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        auth = f"{user}:{password}@" if user else ""
        self.url = f"http://{auth}{host}:{port}"
        self.label = f"{user}@{host}:{port}" if user else f"{host}:{port}"

        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.banned = False
        self.last_request_at = 0.0

        self.uses = 0
        self.rate_limits = 0
        self.cooled = 0
        self.bans = 0
        self.seq = 0

    def is_usable(self, now: float) -> bool:
        return not self.banned and self.cooldown_until <= now


class ProxyManager:
    """Thread-safe round-robin proxy pool with health tracking."""

    def __init__(self, config: dict):
        self._config = config
        self._proxies: list[Proxy] = []
        self._index = -1
        self._lock = threading.Lock()

    def load(self, path: str = PROXY_FILE, force: bool = False) -> int:
        """Parse and append unique proxies from *path*.

        Normal runs only load proxies when ``use_proxies`` is enabled. The
        proxy tester passes ``force=True`` so it can inspect the file directly.
        """
        if not force and not self._config.get("use_proxies", False):
            log_info("Proxy rotation disabled (use_proxies=false) - checking directly.")
            return 0
        if not os.path.exists(path):
            log_warn(f"proxy.txt not found at {path} - checking directly.")
            return 0

        loaded = 0
        duplicates = 0
        with self._lock:
            seen = {(proxy.host, proxy.port) for proxy in self._proxies}

        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                candidate = line
                if candidate.startswith(("http://", "https://")):
                    candidate = candidate.split("://", 1)[1]
                match = PROXY_RE.match(candidate)
                if not match:
                    log_warn(
                        f"proxy.txt:{line_number} - invalid proxy line skipped"
                    )
                    continue

                key = (match.group("host"), match.group("port"))
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                self._proxies.append(
                    Proxy(
                        user=match.group("user") or "",
                        password=match.group("password") or "",
                        host=match.group("host"),
                        port=match.group("port"),
                    )
                )
                self._proxies[-1].seq = loaded + 1
                loaded += 1

        if duplicates:
            log_dim(f"Skipped {duplicates} duplicate proxy line(s).")
        log_ok(f"Loaded {loaded} unique proxy/proxies from {path}")
        if loaded == 0:
            log_warn(
                f"No valid proxies found in {path} - checking directly! "
                "Add proxies to proxy.txt (user:pass@host:port)."
            )
        return loaded

    def get_next_proxy(self) -> Optional[Proxy]:
        """Return the next usable proxy, or ``None`` when none is ready."""
        with self._lock:
            now = time.monotonic()
            if not self._proxies:
                return None
            for _ in range(len(self._proxies)):
                self._index = (self._index + 1) % len(self._proxies)
                proxy = self._proxies[self._index]
                if proxy.is_usable(now):
                    return proxy
        return None

    def _wait_for_proxy(self, max_wait: float) -> Optional[Proxy]:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            proxy = self.get_next_proxy()
            if proxy is not None:
                return proxy
            time.sleep(0.2)
        return None

    def acquire_proxy(self) -> Optional[Proxy]:
        """Return a ready proxy, or ``None`` for direct mode / exhausted waits."""
        if self.total_count() == 0:
            return None
        proxy = self.get_next_proxy()
        return proxy if proxy is not None else self._wait_for_proxy(
            self._config["proxy_wait_timeout_seconds"]
        )

    def display(self, proxy: Optional[Proxy]) -> str:
        """Return a console-safe proxy label (masked when hide_proxies is on)."""
        if proxy is None:
            return "direct"
        if self._config.get("hide_proxies", True):
            return f"proxy#{proxy.seq}"
        return proxy.label

    def wait_for_proxy_slot(self, proxy: Optional[Proxy]) -> None:
        """Reserve a per-proxy request slot and sleep outside the pool lock."""
        if proxy is None:
            return
        rps = max(0.1, self._config.get("per_proxy_max_rps", 2.0))
        gap = 1.0 / rps
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, proxy.last_request_at + gap - now)
            proxy.last_request_at = max(now, proxy.last_request_at) + gap
            proxy.uses += 1
        if wait > 0:
            time.sleep(wait)

    def report_failure(self, proxy: Proxy, reason: Any) -> None:
        """Record a failure and apply cooldown or permanent-ban thresholds."""
        with self._lock:
            proxy.consecutive_failures += 1
            if proxy.consecutive_failures >= self._config["proxy_ban_after_failures"]:
                if not proxy.banned:
                    proxy.banned = True
                    proxy.bans += 1
                action = "banned"
            elif proxy.consecutive_failures >= self._config["proxy_failure_threshold"]:
                proxy.cooldown_until = (
                    time.monotonic() + self._config["proxy_cooldown_seconds"]
                )
                proxy.cooled += 1
                action = "cooled"
            else:
                action = None

        if action == "banned":
            log_warn(f"Proxy {self.display(proxy)} BANNED (too many failures).")
        elif action == "cooled":
            log_warn(
                f"Proxy {self.display(proxy)} cooled down for "
                f"{self._config['proxy_cooldown_seconds']}s "
                f"({proxy.consecutive_failures} consecutive failures: {trim(reason, 60)})"
            )

    def report_rate_limited(self, proxy: Optional[Proxy], cooldown_seconds: float) -> None:
        """Rest one proxy after a 429 without affecting its failure streak."""
        if proxy is None:
            return
        with self._lock:
            proxy.rate_limits += 1
            proxy.cooled += 1
            proxy.cooldown_until = max(
                proxy.cooldown_until,
                time.monotonic() + cooldown_seconds,
            )

    def earliest_ready_at(self) -> Optional[float]:
        """Return the earliest non-banned proxy recovery time, if any."""
        now = time.monotonic()
        with self._lock:
            ready_times = [
                proxy.cooldown_until
                for proxy in self._proxies
                if not proxy.banned and proxy.cooldown_until > now
            ]
        return min(ready_times) if ready_times else None

    def report_success(self, proxy: Optional[Proxy]) -> None:
        if proxy is None:
            return
        with self._lock:
            proxy.consecutive_failures = 0
            proxy.cooldown_until = 0.0

    def total_count(self) -> int:
        return len(self._proxies)

    def live_count(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(proxy.is_usable(now) for proxy in self._proxies)

    def banned_count(self) -> int:
        with self._lock:
            return sum(proxy.banned for proxy in self._proxies)

    def proxies(self) -> list[Proxy]:
        with self._lock:
            return list(self._proxies)

    def reset_health_stats(self) -> None:
        with self._lock:
            for proxy in self._proxies:
                proxy.uses = 0
                proxy.rate_limits = 0
                proxy.cooled = 0
                proxy.bans = 0


# ---------------------------------------------------------------------------
# Username generation and files
# ---------------------------------------------------------------------------


def generate_username(length: int, alphabet: str) -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


def random_usernames(
    count: int,
    length: int,
    alphabet: str,
    seen: Optional[set[str]] = None,
) -> Iterable[str]:
    """Yield up to *count* unique random names without endless collisions."""
    seen = set() if seen is None else seen
    generated = 0
    attempts = 0
    space = len(alphabet) ** length
    max_attempts = min(count * 10 + 1000, space * 5 + 1000)

    while generated < count and attempts < max_attempts:
        attempts += 1
        name = generate_username(length, alphabet)
        if name in seen:
            continue
        seen.add(name)
        generated += 1
        yield name

    if generated < count:
        log_warn(
            f"Only generated {generated}/{count} - the name space for "
            f"{length}-char usernames may be exhausted."
        )


def usernames_from_list(path: str) -> list[str]:
    """Read, lowercase, deduplicate, and return one username per line."""
    if not os.path.exists(path):
        log_error(f"File not found: {path}")
        return []

    names = []
    seen = set()
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            name = raw_line.strip().lower()
            if not name or name.startswith("#") or name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names


def generate_list_file(count: int, length: int, alphabet: str, path: str) -> tuple[int, int]:
    """Append unique generated usernames to *path* and return counts."""
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                name = raw_line.strip().lower()
                if name and not name.startswith("#"):
                    existing.add(name)

    written = 0
    attempts = 0
    space = len(alphabet) ** length
    max_attempts = min(count * 10 + 1000, space * 5 + 1000)
    with open(path, "a", encoding="utf-8") as handle:
        while written < count and attempts < max_attempts:
            attempts += 1
            name = generate_username(length, alphabet)
            if name in existing:
                continue
            existing.add(name)
            handle.write(name + "\n")
            written += 1

    if written < count:
        log_warn(
            f"Only generated {written}/{count} - the name space for "
            f"{length}-char usernames may be exhausted."
        )
    return written, len(existing)


# ---------------------------------------------------------------------------
# Rate limiter and Discord API client
# ---------------------------------------------------------------------------


class RateLimiter:
    """Global request cadence and fleet-wide pause scheduler."""

    def __init__(self, min_interval: float):
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._next_request_at = 0.0

    def throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self._min_interval
        if wait > 0:
            time.sleep(wait)

    def global_pause(self, seconds: float) -> None:
        with self._lock:
            self._next_request_at = max(
                self._next_request_at,
                time.monotonic() + seconds,
            )


class DiscordChecker:
    """HTTP client that maps Discord responses to checker status constants."""

    def __init__(self, config: dict):
        self._timeout = config["timeout_seconds"]
        self._user_agent = config.get("user_agent", DEFAULT_USER_AGENT)

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://discord.com",
            "referer": "https://discord.com/",
            "user-agent": self._user_agent,
            "x-discord-locale": "en-US",
            "x-super-properties": X_SUPER_PROPERTIES,
        }

    def check(self, username: str, proxy: Optional[Proxy]) -> tuple[str, Any]:
        """Check one username and return ``(status, detail)``; never raise."""
        proxies = None
        if proxy is not None:
            proxies = {"http": proxy.url, "https": proxy.url}

        session = requests.Session()
        try:
            response = session.post(
                USERNAME_ATTEMPT_URL,
                json={"username": username},
                headers=self._headers(),
                proxies=proxies,
                timeout=(min(5, self._timeout), self._timeout),
            )
        except requests.exceptions.ProxyError as exc:
            return ST_PROXY_ERROR, f"proxy error: {trim(exc)}"
        except requests.exceptions.SSLError as exc:
            status = ST_PROXY_ERROR if proxies else ST_NETWORK
            return status, f"SSL error: {trim(exc)}"
        except requests.exceptions.Timeout as exc:
            return ST_NETWORK, f"timeout: {trim(exc)}"
        except requests.exceptions.ConnectionError as exc:
            return ST_NETWORK, f"connection error: {trim(exc)}"
        except requests.exceptions.RequestException as exc:
            return ST_NETWORK, f"request error: {trim(exc)}"
        finally:
            session.close()

        return self._classify(response)

    @staticmethod
    def _classify(response: Any) -> tuple[str, Any]:
        status = response.status_code
        data = None
        try:
            data = response.json()
        except ValueError:
            pass
        body = json.dumps(data)[:160] if isinstance(data, dict) else ""

        if status == 200:
            if isinstance(data, dict):
                taken = data.get("taken")
                if taken is True:
                    return ST_TAKEN, "taken"
                if taken is False:
                    return ST_AVAILABLE, "available"
            return ST_UNKNOWN, f"200 with unexpected body: {body}"
        if status == 400:
            return ST_INVALID, f"400: {body or 'invalid username'}"
        if status == 429:
            retry_after = response.headers.get("Retry-After") or response.headers.get(
                "retry-after"
            )
            try:
                seconds = float(retry_after or 1.0)
            except (TypeError, ValueError):
                seconds = 1.0
            is_global = (
                (response.headers.get("X-RateLimit-Global") or "").lower()
                in ("true", "1")
            )
            return ST_RATE_LIMITED, (seconds, is_global)
        if status in (401, 403):
            return ST_BLOCKED, f"{status}: {body or 'access blocked'}"
        if 500 <= status < 600:
            return ST_UNKNOWN, f"{status} server error: {body}"
        return ST_UNKNOWN, f"{status}: {body}"


# ---------------------------------------------------------------------------
# Webhook notifier
# ---------------------------------------------------------------------------


class WebhookNotifier:
    """Background queue for available-name webhook notifications."""

    def __init__(self, config: dict):
        self.url = config["webhook_url"]
        self.enabled = bool(
            config["enable_webhook"]
            and self.url
            and "YOUR_ID" not in self.url
            and "REPLACE" not in self.url
        )
        self._platform = config.get("webhook_platform", "discord")
        self._footer = config.get("webhook_footer", "Discord Username Checker")
        self._retries = config["webhook_retries"]
        self._timeout = config["webhook_timeout_seconds"]
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="webhook-notifier",
            daemon=True,
        )
        self.posted = 0
        self.failed = 0

        if self.enabled:
            log_ok("Webhook notifications ENABLED")
        else:
            log_warn("Webhook disabled - set a real webhook_url in config.json")

    def start(self) -> None:
        if self.enabled:
            self._thread.start()

    def enqueue(self, username: str) -> None:
        if self.enabled:
            self._queue.put(username)

    def _run(self) -> None:
        while True:
            try:
                username = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set() and self._queue.empty():
                    return
                continue
            try:
                self._post(username)
            finally:
                self._queue.task_done()

    def _post(self, username: str) -> None:
        payload = {
            "embeds": [
                {
                    "title": f'{self._platform} "{username}" (available)',
                    "color": 0x57F287,
                    "footer": {"text": self._footer},
                }
            ]
        }
        for attempt in range(1, self._retries + 1):
            try:
                response = requests.post(self.url, json=payload, timeout=self._timeout)
                if response.status_code in (200, 204):
                    self.posted += 1
                    log_ok(f"Webhook posted: {username}")
                    return
                log_warn(
                    f"Webhook HTTP {response.status_code} for {username} "
                    f"(attempt {attempt}/{self._retries})"
                )
            except requests.RequestException as exc:
                log_warn(
                    f"Webhook error for {username}: {trim(exc)} "
                    f"(attempt {attempt}/{self._retries})"
                )
            time.sleep(attempt * 1.5)

        self.failed += 1
        log_error(f"Webhook FAILED for {username} after {self._retries} attempts")

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Worker engine
# ---------------------------------------------------------------------------


class CheckerEngine:
    """Run username checks in a bounded worker pool and collect statistics."""

    def __init__(self, config, proxy_manager, checker, notifier, rate_limiter):
        self._config = config
        self.pm = proxy_manager
        self.checker = checker
        self.notifier = notifier
        self.rl = rate_limiter

        self.stop_event = threading.Event()
        self._producer_done = threading.Event()
        self._queue = queue.Queue(maxsize=config["concurrency"] * 4)
        self._stats = Counter()
        self._stats_lock = threading.Lock()
        self._avail_lock = threading.Lock()
        self._results = 0

        self._verbose = config["verbose_results"]
        self._verbose_rl = config.get("verbose_rate_limits", False)
        self._verbose_waiting = config.get("verbose_proxy_waiting", False)
        self._pool_cooling_reported = False
        self._pool_cooling_lock = threading.Lock()

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] += amount

    def _save_available(self, username: str) -> None:
        path = os.path.join(SCRIPT_DIR, self._config["available_file"])
        try:
            with self._avail_lock, open(path, "a", encoding="utf-8") as handle:
                handle.write(username + "\n")
        except OSError as exc:
            log_error(f"Could not write {path}: {exc}")

    def run(self, usernames: list[str]) -> None:
        if not usernames:
            log_error("Nothing to check - no usernames.")
            return

        total = len(usernames)
        log_info(
            f"Starting check of {total} username(s) with "
            f"{self._config['concurrency']} workers, "
            f"{self.pm.live_count()}/{self.pm.total_count()} proxies live."
        )

        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

        self.stop_event.clear()
        with self._stats_lock:
            self._stats.clear()
            self._results = 0
        self.pm.reset_health_stats()

        started = time.monotonic()
        last_heartbeat = started
        self._producer_done.clear()
        workers = [
            threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True)
            for i in range(self._config["concurrency"])
        ]
        for worker in workers:
            worker.start()

        queued = 0
        try:
            for name in usernames:
                self._queue.put(name)
                queued += 1
                now = time.monotonic()
                if self._verbose and now - last_heartbeat >= 5.0:
                    last_heartbeat = now
                    log_dim(f"filling queue... {queued}/{total} queued")
        except KeyboardInterrupt:
            self.stop_event.set()
        finally:
            self._producer_done.set()

        try:
            while not self.stop_event.is_set() and self._queue.unfinished_tasks > 0:
                time.sleep(0.1)
                now = time.monotonic()
                if self._verbose and now - last_heartbeat >= 5.0:
                    last_heartbeat = now
                    finished = total - self._queue.unfinished_tasks
                    log_dim(
                        f"working... {finished}/{total} finished, "
                        f"{self.pm.live_count()}/{self.pm.total_count()} proxies live"
                    )
        except KeyboardInterrupt:
            self.stop_event.set()
        finally:
            grace = self._config["timeout_seconds"] + self._config["retry_backoff_max"] + 10
            for worker in workers:
                worker.join(timeout=grace)
            if self._queue.unfinished_tasks > 0:
                log_warn(f"{self._queue.unfinished_tasks} username(s) were not checked.")

        self._print_summary(started)

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                username = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._producer_done.is_set() and self._queue.unfinished_tasks == 0:
                    return
                continue
            try:
                self._process(username)
            except Exception:
                log_error(f"Unexpected error while checking {username!r}")
                try:
                    import traceback
                    log_error(trim(traceback.format_exc(limit=3), 300))
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def _acquire_proxy(self, deadline: Optional[float] = None) -> Optional[Proxy]:
        if deadline is None:
            deadline = time.monotonic() + self._config.get(
                "proxy_recovery_timeout_seconds", 1800
            )

        while not self.stop_event.is_set():
            proxy = self.pm.acquire_proxy()
            if proxy is not None:
                with self._pool_cooling_lock:
                    self._pool_cooling_reported = False
                return proxy
            if self.pm.total_count() == 0:
                return None

            ready_at = self.pm.earliest_ready_at()
            if ready_at is None:
                return None
            wait = min(max(0.0, ready_at - time.monotonic()), 60.0)
            if wait <= 0:
                continue
            if time.monotonic() + wait > deadline:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return None

            with self._pool_cooling_lock:
                should_report = (
                    self._verbose
                    and self._verbose_waiting
                    and not self._pool_cooling_reported
                )
                if should_report:
                    self._pool_cooling_reported = True
            if should_report:
                log_dim(
                    f"all proxies cooling ({self.pm.live_count()}/"
                    f"{self.pm.total_count()} live) - waiting for the pool to recover..."
                )
            self.stop_event.wait(wait)
        return None

    def _process(self, username: str) -> None:
        if self.stop_event.is_set():
            return

        name = username.strip().lower()
        min_length = self._config["username_min_length"]
        max_length = self._config["username_max_length"]
        if not min_length <= len(name) <= max_length:
            self._bump(ST_INVALID)
            self._finalize(ST_INVALID, name, f"length must be {min_length}-{max_length} chars")
            return

        retries = self._config["max_retries_per_username"]
        backoff = self._config["retry_backoff_base"]
        backoff_max = self._config["retry_backoff_max"]
        recovery_deadline = time.monotonic() + self._config.get(
            "proxy_recovery_timeout_seconds", 1800
        )
        proxy = self._acquire_proxy(recovery_deadline)

        if proxy is None and self.pm.total_count() > 0:
            self._bump(ST_UNKNOWN)
            self._finalize("gave_up", name, "no live proxy available (all banned)")
            return

        for attempt in range(1, retries + 1):
            if self.stop_event.is_set():
                return
            self.rl.throttle()
            self.pm.wait_for_proxy_slot(proxy)
            status, detail = self.checker.check(name, proxy)

            if status == ST_RATE_LIMITED:
                seconds, is_global = (
                    detail if isinstance(detail, tuple) else (float(detail), False)
                )
                self._bump(ST_RATE_LIMITED)
                where = self.pm.display(proxy)
                is_global = is_global or proxy is None
                if is_global:
                    cooldown = min(seconds, self._config["max_proxy_cooldown_seconds"])
                    pause = min(seconds, self._config["max_global_pause_seconds"])
                    log_rate_limit(
                        f"GLOBAL rate limit on {name!r} via {where} - "
                        f"pausing {pause:.0f}s, cooling proxy {cooldown:.0f}s"
                    )
                    self.rl.global_pause(pause)
                    self.pm.report_rate_limited(proxy, cooldown)
                else:
                    cooldown = max(seconds, self._config["proxy_cooldown_seconds"])
                    cooldown = min(cooldown, self._config["max_proxy_cooldown_seconds"])
                    if self._verbose_rl:
                        log_rate_limit(
                            f"Rate limited on {name!r} via {where} - "
                            f"cooling that proxy {cooldown:.0f}s, switching..."
                        )
                    self.pm.report_rate_limited(proxy, cooldown)
                proxy = self._acquire_proxy(recovery_deadline)
                if proxy is None and self.pm.total_count() > 0:
                    self._bump(ST_UNKNOWN)
                    self._finalize("gave_up", name, "no live proxy available")
                    return
                continue

            if status in (ST_NETWORK, ST_PROXY_ERROR):
                if self._verbose:
                    log_dim(
                        f"{trim(detail, 80)} on {name!r} - "
                        f"retry {attempt}/{retries}"
                    )
                if proxy is not None:
                    self.pm.report_failure(proxy, detail)
                proxy = self._acquire_proxy(recovery_deadline)
                if proxy is None and self.pm.total_count() > 0:
                    log_warn(
                        f"No live proxy available, giving up on {name!r} "
                        f"(attempt {attempt}/{retries})"
                    )
                    break
                time.sleep(min(backoff ** (attempt - 1), backoff_max))
                continue

            if status == ST_BLOCKED:
                self._bump(ST_BLOCKED)
                log_warn(f"Blocked ({trim(detail, 80)}) on {name!r} - swapping proxy")
                if proxy is not None:
                    self.pm.report_failure(proxy, "blocked")
                proxy = self._acquire_proxy(recovery_deadline)
                if proxy is None and self.pm.total_count() > 0:
                    self._bump(ST_UNKNOWN)
                    self._finalize("gave_up", name, "no live proxy available")
                    return
                time.sleep(min(backoff ** attempt, backoff_max))
                continue

            if status == ST_UNKNOWN:
                self._bump(ST_UNKNOWN)
                if proxy is not None:
                    self.pm.report_failure(proxy, detail)
                log_warn(
                    f"{trim(detail, 80)} on {name!r} - retry {attempt}/{retries}"
                )
                proxy = self._acquire_proxy(recovery_deadline)
                if proxy is None and self.pm.total_count() > 0:
                    self._bump(ST_UNKNOWN)
                    self._finalize("gave_up", name, "no live proxy available")
                    return
                time.sleep(min(backoff ** (attempt - 1), backoff_max))
                continue

            if proxy is not None:
                self.pm.report_success(proxy)
            self._bump(status)
            self._finalize(status, name, detail)
            return

        self._bump(ST_UNKNOWN)
        self._finalize("gave_up", name, f"gave up after {retries} attempts")

    def _finalize(self, status: str, username: str, detail: Any) -> None:
        if status == ST_AVAILABLE:
            self._save_available(username)
            if self.notifier.enabled:
                self.notifier.enqueue(username)
            log_success(username)
        elif status == ST_TAKEN:
            if self._verbose:
                log_taken(username)
        elif status == ST_INVALID:
            if self._verbose:
                log_dim(f"INVALID    {username}  ({trim(detail, 60)})")
        elif status == "gave_up":
            if self._verbose:
                log_dim(f"UNKNOWN    {username}  ({trim(detail, 60)})")

        with self._stats_lock:
            self._results += 1
            result_number = self._results
        if result_number % 20 == 0:
            self._print_progress()

    def _print_progress(self) -> None:
        with self._stats_lock:
            checked = self._results
            available = self._stats[ST_AVAILABLE]
            taken = self._stats[ST_TAKEN]
            rate_limited = self._stats[ST_RATE_LIMITED]
        log_dim(
            f"progress: {checked} checked | "
            f"{Fore.GREEN}{available} available{Fore.LIGHTBLACK_EX} | "
            f"{Fore.RED}{taken} taken{Fore.LIGHTBLACK_EX} | "
            f"{Fore.MAGENTA}{rate_limited} rate-limited{Fore.LIGHTBLACK_EX} | "
            f"proxies live {self.pm.live_count()}/{self.pm.total_count()}"
        )

    def _print_summary(self, start_time: float) -> None:
        elapsed = time.monotonic() - start_time
        with self._stats_lock:
            stats = dict(self._stats)
            total = self._results

        available = stats.get(ST_AVAILABLE, 0)
        taken = stats.get(ST_TAKEN, 0)
        invalid = stats.get(ST_INVALID, 0)
        rate_limited = stats.get(ST_RATE_LIMITED, 0)
        blocked = stats.get(ST_BLOCKED, 0)
        unknown = stats.get(ST_UNKNOWN, 0)
        rate = total / elapsed if elapsed > 0 else 0.0

        log_dim("")
        _emit(f"{Fore.MAGENTA}{'=' * 58}{Style.RESET_ALL}")
        _emit(f"{Fore.LIGHTWHITE_EX}  Summary{Style.RESET_ALL}")
        _emit(f"{Fore.MAGENTA}{'=' * 58}{Style.RESET_ALL}")
        _emit(f"  {'Total checked:':<20}{total}   ({rate:.1f}/s in {elapsed:.1f}s)")
        _emit(f"  {Fore.GREEN}{'Available:':<20}{available}{Style.RESET_ALL}")
        _emit(f"  {Fore.RED}{'Taken:':<20}{taken}{Style.RESET_ALL}")
        _emit(f"  {Fore.YELLOW}{'Invalid:':<20}{invalid}{Style.RESET_ALL}")
        _emit(f"  {Fore.MAGENTA}{'Rate limited:':<20}{rate_limited}{Style.RESET_ALL}")
        _emit(f"  {Fore.YELLOW}{'Blocked:':<20}{blocked}{Style.RESET_ALL}")
        _emit(f"  {Fore.RED}{'Errors/gave up:':<20}{unknown}{Style.RESET_ALL}")
        _emit(
            f"  {'Proxies live:':<20}{self.pm.live_count()}/"
            f"{self.pm.total_count()} ({self.pm.banned_count()} banned)"
        )
        if self.notifier.enabled:
            _emit(
                f"  {'Webhook posted:':<20}{self.notifier.posted} "
                f"({self.notifier.failed} failed)"
            )
        _emit(f"  {'Saved to:':<20}{os.path.join(SCRIPT_DIR, self._config['available_file'])}")
        self._print_proxy_summary()
        _emit(f"{Fore.MAGENTA}{'=' * 58}{Style.RESET_ALL}")

    def _print_proxy_summary(self) -> None:
        proxies = self.pm.proxies()
        if not proxies:
            return

        if self._config.get("hide_proxies", True):
            _emit(
                f"  {Fore.LIGHTBLACK_EX}{'Proxy details:':<20}hidden "
                f"(hide_proxies=true){Style.RESET_ALL}"
            )
            return

        limit = int(self._config.get("proxy_summary_limit", 0) or 0)
        rows = sorted(proxies, key=lambda proxy: proxy.uses, reverse=True)
        shown = rows if limit <= 0 else rows[:limit]
        total_uses = sum(proxy.uses for proxy in proxies)
        total_429 = sum(proxy.rate_limits for proxy in proxies)
        total_cooled = sum(proxy.cooled for proxy in proxies)
        total_bans = sum(proxy.bans for proxy in proxies)

        _emit(f"{Fore.LIGHTWHITE_EX}  Proxy health (this run):{Style.RESET_ALL}")
        _emit(f"  {'proxy':<30}{'used':>7}{'429s':>7}{'cooled':>8}{'banned':>9}")
        for proxy in shown:
            color = Fore.RED if proxy.banned else (
                Fore.YELLOW if (proxy.rate_limits or proxy.cooled) else Fore.GREEN
            )
            state = "BANNED" if proxy.banned else str(proxy.bans)
            label = proxy.label if len(proxy.label) <= 29 else proxy.label[:27] + "..."
            _emit(
                f"  {color}{label:<30}{proxy.uses:>7}"
                f"{proxy.rate_limits:>7}{proxy.cooled:>8}{state:>9}{Style.RESET_ALL}"
            )
        if limit > 0 and len(rows) > limit:
            log_dim(
                f"  ... {len(rows) - limit} more proxy/proxies "
                f"(proxy_summary_limit={limit})"
            )
        _emit(
            f"  {Fore.LIGHTBLACK_EX}{'TOTAL':<30}{total_uses:>7}"
            f"{total_429:>7}{total_cooled:>8}{total_bans:>9}{Style.RESET_ALL}"
        )


# ---------------------------------------------------------------------------
# Banner, menu, and CLI helpers
# ---------------------------------------------------------------------------


def startup_animation() -> None:
    """Show the startup animation only when stdout is an interactive TTY."""
    try:
        if not sys.stdout.isatty():
            return
    except (AttributeError, OSError):
        return

    frames = (
        "[>...................] booting availability scanner",
        "[====>...............] loading Discord API client",
        "[========>...........] preparing worker pool",
        "[============>.......] checking proxy configuration",
        "[==================>] scanner ready",
    )
    with _PRINT_LOCK:
        for frame in frames:
            sys.stdout.write(f"\r{Fore.LIGHTWHITE_EX}{frame}{Style.RESET_ALL}")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write(
            f"\r{Fore.GREEN}{Style.BRIGHT}[OK] scanner ready"
            f"{' ' * 42}{Style.RESET_ALL}\n"
        )
        sys.stdout.flush()


def show_banner() -> None:
    banner = r"""
 ____  _                     _
|  _ \(_)___  ___ _ __ ___  | |
| | | | / __|/ _ \ '__/ __| | |
| |_| | \__ \  __/ |  \__ \ |_|
|____/|_|___/\___|_|  |___/ (_)
"""
    _emit(f"{Fore.MAGENTA}{banner}{Style.RESET_ALL}")
    _emit(
        f"{Fore.LIGHTBLACK_EX}Discord Username Availability Checker "
        f"- proxy rotation, retries & webhook notifications{Style.RESET_ALL}"
    )
    _emit(f"{Fore.LIGHTBLACK_EX}{'-' * 62}{Style.RESET_ALL}")
    startup_animation()


MENU_TEXT = """
    +--------------------------------------------------------------+
    |  [1]  Load usernames from list.txt                           |
    |  [2]  Random 4-character usernames  (a-z, 0-9, _)            |
    |  [3]  Random 4-letter usernames     (a-z)                    |
    |  [4]  Random 3-character usernames  (a-z, 0-9, _)            |
    |  [5]  Generate random usernames into list.txt (no checking)  |
    |  [0]  Quit                                                   |
    +--------------------------------------------------------------+
"""


def show_menu() -> None:
    _emit(f"{Fore.MAGENTA}{MENU_TEXT}{Style.RESET_ALL}")


def prompt_int(prompt: str, default: int, low: int, high: int) -> int:
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            return default
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            log_error("Please enter a number.")
            continue
        if low <= value <= high:
            return value
        log_error(f"Please enter a number between {low} and {high}.")


def build_names_for_mode(mode: int, config: dict, count: int) -> list[str]:
    if mode == 1:
        names = usernames_from_list(
            os.path.join(SCRIPT_DIR, config.get("list_file", "list.txt"))
        )
        if not names:
            log_error("list.txt is empty or missing - add usernames first.")
        return names
    if mode == 2:
        return list(random_usernames(count, 4, ALPHABET_ALL))
    if mode == 3:
        return list(random_usernames(count, 4, ALPHABET_LETTERS))
    if mode == 4:
        return list(random_usernames(count, 3, ALPHABET_ALL))
    return []


def build_engine(config: dict) -> CheckerEngine:
    proxy_manager = ProxyManager(config)
    proxy_manager.load(
        os.path.join(REPO_ROOT, config.get("proxy_file", "proxy.txt"))
    )
    checker = DiscordChecker(config)
    limiter = RateLimiter(config["request_interval_seconds"])
    notifier = WebhookNotifier(config)
    notifier.start()
    return CheckerEngine(config, proxy_manager, checker, notifier, limiter)


def run_selftest(config: dict) -> None:
    """Send one direct request and display the raw connectivity result."""
    test_name = "zz" + "".join(random.choices(string.ascii_lowercase, k=8))
    log_info(
        f"Self-test: sending one request to discord.com (direct, no proxy) "
        f"with test name {test_name!r}..."
    )

    session = requests.Session()
    started = time.monotonic()
    try:
        response = session.post(
            USERNAME_ATTEMPT_URL,
            json={"username": test_name},
            headers=DiscordChecker(config)._headers(),
            timeout=(min(5, config["timeout_seconds"]), config["timeout_seconds"]),
        )
        log_ok(f"HTTP {response.status_code} in {time.monotonic() - started:.1f}s")
        log_info(f"Response body: {response.text[:200] or '(empty)'}")
        if response.status_code == 200:
            try:
                taken = response.json().get("taken")
            except ValueError:
                taken = None
            if taken is False:
                log_ok(
                    f"Discord API is fully reachable and checking works "
                    f"(test name {test_name!r} is available)."
                )
            elif taken is True:
                log_ok(
                    f"Discord API is fully reachable and checking works "
                    f"(test name {test_name!r} is currently taken)."
                )
            else:
                log_ok("Discord API is fully reachable and checking works.")
        elif response.status_code == 400:
            log_warn(
                "Discord answered 400 (invalid username) but the API is "
                "reachable - it validated and rejected the name. Checking should work."
            )
        elif response.status_code == 429:
            log_warn("Discord answered with 429 (rate limited) - requests are being throttled.")
        elif response.status_code in (401, 403):
            log_warn("Discord answered with 401/403 - endpoint or IP is blocked.")
        else:
            log_warn(f"Unexpected status {response.status_code}.")
    except requests.exceptions.Timeout:
        log_error(
            f"Request TIMED OUT after {time.monotonic() - started:.1f}s "
            "- check your internet connection."
        )
    except requests.exceptions.ConnectionError as exc:
        log_error(
            f"CONNECTION ERROR after {time.monotonic() - started:.1f}s: "
            f"{trim(exc, 120)}"
        )
    except Exception as exc:
        log_error(
            f"{type(exc).__name__} after {time.monotonic() - started:.1f}s: "
            f"{trim(exc, 120)}"
        )
    finally:
        session.close()


def test_proxies(config: dict) -> None:
    """Test proxy entries and safely prune dead or blocked entries."""
    path = os.path.join(REPO_ROOT, config.get("proxy_file", "proxy.txt"))
    manager = ProxyManager(config)
    loaded = manager.load(path, force=True)
    if loaded == 0:
        log_error(f"No proxies found in {path}.")
        return

    log_info(
        f"Testing {loaded} proxy/proxies with a real request to Discord "
        "(this can take a few seconds)..."
    )
    test_config = dict(config)
    test_config["timeout_seconds"] = min(config["timeout_seconds"], 6)
    checker = DiscordChecker(test_config)
    started = time.monotonic()

    def test_one(proxy: Proxy):
        request_started = time.monotonic()
        status, detail = checker.check("discord", proxy)
        elapsed = time.monotonic() - request_started
        blocked = status == ST_BLOCKED
        ok = status not in (ST_NETWORK, ST_PROXY_ERROR)
        if status == ST_RATE_LIMITED and isinstance(detail, tuple):
            seconds, is_global = detail
            detail_text = f"429 {seconds:.0f}s{' GLOBAL' if is_global else ''}"
        else:
            detail_text = trim(detail, 40)
        return proxy, ok, blocked, detail_text, elapsed

    results = {}
    with ThreadPoolExecutor(max_workers=min(loaded, 25)) as pool:
        futures = [pool.submit(test_one, proxy) for proxy in manager.proxies()]
        for future in as_completed(futures):
            proxy, ok, blocked, detail, elapsed = future.result()
            label = manager.display(proxy)
            results[label] = (proxy, ok, blocked, detail, elapsed)
            if not ok:
                log_dim(f"{label}  DEAD ({elapsed:.1f}s) - {detail}")
            elif blocked:
                log_warn(f"{label}  OK but BLOCKED ({elapsed:.1f}s) - {detail}")
            else:
                log_ok(f"{label}  OK   ({elapsed:.1f}s) - {detail}")

    healthy = [item[0] for item in results.values() if item[1] and not item[2]]
    dead = sum(not item[1] for item in results.values())
    blocked_count = sum(item[2] for item in results.values())
    elapsed = time.monotonic() - started
    removed = loaded - len(healthy)
    log_info(
        f"Test finished in {elapsed:.1f}s: {len(healthy)}/{loaded} proxies kept "
        f"({dead} dead, {blocked_count} blocked)."
    )

    if not healthy:
        log_error("No healthy proxies found - proxy.txt left unchanged.")
        return
    if removed == 0:
        log_ok("All proxies are healthy - proxy.txt unchanged.")
        return

    backup_path = os.path.join(os.path.dirname(path), "proxy_backup.txt")
    try:
        shutil.copy2(path, backup_path)
        log_ok(f"Backed up the previous list to {backup_path}")
    except OSError as exc:
        log_warn(f"Could not back up {path}: {exc}")

    log_info(f"Rewriting {path} with only the {len(healthy)} healthy proxies...")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Discord username checker - HTTP proxy list\n")
            handle.write("# Format: user:pass@host:port (one per line)\n\n")
            for proxy in healthy:
                handle.write(proxy.url.split("://", 1)[1] + "\n")
        log_ok(f"Done - removed {removed} dead/blocked proxy/proxies.")
    except OSError as exc:
        log_error(f"Could not rewrite {path}: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord username availability checker")
    parser.add_argument("--check", metavar="USERNAME", help="check a single username and exit")
    parser.add_argument("--run", type=int, choices=[1, 2, 3, 4], help="run a mode non-interactively")
    parser.add_argument("--count", type=int, help="how many random usernames to generate")
    parser.add_argument("--threads", type=int, help="override concurrency")
    parser.add_argument("--quiet", action="store_true", help="only print available/errors/stats")
    parser.add_argument("--config", default=CONFIG_PATH, help="path to a custom config.json")
    parser.add_argument("--genlist", type=int, metavar="N", help="generate N random usernames into list.txt and exit")
    parser.add_argument("--length", type=int, help="username length for --genlist (default 4)")
    parser.add_argument("--letters", action="store_true", help="--genlist uses letters only")
    parser.add_argument("--selftest", action="store_true", help="send one test request to Discord and exit")
    parser.add_argument("--test-proxies", action="store_true", help="test proxies and keep only healthy entries")
    parser.add_argument("--no-auto-test", action="store_true", help="skip automatic proxy testing at startup")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    config["webhook_url"] = resolve_webhook_url(config)
    if args.threads and args.threads > 0:
        config["concurrency"] = args.threads
    if args.count and args.count > 0:
        config["default_random_count"] = args.count
    if args.quiet:
        config["verbose_results"] = False

    show_banner()

    if args.selftest:
        run_selftest(config)
        return
    if args.test_proxies:
        test_proxies(config)
        return
    if args.genlist:
        length = max(2, min(32, args.length or 4))
        alphabet = ALPHABET_LETTERS if args.letters else ALPHABET_ALL
        path = os.path.join(SCRIPT_DIR, config.get("list_file", "list.txt"))
        written, total = generate_list_file(args.genlist, length, alphabet, path)
        log_ok(f"Generated {written} new username(s). {total} total entries in {path}")
        return

    if config.get("auto_test_proxies") and config.get("use_proxies") and not args.no_auto_test:
        test_proxies(config)

    engine = build_engine(config)
    try:
        if args.check:
            log_info(f"Checking single username: {args.check}")
            engine.run([args.check])
            return
        if args.run:
            names = build_names_for_mode(args.run, config, config["default_random_count"])
            engine.run(names)
            return

        while True:
            show_menu()
            try:
                choice = input("    Select an option [0-4]: ").strip()
            except EOFError:
                break
            if choice in ("", "0"):
                break
            if choice not in ("1", "2", "3", "4", "5"):
                log_error("Invalid option.")
                continue

            mode = int(choice)
            if mode == 5:
                count = prompt_int(
                    f"    How many usernames to generate? [{config['default_random_count']}]: ",
                    config["default_random_count"],
                    1,
                    MAX_GENERATED_NAMES,
                )
                length = prompt_int("    Username length? [4]: ", 4, 2, 32)
                try:
                    alphabet_choice = input(
                        "    Alphabet (1 = letters+digits+underscore, 2 = letters only) [1]: "
                    ).strip()
                except EOFError:
                    alphabet_choice = ""
                alphabet = ALPHABET_LETTERS if alphabet_choice == "2" else ALPHABET_ALL
                path = os.path.join(SCRIPT_DIR, config.get("list_file", "list.txt"))
                written, total = generate_list_file(count, length, alphabet, path)
                log_ok(f"Generated {written} new username(s). {total} total entries in {path}")
                continue

            count = config["default_random_count"]
            if mode in (2, 3, 4):
                count = prompt_int(
                    f"    How many usernames to generate? [{config['default_random_count']}]: ",
                    config["default_random_count"],
                    1,
                    MAX_GENERATED_NAMES,
                )
            engine.run(build_names_for_mode(mode, config, count))
    except KeyboardInterrupt:
        print()
        log_warn("KeyboardInterrupt received - shutting down gracefully...")
    finally:
        engine.stop_event.set()
        engine.notifier.stop()

    _emit(f"{Fore.MAGENTA}Goodbye. Thanks for using the checker!{Style.RESET_ALL}")


if __name__ == "__main__":
    main()
