# Contribuir

¡Gracias por querer ayudar! Issues y PRs son bienvenidos.

## Preparar el entorno

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Antes de enviar un PR

```bash
ruff check . && mypy reminder.py && pytest -q
```

Los tests usan un SMTP falso: nunca envían correos ni leen un feed real.

## Convenciones

- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/es/) (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`).
- **Un cambio, un PR**, con su test si toca el comportamiento.
- **Nunca** pegues en un issue o PR tu URL `.ics`, tus credenciales ni tus tareas reales: cambia los títulos por datos de ejemplo.

## Vulnerabilidades

No abras un issue público: sigue [SECURITY.md](SECURITY.md).
