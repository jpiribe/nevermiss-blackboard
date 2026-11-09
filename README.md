<div align="center">

# 🔔 NeverMiss for Blackboard

**Nunca más una entrega olvidada: tu calendario de Blackboard, directo a tu Gmail.**

Un cron de GitHub Actions lee tu feed iCal cada hora y te escribe **solo** cuando hay algo que importa.

![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/runs%20on-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)
![Gmail](https://img.shields.io/badge/notifica%20por-Gmail-EA4335?logo=gmail&logoColor=white)
[![CI](https://github.com/jpiribe/nevermiss-blackboard/actions/workflows/ci.yml/badge.svg)](https://github.com/jpiribe/nevermiss-blackboard/actions/workflows/ci.yml)
![Licencia MIT](https://img.shields.io/badge/licencia-MIT-yellow)
![Lint](https://img.shields.io/badge/lint-ruff%20%2B%20mypy-D7FF64?logo=ruff&logoColor=black)
![Sin servidor](https://img.shields.io/badge/servidor-ninguno-brightgreen)

[Ejemplo](#-cómo-se-ve) · [Cómo funciona](#-cómo-funciona) · [Inicio rápido](#-inicio-rápido) · [Configuración](#%EF%B8%8F-configuración) · [FAQ](#-preguntas-frecuentes)

</div>

---

## ✨ Características

- 📬 **Solo dos tipos de aviso**, sin ruido: tarea nueva y "vence hoy".
- 🔒 **Sin tu contraseña de Blackboard**: usa únicamente el feed `.ics` de solo lectura.
- ☁️ **Sin servidor ni PC encendida**: corre en GitHub Actions, con secrets cifrados.
- 🧠 **Sin duplicados**: el estado se guarda en `state.json` y se commitea solo cuando cambia.
- 🛡️ **Tolerante a fallos**: si el feed cae más de 24 h te llega un único correo, no una avalancha.
- 🚦 **Límite anti-spam**: máximo 20 correos por ejecución; el excedente va en un resumen.
- 🔁 **Soporta eventos recurrentes** (RRULE) y zonas horarias (la tuya, configurable).
- 🧪 **Probado**: suite de pytest con SMTP falso; nunca envía correos ni toca tu feed real.
- 📌 **Cadena de suministro cuidada**: acciones fijadas por SHA, dependencias con versión exacta y Dependabot.

## 👀 Cómo se ve

```text
Asunto: [Blackboard] Hoy vence: Tarea 3_Ensayo final

Hoy vence esta tarea de Blackboard.

Título: Tarea 3_Ensayo final
Curso: Mi Materia 1
Entrega: viernes 20 de noviembre de 2026, 23:59 (tu zona horaria)
Enlace: https://…
```

## 📨 Cómo funciona

```text
 Blackboard (.ics)  ──►  reminder.py  ──►  Gmail (SMTP)  ──►  📥 tu bandeja
                              │
                              └──►  state.json  (qué ya avisé)
```

| Asunto del correo | Cuándo se envía |
| --- | --- |
| `[Blackboard] Nueva tarea: …` | Aparece una tarea nueva en el feed. |
| `[Blackboard] Hoy vence: …` | El día de la entrega, en la primera ejecución desde las **10:00 de tu zona horaria** (configurable). |

> [!NOTE]
> Si la entrega es antes de las 10:00, el aviso sale en la primera ejecución del día para que no llegue tarde. Si GitHub se salta una ejecución, sale en la siguiente.

**Reglas finas**

- **Primera ejecución = *baseline***: guarda lo existente sin avisos de "nueva", pero sí avisa lo que vence hoy.
- Si cambia la fecha de una tarea, su recordatorio se reinicia (sin nuevo aviso de detección).
- Si hay más de 20 avisos en una corrida, el excedente llega en **un solo correo resumen**.
- Si el feed falla más de 24 h seguidas, recibes **un** correo (y otro solo si se recupera y vuelve a fallar).

### 🗂️ ¿Qué cuenta como "tarea"?

| Tipo de evento | ¿Cuenta? |
| --- | :---: |
| Con hora y duración 0 (`DTSTART == DTEND`, como Blackboard publica las entregas) | ✅ |
| De **todo el día**, de un solo día (vence ese día; aviso "Hoy vence" a las 10:00) | ✅ |
| Con duración (clases, reuniones) | ❌ |
| De varios días (recesos) | ❌ |
| Pasados | ❌ |

- Si tus entregas traen duración, sube `MAX_TASK_MINUTES`.
- **Recurrentes (RRULE)**: cada ocurrencia es una tarea; las nuevas ocurrencias de una serie ya conocida **no** generan aviso de "nueva".
- Una hora sin zona se interpreta en tu zona horaria (`TIMEZONE`).

## 🚀 Inicio rápido

> [!IMPORTANT]
> Necesitas una cuenta de GitHub, una de Gmail con verificación en 2 pasos y acceso al calendario de tu Blackboard.

### 1. Obtén el enlace `.ics` de Blackboard

- **Blackboard Ultra:** Calendario → ⚙ (o menú ⋯) → **Compartir calendario / Get calendar link (iCal)** → copia la URL.
- **Blackboard Original:** Calendario → **Obtener enlace / Compartir calendario**.

Si tu institución no muestra esa opción, el feed está deshabilitado: pídeselo a soporte TI. No hay alternativa sin tu contraseña, y no la usaremos.

> [!WARNING]
> Trata la URL como una contraseña: da acceso de lectura a tu calendario.

### 2. Crea una App Password de Gmail

1. Activa la verificación en 2 pasos: <https://myaccount.google.com/security>
2. Entra a <https://myaccount.google.com/apppasswords> y crea una con el nombre "Blackboard".
3. Copia los 16 caracteres (sin espacios).

### 3. Prepara el repositorio

1. Pulsa **Use this template → Create a new repository** y créalo como **privado** (el workflow guarda en `state.json` los títulos de tus tareas, así que se niega a correr en un repo público).
2. En **Settings → Secrets and variables → Actions → New repository secret** agrega los [secrets](#-secrets).
3. En **Settings → Actions → General → Workflow permissions** elige **Read repository contents** (el workflow pide `contents: write` solo para su job, para commitear `state.json`).
4. En **Settings → Code security** activa **Secret scanning**, **Push protection**, **Dependabot alerts** y **Dependabot security updates** (`.github/dependabot.yml` ya configura las actualizaciones semanales).

### 4. Actívalo

Pestaña **Actions** → "Blackboard reminder" → **Run workflow** (la primera corrida es el baseline). Después corre solo cada hora (`7 * * * *`, UTC).

## ⚙️ Configuración

### 🔑 Secrets

| Secret | Obligatorio | Descripción |
| --- | :---: | --- |
| `BLACKBOARD_ICS_URL` | ✅ | URL del feed iCal de tu calendario. |
| `GMAIL_USER` | ✅ | Cuenta de Gmail que envía los correos. |
| `GMAIL_APP_PASSWORD` | ✅ | App Password de 16 caracteres. |
| `MAIL_TO` | — | Destinatario. Por defecto, igual a `GMAIL_USER`. |

### 🎛️ Variables de entorno

En GitHub se definen en **Settings → Secrets and variables → Actions → Variables** (`TIMEZONE`, `REMIND_HOUR`). Pon al menos `TIMEZONE`, o los avisos se calcularán en UTC.

| Variable | Por defecto | Efecto |
| --- | :---: | --- |
| `TIMEZONE` | `UTC` | Tu zona horaria (nombre IANA, p. ej. `America/Mexico_City`). |
| `REMIND_HOUR` | `10` | Hora (0–23) desde la que se envía el aviso "Hoy vence". |
| `MAX_TASK_MINUTES` | `0` | Duración máxima (min) para considerar un evento como tarea. |
| `DRY_RUN` | — | Con `1`, imprime en vez de enviar y no escribe el state (salvo que pases `--state ruta`). |

### 🏷️ Etiquetar por materia

`courses.json` asocia el título de cada evento con una materia mediante expresiones regulares:

```json
[
  {"pattern": "^Tarea \\d+", "course": "Mi Materia 1"},
  {"pattern": "^Examen|^Práctica", "course": "Mi Materia 2"}
]
```

### ⏱️ Disparo externo cada hora (recomendado)

El `schedule` de GitHub se retrasa o se salta corridas (a veces solo ~3 al día). Para que corra puntual sin tu PC, un servicio externo (p. ej. [cron-job.org](https://cron-job.org)) llama cada hora a la API de GitHub; el `schedule` queda como respaldo. Si coinciden, el `concurrency` las pone en fila y `state.json` evita correos repetidos.

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → nuevo token: solo tu repo `nevermiss-blackboard`, permiso **Actions: Read and write** (nada más). Pon fecha de expiración y anótala para renovarlo.
2. En cron-job.org crea una tarea cada hora en el minuto 7:

   ```http
   POST https://api.github.com/repos/<usuario>/nevermiss-blackboard/actions/workflows/reminder.yml/dispatches
   Authorization: Bearer <token>
   Accept: application/vnd.github+json
   Content-Type: application/json

   {"ref":"main"}
   ```

   Respuesta esperada: `204`.

> [!TIP]
> El token no da acceso al código ni a los secrets, pero con *Actions: Read and write* también podría relanzar o cancelar ejecuciones y leer sus logs. Límitalo a tu repo, ponle caducidad y, si se filtra, bórralo en GitHub.

## 🧪 Desarrollo y pruebas

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q        # usa un SMTP falso: nunca envía correos ni lee tu feed real
```

Uso local real:

```bash
export BLACKBOARD_ICS_URL=… GMAIL_USER=… GMAIL_APP_PASSWORD=…
python reminder.py               # envía de verdad
DRY_RUN=1 python reminder.py     # solo imprime
```

Banderas útiles: `--state ruta`, `--ics-file archivo.ics` (leer de un archivo) y `--now 2026-11-09T10:00` (simular la fecha).

## 🤝 Contribuir

Issues y PRs son bienvenidos. Lee la [guía de contribución](CONTRIBUTING.md) antes de enviar un PR.

## 📄 Licencia

[MIT](LICENSE)

## 🏗️ Decisiones de diseño

<details>
<summary><b>¿Por qué <code>state.json</code> en el repo y no <code>actions/cache</code>?</b></summary>

<br>

La caché se evicta (7 días sin uso o límite de 10 GB) y es inmutable por clave; perderla provocaría un nuevo baseline silencioso. El commit es durable y auditable (el repo es privado), y el `concurrency` del workflow evita corridas solapadas.

</details>

<details>
<summary><b>¿Qué pasa si dos ejecuciones coinciden?</b></summary>

<br>

El `concurrency` las pone en fila. Ante un conflicto en `state.json` gana el de la ejecución actual (la más reciente), con hasta 3 reintentos.

</details>

## ❓ Preguntas frecuentes

<details>
<summary><b>¿Puedo correrlo sin GitHub Actions?</b></summary>

<br>

Sí. Un **cron local / Task Scheduler** ejecutando `python reminder.py` cada hora es gratis y simple, pero solo corre con la computadora encendida y las credenciales quedan en variables de entorno locales. **Actions** corre sin tu PC y con secrets cifrados; su cron puede retrasarse unos minutos en horas pico.

</details>

<details>
<summary><b>No me llega ningún correo</b></summary>

<br>

Revisa que la primera corrida ya haya hecho el baseline (solo avisa lo que vence hoy), que los tres secrets obligatorios estén bien escritos y que tus entregas cumplan las reglas de [qué cuenta como tarea](#%EF%B8%8F-qué-cuenta-como-tarea).

</details>

<details>
<summary><b>¿Es seguro compartir mi URL .ics?</b></summary>

<br>

No la compartas: equivale a una contraseña de lectura de tu calendario. Guárdala solo como secret.

</details>
