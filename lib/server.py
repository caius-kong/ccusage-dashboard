#!/usr/bin/env python3
"""
ccusage-ui — a tiny zero-dependency local dashboard for ccusage.

It shells out to ccusage's JSON reports and renders:
  - today / this week / this month / custom range, grouped by model
  - a 30-day cost trend chart
  - a monthly budget alert (default cap: $300)

Because all numbers come from ccusage itself, the figures always match what
`ccusage` reports (the source you already trust). No pricing table to maintain,
no third-party packages — only the Python standard library. The sole optional
This dashboard is fully offline except one manual check: clicking the
"⇪ check update" button in the header makes a single request to the npm
registry and shows the result in a popup (up to date / update available
with the `npx @caius_kong/ccusage-dashboard@latest` command to run yourself /
unreachable). Nothing is ever checked automatically.

Usage:
    python3 server.py [--port 8799] [--budget 300]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# Cache: args-key -> (expires_at, data)
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
# key -> Event set when the in-flight run for that key finishes (single-flight)
_inflight: dict[str, threading.Event] = {}

# Every report scans the full local session history, so N concurrent runs burn N
# cores to compute an answer. Same-key requests coalesce onto a single run (see
# run_ccusage); this gate additionally keeps DIFFERENT keys from stacking. It is
# deliberately small rather than 1: with per-endpoint TTLs (below) a steady-state
# tick has at most one miss, so the gate is only touched when warm-up or a slow
# endpoint's expiry collides with the default view's refresh. 2 bounds that
# collision without letting it re-stack into the multi-core spikes this repo had.
_MAX_CONCURRENT_RUNS = 2
_run_gate = threading.Semaphore(_MAX_CONCURRENT_RUNS)


def resolve_ccusage() -> list[str]:
    """Resolve the ccusage CLI the system itself uses, and only that.

    Hard constraint: every number comes from the ccusage CLI, and it must be
    the same one the user runs (`npx ccusage ...`). So we look for, in order,
    an explicit --ccusage-path, a `ccusage` on PATH, then `npx --yes ccusage`.
    No cache-scanning and no version-picking: the version is whatever the
    system resolves, never a stale copy we chose ourselves.
    """
    explicit = _CCUSAGE_PATH_OVERRIDE
    if explicit:
        return [explicit]
    p = shutil.which("ccusage")
    if p:
        return [p]
    return ["npx", "--yes", "ccusage"]


_CCUSAGE_PATH_OVERRIDE: str | None = None

BUDGET = 300.0  # monthly cap in USD (override via --budget or CCUSAGE_BUDGET)

# Refresh cadence is driven by the browser (lib/index.html), which polls the
# ACTIVE view only. Only the default Today view needs re-scanning every minute;
# the heavier reports cost ~12-16 CPU-seconds each and are rarely watched, so
# they re-scan a tenth as often. Inactive views are never polled at all.
#
# A value here is the refresh PERIOD, not a length of time an entry is kept.
# run_ccusage expires entries slightly before their period elapses, so each new
# period triggers exactly one fresh scan regardless of how long a scan takes.
_REFRESH_INTERVAL = 60   # browser poll interval; must match lib/index.html
_SLOW_INTERVAL = 600     # cadence for the non-default views
TTL = {
    "/api/today": _REFRESH_INTERVAL,   # default view: re-scanned every minute
    "/api/week": _SLOW_INTERVAL,
    "/api/month": _SLOW_INTERVAL,
    "/api/range": _SLOW_INTERVAL,
    "/api/trend": _SLOW_INTERVAL,
    "/api/sessions": _SLOW_INTERVAL,   # the today view overrides this (see sessions())
}

# A transient failure is remembered only briefly: long enough that concurrent
# waiters share it instead of each retrying, short enough that the endpoint
# recovers quickly rather than staying whole a full period.
_ERROR_TTL = 30

# Entries expire this many seconds before their period ends, so the next poll of
# the same cadence always triggers a fresh scan. Expiry is measured from the
# REQUEST, so it does not care when the scan started; the margin only has to
# cover the gap between a poll and the entry it replaces. Must stay well below
# the smallest period.
_EXPIRY_MARGIN = 5

# --- optional self-update check (purely user-triggered) ----------------------
# The dashboard is otherwise fully offline: it makes a network request ONLY when
# the user clicks the "check update" button in the UI. A short debounce stops
# rapid re-clicks from hammering the registry. Disable entirely with
# --no-update-check / CCUSAGE_NO_UPDATE_CHECK=1.
PACKAGE_NAME = "@caius_kong/ccusage-dashboard"
UPDATE_CHECKS = os.environ.get("CCUSAGE_NO_UPDATE_CHECK", "").lower() not in ("1", "true", "yes")
UPDATE_REGISTRY = os.environ.get("CCUSAGE_REGISTRY", "https://registry.npmjs.org")
CHECK_DEBOUNCE = 10  # seconds; ignore bursts of clicks

_update_lock = threading.Lock()
_last_check = None  # (timestamp, result dict) — debounce + last outcome


def run_ccusage(args: list[str], ttl: float) -> dict:
    """Run ccusage and cache the JSON report under its argv for one `ttl` window.

    ``ttl`` here is a refresh PERIOD (the cadence the browser polls this report
    at), and the entry is made to expire just before that period elapses — see
    _EXPIRY_MARGIN. Expiry is measured from the REQUEST time (the tick), not from
    when the scan happened to start, so queueing behind other scans cannot push
    an entry past the next tick and silently double the cadence.

    Two failure modes this avoids:

    * Stamping the pre-run clock gave an entry a lifetime of `ttl - runtime`,
      i.e. negative when a run outlasted its period, so the cache never hit and
      every poll re-executed ccusage.
    * Anchoring to the wall clock ("end of the current minute") expires an entry
      on write whenever a run starts near a boundary.

    Post-condition for any run shorter than `ttl - _EXPIRY_MARGIN`: the entry is
    valid now, and stale by the next poll of the same cadence.

    Requests for the same key are coalesced (single-flight): the first caller
    runs ccusage and the rest wait for its result, so N concurrent misses spawn
    1 subprocess. A global semaphore additionally caps concurrent ccusage
    processes at _MAX_CONCURRENT_RUNS across all keys, so warm-up and browser
    polls cannot stack heavy scans on top of each other.
    """
    key = " ".join(args)
    while True:
        now = time.time()
        with _lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]  # type: ignore[return-value]
            waiter = _inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                _inflight[key] = waiter
                requested_at = now
                break
        waiter.wait()  # someone else is already running this exact key
    try:
        with _run_gate:
            try:
                proc = subprocess.run(
                    resolve_ccusage() + args,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                data = json.loads(proc.stdout)
            except Exception as exc:  # noqa: BLE001
                data = {"error": f"{exc}"}
        is_error = isinstance(data, dict) and bool(data.get("error"))
        window = _ERROR_TTL if is_error else ttl
        # Expire just before this period ends, measured from the REQUEST, so the
        # next poll of this cadence always finds it stale even if the scan was
        # queued behind others. Never let the margin swallow a short window.
        margin = min(_EXPIRY_MARGIN, window / 4)
        expires_at = requested_at + window - margin
        with _lock:
            _cache[key] = (expires_at, data)
        return data
    finally:
        with _lock:
            done = _inflight.pop(key, None)
        if done is not None:
            done.set()


def _read_current_version() -> str | None:
    """Version of the running package. Finds the nearest package.json whose name
    is ours (installed layout: <pkg>/package.json next to lib/; dev: repo root)."""
    try:
        for parent in APP_DIR.parents:
            pj = parent / "package.json"
            if pj.is_file():
                meta = json.loads(pj.read_text(encoding="utf-8"))
                if meta.get("name") == PACKAGE_NAME and meta.get("version"):
                    return str(meta["version"])
    except Exception:
        return None
    return None


def _num(s: str) -> int:
    m = re.match(r"\d+", s)
    return int(m.group()) if m else 0


def compare_versions(a: str, b: str) -> int:
    """Minimal numeric semver compare for simple x.y.z tags (no deps)."""
    pa = [_num(x) for x in re.sub(r"^v", "", a).split("-")[0].split(".")]
    pb = [_num(x) for x in re.sub(r"^v", "", b).split("-")[0].split(".")]
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else 0
        y = pb[i] if i < len(pb) else 0
        if x != y:
            return -1 if x < y else 1
    return 0


def _fetch_latest_version() -> str:
    """One GET to the npm registry's <latest> dist-tag endpoint."""
    pkg = PACKAGE_NAME.replace("/", "%2F")
    url = f"{UPDATE_REGISTRY.rstrip('/')}/{pkg}/latest"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"ccusage-dashboard/{_read_current_version() or 'unknown'}",
        },
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        return str(json.loads(resp.read().decode("utf-8"))["version"])


def check_now() -> dict:
    """One synchronous registry lookup, triggered by the user's click. Debounced
    briefly so double-clicks don't hit the registry twice; never runs on its own."""
    global _last_check
    if not UPDATE_CHECKS:
        return {"disabled": True, "outdated": False}
    now = time.time()
    with _update_lock:
        if _last_check and now - _last_check[0] < CHECK_DEBOUNCE:
            return _last_check[1]
    current = _read_current_version()
    try:
        latest = _fetch_latest_version()
        result = {
            "current": current,
            "latest": latest,
            "outdated": bool(current) and bool(latest) and compare_versions(latest, current) > 0,
        }
    except Exception:
        result = {"current": current, "latest": None, "outdated": False, "error": "unreachable"}
    with _update_lock:
        _last_check = (time.time(), result)
    return result


def pick_all_row(rows: list[dict]) -> dict:
    return next((r for r in rows if r.get("agent") == "all"), None) or (rows[0] if rows else {})


def summarize(row: dict) -> dict:
    models = sorted(row.get("modelBreakdowns", []), key=lambda m: -m.get("cost", 0))
    total_from_models = sum(m.get("cost", 0) for m in models)
    return {
        "period": row.get("period") or row.get("date"),
        "totalCost": round(row.get("totalCost", total_from_models) or 0, 4),
        "inputTokens": row.get("inputTokens", 0),
        "outputTokens": row.get("outputTokens", 0),
        "cacheReadTokens": row.get("cacheReadTokens", 0),
        "cacheCreationTokens": row.get("cacheCreationTokens", 0),
        "models": [
            {
                "name": m.get("modelName") or m.get("name") or "?",
                "cost": round(m.get("cost", 0), 4),
                "inputTokens": m.get("inputTokens", 0),
                "outputTokens": m.get("outputTokens", 0),
                "cacheReadTokens": m.get("cacheReadTokens", 0),
                "cacheCreationTokens": m.get("cacheCreationTokens", 0),
            }
            for m in models
        ],
    }


def aggregate_range(data: dict, from_date: str, to_date: str) -> dict:
    """Merge per-day rows (daily range report) into one period summary."""
    rows = data.get("daily", []) or []
    agg: dict[str, dict] = {}
    total_cost = 0.0
    keys = ("cost", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens")
    for r in rows:
        if r.get("agent") != "all":
            continue
        total_cost += r.get("totalCost", 0) or 0
        for m in r.get("modelBreakdowns", []):
            name = m.get("modelName") or "?"
            t = agg.setdefault(name, {"name": name, **{k: 0 for k in keys}})
            for k in keys:
                t[k] += m.get(k, 0) or 0
    row = pick_all_row(rows)
    models = sorted(agg.values(), key=lambda m: -m["cost"])
    return {
        "period": f"{from_date} → {to_date}",
        "totalCost": round(total_cost, 4),
        "inputTokens": sum(r.get("inputTokens", 0) for r in rows if r.get("agent") == "all"),
        "outputTokens": sum(r.get("outputTokens", 0) for r in rows if r.get("agent") == "all"),
        "cacheReadTokens": sum(r.get("cacheReadTokens", 0) for r in rows if r.get("agent") == "all"),
        "cacheCreationTokens": sum(r.get("cacheCreationTokens", 0) for r in rows if r.get("agent") == "all"),
        "models": models,
        "dayCount": len([r for r in rows if r.get("agent") == "all"]),
    }


def trend(days: int = 30) -> dict:
    end = date.today()
    start = end - timedelta(days=days - 1)
    data = run_ccusage(
        ["daily", "--since", start.isoformat(), "--until", end.isoformat(), "--json", "--offline"],
        TTL["/api/trend"],
    )
    rows = data.get("daily", []) or []
    by_period = {r.get("period"): (r.get("totalCost", 0) or 0) for r in rows if r.get("agent") == "all"}
    out, d = [], start
    while d <= end:
        iso = d.isoformat()
        out.append({"period": iso, "totalCost": round(by_period.get(iso, 0.0), 4)})
        d += timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(), "days": out}


def sessions(since_days: int = 30, period: str = "", from_date: str = "", to_date: str = "") -> dict:
    """Return ccusage's session report, each with its real workdir (dirName) when
    the agent's local session store records one. Every numeric cost/token field
    comes 100% from ccusage; the workdir is only a display label.

    Filtering: pass `period` in ("today" | "week" | "month") OR an explicit
    date range via `from_date`/`to_date` (YYYY-MM-DD). `since_days` is kept for
    backward compatibility with the old 7d/30d/90d/all selector.

    The window is passed to ccusage as --since/--until so it filters by usage
    EVENT date (entry.date) before summarising, exactly like the CLI session
    report. Filtering rows ourselves by lastActivity would keep a whole session's
    lifetime cost even when only a few events fall in the window, inflating the
    total (observed: today $229 vs ccusage's true $0.09).
    """
    today = date.today()
    lo = hi = None
    if from_date and to_date:
        try:
            lo = date.fromisoformat(from_date)
            hi = date.fromisoformat(to_date)
        except ValueError:
            lo = hi = None
    elif period == "today":
        lo = hi = today
    elif period == "week":
        lo = today - timedelta(days=6)
        hi = today
    elif period == "month":
        lo = today.replace(day=1)
        hi = today
    elif period == "custom":
        pass  # fall through to since_days below (legacy)
    if lo is None:
        lo = today - timedelta(days=since_days)
        hi = today

    ccusage_args = ["session", "--json", "--offline"]
    if lo and hi:
        ccusage_args += ["--since", lo.isoformat(), "--until", hi.isoformat()]
    # The sessions panel follows the active view, so its cache lifetime must
    # match that view's cadence: the today view is polled every minute, the
    # others every _SLOW_INTERVAL. Using the slow TTL for a today window would
    # keep the panel a minute stale behind its own refresh.
    sessions_ttl = TTL["/api/today"] if period == "today" else TTL["/api/sessions"]
    data = run_ccusage(ccusage_args, sessions_ttl)
    rows = data.get("session") or []

    out = []
    for r in rows:
        meta = r.get("metadata") or {}
        sid = r.get("period")
        last_activity = meta.get("lastActivity") or ""
        project_raw = meta.get("projectPath") or ""
        agent_name = r.get("agent", "?")
        cwd = ""
        dir_name = ""
        if sid:
            disk_cwd = _cwd_for(agent_name, sid, project_raw)
            if disk_cwd:
                cwd = disk_cwd
                cwd_base = disk_cwd.rstrip("/").split("/")[-1]
                if cwd_base:
                    dir_name = cwd_base
        if not cwd:
            cwd = _decode_cwd(project_raw)
            dir_name = _basename(project_raw)
        out.append(
            {
                "id": sid or "",
                "agent": agent_name,
                "cost": round(r.get("totalCost", 0) or 0, 4),
                "inputTokens": r.get("inputTokens", 0),
                "outputTokens": r.get("outputTokens", 0),
                "cacheReadTokens": r.get("cacheReadTokens", 0),
                "cacheCreationTokens": r.get("cacheCreationTokens", 0),
                "lastActivity": last_activity,
                "cwd": cwd,
                "dirName": dir_name,
                "projectKey": project_raw,
                "hasCwd": bool(cwd),
            }
        )
    out.sort(key=lambda s: s["cost"], reverse=True)
    total_cost = round(sum(s["cost"] for s in out), 4)
    return {"total": len(out), "totalCost": total_cost, "sessions": out}


def _pi_cwd_from_disk(project_key: str):
    """Best-effort real workdir for a pi session project.

    pi stores sessions under ~/.pi/agent/sessions/<projectKey>/<ts>_<id>.jsonl,
    whose first line carries an authoritative "cwd". Reading it lets the dashboard
    show the REAL dir name (e.g. "fund-tracker") instead of the lossy basename that
    the encoded projectPath produces ("tracker").

    This only supplies the DISPLAY label — costs/tokens still come from ccusage.
    Returns None (caller falls back) if the dir/file is missing or unreadable.
    """
    import glob

    try:
        root = Path.home() / ".pi" / "agent" / "sessions" / project_key
        if not root.is_dir():
            return None
        candidates = sorted(glob.glob(str(root / "*.jsonl")))
        for path in candidates:
            cwd = _cwd_from_jsonl(path)
            if cwd:
                return cwd
    except Exception:
        return None
    return None


# --- Unified authoritative-workdir resolution for every agent that stores one ---
#
# ccusage's session report only exposes projectPath for pi (and it's lossy for
# hyphenated dir names). To show the REAL dir name for all sessions we resolve the
# workdir from each agent's own local session store. This drives only the DISPLAY
# label (dirName); every numeric cost/token value still comes 100% from ccusage.

_CWD_CACHE = {}      # "agent\x1fsid" -> cwd | None
_AGENT_INDEX = {}     # agent -> {session_id: [paths]} (built lazily)


def _cwd_from_jsonl(path):
    """Scan a session .jsonl (first few lines) for the first real 'cwd' string."""
    try:
        with open(path, encoding="utf-8") as fh:
            for _ in range(80):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        return cwd.strip().rstrip("/")
    except (OSError, ValueError):
        return None
    return None


def _codex_cwd(path):
    """codex stores cwd inside the session_meta payload, not top-level."""
    try:
        with open(path, encoding="utf-8") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    cwd = (rec.get("payload") or {}).get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        return cwd.strip().rstrip("/")
    except (OSError, ValueError):
        return None
    return None


def _agent_index(agent):
    """Build (once) a {session_id: [paths]} for an agent's local session store."""
    if agent in _AGENT_INDEX:
        return _AGENT_INDEX[agent]
    home = Path.home()
    idx = {}
    if agent == "openclaw":
        base = home / ".openclaw" / "agents" / "main" / "sessions"
        for f in (base.glob("*.jsonl") if base.is_dir() else []):
            idx.setdefault(f.name[:-6], []).append(str(f))
    elif agent == "claude":
        proj = home / ".claude" / "projects"
        if proj.is_dir():
            for sub in proj.iterdir():
                if sub.is_dir():
                    for f in sub.glob("*.jsonl"):
                        idx.setdefault(f.name[:-6], []).append(str(f))
    elif agent == "codex":
        root = home / ".codex" / "sessions"
        if root.is_dir():
            for f in root.glob("*/*/*/rollout-*.jsonl"):
                idx.setdefault(f.name[len("rollout-"):-len(".jsonl")], []).append(str(f))
            for f in root.glob("*/*/rollout-*.jsonl"):
                idx.setdefault(f.name[len("rollout-"):-len(".jsonl")], []).append(str(f))
    _AGENT_INDEX[agent] = idx
    return idx


def _session_to_cwd(agent, sid):
    """Locate an agent's session file by id and return its real cwd (or None)."""
    paths = _agent_index(agent).get(sid)
    if not paths:
        return None
    for p in paths:
        cwd = _codex_cwd(p) if agent == "codex" else _cwd_from_jsonl(p)
        if cwd:
            return cwd
    return None


def _cwd_for(agent, sid, project_key=""):
    """Real workdir for a session. pi resolves by project key; the rest by session id.
    Returns the real cwd (or None if unresolvable). Only drives the DISPLAY label."""
    if agent == "pi":
        if not project_key:
            return None
        key = "pi\x1f" + project_key
        if key in _CWD_CACHE:
            return _CWD_CACHE[key]
        cwd = _pi_cwd_from_disk(project_key)
        _CWD_CACHE[key] = cwd
        return cwd
    if not sid:
        return None
    # codex session ids are date-path prefixed ("2025/10/17/rollout-<id>"); the
    # local file is named "rollout-<id>.jsonl", so match on the final segment.
    lookup_sid = sid.split("/")[-1]
    if lookup_sid.startswith("rollout-") and agent == "codex":
        lookup_sid = lookup_sid[len("rollout-"):]
    key = agent + "\x1f" + lookup_sid
    if key in _CWD_CACHE:
        return _CWD_CACHE[key]
    cwd = _session_to_cwd(agent, lookup_sid)
    _CWD_CACHE[key] = cwd
    return cwd


def _decode_cwd(raw: str) -> str:
    """Best-effort decode of ccusage's projectPath into a readable path.

    pi's projectPath is a Claude-Code-style encoded dir name where '/' became '-',
    and literal '-' inside a dir name is NOT distinguishable from the separator
    (encoding is lossy). So this is approximate: 'fund-tracker' may come back as
    'fund/tracker'. The authoritative string is the raw projectPath itself.
    Example: '--Users-caius-kong-Documents-...-AutoTrans--' -> /Users/caius_kong/.../AutoTrans
    """
    if not raw:
        return ""
    parts = [p for p in raw.replace("-", "/").split("/") if p]
    if not parts:
        return ""
    return "/" + "/".join(parts)


def _basename(raw: str) -> str:
    """Last path segment of projectPath as a readable dir name.

    Works even for encoded paths (the trailing segment is unaffected by the
    dash encoding except when the real dir name itself contains '-'s).
    """
    if not raw:
        return ""
    cleaned = raw.strip("/-\\")
    if not cleaned:
        return ""
    parts = cleaned.replace("-", "/").split("/")
    last = parts[-1] if parts else cleaned
    # If the real dir name itself got dash-encoded (e.g. 'my-proj' -> 'my_proj'),
    # we can't perfectly recover it; fall back to a reasonable label.
    return last or cleaned


class Handler(BaseHTTPRequestHandler):
    server_version = "ccusage-ui/0.2"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict) -> None:
        self._send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path in ("/", "/index.html"):
            self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json({"ok": True, "budget": BUDGET})
            return
        if path == "/api/update":
            self._json(check_now())
            return

        if path == "/api/today":
            data = run_ccusage(["daily", "--last", "1", "--json", "--offline"], TTL[path])
            rows_key, summary = "daily", summarize(pick_all_row(data.get("daily", [])))
        elif path == "/api/week":
            data = run_ccusage(["weekly", "--last", "1", "--json", "--offline"], TTL[path])
            summary = summarize(pick_all_row(data.get("weekly", [])))
        elif path == "/api/month":
            data = run_ccusage(["monthly", "--last", "1", "--json", "--offline"], TTL[path])
            summary = summarize(pick_all_row(data.get("monthly", [])))
            summary["budget"] = BUDGET
            summary["budgetUsedPct"] = round((summary["totalCost"] / BUDGET) * 100, 1) if BUDGET else 0
        elif path == "/api/range":
            frm, to = params.get("from", ""), params.get("to", "")
            if not frm or not to:
                self._json({"error": "from/to required (YYYY-MM-DD)"})
                return
            data = run_ccusage(["daily", "--since", frm, "--until", to, "--json", "--offline"], TTL[path])
            summary = aggregate_range(data, frm, to)
        elif path == "/api/trend":
            days = max(1, min(366, int(params.get("days", "30"))))
            self._json(trend(days))
            return
        elif path == "/api/sessions":
            period = params.get("period", "")
            frm, to = params.get("from", ""), params.get("to", "")
            days = max(1, min(366, int(params.get("days", "30"))))
            self._json(sessions(days, period, frm, to))
            return
        else:
            self._send(404, b"not found", "text/plain")
            return

        if isinstance(data, dict) and data.get("error"):
            self._json({"error": data["error"]})
            return
        self._json(summary)


def main() -> None:
    global BUDGET, _CCUSAGE_PATH_OVERRIDE, UPDATE_CHECKS
    parser = argparse.ArgumentParser(description="ccusage dashboard")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--budget", type=float, default=None, help="monthly budget cap in USD (default 300)")
    parser.add_argument("--ccusage-path", default=None, help="explicit path to a ccusage binary or src/cli.js")
    parser.add_argument("--no-warm", action="store_true", help="skip background warm-up (first requests may be slow)")
    parser.add_argument("--no-update-check", action="store_true", help="disable the npm version hint entirely")
    args = parser.parse_args()

    BUDGET = args.budget if args.budget is not None else float(os_env_budget() or 300.0)
    _CCUSAGE_PATH_OVERRIDE = args.ccusage_path
    if args.no_update_check:
        UPDATE_CHECKS = False
    print(f"using ccusage → {' '.join(resolve_ccusage())}", flush=True)

    def warm():
        # Fire concurrently and let _run_gate bound how many actually run at once;
        # serializing here would make a cold start pay for all the scans
        # back-to-back for no benefit.
        #
        # Only reports the browser asks for on load are pre-warmed: the default
        # view (today), the two always-visible panels (budget=month, 30-day
        # trend). week/range are omitted on purpose — the UI only fetches them
        # when the user switches to that tab, so warming them would be a wasted
        # full-history scan at every boot.
        jobs = [
            lambda: run_ccusage(["daily", "--last", "1", "--json", "--offline"], TTL["/api/today"]),
            lambda: run_ccusage(["monthly", "--last", "1", "--json", "--offline"], TTL["/api/month"]),
            lambda: trend(30),
        ]
        threads = [threading.Thread(target=job) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # warm the caches in the background so the server is reachable immediately;
    # a page load arriving mid-warm-up joins the matching in-flight run (see
    # single-flight in run_ccusage) instead of starting a duplicate scan.
    if not args.no_warm:
        print("warming ccusage caches in background…", flush=True)
        threading.Thread(target=warm, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"ccusage-ui → {url}  (monthly budget ${BUDGET:g}, Ctrl+C to stop)", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)


def os_env_budget() -> str:
    import os

    return os.environ.get("CCUSAGE_BUDGET", "")


if __name__ == "__main__":
    main()