# Coordinación: alumno + Codex

Este archivo es el punto de encuentro para evolucionar el proyecto original hacia **NEXUS Team Chat** sin perder el trabajo que ya funciona.

## Regla principal

El prototipo existente es la línea base. No se reemplazará `chat_arduino.py` de golpe ni se cambiará una función que ya funciona sin una prueba que proteja su comportamiento.

## Estado conocido

- Rama base: `main`.
- El chat general por LAN funciona sin Internet.
- El servidor usa Python estándar, HTTP `POST` y Server-Sent Events.
- La interfaz web está embebida en `chat_arduino.py`.
- El historial y los usuarios conectados viven en memoria.
- Todavía no hay autenticación, persistencia, salas, archivos, administración, QR ni IA.
- La auditoría completa y el plan propuesto están en `PLAN_NEXUS_TEAM_CHAT.md`.

## Acuerdos propuestos

Estos puntos necesitan revisión del alumno. Se consideran propuestos, no impuestos, hasta que se respondan en el pull request de coordinación.

1. Conservar Python, el acceso por navegador y el aspecto Arduino.
2. Mantener SSE + HTTP inicialmente; no migrar a WebSockets sin una necesidad comprobada.
3. Mantener `chat_arduino.py` como lanzador compatible aunque después delegue en módulos.
4. El chat local debe funcionar aunque no haya Internet o falle la IA.
5. No usar CDNs ni recursos externos para la interfaz local.
6. No guardar claves en JavaScript, Git, `localStorage` ni archivos versionados.
7. Trabajar en ramas y revisar mediante pull requests; no desarrollar directamente sobre `main`.
8. Antes de cada función nueva, agregar o actualizar pruebas.
9. No mezclar en un mismo cambio modularización, archivos e IA.
10. Los archivos recibidos nunca se ejecutarán ni se guardarán con una ruta proporcionada por el usuario.

## Primera integración propuesta

La primera integración compartida debería ser pequeña y sin cambios visibles para los usuarios:

1. Documentar cómo iniciar y probar el prototipo.
2. Crear pruebas automáticas para las rutas actuales y SSE.
3. Probar dos dispositivos reales en la misma LAN.
4. Registrar los resultados y problemas de firewall/red.
5. Solo después, extraer HTML/CSS/JavaScript conservando el aspecto y las rutas.

No se propone empezar todavía por IA, archivos ni una reescritura del servidor. Primero necesitamos una base verificable que ambos podamos modificar sin romper el chat actual.

## Reparto inicial sugerido

### Alumno

- Explicar qué partes diseñó intencionalmente y cuáles considera provisionales.
- Indicar si está trabajando ya en alguna rama o archivo.
- Confirmar qué experiencia visual no debe cambiar.
- Ejecutar la prueba desde al menos un celular hacia la PC servidor y registrar sistema, navegador y resultado.
- Revisar o ajustar los acuerdos de este documento.

### Codex

- Mantener la auditoría y el plan técnico actualizados.
- Preparar la red de pruebas automatizadas sin cambiar el comportamiento.
- Proponer cambios pequeños, separados y reversibles.
- Documentar riesgos, decisiones y resultados de cada pull request.
- No integrar una decisión controvertida sin dejarla explícita para revisión.

### Profesor

- Resolver decisiones de producto cuando haya dos opciones válidas.
- Definir la política escolar de nombres, historial, moderación, archivos y uso de IA.
- Autorizar cada fase de mayor riesgo.

## Preguntas para el alumno

Por favor responde en el pull request de coordinación:

1. ¿Hay trabajo nuevo que todavía no hayas subido? ¿En qué archivos o funciones?
2. ¿Estás de acuerdo con conservar Python, navegador, SSE y la apariencia Arduino durante la primera fase?
3. ¿Qué parte del programa actual consideras terminada y no quieres que cambie?
4. ¿Qué función quieres construir tú primero?
5. ¿Prefieres encargarte de la interfaz, del servidor, de pruebas o de una función completa?
6. ¿Ya probaste el chat desde un celular? Incluye sistema, navegador y cualquier error.
7. ¿Aceptas comenzar por documentación + pruebas y después modularizar sin cambiar el comportamiento?

## Forma de trabajar juntos

- Este documento mantiene los acuerdos estables.
- El pull request asociado sirve para conversación y aprobación.
- Cada tarea debe tener responsable y archivos previstos antes de empezar.
- Quien inicie una tarea publica primero la rama o lo anota en la tabla siguiente.
- Si dos tareas tocarían la misma zona de `chat_arduino.py`, se coordinan antes de editar.
- Cada pull request indica qué conserva, qué cambia, cómo se probó y qué falta.

## Tablero de tareas

| Tarea | Responsable | Estado | Archivos previstos | Dependencia |
|---|---|---|---|---|
| Revisar y aceptar acuerdos | Alumno + Codex | Esperando respuesta del alumno | `COORDINACION_NEXUS.md` | Ninguna |
| Pruebas de regresión del prototipo | Por acordar | Propuesta | `tests/` | Acuerdos aceptados |
| Guía de arranque y prueba LAN | Por acordar | Propuesta | `README.md`, `docs/LAN_SETUP.md` | Prueba física |
| Extraer frontend sin cambio visual | Por acordar | Bloqueada | `chat_arduino.py`, `nexus_chat/web/` | Pruebas de regresión |
| Persistencia e identidad | Por acordar | Bloqueada | Por diseñar | Modularización |
| Salas y privados | Por acordar | Bloqueada | Por diseñar | Identidad + persistencia |
| Archivos | Por acordar | Bloqueada | Por diseñar | Seguridad + cuotas |
| NEXUS IA | Por acordar | Bloqueada | Por diseñar | Chat estable + secretos backend |

## Registro de decisiones

| Fecha | Decisión | Estado | Participantes |
|---|---|---|---|
| 2026-09-18 | Preservar el prototipo y auditar antes de programar | Aceptada | Profesor + Codex |
| 2026-09-18 | Usar este archivo y su pull request como punto de coordinación | Propuesta | Pendiente del alumno |
| 2026-09-18 | Empezar por pruebas/documentación antes de modularizar | Propuesta | Pendiente del alumno |

## Criterio para comenzar a integrar código

La primera tarea de código puede empezar cuando el alumno:

- confirme que no existe trabajo local pendiente que vaya a chocar;
- acepte o corrija el alcance de la primera integración;
- elija o acepte el reparto de esa tarea.

Mientras esos puntos estén pendientes, esta rama solo contendrá coordinación y documentación. Así evitamos duplicar o sobrescribir su trabajo.
