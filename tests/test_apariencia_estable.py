#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Red de seguridad para extraer el frontend sin cambiar la apariencia.

Esta prueba existe para una tarea que todavia no empezo: sacar el
HTML/CSS/JS de adentro de `chat_arduino_v2.py` y llevarlo a archivos
aparte. Lo dificil de esa tarea no es mover el codigo, es *demostrar*
que la pantalla quedo igual.

Por que no comparar el archivo byte a byte: al extraer, el HTML se va a
reindentar, van a cambiar saltos de linea y va a aparecer algun
`<link>`. Un hash del cuerpo fallaria por todo eso sin que nada se vea
distinto, y despues de la tercera falsa alarma alguien lo desactiva.

Lo que se compara es lo que de verdad sostiene la apariencia y el
funcionamiento:

  - los `id` de los elementos, que son el contrato entre HTML y JS;
  - los elementos que el JS busca con getElementById;
  - la paleta de colores;
  - los nombres de las variables CSS;
  - que no aparezcan recursos externos.

Si la extraccion mantiene todo eso, la pantalla quedo igual aunque no
haya un solo byte en el mismo lugar. Y si rompe algo, dice exactamente
que falta.

    python -m unittest tests.test_apariencia_estable -v

Para volver a fijar la referencia despues de un cambio de diseno
DELIBERADO:

    python tests/test_apariencia_estable.py --fijar
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCIA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "apariencia_referencia.json")
PUERTO = 8711


# ---------------------------------------------------------------------
# Extraccion de la huella
# ---------------------------------------------------------------------

def huella(html: str) -> dict:
    """Los rasgos de la pagina que no pueden cambiar sin que se note."""
    return {
        "ids": sorted(set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))),
        "buscados": sorted(set(
            re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", html)
            + re.findall(r"\$\(['\"]([^'\"]+)['\"]\)", html)
        )),
        "colores": sorted({c.lower() for c in
                           re.findall(r'#[0-9a-fA-F]{3,8}\b', html)}),
        "variables_css": sorted(set(re.findall(r'(--[a-z0-9-]+)\s*:', html))),
        "externos": sorted(set(
            re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        )),
    }


def diferencias(esperado: dict, obtenido: dict) -> list:
    """Que falta y que sobra, campo por campo."""
    problemas = []
    for campo in sorted(esperado):
        faltan = sorted(set(esperado[campo]) - set(obtenido.get(campo, [])))
        sobran = sorted(set(obtenido.get(campo, [])) - set(esperado[campo]))
        if faltan:
            problemas.append("%s: faltan %s" % (campo, faltan))
        if sobran:
            problemas.append("%s: aparecieron %s" % (campo, sobran))
    return problemas


# ---------------------------------------------------------------------
# Servidor de prueba
# ---------------------------------------------------------------------

def _datos_temporales(sufijo):
    import tempfile
    return tempfile.mkdtemp(prefix="chatide_apariencia_%s_" % sufijo)


def pagina_servida(programa, puerto):
    """Levanta el programa, pide `/` y lo apaga. Devuelve el HTML."""
    entorno = dict(os.environ)
    entorno["CHATIDE_DATA_DIR"] = _datos_temporales(str(puerto))

    proceso = subprocess.Popen(
        [sys.executable, os.path.join(RAIZ, programa), str(puerto)],
        cwd=RAIZ, env=entorno,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        limite = time.time() + 15
        ultimo = None
        while time.time() < limite:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/" % puerto, timeout=2) as r:
                    return r.read().decode("utf-8", "replace")
            except Exception as e:      # todavia no levanto
                ultimo = e
                time.sleep(0.3)
        raise AssertionError("%s no respondio en 15 s (%s)" % (programa, ultimo))
    finally:
        proceso.terminate()
        try:
            proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proceso.kill()


# ---------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------

class AparienciaEstable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REFERENCIA):
            raise unittest.SkipTest(
                "falta %s; generalo con:\n"
                "    python tests/test_apariencia_estable.py --fijar"
                % os.path.basename(REFERENCIA))
        with open(REFERENCIA, encoding="utf-8") as f:
            cls.referencia = json.load(f)
        cls.html = pagina_servida("chat_arduino_v2.py", PUERTO)
        cls.actual = huella(cls.html)

    def test_la_pantalla_no_cambio(self):
        problemas = diferencias(self.referencia["huella"], self.actual)
        if problemas:
            self.fail(
                "La pagina servida ya no coincide con la referencia.\n\n"
                + "\n".join("  - " + p for p in problemas)
                + "\n\nSi el cambio fue a proposito, volve a fijar la "
                  "referencia:\n"
                  "    python tests/test_apariencia_estable.py --fijar")

    def test_sigue_sin_recursos_externos(self):
        """Sin Internet no puede depender de nada de afuera."""
        self.assertEqual(
            self.actual["externos"], [],
            "la pagina carga recursos externos y el chat tiene que "
            "funcionar sin Internet")

    def test_todo_lo_que_busca_el_js_existe_en_el_html(self):
        """Un getElementById sin su id devuelve null y rompe en silencio.

        Es exactamente el error que deja una extraccion a medias: el JS
        se movio a un archivo y un trozo de HTML quedo atras.
        """
        ids = set(self.actual["ids"])
        huerfanos = [b for b in self.actual["buscados"] if b not in ids]
        self.assertEqual(
            huerfanos, [],
            "el JS busca elementos que no estan en el HTML: %s" % huerfanos)


def fijar():
    """Graba la referencia con el estado actual."""
    html = pagina_servida("chat_arduino_v2.py", PUERTO + 1)
    datos = {
        "_comentario": "Referencia de apariencia. Regenerar solo ante un "
                       "cambio de diseno deliberado, nunca para 'arreglar' "
                       "una prueba en rojo.",
        "programa": "chat_arduino_v2.py",
        "bytes_al_fijar": len(html),
        "huella": huella(html),
    }
    with open(REFERENCIA, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
        f.write("\n")
    h = datos["huella"]
    print("Referencia grabada en %s" % REFERENCIA)
    print("  %d ids, %d elementos buscados por el JS, %d colores, "
          "%d variables CSS, %d recursos externos"
          % (len(h["ids"]), len(h["buscados"]), len(h["colores"]),
             len(h["variables_css"]), len(h["externos"])))


if __name__ == "__main__":
    if "--fijar" in sys.argv:
        fijar()
    else:
        unittest.main(verbosity=2)
