"""Escenarios de extremo a extremo con los feeds de ejemplo (antes en run_scenarios.sh)."""
import json
from pathlib import Path

from conftest import subjects

HERE = Path(__file__).parent
V1, V2 = HERE / "sample_v1.ics", HERE / "sample_v2.ics"


def test_flujo_completo(run, smtp):
    # 1) Baseline: nada de "Nueva tarea" por lo que ya existía; nada vence hoy.
    assert run(V1, "2026-10-30T10:07") == 0
    assert smtp.sent == []

    # 2) El día de la entrega, a partir de las 10:00: "Hoy vence".
    run(V1, "2026-10-31T09:07")
    assert smtp.sent == []
    run(V1, "2026-10-31T10:07")
    assert subjects(smtp) == [
        "[Blackboard] Hoy vence: Entrega de portafolio",
        "[Blackboard] Hoy vence: Examen parcial (entrega en línea)",
    ]

    # 3) Repetir la ejecución no envía nada.
    smtp.sent.clear()
    run(V1, "2026-10-31T11:07")
    assert smtp.sent == []

    # 4) Feed v2: dos tareas nuevas; T1 cambia de fecha sin aviso; la serie no se anuncia.
    run(V2, "2026-10-31T12:07")
    assert sorted(subjects(smtp)) == [
        "[Blackboard] Nueva tarea: Proyecto final",
        "[Blackboard] Nueva tarea: Reporte de laboratorio",
    ]

    # 5) T1 (movida al 12-nov) se recuerda ese día; nada más.
    smtp.sent.clear()
    run(V2, "2026-11-12T10:07")
    assert subjects(smtp) == ["[Blackboard] Hoy vence: Tarea 3: Diagrama de red"]

    state = json.loads(run.state.read_text(encoding="utf-8"))
    assert state["initialized"] is True and state["failure"] is None


def test_clases_recesos_y_pasadas_se_ignoran(run, smtp):
    run(V1, "2026-10-30T10:00")
    state = json.loads(run.state.read_text(encoding="utf-8"))
    titles = {e["title"] for e in state["tasks"].values()}
    assert not titles & {"Clase: Teoría (sesión)", "Receso académico", "Tarea 1 (ya vencida)"}
