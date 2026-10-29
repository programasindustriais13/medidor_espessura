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
        # Verificação do cálculo de material
        self.assertIn("material", data)
        self.assertEqual(data["material"]["largura"], 10.0)
        self.assertEqual(data["material"]["x_inicial"], 10.0)
        self.assertEqual(data["material"]["x_final"], 20.0)
        self.assertEqual(data["material"]["corte_min"], 0.9)

    def test_calcular_tamanho_material_corte_09(self):
        # Simula medições reais onde o carro andou no vazio antes e depois do material
        leituras_simuladas = [
            {'x': 50.0, 'y': 0.0},    # Antes do material (ar)
            {'x': 75.0, 'y': 0.85},   # Borda externa abaixo do corte
            {'x': 86.0, 'y': 2.1},    # Início do material (> 0.9mm)
            {'x': 250.0, 'y': 10.5},  # Centro
            {'x': 480.0, 'y': 2.3},   # Fim do material (> 0.9mm)
            {'x': 485.0, 'y': 0.4},   # Carro ainda andando após o material
            {'x': 490.0, 'y': 0.0},   # Parada final
        ]
        res = ProfileLogic.calcular_tamanho_material(leituras_simuladas, corte_min=0.9)
        self.assertEqual(res['x_inicial'], 86.0)
        self.assertEqual(res['x_final'], 480.0)
        self.assertEqual(res['largura'], 394.0)  # 480 - 86 = 394
        self.assertEqual(res['pontos_validos'], 3)

        # Caso onde nenhum ponto é > 0.9mm
        vazio = [{'x': 10.0, 'y': 0.2}, {'x': 20.0, 'y': 0.5}]
        res_vazio = ProfileLogic.calcular_tamanho_material(vazio, corte_min=0.9)
        self.assertIsNone(res_vazio['largura'])
        self.assertEqual(res_vazio['pontos_validos'], 0)

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


class ControlledImplementationScenariosTestCase(TestCase):
    """
    Testes obrigatórios cobrindo os cenários industriais A a H:
    A - Ciclo normal (0 -> 1 -> 1 -> 1 -> 0)
    B - Variações de Y com X constante
    C - Deduplicação de registros gêmeos no Scada
    D - Encerramento com medições pendentes (drain)
    E - Falha de comunicação e resiliência
    F - Reinício do coletor (com gatilho 0 e com gatilho 1)
    G - Integração frontend (Chart.js / API)
    H - Django Admin (Point Values removido, demais mantidos)
    """

    def setUp(self):
        self.client = Client()
        self.config = SystemConfiguration.get_config()
        self.config.data_source = "mysql"
        self.config.delta_x_minimo = 0.1
        self.config.save()
        self.service = ScadaAcquisitionService(override_source="mysql")

    # ── Cenário A: Ciclo Normal ────────────────────────────────────────────
    @patch.object(ScadaAcquisitionService, "get_trigger_info")
    @patch.object(ScadaAcquisitionService, "_collect_measurements_mysql")
    def test_cenario_a_ciclo_normal(self, mock_collect, mock_trigger):
        """
        Sequência 0 -> 1 -> 1 -> 1 -> 0.
        Garante início de ciclo na borda de subida, permanência no nível alto
        e encerramento único na borda de descida.
        """
        t0 = 1700000000000
        # 1. Gatilho em repouso (0)
        mock_trigger.return_value = {"active": False, "ts": t0, "datetime": timezone.now()}
        res1 = self.service.process_cycle()
        self.assertEqual(res1["status"], "IDLE")
        self.assertFalse(self.service.is_scanning)

        # 2. Borda de subida (1)
        t1 = t0 + 1000
        mock_trigger.return_value = {"active": True, "ts": t1, "datetime": timezone.now()}
        mock_collect.return_value = 2
        res2 = self.service.process_cycle()
        self.assertEqual(res2["status"], "SCANNING")
        self.assertTrue(self.service.is_scanning)
        scan_id = self.service.current_scan_id
        self.assertIsNotNone(scan_id)

        # 3. Nível alto contínuo (1)
        t2 = t1 + 200
        mock_trigger.return_value = {"active": True, "ts": t2, "datetime": timezone.now()}
        mock_collect.return_value = 3
        res3 = self.service.process_cycle()
        self.assertEqual(res3["status"], "SCANNING")
        self.assertEqual(self.service.current_scan_id, scan_id)

        # 4. Nível alto contínuo (1)
        t3 = t2 + 200
        mock_trigger.return_value = {"active": True, "ts": t3, "datetime": timezone.now()}
        mock_collect.return_value = 1
        res4 = self.service.process_cycle()
        self.assertEqual(res4["status"], "SCANNING")
        self.assertEqual(self.service.current_scan_id, scan_id)

        # 5. Borda de descida (0)
        t4 = t3 + 200
        mock_trigger.return_value = {"active": False, "ts": t4, "datetime": timezone.now()}
        mock_collect.return_value = 0
        res5 = self.service.process_cycle()
        self.assertEqual(res5["status"], "IDLE")
        self.assertFalse(self.service.is_scanning)
        self.assertIsNone(self.service.current_scan_id)
        self.assertEqual(self.service.last_completed_scan_id, scan_id)

    # ── Cenário B: Variações de Y com X constante ───────────────────────────
    def test_cenario_b_variacao_y_com_x_constante(self):
        """
        Exemplo real: [{"x":0,"y":1.91}], [{"x":0,"y":5.30}], [{"x":0,"y":8.24}].
        Todas as 3 medições com X=0 devem ser preservadas sem descarte pelo filtro espacial.
        """
        self.service.is_scanning = True
        self.service.current_scan_id = "SCAN_TEST_CENARIO_B"

        dt_base = timezone.now()
        samples = [
            {"value": '[{"x":0,"y":1.91}]', "ts": 1700000001000, "datetime": dt_base},
            {"value": '[{"x":0,"y":5.30}]', "ts": 1700000001200, "datetime": dt_base},
            {"value": '[{"x":0,"y":8.24}]', "ts": 1700000001400, "datetime": dt_base},
        ]

        with patch.object(self.service.repo, "get_incremental_readings") as mock_inc:
            mock_inc.return_value = (samples, 103, 1700000001400)
            saved = self.service._collect_measurements_mysql(self.config)

            self.assertEqual(saved, 3)
            leituras = list(LeituraScada.objects.filter(scan_id="SCAN_TEST_CENARIO_B").order_by("id"))
            self.assertEqual(len(leituras), 3)

            self.assertEqual(leituras[0].eixo_x, 0.0)
            self.assertEqual(leituras[0].eixo_y, 1.91)

            self.assertEqual(leituras[1].eixo_x, 0.0)
            self.assertEqual(leituras[1].eixo_y, 5.30)

            self.assertEqual(leituras[2].eixo_x, 0.0)
            self.assertEqual(leituras[2].eixo_y, 8.24)

    # ── Cenário C: Duplicação no Scada ─────────────────────────────────────
    def test_cenario_c_duplicacao_no_scada(self):
        """
        Dois registros no Scada possuem mesmo timestamp e mesmo conteúdo (amostras gêmeas).
        O coletor deve descartar a réplica gêmea mas aceitar uma terceira amostra válida distinta.
        """
        self.service.is_scanning = True
        self.service.current_scan_id = "SCAN_TEST_CENARIO_C"

        dt_base = timezone.now()
        # Amostra 1 e Amostra 2 são idênticas no mesmo ts (gêmeas do Scada)
        # Amostra 3 é um evento posterior legítimo
        samples = [
            {"value": '[{"x":0,"y":1.32}]', "ts": 1700000001000, "datetime": dt_base},
            {"value": '[{"x":0,"y":1.32}]', "ts": 1700000001000, "datetime": dt_base},  # Gêmeo
            {"value": '[{"x":0,"y":5.37}]', "ts": 1700000001200, "datetime": dt_base},  # Novo
        ]

        with patch.object(self.service.repo, "get_incremental_readings") as mock_inc:
            mock_inc.return_value = (samples, 103, 1700000001200)
            saved = self.service._collect_measurements_mysql(self.config)

            # Apenas 2 pontos devem ser salvos (o gêmeo é descartado)
            self.assertEqual(saved, 2)
            leituras = LeituraScada.objects.filter(scan_id="SCAN_TEST_CENARIO_C")
            self.assertEqual(leituras.count(), 2)
            valores_y = [l.eixo_y for l in leituras.order_by("id")]
            self.assertEqual(valores_y, [1.32, 5.37])

    # ── Cenário D: Encerramento com medições pendentes ──────────────────────
    def test_cenario_d_encerramento_com_pendencias(self):
        """
        Ao detectar borda de descida (0), o serviço executa drain residual até o
        timestamp do gatilho, preservando os últimos pontos do ciclo antes de fechar o scan.
        """
        self.service._first_run = False
        self.service.is_scanning = True
        self.service.current_scan_id = "SCAN_TEST_CENARIO_D"

        t_end = 1700000005000
        dt_end = timezone.now()

        # Simula medição residual no momento do retorno a 0
        residual_samples = [
            {"value": '[{"x":24.6,"y":0}]', "ts": t_end, "datetime": dt_end},
        ]

        with patch.object(self.service, "read_trigger") as mock_trig:
            # Gatilho desce para 0
            mock_trig.return_value = False
            self.service._last_trigger_info = {"active": False, "ts": t_end, "datetime": dt_end}

            with patch.object(self.service.repo, "get_incremental_readings") as mock_inc:
                mock_inc.return_value = (residual_samples, 201, t_end)
                res = self.service.process_cycle()

                # Verifica que o ciclo foi encerrado
                self.assertEqual(res["status"], "IDLE")
                self.assertFalse(self.service.is_scanning)

                # Verifica que a medição residual foi salva no ciclo que acabou de fechar
                leituras = LeituraScada.objects.filter(scan_id="SCAN_TEST_CENARIO_D")
                self.assertEqual(leituras.count(), 1)
                self.assertEqual(leituras.first().eixo_x, 24.6)
                self.assertEqual(leituras.first().eixo_y, 0.0)

    # ── Cenário E: Falha de comunicação e resiliência ───────────────────────
    def test_cenario_e_falha_comunicacao(self):
        """
        Quando o MySQL/Scada fica inacessível (OperationalError), o serviço
        reporta NO_COMMUNICATION sem inventar medições e sem corromper o histórico.
        Ao restabelecer, retorna a operar normalmente.
        """
        # Cria histórico prévio
        LeituraScada.objects.create(
            scan_id="SCAN_HISTORICO_PREVIO",
            xid="DP_747174",
            eixo_x=10.0,
            eixo_y=2.0,
            data_leitura=timezone.now(),
        )

        with patch.object(self.service, "read_trigger") as mock_trig:
            # 1. Falha de conexão
            mock_trig.return_value = None
            res_fail = self.service.process_cycle()
            self.assertEqual(res_fail["status"], "NO_COMMUNICATION")
            self.assertEqual(res_fail["saved"], 0)

            # Histórico deve permanecer intacto
            self.assertEqual(LeituraScada.objects.count(), 1)

            # 2. Restabelecimento da conexão (gatilho em 0)
            mock_trig.return_value = False
            self.service._last_trigger_info = {"active": False, "ts": 1700000000000, "datetime": timezone.now()}
            res_ok = self.service.process_cycle()
            self.assertEqual(res_ok["status"], "IDLE")

    # ── Cenário F: Reinício do coletor ──────────────────────────────────────
    def test_cenario_f_reinicio_coletor(self):
        """
        F1: Reinício com gatilho em 0 -> Inicia em repouso (IDLE), aguardando subida.
        F2: Reinício com gatilho em 1 -> Assume explicitamente ciclo em andamento.
        """
        # Caso F1: Reinício com gatilho em 0
        service_f1 = ScadaAcquisitionService(override_source="mysql")
        with patch.object(service_f1, "read_trigger") as mock_trig1:
            mock_trig1.return_value = False
            service_f1._last_trigger_info = {"active": False, "ts": 1700000000000, "datetime": timezone.now()}
            res_f1 = service_f1.process_cycle()
            self.assertEqual(res_f1["status"], "IDLE")
            self.assertFalse(service_f1.is_scanning)
            self.assertIsNone(service_f1.current_scan_id)

        # Caso F2: Reinício com gatilho em 1
        service_f2 = ScadaAcquisitionService(override_source="mysql")
        with patch.object(service_f2, "read_trigger") as mock_trig2:
            mock_trig2.return_value = True
            service_f2._last_trigger_info = {"active": True, "ts": 1700000010000, "datetime": timezone.now()}
            with patch.object(service_f2, "_collect_measurements_mysql") as mock_col2:
                mock_col2.return_value = 0
                res_f2 = service_f2.process_cycle()
                self.assertEqual(res_f2["status"], "SCANNING")
                self.assertTrue(service_f2.is_scanning)
                self.assertIsNotNone(service_f2.current_scan_id)
                self.assertEqual(service_f2.last_reading_ts, 1700000010000)
                self.assertEqual(service_f2.last_reading_id, 0)

    # ── Cenário G: Integração Frontend ─────────────────────────────────────
    def test_cenario_g_integracao_frontend(self):
        """
        Verifica que a API entrega os campos esperados pelo Chart.js:
        scan_id, collector_status, is_live_cycle, count e coordenadas estruturadas.
        """
        now = timezone.now()
        LeituraScada.objects.create(
            scan_id="SCAN_FRONTEND_01",
            xid="DP_747174",
            eixo_x=0.0,
            eixo_y=1.91,
            data_leitura=now,
        )
        LeituraScada.objects.create(
            scan_id="SCAN_FRONTEND_01",
            xid="DP_747174",
            eixo_x=0.0,
            eixo_y=5.30,
            data_leitura=now,
        )

        resp = self.client.get("/api/dados/?scan_id=SCAN_FRONTEND_01")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data["scan_id"], "SCAN_FRONTEND_01")
        self.assertEqual(data["count"], 2)
        self.assertIn("collector_status", data)
        self.assertIn("is_live_cycle", data)

        ponto = data["data"][0]
        self.assertEqual(ponto["x"], 0.0)
        self.assertEqual(ponto["y"], 1.91)
        self.assertIn("target", ponto)
        self.assertIn("min", ponto)
        self.assertIn("max", ponto)
        self.assertIn("is_ok", ponto)
        self.assertIn("zona", ponto)

    # ── Cenário H: Django Admin ────────────────────────────────────────────
    def test_cenario_h_admin(self):
        """
        Confirmar que Point Values (Scada-LTS) não está mais registrado no Admin,
        sua rota antiga retorna 404, e os demais modelos e rotas permanecem operacionais.
        """
        from django.contrib import admin as django_admin
        from core.models import ScadaPointValue, ScadaDataPoint, LeituraScada, SystemConfiguration

        # 1. ScadaPointValue não está registrado no admin
        self.assertFalse(django_admin.site.is_registered(ScadaPointValue))

        # 2. Demais modelos continuam registrados
        self.assertTrue(django_admin.site.is_registered(ScadaDataPoint))
        self.assertTrue(django_admin.site.is_registered(LeituraScada))
        self.assertTrue(django_admin.site.is_registered(SystemConfiguration))

        # 3. Rota antiga de Point Values retorna 404
        admin_user = User.objects.create_superuser("super_h", "h@test.com", "pass123")
        self.client.force_login(admin_user)
        resp_pv = self.client.get("/admin/core/scadapointvalue/")
        self.assertEqual(resp_pv.status_code, 404)

        # 4. Rotas ativas no banco local funcionam normalmente (200 OK)
        resp_leitura = self.client.get("/admin/core/leiturascada/")
        self.assertEqual(resp_leitura.status_code, 200)

        resp_config = self.client.get("/admin/core/systemconfiguration/")
        self.assertEqual(resp_config.status_code, 200)

        # 5. Rota personalizada de diagnóstico funciona normalmente (200 OK)
        resp_diag = self.client.get("/admin/scada-diagnostico/")
        self.assertEqual(resp_diag.status_code, 200)



# ═════════════════════════════════════════════════════════════════════════════
# SUÍTE DE TESTES: MEDIÇÕES DIMENSIONAIS AUTOMÁTICAS DA BANDA DE RODAGEM
# ═════════════════════════════════════════════════════════════════════════════

from core.dimensional_analysis import DimensionalAnalyzer
from core.models import ResultadoDimensionalScan


class BandaDimensionalAnalysisTestCase(TestCase):
    """
    Testes automatizados cobrindo rigorosamente todos os 9 cenários dimensionais
    requisitados para a banda de rodagem, com foco em confiabilidade metrológica
    e validação de que a espessura média da coroa utiliza todo o patamar.
    """

    def setUp(self):
        self.config = SystemConfiguration.get_config()
        self.config.corte_min_espessura_banda = 1.0
        self.config.limiar_coroa_relativo = 0.75
        self.config.min_delta_altura_coroa = 0.8
        self.config.min_pontos_scan = 10
        self.config.modo_deteccao_coroa = "auto"
        self.config.save()

    # ── Cenário 1: Banda com coroa central uniforme ────────────────────────
    def test_cenario_1_coroa_central_uniforme(self):
        pts = []
        # Aba lateral esquerda: x=0.0 a 4.0, y=4.0
        for x in [0.0, 1.0, 2.0, 3.0, 4.0]:
            pts.append({'x': x, 'y': 4.0})
        # Rampa de subida: x=4.5 a 5.5
        pts.append({'x': 4.5, 'y': 5.0})
        pts.append({'x': 5.0, 'y': 6.2})
        pts.append({'x': 5.5, 'y': 6.8})
        # Coroa central uniforme: x=6.0 a 14.0, y=7.00
        for x in [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]:
            pts.append({'x': x, 'y': 7.0})
        # Rampa de descida: x=14.5 a 15.5
        pts.append({'x': 14.5, 'y': 6.8})
        pts.append({'x': 15.0, 'y': 6.2})
        pts.append({'x': 15.5, 'y': 5.0})
        # Aba lateral direita: x=16.0 a 20.0, y=4.0
        for x in [16.0, 17.0, 18.0, 19.0, 20.0]:
            pts.append({'x': x, 'y': 4.0})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "CONCLUIDO")
        self.assertEqual(res["largura_total"], 20.0)
        self.assertEqual(res["x_esquerda"], 0.0)
        self.assertEqual(res["x_direita"], 20.0)
        self.assertAlmostEqual(res["x_inicio_coroa"], 6.0, delta=0.5)
        self.assertAlmostEqual(res["x_fim_coroa"], 14.0, delta=0.5)
        self.assertAlmostEqual(res["largura_coroa"], 9.0, delta=1.0)
        # ESPESSURA MÉDIA DA COROA (mm)
        self.assertAlmostEqual(res["espessura_media_coroa"], 7.00, delta=0.05)
        self.assertEqual(res["espessura_min_coroa"], 6.80)
        self.assertEqual(res["espessura_max_coroa"], 7.00)
        self.assertGreaterEqual(res["pontos_coroa"], 9)
        self.assertAlmostEqual(res["espessura_lateral_esq"], 4.00, delta=0.1)
        self.assertAlmostEqual(res["espessura_lateral_dir"], 4.00, delta=0.1)

    # ── Cenário 2: Coroa com diferentes espessuras ao longo da largura ─────
    def test_cenario_2_coroa_diferentes_espessuras_e_intervalo_completo(self):
        """
        Requisito fundamental: verificar que a média da coroa utiliza todo
        o intervalo válido e não apenas o ponto central ou ponto de crista.
        """
        pts = []
        for x in [0.0, 1.0, 2.0, 3.0, 4.0]:
            pts.append({'x': x, 'y': 3.5})
        pts.append({'x': 5.0, 'y': 5.5})

        # Região da coroa com espessuras intencionalmente variáveis:
        # Ponto central x=10.0 tem pico isolado de 9.5 mm
        # Os demais pontos estão entre 7.0 e 7.4 mm
        coroa_coords = [
            (6.0, 7.0),
            (7.0, 7.2),
            (8.0, 7.1),
            (9.0, 7.3),
            (10.0, 9.5),  # Pico central
            (11.0, 7.4),
            (12.0, 7.2),
            (13.0, 7.1),
            (14.0, 7.0),
        ]
        for x, y in coroa_coords:
            pts.append({'x': x, 'y': y})

        pts.append({'x': 15.0, 'y': 5.5})
        for x in [16.0, 17.0, 18.0, 19.0, 20.0]:
            pts.append({'x': x, 'y': 3.5})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "CONCLUIDO")
        # Prova que NÃO utilizou apenas o ponto central (9.5) nem apenas o mínimo (7.0):
        self.assertNotEqual(res["espessura_media_coroa"], 9.5)
        self.assertNotEqual(res["espessura_media_coroa"], 7.0)
        # A média real ponderada de todos os 9 pontos fica em torno de ~7.4 mm
        self.assertTrue(7.2 <= res["espessura_media_coroa"] <= 8.0)
        self.assertEqual(res["espessura_min_coroa"], 7.1)
        self.assertEqual(res["espessura_max_coroa"], 9.5)
        self.assertEqual(res["pontos_coroa"], 7)

    # ── Cenário 3: Quantidade desigual de pontos por região ────────────────
    def test_cenario_3_quantidade_desigual_de_pontos_por_regiao(self):
        pts = []
        # Aba esquerda com poucos pontos (apenas 3)
        pts.append({'x': 0.0, 'y': 3.8})
        pts.append({'x': 2.0, 'y': 3.8})
        pts.append({'x': 4.0, 'y': 4.0})
        pts.append({'x': 5.0, 'y': 5.8})

        # Coroa com alta densidade amostral (50 pontos entre x=6 e x=14)
        for i in range(50):
            x = 6.0 + i * (8.0 / 49.0)
            pts.append({'x': x, 'y': 7.2})

        pts.append({'x': 15.0, 'y': 5.8})
        # Aba direita com poucos pontos (apenas 3)
        pts.append({'x': 16.0, 'y': 3.8})
        pts.append({'x': 18.0, 'y': 3.8})
        pts.append({'x': 20.0, 'y': 3.8})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "CONCLUIDO")
        self.assertAlmostEqual(res["largura_coroa"], 8.0, delta=0.5)
        self.assertAlmostEqual(res["espessura_media_coroa"], 7.20, delta=0.1)
        self.assertGreaterEqual(res["pontos_coroa"], 45)

    # ── Cenário 4: Espaçamento irregular entre leituras X (Ponderação) ──────
    def test_cenario_4_espacamento_irregular_ponderacao_espacial(self):
        """
        Verifica que medições agrupadas/densas em um trecho curto não inflam
        desproporcionalmente a espessura média em relação ao trecho extenso.
        """
        pts = []
        for x in [0.0, 2.0, 4.0]:
            pts.append({'x': x, 'y': 3.5})
        pts.append({'x': 5.0, 'y': 6.0})

        # Na coroa (x=6.0 a 14.0, largura total = 8.0 mm):
        # Trecho 1: x=6 a 10 (largura 4.0 mm, espaçado): 4 pontos com y=8.0
        pts.append({'x': 6.0, 'y': 8.0})
        pts.append({'x': 7.5, 'y': 8.0})
        pts.append({'x': 9.0, 'y': 8.0})
        pts.append({'x': 10.0, 'y': 8.0})

        # Trecho 2: x=10 a 11 (largura de apenas 1.0 mm), com 20 leituras adensadas de y=6.0
        for i in range(20):
            x = 10.05 + i * (0.9 / 20.0)
            pts.append({'x': x, 'y': 6.0})

        # Trecho 3: x=11 a 14 (largura 3.0 mm, espaçado): 3 pontos com y=8.0
        pts.append({'x': 11.0, 'y': 8.0})
        pts.append({'x': 12.5, 'y': 8.0})
        pts.append({'x': 14.0, 'y': 8.0})

        pts.append({'x': 15.0, 'y': 6.0})
        for x in [16.0, 18.0, 20.0]:
            pts.append({'x': x, 'y': 3.5})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "CONCLUIDO")
        # Se fosse média aritmética simples dos 27 pontos da coroa:
        # (7*8.0 + 20*6.0)/27 = 6.51 mm (completamente distorcida pelo clustering)
        # Pela média espacial trapezoidal sobre a distância X (8mm):
        # A região de 6.0mm ocupa apenas 1/8 da coroa, logo a média deve ser ~7.75 mm
        self.assertGreater(res["espessura_media_coroa"], 7.3)

    # ── Cenário 5: Ruídos próximos às transições ───────────────────────────
    def test_cenario_5_ruidos_proximos_as_transicoes(self):
        pts = []
        for x in [0.0, 1.0, 2.0, 3.0, 4.0]:
            pts.append({'x': x, 'y': 4.0})
        # Ruído espúrio pontual na transição
        pts.append({'x': 4.8, 'y': 8.5})  # Spike isolado
        pts.append({'x': 5.2, 'y': 5.5})
        # Coroa estável
        for x in [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]:
            pts.append({'x': x, 'y': 7.0})
        pts.append({'x': 15.0, 'y': 5.5})
        for x in [16.0, 17.0, 18.0, 19.0, 20.0]:
            pts.append({'x': x, 'y': 4.0})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "CONCLUIDO")
        self.assertAlmostEqual(res["espessura_media_coroa"], 7.00, delta=0.2)

    # ── Cenário 6: Ausência de uma ou das duas laterais ────────────────────
    def test_cenario_6_ausencia_de_uma_das_laterais(self):
        pts = []
        # Peça inicia direto na rampa/coroa (sem aba lateral esquerda)
        for x in [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]:
            pts.append({'x': x, 'y': 7.2})
        pts.append({'x': 15.0, 'y': 5.5})
        # Aba lateral direita presente
        for x in [16.0, 17.0, 18.0, 19.0, 20.0]:
            pts.append({'x': x, 'y': 3.9})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        # Deve processar sem erros, identificando que a aba esquerda não está presente
        self.assertIn(res["status"], ["CONCLUIDO", "SEM_COROA_CLARA"])
        if res["status"] == "CONCLUIDO":
            self.assertIsNone(res["espessura_lateral_esq"])
            self.assertIsNotNone(res["espessura_lateral_dir"])

    # ── Cenário 7: Scan interrompido antes do encerramento ─────────────────
    def test_cenario_7_scan_interrompido_dados_insuficientes(self):
        # Apenas 3 pontos coletados antes de o gatilho cair ou ocorrer falha
        pts = [
            {'x': 0.0, 'y': 4.0},
            {'x': 0.5, 'y': 4.1},
            {'x': 1.0, 'y': 4.0},
        ]
        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "DADOS_INSUFICIENTES")
        self.assertIn("insuficiente", res["motivo"].lower())
        self.assertIsNone(res["largura_total"])
        self.assertIsNone(res["largura_coroa"])
        self.assertIsNone(res["espessura_media_coroa"])

    # ── Cenário 8: Perfil sem coroa claramente identificável ───────────────
    def test_cenario_8_perfil_sem_coroa_identificavel(self):
        # Chapa plana ou homogênea sem degrau entre centro e abas (y uniforme ~4.0mm)
        pts = []
        for x in range(25):
            pts.append({'x': float(x), 'y': 4.0 + (x % 3) * 0.05})

        res = DimensionalAnalyzer.analisar_perfil(pts, config=self.config)
        self.assertEqual(res["status"], "SEM_COROA_CLARA")
        self.assertIn("uniforme", res["motivo"].lower())
        # Largura total pode ser determinada, mas coroa não
        self.assertEqual(res["largura_total"], 24.0)
        self.assertIsNone(res["largura_coroa"])
        self.assertIsNone(res["espessura_media_coroa"])

    # ── Cenário 9: Scan histórico já armazenado e persistência ─────────────
    def test_cenario_9_scan_historico_ja_armazenado(self):
        scan_id = "SCAN_TEST_HISTORICO_99"
        # Gravar leituras no banco
        leituras_db = []
        # Esquerda
        for x in [0.0, 1.0, 2.0, 3.0, 4.0]:
            leituras_db.append(LeituraScada(scan_id=scan_id, xid="DP_747174", eixo_x=x, eixo_y=4.1, data_leitura=timezone.now()))
        # Coroa
        for x in [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]:
            leituras_db.append(LeituraScada(scan_id=scan_id, xid="DP_747174", eixo_x=x, eixo_y=7.15, data_leitura=timezone.now()))
        # Direita
        for x in [16.0, 17.0, 18.0, 19.0, 20.0]:
            leituras_db.append(LeituraScada(scan_id=scan_id, xid="DP_747174", eixo_x=x, eixo_y=4.1, data_leitura=timezone.now()))

        LeituraScada.objects.bulk_create(leituras_db)

        # 1. Primeira chamada: calcula e persiste
        dim1 = DimensionalAnalyzer.obter_ou_calcular_dimensoes(scan_id, config=self.config)
        self.assertEqual(dim1["status"], "CONCLUIDO")
        self.assertAlmostEqual(dim1["espessura_media_coroa"], 7.15, delta=0.05)

        # Verifica se registro foi persistido na tabela ResultadoDimensionalScan
        obj = ResultadoDimensionalScan.objects.filter(scan_id=scan_id).first()
        self.assertIsNotNone(obj)
        self.assertEqual(obj.status_leitura, "CONCLUIDO")
        self.assertAlmostEqual(obj.espessura_media_coroa, 7.15, delta=0.05)

        # 2. Segunda chamada: recupera do banco com resultado idêntico
        dim2 = DimensionalAnalyzer.obter_ou_calcular_dimensoes(scan_id, config=self.config)
        self.assertEqual(dim1["espessura_media_coroa"], dim2["espessura_media_coroa"])
        self.assertEqual(dim1["largura_total"], dim2["largura_total"])

        # 3. Teste via API HTTP /api/dados/
        resp = self.client.get(f"/api/dados/?scan_id={scan_id}")
        self.assertEqual(resp.status_code, 200)
        json_data = resp.json()
        self.assertIn("dimensoes", json_data)
        self.assertEqual(json_data["dimensoes"]["status"], "CONCLUIDO")
        self.assertAlmostEqual(json_data["dimensoes"]["espessura_media_coroa"], 7.15, delta=0.05)

    # ── Teste Admin: Registro de ResultadoDimensionalScan no Admin ─────────
    def test_admin_resultado_dimensional_registrado(self):
        from django.contrib import admin as django_admin
        self.assertTrue(django_admin.site.is_registered(ResultadoDimensionalScan))

        admin_user = User.objects.create_superuser("super_dim", "dim@test.com", "pass123")
        self.client.force_login(admin_user)
        resp = self.client.get("/admin/core/resultadodimensionalscan/")
        self.assertEqual(resp.status_code, 200)
