"""Utilidades de prueba: SMTP falso (nunca se conecta a Gmail) y feeds .ics en línea."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("REMIND_HOUR", None)
os.environ["TIMEZONE"] = "America/Los_Angeles"  # los tests fijan una zona con horario de verano

import reminder  # noqa: E402


class FakeSMTP:
    """Sustituye a smtplib.SMTP_SSL y guarda los mensajes en memoria."""

    sent: list = []
    connections = 0
    fail_with: Exception | None = None

    def __init__(self, host, port, context=None, timeout=None):
        type(self).connections += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if type(self).fail_with is not None:
            raise type(self).fail_with
        type(self).sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.connections, FakeSMTP.fail_with = [], 0, None
    monkeypatch.setattr(reminder.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("GMAIL_USER", "yo@correo.test")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)
    for var in ("DRY_RUN", "MAIL_TO", "BLACKBOARD_ICS_URL"):
        monkeypatch.delenv(var, raising=False)
    return FakeSMTP


def subjects(fake) -> list[str]:
    return [m["Subject"] for m in fake.sent]


def ics(*events: str) -> str:
    """Envuelve bloques VEVENT (sin BEGIN/END) en un VCALENDAR."""
    body = "".join(f"BEGIN:VEVENT\r\nDTSTAMP:20261001T000000Z\r\n{e.strip()}\r\nEND:VEVENT\r\n"
                   for e in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//ES\r\n{body}END:VCALENDAR\r\n"


@pytest.fixture
def run(tmp_path):
    """run(feed, now) -> código de salida; feed es texto .ics o ruta a un archivo."""
    state = tmp_path / "state.json"

    def _run(feed, now: str) -> int:
        path = Path(feed) if isinstance(feed, Path) else tmp_path / "feed.ics"
        if not isinstance(feed, Path):
            path.write_text(feed, encoding="utf-8")
        return reminder.main(["--state", str(state), "--ics-file", str(path), "--now", now])

    _run.state = state
    return _run
