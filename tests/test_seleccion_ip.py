#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas de la seleccion de IP para la LAN, en v1 y v2 a la vez.

Codex preguntó si la correccion iba en v1, en v2 o en un `network.py`
aparte. La respuesta de este archivo es: la misma implementacion en los
dos, y estas pruebas corren contra ambos para que no puedan divergir.
Cuando exista `nexus_chat/communication/network.py`, se mueve la funcion
alli y esta misma suite lo verifica sin cambiarle nada mas que el import.

Solo usa unittest de la biblioteca estandar: el proyecto no tiene
dependencias y estas pruebas no deberian agregar la primera.

    python -m unittest discover -s tests -v

Ninguna prueba abre el servidor ni depende de la red: las que necesitan
una lista de direcciones la simulan. La unica que toca la red de verdad
acepta cualquier resultado, incluido None, porque se tiene que poder
correr en una maquina sin conexion.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# chat_arduino_v2 hace dos cosas al importarse que hay que contener:
#   - lee sys.argv[1] para el puerto, y aqui son los argumentos de unittest;
#   - crea el directorio de la base de datos, que con el valor por defecto
#     cae DENTRO del repositorio.
_TMP = tempfile.mkdtemp(prefix="chatide_pruebas_")
os.environ.setdefault("CHATIDE_DATA_DIR", _TMP)

_ARGV = list(sys.argv)
sys.argv = sys.argv[:1]
try:
    import chat_arduino as v1
    import chat_arduino_v2 as v2
finally:
    sys.argv = _ARGV


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class BaseSeleccionIP:
    """Las pruebas. Cada subclase las corre contra un modulo distinto."""

    modulo = None

    # -------------------------------------------------- es_privada ----

    def test_rangos_privados(self):
        for ip in ("10.0.0.5", "10.255.255.254",
                   "172.16.0.1", "172.24.224.1", "172.31.255.254",
                   "192.168.0.20", "192.168.1.1"):
            self.assertTrue(self.modulo.es_privada(ip), ip)

    def test_rangos_publicos(self):
        # 172.15 y 172.32 quedan justo afuera del bloque 172.16-31: es el
        # borde donde falla comparar texto con startswith("172.").
        for ip in ("8.8.8.8", "172.15.0.1", "172.32.0.1", "191.168.0.1"):
            self.assertFalse(self.modulo.es_privada(ip), ip)

    def test_es_privada_con_basura(self):
        for ip in ("", "no.es.una.ip", "192.168.0", "a.b.c.d"):
            self.assertFalse(self.modulo.es_privada(ip), ip)

    # ------------------------------------------------ es_inservible ----

    def test_loopback_y_autoasignada(self):
        for ip in ("127.0.0.1", "127.1.2.3", "169.254.131.57"):
            self.assertTrue(self.modulo.es_inservible(ip), ip)

    def test_direcciones_utiles(self):
        for ip in ("192.168.0.20", "10.0.0.5", "172.24.224.1"):
            self.assertFalse(self.modulo.es_inservible(ip), ip)

    def test_una_ip_publica_no_se_descarta(self):
        """Quedarse solo con las privadas es una regresion.

        Una escuela con IP publica en la placa quedaria sin ninguna
        direccion que mostrar, y sin explicacion de por que.
        """
        for ip in ("200.10.5.4", "8.8.8.8"):
            self.assertFalse(self.modulo.es_inservible(ip), ip)

    def test_basura_se_considera_inservible(self):
        # Al reves que es_privada(): ante algo que no es una IP, lo seguro
        # es no ofrecersela a nadie.
        for ip in ("", "no.es.una.ip", "192.168.0"):
            self.assertTrue(self.modulo.es_inservible(ip), ip)

    # --------------------------------------------------- otras_ips ----

    CASO_REAL = ["192.168.0.20", "172.24.224.1",
                 "169.254.131.57", "169.254.101.157", "127.0.0.1"]

    def otras(self, encontradas, principal):
        with mock.patch.object(self.modulo.socket, "gethostbyname_ex",
                               return_value=("equipo", [], encontradas)):
            return self.modulo.otras_ips(principal)

    def test_descarta_loopback_y_autoasignadas(self):
        salida = [ip for ip, _ in self.otras(self.CASO_REAL, "192.168.0.20")]
        for ip in ("127.0.0.1", "169.254.131.57", "169.254.101.157"):
            self.assertNotIn(ip, salida)

    def test_no_repite_la_principal(self):
        salida = [ip for ip, _ in self.otras(self.CASO_REAL, "192.168.0.20")]
        self.assertNotIn("192.168.0.20", salida)

    def test_marca_el_adaptador_de_wsl(self):
        salida = dict(self.otras(self.CASO_REAL, "192.168.0.20"))
        self.assertIn("172.24.224.1", salida)
        self.assertTrue(salida["172.24.224.1"],
                        "la de WSL tiene que quedar marcada como sospechosa")

    def test_marca_docker_y_virtualbox(self):
        """El caso que el orden por rango no podia resolver.

        Docker Desktop usa 192.168.65.x y VirtualBox 192.168.56.x. Con el
        orden por rango quedaban en prioridad 0, por encima de una LAN
        escolar real en 10.x.
        """
        encontradas = ["10.20.30.40", "192.168.65.1", "192.168.56.1"]
        salida = dict(self.otras(encontradas, "10.20.30.40"))
        self.assertTrue(salida["192.168.65.1"], "Docker")
        self.assertTrue(salida["192.168.56.1"], "VirtualBox")

    def test_una_lan_real_en_diez_gana(self):
        """La de la tabla de rutas manda, sin importar su rango."""
        encontradas = ["10.20.30.40", "192.168.56.1"]
        with mock.patch.object(self.modulo.socket, "gethostbyname_ex",
                               return_value=("equipo", [], encontradas)), \
             mock.patch.object(self.modulo, "ip_de_salida",
                               return_value="10.20.30.40"):
            self.assertEqual(self.modulo.local_ips()[0], "10.20.30.40")

    def test_una_segunda_placa_buena_no_se_marca(self):
        # Un equipo con Wi-Fi y cable. La segunda sirve, y no termina en .1,
        # asi que no se puede confundir con un adaptador virtual.
        salida = dict(self.otras(["192.168.0.20", "192.168.0.44"],
                                 "192.168.0.20"))
        self.assertIn("192.168.0.44", salida)
        self.assertFalse(salida["192.168.0.44"])

    def test_sin_hostname_no_revienta(self):
        with mock.patch.object(self.modulo.socket, "gethostbyname_ex",
                               side_effect=OSError):
            self.assertEqual(self.modulo.otras_ips("192.168.0.20"), [])

    # --------------------------------------------------- local_ips ----

    def test_la_buena_va_primero(self):
        with mock.patch.object(self.modulo, "ip_de_salida",
                               return_value="192.168.0.20"), \
             mock.patch.object(self.modulo, "otras_ips",
                               return_value=[("172.24.224.1", True)]):
            self.assertEqual(self.modulo.local_ips(),
                             ["192.168.0.20", "172.24.224.1"])

    def test_sin_red_util_no_inventa_nada(self):
        with mock.patch.object(self.modulo, "ip_de_salida", return_value=None), \
             mock.patch.object(self.modulo, "otras_ips", return_value=[]):
            self.assertEqual(self.modulo.local_ips(), [])

    def test_nunca_devuelve_una_inservible(self):
        with mock.patch.object(self.modulo.socket, "gethostbyname_ex",
                               return_value=("equipo", [],
                                             ["127.0.0.1", "169.254.1.1"])):
            for ip in self.modulo.local_ips():
                self.assertFalse(self.modulo.es_inservible(ip), ip)

    # ------------------------------------------------ ip_de_salida ----

    def test_en_esta_maquina(self):
        """Acepta cualquier resultado: tiene que correr sin red."""
        ip = self.modulo.ip_de_salida()
        if ip is not None:
            self.assertFalse(self.modulo.es_inservible(ip))
            self.assertEqual(len(ip.split(".")), 4)

    def test_si_el_socket_falla_devuelve_none(self):
        with mock.patch.object(self.modulo.socket, "socket",
                               side_effect=OSError):
            self.assertIsNone(self.modulo.ip_de_salida())

    def test_descarta_una_autoasignada(self):
        """Sin DHCP el sistema puede devolver una 169.254: no sirve."""
        falso = mock.MagicMock()
        falso.getsockname.return_value = ("169.254.131.57", 0)
        with mock.patch.object(self.modulo.socket, "socket",
                               return_value=falso):
            self.assertIsNone(self.modulo.ip_de_salida())


class PruebaV1(BaseSeleccionIP, unittest.TestCase):
    modulo = v1


class PruebaV2(BaseSeleccionIP, unittest.TestCase):
    modulo = v2


class PruebaLasDosCoinciden(unittest.TestCase):
    """v1 y v2 tienen que responder igual. Si divergen, esto lo dice."""

    CASOS = ["192.168.0.20", "172.24.224.1", "192.168.56.1", "192.168.65.1",
             "10.20.30.40", "169.254.131.57", "127.0.0.1", "8.8.8.8",
             "172.15.0.1", "172.16.0.1", "", "no.es.una.ip"]

    def test_misma_respuesta_en_es_privada(self):
        for ip in self.CASOS:
            self.assertEqual(v1.es_privada(ip), v2.es_privada(ip), ip)

    def test_misma_respuesta_en_es_inservible(self):
        for ip in self.CASOS:
            self.assertEqual(v1.es_inservible(ip), v2.es_inservible(ip), ip)

    def test_misma_clasificacion_de_sospechosas(self):
        encontradas = ["192.168.0.20", "172.24.224.1", "192.168.56.1"]
        with mock.patch.object(v1.socket, "gethostbyname_ex",
                               return_value=("equipo", [], encontradas)), \
             mock.patch.object(v2.socket, "gethostbyname_ex",
                               return_value=("equipo", [], encontradas)):
            self.assertEqual(v1.otras_ips("192.168.0.20"),
                             v2.otras_ips("192.168.0.20"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
