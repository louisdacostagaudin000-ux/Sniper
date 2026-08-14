#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the username checkers: rotating proxies + webhook alerts.

Reads proxies from ``proxy.txt`` and the webhook URL from ``webhook.txt`` at the
repo root, so every checker uses the same lists. The checkers stay
``roblox.py``-style; this module only supplies the shared proxy pool and
webhook notifier they hook into.
"""

import concurrent.futures
import functools
import os
import threading
import time
from queue import Queue

import requests

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
PROXY_FILE = os.path.join(REPO_ROOT, "proxy.txt")
WEBHOOK_FILE = os.path.join(REPO_ROOT, "webhook.txt")


def _lines(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def load_proxies():
    """Return the list of proxy URLs from the shared root ``proxy.txt``."""
    return _lines(PROXY_FILE)


def load_webhook_url():
    """Return the shared webhook URL, or "" if unset/placeholder."""
    for line in _lines(WEBHOOK_FILE):
        if line.startswith("http") and "YOUR_" not in line:
            return line
    return ""


class ProxyPool:
    """Thread-safe round-robin pool of proxy URLs with dead-proxy cooldown.

    After ``max_failures`` consecutive failures a proxy is put on cooldown for
    ``cooldown_seconds`` and ``next()`` skips it, so a batch of expired proxies
    (e.g. ones answering ``407 Proxy Authentication Required``) isn't retried
    once per username.
    """

    def __init__(self, proxies, max_failures=3, cooldown_seconds=300):
        self._lock = threading.Lock()
        self._proxies = list(proxies)
        self._index = 0
        self._max_failures = max_failures
        self._cooldown_seconds = cooldown_seconds
        self._failures = {}
        self._cooldown_until = {}
        self.pruned = False

    def __len__(self):
        return len(self._proxies)

    @property
    def enabled(self):
        return len(self._proxies) > 0

    def live_count(self):
        """Number of proxies that are not currently cooling down."""
        now = time.monotonic()
        with self._lock:
            return sum(
                1 for proxy in self._proxies
                if self._cooldown_until.get(proxy, 0.0) <= now
            )

    def next(self):
        """Return the next non-cooling proxy, or ``None`` if none is ready."""
        with self._lock:
            if not self._proxies:
                return None
            now = time.monotonic()
            for _ in range(len(self._proxies)):
                self._index = (self._index + 1) % len(self._proxies)
                proxy = self._proxies[self._index]
                if self._cooldown_until.get(proxy, 0.0) <= now:
                    return proxy
            return None

    def report_failure(self, proxy):
        """Record a proxy failure; cooldown after ``max_failures`` in a row."""
        if not proxy:
            return
        with self._lock:
            fails = self._failures.get(proxy, 0) + 1
            if fails >= self._max_failures:
                self._cooldown_until[proxy] = time.monotonic() + self._cooldown_seconds
                self._failures.pop(proxy, None)
            else:
                self._failures[proxy] = fails

    def report_success(self, proxy):
        """Clear a proxy's failure/cooldown state after a usable response."""
        if not proxy:
            return
        with self._lock:
            self._failures.pop(proxy, None)
            self._cooldown_until.pop(proxy, None)

    def report_dead(self, proxy):
        """Permanently disable a proxy for the rest of this process.

        A 407 (proxy authentication required) means the proxy's credentials are
        expired or invalid; it won't recover this session, so it is removed
        from rotation rather than re-tried every cooldown cycle.
        """
        if not proxy:
            return
        with self._lock:
            self._cooldown_until[proxy] = time.monotonic() + 365 * 24 * 3600
            self._failures.pop(proxy, None)

    def prune_dead(self, timeout=5, max_workers=100):
        """One-time startup probe: disable dead/expired proxies.

        Tests every proxy in parallel and permanently disables the dead ones.
        Returns the number pruned. Idempotent - the probe runs at most once per
        process, so callers can invoke it freely.
        """
        if self.pruned:
            return 0
        self.pruned = True
        with self._lock:
            proxies = list(self._proxies)
        if not proxies:
            return 0
        test = functools.partial(_proxy_alive, timeout=timeout)
        workers = min(max_workers, len(proxies))
        killed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for proxy, ok in zip(proxies, pool.map(test, proxies)):
                if not ok:
                    self.report_dead(proxy)
                    killed += 1
        return killed


def proxies_dict(proxy):
    """Build a ``requests`` proxies mapping for one proxy line.

    ``proxy.txt`` entries are ``user:pass@host:port`` (no scheme), so prepend
    ``http://`` when it is missing - otherwise ``requests`` can fail to send the
    credentials and the proxy responds with a 407.
    """
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def is_proxy_failure(exc=None, status_code=None):
    """Heuristic: did the *proxy* fail, rather than the platform throttling us?

    ``exc`` is the exception a request raised; ``status_code`` is an HTTP
    status. A 407 (proxy authentication required) or a connection error while a
    proxy is in use both point at the proxy rather than the target site.
    """
    if status_code == 407:
        return True
    if exc is not None:
        # requests.exceptions.ProxyError is a subclass of ConnectionError.
        return isinstance(exc, requests.exceptions.ConnectionError)
    return False


def _proxy_alive(proxy, timeout=5):
    """Return True if ``proxy`` can fetch example.com (i.e. it isn't dead)."""
    try:
        r = requests.get(
            "https://example.com",
            proxies=proxies_dict(proxy),
            timeout=timeout,
            allow_redirects=False,
        )
        return r.status_code == 200
    except Exception:
        return False


def probe_proxies(proxies, timeout=5, max_workers=100):
    """Return ``(live, dead)`` counts by testing each proxy in parallel.

    Each proxy fetches ``https://example.com`` through itself; a 200 response
    marks it live, while a 407 / connection error / timeout marks it dead. This
    is used by the menu to show proxy health without touching each platform.
    """
    if not proxies:
        return 0, 0
    live = 0
    test = functools.partial(_proxy_alive, timeout=timeout)
    workers = min(max_workers, len(proxies))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for ok in pool.map(test, proxies):
            if ok:
                live += 1
    return live, len(proxies) - live


class WebhookNotifier:
    """Posts ``platform "username" (status)`` alerts on a background thread."""

    def __init__(self, platform):
        self.platform = platform
        self.url = load_webhook_url()
        self._queue = Queue()
        self._thread = None
        if self.url:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def enabled(self):
        return bool(self.url)

    def notify(self, username, status):
        if self.url:
            self._queue.put((username, status))

    def _run(self):
        while True:
            username, status = self._queue.get()
            try:
                self._post(username, status)
            except Exception:
                pass
            self._queue.task_done()

    def _post(self, username, status):
        payload = {
            "username": "Username Checker",
            "embeds": [
                {
                    "title": f'{self.platform} "{username}" ({status})',
                    "color": 0x9B59B6,
                    "footer": {"text": f"{self.platform.capitalize()} Username Checker"},
                }
            ],
        }
        requests.post(self.url, json=payload, timeout=10)
