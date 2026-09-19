#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas de la seleccion de IP para la LAN.

Solo usa unittest de la biblioteca estandar: el proyecto no tiene
dependencias y estas pruebas no deberian agregar la primera.

    python -m unittest discover -s tests -v

Ninguna prueba abre el servidor ni depende de la red: las que necesitan
una lista de direcciones la simulan. La unica que toca la red de verdad
acepta cualquier resultado, incluido None, porque se tiene que poder
correr en una maquina sin conexion.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_arduino as chat


class PruebaEsPrivada(unittest.TestCase):

    def test_rangos_privados(self):
        for ip in ("10.0.0.5", "10.255.255.254",
                   "172.16.0.1", "172.24.224.1", "172.31.255.254",
                   "192.168.0.20", "192.168.1.1"):
            self.assertTrue(chat.es_privada(ip), ip)

    def test_rangos_publicos(self):
        # 172.15 y 172.32 quedan justo afuera del bloque 172.16-31: son
        # el borde donde es facil equivocarse al escribir la condicion.
        for ip in ("8.8.8.8", "172.15.0.1", "172.32.0.1", "191.168.0.1"):
            self.assertFalse(chat.es_privada(ip), ip)

    def test_basura(self):
        for ip in ("", "no.es.una.ip", "192.168.0", "a.b.c.d"):
            self.assertFalse(chat.es_privada(ip), ip)


class PruebaEsInservible(unittest.TestCase):

    def test_loopback_y_autoasignada(self):
        for ip in ("127.0.0.1", "127.1.2.3", "169.254.131.57"):
            self.assertTrue(chat.es_inservible(ip), ip)

    def test_direcciones_utiles(self):
        for ip in ("192.168.0.20", "10.0.0.5", "172.24.224.1"):
            self.assertFalse(chat.es_inservible(ip), ip)

    def test_una_ip_publica_no_se_descarta(self):
        """El V2 se queda solo con las privadas; esto lo corrige.

        Una escuela con IP publica en la placa quedaria sin ninguna
        direccion para mostrar, y sin explicacion de por que.
        """
        for ip in ("200.10.5.4", "8.8.8.8"):
            self.assertFalse(chat.es_inservible(ip), ip)

    def test_basura_se_considera_inservible(self):
        # Al reves que es_privada(): ante algo que no es una IP, lo
        # seguro es no ofrecersela a nadie.
        for ip in ("", "no.es.una.ip", "192.168.0"):
            self.assertTrue(chat.es_inservible(ip), ip)


class PruebaOtrasIps(unittest.TestCase):
    """El caso real de la maquina donde se detecto el problema.

    Tiene Wi-Fi en 192.168.0.20, el adaptador de WSL en 172.24.224.1,
    dos direcciones autoasignadas y la Ethernet desconectada. La
    auditoria registro el mismo sintoma con 172.26.96.1.
    """

    CASO_REAL = ["192.168.0.20", "172.24.224.1",
                 "169.254.131.57", "169.254.101.157", "127.0.0.1"]

    def otras(self, encontradas, principal):
        with mock.patch.object(chat.socket, "gethostbyname_ex",
                               return_value=("equipo", [], encontradas)):
            return chat.otras_ips(principal)

    def test_descarta_loopback_y_autoasignadas(self):
        salida = [ip for ip, _ in self.otras(self.CASO_REAL, "192.168.0.20")]
        self.assertNotIn("127.0.0.1", salida)
        self.assertNotIn("169.254.131.57", salida)
        self.assertNotIn("169.254.101.157", salida)

    def test_no_repite_la_principal(self):
        salida = [ip for ip, _ in self.otras(self.CASO_REAL, "192.168.0.20")]
        self.assertNotIn("192.168.0.20", salida)

    def test_marca_el_adaptador_virtual(self):
        salida = dict(self.otras(self.CASO_REAL, "192.168.0.20"))
        self.assertIn("172.24.224.1", salida)
        self.assertTrue(salida["172.24.224.1"],
                        "la de WSL tiene que quedar marcada como sospechosa")

    def test_una_segunda_placa_buena_no_se_marca(self):
        # Un equipo con Wi-Fi y cable. La segunda sirve, y no termina
        # en .1, asi que no se puede confundir con un adaptador virtual.
        salida = dict(self.otras(["192.168.0.20", "192.168.0.44"],
                                 "192.168.0.20"))
        self.assertIn("192.168.0.44", salida)
        self.assertFalse(salida["192.168.0.44"])

    def test_sin_hostname_no_revienta(self):
        with mock.patch.object(chat.socket, "gethostbyname_ex",
                               side_effect=OSError):
            self.assertEqual(chat.otras_ips("192.168.0.20"), [])


class PruebaLocalIps(unittest.TestCase):
    """local_ips() sigue existiendo y ahora ordena bien."""

    def test_la_buena_va_primero(self):
        with mock.patch.object(chat, "ip_de_salida",
                               return_value="192.168.0.20"), \
             mock.patch.object(chat, "otras_ips",
                               return_value=[("172.24.224.1", True)]):
            self.assertEqual(chat.local_ips(),
                             ["192.168.0.20", "172.24.224.1"])

    def test_sin_red_util_no_inventa_nada(self):
        with mock.patch.object(chat, "ip_de_salida", return_value=None), \
             mock.patch.object(chat, "otras_ips", return_value=[]):
            self.assertEqual(chat.local_ips(), [])

    def test_nunca_devuelve_una_inservible(self):
        with mock.patch.object(chat.socket, "gethostbyname_ex",
                               return_value=("equipo", [],
                                             ["127.0.0.1", "169.254.1.1"])):
            for ip in chat.local_ips():
                self.assertFalse(chat.es_inservible(ip), ip)


class PruebaIpDeSalida(unittest.TestCase):

    def test_en_esta_maquina(self):
        """Acepta cualquier resultado: tiene que correr sin red."""
        ip = chat.ip_de_salida()
        if ip is not None:
            self.assertFalse(chat.es_inservible(ip))
            self.assertEqual(len(ip.split(".")), 4)

    def test_si_el_socket_falla_devuelve_none(self):
        with mock.patch.object(chat.socket, "socket", side_effect=OSError):
            self.assertIsNone(chat.ip_de_salida())

    def test_descarta_una_autoasignada(self):
        """Sin DHCP el sistema puede devolver una 169.254: no sirve."""
        falso = mock.MagicMock()
        falso.getsockname.return_value = ("169.254.131.57", 0)
        with mock.patch.object(chat.socket, "socket", return_value=falso):
            self.assertIsNone(chat.ip_de_salida())


if __name__ == "__main__":
    unittest.main(verbosity=2)
