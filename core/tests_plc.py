"""Contrato de aquisição da espessura escalonada no registrador D4 do PLC."""

from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from .models import LeituraScada, SystemConfiguration
from .scada_service import ScadaAcquisitionService
from .scada_repository import ScadaRepository


class PlcThicknessTestCase(TestCase):
    def setUp(self):
        self.config = SystemConfiguration.get_config()
        self.config.data_source = "mysql"
        self.config.modo_aquisicao = "plc"
        self.config.save()

    def test_scada_millimeters_are_used_directly_and_invalid_readings_rejected(self):
        for value in (0.0, 2.5, 10.0, 19.9, 20.0, 20.1):
            with self.subTest(value=value):
                result = ScadaAcquisitionService.calcular_leitura_instantanea(None, value)
                self.assertTrue(result["valido"])
                self.assertEqual(result["espessura_mm"], value)
        for bad in (None, "", "abc", float("nan"), float("inf"), True):
            with self.subTest(bad=bad):
                result = ScadaAcquisitionService.calcular_leitura_instantanea(None, bad)
                self.assertFalse(result["valido"])
                self.assertIsNone(result["espessura_mm"])
        self.assertEqual(ScadaRepository.normalize_value(3, None), (None, ""))

    def test_direct_instant_read_uses_d4_without_active_calibration(self):
        latest = {
            self.config.xid_espessura_plc: {"value": 20.1, "ts": 1700000001000},
        }
        with patch.object(ScadaRepository, "get_last_values_batch", return_value=latest), \
             patch("core.calibration_service.CalibrationService.get_active_calibration",
                   side_effect=AssertionError("calibração chamada")):
            result = ScadaAcquisitionService.obter_leitura_instantanea_direta(self.config)
        self.assertEqual(result["espessura_mm"], 20.1)

    def test_idle_indicator_is_independent_of_trigger_and_position(self):
        service = ScadaAcquisitionService(override_source="mysql")
        latest = {
            self.config.xid_espessura_plc: {"value": 19.9, "ts": 1700000001000},
        }
        with patch.object(service, "read_trigger", return_value=False), \
             patch.object(service.repo, "get_last_values_batch", return_value=latest), \
             patch.object(service, "generate_scan_id") as generate:
            result = service.process_cycle()
        self.assertEqual(result["status"], "IDLE")
        self.assertEqual(result["espessura_atual"]["espessura_mm"], 19.9)
        self.assertIsNone(result["espessura_atual"]["posicao_x"])
        self.assertIsNone(result["scan_id"])
        self.assertEqual(LeituraScada.objects.count(), 0)
        generate.assert_not_called()

    def test_dashboard_api_exposes_live_millimeters_without_rescaling(self):
        live = ScadaAcquisitionService.calcular_leitura_instantanea(None, 20.0)
        with patch("core.views.get_collector_status", return_value={
            "status": "IDLE", "is_scanning": False,
            "scan_id": None, "espessura_atual": live,
        }):
            response = Client().get("/api/dados/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["espessura_atual"]["espessura_mm"], 20.0)

    def test_mysql_pairs_x_and_d4_only_inside_scan_without_calibration(self):
        service = ScadaAcquisitionService(override_source="mysql")
        service.is_scanning = True
        service.current_scan_id = "SCAN_PLC"
        readings = [
            {"xid": self.config.xid_posicao, "value": 80.0, "ts": 1000},
            {"xid": self.config.xid_espessura_plc, "value": 20.0, "ts": 1005},
            {"xid": self.config.xid_espessura_plc, "value": 19.9, "ts": 1010},
            {"xid": self.config.xid_posicao, "value": 400.0, "ts": 2000},
            {"xid": self.config.xid_espessura_plc, "value": 20.1, "ts": 2002},
        ]
        with patch.object(service.repo, "get_incremental_readings_multi", return_value=(readings, 5, 2002)), \
             patch("core.calibration_service.CalibrationService.calcular_espessura", side_effect=AssertionError("calibração chamada")):
            saved = service._collect_measurements_mysql(self.config)
        self.assertEqual(saved, 3)
        rows = list(LeituraScada.objects.filter(scan_id="SCAN_PLC").order_by("data_leitura"))
        self.assertEqual([(r.eixo_x, r.eixo_y) for r in rows],
                         [(80.0, 20.0), (80.0, 19.9), (400.0, 20.1)])
        self.assertTrue(all(r.calibracao_id is None and r.zero_interpolado is None
                            and r.fator_conversao is None and r.valor_bruto is None for r in rows))
        self.assertTrue(all(r.xid == self.config.xid_espessura_plc for r in rows))

    def test_mysql_rejects_incoherent_or_invalid_pairs(self):
        service = ScadaAcquisitionService(override_source="mysql")
        service.is_scanning = True
        service.current_scan_id = "SCAN_BAD"
        readings = [
            {"xid": self.config.xid_posicao, "value": 100, "ts": 1000},
            {"xid": self.config.xid_espessura_plc, "value": None, "ts": 1001},
            {"xid": self.config.xid_espessura_plc, "value": 20.0, "ts": 6000},
        ]
        with patch.object(service.repo, "get_incremental_readings_multi", return_value=(readings, 3, 6000)):
            self.assertEqual(service._collect_measurements_mysql(self.config), 0)
        self.assertFalse(LeituraScada.objects.filter(scan_id="SCAN_BAD").exists())

    def test_zero_is_saved_and_duplicate_pair_is_ignored(self):
        service = ScadaAcquisitionService(override_source="mysql")
        service.is_scanning = True
        service.current_scan_id = "SCAN_ZERO"
        readings = [
            {"xid": self.config.xid_posicao, "value": 80, "ts": 1000},
            {"xid": self.config.xid_espessura_plc, "value": 0, "ts": 1000},
            {"xid": self.config.xid_espessura_plc, "value": 0, "ts": 1000},
        ]
        with patch.object(service.repo, "get_incremental_readings_multi", return_value=(readings, 3, 1000)):
            self.assertEqual(service._collect_measurements_mysql(self.config), 1)
        self.assertEqual(LeituraScada.objects.get(scan_id="SCAN_ZERO").eixo_y, 0.0)

    def test_history_api_and_csv_keep_stored_millimeters(self):
        read_time = timezone.now()
        LeituraScada.objects.create(
            scan_id="SCAN_OLD", xid="DP_632601", eixo_x=80, eixo_y=30.03,
            data_leitura=read_time, valor_bruto=782, status_validade="VALIDO",
        )
        LeituraScada.objects.create(
            scan_id="SCAN_NEW", xid=self.config.xid_espessura_plc, eixo_x=80,
            eixo_y=20.0, data_leitura=read_time, status_validade="VALIDO",
        )
        LeituraScada.objects.create(
            scan_id="SCAN_NEW", xid=self.config.xid_espessura_plc, eixo_x=260,
            eixo_y=10.0, data_leitura=read_time + timedelta(milliseconds=10),
            status_validade="VALIDO",
        )
        client = Client()
        old_data = client.get("/api/dados/?scan_id=SCAN_OLD").json()
        new_data = client.get("/api/dados/?scan_id=SCAN_NEW").json()
        self.assertEqual(old_data["data"][0]["y"], 30.03)
        self.assertEqual(
            [(point["x"], point["y"], point["is_ok"]) for point in new_data["data"]],
            [(80.0, 20.0, False), (260.0, 10.0, True)],
        )
        self.assertIsNone(new_data["calibracao"]["id"])
        csv_old = client.get("/exportar/csv/?scan_id=SCAN_OLD").content.decode("utf-8")
        csv_new = client.get("/exportar/csv/?scan_id=SCAN_NEW").content.decode("utf-8")
        self.assertIn("30,03", csv_old)
        self.assertIn("20,0", csv_new)
        self.assertIn("10,0", csv_new)
        self.assertIn("NOK", csv_new)
        self.assertIn("OK", csv_new)

    def test_rest_pair_uses_d4_without_second_conversion(self):
        self.config.data_source = "rest_api"
        service = ScadaAcquisitionService(override_source="rest_api")
        service.is_scanning = True
        service.current_scan_id = "SCAN_REST"

        class Response:
            status_code = 200
            url = "http://scada/api"

            def __init__(self, value, ts):
                self.value, self.ts = value, ts

            def json(self):
                return {"value": self.value, "ts": self.ts}

        with patch.object(service.http_session, "get", side_effect=[Response(260, 1000), Response(20.0, 1001)]):
            self.assertEqual(service._collect_measurements_rest(self.config), 1)
        row = LeituraScada.objects.get(scan_id="SCAN_REST")
        self.assertEqual((row.eixo_x, row.eixo_y), (260.0, 20.0))
