"""The Print Station has to find a printer that is switched on.

The polite way to find a printer is a multicast probe. Some routers and
some VPNs drop it, and then the printer is sitting there on its own port,
answering perfectly, and the app says no printer answered. The subnet walk
is the fallback for exactly that, and it has to keep working.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

STATION = Path(__file__).resolve().parent

os.environ.setdefault(
    "PRINT_STATION_DATA",
    str(Path(tempfile.gettempdir()) / "malmars-print-station-discovery-test"),
)

if str(STATION) not in sys.path:
    sys.path.insert(0, str(STATION))

spec = importlib.util.spec_from_file_location(
    "malmars_print_station_wgprinter", STATION / "wgprinter.py")
wgprinter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wgprinter
spec.loader.exec_module(wgprinter)


PRINTER_ADDRESS = "192.168.99.99"
PRINTER_INFO = {
    "machine type": "Flashforge Adventurer 5M",
    "machine name": "TESTSERIAL0001",
    "sn": "TESTSERIAL0001",
    "firmware": "v5.2.0",
}


class _FakeLink:
    """The printer's own answer to "who are you"."""

    def __init__(self, address, *args, **kwargs):
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def info(self):
        if self.address == PRINTER_ADDRESS:
            return dict(PRINTER_INFO)
        raise OSError("not a printer")


class _FakeSocket:
    """Only one address on the subnet has a printer on it."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, value):
        pass

    def connect(self, target):
        pass

    def getsockname(self):
        return ("192.168.99.1", 0)

    def close(self):
        pass


class PrinterDiscoveryTests(unittest.TestCase):
    def setUp(self):
        wgprinter._SCAN_CACHE["at"] = 0.0
        wgprinter._SCAN_CACHE["found"] = []

    def test_the_multicast_probe_is_tried_first(self):
        found = [{"name": "polite", "serial": "S1", "address": "10.0.0.2"}]
        with mock.patch.object(wgprinter, "discover", return_value=found) as probe:
            with mock.patch.object(wgprinter, "scan_lan") as scan:
                self.assertEqual(wgprinter.find_printers(), found)
        probe.assert_called_once()
        scan.assert_not_called()

    def test_a_blocked_multicast_probe_falls_back_to_the_subnet_walk(self):
        with mock.patch.object(wgprinter, "discover", return_value=[]):
            with mock.patch.object(wgprinter, "_local_ipv4",
                                   return_value="192.168.99.1"):
                with mock.patch.object(wgprinter, "Printer", _FakeLink):
                    with mock.patch.object(
                            wgprinter.socket, "create_connection",
                            side_effect=lambda address, timeout=None: (
                                _FakeSocket() if address[0] == PRINTER_ADDRESS
                                else (_ for _ in ()).throw(OSError("closed")))):
                        found = wgprinter.find_printers()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["address"], PRINTER_ADDRESS)
        self.assertEqual(found[0]["serial"], "TESTSERIAL0001")
        self.assertEqual(found[0]["name"], "Flashforge Adventurer 5M")

    def test_a_network_with_no_printer_answers_with_nothing(self):
        with mock.patch.object(wgprinter, "_local_ipv4",
                               return_value="192.168.99.1"):
            with mock.patch.object(wgprinter, "Printer", _FakeLink):
                with mock.patch.object(
                        wgprinter.socket, "create_connection",
                        side_effect=OSError("closed")):
                    found = wgprinter.scan_lan()
        self.assertEqual(found, [])

    def test_the_last_scan_is_kept_so_the_network_is_not_walked_twice(self):
        with mock.patch.object(wgprinter, "_local_ipv4",
                               return_value="192.168.99.1"):
            with mock.patch.object(wgprinter, "Printer", _FakeLink):
                with mock.patch.object(
                        wgprinter.socket, "create_connection",
                        side_effect=lambda address, timeout=None: (
                            _FakeSocket() if address[0] == PRINTER_ADDRESS
                            else (_ for _ in ()).throw(OSError("closed")))):
                    first = wgprinter.scan_lan()
                    with mock.patch.object(wgprinter.socket, "create_connection",
                                           side_effect=AssertionError(
                                               "the network was walked twice")):
                        second = wgprinter.scan_lan()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
