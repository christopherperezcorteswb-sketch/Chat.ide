"""Pruebas de caracterización para el prototipo original de CHAT.ide.

Estas pruebas arrancan el servidor real como subproceso. No importan ni modifican
``chat_arduino.py`` para que también cubran su forma pública de ejecución.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = ROOT / "chat_arduino.py"


def free_tcp_port() -> int:
    """Ask the OS for a currently free loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunningServer:
    def __init__(
        self,
        script: Path = SERVER_SCRIPT,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.port = free_tcp_port()
        self.script = script
        self.environment = environment or {}
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(self.script), str(self.port)],
            cwd=ROOT,
            env={**os.environ, **self.environment},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + 8
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"El servidor terminó al arrancar:\n{output}")
            try:
                status, _, body = self.request("GET", "/", timeout=0.5)
                # V1 uses an uppercase doctype and V2 a lowercase one. The
                # harness only needs proof that the HTTP application is ready.
                if status == 200 and b"<html" in body.lower():
                    return
            except (OSError, TimeoutError) as exc:
                last_error = exc
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"El servidor no quedó listo: {last_error}")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process.stdout:
            self.process.stdout.close()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 3,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(
        self, method: str, path: str, payload: dict[str, object]
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request(
            method,
            path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )


class SSEClient:
    """Small SSE reader used only by the integration tests."""

    def __init__(self, server: RunningServer, cid: str, name: str) -> None:
        self.server = server
        self.cid = cid
        self.events: list[dict[str, object]] = []
        self.condition = threading.Condition()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=5
        )
        query = urlencode({"cid": cid, "name": name})
        self.connection.request("GET", f"/events?{query}")
        self.response = self.connection.getresponse()
        if self.response.status != 200:
            raise AssertionError(f"SSE devolvió {self.response.status}")
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        try:
            while True:
                line = self.response.readline()
                if not line:
                    return
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:])
                with self.condition:
                    self.events.append(event)
                    self.condition.notify_all()
        except (OSError, TimeoutError, ValueError):
            return

    def wait_for(self, predicate, timeout: float = 3) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for event in self.events:
                    if predicate(event):
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Evento SSE no recibido. Eventos: {self.events!r}")
                self.condition.wait(remaining)

    def leave(self) -> None:
        try:
            self.server.json_request("POST", "/bye", {"cid": self.cid})
        finally:
            self.thread.join(timeout=2)
            self.connection.close()


class ChatServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = RunningServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_01_index_is_self_contained_and_mobile_ready(self) -> None:
        status, headers, body = self.server.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn(b'<meta name="viewport"', body)
        self.assertIn(b"new EventSource(", body)
        self.assertIn(b"fetch('/send'", body)
        self.assertNotIn(b"https://", body)
        self.assertNotIn(b"http://", body)

    def test_02_http_errors_and_body_limits(self) -> None:
        status, _, _ = self.server.request("GET", "/favicon.ico")
        self.assertEqual(status, 204)

        status, _, _ = self.server.request("GET", "/no-existe")
        self.assertEqual(status, 404)

        status, _, _ = self.server.json_request(
            "POST", "/send", {"cid": "cliente-inexistente", "text": "hola"}
        )
        self.assertEqual(status, 403)

        status, _, _ = self.server.request(
            "POST",
            "/send",
            body=b"{json invalido",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

        status, _, _ = self.server.request(
            "POST",
            "/send",
            body=b"x" * 4097,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_03_sse_users_messages_limits_and_leave(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        cid_a = f"test-a-{suffix}"
        cid_b = f"test-b-{suffix}"
        name = f"Ana-{suffix}"
        client_a = SSEClient(self.server, cid_a, name)
        client_b: SSEClient | None = None
        try:
            hello_a = client_a.wait_for(lambda event: event.get("type") == "hello")
            self.assertEqual(hello_a.get("name"), name)

            history = client_a.wait_for(lambda event: event.get("type") == "history")
            self.assertIsInstance(history.get("items"), list)

            client_b = SSEClient(self.server, cid_b, name)
            hello_b = client_b.wait_for(lambda event: event.get("type") == "hello")
            self.assertEqual(hello_b.get("name"), f"{name} (2)")

            users = client_a.wait_for(
                lambda event: event.get("type") == "users"
                and set(event.get("names", [])) == {name, f"{name} (2)"}
            )
            self.assertEqual(len(users["names"]), 2)

            unsafe_text = '<img src=x onerror=alert(1)>\nsegunda línea'
            status, _, _ = self.server.json_request(
                "POST", "/send", {"cid": cid_a, "text": unsafe_text}
            )
            self.assertEqual(status, 204)
            message = client_a.wait_for(
                lambda event: event.get("type") == "msg"
                and event.get("text") == "<img src=x onerror=alert(1)> segunda línea"
            )
            self.assertEqual(message.get("name"), name)

            status, _, _ = self.server.json_request(
                "POST", "/send", {"cid": cid_a, "text": "Z" * 700}
            )
            self.assertEqual(status, 204)
            long_message = client_a.wait_for(
                lambda event: event.get("type") == "msg"
                and isinstance(event.get("text"), str)
                and event["text"].startswith("Z")
                and len(event["text"]) == 500
            )
            self.assertEqual(len(long_message["text"]), 500)

            client_b.leave()
            client_b = None
            remaining = client_a.wait_for(
                lambda event: event.get("type") == "users"
                and event.get("names") == [name]
            )
            self.assertEqual(remaining["names"], [name])
        finally:
            if client_b is not None:
                client_b.leave()
            client_a.leave()


if __name__ == "__main__":
    unittest.main(verbosity=2)
