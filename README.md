# CHAT.ide

Chat de aula con apariencia de Arduino IDE que funciona entre computadoras y celulares conectados a la misma red local. El chat no necesita Internet: una PC ejecuta el servidor y los demás dispositivos entran desde el navegador.

> Estado actual: prototipo funcional de una sala general. La evolución hacia **NEXUS Team Chat** se realiza de forma incremental y conserva el trabajo original.

## Qué funciona

- Chat de texto multiusuario dentro de una LAN.
- Acceso desde navegador, sin instalar una app en los dispositivos cliente.
- Usuarios con nombres únicos durante la sesión.
- Lista de conectados en escritorio.
- Avisos de entrada y salida.
- Historial en memoria de hasta 300 eventos.
- Reconexión mediante Server-Sent Events (SSE).
- Interfaz adaptable con apariencia de Arduino IDE.
- Cero servicios externos durante la ejecución.

## Requisitos

En la PC servidor:

- Python 3 instalado. El proyecto fue auditado con Python 3.13.14.
- Una conexión Wi-Fi o Ethernet compartida con los alumnos.
- Un puerto TCP permitido por el firewall para la red del aula.

En los dispositivos cliente solo hace falta un navegador moderno.

El proyecto actual usa únicamente la biblioteca estándar de Python. No hay que ejecutar `pip install`.

## Iniciar el servidor

Desde la carpeta del repositorio:

```powershell
python chat_arduino.py
```

El puerto predeterminado es `8000`. Para elegir otro:

```powershell
python chat_arduino.py 8080
```

La consola muestra una o más direcciones:

```text
http://192.168.x.x:8000
```

En la propia PC se puede comprobar con:

```text
http://localhost:8000
```

Si aparecen varias IP, use la correspondiente a la interfaz Wi-Fi o Ethernet de la clase. No use `127.0.0.1`, adaptadores `vEthernet`, WSL, Hyper-V o VPN para los celulares. La selección automática de la IP correcta está asignada como una mejora separada.

La guía completa está en [`docs/LAN_SETUP.md`](docs/LAN_SETUP.md).

## Ejecutar las pruebas

Las pruebas levantan temporalmente el servidor en un puerto libre, comprueban HTTP y SSE y lo cierran al terminar. No modifican el código ni necesitan Internet.

```powershell
python -m unittest discover -s tests -v
```

Cubren:

- carga de la interfaz y ausencia de recursos web externos;
- rutas y códigos HTTP;
- rechazo de JSON inválido, cuerpos grandes y clientes no registrados;
- dos conexiones SSE simultáneas;
- nombres duplicados, presencia, mensajes e historial de eventos;
- tratamiento de HTML como texto;
- normalización y límite de mensajes;
- salida de usuarios.

## Estructura actual

```text
Chat.ide/
├── chat_arduino.py              # prototipo original: servidor + interfaz
├── README.md
├── PLAN_NEXUS_TEAM_CHAT.md      # auditoría y arquitectura propuesta
├── COORDINACION_NEXUS.md        # acuerdos y reparto del equipo
├── docs/
│   └── LAN_SETUP.md
└── tests/
    └── test_chat_server.py
```

`chat_arduino.py` se mantiene como línea base y lanzador compatible. La modularización futura no debe reemplazarlo de golpe.

## Limitaciones actuales

- Una sola sala general.
- Nombres declarados por el usuario; no hay autenticación ni rol de profesor.
- Historial solo en RAM: se pierde al cerrar el servidor.
- Sin mensajes privados, respuestas, menciones, archivos, imágenes, QR ni IA.
- El servidor escucha en todas las interfaces IPv4.
- Sin límites de conexiones, colas o frecuencia de mensajes.
- HTTP sin cifrado; no debe exponerse directamente a Internet.
- La compatibilidad móvil necesita validación física en Android y iPhone/iPad.

## Seguridad de operación

- Utilice una red de aula controlada.
- No configure port forwarding ni publique el puerto en Internet.
- Limite el firewall al perfil/subred adecuados.
- No coloque contraseñas, tokens o API keys en este repositorio.
- No use todavía el prototipo para información sensible.

Los riesgos y controles futuros están documentados en [`PLAN_NEXUS_TEAM_CHAT.md`](PLAN_NEXUS_TEAM_CHAT.md).

## Colaboración

El equipo coordina tareas en [`COORDINACION_NEXUS.md`](COORDINACION_NEXUS.md) y en el issue de coordinación del repositorio.

Reglas básicas:

- una rama por tarea;
- anunciar responsable y archivos antes de editar;
- no trabajar directamente en `main`;
- pull requests pequeños con pruebas y explicación;
- no compartir secretos ni datos privados con herramientas de IA.

## Próximos pasos acordados

1. Automatizar la regresión del prototipo.
2. Verificar una conexión real desde celular.
3. Mejorar la selección de IP LAN.
4. Modularizar gradualmente `communication/` y después `web/`.
5. Añadir límites de conexiones, colas y frecuencia de mensajes.
6. Incorporar persistencia, identidad y funciones colaborativas en fases posteriores.
