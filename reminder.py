#!/usr/bin/env python3
"""Recordatorios de tareas de Blackboard por Gmail, a partir del feed iCal.

Envía correo solo en dos casos: tarea nueva detectada y "hoy vence" (el día de la
entrega, desde la hora configurada, 10:00 por defecto).
Nunca imprime la URL del feed ni las credenciales.
"""
import argparse
import json
import logging
import os
import re
import signal
import smtplib
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import recurring_ical_events  # type: ignore[import-untyped]
import requests


def _load_tz() -> ZoneInfo:
    """Zona horaria de TIMEZONE (nombre IANA, p. ej. America/Mexico_City); UTC por defecto."""
    name = os.environ.get("TIMEZONE") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"TIMEZONE inválida: {name!r}. Usa un nombre IANA, "
                         f"p. ej. America/Mexico_City.") from None


def _load_remind_hour() -> int:
    raw = os.environ.get("REMIND_HOUR") or "10"
    if not raw.isdigit() or not 0 <= int(raw) <= 23:
        raise SystemExit(f"REMIND_HOUR inválida: {raw!r}. Debe ser un entero de 0 a 23.")
    return int(raw)


TZ = _load_tz()
REMIND_HOUR = _load_remind_hour()  # "Hoy vence" se envía el día de la entrega desde esta hora
HORIZON_DAYS = 120          # cuántos días hacia adelante se expanden eventos
PRUNE_AFTER_DAYS = 30       # se olvidan entradas del state vencidas hace más de esto
FAILURE_ALERT_AFTER = timedelta(hours=24)
DOWNLOAD_BUDGET_S = 60        # plazo total de la descarga (frente a servidores lentos)
EXTRACT_BUDGET_S = 60         # plazo total para expandir el calendario
UID_BUDGET_S = 5              # plazo duro para expandir un solo UID
MAX_RRULES_PER_UID = 2
MAX_RDATES_PER_UID = 100
MAX_TIMEZONES = 50
MAX_LINK_CHARS = 500
MAX_UID_CHARS = 200
MAX_OCCURRENCES_PER_UID = 200   # tope por serie: un RRULE hostil no debe inundar correo ni state
MAX_TASKS = 2000            # tope total de tareas por ejecución
SUBDAILY_FREQS = {"SECONDLY", "MINUTELY", "HOURLY"}
MAX_TITLE_CHARS = 150       # el título va en el asunto: se recorta
MAX_EMAILS_PER_RUN = 20     # incluido el correo resumen; evita avalanchas (límite diario de Gmail)
MAX_SMTP_FAILURES = 2       # fallos seguidos tras los que se deja de intentar en esta ejecución
MAX_ICS_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_ATTEMPTS = 3          # descarga: reintentos ante red/5xx con espera 2 s, 4 s
MAX_TASK_MINUTES = int(os.environ.get("MAX_TASK_MINUTES", "0"))

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

log = logging.getLogger("reminder")


class FeedError(Exception):
    """Fallo al descargar o validar el feed. El mensaje nunca incluye la URL."""


# ---------------------------------------------------------------- feed

class _Transient(FeedError):
    """Fallo que vale la pena reintentar (red, 5xx, 429)."""


def fetch_ics(url: str) -> str:
    """Descarga el feed: solo https, redirecciones solo al mismo host, tamaño acotado.

    Los mensajes de error nunca incluyen la URL (lleva el token del calendario).
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise FeedError("la URL del feed debe ser https")
    error: FeedError = FeedError("descarga fallida")
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            return _download(url, parts.hostname)
        except _Transient as e:
            error = e
    raise error


@contextmanager
def _hard_deadline(seconds: float):
    """Corta con TimeoutError lo que tarde más de `seconds` (también un bloqueo en C o en
    una lectura de red). Solo en el hilo principal de Linux/macOS; si no, no hace nada."""
    en_principal = threading.current_thread() is threading.main_thread()
    if not hasattr(signal, "setitimer") or not en_principal:
        yield
        return

    def _expired(*_):
        raise TimeoutError

    old = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _download(url: str, host: str) -> str:
    deadline = time.monotonic() + DOWNLOAD_BUDGET_S
    for _ in range(MAX_REDIRECTS + 1):
        try:
            r = requests.get(url, timeout=30, stream=True, allow_redirects=False)
        except requests.RequestException as e:
            # str(e) suele contener la URL: solo se registra el tipo.
            raise _Transient(f"descarga fallida ({type(e).__name__})") from None
        with r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("Location", ""))
                nxt = urlsplit(url)
                try:
                    port = nxt.port or 443
                except ValueError:
                    port = -1
                if (nxt.scheme != "https" or nxt.hostname != host or port != 443
                        or nxt.username or nxt.password):
                    raise FeedError("redirección a un destino no permitido (otro host o sin https)")
                continue
            if r.status_code >= 500 or r.status_code == 429:
                raise _Transient(f"descarga fallida (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise FeedError(f"descarga fallida (HTTP {r.status_code}; ¿el enlace expiró?)")
            data = bytearray()
            try:
                with _hard_deadline(max(1.0, deadline - time.monotonic())):
                    for chunk in r.iter_content(64 * 1024):
                        data += chunk
                        if len(data) > MAX_ICS_BYTES:
                            raise FeedError("respuesta demasiado grande para ser un feed .ics")
            except TimeoutError:
                raise _Transient("descarga demasiado lenta") from None
            # Sin depender del charset del servidor (r.text asume ISO-8859-1).
            return data.decode("utf-8-sig", "replace")
    raise FeedError("demasiadas redirecciones")


def parse_calendar(text: str) -> icalendar.Calendar:
    head = text.lstrip()[:100].lower()
    if not head:
        raise FeedError("respuesta vacía")
    if head.startswith(("<!doctype html", "<html")):
        raise FeedError("se recibió HTML en vez de .ics (¿el enlace expiró o pide login?)")
    try:
        cal = icalendar.Calendar.from_ical(text)
    except Exception as e:  # noqa: BLE001 - icalendar lanza muchos tipos; todos = feed inválido
        raise FeedError(f"contenido no es iCalendar ({type(e).__name__})") from None
    if cal.name != "VCALENDAR":
        raise FeedError("el contenido no es un VCALENDAR")
    return cal


# ---------------------------------------------------------------- eventos

def _text(comp, name) -> str:
    v = comp.get(name)
    if v is None:
        return ""
    if hasattr(v, "cats"):  # vCategory
        return ", ".join(str(c) for c in v.cats)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


def _oneline(s: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Texto del .ics (no confiable) en una sola línea: sin saltos ni caracteres de control."""
    s = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", s).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def load_course_rules() -> list:
    """Reglas {pattern, course} de courses.json (junto al script); se aplican al título."""
    path = Path(__file__).with_name("courses.json")
    if not path.exists():
        return []
    return [(re.compile(r["pattern"], re.IGNORECASE), r["course"])
            for r in json.loads(path.read_text(encoding="utf-8"))]


def guess_course(title: str, rules: list) -> str:
    return next((course for rx, course in rules if rx.search(title)), "")


def _find_link(comp) -> str:
    url = _text(comp, "URL")
    if not url:
        m = re.search(r"https?://\S+", _text(comp, "DESCRIPTION"))
        url = m.group(0).rstrip(").,>") if m else ""
    if len(url) > MAX_LINK_CHARS or urlsplit(url).scheme != "https":
        return ""   # nada de javascript:, data: ni enlaces kilométricos
    return _oneline(url)


def _to_local(dt):
    """datetime con/sin zona -> datetime en la zona configurada (sin zona = esa zona)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def extract_tasks(cal, now: datetime) -> dict:
    """Devuelve {clave: tarea} con las entregas vigentes del feed.

    Se consideran entregas:
      - eventos con hora y duración cero (DTSTART == DTEND; así publica Blackboard
        las fechas de entrega), o duración <= MAX_TASK_MINUTES;
      - eventos de todo el día de un solo día (la entrega es ese día).
    Se ignoran clases/reuniones (con duración) y eventos de varios días.
    """
    events = [c for c in cal.walk("VEVENT")]
    recurring_uids = {
        str(c.get("UID")) for c in events
        if "RRULE" in c or "RDATE" in c or "RECURRENCE-ID" in c
    }
    rules = load_course_rules()
    start = now - timedelta(days=1)
    end = now + timedelta(days=HORIZON_DAYS)

    tasks: dict[str, dict] = {}
    deadline = time.monotonic() + EXTRACT_BUDGET_S
    for group in _by_uid(cal, events):
        if len(tasks) >= MAX_TASKS or time.monotonic() > deadline:
            log.warning("calendario truncado: demasiados eventos o tiempo agotado")
            break
        # Cada UID (serie + excepciones) se expande y procesa por separado:
        # un evento malformado no debe dejar sin recordatorios al resto.
        try:
            with _hard_deadline(UID_BUDGET_S):
                occurrences = recurring_ical_events.of(group).between(start, end)
            for n, comp in enumerate(occurrences):
                if n >= MAX_OCCURRENCES_PER_UID or len(tasks) >= MAX_TASKS:
                    log.warning("serie truncada: demasiadas ocurrencias")
                    break
                t = _task_from(comp, now, recurring_uids, rules)
                if t is not None:
                    tasks.setdefault(t.pop("key"), t)
        except Exception as e:  # noqa: BLE001 - aislamiento por evento, a propósito
            log.warning("evento omitido por formato inválido (%s)", type(e).__name__)
    return tasks


def _is_subdaily(comps) -> bool:
    """¿Es una serie que se repite varias veces al día? No es una entrega: se descarta
    antes de expandirla (expandir un RRULE SECONDLY, o un DAILY con BYMINUTE/BYSECOND
    múltiples, puede colgar la ejecución). Tolera RRULE repetido (icalendar da lista)."""
    for c in comps:
        rules = c.get("RRULE")
        rules = rules if isinstance(rules, list) else [rules]
        if sum(r is not None for r in rules) > MAX_RRULES_PER_UID:
            return True
        rdates = c.get("RDATE")
        rdates = rdates if isinstance(rdates, list) else [rdates]
        if sum(len(getattr(r, "dts", [])) for r in rdates if r is not None) > MAX_RDATES_PER_UID:
            return True
        for rule in rules:
            if rule is None:
                continue
            if any(str(f).upper() in SUBDAILY_FREQS for f in rule.get("FREQ", [])):
                return True
            if any(len(rule.get(k, [])) > 1 for k in ("BYHOUR", "BYMINUTE", "BYSECOND")):
                return True
    return False


def _by_uid(cal, events):
    """Un VCALENDAR por UID (generador), con las propiedades y zonas del original."""
    groups: dict[str, list] = {}
    for c in events:
        groups.setdefault(str(c.get("UID", "")), []).append(c)
    timezones = list(cal.walk("VTIMEZONE"))[:MAX_TIMEZONES]
    for comps in groups.values():
        try:
            subdaily = _is_subdaily(comps)
        except Exception:  # noqa: BLE001 - un RRULE raro no debe tumbar todo el feed
            subdaily = True
        if subdaily:
            log.warning("serie omitida: demasiadas repeticiones o ilegible")
            continue
        sub = icalendar.Calendar()
        for k, v in cal.items():
            sub[k] = v
        for c in timezones + comps:
            sub.add_component(c)
        yield sub


def _task_from(comp, now: datetime, recurring_uids: set, rules: list) -> dict | None:
    """Tarea a partir de una ocurrencia, o None si no es una entrega vigente."""
    uid = str(comp.get("UID", "")).strip()[:MAX_UID_CHARS]
    if not uid or "DTSTART" not in comp:
        return None
    if str(comp.get("STATUS", "")).upper() == "CANCELLED":
        return None
    today = now.astimezone(TZ).date()
    ds = comp.get("DTSTART").dt
    de = comp.get("DTEND").dt if "DTEND" in comp else None

    if isinstance(ds, datetime):
        due = _to_local(ds)
        dur = (_to_local(de) - due) if isinstance(de, datetime) else timedelta(0)
        if dur > timedelta(minutes=MAX_TASK_MINUTES):
            return None  # clase / reunión
        all_day = False
        if due < now:
            return None
        due_key = due.isoformat()
    else:  # DATE => todo el día
        dur = (de - ds) if isinstance(de, date) else timedelta(days=1)
        if dur > timedelta(days=1):
            return None  # evento de varios días
        all_day = True
        if ds < today:
            return None
        due = datetime.combine(ds, dtime(0, 0), tzinfo=TZ)
        due_key = ds.isoformat()

    series = uid in recurring_uids
    title = _oneline(_text(comp, "SUMMARY")) or "(sin título)"
    return {
        "key": f"{uid}#{due_key}" if series else uid,
        "uid": uid, "series": series, "title": title,
        "due": due_key, "due_dt": due, "all_day": all_day,
        "course": _oneline(_text(comp, "CATEGORIES") or _text(comp, "LOCATION")
                           or guess_course(title, rules)),
        "link": _find_link(comp),
    }


# ---------------------------------------------------------------- correo

def fmt_due(t) -> str:
    d = t["due_dt"]
    base = f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]} de {d.year}"
    if t["all_day"]:
        return f"{base} (todo el día)"
    return f"{base}, {d:%H:%M} ({TZ.key})"


def build_body(t, intro: str) -> str:
    lines = [intro, "", f"Título: {t['title']}"]
    if t["course"]:
        lines.append(f"Curso: {t['course']}")
    lines.append(f"Entrega: {fmt_due(t)}")
    if t["link"]:
        lines.append(f"Enlace: {t['link']}")
    return "\n".join(lines) + "\n"


class Mailer:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.user = os.environ.get("GMAIL_USER", "")
        self.password = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.to = os.environ.get("MAIL_TO") or self.user
        self._smtp: smtplib.SMTP_SSL | None = None
        self.failures = 0           # si > 0, la ejecución termina con código 1
        self._consecutive = 0
        if not dry_run and not (self.user and self.password):
            raise SystemExit("Faltan GMAIL_USER / GMAIL_APP_PASSWORD")

    def send(self, subject: str, body: str) -> bool:
        if self.dry_run:
            print(f"--- [DRY_RUN] correo ---\nPara: {self.to or '(MAIL_TO)'}\n"
                  f"Asunto: {subject}\n\n{body}")
            return True
        try:
            msg = EmailMessage()
            msg["From"], msg["To"] = self.user, self.to
            msg["Subject"] = _oneline(subject, 200)
            msg.set_content(body)
        except ValueError as e:
            log.error("correo no construido (%s)", type(e).__name__)
            self.failures += 1
            return False
        if self._consecutive >= MAX_SMTP_FAILURES:
            self.failures += 1
            return False  # Gmail ya falló varias veces: no insistir en esta ejecución
        try:
            if self._smtp is None:  # una sola sesión por ejecución
                self._smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465,
                                              context=ssl.create_default_context(), timeout=30)
                self._smtp.login(self.user, self.password)
            self._smtp.send_message(msg)
            self._consecutive = 0
            return True
        except (smtplib.SMTPException, OSError) as e:
            log.error("envío de correo fallido (%s)", type(e).__name__)
            self.failures += 1
            self._consecutive += 1
            if isinstance(e, smtplib.SMTPAuthenticationError):
                self._consecutive = MAX_SMTP_FAILURES  # credenciales malas: reintentar no sirve
            self.close()
            return False

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self._smtp = None


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    """Lee el state. Si está ilegible aborta sin tocarlo: reiniciarlo en silencio
    provocaría un baseline nuevo o una avalancha de correos."""
    if not path.exists():
        return {"tasks": {}, "failure": None, "initialized": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SystemExit(f"{path.name} ilegible ({type(e).__name__}): restáuralo desde git "
                         f"o bórralo para empezar de cero. No se envió nada.") from None
    tasks = state.get("tasks") if isinstance(state, dict) else None
    if not isinstance(tasks, dict) or not all(isinstance(v, dict) for v in tasks.values()):
        raise SystemExit(f"{path.name} con formato inesperado: restáuralo desde git "
                         f"o bórralo para empezar de cero. No se envió nada.")
    # Formato viejo: lo ya registrado se da por anunciado.
    state.setdefault("failure", None)
    state.setdefault("initialized", bool(tasks))
    for key, entry in tasks.items():
        entry.setdefault("uid", key.split("#")[0])
        entry.setdefault("series", False)
        entry.setdefault("detected_sent", True)
        entry.pop("reminder_sent", None)  # era el aviso "Mañana vence", ya no existe
        entry.setdefault("today_sent", False)
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- lógica

def handle_failure(state, now, mailer, reason):
    log.error("feed no disponible: %s", reason)
    f = state.get("failure") or {"since": now.isoformat(), "alerted": False}
    state["failure"] = f
    since = datetime.fromisoformat(f["since"])
    if not f["alerted"] and now - since > FAILURE_ALERT_AFTER:
        ok = mailer.send(
            "[Blackboard] El recordatorio no puede leer tu calendario",
            f"Llevo más de 24 horas sin poder leer el feed de Blackboard "
            f"(desde {since.astimezone(TZ):%d/%m/%Y %H:%M}, {TZ.key}).\n"
            f"Último error: {reason}\n\nPosible causa: el enlace .ics expiró o cambió. "
            f"Genera uno nuevo y actualiza el secret BLACKBOARD_ICS_URL.\n"
            f"No volveré a avisarte de esto hasta que se recupere.\n")
        f["alerted"] = ok


def send_outbox(outbox, mailer, checkpoint) -> None:
    """Un correo por aviso hasta MAX_EMAILS_PER_RUN; el excedente va en un solo resumen.

    Tras cada envío exitoso se llama a checkpoint() para guardar el state: si el
    proceso cae después, lo ya enviado no se repite.
    """
    if len(outbox) <= MAX_EMAILS_PER_RUN:
        individual, rest = outbox, []
    else:
        individual, rest = outbox[:MAX_EMAILS_PER_RUN - 1], outbox[MAX_EMAILS_PER_RUN - 1:]
    for e, field, t, subject, intro in individual:
        e[field] = mailer.send(f"[Blackboard] {subject}", build_body(t, intro))
        if e[field]:
            checkpoint()
    if rest:
        lines = [f"- {subject} ({fmt_due(t)})" for _, _, t, subject, _ in rest]
        ok = mailer.send(f"[Blackboard] Y {len(rest)} avisos más",
                         "Hay demasiados avisos para enviarlos por separado:\n\n"
                         + "\n".join(lines) + "\n")
        for e, field, *_ in rest:
            e[field] = ok
        if ok:
            checkpoint()


def due_today_ready(t, local_now: datetime) -> bool:
    """¿Toca ya el aviso "Hoy vence"? El día de la entrega desde las REMIND_HOUR:00
    (en la zona configurada); si la entrega es antes de esa hora, desde la primera
    ejecución del día para que no llegue tarde. Las vencidas ya no llegan aquí."""
    due = t["due_dt"]
    if due.date() != local_now.date():
        return False
    remind_at = dtime(REMIND_HOUR, 0)
    if not t["all_day"] and due.time() <= remind_at:
        return True
    return local_now.time() >= remind_at


def process(state, tasks, now, mailer, checkpoint=lambda: None):
    local_now = now.astimezone(TZ)
    today = local_now.date()
    baseline = not state.get("initialized", False)
    known = state["tasks"]
    series_known = {e["uid"] for e in known.values() if e.get("series")}
    outbox = []  # (entrada del state, campo a marcar, tarea, asunto, intro)

    for key, t in sorted(tasks.items(), key=lambda kv: kv[1]["due"]):
        e = known.get(key)
        if e is None:
            # Las series se anuncian una sola vez (su primera ocurrencia).
            silent = baseline or (t["series"] and t["uid"] in series_known)
            if t["series"]:
                series_known.add(t["uid"])
            e = known[key] = {"uid": t["uid"], "series": t["series"],
                              "detected_sent": silent, "today_sent": False}
        elif e.get("due") != t["due"]:
            log.info("fecha cambiada: reinicio recordatorio")
            e["today_sent"] = False
        e["title"], e["due"] = t["title"], t["due"]

        if not e["detected_sent"]:
            outbox.append((e, "detected_sent", t, f"Nueva tarea: {t['title']}",
                           "Se detectó una nueva tarea en tu calendario de Blackboard."))
        if due_today_ready(t, local_now) and not e["today_sent"]:
            outbox.append((e, "today_sent", t, f"Hoy vence: {t['title']}",
                           "Hoy vence esta tarea de Blackboard."))

    send_outbox(outbox, mailer, checkpoint)
    state["initialized"] = True
    cutoff = (today - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    for k in [k for k, e in known.items() if e.get("due", "9") < cutoff]:
        del known[k]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", help="ruta de state.json (por defecto ./state.json)")
    ap.add_argument("--ics-file", help="leer el .ics de un archivo (pruebas)")
    ap.add_argument("--now", help="fecha/hora ISO simulada (pruebas)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    dry = os.environ.get("DRY_RUN") == "1"
    now = datetime.fromisoformat(args.now).replace(tzinfo=TZ) if args.now \
        else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)

    state_path = Path(args.state or "state.json")
    persist = (not dry) or bool(args.state)
    state = load_state(state_path)
    mailer = Mailer(dry)

    def checkpoint():
        if persist:
            save_state(state_path, state)

    try:
        if args.ics_file:
            text = Path(args.ics_file).read_text(encoding="utf-8")
        else:
            url = os.environ.get("BLACKBOARD_ICS_URL")
            if not url:
                raise SystemExit("Falta BLACKBOARD_ICS_URL")
            text = fetch_ics(url)
        tasks = extract_tasks(parse_calendar(text), now)
    except FeedError as e:
        handle_failure(state, now, mailer, str(e))
    else:
        state["failure"] = None
        process(state, tasks, now, mailer, checkpoint)
    finally:
        mailer.close()
        checkpoint()
    if mailer.failures:
        # Termina en rojo para que GitHub avise por correo del workflow fallido.
        log.error("%d correo(s) sin enviar; se reintentarán en la próxima ejecución",
                  mailer.failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
