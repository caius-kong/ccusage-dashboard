#!/usr/bin/env python3
"""Regression tests for issue #2: the polling path re-executed ccusage forever.

Root cause was two independent defects in run_ccusage() plus a missing guard:

  1. cache expiry was stamped with the *pre-run* clock, so an entry's real
     lifetime was `ttl - runtime` — negative whenever a run outlasted its TTL,
     which made the cache never hit and re-ran ccusage on every poll;
  2. `_lock` guarded only the cache dict, never the subprocess, so N concurrent
     requests for the same key spawned N ccusage processes.

These tests pin the fixed behaviour without ever invoking the real ccusage:
subprocess.run is replaced with a stub that sleeps and records spawns.

Run with:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import server  # noqa: E402


class FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class StubCcusage:
    """Stand-in for subprocess.run: counts spawns, tracks concurrency, sleeps."""

    def __init__(self, runtime: float = 0.2, stdout: str | None = None, exc: Exception | None = None) -> None:
        self.runtime = runtime
        self.stdout = stdout if stdout is not None else json.dumps({"daily": [], "session": []})
        self.exc = exc
        self.spawns = 0
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, cmd, **kwargs):
        with self._lock:
            self.spawns += 1
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.runtime)
            if self.exc is not None:
                raise self.exc
            return FakeProc(self.stdout)
        finally:
            with self._lock:
                self.live -= 1


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        server._cache.clear()
        server._inflight.clear()
        self._real_run = server.subprocess.run

    def tearDown(self) -> None:
        server.subprocess.run = self._real_run
        server._cache.clear()
        server._inflight.clear()

    def test_expiry_never_precedes_completion(self) -> None:
        """An entry must never be expired at the moment it is written.

        This is the original defect, and it is easy to reintroduce: a wall-clock
        aligned window ("end of the current minute") expires on write whenever a
        run starts near a boundary. Simulate completion without real sleeping.
        """
        ttl = 60.0
        runtime = 12.0
        for phase in (0.0, 5.0, 30.0, 57.0, 59.5, 59.99):
            with self.subTest(phase=phase):
                request_at = 1000.0 + phase
                stub = StubCcusage(runtime=0.0)
                server.subprocess.run = stub
                server._cache.clear()
                server._inflight.clear()
                real_time = time.time
                stamps = [request_at, request_at + runtime]
                time.time = lambda: stamps.pop(0) if len(stamps) > 1 else stamps[0]  # type: ignore[assignment]
                try:
                    server.run_ccusage(["daily", "--last", "1"], ttl=ttl)
                    expires_at = server._cache["daily --last 1"][0]
                    self.assertGreater(
                        expires_at,
                        request_at + runtime,
                        f"entry expired {(request_at + runtime - expires_at):.1f}s BEFORE the run finished",
                    )
                finally:
                    time.time = real_time  # type: ignore[assignment]

    def test_cadence_is_exactly_one_scan_per_period(self) -> None:
        """An entry must be stale by the next poll of the same cadence, even when
        the scan was delayed before starting (queued behind other scans).

        If the entry outlives the next tick the real cadence silently becomes
        `period + delay` (e.g. 120s instead of 60s), which is why expiry is
        measured from the request rather than from when the scan started.
        """
        ttl = 60.0
        runtime = 12.0
        for start_delay in (0.0, 0.5, 3.0, 7.0, 20.0):
            with self.subTest(start_delay=start_delay):
                tick = 1000.0
                stub = StubCcusage(runtime=0.0)
                server.subprocess.run = stub
                server._cache.clear()
                server._inflight.clear()
                real_time = time.time
                stamps = [tick, tick + start_delay + runtime]
                time.time = lambda: stamps.pop(0) if len(stamps) > 1 else stamps[0]  # type: ignore[assignment]
                try:
                    server.run_ccusage(["daily", "--last", "1"], ttl=ttl)
                    expires_at = server._cache["daily --last 1"][0]
                    next_tick = tick + ttl
                    self.assertLess(
                        expires_at,
                        next_tick,
                        f"entry valid {expires_at - next_tick:.1f}s past the next tick -> cadence exceeds {ttl}s",
                    )
                    self.assertGreater(expires_at, tick + start_delay + runtime)
                finally:
                    time.time = real_time  # type: ignore[assignment]

    def test_sessions_uses_the_active_view_ttl(self) -> None:
        """The sessions panel is polled with the active view, so its cache period
        must match that view — otherwise a today window is served stale behind a
        60s refresh, or re-scanned when it did not need to be."""
        seen: list[float] = []
        stub = StubCcusage(runtime=0.0)
        server.subprocess.run = stub
        real = server.run_ccusage

        def spy(args, ttl):
            seen.append(ttl)
            return real(args, ttl)

        server.run_ccusage = spy  # type: ignore[assignment]
        try:
            server.sessions(period="today")
            self.assertEqual(seen[-1], server.TTL["/api/today"])
            server.sessions(period="week")
            self.assertEqual(seen[-1], server.TTL["/api/sessions"])
            self.assertGreater(server.TTL["/api/sessions"], server.TTL["/api/today"])
        finally:
            server.run_ccusage = real  # type: ignore[assignment]

    def test_expiry_is_window_boundary_not_now_plus_ttl(self) -> None:
        """Expiry must track the period, not `now + ttl` from the request time.

        The original code used the PRE-run clock, giving a lifetime of
        `ttl - runtime`; expiry must instead be measured from the scan's start.
        """
        stub = StubCcusage(runtime=0.2)
        server.subprocess.run = stub
        ttl = 1.0

        server.run_ccusage(["daily", "--last", "1"], ttl=ttl)
        expires_at = server._cache["daily --last 1"][0]
        remaining = expires_at - time.time()
        # must be at most one full window away (not ttl + runtime)
        self.assertLessEqual(remaining, ttl)
        self.assertGreater(remaining, 0)

    def test_ttl_shorter_than_runtime_still_caches(self) -> None:
        """A ttl below the runtime must still serve calls, not expire on write
        (the original bug: the entry was dead the moment it was stored)."""
        stub = StubCcusage(runtime=0.2)
        server.subprocess.run = stub
        server.run_ccusage(["daily", "--last", "1"], ttl=1.0)
        server.run_ccusage(["daily", "--last", "1"], ttl=1.0)
        self.assertEqual(stub.spawns, 1)

    def test_cache_outlives_one_poll_interval(self) -> None:
        """A TTL above the run cost must survive a back-to-back poll round."""
        stub = StubCcusage(runtime=0.05)
        server.subprocess.run = stub

        server.run_ccusage(["weekly", "--last", "1"], ttl=1.0)
        server.run_ccusage(["weekly", "--last", "1"], ttl=1.0)
        server.run_ccusage(["weekly", "--last", "1"], ttl=1.0)

        self.assertEqual(stub.spawns, 1)

    def test_concurrent_same_key_spawns_once(self) -> None:
        """Defect 2: N concurrent cold-key requests must coalesce to 1 spawn."""
        stub = StubCcusage(runtime=0.3)
        server.subprocess.run = stub
        n = 8

        results: list[object] = []
        lock = threading.Lock()

        def call() -> None:
            r = server.run_ccusage(["daily", "--since", "A", "--until", "B"], ttl=5)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=call) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(stub.spawns, 1, "single-flight must coalesce identical concurrent misses")
        self.assertEqual(len(results), n)
        self.assertTrue(all(r == results[0] for r in results))

    def test_global_concurrency_is_bounded(self) -> None:
        """Distinct keys must not stack unbounded ccusage processes."""
        stub = StubCcusage(runtime=0.2)
        server.subprocess.run = stub

        keys = [[f"daily", "--since", f"2025-01-{i:02d}", "--until", "2025-02-01"] for i in range(1, 7)]
        threads = [threading.Thread(target=server.run_ccusage, args=(k, 5)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(stub.spawns, len(keys))
        self.assertLessEqual(
            stub.peak,
            server._MAX_CONCURRENT_RUNS,
            "concurrent ccusage processes must respect the global gate",
        )

    def test_errors_are_not_cached_for_the_full_ttl(self) -> None:
        """A transient failure must expire fast, not stay cached for a whole TTL."""
        stub = StubCcusage(runtime=0.01, exc=RuntimeError("boom"))
        server.subprocess.run = stub

        first = server.run_ccusage(["daily", "--last", "1"], ttl=300)
        self.assertIn("error", first)

        key = "daily --last 1"
        expires_at = server._cache[key][0]
        remaining = expires_at - time.time()
        self.assertLess(remaining, 60, "error entries must not sit in cache for the full TTL")
        self.assertLessEqual(remaining, server._ERROR_TTL + 1)

    def test_waiter_retries_when_inflight_run_produced_no_entry(self) -> None:
        """Defensive: a crashed in-flight owner must not strand its waiters."""
        stub = StubCcusage(runtime=0.01)
        server.subprocess.run = stub
        key = "daily --last 1"
        server._inflight[key] = threading.Event()  # owner that never publishes
        result: list[object] = []

        def waiter() -> None:
            result.append(server.run_ccusage(["daily", "--last", "1"], ttl=5))

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        t.join(timeout=0.2)
        self.assertEqual(result, [], "waiter should be blocked on the in-flight owner")

        with server._lock:
            ev = server._inflight.pop(key, None)
        if ev:
            ev.set()
        t.join(timeout=2.0)

        self.assertEqual(len(result), 1, "waiter must retry once the stale owner is gone")
        self.assertEqual(stub.spawns, 1)

    def test_per_endpoint_ttls_match_the_cadence_design(self) -> None:
        """Only the default view re-scans every poll; heavier views re-scan slowly.

        Staggering the cadences is what keeps a steady-state tick down to ~1 miss,
        which is why a small (not serial) concurrency gate is enough.
        """
        self.assertEqual(server.TTL["/api/today"], server._REFRESH_INTERVAL)
        for endpoint, ttl in server.TTL.items():
            self.assertGreater(
                ttl,
                25,
                f"{endpoint} TTL={ttl}s must exceed the slowest single ccusage run (~25s)",
            )
        for endpoint in ("/api/week", "/api/month", "/api/range", "/api/trend", "/api/sessions"):
            self.assertEqual(
                server.TTL[endpoint],
                server._SLOW_INTERVAL,
                f"{endpoint} should use the slow cadence, not the default one",
            )
        self.assertGreater(server._SLOW_INTERVAL, server._REFRESH_INTERVAL)
    def test_concurrency_gate_is_bounded_and_not_serial(self) -> None:
        """Serial (1) would make a warm-up with several misses needlessly slow;
        unbounded would re-create the stacked-scan spikes. Keep it small but > 1."""
        self.assertGreaterEqual(server._MAX_CONCURRENT_RUNS, 2)
        self.assertLessEqual(server._MAX_CONCURRENT_RUNS, 4)

    def test_refresh_interval_matches_the_frontend(self) -> None:
        """The server's refresh constant and the browser's default-view cadence must agree."""
        html = (Path(__file__).resolve().parent.parent / "lib" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"today:{server._REFRESH_INTERVAL * 1000}", html)
        self.assertIn(f"auto-refresh {server._REFRESH_INTERVAL}s", html)

    def test_frontend_uses_per_view_cadence_and_does_not_poll_inactive_views(self) -> None:
        """The polling timer must be re-armed per active view, and slow views must
        use the slow cadence — otherwise an idle 'today' tab keeps re-scanning
        monthly/weekly data in the background."""
        html = (Path(__file__).resolve().parent.parent / "lib" / "index.html").read_text(encoding="utf-8")
        slow_ms = server._SLOW_INTERVAL * 1000
        self.assertIn("VIEW_MS", html)
        self.assertIn(f"week:{slow_ms}", html)
        self.assertIn(f"month:{slow_ms}", html)
        self.assertIn(f"range:{slow_ms}", html)
        # the timer follows the active view rather than a single fixed interval
        self.assertIn("setInterval(tick, VIEW_MS[active]", html)
        self.assertNotIn("setInterval(tick,60000)", html)


class FrontendPollTest(unittest.TestCase):
    def test_no_legacy_fixed_interval(self) -> None:
        html = (Path(__file__).resolve().parent.parent / "lib" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("setInterval(tick,15000)", html)
        self.assertIn(f"auto-refresh {server._REFRESH_INTERVAL}s", html)

if __name__ == "__main__":
    unittest.main()