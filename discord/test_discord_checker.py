#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for the quiet-logging behavior of discord_checker.py.

Covers the two changes made to tame console spam:

1. The once-per-transition "all proxies cooling" message in
   CheckerEngine._acquire_proxy - it must print at most once while the pool is
   dead (even with many workers racing), be suppressed when
   verbose_proxy_waiting is off, be suppressed under --quiet
   (verbose_results=False), and print again after the pool recovers and dies
   again.

2. The gating of per-name 429 log lines in CheckerEngine._process - non-global
   429s only print when verbose_rate_limits is on, while *global* 429s always
   print.

3. The other log paths in CheckerEngine._process - the "Blocked ... - swapping
   proxy" warning and the dim network/proxy-error retry lines (which respect
   verbose_results / --quiet).

4. The heartbeat lines in CheckerEngine.run - "filling queue..." and
   "working... N/M finished" only print when verbose_results is on. The clock
   is faked (FakeTime replaces dc.time) so a 5-second heartbeat can be
   observed without actually waiting 5 seconds.

5. The run() summary output - CheckerEngine._print_summary renders the totals
   block (available / taken / invalid / rate limited / blocked / gave up) and
   _print_proxy_summary renders the per-proxy health table (used / 429s /
   cooled / banned) plus a TOTAL row, respecting proxy_summary_limit.

6. An end-to-end --quiet run: QuietIntegrationTest invokes the real main()
   (with the network mocked and temp dirs) and verifies that only available
   names / errors / stats print - no taken/invalid/heartbeat lines - and that
   available names are still written to available.txt.

7. The webhook notifier: WebhookNotifierTest drives WebhookNotifier._post (and
   the full queue/thread lifecycle) with requests.post mocked - a 200/204
   posts once, HTTP errors and transport exceptions retry up to webhook_retries
   then fail, the retry backoff sleeps increase 1.5x per attempt (1.5/3.0/4.5s)
   and a success never sleeps, and the background thread drains and stops
   cleanly.

8. The interactive menu: InteractiveMenuTest feeds input() responses into
   main() (with build_engine / name-building stubbed) and verifies mode
   selection - 1 runs list.txt, 2-4 prompt for a count, 5 generates a list,
   invalid options and bad counts reprompt, EOF / blank choices quit, and
   Ctrl+C (KeyboardInterrupt) shuts down gracefully - the warning prints, the
   engine's stop flag is set, the notifier is stopped, and Goodbye is shown.

9. The proxy health logic: ProxyManagerHealthTest exercises round-robin
   rotation (skipping cooling/banned proxies), the failure -> cooldown -> ban
   transitions, success resetting a failure streak, rate-limit rests that
   never shorten an existing cooldown, earliest_ready_at, and load() parsing
   with deduplication.

10. The global rate limiter: RateLimiterTest verifies that the first request
    passes instantly, later throttles keep the min_interval cadence,
    global_pause delays the next request and only ever *extends* the schedule
    (never shortens it), and concurrent throttles never share a slot.

11. The --genlist CLI path: GenListIntegrationTest runs main() with
    --genlist against a temp list.txt (SCRIPT_DIR redirected) and verifies the
    real generate_list_file() - seed names are never duplicated, counts are
    reported correctly, default length/alphabet apply, comments/blanks are
    ignored, and an exhausted name space warns without writing anything.

12. The --run CLI path: RunModeIntegrationTest runs main() with --run MODE
    against a temp dir (SCRIPT_DIR redirected, network mocked). Modes 2-4 use
    the *real* random_usernames() generator and the tests verify the produced
    names match the mode's charset/length, are unique, and every one reaches
    the checker; mode 1 reads a seeded temp list.txt end-to-end. --count
    overrides the configured default_random_count.

13. The --selftest CLI path: SelftestIntegrationTest runs main() with
    --selftest and a mocked requests.Session().post, verifying the raw
    request output - the HTTP status/timing line, the response-body echo, and
    the per-status conclusion (200 available/taken, 400 reachable, 429 rate
    limited, 401/403 blocked) plus the timeout / connection-error paths - and
    pins the request shape (URL, test-name payload, timeout tuple).

14. The --test-proxies CLI path: TestProxiesIntegrationTest runs main() with
    --test-proxies against a temp proxy.txt and DiscordChecker.check mocked
    per proxy, verifying the per-proxy verdicts (OK / OK but BLOCKED / DEAD),
    the keep/dead/blocked summary, the proxy_backup.txt safety copy, and the
    rewrite of proxy.txt keeping only healthy proxies (auth lines preserved) -
    while all-healthy and all-dead runs leave the file untouched.

Run with:  python -m unittest test_discord_checker -v
"""

import io
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import discord_checker as dc

# Matches colorama escape sequences so summary lines can be asserted on
# their visible text (e.g. the numbers in the proxy health table).
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def make_engine(**overrides):
    """Build a CheckerEngine with the default config plus `overrides`."""
    config = dict(dc.DEFAULT_CONFIG)
    # Tests assert on the visible proxy health table, so opt out of the
    # production hide_proxies default unless a test overrides it.
    config["hide_proxies"] = False
    config.update(overrides)
    # proxy_manager / checker / notifier / rate_limiter are stubbed: the tests
    # below replace or configure engine.pm / engine.checker as needed.
    return dc.CheckerEngine(config, Mock(), Mock(), Mock(), Mock())


class AllCoolingPool:
    """
    ProxyManager stand-in where every proxy is always cooling down.
    Set `return_proxy` to a Proxy to simulate the pool recovering.
    """

    def __init__(self, count=3):
        self._count = count
        self.return_proxy = None

    def acquire_proxy(self):
        return self.return_proxy

    def total_count(self):
        return self._count

    def live_count(self):
        return 0

    def earliest_ready_at(self):
        return time.monotonic() + 0.01


class FakeTime:
    """
    Stand-in for the time module as imported by discord_checker (dc.time).

    monotonic() jumps forward 1 second per call so 5-second heartbeat checks
    trigger after a handful of calls; sleep() is a no-op so tests run fast.
    The real time module (used by queue / threading / this test file) is
    untouched.
    """

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 1.0
        return self.now

    def sleep(self, seconds):
        pass


class CoolingMessageTest(unittest.TestCase):
    """CheckerEngine._acquire_proxy's once-per-transition message."""

    def _run_acquire(self, engine, wait_seconds=0.3, threads=1):
        """Run _acquire_proxy (possibly concurrently) and capture log lines.

        Each worker computes its own deadline, so slow thread startup can't
        push every worker past the deadline before it ever enters the loop.
        Returns (cooling_lines, worker_return_values).
        """
        calls = []
        results = [None] * threads

        def worker(i):
            results[i] = engine._acquire_proxy(time.monotonic() + wait_seconds)

        with patch.object(dc, "log_dim", side_effect=calls.append):
            workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=15)
        return [c for c in calls if "all proxies cooling" in str(c)], results

    def test_once_per_transition_across_workers(self):
        """8 workers hitting a dead pool -> exactly one message, all return None."""
        engine = make_engine(verbose_results=True, verbose_proxy_waiting=True)
        engine.pm = AllCoolingPool()
        cooling_lines, results = self._run_acquire(engine, threads=8)
        self.assertEqual(len(cooling_lines), 1)
        self.assertTrue(all(r is None for r in results))

    def test_silent_when_verbose_proxy_waiting_off(self):
        engine = make_engine(verbose_results=True, verbose_proxy_waiting=False)
        engine.pm = AllCoolingPool()
        cooling_lines, _ = self._run_acquire(engine)
        self.assertEqual(cooling_lines, [])

    def test_silent_when_quiet(self):
        """--quiet (verbose_results=False) silences it even with the flag on."""
        engine = make_engine(verbose_results=False, verbose_proxy_waiting=True)
        engine.pm = AllCoolingPool()
        cooling_lines, _ = self._run_acquire(engine)
        self.assertEqual(cooling_lines, [])

    def test_direct_mode_never_prints(self):
        """No proxies at all (direct mode) -> return None, no message."""
        engine = make_engine(verbose_results=True, verbose_proxy_waiting=True)
        engine.pm = AllCoolingPool(count=0)
        with patch.object(dc, "log_dim", side_effect=lambda m: self.fail(
                "expected no output in direct mode, got: %r" % (m,))):
            result = engine._acquire_proxy(time.monotonic() + 0.05)
        self.assertIsNone(result)

    def test_reports_again_after_pool_recovery(self):
        """Dead -> recovers -> dead again prints exactly twice total."""
        engine = make_engine(verbose_results=True, verbose_proxy_waiting=True)
        pool = AllCoolingPool()
        engine.pm = pool
        captured = []
        with patch.object(dc, "log_dim", side_effect=captured.append):
            # Pool dead: one message.
            engine._acquire_proxy(time.monotonic() + 0.3)
            dead1 = sum("all proxies cooling" in str(c) for c in captured)
            self.assertEqual(dead1, 1)
            # Pool recovers: a worker acquires a proxy and resets the flag.
            pool.return_proxy = dc.Proxy("", "", "203.0.113.10", "3128")
            proxy = engine._acquire_proxy(time.monotonic() + 1.0)
            self.assertIsNotNone(proxy)
            self.assertFalse(engine._pool_cooling_reported)
            # Pool dead again: exactly one more message.
            pool.return_proxy = None
            engine._acquire_proxy(time.monotonic() + 0.3)
        total = sum("all proxies cooling" in str(c) for c in captured)
        self.assertEqual(total, 2)


class RateLimitLoggingTest(unittest.TestCase):
    """CheckerEngine._process's gating of per-name 429 log lines."""

    def _process_with_429(self, retry=(1.0, False), **overrides):
        """Run _process against a checker that always answers HTTP 429."""
        overrides.setdefault("verbose_results", False)
        engine = make_engine(**overrides)
        engine.pm.acquire_proxy.return_value = dc.Proxy("u", "p", "203.0.113.10", "3128")
        engine.checker.check.return_value = (dc.ST_RATE_LIMITED, retry)
        rate_calls, dim_calls = [], []
        with patch.object(dc, "log_rate_limit", side_effect=rate_calls.append), \
             patch.object(dc, "log_dim", side_effect=dim_calls.append):
            engine._process("abcd")
        return engine, rate_calls, dim_calls

    def test_non_global_429_silent_by_default(self):
        """verbose_rate_limits off (default): per-name 429 lines not printed."""
        engine, rate_calls, _ = self._process_with_429()
        self.assertEqual(rate_calls, [])
        self.assertEqual(engine._stats[dc.ST_RATE_LIMITED], 3)  # still counted

    def test_non_global_429_prints_when_enabled(self):
        _, rate_calls, _ = self._process_with_429(verbose_rate_limits=True)
        self.assertEqual(len(rate_calls), 3)  # one per retry attempt
        self.assertTrue(all("Rate limited on" in str(c) for c in rate_calls))

    def test_global_429_always_prints(self):
        """A global 429 must print even with verbose_rate_limits off."""
        _, rate_calls, _ = self._process_with_429(retry=(1.0, True))
        self.assertEqual(len(rate_calls), 3)
        self.assertTrue(all("GLOBAL rate limit" in str(c) for c in rate_calls))


class RetryLineTest(unittest.TestCase):
    """The dim network / proxy-error retry lines in _process."""

    def _process_with_network_status(self, status, detail, **overrides):
        """Run _process against a checker that always returns a network status."""
        overrides.setdefault("verbose_results", True)
        engine = make_engine(**overrides)
        engine.pm.acquire_proxy.return_value = dc.Proxy("u", "p", "203.0.113.10", "3128")
        engine.checker.check.return_value = (status, detail)
        dim_calls = []
        with patch.object(dc, "log_dim", side_effect=dim_calls.append), \
             patch.object(dc.time, "sleep"):          # skip backoff sleeps (patches the real time module process-wide - fine, serial unittest)
            engine._process("abcd")
        return engine, dim_calls

    @staticmethod
    def _retry_lines(calls):
        return [c for c in calls
                if "- retry 1/3" in str(c) or "- retry 2/3" in str(c)
                or "- retry 3/3" in str(c)]

    def test_retry_lines_printed_when_verbose(self):
        """One dim retry line per attempt (1/3, 2/3, 3/3) for both statuses."""
        for status, detail in ((dc.ST_PROXY_ERROR, "proxy error: boom"),
                               (dc.ST_NETWORK, "connection error: boom")):
            with self.subTest(status=status):
                engine, dim_calls = self._process_with_network_status(status, detail)
                self.assertEqual(len(self._retry_lines(dim_calls)), 3)
                self.assertEqual(engine.pm.report_failure.call_count, 3)

    def test_retry_lines_silent_when_quiet(self):
        """--quiet (verbose_results=False) hides the retry lines."""
        engine, dim_calls = self._process_with_network_status(
            dc.ST_PROXY_ERROR, "proxy error: boom", verbose_results=False)
        self.assertEqual(self._retry_lines(dim_calls), [])
        self.assertEqual(engine._stats[dc.ST_UNKNOWN], 1)  # gave up after 3 retries


class BlockedLoggingTest(unittest.TestCase):
    """The "Blocked ... - swapping proxy" warning in _process (401/403)."""

    def test_blocked_warns_and_counts(self):
        engine = make_engine(verbose_results=False)
        engine.pm.acquire_proxy.return_value = dc.Proxy("u", "p", "203.0.113.10", "3128")
        engine.checker.check.return_value = (dc.ST_BLOCKED, "403: blocked")
        warn_calls = []
        with patch.object(dc, "log_warn", side_effect=warn_calls.append), \
             patch.object(dc.time, "sleep"):          # skip backoff sleeps (patches the real time module process-wide - fine, serial unittest)
            engine._process("abcd")
        blocked = [c for c in warn_calls
                   if "Blocked" in str(c) and "swapping proxy" in str(c)]
        self.assertEqual(len(blocked), 3)              # one per retry attempt
        self.assertEqual(engine._stats[dc.ST_BLOCKED], 3)


class FakePoolWithProxies:
    """ProxyManager stand-in exposing a fixed proxy list for summary tests."""

    def __init__(self, proxies):
        self._proxies = list(proxies)

    def proxies(self):
        return list(self._proxies)

    def live_count(self):
        return sum(1 for p in self._proxies if not p.banned)

    def total_count(self):
        return len(self._proxies)

    def banned_count(self):
        return sum(1 for p in self._proxies if p.banned)


class SummaryOutputTest(unittest.TestCase):
    """CheckerEngine._print_summary: totals block + per-proxy health table."""

    @staticmethod
    def _strip_ansi(text):
        return ANSI_RE.sub("", text)

    @staticmethod
    def _proxy(host, **attrs):
        """A Proxy with explicitly settable per-run health counters."""
        proxy = dc.Proxy("", "", host, "3128")
        proxy.uses = 0
        proxy.rate_limits = 0
        proxy.cooled = 0
        proxy.bans = 0
        proxy.banned = False
        for key, value in attrs.items():
            setattr(proxy, key, value)
        return proxy

    def _summary_lines(self, proxies, results=100, stats=None, **notifier_attrs):
        """Run _print_summary and return the emitted lines without ANSI codes."""
        engine = make_engine()
        engine.pm = FakePoolWithProxies(proxies)
        engine._results = results
        with engine._stats_lock:
            engine._stats.update(stats or {})
        engine.notifier = Mock(enabled=False)
        for key, value in notifier_attrs.items():
            setattr(engine.notifier, key, value)
        emitted = []
        # time.monotonic is patched (synchronous, nothing else calls it here)
        # so elapsed is exactly 10.0s and the rate line is stable.
        with patch.object(dc, "_emit", side_effect=emitted.append), \
             patch.object(dc.time, "monotonic", return_value=1000.0):
            engine._print_summary(990.0)
        return [self._strip_ansi(line) for line in emitted]

    def test_totals_and_proxy_health_table(self):
        """All totals, the webhook line, and the per-proxy row render correctly."""
        proxy = self._proxy("203.0.113.10", uses=100, rate_limits=7, cooled=7, bans=0)
        lines = self._summary_lines(
            [proxy],
            results=100,
            stats={dc.ST_AVAILABLE: 1, dc.ST_TAKEN: 80, dc.ST_INVALID: 5,
                   dc.ST_RATE_LIMITED: 7, dc.ST_BLOCKED: 3, dc.ST_UNKNOWN: 4},
            enabled=True, posted=5, failed=1,
        )
        joined = "\n".join(lines)
        # Replicate the summary's own f-string formatting so spacing matches.
        self.assertIn(f"  {'Total checked:':<20}100   (10.0/s in 10.0s)", joined)
        self.assertIn(f"  {'Available:':<20}1", joined)
        self.assertIn(f"  {'Taken:':<20}80", joined)
        self.assertIn(f"  {'Invalid:':<20}5", joined)
        self.assertIn(f"  {'Rate limited:':<20}7", joined)
        self.assertIn(f"  {'Blocked:':<20}3", joined)
        self.assertIn(f"  {'Errors/gave up:':<20}4", joined)
        self.assertIn(f"  {'Proxies live:':<20}1/1 (0 banned)", joined)
        self.assertIn(f"  {'Webhook posted:':<20}5 (1 failed)", joined)
        self.assertIn("Saved to:", joined)
        self.assertIn("Proxy health (this run):", joined)
        self.assertRegex(joined, r"203\.0\.113\.10:3128\s+100\s+7\s+7\s+0")
        self.assertRegex(joined, r"TOTAL\s+100\s+7\s+7\s+0")

    def test_banned_proxy_row_and_totals(self):
        """Banned proxies render a BANNED state and the totals sum all rows."""
        banned = self._proxy("198.51.100.1", uses=50, rate_limits=0, cooled=0,
                             bans=1, banned=True)
        healthy = self._proxy("198.51.100.2", uses=100, rate_limits=10, cooled=5,
                              bans=0)
        lines = self._summary_lines([banned, healthy], results=150)
        joined = "\n".join(lines)
        self.assertIn(f"  {'Proxies live:':<20}1/2 (1 banned)", joined)
        self.assertRegex(joined, r"198\.51\.100\.2:3128\s+100\s+10\s+5\s+0")
        self.assertRegex(joined, r"198\.51\.100\.1:3128\s+50\s+0\s+0\s+BANNED")
        self.assertRegex(joined, r"TOTAL\s+150\s+10\s+5\s+1")

    def test_proxy_summary_limit_truncates_rows(self):
        """proxy_summary_limit shows only the top N proxies and notes the rest."""
        proxies = [self._proxy("203.0.113.%d" % i, uses=10 - i) for i in range(3)]
        engine = make_engine(proxy_summary_limit=1)
        engine.pm = FakePoolWithProxies(proxies)
        engine._results = 30
        emitted = []
        with patch.object(dc, "_emit", side_effect=emitted.append), \
             patch.object(dc.time, "monotonic", return_value=1000.0):
            engine._print_summary(990.0)
        joined = "\n".join(self._strip_ansi(l) for l in emitted)
        self.assertIn("2 more proxy/proxies", joined)
        self.assertIn("(proxy_summary_limit=1)", joined)
        self.assertIn("203.0.113.0:3128", joined)     # the top proxy is shown
        self.assertNotIn("203.0.113.2:3128", joined)  # the rest are hidden

    def test_no_proxy_table_without_proxies(self):
        """Direct mode (no proxies) skips the health table entirely."""
        lines = self._summary_lines([])
        joined = "\n".join(lines)
        self.assertIn(f"  {'Proxies live:':<20}0/0 (0 banned)", joined)
        self.assertNotIn("Proxy health (this run):", joined)

    def test_health_table_column_header(self):
        """The table prints its column-header row right before the proxy rows."""
        lines = self._summary_lines([self._proxy("203.0.113.10", uses=1)])
        joined = "\n".join(lines)
        header = f"  {'proxy':<30}{'used':>7}{'429s':>7}{'cooled':>8}{'banned':>9}"
        self.assertIn(header, joined)
        # The header must precede the first proxy row.
        self.assertLess(joined.index(header), joined.index("203.0.113.10:3128"))

    def test_long_proxy_label_is_truncated(self):
        """Labels longer than 29 chars are cut to 27 chars + '...'."""
        full_label = "very_long_username_string@198.51.100.1:3128"   # 42 chars, > 29
        proxy = self._proxy("198.51.100.1", uses=5)
        proxy.label = full_label
        lines = self._summary_lines([proxy])
        joined = "\n".join(lines)
        self.assertIn(full_label[:27] + "...", joined)
        self.assertNotIn(full_label, joined)      # the untruncated label is gone


class HeartbeatTest(unittest.TestCase):
    """The 5-second heartbeat lines in run() (filling queue / working...)."""

    @staticmethod
    def _slow_check(name, proxy):
        """Check that takes real time, so workers lag behind the producer."""
        time.sleep(0.1)
        return dc.ST_TAKEN, "taken"

    def _run_with_heartbeat(self, **overrides):
        """Run 30 names through run() with a fake clock; return emitted lines."""
        overrides.setdefault("verbose_results", True)
        overrides.setdefault("concurrency", 2)        # keep workers slower than the producer
        engine = make_engine(**overrides)
        engine.pm.acquire_proxy.return_value = dc.Proxy("u", "p", "203.0.113.10", "3128")
        engine.pm.live_count.return_value = 1
        engine.pm.total_count.return_value = 1
        engine.pm.banned_count.return_value = 0
        engine.pm.proxies.return_value = []           # keeps the run summary simple
        engine.checker.check.side_effect = self._slow_check
        emitted = []
        with patch.object(dc, "_emit", side_effect=emitted.append), \
             patch.object(dc, "time", FakeTime()):
            engine.run(["name%04d" % i for i in range(30)])
        return emitted, engine

    @staticmethod
    def _heartbeat_lines(lines):
        return [l for l in lines
                if "filling queue..." in l or "working..." in l]

    def test_heartbeat_printed_when_verbose(self):
        """Both heartbeat flavors appear on a normal verbose run."""
        lines, engine = self._run_with_heartbeat()
        self.assertTrue(any("filling queue..." in l for l in lines))
        self.assertTrue(any("working..." in l for l in lines))
        self.assertEqual(engine._results, 30)          # run actually completed

    def test_heartbeat_silent_when_quiet(self):
        """--quiet (verbose_results=False) suppresses both heartbeats."""
        lines, engine = self._run_with_heartbeat(verbose_results=False)
        self.assertEqual(self._heartbeat_lines(lines), [])
        self.assertEqual(engine._results, 30)          # run actually completed


class QuietIntegrationTest(unittest.TestCase):
    """End-to-end: invoke the CLI's main() with --quiet, network mocked."""

    def _run_cli(self, name, status, detail, check_side_effect=None):
        """
        Run main() as `python discord_checker.py --check NAME --quiet ...`.

        Returns (output_without_ansi, tmpdir, check_mock). The checker's
        network call is replaced by `check_mock` and SCRIPT_DIR is redirected
        into a temp dir, so the real project files (config.json /
        available.txt) are never touched. Real sleeps are stubbed so slow
        backoff paths can't stall the run.
        """
        tmpdir = tempfile.mkdtemp(prefix="dc_test_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        config_path = os.path.join(tmpdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({
                "webhook_url": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
                "enable_webhook": True,       # stays disabled: placeholder URL
                "use_proxies": False,
                "concurrency": 4,
            }, fh)

        check_mock = Mock(return_value=(status, detail))
        if check_side_effect is not None:
            check_mock.side_effect = check_side_effect

        captured = io.StringIO()
        real_stdout = sys.stdout
        old_argv = sys.argv
        sys.argv = ["discord_checker.py", "--check", name, "--quiet",
                    "--config", config_path]
        try:
            with patch.object(dc, "SCRIPT_DIR", tmpdir), \
                 patch.object(dc.DiscordChecker, "check", check_mock), \
                 patch.object(dc.time, "sleep"):       # no real backoff waits
                sys.stdout = captured
                try:
                    dc.main()
                finally:
                    sys.stdout = real_stdout
        finally:
            sys.argv = old_argv
        return ANSI_RE.sub("", captured.getvalue()), tmpdir, check_mock

    def test_quiet_taken_prints_only_stats(self):
        """A taken name prints no result line under --quiet, only the summary."""
        out, _, check_mock = self._run_cli("testname", dc.ST_TAKEN, "taken")
        check_mock.assert_called_once()
        self.assertIn("Summary", out)
        self.assertIn(f"  {'Taken:':<20}1", out)
        self.assertNotIn("TAKEN", out)          # no per-result line
        self.assertNotIn("filling queue...", out)
        self.assertNotIn("working...", out)
        self.assertNotIn("progress:", out)
        self.assertNotIn("[429]", out)

    def test_quiet_available_still_prints_and_saves(self):
        """Available names still print and are appended to available.txt."""
        out, tmpdir, check_mock = self._run_cli("coolname", dc.ST_AVAILABLE, "available")
        check_mock.assert_called_once()
        self.assertIn("AVAILABLE", out)
        self.assertIn(f"  {'Available:':<20}1", out)
        self.assertNotIn("TAKEN", out)
        self.assertNotIn("filling queue...", out)
        avail_path = os.path.join(tmpdir, "available.txt")
        with open(avail_path, encoding="utf-8") as fh:
            self.assertIn("coolname\n", fh.read())

    def test_quiet_invalid_name_prints_only_stats(self):
        """A too-short name is rejected locally (no network) and only counted."""
        def _never_called(name, proxy):
            raise AssertionError("check() must not be called for an invalid name")

        out, _, check_mock = self._run_cli("x", None, None,
                                           check_side_effect=_never_called)
        check_mock.assert_not_called()
        self.assertIn(f"  {'Invalid:':<20}1", out)
        self.assertNotIn("INVALID", out)       # no dim result line

    def test_quiet_errors_still_print(self):
        """Warn/error lines still print under --quiet; result/heartbeat lines don't."""
        out, _, check_mock = self._run_cli("errname", dc.ST_UNKNOWN,
                                           "500 server error: xyz")
        check_mock.assert_called()              # one per retry attempt
        self.assertIn("[!]", out)              # errors still print
        self.assertIn("- retry 1/3", out)
        # ST_UNKNOWN is bumped per retry attempt (3) + the final gave-up (1).
        self.assertIn(f"  {'Errors/gave up:':<20}4", out)
        self.assertNotIn("TAKEN", out)
        self.assertNotIn("filling queue...", out)
        self.assertNotIn("working...", out)


class WebhookNotifierTest(unittest.TestCase):
    """WebhookNotifier._post and the queue/thread lifecycle, requests mocked."""

    @staticmethod
    def _notifier_config(**overrides):
        """A config with a real-looking webhook so the notifier is enabled."""
        config = dict(dc.DEFAULT_CONFIG)
        config.update({
            "webhook_url": "https://discord.com/api/webhooks/123456789/abcdef",
            "enable_webhook": True,
            "webhook_retries": 3,
            "webhook_timeout_seconds": 5,
        })
        config.update(overrides)
        return config

    def _run_post(self, username="aval1", **config_overrides):
        """Build a notifier and run its _post synchronously; return (notifier, lines)."""
        emitted = []
        with patch.object(dc, "_emit", side_effect=emitted.append), \
             patch.object(dc.time, "sleep"):   # skip the 1.5/3/4.5s backoff sleeps
            notifier = dc.WebhookNotifier(self._notifier_config(**config_overrides))
            notifier._post(username)
        return notifier, [ANSI_RE.sub("", l) for l in emitted]

    def test_webhook_success_posts_once(self):
        """A 200 response posts the name once with the right request shape."""
        post_mock = Mock(return_value=Mock(status_code=200))
        with patch.object(dc.requests, "post", post_mock):
            notifier, lines = self._run_post("aval1")
        self.assertEqual(notifier.posted, 1)
        self.assertEqual(notifier.failed, 0)
        self.assertIn("[+] Webhook posted: aval1", lines)
        post_mock.assert_called_once()
        url, kwargs = post_mock.call_args
        self.assertEqual(url, ("https://discord.com/api/webhooks/123456789/abcdef",))
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["json"]["embeds"][0]["title"], 'discord "aval1" (available)')

    def test_webhook_http_error_retries_then_fails(self):
        """Persistent HTTP errors retry webhook_retries times, then fail."""
        with patch.object(dc.requests, "post", return_value=Mock(status_code=500)):
            notifier, lines = self._run_post("aval2")
        self.assertEqual(notifier.failed, 1)
        self.assertEqual(notifier.posted, 0)
        joined = "\n".join(lines)
        for attempt in (1, 2, 3):
            self.assertIn(f"[!] Webhook HTTP 500 for aval2 (attempt {attempt}/3)", joined)
        self.assertIn("[x] Webhook FAILED for aval2 after 3 attempts", joined)

    def test_webhook_exception_retries_then_fails(self):
        """A transport exception retries each attempt, then fails."""
        exc = dc.requests.exceptions.ConnectionError("boom")
        with patch.object(dc.requests, "post", side_effect=exc):
            notifier, lines = self._run_post("aval3")
        self.assertEqual(notifier.failed, 1)
        self.assertEqual(notifier.posted, 0)
        joined = "\n".join(lines)
        for attempt in (1, 2, 3):
            self.assertIn(f"[!] Webhook error for aval3: boom (attempt {attempt}/3)", joined)
        self.assertIn("[x] Webhook FAILED for aval3 after 3 attempts", joined)

    def test_webhook_backoff_sleeps_increase_by_1_5x(self):
        """Between retries the notifier sleeps attempt*1.5: 1.5, 3.0, 4.5s."""
        sleeps = []
        post_mock = Mock(return_value=Mock(status_code=500))
        with patch.object(dc.requests, "post", post_mock), \
             patch.object(dc, "_emit"), \
             patch.object(dc.time, "sleep", side_effect=sleeps.append):
            notifier = dc.WebhookNotifier(self._notifier_config(webhook_retries=3))
            notifier._post("aval4")
        self.assertEqual(notifier.failed, 1)
        self.assertEqual(post_mock.call_count, 3)      # one attempt per retry
        self.assertEqual(len(sleeps), 3)               # one backoff per attempt
        for got, want in zip(sleeps, [1.5, 3.0, 4.5]): # attempt * 1.5
            self.assertAlmostEqual(got, want)

    def test_webhook_success_does_not_backoff(self):
        """A successful post returns before any backoff sleep."""
        sleeps = []
        with patch.object(dc.requests, "post", return_value=Mock(status_code=200)), \
             patch.object(dc, "_emit"), \
             patch.object(dc.time, "sleep", side_effect=sleeps.append):
            notifier = dc.WebhookNotifier(self._notifier_config(webhook_retries=3))
            notifier._post("aval5")
        self.assertEqual(notifier.posted, 1)
        self.assertEqual(notifier.failed, 0)
        self.assertEqual(sleeps, [])

    def test_webhook_thread_lifecycle(self):
        """enqueue -> background thread posts -> stop drains and exits cleanly."""
        post_mock = Mock(return_value=Mock(status_code=204))
        emitted = []
        notifier = None
        try:
            with patch.object(dc, "_emit", side_effect=emitted.append), \
                 patch.object(dc.requests, "post", post_mock), \
                 patch.object(dc.time, "sleep"):
                notifier = dc.WebhookNotifier(self._notifier_config())
                notifier.start()
                notifier.enqueue("nameone")
                notifier.enqueue("nametwo")
                # Busy-wait (deliberately not time.sleep: dc.time.sleep is
                # stubbed to a no-op inside this context) on the real clock
                # until the thread has posted both names.
                deadline = time.monotonic() + 5.0
                while notifier.posted < 2 and time.monotonic() < deadline:
                    pass
                # Stop inside the patch context so the thread can never call
                # the real (unmocked) requests.post/_emit after restoration.
                notifier.stop()
        finally:
            if notifier is not None:
                notifier.stop()   # no-op if already joined; guards early failures
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(notifier.posted, 2)
        self.assertEqual(notifier.failed, 0)
        lines = [ANSI_RE.sub("", l) for l in emitted]
        self.assertTrue(any("Webhook posted: nameone" in l for l in lines))
        self.assertTrue(any("Webhook posted: nametwo" in l for l in lines))


class InteractiveMenuTest(unittest.TestCase):
    """The interactive menu in main(): mode selection + prompt handling."""

    def _run_menu(self, responses, mode_names=None, run_side_effect=None):
        """
        Run main()'s interactive menu, feeding `responses` to input().

        build_engine is stubbed (no real workers/webhook threads), the name
        builder and list generator record their args and return canned values,
        and emitted console lines are captured. Returns (engine, mode_calls,
        gen_calls, lines). Pass an exception instance (e.g. EOFError() or
        KeyboardInterrupt()) to simulate input() raising on every call; pass
        `run_side_effect` to make the stubbed engine.run raise (e.g. an
        interrupt surfacing from inside a run).
        """
        config = dict(dc.DEFAULT_CONFIG)
        config.update({"use_proxies": False, "default_random_count": 100})

        engine = Mock()
        engine.run = Mock(side_effect=run_side_effect)
        engine.stop_event = Mock()
        engine.notifier = Mock()

        mode_calls = []
        gen_calls = []

        def fake_build_names(mode, cfg, count):
            mode_calls.append((mode, cfg, count))
            return (mode_names or {}).get(mode, ["name%d" % mode])

        def fake_generate_list_file(count, length, alphabet, path):
            gen_calls.append((count, length, alphabet, path))
            return 5, 10

        emitted = []
        old_argv = sys.argv
        sys.argv = ["discord_checker.py"]
        try:
            with patch.object(dc, "load_config", return_value=config), \
                 patch.object(dc, "build_engine", return_value=engine), \
                 patch.object(dc, "build_names_for_mode", side_effect=fake_build_names), \
                 patch.object(dc, "generate_list_file", side_effect=fake_generate_list_file), \
                 patch.object(dc, "_emit", side_effect=emitted.append), \
                 patch("builtins.input", side_effect=responses):
                dc.main()
        finally:
            sys.argv = old_argv
        return engine, mode_calls, gen_calls, [ANSI_RE.sub("", l) for l in emitted]

    def test_mode1_loads_list_and_runs(self):
        """Mode 1 runs list.txt without a count prompt."""
        engine, mode_calls, _, _ = self._run_menu(["1", "0"], mode_names={1: ["aa", "bb"]})
        self.assertEqual(len(mode_calls), 1)
        self.assertEqual(mode_calls[0][0], 1)
        self.assertEqual(mode_calls[0][2], 100)          # default count, no prompt
        engine.run.assert_called_once_with(["aa", "bb"])

    def test_random_modes_prompt_count_and_run(self):
        """Modes 2-4 ask for a count, then build names and run."""
        for mode, names in ((2, ["aaaa", "bbbb"]), (3, ["cccc"]), (4, ["dddd"])):
            with self.subTest(mode=mode):
                engine, mode_calls, _, _ = self._run_menu(
                    [str(mode), "7", "0"], mode_names={mode: names})
                self.assertEqual([(m, c) for m, _, c in mode_calls], [(mode, 7)])
                engine.run.assert_called_once_with(names)

    def test_invalid_choice_reprompts(self):
        """An invalid option is rejected and the menu keeps waiting."""
        engine, mode_calls, _, lines = self._run_menu(["x", "9", "0"])
        self.assertEqual(mode_calls, [])
        engine.run.assert_not_called()
        self.assertEqual(lines.count("[x] Invalid option."), 2)

    def test_bad_count_reprompts_until_valid(self):
        """prompt_int rejects non-numbers and out-of-range values."""
        engine, mode_calls, _, lines = self._run_menu(["2", "abc", "10000001", "10", "0"])
        self.assertEqual([(m, c) for m, _, c in mode_calls], [(2, 10)])
        joined = "\n".join(lines)
        self.assertIn("[x] Please enter a number.", joined)
        self.assertIn("[x] Please enter a number between 1 and 10000000.", joined)

    def test_mode5_generates_list(self):
        """Mode 5 asks count/length/alphabet and writes via generate_list_file."""
        engine, mode_calls, gen_calls, lines = self._run_menu(["5", "3", "6", "2", "0"])
        self.assertEqual(mode_calls, [])                # no checking happens
        engine.run.assert_not_called()
        self.assertEqual(len(gen_calls), 1)
        count, length, alphabet, path = gen_calls[0]
        self.assertEqual((count, length), (3, 6))
        self.assertIs(alphabet, dc.ALPHABET_LETTERS)    # alphabet choice "2"
        self.assertTrue(path.endswith("list.txt"))
        self.assertIn("Generated 5 new username(s). 10 total entries", "\n".join(lines))

    def test_mode5_default_alphabet_uses_all(self):
        """A blank alphabet choice falls back to letters+digits+underscore."""
        engine, mode_calls, gen_calls, _ = self._run_menu(["5", "2", "4", "", "0"])
        self.assertEqual(mode_calls, [])
        self.assertEqual(len(gen_calls), 1)
        self.assertIs(gen_calls[0][2], dc.ALPHABET_ALL)

    def test_menu_is_rendered(self):
        """The menu text is shown before any choice is read."""
        engine, mode_calls, _, lines = self._run_menu(["0"])
        self.assertIn("[0]  Quit", "\n".join(lines))
        self.assertEqual(mode_calls, [])

    def test_eof_and_blank_quit_cleanly(self):
        """EOF (Ctrl+D) and an empty choice both exit without running."""
        for responses in (EOFError(), [""]):
            with self.subTest(responses=responses):
                engine, mode_calls, _, lines = self._run_menu(responses)
                self.assertEqual(mode_calls, [])
                engine.run.assert_not_called()
                self.assertIn("Goodbye", "\n".join(lines))

    def test_keyboard_interrupt_shuts_down_gracefully(self):
        """Ctrl+C at the menu prompt warns, stops the engine, and says goodbye."""
        with patch("builtins.print"):        # main's handler prints a blank line
            engine, mode_calls, _, lines = self._run_menu(KeyboardInterrupt())
        self.assertEqual(mode_calls, [])     # no mode was ever chosen
        engine.run.assert_not_called()
        engine.stop_event.set.assert_called_once()   # graceful shutdown
        engine.notifier.stop.assert_called_once()
        joined = "\n".join(lines)
        self.assertIn("[!] KeyboardInterrupt received - shutting down gracefully...",
                      joined)
        self.assertIn("Goodbye. Thanks for using the checker!", joined)

    def test_keyboard_interrupt_during_run_stops_engine(self):
        """An interrupt surfacing from engine.run is caught by main() too."""
        # Note: the real CheckerEngine.run() swallows its own KeyboardInterrupt
        # (sets the stop flag and drains), so an interrupt escaping run() into
        # main() is hypothetical with the current engine - this test exercises
        # main()'s *outer* handler defensively, whatever the source.
        with patch("builtins.print"):
            engine, mode_calls, _, lines = self._run_menu(
                ["1"], mode_names={1: ["aa"]},
                run_side_effect=KeyboardInterrupt())
        self.assertEqual([m for m, _, _ in mode_calls], [1])   # mode 1 was chosen
        engine.run.assert_called_once_with(["aa"])            # the run was attempted
        engine.stop_event.set.assert_called_once()
        engine.notifier.stop.assert_called_once()
        joined = "\n".join(lines)
        self.assertIn("[!] KeyboardInterrupt received - shutting down gracefully...",
                      joined)
        self.assertIn("Goodbye. Thanks for using the checker!", joined)


class ProxyManagerHealthTest(unittest.TestCase):
    """ProxyManager: rotation, cooldown/ban transitions, and load()."""

    def _manager(self, **overrides):
        """A ProxyManager with explicit health thresholds."""
        config = dict(dc.DEFAULT_CONFIG)
        config.update({
            "proxy_failure_threshold": 3,
            "proxy_cooldown_seconds": 30,
            "proxy_ban_after_failures": 10,
            "proxy_wait_timeout_seconds": 10,
        })
        config.update(overrides)
        return dc.ProxyManager(config)

    def _manager_with(self, lines, **overrides):
        """Build a manager and load proxies from a temp file; return (pm, path, loaded)."""
        pm = self._manager(**overrides)
        tmpdir = tempfile.mkdtemp(prefix="dc_px_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "proxy.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        with patch.object(dc, "log_warn"), patch.object(dc, "log_dim"), \
             patch.object(dc, "log_ok"):          # silence load()'s console chatter
            loaded = pm.load(path, force=True)
        return pm, loaded

    def test_round_robin_rotation(self):
        """get_next_proxy cycles through all proxies in order."""
        pm, loaded = self._manager_with([
            "203.0.113.1:3128", "203.0.113.2:3128", "203.0.113.3:3128",
        ])
        self.assertEqual(loaded, 3)
        labels = [p.label for p in pm.proxies()]
        order = [pm.get_next_proxy().label for _ in range(6)]
        self.assertEqual(order, labels * 2)

    def test_rotation_skips_cooling_proxy(self):
        """A cooling proxy is skipped; rotation alternates the healthy ones."""
        pm, _ = self._manager_with([
            "198.51.100.1:3128", "198.51.100.2:3128", "198.51.100.3:3128",
        ])
        proxies = pm.proxies()
        with patch.object(dc, "log_warn"):
            for _ in range(3):                   # threshold reached -> cooled
                pm.report_failure(proxies[1], "timeout")
        self.assertTrue(proxies[1].cooled == 1)
        seen = [pm.get_next_proxy().label for _ in range(6)]
        self.assertNotIn(proxies[1].label, seen)
        self.assertEqual(set(seen), {proxies[0].label, proxies[2].label})

    def test_failures_below_threshold_no_cooldown(self):
        """Failures under the threshold don't cool the proxy or log anything."""
        pm, _ = self._manager_with(["198.51.100.1:3128"])
        proxy = pm.proxies()[0]
        warns = []
        with patch.object(dc, "log_warn", side_effect=warns.append):
            pm.report_failure(proxy, "timeout")
            pm.report_failure(proxy, "timeout")
        self.assertEqual(warns, [])
        self.assertEqual(proxy.consecutive_failures, 2)
        self.assertEqual(proxy.cooled, 0)
        self.assertTrue(proxy.is_usable(time.monotonic()))

    def test_threshold_cools_down_proxy(self):
        """Reaching the threshold rests the proxy for proxy_cooldown_seconds."""
        pm, _ = self._manager_with(["198.51.100.1:3128"])
        proxy = pm.proxies()[0]
        warns = []
        with patch.object(dc, "log_warn", side_effect=warns.append), \
             patch.object(dc.time, "monotonic", return_value=1000.0):
            for _ in range(3):
                pm.report_failure(proxy, "timeout")
        self.assertEqual(proxy.cooled, 1)
        self.assertEqual(proxy.consecutive_failures, 3)
        self.assertAlmostEqual(proxy.cooldown_until, 1030.0)
        self.assertFalse(proxy.is_usable(1000.0))
        self.assertTrue(proxy.is_usable(1030.0))
        self.assertIn("cooled down", "\n".join(str(w) for w in warns))

    def test_cooled_proxy_returns_after_cooldown(self):
        """Once the cooldown expires the proxy is rotated in again."""
        pm, _ = self._manager_with(["198.51.100.1:3128", "198.51.100.2:3128"])
        p0, p1 = pm.proxies()
        clock = {"t": 1000.0}

        def fake_monotonic():
            clock["t"] += 1.0
            return clock["t"]

        with patch.object(dc, "log_warn"), \
             patch.object(dc.time, "monotonic", side_effect=fake_monotonic):
            for _ in range(3):
                pm.report_failure(p0, "timeout")   # p0 rests until ~1031
            self.assertEqual({pm.get_next_proxy().label for _ in range(6)}, {p1.label})
            clock["t"] = 1100.0                   # past p0's cooldown
            self.assertEqual(
                {pm.get_next_proxy().label for _ in range(6)}, {p0.label, p1.label})

    def test_success_resets_failure_streak(self):
        """A successful request clears a proxy's failure streak and cooldown."""
        pm, _ = self._manager_with(["198.51.100.1:3128"])
        proxy = pm.proxies()[0]
        pm.report_failure(proxy, "timeout")
        pm.report_failure(proxy, "timeout")
        pm.report_success(proxy)
        self.assertEqual(proxy.consecutive_failures, 0)
        self.assertEqual(proxy.cooldown_until, 0.0)
        self.assertTrue(proxy.is_usable(time.monotonic()))

    def test_ban_after_threshold_failures(self):
        """proxy_ban_after_failures consecutive failures ban the proxy forever."""
        pm, _ = self._manager_with(["198.51.100.1:3128"])
        proxy = pm.proxies()[0]
        warns = []
        with patch.object(dc, "log_warn", side_effect=warns.append):
            for _ in range(10):
                pm.report_failure(proxy, "timeout")
        self.assertTrue(proxy.banned)
        self.assertEqual(proxy.bans, 1)
        self.assertEqual(proxy.consecutive_failures, 10)
        self.assertIn("BANNED", "\n".join(str(w) for w in warns))
        # A fully-banned pool is unusable and never rotated.
        self.assertFalse(proxy.is_usable(time.monotonic()))
        self.assertIsNone(pm.get_next_proxy())
        self.assertEqual(pm.live_count(), 0)
        self.assertEqual(pm.banned_count(), 1)

    def test_rate_limit_rests_proxy_and_never_shortens(self):
        """report_rate_limited rests a proxy but a smaller 429 can't shorten it."""
        pm, _ = self._manager_with(["198.51.100.1:3128"])
        proxy = pm.proxies()[0]
        with patch.object(dc.time, "monotonic", return_value=1000.0):
            pm.report_rate_limited(proxy, 60)
            self.assertAlmostEqual(proxy.cooldown_until, 1060.0)
            pm.report_rate_limited(proxy, 10)     # must NOT shorten the rest
            self.assertAlmostEqual(proxy.cooldown_until, 1060.0)
        self.assertEqual(proxy.rate_limits, 2)
        self.assertEqual(proxy.cooled, 2)

    def test_earliest_ready_at(self):
        """earliest_ready_at returns the soonest moment the pool can serve."""
        pm, _ = self._manager_with(["198.51.100.1:3128", "198.51.100.2:3128"])
        p1, p2 = pm.proxies()
        with patch.object(dc.time, "monotonic", return_value=1000.0):
            pm.report_rate_limited(p1, 60)        # ready at 1060
            pm.report_rate_limited(p2, 10)        # ready at 1010
            self.assertAlmostEqual(pm.earliest_ready_at(), 1010.0)

    def test_acquire_proxy_direct_mode_returns_none(self):
        """With no proxies configured, acquire_proxy means direct mode (None)."""
        pm = self._manager()
        self.assertEqual(pm.total_count(), 0)
        self.assertIsNone(pm.acquire_proxy())

    def test_load_parses_and_dedups(self):
        """load() parses auth/plain/URL lines and dedups on (host, port)."""
        pm, loaded = self._manager_with([
            "# a comment",
            "user:pass@203.0.113.1:3128",
            "203.0.113.1:3128",               # same host:port -> deduped
            "198.51.100.2:8080",
            "not-a-valid-line",               # skipped with a warning
            "http://198.51.100.3:9000",
        ])
        self.assertEqual(loaded, 3)
        labels = sorted(p.label for p in pm.proxies())
        self.assertEqual(labels, sorted([
            "user@203.0.113.1:3128",          # the auth'd version wins
            "198.51.100.2:8080",
            "198.51.100.3:9000",
        ]))


class RateLimiterTest(unittest.TestCase):
    """RateLimiter: global throttle cadence + global_pause behavior."""

    def _limiter_with_clock(self, start=0.0, min_interval=0.1, advance_clock=True):
        """
        A RateLimiter on a fake clock: monotonic() reads clock[0], and sleep()
        records the wait. With advance_clock (default) the sleep also advances
        clock[0], as real sleeping would - single-threaded cadence tests need
        this. Thread tests pass advance_clock=False so the clock stays put and
        every throttle's math is deterministic. Returns (limiter, clock, sleeps).
        """
        clock = [float(start)]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if advance_clock:
                clock[0] += seconds

        patcher_m = patch.object(dc.time, "monotonic", side_effect=lambda: clock[0])
        patcher_s = patch.object(dc.time, "sleep", side_effect=fake_sleep)
        patcher_m.start()
        patcher_s.start()
        self.addCleanup(patcher_m.stop)
        self.addCleanup(patcher_s.stop)
        return dc.RateLimiter(min_interval), clock, sleeps

    def _assert_sleeps(self, sleeps, expected):
        """Compare recorded sleeps with almost-equality (float arithmetic)."""
        self.assertEqual(len(sleeps), len(expected))
        for got, want in zip(sleeps, expected):
            self.assertAlmostEqual(got, want)

    def test_first_throttle_is_instant(self):
        """The very first request has nothing to wait for."""
        rl, _, sleeps = self._limiter_with_clock(start=500.0)
        rl.throttle()
        self.assertEqual(sleeps, [])
        self.assertAlmostEqual(rl._next_request_at, 500.1)

    def test_throttle_spaces_requests_by_min_interval(self):
        """Back-to-back throttles each wait exactly min_interval."""
        rl, clock, sleeps = self._limiter_with_clock(min_interval=0.1)
        rl.throttle()            # instant
        rl.throttle()            # waits 0.1 (the sleep advances the clock)
        rl.throttle()            # waits 0.1 again
        self._assert_sleeps(sleeps, [0.1, 0.1])
        # Cadence is preserved: the next slot stays exactly one interval out.
        self.assertAlmostEqual(rl._next_request_at - clock[0], 0.1)

    def test_global_pause_delays_next_request(self):
        """After global_pause(N) the next request waits out the full N."""
        rl, _, sleeps = self._limiter_with_clock(start=100.0)
        rl.global_pause(5.0)
        rl.throttle()            # must wait the whole pause
        rl.throttle()            # then back to the normal cadence
        self._assert_sleeps(sleeps, [5.0, 0.1])

    def test_global_pause_extends_throttle_schedule(self):
        """A pause after a throttle pushes the slot out to the pause time."""
        rl, _, sleeps = self._limiter_with_clock(start=10.0)
        rl.throttle()            # next request at 10.1
        rl.global_pause(2.0)     # extends it to 12.0
        rl.throttle()            # waits 2.0
        self._assert_sleeps(sleeps, [2.0])
        self.assertAlmostEqual(rl._next_request_at, 12.1)

    def test_small_global_pause_does_not_shorten(self):
        """A pause smaller than the pending schedule is ignored (max semantics)."""
        rl, _, sleeps = self._limiter_with_clock(start=10.0)
        rl.throttle()            # next request at 10.1
        rl.global_pause(0.01)    # smaller -> no effect
        self.assertEqual(sleeps, [])
        self.assertAlmostEqual(rl._next_request_at, 10.1)

    def test_idle_time_skips_ahead_without_waiting(self):
        """After an idle gap the limiter doesn't wait for long-past slots."""
        rl, clock, sleeps = self._limiter_with_clock(start=10.0)
        rl.throttle()            # next request at 10.1
        clock[0] = 50.0          # long idle gap
        rl.throttle()            # slot long past -> instant, next = now + interval
        self.assertEqual(sleeps, [])
        self.assertAlmostEqual(rl._next_request_at, 50.1)

    def test_throttle_is_thread_safe(self):
        """Concurrent throttles reserve distinct slots - never share one."""
        # Static clock: sleeps are recorded but don't advance time, so each
        # throttle sees now=0 and wait = the growing next slot. Deterministic.
        rl, _, sleeps = self._limiter_with_clock(start=0.0, advance_clock=False)

        def worker():
            for _ in range(5):
                rl.throttle()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # 8 workers * 5 calls: the first is instant, the other 39 each reserve
        # and sleep for their own 0.1s slot (values may vary by interleaving,
        # but every slot is positively spaced and none are shared).
        self.assertAlmostEqual(rl._next_request_at, 0.1 * 40)
        self.assertEqual(len(sleeps), 39)
        self.assertTrue(all(s > 0.0 for s in sleeps))


class GenListIntegrationTest(unittest.TestCase):
    """End-to-end: python discord_checker.py --genlist N with a temp list.txt."""

    def _run_genlist(self, count, length=None, letters=False, seed_lines=()):
        """
        Run main() with --genlist against a temp dir; return (file_lines, lines).

        A `length` of None omits --length entirely, exercising main()'s
        default. SCRIPT_DIR is redirected so the real list.txt in the project
        is never touched; the real generate_list_file() runs against the temp
        file.
        """
        tmpdir = tempfile.mkdtemp(prefix="dc_gen_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        list_path = os.path.join(tmpdir, "list.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(seed_lines))
            if seed_lines:
                fh.write("\n")
        config_path = os.path.join(tmpdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({"use_proxies": False}, fh)

        argv = ["discord_checker.py", "--genlist", str(count)]
        if length is not None:
            argv += ["--length", str(length)]
        argv += ["--config", config_path]
        if letters:
            argv.append("--letters")
        emitted = []
        old_argv = sys.argv
        sys.argv = argv
        try:
            with patch.object(dc, "SCRIPT_DIR", tmpdir), \
                 patch.object(dc, "_emit", side_effect=emitted.append):
                dc.main()
        finally:
            sys.argv = old_argv
        with open(list_path, encoding="utf-8") as fh:
            file_lines = [l for l in fh.read().splitlines()
                          if l and not l.startswith("#")]
        return file_lines, [ANSI_RE.sub("", l) for l in emitted]

    def test_genlist_appends_unique_names_and_counts(self):
        """New names are appended; seed names are never duplicated; counts add up."""
        seed = ["aa", "bb"]
        file_lines, lines = self._run_genlist(5, 2, letters=True, seed_lines=seed)
        self.assertIn("[+] Generated 5 new username(s). 7 total entries",
                      "\n".join(lines))
        self.assertEqual(len(file_lines), 7)
        self.assertEqual(len(set(file_lines)), 7)          # all unique
        self.assertEqual(file_lines.count("aa"), 1)       # seed kept exactly once
        self.assertEqual(file_lines.count("bb"), 1)
        new_names = [n for n in file_lines if n not in seed]
        self.assertEqual(len(new_names), 5)
        self.assertTrue(all(re.fullmatch(r"[a-z]{2}", n) for n in new_names))

    def test_genlist_default_length_and_alphabet(self):
        """No --length/--letters: 4-char names from letters+digits+underscore."""
        file_lines, lines = self._run_genlist(3)       # --length omitted
        self.assertIn("[+] Generated 3 new username(s). 3 total entries",
                      "\n".join(lines))
        self.assertEqual(len(file_lines), 3)
        self.assertEqual(len(set(file_lines)), 3)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{4}", n) for n in file_lines))

    def test_genlist_clamps_length(self):
        """--length is clamped to Discord's 2-32 range."""
        file_lines, _ = self._run_genlist(2, length=1)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{2}", n) for n in file_lines))
        file_lines, _ = self._run_genlist(2, length=99)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{32}", n) for n in file_lines))

    def test_genlist_exhausted_namespace_warns(self):
        """A pre-filled name space writes nothing and warns."""
        seed = ["".join(p) for p in itertools.product(dc.ALPHABET_LETTERS, repeat=2)]
        file_lines, lines = self._run_genlist(10, 2, letters=True, seed_lines=seed)
        joined = "\n".join(lines)
        self.assertIn("[!] Only generated 0/10 - the name space for "
                      "2-char usernames may be exhausted.", joined)
        self.assertEqual(len(file_lines), len(seed))       # unchanged
        self.assertEqual(len(set(file_lines)), len(seed))

    def test_genlist_skips_comments_and_blanks(self):
        """Comment/blank lines in list.txt are ignored (not counted or matched)."""
        file_lines, lines = self._run_genlist(2, 4, seed_lines=["# note", "", "mnop"])
        self.assertIn("[+] Generated 2 new username(s). 3 total entries",
                      "\n".join(lines))
        self.assertEqual(len(file_lines), 3)               # mnop + 2 new
        self.assertEqual(file_lines.count("mnop"), 1)


class RunModeIntegrationTest(unittest.TestCase):
    """End-to-end: python discord_checker.py --run MODE, network mocked."""

    def _run_cli(self, mode, count, seed_names=(), config_overrides=None):
        """
        Run main() as `discord_checker.py --run MODE --count N --config <tmp>`
        with the network mocked and SCRIPT_DIR redirected to a temp dir.

        The real build_names_for_mode()/random_usernames() run (mode 1 reads
        the seeded temp list.txt, modes 2-4 generate real random names), while
        DiscordChecker.check is replaced so nothing touches the network. Returns
        (output_without_ansi, tmpdir, check_mock, checked_names) where
        checked_names is the list of names the engine actually asked the
        checker about. Pass `config_overrides` to tweak the written config.json
        (e.g. a large default_random_count to prove --count overrides it).
        """
        tmpdir = tempfile.mkdtemp(prefix="dc_run_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        list_path = os.path.join(tmpdir, "list.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(seed_names))
            if seed_names:
                fh.write("\n")

        config = {
            "webhook_url": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
            "enable_webhook": True,       # stays disabled: placeholder URL
            "use_proxies": False,
            "concurrency": 4,
        }
        config.update(config_overrides or {})
        config_path = os.path.join(tmpdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)

        check_mock = Mock(return_value=(dc.ST_TAKEN, "taken"))
        captured = io.StringIO()
        real_stdout = sys.stdout
        old_argv = sys.argv
        sys.argv = ["discord_checker.py", "--run", str(mode), "--count",
                    str(count), "--config", config_path]
        try:
            with patch.object(dc, "SCRIPT_DIR", tmpdir), \
                 patch.object(dc.DiscordChecker, "check", check_mock), \
                 patch.object(dc.time, "sleep"):       # no real rate-limiter/backoff waits
                sys.stdout = captured
                try:
                    dc.main()
                finally:
                    sys.stdout = real_stdout
        finally:
            sys.argv = old_argv

        checked_names = [c.args[0] for c in check_mock.call_args_list]
        return ANSI_RE.sub("", captured.getvalue()), tmpdir, check_mock, checked_names

    def test_mode2_random_generation_end_to_end(self):
        """Mode 2: 4-char names from letters+digits+underscore, all checked."""
        out, _, check_mock, names = self._run_cli(2, 10)
        self.assertEqual(len(names), 10)
        self.assertEqual(len(set(names)), 10)             # generated names are unique
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{4}", n) for n in names))
        self.assertEqual(check_mock.call_count, 10)       # every name reached the checker
        self.assertIn(f"  {'Taken:':<20}10", out)         # and the run summary counted them

    def test_mode3_letters_only(self):
        """Mode 3: 4-char names restricted to letters."""
        out, _, check_mock, names = self._run_cli(3, 10)
        self.assertEqual(len(names), 10)
        self.assertTrue(all(re.fullmatch(r"[a-z]{4}", n) for n in names))
        self.assertEqual(check_mock.call_count, 10)
        self.assertIn(f"  {'Taken:':<20}10", out)

    def test_mode4_three_char_names(self):
        """Mode 4: 3-char names from the full alphabet."""
        out, _, check_mock, names = self._run_cli(4, 10)
        self.assertEqual(len(names), 10)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{3}", n) for n in names))
        self.assertEqual(check_mock.call_count, 10)
        self.assertIn(f"  {'Taken:':<20}10", out)

    def test_count_ignores_config_default(self):
        """--count overrides the configured default_random_count."""
        _, _, _, names = self._run_cli(
            2, 4, config_overrides={"default_random_count": 1000})
        self.assertEqual(len(names), 4)                   # 4, not the config's 1000

    def test_mode1_loads_list_and_checks_it(self):
        """Mode 1 runs a temp list.txt: blanks/comments skipped, dupes collapsed."""
        out, tmpdir, check_mock, names = self._run_cli(
            1, 1, seed_names=["# comment", "", "Alice", "bob", "alice", "CAROL"])
        self.assertEqual(sorted(names), ["alice", "bob", "carol"])
        self.assertEqual(check_mock.call_count, 3)
        self.assertIn(f"  {'Taken:':<20}3", out)
        # list.txt is not touched by the run (no genlist path involved).
        with open(os.path.join(tmpdir, "list.txt"), encoding="utf-8") as fh:
            self.assertIn("Alice\n", fh.read())          # seed lines unchanged


class SelftestIntegrationTest(unittest.TestCase):
    """End-to-end: python discord_checker.py --selftest, network mocked."""

    def _run_selftest(self, status_code=200, text="{}", json_data=None, exc=None):
        """
        Run main() with --selftest against a temp config; the single network
        call (requests.Session().post) is replaced by a mock response or an
        exception. Returns (output_without_ansi, post_mock).
        """
        tmpdir = tempfile.mkdtemp(prefix="dc_selftest_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        config_path = os.path.join(tmpdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({"timeout_seconds": 10}, fh)

        session = Mock()
        post_mock = Mock()
        if exc is not None:
            post_mock.side_effect = exc
        else:
            resp = Mock()
            resp.status_code = status_code
            resp.text = text
            resp.json.return_value = json_data if json_data is not None else {}
            post_mock.return_value = resp
        session.post = post_mock

        captured = io.StringIO()
        real_stdout = sys.stdout
        old_argv = sys.argv
        sys.argv = ["discord_checker.py", "--selftest", "--config", config_path]
        try:
            with patch.object(dc, "SCRIPT_DIR", tmpdir), \
                 patch.object(dc.requests, "Session", return_value=session):
                sys.stdout = captured
                try:
                    dc.main()
                finally:
                    sys.stdout = real_stdout
        finally:
            sys.argv = old_argv
        return ANSI_RE.sub("", captured.getvalue()), post_mock, session

    def test_selftest_200_available(self):
        """A 200 taken:false prints the raw lines and the 'is available' verdict."""
        out, post_mock, session = self._run_selftest(
            status_code=200, text='{"taken": false}',
            json_data={"taken": False})
        self.assertIn("[+] HTTP 200 in", out)
        self.assertIn("[i] Response body: {\"taken\": false}", out)
        self.assertIn("is available", out)
        post_mock.assert_called_once()
        url, kwargs = post_mock.call_args
        self.assertEqual(url, (dc.USERNAME_ATTEMPT_URL,))
        self.assertRegex(kwargs["json"]["username"], r"^zz[a-z]{8}$")
        self.assertEqual(kwargs["timeout"], (5, 10))    # connect capped at 5s
        session.close.assert_called_once()               # finally always closes

    def test_selftest_200_taken(self):
        """A 200 taken:true prints the 'is currently taken' verdict."""
        out, _, _ = self._run_selftest(
            status_code=200, text='{"taken": true}',
            json_data={"taken": True})
        self.assertIn("[+] HTTP 200 in", out)
        self.assertIn("is currently taken", out)

    def test_selftest_400_is_reachable(self):
        """A 400 still proves reachability and warns accordingly."""
        out, _, _ = self._run_selftest(status_code=400, text='{"code": 50035}')
        self.assertIn("[!] Discord answered 400 (invalid username) but the API is "
                      "reachable", out)

    def test_selftest_429_warns_rate_limited(self):
        """A 429 warns that requests are being throttled."""
        out, _, _ = self._run_selftest(status_code=429, text="rate limited")
        self.assertIn("[!] Discord answered with 429 (rate limited)", out)

    def test_selftest_blocked_warns(self):
        """A 401/403 warns that the endpoint or IP is blocked."""
        for status in (401, 403):
            with self.subTest(status=status):
                out, _, _ = self._run_selftest(status_code=status, text="blocked")
                self.assertIn("[!] Discord answered with 401/403 - endpoint or IP "
                              "is blocked.", out)

    def test_selftest_unexpected_status_warns(self):
        """Any other status falls through to the generic warning."""
        out, _, _ = self._run_selftest(status_code=503, text="boom")
        self.assertIn("[!] Unexpected status 503.", out)

    def test_selftest_200_unexpected_body(self):
        """A 200 without a usable 'taken' value still reports reachability."""
        out, _, _ = self._run_selftest(status_code=200, text="<html>", json_data={})
        self.assertIn("[+] Discord API is fully reachable and checking works.", out)

    def test_selftest_connection_error(self):
        """A connection failure prints the CONNECTION ERROR diagnostic."""
        exc = dc.requests.exceptions.ConnectionError("dns failure")
        out, _, _ = self._run_selftest(exc=exc)
        self.assertIn("[x] CONNECTION ERROR after", out)
        self.assertIn("dns failure", out)

    def test_selftest_timeout(self):
        """A timeout prints the TIMED OUT diagnostic."""
        out, _, _ = self._run_selftest(exc=dc.requests.exceptions.Timeout())
        self.assertIn("[x] Request TIMED OUT after", out)


class TestProxiesIntegrationTest(unittest.TestCase):
    """End-to-end: python discord_checker.py --test-proxies, check mocked."""

    def _run_test_proxies(self, proxy_lines, results, missing_file=False):
        """
        Run main() with --test-proxies against a temp dir holding the given
        proxy.txt lines; DiscordChecker.check is replaced by `results`, a dict
        mapping each proxy label to its (status, detail) response tuple.
        Pass `missing_file=True` to test with no proxy.txt at all.
        Returns (output_without_ansi, tmpdir).
        """
        tmpdir = tempfile.mkdtemp(prefix="dc_tpx_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        proxy_path = os.path.join(tmpdir, "proxy.txt")
        if not missing_file:
            with open(proxy_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(proxy_lines))
                if proxy_lines:
                    fh.write("\n")
        config_path = os.path.join(tmpdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({"use_proxies": True, "hide_proxies": False}, fh)

        def fake_check(name, proxy):
            return results.get(proxy.label, (dc.ST_NETWORK, "unexpected"))

        captured = io.StringIO()
        real_stdout = sys.stdout
        old_argv = sys.argv
        sys.argv = ["discord_checker.py", "--test-proxies", "--config", config_path]
        try:
            with patch.object(dc, "SCRIPT_DIR", tmpdir), \
                 patch.object(dc, "REPO_ROOT", tmpdir), \
                 patch.object(dc.DiscordChecker, "check",
                              side_effect=fake_check):
                sys.stdout = captured
                try:
                    dc.main()
                finally:
                    sys.stdout = real_stdout
        finally:
            sys.argv = old_argv
        return ANSI_RE.sub("", captured.getvalue()), tmpdir

    @staticmethod
    def _kept_proxies(tmpdir):
        """The non-comment lines currently in proxy.txt."""
        with open(os.path.join(tmpdir, "proxy.txt"), encoding="utf-8") as fh:
            return [l for l in fh.read().splitlines()
                    if l and not l.startswith("#")]

    def test_all_healthy_leaves_file_unchanged(self):
        """Every proxy OK: file untouched, no backup, no rewrite."""
        lines = ["good.one:3128", "good.two:3128"]
        results = {
            "good.one:3128": (dc.ST_TAKEN, "taken"),
            "good.two:3128": (dc.ST_AVAILABLE, "available"),
        }
        out, tmpdir = self._run_test_proxies(lines, results)
        self.assertIn("good.one:3128  OK", out)
        self.assertIn("good.two:3128  OK", out)
        self.assertIn("[i] Test finished in", out)
        self.assertIn("2/2 proxies kept (0 dead, 0 blocked)", out)
        self.assertIn("[+] All proxies are healthy - proxy.txt unchanged.", out)
        self.assertEqual(self._kept_proxies(tmpdir), lines)   # untouched
        self.assertFalse(os.path.exists(
            os.path.join(tmpdir, "proxy_backup.txt")))        # no backup needed

    def test_mixed_prunes_dead_and_blocked(self):
        """Dead + blocked proxies are dropped; only the healthy one survives."""
        lines = ["dead.one:3128", "blocked.one:3128", "keep.one:3128"]
        results = {
            "dead.one:3128": (dc.ST_NETWORK, "connection error: boom"),
            "blocked.one:3128": (dc.ST_BLOCKED, "403: blocked"),
            "keep.one:3128": (dc.ST_TAKEN, "taken"),
        }
        out, tmpdir = self._run_test_proxies(lines, results)
        self.assertIn("dead.one:3128  DEAD", out)
        self.assertIn("blocked.one:3128  OK but BLOCKED", out)
        self.assertIn("keep.one:3128  OK", out)
        self.assertIn("1/3 proxies kept (1 dead, 1 blocked)", out)
        self.assertIn("[+] Backed up the previous list to", out)
        self.assertIn("[i] Rewriting", out)
        self.assertIn("[+] Done - removed 2 dead/blocked proxy/proxies.", out)
        self.assertEqual(self._kept_proxies(tmpdir), ["keep.one:3128"])
        # The previous list is kept safe before rewriting.
        with open(os.path.join(tmpdir, "proxy_backup.txt"),
                  encoding="utf-8") as fh:
            self.assertIn("dead.one:3128", fh.read())

    def test_all_dead_leaves_file_unchanged(self):
        """Nothing healthy: proxy.txt untouched and no backup written."""
        lines = ["dead.one:3128", "dead.two:3128"]
        results = {
            "dead.one:3128": (dc.ST_NETWORK, "timeout: boom"),
            "dead.two:3128": (dc.ST_PROXY_ERROR, "proxy error: boom"),
        }
        out, tmpdir = self._run_test_proxies(lines, results)
        self.assertIn("0/2 proxies kept (2 dead, 0 blocked)", out)
        self.assertIn("[x] No healthy proxies found - proxy.txt left unchanged.",
                      out)
        self.assertEqual(self._kept_proxies(tmpdir), lines)
        self.assertFalse(os.path.exists(
            os.path.join(tmpdir, "proxy_backup.txt")))

    def test_rate_limited_proxy_is_kept(self):
        """A 429 answers the request, so the proxy counts as OK, not dead."""
        out, _ = self._run_test_proxies(
            ["rl.one:3128"],
            {"rl.one:3128": (dc.ST_RATE_LIMITED, (5.0, True))})
        self.assertIn("rl.one:3128  OK", out)
        self.assertIn("429 5s GLOBAL", out)     # the 429 detail is echoed
        self.assertIn("1/1 proxies kept (0 dead, 0 blocked)", out)

    def test_auth_proxy_rewritten_with_credentials(self):
        """The rewrite preserves user:pass@host:port lines."""
        out, tmpdir = self._run_test_proxies(
            ["user:pass@keep.one:3128", "dead.one:3128"],
            {
                "user@keep.one:3128": (dc.ST_TAKEN, "taken"),
                "dead.one:3128": (dc.ST_NETWORK, "connection error: boom"),
            })
        self.assertIn("user@keep.one:3128  OK", out)
        self.assertEqual(self._kept_proxies(tmpdir),
                         ["user:pass@keep.one:3128"])

    def test_no_proxies_file_errors(self):
        """An empty proxy.txt stops the run with a clear error."""
        out, _ = self._run_test_proxies([], {})
        self.assertIn("[x] No proxies found in", out)

    def test_missing_proxy_file_errors(self):
        """No proxy.txt at all: warns it's missing, then the same error."""
        out, _ = self._run_test_proxies([], {}, missing_file=True)
        self.assertIn("[!] proxy.txt not found at", out)
        self.assertIn("[x] No proxies found in", out)


if __name__ == "__main__":
    unittest.main()
