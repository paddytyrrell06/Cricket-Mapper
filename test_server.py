import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import server as S
from runtime import MarketRuntime
from test_runtime import write_runtime_snapshot


class RefreshControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / "output" / "data").mkdir(parents=True)
        self.report = self.root / "output" / S.REPORT_NAME
        self.report.write_text("previous audited report")

    def test_refresh_uses_fixed_command_and_preserves_saved_sport_scope(self):
        (self.root / "output" / "data" / "snapshot_meta.json").write_text(
            json.dumps({"sports": ["cricket", "tennis", "cricket"]}))
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "success")

        refresh = S.QuoteRefresh(self.root, runner=runner)
        self.assertFalse(refresh.request()["coalesced"])
        refresh.wait(2)
        command, kwargs = calls[0]
        self.assertEqual(command, [sys.executable, str(self.root / "run.py"),
                                  "--snapshot-dir=output/data", "--refresh-quotes",
                                  "--no-open", "--sports=cricket,tennis"])
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], S.REFRESH_TIMEOUT_SECONDS)
        state = refresh.status()
        self.assertEqual((state["state"], state["running"], state["revision"]),
                         ("idle", False, 1))
        self.assertIsNotNone(state["last_success_at"])
        state["revision"] = 99
        self.assertEqual(refresh.status()["revision"], 1)

    def test_simultaneous_requests_share_one_refresh_and_next_request_can_run(self):
        started, finish = threading.Event(), threading.Event()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            started.set()
            if not finish.wait(2):
                raise AssertionError("Test did not release refresh")
            return subprocess.CompletedProcess(command, 0)

        refresh = S.QuoteRefresh(self.root, runner=runner)
        self.addCleanup(finish.set)
        refresh.request()
        self.assertTrue(started.wait(2))
        replies = []
        requests = [threading.Thread(target=lambda: replies.append(refresh.request()))
                    for _ in range(8)]
        for request in requests:
            request.start()
        for request in requests:
            request.join(2)
        self.assertEqual(len(replies), 8)
        self.assertTrue(all(reply["coalesced"] and reply["running"] for reply in replies))
        self.assertEqual(len(calls), 1)
        finish.set()
        refresh.wait(2)
        self.assertFalse(refresh.request()["coalesced"])
        refresh.wait(2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(refresh.status()["revision"], 2)

    def test_failure_keeps_previous_publication_and_last_success(self):
        results = iter([0, 7, 0])

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, next(results), "mock quote failure")

        refresh = S.QuoteRefresh(self.root, runner=runner)
        refresh.request()
        refresh.wait(2)
        success = refresh.status()["last_success_at"]
        refresh.request()
        refresh.wait(2)
        state = refresh.status()
        self.assertEqual((state["state"], state["running"], state["revision"]),
                         ("error", False, 1))
        self.assertIn("status 7", state["error"])
        self.assertEqual(state["last_success_at"], success)
        self.assertEqual(self.report.read_text(), "previous audited report")
        refresh.request()
        refresh.wait(2)
        self.assertEqual(refresh.status()["state"], "idle")
        self.assertIsNone(refresh.status()["error"])

    def test_timeout_becomes_recoverable_error(self):
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        refresh = S.QuoteRefresh(self.root, runner=runner)
        refresh.request()
        refresh.wait(2)
        self.assertIn("timed out", refresh.status()["error"])
        self.assertFalse(refresh.status()["running"])
        self.assertEqual(self.report.read_text(), "previous audited report")

    def test_invalid_saved_scope_cannot_become_command_options(self):
        for sports in (["--no-open"], "cricket", [], ["cricket,tennis"], [None]):
            with self.subTest(sports=sports):
                (self.root / "output" / "data" / "snapshot_meta.json").write_text(
                    json.dumps({"sports": sports}))
                with self.assertRaisesRegex(ValueError, "sport scope"):
                    S.refresh_command(self.root)

    def test_missing_discovery_does_not_launch_process(self):
        (self.root / "output" / "data").rmdir()
        calls = []
        refresh = S.QuoteRefresh(self.root, runner=lambda *args, **kwargs: calls.append(args))
        refresh.request()
        refresh.wait(2)
        self.assertEqual(calls, [])
        self.assertIn("run python3 run.py", refresh.status()["error"])


class TerminalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "output" / "data").mkdir(parents=True)
        (self.root / "output" / S.REPORT_NAME).write_text("<html>audited terminal</html>")
        (self.root / "output" / S.VERSION_NAME).write_text("window.version('123');")
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []

        def runner(command, **kwargs):
            self.calls.append(command)
            self.started.set()
            self.release.wait(3)
            return subprocess.CompletedProcess(command, 0)

        self.refresh = S.QuoteRefresh(self.root, runner=runner)
        self.server = S.TerminalServer(0, root=self.root, refresh=self.refresh)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.release.set()
        self.refresh.wait(3)
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.directory.cleanup()

    def request(self, method="GET", path="/", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port,
                                                timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def refresh_headers(self, **overrides):
        return {"Origin": self.origin, "Content-Type": "application/json",
                "X-Terminal-Request": "refresh", **overrides}

    def test_report_and_version_are_uncached_and_status_is_read_only(self):
        for path in ("/", "/" + S.REPORT_NAME):
            status, headers, body = self.request(path=path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn(b"audited terminal", body)
        status, headers, body = self.request(path="/" + S.VERSION_NAME + "?check=123")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/javascript"))
        self.assertEqual(body, b"window.version('123');")
        status, _, body = self.request(path="/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "idle")
        self.assertEqual(self.calls, [])

    def test_same_origin_refresh_coalesces_and_publishes_status(self):
        headers = self.refresh_headers()
        status, _, body = self.request("POST", "/api/refresh", "{}", headers)
        self.assertEqual(status, 202)
        self.assertFalse(json.loads(body)["coalesced"])
        self.assertTrue(self.started.wait(2))
        status, _, body = self.request("POST", "/api/refresh", "{}", headers)
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(body)["coalesced"])
        self.assertEqual(len(self.calls), 1)
        self.release.set()
        self.refresh.wait(2)
        status, _, body = self.request(path="/api/status")
        self.assertEqual(json.loads(body)["revision"], 1)

    def test_cross_origin_or_unmarked_requests_cannot_refresh(self):
        for headers in ({}, self.refresh_headers(Origin="https://attacker.example"),
                        self.refresh_headers(Origin="null"),
                        self.refresh_headers(**{"X-Terminal-Request": "other"}),
                        self.refresh_headers(**{"Sec-Fetch-Site": "cross-site"})):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/refresh", "{}", headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_host_guard_blocks_dns_rebinding_and_local_origin_mismatch(self):
        for method, path in (("GET", "/"), ("GET", "/api/status"),
                             ("POST", "/api/refresh")):
            headers = self.refresh_headers(Host="attacker.example")
            status, _, _ = self.request(method, path, "{}" if method == "POST" else None,
                                        headers)
            self.assertEqual(status, 403)
        headers = self.refresh_headers(Origin=f"http://localhost:{self.server.server_port}")
        self.assertEqual(self.request("POST", "/api/refresh", "{}", headers)[0], 403)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_client_cannot_submit_commands_or_paths(self):
        for body in ('{"command":"rm"}', '{"sports":["tennis"]}',
                     '{"path":"run.py"}', '[]', 'null', 'broken', '"' + 'x' * 1100 + '"'):
            with self.subTest(body=body[:50]):
                status, _, _ = self.request("POST", "/api/refresh", body,
                                            self.refresh_headers())
                self.assertEqual(status, 400)
        self.assertEqual(self.request("POST", "/api/refresh", "{}",
                                     self.refresh_headers(**{"Content-Type": "text/plain"}))[0], 415)
        self.assertEqual(self.calls, [])

    def test_only_public_report_assets_are_served(self):
        for path in ("/run.py", "/data/snapshot_meta.json", "/../run.py", "/%2e%2e/run.py",
                     "/api/refresh"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request("POST", "/api/unknown", "{}",
                                     self.refresh_headers())[0], 404)

    def test_publication_gap_serves_previous_audited_report(self):
        (self.root / "output").replace(self.root / ".cricket_matching_previous_output")
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"audited terminal", body)
        # A previously loaded asset remains readable even during two missing paths.
        (self.root / ".cricket_matching_previous_output" / S.REPORT_NAME).unlink()
        self.assertEqual(self.request()[2], body)

    def test_missing_report_is_clear_and_head_does_not_start_refresh(self):
        (self.root / "output" / S.REPORT_NAME).unlink()
        status, _, body = self.request()
        self.assertEqual(status, 503)
        self.assertIn(b"run python3 run.py", body)
        status, _, body = self.request("HEAD", "/api/status")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(self.calls, [])


class RuntimeHTTPTests(unittest.TestCase):
    request = TerminalHTTPTests.request
    refresh_headers = TerminalHTTPTests.refresh_headers

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        write_runtime_snapshot(self.root)
        self.calls = []
        def collector(markets, raw, **kwargs):
            self.calls.append([market.market_id for market in markets])
            return [], []
        self.runtime = MarketRuntime(self.root, collector=collector)
        self.refresh = S.QuoteRefresh(self.root, runtime=self.runtime)
        self.server = S.TerminalServer(0, root=self.root, refresh=self.refresh, runtime=self.runtime)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.refresh.wait(3)
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.directory.cleanup()

    def test_runtime_reads_are_uncached_and_same_revision_query_is_a_heartbeat(self):
        status, headers, body = self.request(path="/api/markets")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        first = json.loads(body)
        self.assertEqual(first["mode"], "runtime_rest")
        self.assertEqual(len(first["records"]), 2)
        self.assertFalse(first["partial"])
        _, _, body = self.request(path="/api/markets?revision=" + str(first["revision"]))
        self.assertIsNone(json.loads(body)["records"])
        self.assertEqual(self.calls, [])
        status, _, body = self.request("HEAD", "/api/markets")
        self.assertEqual((status, body), (200, b""))

    def test_invalid_or_future_revision_requests_get_a_complete_resynchronization(self):
        for revision in ("broken", "-5", "999999", "", "1.5"):
            with self.subTest(revision=revision):
                status, _, body = self.request(path="/api/markets?revision=" + revision)
                self.assertEqual(status, 200)
                result = json.loads(body)
                self.assertFalse(result["partial"])
                self.assertEqual(len(result["records"]), 2)

    def test_consecutive_revision_sends_delta_over_the_http_api(self):
        previous = self.runtime.revision
        self.runtime.refresh()
        status, _, body = self.request(path="/api/markets?revision=" + str(previous))
        self.assertEqual(status, 200)
        update = json.loads(body)
        self.assertTrue(update["partial"])
        self.assertEqual(update["records"], [])
        self.assertEqual(len(update["heartbeats"]), 2)
        self.assertEqual(update["revision"], previous + 1)

    def test_detail_route_reads_cached_current_row_and_validates_the_index(self):
        status, _, body = self.request(path="/api/market/0?revision=ignored")
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertEqual(detail["revision"], self.runtime.revision)
        self.assertIn("Alpha Academy vs Beta Academy", detail["details"])
        self.assertEqual(self.calls, [])
        for index in ("-1", "not-a-number", "100", ""):
            self.assertEqual(self.request(path="/api/market/" + index)[0], 404)

    def test_settings_update_scanner_and_run_a_runtime_refresh(self):
        watched = self.runtime.snapshot()["records"][0]["key"]
        body = json.dumps({"min_liquidity_usd": "250", "watch_only": True,
                           "watchlist": [watched, "unknown"]})
        status, _, response = self.request("POST", "/api/settings", body, self.refresh_headers())
        self.assertEqual(status, 202)
        settings = json.loads(response)["settings"]
        self.assertEqual(settings, {"min_liquidity_usd": "250", "watch_only": True, "watchlist": [watched]})
        self.refresh.wait(2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]), 2)
        status, _, body = self.request(path="/api/status")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual((state["mode"], state["state"], state["revision"]),
                         ("runtime_rest", "idle", self.runtime.revision))

    def test_invalid_settings_or_cross_origin_requests_cannot_mutate_runtime(self):
        before = self.runtime.settings()
        invalid = ({"command": "anything"}, {"min_liquidity_usd": "NaN"},
                   {"watch_only": 1}, {"watchlist": [False]}, [], None)
        for value in invalid:
            with self.subTest(value=value):
                status, _, _ = self.request("POST", "/api/settings", json.dumps(value), self.refresh_headers())
                self.assertEqual(status, 400)
        self.assertEqual(self.request("POST", "/api/settings", '{"watch_only":true}',
                                     self.refresh_headers(Origin="https://example.com"))[0], 403)
        self.assertEqual(self.runtime.settings(), before)
        self.assertEqual(self.calls, [])

    def test_runtime_refresh_endpoint_updates_cache_without_requiring_an_html_publication(self):
        previous = self.runtime.revision
        status, _, _ = self.request("POST", "/api/refresh", "{}", self.refresh_headers())
        self.assertEqual(status, 202)
        self.refresh.wait(2)
        self.assertEqual(self.runtime.revision, previous + 1)
        self.assertFalse((self.root / "output" / S.REPORT_NAME).exists())
        self.assertEqual(self.refresh.status()["state"], "idle")

    def test_runtime_failure_preserves_last_visible_frame_and_can_recover(self):
        previous = self.runtime.snapshot()
        def fail(markets, *_args, **_kwargs):
            markets[0].bid = "0.99"
            raise OSError("mock network failure")
        original = self.runtime.collector
        self.runtime.collector = fail
        self.request("POST", "/api/refresh", "{}", self.refresh_headers())
        self.refresh.wait(2)
        self.assertEqual(self.refresh.status()["state"], "error")
        self.assertEqual(self.runtime.snapshot(), previous)
        self.runtime.collector = original
        self.request("POST", "/api/refresh", "{}", self.refresh_headers())
        self.refresh.wait(2)
        self.assertEqual(self.refresh.status()["state"], "idle")
        self.assertGreater(self.runtime.revision, previous["revision"])


if __name__ == "__main__":
    unittest.main()
