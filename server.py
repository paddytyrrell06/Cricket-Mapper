"""Serve the audited terminal locally and refresh quotes without opening new tabs.

Run ``python3 server.py`` and use the HTTP address printed at startup. The
browser requests fixed, read-only venue quote collection; this server never
accepts executable commands, file paths, or trading instructions from clients.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs


ROOT = Path(__file__).resolve().parent
REPORT_NAME = "CricketMatchingReport.html"
VERSION_NAME = "CricketMatchingReport.version.js"
REFRESH_TIMEOUT_SECONDS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refresh_command(root: Path) -> list[str]:
    """Keep the published snapshot's sport scope when refreshing discovery."""
    command = [sys.executable, str(root / "run.py"),
               "--snapshot-dir=output/data", "--refresh-quotes", "--no-open"]
    metadata_path = root / "output" / "data" / "snapshot_meta.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return command
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read the saved discovery metadata") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Invalid saved discovery metadata")
    sports = metadata.get("sports")
    if sports is not None:
        if (not isinstance(sports, list) or not sports or
                any(not isinstance(sport, str) or not sport.isidentifier()
                    or not sport.isascii() for sport in sports)):
            raise ValueError("Invalid saved discovery sport scope")
        command.append("--sports=" + ",".join(dict.fromkeys(sports)))
    return command


class QuoteRefresh:
    """Coalesce browser requests into one background, audited publication."""

    def __init__(self, root: Path = ROOT, *, runner=None, runtime=None):
        self.root = Path(root).resolve()
        self._runner = runner or subprocess.run
        self.runtime = runtime
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._state = {
            "state": "idle", "running": False, "revision": 0,
            "started_at": None, "completed_at": None,
            "last_success_at": None, "error": None,
        }

    def status(self) -> dict:
        with self._lock:
            result = dict(self._state)
        if self.runtime is not None:
            result.update(mode="runtime_rest", revision=self.runtime.revision)
        return result

    def request(self) -> dict:
        with self._lock:
            if self._state["running"]:
                return {**self._state, "coalesced": True}
            self._state.update(state="running", running=True, started_at=utc_now(),
                               completed_at=None, error=None)
            self._worker = threading.Thread(target=self._refresh,
                                            name="terminal-quote-refresh", daemon=True)
            self._worker.start()
            return {**self._state, "coalesced": False}

    def wait(self, timeout: float = REFRESH_TIMEOUT_SECONDS + 5) -> None:
        """Allow callers/tests to await a requested publication."""
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _refresh(self) -> None:
        error = None
        try:
            if self.runtime is not None:
                self.runtime.refresh()
                self._finish(None)
                return
            if not (self.root / "output" / "data").is_dir():
                raise ValueError("No saved discovery: run python3 run.py once first")
            result = self._runner(
                refresh_command(self.root), cwd=str(self.root), shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=REFRESH_TIMEOUT_SECONDS, check=False,
            )
            if result.returncode:
                error = f"Quote refresh exited with status {result.returncode}; previous report retained"
                print(result.stdout or error, file=sys.stderr, flush=True)
            elif not (self.root / "output" / REPORT_NAME).is_file():
                error = "Refresh finished without a published report"
        except subprocess.TimeoutExpired:
            error = "Quote refresh timed out; previous report retained"
        except (OSError, ValueError) as exc:
            error = str(exc)
        except Exception:
            error = "Quote refresh failed; previous report retained"
        self._finish(error)

    def _finish(self, error):
        with self._lock:
            completed_at = utc_now()
            self._state.update(running=False, completed_at=completed_at,
                               state="error" if error else "idle", error=error)
            if not error:
                self._state["revision"] += 1
                self._state["last_success_at"] = completed_at
        print(error or f"Quotes refreshed at {completed_at}", flush=True)


class TerminalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 8765, *, root: Path = ROOT, refresh=None, runtime=None, research=None):
        self.root = Path(root).resolve()
        self.runtime = runtime
        if refresh is None and runtime is None:
            try:
                from runtime import MarketRuntime
                self.runtime = MarketRuntime(self.root)
            except (OSError, ValueError, KeyError):
                self.runtime = None
        self.refresh = refresh or QuoteRefresh(self.root, runtime=self.runtime)
        self.research = research
        if research is None and refresh is None:
            try:
                from research_store import ResearchStore
                from research import ResearchService
                store = ResearchStore(self.root / "data/eventlens.sqlite3")
                if (self.root / "output/data/market_map.csv").exists():
                    store.import_snapshot(self.root / "output/data")
                self.research = ResearchService(self.root, store, self.runtime)
                if self.runtime is not None:
                    self.runtime.recorder = store
            except (OSError, ValueError, sqlite3.Error) as exc:
                print("Research storage unavailable: " + type(exc).__name__, file=sys.stderr)
        self._assets: dict[str, bytes] = {}
        self._asset_lock = threading.Lock()
        # Bind only to IPv4 loopback; exposing this control endpoint is unsupported.
        super().__init__(("127.0.0.1", port), TerminalHandler)

    def read_asset(self, name: str) -> bytes | None:
        with self._asset_lock:
            # run.py atomically replaces output via the previous directory.
            # Serve the last audited asset during that short rename window.
            for directory in ("output", ".cricket_matching_previous_output", "output"):
                try:
                    data = (self.root / directory / name).read_bytes()
                except FileNotFoundError:
                    continue
                self._assets[name] = data
                return data
            return self._assets.get(name)


class TerminalHandler(BaseHTTPRequestHandler):
    server: TerminalServer
    server_version = "MarketTerminal/1"

    def _host_valid(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        return len(hosts) == 1 and hosts[0] in (
            f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code: int, body: bytes, content_type: str,
              *, head: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, code: int, value: dict, *, head: bool = False) -> None:
        self._send(code, json.dumps(value).encode("utf-8"),
                   "application/json; charset=utf-8", head=head)

    def do_HEAD(self) -> None:
        self._get(head=True)

    def do_GET(self) -> None:
        self._get()

    def _get(self, *, head: bool = False) -> None:
        if not self._host_valid():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid local host"}, head=head)
            return
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        if path == "/research":
            try:
                body = (ROOT / "research.html").read_bytes()
            except FileNotFoundError:
                self._json(503, {"error": "Research page unavailable"}, head=head)
                return
            self._send(200, body, "text/html; charset=utf-8", head=head)
            return
        if path.startswith("/api/research/") and self.server.research is not None:
            try:
                query = parse_qs(parsed_url.query)
                if path == "/api/research/export":
                    self._send(200, self.server.research.export(query.get("dataset", [""])[0]), "text/csv; charset=utf-8", head=head)
                    return
                result = self.server.research.get(path, query)
                if result is not None:
                    self._json(200, result, head=head)
                    return
            except (ValueError, TypeError, KeyError):
                self._json(400, {"error": "Invalid research query"}, head=head)
                return
        if path == "/api/status":
            self._json(HTTPStatus.OK, self.server.refresh.status(), head=head)
            return
        if self.server.runtime is not None and path == "/api/markets":
            revision = parse_qs(parsed_url.query).get("revision", [None])[0]
            self._json(HTTPStatus.OK, self.server.runtime.snapshot(revision), head=head)
            return
        if self.server.runtime is not None and path.startswith("/api/market/"):
            try:
                index = int(path.rsplit("/", 1)[1])
                if index < 0:
                    raise ValueError
                detail = self.server.runtime.detail(index)
            except (IndexError, ValueError):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown market"}, head=head)
                return
            self._json(HTTPStatus.OK, detail, head=head)
            return
        assets = {"/": REPORT_NAME, "/" + REPORT_NAME: REPORT_NAME,
                  "/" + VERSION_NAME: VERSION_NAME}
        name = assets.get(path)
        if name is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"}, head=head)
            return
        data = self.server.read_asset(name)
        if data is None:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                       {"error": "No audited report: run python3 run.py once first"}, head=head)
            return
        content_type = ("application/javascript; charset=utf-8" if name == VERSION_NAME
                        else "text/html; charset=utf-8")
        self._send(HTTPStatus.OK, data, content_type, head=head)

    def do_POST(self) -> None:
        if not self._host_valid():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid local host"})
            return
        research_paths = {"/api/research/discover", "/api/research/import", "/api/research/book", "/api/research/candles"}
        if self.path not in {"/api/refresh", "/api/settings"} | research_paths:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        expected_origin = "http://" + self.headers["Host"]
        if (self.headers.get_all("Origin", []) != [expected_origin] or
                self.headers.get_all("X-Terminal-Request", []) != ["refresh"] or
                self.headers.get("Sec-Fetch-Site", "same-origin") not in
                ("same-origin", "none")):
            self._json(HTTPStatus.FORBIDDEN, {"error": "Same-origin refresh required"})
            return
        if (self.headers.get("Content-Type", "").split(";", 1)[0].lower() !=
                "application/json" or self.headers.get("Transfer-Encoding") is not None):
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                       {"error": "Use application/json"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError
            length = int(lengths[0])
            if length < 0 or length > (1024 if self.path == "/api/refresh" else 1024 * 1024):
                raise ValueError
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length)) if length else {}
            if self.path == "/api/refresh" and body != {}:
                raise ValueError
        except (ValueError, OSError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Refresh body must be {}"})
            return
        if self.path in research_paths:
            if self.server.research is None:
                self._json(503, {"error": "Research storage unavailable"})
                return
            try:
                self._json(200, self.server.research.post(self.path, body))
            except (ValueError, TypeError, KeyError) as exc:
                self._json(400, {"error": str(exc)[:250]})
            except Exception as exc:
                # Optional SDK errors may carry URLs/credentials. Never echo them.
                name = type(exc).__name__
                message = ("This outcome has no current provider book (closed or not yet quoted). Saved history remains available."
                    if name in {"NotFoundError", "MarketNotFound", "EventNotFound"} else
                    "Data provider unavailable (" + name + "). For PMXT use .venv-pmxt/bin/python server.py; see docs/PMXT.md.")
                self._json(503, {"error": message})
            return
        if self.path == "/api/settings":
            if self.server.runtime is None:
                self._json(HTTPStatus.CONFLICT, {"error": "Runtime cache unavailable"})
                return
            try:
                settings = self.server.runtime.configure(body)
            except (ValueError, TypeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid scanner settings"})
                return
            self._json(HTTPStatus.ACCEPTED, {"settings": settings, **self.server.refresh.request()})
            return
        self._json(HTTPStatus.ACCEPTED, self.server.refresh.request())

    def log_message(self, format: str, *args) -> None:
        # Version checks poll every two seconds; keep terminal logs useful.
        if len(args) < 2 or str(args[1]) not in ("200", "202"):
            super().log_message(format, *args)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser tab")
    parser.add_argument("--interval", type=float, default=15,
                        help="Continuous REST refresh seconds; 0 disables automatic refresh")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.interval != 0 and not 5 <= args.interval <= 3600:
        parser.error("interval must be 0 or between 5 and 3600 seconds")
    try:
        server = TerminalServer(args.port)
    except OSError as exc:
        print(f"Cannot start local terminal on port {args.port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_port}/{REPORT_NAME}"
    print(f"Market Terminal: {url}\nRefresh prices or reload to request quotes; REST interval {args.interval:g}s.\n"
          "Keep this process running; Ctrl+C stops the local service.", flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            print("Open the terminal URL above in your browser.", flush=True)
    stopped = threading.Event()
    def scan_loop():
        while not stopped.wait(args.interval):
            server.refresh.request()
    if server.runtime is not None:
        server.refresh.request()
        if args.interval:
            threading.Thread(target=scan_loop, name="terminal-rest-scheduler", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
