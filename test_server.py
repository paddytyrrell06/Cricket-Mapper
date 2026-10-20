import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import server as S


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


if __name__ == "__main__":
    unittest.main()
