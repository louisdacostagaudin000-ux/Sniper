#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the username checkers: rotating proxies + webhook alerts.

Reads proxies from ``proxy.txt`` and the webhook URL from ``webhook.txt`` at the
repo root, so every checker uses the same lists. The checkers stay
``roblox.py``-style; this module only supplies the shared proxy pool and
webhook notifier they hook into.
"""

import os
import threading
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
    """Thread-safe round-robin pool of proxy URLs."""

    def __init__(self, proxies):
        self._lock = threading.Lock()
        self._proxies = list(proxies)
        self._index = 0

    def __len__(self):
        return len(self._proxies)

    @property
    def enabled(self):
        return len(self._proxies) > 0

    def next(self):
        with self._lock:
            if not self._proxies:
                return None
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            return proxy


def proxies_dict(proxy):
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


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
