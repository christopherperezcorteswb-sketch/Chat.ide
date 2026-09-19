"""Caracterización de la candidata ``chat_arduino_v2.py``.

V2 fue agregada directamente a ``main`` mientras se preparaba la red de
seguridad. Estas pruebas permiten evaluarla sin declararla todavía reemplazo de
la línea base. La base SQLite vive siempre en un directorio temporal.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode

from test_chat_server import ROOT, RunningServer


V2_SCRIPT = ROOT / "chat_arduino_v2.py"


class V2SSEClient:
    def __init__(self, server: RunningServer, token: str) -> None:
        self.server = server
        self.token = token
        self.events: list[dict[str, object]] = []
        self.condition = threading.Condition()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=5
        )
        query = urlencode({"token": token})
        self.connection.request("GET", f"/events?{query}")
        self.response = self.connection.getresponse()
        if self.response.status != 200:
            raise AssertionError(f"SSE v2 devolvió {self.response.status}")
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
                    raise AssertionError(f"Evento SSE v2 no recibido: {self.events!r}")
                self.condition.wait(remaining)

    def leave(self) -> None:
        try:
            self.server.json_request("POST", "/bye", {"token": self.token})
        finally:
            self.thread.join(timeout=2)
            self.connection.close()


class ChatServerV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = tempfile.TemporaryDirectory(prefix="chatide-v2-tests-")
        cls.server = cls.new_server()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.data_dir.cleanup()

    @classmethod
    def new_server(cls) -> RunningServer:
        return RunningServer(
            script=V2_SCRIPT,
            environment={"CHATIDE_DATA_DIR": cls.data_dir.name},
        )

    def join(self, name: str, room: str = "General") -> dict[str, object]:
        status, _, body = self.server.json_request(
            "POST", "/join", {"name": name, "room": room}
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_01_page_health_and_security_headers(self) -> None:
        status, headers, body = self.server.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"CHAT.ide LAN", body)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertIn("Content-Security-Policy", headers)
        self.assertNotIn(b"https://", body)

        status, _, body = self.server.request("GET", "/health")
        health = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(
            health["rooms"], ["General", "Robot", "Programacion", "Equipo"]
        )

    def test_02_server_sessions_rooms_presence_and_messages(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        first = self.join(f"Ana-{suffix}", "sala-invalida")
        second = self.join(f"Ana-{suffix}", "General")
        self.assertEqual(first["room"], "General")
        self.assertRegex(str(first["token"]), r"^[0-9a-f]{32}$")
        self.assertEqual(second["name"], f"Ana-{suffix} (2)")

        stream_a = V2SSEClient(self.server, str(first["token"]))
        stream_b = V2SSEClient(self.server, str(second["token"]))
        try:
            hello = stream_a.wait_for(lambda event: event.get("type") == "hello")
            self.assertEqual(hello["name"], first["name"])

            users = stream_a.wait_for(
                lambda event: event.get("type") == "users"
                and event.get("count") == 2
            )
            self.assertEqual(set(users["names"]), {first["name"], second["name"]})

            status, _, body = self.server.json_request(
                "POST",
                "/send",
                {"token": first["token"], "text": "<b>texto</b>"},
            )
            sent = json.loads(body)
            self.assertEqual(status, 200)
            message = stream_a.wait_for(
                lambda event: event.get("type") == "msg"
                and event.get("id") == sent.get("id")
            )
            self.assertEqual(message["text"], "<b>texto</b>")
            self.assertEqual(message["room"], "General")
        finally:
            stream_b.leave()
            stream_a.leave()

    def test_03_sqlite_history_survives_restart(self) -> None:
        marker = f"persistencia-{uuid.uuid4().hex}"
        session = self.join("Persistencia", "Programacion")
        status, _, _ = self.server.json_request(
            "POST", "/send", {"token": session["token"], "text": marker}
        )
        self.assertEqual(status, 200)

        self.server.stop()
        self.__class__.server = self.new_server()
        self.server.start()

        restored = self.join("Lector", "Programacion")
        self.assertTrue(
            any(item.get("text") == marker for item in restored["history"]),
            restored["history"],
        )
        self.server.json_request("POST", "/bye", {"token": restored["token"]})

    def test_04_rate_limit_rejects_thirteenth_message(self) -> None:
        session = self.join("Rate", "Robot")
        statuses = [
            self.server.json_request(
                "POST", "/send", {"token": session["token"], "text": f"m-{i}"}
            )[0]
            for i in range(13)
        ]
        self.assertEqual(statuses[:12], [200] * 12)
        self.assertEqual(statuses[12], 429)
        self.server.json_request("POST", "/bye", {"token": session["token"]})

    @unittest.expectedFailure
    def test_05_newline_is_normalized_to_a_space(self) -> None:
        """Known v2 bug: clean_text currently turns ``hola\nmundo`` into one word."""
        session = self.join("Saltos", "Equipo")
        stream = V2SSEClient(self.server, str(session["token"]))
        try:
            status, _, body = self.server.json_request(
                "POST",
                "/send",
                {"token": session["token"], "text": "hola\nmundo"},
            )
            sent = json.loads(body)
            self.assertEqual(status, 200)
            message = stream.wait_for(
                lambda event: event.get("type") == "msg"
                and event.get("id") == sent.get("id")
            )
            self.assertEqual(message["text"], "hola mundo")
        finally:
            stream.leave()


if __name__ == "__main__":
    unittest.main(verbosity=2)
