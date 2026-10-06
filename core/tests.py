import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.test import Client, TestCase
from django.utils import timezone
from django.contrib.auth.models import User

from core.models import (
    LeituraScada,
    SystemConfiguration,
    ScadaDataPoint,
    ScadaPointValue,
    ScadaPointValueAnnotation,
)
from core.models_scada import ScadaDataPoint as ScadaDPTarget
from core.profile_logic import ProfileLogic
from core.routers import ScadaRouter
from core.scada_repository import (
    ScadaRepository,
    DATA_TYPE_BINARY,
    DATA_TYPE_MULTISTATE,
    DATA_TYPE_NUMERIC,
    DATA_TYPE_ALPHANUMERIC,
    STATUS_OK,
    STATUS_EMPTY,
    STATUS_NOT_FOUND,
    STATUS_NO_READINGS,
    STATUS_SCADA_OFFLINE,
    STATUS_STALE,
)
from core.scada_service import CrossProcessLock, ScadaAcquisitionService


class ScadaRouterTestCase(TestCase):
    """Testa isolamento estrito de bancos de dados pelo ScadaRouter."""

    def setUp(self):
        self.router = ScadaRouter()

    def test_db_for_read(self):
        # Modelos não gerenciados do Scada leem exclusivamente no banco 'scada'
        self.assertEqual(self.router.db_for_read(ScadaDataPoint), 'scada')
        self.assertEqual(self.router.db_for_read(ScadaPointValue), 'scada')
        self.assertEqual(self.router.db_for_read(ScadaPointValueAnnotation), 'scada')

        # Modelos locais leem em 'default'
        self.assertEqual(self.router.db_for_read(LeituraScada), 'default')
        self.assertEqual(self.router.db_for_read(SystemConfiguration), 'default')

    def test_db_for_write_blocks_scada(self):
        # Qualquer tentativa de escrita nos modelos Scada lança PermissionError
        with self.assertRaises(PermissionError):
            self.router.db_for_write(ScadaDataPoint)

        with self.assertRaises(PermissionError):
            self.router.db_for_write(ScadaPointValue)

        # Modelos locais podem escrever no 'default'
        self.assertEqual(self.router.db_for_write(LeituraScada), 'default')
        self.assertEqual(self.router.db_for_write(SystemConfiguration), 'default')

    def test_allow_migrate_blocks_scada(self):
        # NUNCA permite migrações no banco 'scada'
        self.assertFalse(self.router.allow_migrate('scada', 'core'))
        self.assertFalse(self.router.allow_migrate('scada', 'auth'))

        # Modelos locais migram em 'default'
        self.assertTrue(self.router.allow_migrate('default', 'core', model=LeituraScada))
        self.assertTrue(self.router.allow_migrate('default', 'core', model=SystemConfiguration))

        # Modelos unmanaged não migram nem em 'default'
        self.assertFalse(self.router.allow_migrate('default', 'core', model=ScadaDataPoint))

    def test_allow_relation(self):
        # Impede relações entre tabelas Scada e tabelas locais
        self.assertFalse(self.router.allow_relation(LeituraScada(), ScadaDataPoint()))
        self.assertTrue(self.router.allow_relation(LeituraScada(), SystemConfiguration()))


class ScadaRepositoryTestCase(TestCase):
    """Testa regras de decodificação, preservação de zeros e cursor incremental."""

    def setUp(self):
        self.repo = ScadaRepository(xid_cache_ttl=60, failed_xid_ttl=5)

    def test_normalize_binary_preserves_false(self):
        # Deve preservar False e não confundir com erro ou nulo
        val, sval = self.repo.normalize_value(DATA_TYPE_BINARY, 0.0)
        self.assertIs(val, False)
        self.assertEqual(sval, "0")

        val, sval = self.repo.normalize_value(DATA_TYPE_BINARY, 1.0)
        self.assertIs(val, True)
        self.assertEqual(sval, "1")

        val, sval = self.repo.normalize_value(DATA_TYPE_BINARY, None)
        self.assertIs(val, False)

    def test_normalize_multistate_preserves_zero(self):
        # 0 é estado legítimo (ex: máquina parada ou normal)
        val, sval = self.repo.normalize_value(DATA_TYPE_MULTISTATE, 0.0)
        self.assertEqual(val, 0)
        self.assertEqual(sval, "0")

        val, sval = self.repo.normalize_value(DATA_TYPE_MULTISTATE, 6.0)
        self.assertEqual(val, 6)
        self.assertEqual(sval, "6")

    def test_normalize_numeric_preserves_zero_float(self):
        # 0.0 é cota ou pressão zerada legítima
        val, sval = self.repo.normalize_value(DATA_TYPE_NUMERIC, 0.0)
        self.assertEqual(val, 0)
        self.assertEqual(sval, "0")

        val, sval = self.repo.normalize_value(DATA_TYPE_NUMERIC, 2.3456)
        self.assertEqual(val, 2.346)
        self.assertEqual(sval, "2.346")

    def test_normalize_alphanumeric_preserves_empty_string(self):
        # String vazia é texto legítimo
        val, sval = self.repo.normalize_value(DATA_TYPE_ALPHANUMERIC, None, text_short="")
        self.assertEqual(val, "")
        self.assertEqual(sval, "")

        payload = '[{"x": 10.0, "y": 2.5}]'
        val, sval = self.repo.normalize_value(DATA_TYPE_ALPHANUMERIC, None, text_long=payload)
        self.assertEqual(val, payload)

    def test_ts_to_datetime_sao_paulo(self):
        # Epoch ms 1700000000000
        dt = self.repo.ts_to_datetime(1700000000000)
        self.assertEqual(dt.tzinfo.key, "America/Sao_Paulo")

    @patch("core.scada_repository.connections")
    def test_get_incremental_readings_with_cursor(self, mock_connections):
        # Mock do cursor MySQL
        mock_cursor = MagicMock()
        mock_connections.__getitem__.return_value.cursor.return_value.__enter__.return_value = mock_cursor

        # Simula metadados do ponto
        self.repo._xid_to_dp_cache["DP_747174"] = (870, 4, "P01_Perfil", time.time() + 100)

        # Simula 2 registros retornados com IDs e Timestamps crescentes
        mock_cursor.fetchall.return_value = [
            (101, 4, None, 1700000001000, None, '[{"x": 1.0, "y": 2.0}]'),
            (102, 4, None, 1700000002000, None, '[{"x": 2.0, "y": 2.1}]'),
        ]

        readings, new_id, new_ts = self.repo.get_incremental_readings(
            xid="DP_747174",
            last_id=100,
            last_ts=1700000000000,
            limit=50,
        )

        self.assertEqual(len(readings), 2)
        self.assertEqual(new_id, 102)
        self.assertEqual(new_ts, 1700000002000)
        self.assertEqual(readings[0]["point_value_id"], 101)
        self.assertEqual(readings[1]["point_value_id"], 102)

    def test_diagnosticar_xid_empty(self):
        res = self.repo.diagnosticar_xid("")
        self.assertEqual(res["status"], STATUS_EMPTY)

    @patch.object(ScadaRepository, "resolve_xid")
    def test_diagnosticar_xid_not_found(self, mock_resolve):
        mock_resolve.return_value = None
        res = self.repo.diagnosticar_xid("XID_INEXISTENTE")
        self.assertEqual(res["status"], STATUS_NOT_FOUND)

    @patch.object(ScadaRepository, "resolve_xid")
    @patch.object(ScadaRepository, "get_last_value")
    def test_diagnosticar_xid_no_readings(self, mock_get_last, mock_resolve):
        mock_resolve.return_value = {"id": 99, "point_name": "Tag Teste"}
        mock_get_last.return_value = None
        res = self.repo.diagnosticar_xid("DP_TESTE")
        self.assertEqual(res["status"], STATUS_NO_READINGS)

    @patch.object(ScadaRepository, "resolve_xid")
    @patch.object(ScadaRepository, "get_last_value")
    def test_diagnosticar_xid_stale_and_ok(self, mock_get_last, mock_resolve):
        mock_resolve.return_value = {"id": 99, "point_name": "Tag Teste"}

        # Amostra antiga (100 segundos atrás -> STALE com limite de 30s)
        old_ts = int((time.time() - 100) * 1000)
        mock_get_last.return_value = {
            "data_point_id": 99,
            "data_type": 1,
            "value": True,
            "str_value": "1",
            "ts": old_ts,
            "datetime": timezone.now(),
        }
        res_stale = self.repo.diagnosticar_xid("DP_TESTE", stale_limit_seconds=30)
        self.assertEqual(res_stale["status"], STATUS_STALE)

        # Amostra recente (1 segundo atrás -> OK)
        recent_ts = int((time.time() - 1) * 1000)
        mock_get_last.return_value["ts"] = recent_ts
        res_ok = self.repo.diagnosticar_xid("DP_TESTE", stale_limit_seconds=30)
        self.assertEqual(res_ok["status"], STATUS_OK)


class ScadaAcquisitionMachineTestCase(TestCase):
    """Testa a máquina de estados de ciclo, parsing de JSON e filtro espacial."""

    def setUp(self):
        self.service = ScadaAcquisitionService(override_source="rest_api")
        self.config = SystemConfiguration.get_config()

    def test_parse_coordinate_payload(self):
        # Array JSON
        payload = '[{"x": 12.4, "y": 2.35}, {"x": 14.8, "y": 2.38}]'
        coords = self.service.parse_coordinate_payload(payload)
        self.assertEqual(coords, [(12.4, 2.35), (14.8, 2.38)])

        # Objeto único
        coords_single = self.service.parse_coordinate_payload('{"x": 5.0, "y": 10.0}')
        self.assertEqual(coords_single, [(5.0, 10.0)])

        # JSON inválido
        self.assertEqual(self.service.parse_coordinate_payload("inválido"), [])
        self.assertEqual(self.service.parse_coordinate_payload(""), [])

    @patch.object(ScadaAcquisitionService, "read_trigger")
    @patch.object(ScadaAcquisitionService, "_collect_measurements_rest")
    def test_state_machine_transitions(self, mock_collect, mock_trigger):
        # 1. Ciclo Idle (Trigger = False)
        mock_trigger.return_value = False
        res1 = self.service.process_cycle()
        self.assertEqual(res1["status"], "IDLE")
        self.assertFalse(self.service.is_scanning)
        self.assertIsNone(self.service.current_scan_id)

        # 2. Borda de Subida (Trigger = True) -> Inicia Scan
        mock_trigger.return_value = True
        mock_collect.return_value = 5
        res2 = self.service.process_cycle()
        self.assertEqual(res2["status"], "SCANNING")
        self.assertTrue(self.service.is_scanning)
        self.assertIsNotNone(self.service.current_scan_id)
        scan_id_iniciado = self.service.current_scan_id

        # 3. Permanece ativo (Trigger = True)
        res3 = self.service.process_cycle()
        self.assertEqual(res3["status"], "SCANNING")
        self.assertEqual(self.service.current_scan_id, scan_id_iniciado)

        # 4. Borda de Descida (Trigger = False) -> Finaliza Scan
        mock_trigger.return_value = False
        res4 = self.service.process_cycle()
        self.assertEqual(res4["status"], "IDLE")
        self.assertFalse(self.service.is_scanning)
        self.assertIsNone(self.service.current_scan_id)

    def test_spatial_filter_discards_small_delta(self):
        # Simula salvamento com filtro espacial delta_x > 0.1
        self.service.is_scanning = True
        self.service.current_scan_id = "SCAN_TEST_SPATIAL"
        self.service.last_valid_x = 10.0

        # Ponto com delta_x = 0.05 (menor que 0.1) -> descartado
        with patch.object(self.service, "parse_coordinate_payload") as mock_parse:
            mock_parse.return_value = [(10.05, 2.34)]
            with patch.object(self.service.http_session, "get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"value": "dummy", "ts": 1700000000000}
                mock_get.return_value = mock_resp

                saved = self.service._collect_measurements_rest(self.config)
                self.assertEqual(saved, 0)
                self.assertEqual(self.service.last_valid_x, 10.0)

        # Ponto com delta_x = 0.5 (maior que 0.1) -> gravado
        with patch.object(self.service, "parse_coordinate_payload") as mock_parse:
            mock_parse.return_value = [(10.5, 2.34)]
            with patch.object(self.service.http_session, "get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"value": "dummy", "ts": 1700000000000}
                mock_get.return_value = mock_resp

                saved = self.service._collect_measurements_rest(self.config)
                self.assertEqual(saved, 1)
                self.assertEqual(self.service.last_valid_x, 10.5)
                self.assertTrue(LeituraScada.objects.filter(scan_id="SCAN_TEST_SPATIAL").exists())


class CrossProcessLockTestCase(TestCase):
    """Testa trava exclusiva de processo."""

    def test_lock_acquire_and_release(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test_worker.lock"
            lock1 = CrossProcessLock(lock_path)
            acquired1 = lock1.acquire()
            self.assertTrue(acquired1)

            # Segunda tentativa no mesmo arquivo deve falhar
            lock2 = CrossProcessLock(lock_path)
            acquired2 = lock2.acquire()
            self.assertFalse(acquired2)

            # Libera lock1
            lock1.release()

            # Agora lock2 deve conseguir
            acquired3 = lock2.acquire()
            self.assertTrue(acquired3)
            lock2.release()


class ProfileLogicTestCase(TestCase):
    """Testa classificação dimensional e tolerâncias geométricas."""

    def setUp(self):
        # Garante configuração no banco
        self.config = SystemConfiguration.get_config()
        self.config.espessura_borda_nominal = 2.0
        self.config.espessura_centro_nominal = 10.0
        self.config.tolerancia_positiva = 0.5
        self.config.save()

    def test_classificacao_zona_borda(self):
        # Ponto Y=2.1 está muito mais próximo de 2.0 (borda) do que de 10.0 (centro)
        res = ProfileLogic.validar_ponto(x=10.0, y=2.1)
        self.assertEqual(res["zona"], "Borda (Nível)")
        self.assertEqual(res["alvo"], 2.0)
        self.assertEqual(res["lim_min"], 2.0)
        self.assertEqual(res["lim_max"], 2.5)
        self.assertTrue(res["is_ok"])

    def test_classificacao_zona_centro(self):
        # Ponto Y=10.2 está mais próximo de 10.0 (centro)
        res = ProfileLogic.validar_ponto(x=100.0, y=10.2)
        self.assertEqual(res["zona"], "Centro (Nível)")
        self.assertEqual(res["alvo"], 10.0)
        self.assertEqual(res["lim_min"], 10.0)
        self.assertEqual(res["lim_max"], 10.5)
        self.assertTrue(res["is_ok"])

    def test_fora_de_tolerancia_nok(self):
        # Y=2.8 na borda ultrapassa o limite máximo (2.5)
        res = ProfileLogic.validar_ponto(x=10.0, y=2.8)
        self.assertFalse(res["is_ok"])

        # Y=1.8 na borda está abaixo do limite mínimo (2.0)
        res_abaixo = ProfileLogic.validar_ponto(x=10.0, y=1.8)
        self.assertFalse(res_abaixo["is_ok"])


class ViewsAndApiTestCase(TestCase):
    """Testa estabilidade dos endpoints web, APIs e relatórios CSV."""

    def setUp(self):
        self.client = Client()
        self.now = timezone.now()
        # Cria dados sintéticos para um scan
        LeituraScada.objects.create(
            scan_id="SCAN_TEST_VIEW_01",
            xid="DP_747174",
            eixo_x=10.0,
            eixo_y=2.1,
            data_leitura=self.now,
        )
        LeituraScada.objects.create(
            scan_id="SCAN_TEST_VIEW_01",
            xid="DP_747174",
            eixo_x=20.0,
            eixo_y=10.2,
            data_leitura=self.now,
        )

    def test_dashboard_view_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Perfil de Espessura")

    def test_api_scans_list(self):
        resp = self.client.get("/api/scans/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("scans", data)
        self.assertIn("SCAN_TEST_VIEW_01", data["scans"])

    def test_api_dados_scada(self):
        resp = self.client.get("/api/dados/?scan_id=SCAN_TEST_VIEW_01")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["scan_id"], "SCAN_TEST_VIEW_01")
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["data"]), 2)
        ponto = data["data"][0]
        self.assertIn("x", ponto)
        self.assertIn("y", ponto)
        self.assertIn("is_ok", ponto)
        self.assertIn("zona", ponto)

    def test_exportar_csv(self):
        resp = self.client.get("/exportar/csv/?scan_id=SCAN_TEST_VIEW_01")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        content = resp.content.decode("utf-8")
        self.assertIn("Scan ID;X (mm);Y (mm);Status;Zona", content)
        self.assertIn("SCAN_TEST_VIEW_01", content)
        self.assertIn("10,0", content)  # Vírgula decimal brasileira

    def test_admin_scada_diagnostico_view(self):
        admin_user = User.objects.create_superuser("adm_test", "adm@test.com", "pass123")
        self.client.force_login(admin_user)

        # GET sem parâmetro
        resp = self.client.get("/admin/scada-diagnostico/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Diagnóstico de Data Point")

        # GET com parâmetro XID
        with patch.object(ScadaRepository, "diagnosticar_xid") as mock_diag:
            mock_diag.return_value = {
                "xid": "DP_TEST",
                "status": "OK",
                "mensagem": "Operação normal",
                "data_point_id": 100,
                "point_name": "Sensor",
                "data_type": 1,
                "valor": True,
                "str_valor": "1",
                "timestamp": self.now,
                "idade_segundos": 2,
            }
            resp_xid = self.client.get("/admin/scada-diagnostico/?xid=DP_TEST")
            self.assertEqual(resp_xid.status_code, 200)
            self.assertContains(resp_xid, "DP_TEST")
            self.assertContains(resp_xid, "Operação normal")

    def test_admin_leitura_scada_list(self):
        admin_user = User.objects.create_superuser("adm_test2", "adm2@test.com", "pass123")
        self.client.force_login(admin_user)
        resp = self.client.get("/admin/core/leiturascada/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SCAN_TEST_VIEW_01")



class SystemConfigurationAdminValidationTestCase(TestCase):
    """Testa validações preventivas do modelo SystemConfiguration."""

    def test_validations(self):
        config = SystemConfiguration(
            data_source="rest_api",
            xid_gatilho="",  # Inválido
            intervalo_consulta=-0.1,  # Inválido
            delta_x_minimo=-1.0,  # Inválido
            tolerancia_positiva=-0.5,  # Inválido
            espessura_borda_nominal=0.0,  # Inválido
        )
        with self.assertRaises(ValidationError) as ctx:
            config.clean()

        errs = ctx.exception.message_dict
        self.assertIn("xid_gatilho", errs)
        self.assertIn("intervalo_consulta", errs)
        self.assertIn("delta_x_minimo", errs)
        self.assertIn("tolerancia_positiva", errs)
        self.assertIn("espessura_borda_nominal", errs)

    def test_singleton_preservation(self):
        # Primeiro save cria pk=1
        c1 = SystemConfiguration.objects.create(pk=1)
        # Segundo save com pk=2 é forçado para pk=1
        c2 = SystemConfiguration(pk=2, intervalo_consulta=0.1)
        c2.save()
        self.assertEqual(c2.pk, 1)
        self.assertEqual(SystemConfiguration.objects.count(), 1)
