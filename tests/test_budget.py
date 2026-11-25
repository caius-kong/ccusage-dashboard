#!/usr/bin/env python3
"""Regression tests for the page-editable monthly budget.

The cap is the dashboard's own setting (not a ccusage number) and the page can
edit it, so it must validate hostile input, persist across a restart, and only be
writable through the JSON endpoint. These tests never invoke ccusage.

Run with:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import server  # noqa: E402


class BudgetValueTest(unittest.TestCase):
    def test_accepts_numbers_and_numeric_strings(self):
        self.assertEqual(server.validate_budget(300), 300.0)
        self.assertEqual(server.validate_budget("500.5"), 500.5)
        self.assertEqual(server.validate_budget(0), 0.0)  # 0 = alert off

    def test_rejects_bools_nonsense_and_out_of_range(self):
        for bad in (True, False, -1, "abc", None, float("nan"), float("inf"), 1_000_001):
            with self.assertRaises(ValueError, msg=repr(bad)):
                server.validate_budget(bad)

    def test_roundtrip_and_tolerates_missing_or_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "config.json"
            self.assertIsNone(server.load_saved_budget(path))
            server.save_budget(742.5, path)
            self.assertEqual(server.load_saved_budget(path), 742.5)
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(server.load_saved_budget(path))
            path.write_text(json.dumps({"budget": -5}), encoding="utf-8")
            self.assertIsNone(server.load_saved_budget(path))


class BudgetEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.json"
        self._orig_path = server._config_path
        self._orig_budget = server.BUDGET
        server._config_path = lambda: self.config
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/budget"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server._config_path = self._orig_path
        server.BUDGET = self._orig_budget
        self.tmp.cleanup()

    def post(self, body: bytes, content_type: str = "application/json"):
        req = urllib.request.Request(
            self.url, data=body, method="POST", headers={"Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_saves_to_memory_and_disk(self):
        status, payload = self.post(b'{"budget": 450}')
        self.assertEqual(status, 200)
        self.assertEqual(payload["budget"], 450.0)
        self.assertEqual(server.BUDGET, 450.0)
        self.assertEqual(json.loads(self.config.read_text())["budget"], 450.0)

    def test_rejects_bad_value_and_non_json_body(self):
        status, payload = self.post(b'{"budget": -1}')
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, _ = self.post(b'{"budget": 500}', "text/plain")
        self.assertEqual(status, 415)
        status, _ = self.post(b"[]")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
