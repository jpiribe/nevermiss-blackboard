"""Un test por hallazgo de la auditoría."""
import pytest
from conftest import ics, subjects

import reminder


def tarea(uid: str, summary: str, dtstart: str = "20261101T065900Z", extra: str = "") -> str:
    return f"UID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART:{dtstart}\r\nDTEND:{dtstart}\r\n{extra}"


# --- #1 título con saltos de línea (inyección de cabeceras) -----------------

def test_titulo_con_salto_de_linea_no_rompe_ni_inyecta_cabeceras(run, smtp):
    run(ics(tarea("a", "base")), "2026-10-29T10:00")  # baseline
    feed = ics(tarea("a", "base"), tarea("b", r"Hola\nBcc: victima@x.test"))
    assert run(feed, "2026-10-29T11:00") == 0
    (msg,) = smtp.sent
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    assert msg["Bcc"] is None
    assert '"b"' in run.state.read_text(encoding="utf-8")


# --- #2 un evento malformado no tumba el resto ------------------------------

def test_evento_malformado_se_omite_y_el_resto_sigue(run, smtp):
    feed = ics(tarea("roto", "Roto", dtstart="garbage"),
               tarea("ok", "Válida", dtstart="20261101T065900Z"))
    assert run(feed, "2026-10-31T10:07") == 0
    assert subjects(smtp) == ["[Blackboard] Hoy vence: Válida"]


# --- #3 tope de correos, resumen y una sola sesión SMTP ---------------------

def test_avalancha_de_tareas_nuevas_se_resume(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    feed = ics(*(tarea(f"n{i}", f"Nueva {i}", dtstart="20261201T065900Z") for i in range(300)))
    assert run(feed, "2026-10-29T11:00") == 0
    assert len(smtp.sent) == 20 and smtp.connections == 1
    assert smtp.sent[-1]["Subject"] == "[Blackboard] Y 281 avisos más"
    smtp.sent.clear()
    run(feed, "2026-10-29T12:00")
    assert smtp.sent == []


def test_serie_nueva_se_anuncia_una_sola_vez(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    serie = tarea("diaria", "Bitácora", dtstart="20261102T065900Z", extra="RRULE:FREQ=DAILY")
    run(ics(serie), "2026-10-29T11:00")
    assert subjects(smtp) == ["[Blackboard] Nueva tarea: Bitácora"]


# --- #4 el state se guarda tras cada envío ----------------------------------

def test_caida_a_mitad_no_reenvia_lo_ya_enviado(run, smtp, monkeypatch):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    feed = ics(tarea("a", "A", dtstart="20261201T065900Z"),
               tarea("b", "B", dtstart="20261202T065900Z"))
    original = smtp.send_message

    def cae_en_el_segundo(self, msg):
        if smtp.sent:
            raise RuntimeError("caída inesperada")
        original(self, msg)

    monkeypatch.setattr(smtp, "send_message", cae_en_el_segundo)
    with pytest.raises(RuntimeError):
        run(feed, "2026-10-29T11:00")
    assert subjects(smtp) == ["[Blackboard] Nueva tarea: A"]

    monkeypatch.setattr(smtp, "send_message", original)
    smtp.sent.clear()
    run(feed, "2026-10-29T12:00")
    assert subjects(smtp) == ["[Blackboard] Nueva tarea: B"]


# --- #5 STATUS:CANCELLED -----------------------------------------------------

def test_tarea_cancelada_no_genera_correo(run, smtp):
    feed = ics(tarea("c", "Cancelada", extra="STATUS:CANCELLED"), tarea("v", "Vigente"))
    run(feed, "2026-10-31T10:07")
    assert subjects(smtp) == ["[Blackboard] Hoy vence: Vigente"]


# --- #6 state.json corrupto o de formato viejo --------------------------------

@pytest.mark.parametrize("contenido", ["", "{no es json", "[]", '{"tasks": 3}'])
def test_state_ilegible_aborta_claro_sin_sobrescribir(run, smtp, contenido):
    run.state.write_text(contenido, encoding="utf-8")
    with pytest.raises(SystemExit, match="state.json"):
        run(ics(tarea("a", "A")), "2026-10-30T10:00")
    assert run.state.read_text(encoding="utf-8") == contenido
    assert smtp.sent == []


def test_state_de_formato_viejo_no_provoca_avalancha(run, smtp):
    run.state.write_text('{"tasks": {"a": {"uid": "a", "due": "2026-12-01"}}}', encoding="utf-8")
    assert run(ics(tarea("a", "A", dtstart="20261201T065900Z")), "2026-10-30T10:00") == 0
    assert smtp.sent == []


# --- #7 fallos SMTP: salida != 0, reintentos acotados, nada marcado ----------

def test_fallo_smtp_termina_en_error_y_no_marca_enviado(run, smtp):
    import smtplib
    feed = ics(*(tarea(f"t{i}", f"T{i}") for i in range(5)))
    smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    assert run(feed, "2026-10-31T10:07") == 1
    assert smtp.connections <= 2

    smtp.fail_with = None
    assert run(feed, "2026-10-31T11:07") == 0
    assert len(smtp.sent) == 5


# --- descarga segura del feed (sin red: requests simulado) -------------------

class FakeResp:
    def __init__(self, status=200, body=b"", location=None):
        self.status_code, self._body = status, body
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http(monkeypatch):
    """http(resp1, resp2, ...) programa las respuestas; .calls guarda las URLs pedidas."""
    import reminder
    calls, queue = [], []

    def get(url, **kw):
        assert kw.get("allow_redirects") is False and kw.get("timeout")
        calls.append(url)
        return queue.pop(0)

    monkeypatch.setattr(reminder.requests, "get", get)
    monkeypatch.setattr(reminder.time, "sleep", lambda s: None)

    def program(*resps):
        queue.extend(resps)
        return calls
    return program


URL = "https://example.blackboard.test/feed/SECRETO123/learn.ics"
ICS_OK = ics(tarea("a", "Reflexión")).encode("utf-8")


def test_feed_solo_https(http):
    import reminder
    calls = http()
    with pytest.raises(reminder.FeedError, match="https"):
        reminder.fetch_ics(URL.replace("https", "http"))
    assert calls == []


@pytest.mark.parametrize("destino", ["https://otro.test/x.ics",
                                     "http://example.blackboard.test/x.ics"])
def test_redireccion_a_destino_no_permitido(http, destino):
    import reminder
    calls = http(FakeResp(302, location=destino))
    with pytest.raises(reminder.FeedError, match="redirección") as exc:
        reminder.fetch_ics(URL)
    assert calls == [URL] and "SECRETO" not in str(exc.value)


def test_redireccion_al_mismo_host_https_se_sigue(http):
    import reminder
    http(FakeResp(301, location="/nuevo.ics"), FakeResp(200, ICS_OK))
    assert "Reflexión" in reminder.fetch_ics(URL)


def test_utf8_sin_charset_se_decodifica_bien(http):
    import reminder
    http(FakeResp(200, ICS_OK))
    assert "Reflexión" in reminder.fetch_ics(URL)


def test_respuesta_demasiado_grande(http):
    import reminder
    http(FakeResp(200, b"x" * (reminder.MAX_ICS_BYTES + 1)))
    with pytest.raises(reminder.FeedError, match="grande"):
        reminder.fetch_ics(URL)


def test_5xx_se_reintenta_y_4xx_no(http):
    import reminder
    calls = http(FakeResp(503), FakeResp(503), FakeResp(200, ICS_OK))
    assert "Reflexión" in reminder.fetch_ics(URL) and len(calls) == 3
    calls.clear()
    http(FakeResp(404))
    with pytest.raises(reminder.FeedError, match="404") as exc:
        reminder.fetch_ics(URL)
    assert len(calls) == 1 and "SECRETO" not in str(exc.value)


@pytest.mark.parametrize("texto,motivo", [("", "vacía"),
                                          ("<!DOCTYPE html><html>Login</html>", "HTML")])
def test_contenido_que_no_es_ics(texto, motivo):
    import reminder
    with pytest.raises(reminder.FeedError, match=motivo):
        reminder.parse_calendar(texto)


# --- "Hoy vence" a partir de las 10:00 (hora local) -----------------------------

def _ventana(run, smtp, evento, antes, despues):
    """Ningún aviso en `antes`; exactamente un "Hoy vence" en `despues` (y no se repite)."""
    run(ics(evento), antes)
    assert smtp.sent == []
    run(ics(evento), despues)
    assert subjects(smtp) == ["[Blackboard] Hoy vence: X"]
    smtp.sent.clear()
    run(ics(evento), despues[:-5] + "23:00")
    assert smtp.sent == []


@pytest.mark.parametrize("evento", [
    tarea("x", "X", dtstart="20261031T065900Z"),  # 30-oct 23:59 PDT, en UTC (Z)
    "UID:x\r\nSUMMARY:X\r\nDTSTART;TZID=America/Los_Angeles:20261030T235900\r\n"
    "DTEND;TZID=America/Los_Angeles:20261030T235900",
], ids=["utc", "tzid"])
def test_hoy_vence_entrega_2359(run, smtp, evento):
    _ventana(run, smtp, evento, "2026-10-30T09:07", "2026-10-30T10:07")


def test_hoy_vence_hora_flotante(run, smtp):
    _ventana(run, smtp, tarea("x", "X", dtstart="20261030T235900"),
             "2026-10-29T10:07", "2026-10-30T10:07")


def test_dia_anterior_no_avisa(run, smtp):
    run(ics(tarea("x", "X", dtstart="20261031T065900Z")), "2026-10-29T15:07")
    assert smtp.sent == []


def test_entrega_antes_de_las_10_se_avisa_desde_medianoche(run, smtp):
    # 30-oct 00:30 PDT: con el umbral de las 10:00 el aviso llegaría vencido.
    run(ics(tarea("x", "X", dtstart="20261030T073000Z")), "2026-10-30T00:07")
    assert subjects(smtp) == ["[Blackboard] Hoy vence: X"]


def test_tarea_ya_vencida_no_avisa(run, smtp):
    run(ics(tarea("x", "X", dtstart="20261030T160000Z")), "2026-10-30T10:07")  # 09:00 PDT
    assert smtp.sent == []


def test_todo_el_dia(run, smtp):
    evento = "UID:x\r\nSUMMARY:X\r\nDTSTART;VALUE=DATE:20261030\r\nDTEND;VALUE=DATE:20261031"
    _ventana(run, smtp, evento, "2026-10-30T09:07", "2026-10-30T10:07")


def test_cambio_de_horario_1_nov(run, smtp):
    # 1-nov-2026 la zona pasa de -07 a -08 a las 02:00. Entrega 1-nov 23:59 PST.
    _ventana(run, smtp, tarea("x", "X", dtstart="20261102T075900Z"),
             "2026-11-01T09:07", "2026-11-01T10:07")


def test_hueco_del_cron_no_pierde_el_aviso(run, smtp):
    evento = tarea("x", "X", dtstart="20261031T065900Z")
    run(ics(evento), "2026-10-30T08:07")
    run(ics(evento), "2026-10-30T14:07")  # GitHub se saltó 09:07–13:07
    assert subjects(smtp) == ["[Blackboard] Hoy vence: X"]


def test_fecha_movida_reinicia_el_aviso(run, smtp):
    run(ics(tarea("x", "X", dtstart="20261031T065900Z")), "2026-10-30T10:07")
    run(ics(tarea("x", "X", dtstart="20261101T065900Z")), "2026-10-30T11:07")
    smtp.sent.clear()
    run(ics(tarea("x", "X", dtstart="20261101T065900Z")), "2026-10-31T10:07")
    assert subjects(smtp) == ["[Blackboard] Hoy vence: X"]


def test_state_viejo_con_reminder_sent_migra(run, smtp):
    run.state.write_text('{"initialized": true, "failure": null, "tasks": {"x": {"uid": "x", '
                         '"series": false, "detected_sent": true, "reminder_sent": true, '
                         '"due": "2026-10-30T23:59:00-07:00", "title": "X"}}}', encoding="utf-8")
    run(ics(tarea("x", "X", dtstart="20261031T065900Z")), "2026-10-30T10:07")
    assert subjects(smtp) == ["[Blackboard] Hoy vence: X"]
    assert "reminder_sent" not in run.state.read_text(encoding="utf-8")


# --- feeds hostiles ----------------------------------------------------------

def test_rrule_subdiario_se_descarta_sin_expandir(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")  # baseline
    serie = tarea("s", "Spam", dtstart="20261101T065900Z", extra="RRULE:FREQ=SECONDLY")
    assert run(ics(tarea("base", "base", dtstart="20261201T065900Z"), serie),
               "2026-10-29T11:00") == 0
    assert smtp.sent == []


def test_serie_larga_se_trunca(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    serie = tarea("s", "Diaria", dtstart="20261101T065900Z", extra="RRULE:FREQ=DAILY")
    assert run(ics(tarea("base", "base", dtstart="20261201T065900Z"), serie),
               "2026-10-29T11:00") == 0
    assert len(smtp.sent) <= reminder.MAX_EMAILS_PER_RUN


@pytest.mark.parametrize("enlace", ["javascript:alert(1)", "data:text/html,x", "http://x.test/a"])
def test_enlace_no_https_no_va_al_correo(run, smtp, enlace):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    feed = ics(tarea("base", "base", dtstart="20261201T065900Z"),
               tarea("a", "A", extra=f"URL:{enlace}"))
    run(feed, "2026-10-29T11:00")
    (msg,) = smtp.sent
    assert "Enlace:" not in msg.get_content()


def _feed_con_serie(extra: str):
    return ics(tarea("base", "base", dtstart="20261201T065900Z"),
               tarea("s", "Serie", dtstart="20261101T065900Z", extra=extra))


def test_rrule_repetido_no_tumba_el_feed(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    feed = _feed_con_serie("RRULE:FREQ=DAILY\r\nRRULE:FREQ=SECONDLY")
    assert run(feed, "2026-10-29T11:00") == 0
    assert smtp.sent == []


def test_daily_con_muchos_byminute_se_descarta_sin_colgarse(run, smtp):
    import time
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    minutos = ",".join(str(i) for i in range(60))
    feed = _feed_con_serie(f"RRULE:FREQ=DAILY;BYMINUTE={minutos};BYSECOND={minutos}")
    t0 = time.monotonic()
    assert run(feed, "2026-10-29T11:00") == 0
    assert time.monotonic() - t0 < 5
    assert smtp.sent == []


def test_muchos_uids_se_truncan_a_max_tasks(run, smtp, monkeypatch):
    monkeypatch.setattr(reminder, "MAX_TASKS", 5)
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    feed = ics(tarea("base", "base", dtstart="20261201T065900Z"),
               *(tarea(f"u{i}", f"T{i}", dtstart="20261101T065900Z") for i in range(50)))
    assert run(feed, "2026-10-29T11:00") == 0
    assert len(smtp.sent) <= 5


@pytest.mark.parametrize("destino", ["https://host:99999/x", "https://user@host/x",
                                     "https://host:8443/x", "http://host/x"])
def test_redireccion_invalida_es_error_controlado(monkeypatch, destino):
    class Resp:
        is_redirect, status_code, headers = True, 302, {"Location": destino}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(reminder.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(reminder.FeedError):
        reminder._download("https://host/feed.ics", "host")


def test_muchas_rrule_con_dtstart_antiguo_no_cuelgan(run, smtp):
    import time
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    rrules = "\r\n".join(f"RRULE:FREQ=DAILY;BYHOUR={h};BYMINUTE=0" for h in range(24))
    feed = _feed_con_serie(rrules).replace("DTSTART:20261101T065900Z", "DTSTART:00010101T000000Z")
    t0 = time.monotonic()
    assert run(feed, "2026-10-29T11:00") == 0
    assert time.monotonic() - t0 < 10
    assert smtp.sent == []


def test_rdate_masivo_se_descarta(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    fechas = ",".join(f"20261101T{h:02d}{m:02d}00Z" for h in range(24) for m in range(0, 60, 15))
    feed = _feed_con_serie("RDATE:" + fechas + "\r\n" + "\r\n".join(
        f"RDATE:2026110{d}T120000Z,2026110{d}T130000Z" for d in range(1, 10)) * 8)
    assert run(feed, "2026-10-29T11:00") == 0
    assert len(smtp.sent) <= 2


def test_enlace_kilometrico_no_va_al_correo(run, smtp):
    run(ics(tarea("base", "base", dtstart="20261201T065900Z")), "2026-10-29T10:00")
    feed = ics(tarea("base", "base", dtstart="20261201T065900Z"),
               tarea("a", "A", extra="URL:https://x.test/" + "a" * 5000))
    run(feed, "2026-10-29T11:00")
    (msg,) = smtp.sent
    assert "Enlace:" not in msg.get_content()


def test_descarga_con_goteo_se_corta_por_plazo(monkeypatch):
    import time

    class Resp:
        is_redirect, status_code = False, 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, n):
            while True:
                time.sleep(0.2)
                yield b"x"

    monkeypatch.setattr(reminder.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(reminder, "DOWNLOAD_BUDGET_S", 1)
    t0 = time.monotonic()
    with pytest.raises(reminder._Transient):
        reminder._download("https://host/feed.ics", "host")
    assert time.monotonic() - t0 < 5
