#!/usr/bin/env python3
"""
ccusage-ui — a tiny zero-dependency local dashboard for ccusage.

It shells out to ccusage's JSON reports and renders:
  - today / this week / this month / custom range, grouped by model
  - a 30-day cost trend chart
  - a monthly budget alert (default cap: $300)

Because all numbers come from ccusage itself, the figures always match what
`ccusage` reports (the source you already trust). No network, no pricing table
to maintain, no third-party packages — only the Python standard library.

Usage:
    python3 server.py [--port 8799] [--budget 300] [--open]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# Cache: args-key -> (expires_at, data)
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def resolve_ccusage() -> list[str]:
    """Resolve a fast local ccusage entrypoint.

    Prefer a directly invocable local binary/cache so we don't pay an `npx`
    resolution + possible network fetch on every report (~5-7s cold). Look for,
    in order: an explicit --ccusage-path, a global `ccusage` on PATH, the npm
    npx cache, the bun cache, then fall back to `npx --yes ccusage@latest`.
    Returns a command list to run.
    """
    def _npx_cached_cli() -> str | None:
        for base in ("npm", "bun"):
            roots = []
            if base == "npm":
                target = Path.home() / ".npm/_npx"
                if target.is_dir():
                    roots.extend(p for p in target.glob("*/node_modules/ccusage/src/cli.js"))
                # also newer npm layout
                roots.extend((Path.home() / ".npm/_npx").glob("*/node_modules/ccusage/src/cli.js"))
            else:
                target = Path.home() / ".bun/install/cache"
                if target.is_dir():
                    roots.extend(p for p in target.glob("ccusage*/src/cli.js"))
            # prefer highest semver-ish (sort by version suffix descending)
            def _ver(p: Path) -> tuple:
                s = p.as_posix()
                m = re.search(r"ccusage@?(\d+)\.(\d+)\.(\d+)", s)
                return tuple(map(int, m.groups())) if m else (0, 0, 0)
            best = max(roots, key=_ver, default=None)
            if best:
                return str(best)
        return None

    explicit = _CCUSAGE_PATH_OVERRIDE
    if explicit:
        return [explicit]
    p = shutil.which("ccusage")
    if p:
        return [p]
    # Bundled ccusage dependency: walk up from the package to find node_modules/ccusage
    for parent in APP_DIR.parents:
        bundled = parent / "node_modules" / "ccusage" / "src" / "cli.js"
        if bundled.exists():
            return [_node(), str(bundled)]
    cli = _npx_cached_cli()
    if cli:
        return [_node(), str(cli)]
    return ["npx", "--yes", "ccusage@latest"]


_CCUSAGE_PATH_OVERRIDE: str | None = None


def _node() -> str:
    import shutil as _s

    n = _s.which("node") or "node"
    return n

BUDGET = 300.0  # monthly cap in USD (override via --budget or CCUSAGE_BUDGET)
TTL = {"/api/today": 15, "/api/week": 60, "/api/month": 60, "/api/range": 120, "/api/trend": 120}


def run_ccusage(args: list[str], ttl: float) -> dict:
    key = " ".join(args)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]  # type: ignore[return-value]
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
    with _lock:
        _cache[key] = (now + ttl, data)
    return data


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
        else:
            self._send(404, b"not found", "text/plain")
            return

        if isinstance(data, dict) and data.get("error"):
            self._json({"error": data["error"]})
            return
        self._json(summary)


def main() -> None:
    global BUDGET, _CCUSAGE_PATH_OVERRIDE
    parser = argparse.ArgumentParser(description="ccusage dashboard")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--budget", type=float, default=None, help="monthly budget cap in USD (default 300)")
    parser.add_argument("--ccusage-path", default=None, help="explicit path to a ccusage binary or src/cli.js")
    parser.add_argument("--no-warm", action="store_true", help="skip blocking warm-up (first requests may be slow)")
    parser.add_argument("--open", action="store_true", help="open browser after start")
    args = parser.parse_args()

    BUDGET = args.budget if args.budget is not None else float(os_env_budget() or 300.0)
    _CCUSAGE_PATH_OVERRIDE = args.ccusage_path
    print(f"using ccusage → {' '.join(resolve_ccusage())}")

    def warm_one(fn):
        fn()

    def warm():
        threads = [
            threading.Thread(target=warm_one, args=(lambda: run_ccusage(["daily", "--last", "1", "--json", "--offline"], TTL["/api/today"]),)),
            threading.Thread(target=warm_one, args=(lambda: run_ccusage(["weekly", "--last", "1", "--json", "--offline"], TTL["/api/week"]),)),
            threading.Thread(target=warm_one, args=(lambda: run_ccusage(["monthly", "--last", "1", "--json", "--offline"], TTL["/api/month"]),)),
            threading.Thread(target=warm_one, args=(lambda: trend(30),)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    if not args.no_warm:
        print("warming ccusage caches (first load will be instant after this)…", flush=True)
        warm()
        print("warm-up complete.")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ccusage-ui → http://{args.host}:{args.port}  (monthly budget ${BUDGET:g}, Ctrl+C to stop)")
    if args.open:
        import webbrowser

        webbrowser.open(f"http://{args.host}:{args.port}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def os_env_budget() -> str:
    import os

    return os.environ.get("CCUSAGE_BUDGET", "")


if __name__ == "__main__":
    main()