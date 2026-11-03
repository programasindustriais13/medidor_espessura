import time
from unittest.mock import MagicMock, patch
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from core.models import (
    CalibracaoEspessura,
    PontoCalibracao,
    LeituraScada,
    SystemConfiguration,
    ResultadoDimensionalScan,
)
from core.calibration_service import CalibrationService
from core.profile_logic import ProfileLogic
from core.scada_service import CrossProcessLock, ScadaAcquisitionService


class MultipointCalibrationTestCase(TestCase):
    """
    Suíte completa de testes automatizados para a Calibração Multiponto (15 requisitos).
    """

    def setUp(self):
        CalibrationService.clear_cache()
        # Garante configuração default
        self.config = SystemConfiguration.get_config()
        self.config.modo_aquisicao = "calibrado"
        self.config.save()

        # Obtém calibração inicial (criada pela migration 0006)
        self.calib_inicial = CalibracaoEspessura.objects.filter(ativa=True).first()

    # ── 1. Cadastro da calibração inicial com os seis pontos ──────────────────
    def test_01_cadastro_calibracao_inicial_seis_pontos(self):
        self.assertIsNotNone(self.calib_inicial, "A calibração inicial deve existir e estar ativa.")
        self.assertTrue(self.calib_inicial.ativa)
        self.assertEqual(self.calib_inicial.espessura_bloco_padrao_mm, 30.0)

        pontos = list(self.calib_inicial.pontos.all().order_by("posicao_x"))
        self.assertEqual(len(pontos), 6, "Devem existir exatamente 6 pontos cadastrados na calibração inicial.")

        posicoes_esperadas = [80.0, 170.0, 260.0, 357.0, 450.0, 543.0]
        posicoes_reais = [p.posicao_x for p in pontos]
        self.assertEqual(posicoes_reais, posicoes_esperadas)

    # ── 2. Cálculo correto das médias de leituras ────────────────────────────
    def test_02_calculo_correto_das_medias_de_leituras(self):
        # Testa médias e parsing de strings
        p80 = self.calib_inicial.pontos.get(posicao_x=80.0)
        self.assertEqual(p80.leituras_zero_list, [345.0, 346.0])
        self.assertEqual(p80.zero_medio, 345.5)
        self.assertEqual(p80.leituras_bloco_list, [778.0, 780.0])
        self.assertEqual(p80.bloco_medio, 779.0)
        self.assertEqual(p80.diferenca, 433.5)

        p170 = self.calib_inicial.pontos.get(posicao_x=170.0)
        self.assertEqual(p170.zero_medio, 350.0)
        self.assertEqual(p170.bloco_medio, 783.0)
        self.assertEqual(p170.diferenca, 433.0)

        # Testa criação manual de novo ponto com string delimitada por vírgula e espaços
        novo_ponto = PontoCalibracao(
            calibracao=self.calib_inicial,
            posicao_x=200.0,
            leituras_zero_input="348, 350, 352",
            leituras_bloco_input="780; 784",
        )
        novo_ponto.clean()
        self.assertEqual(novo_ponto.zero_medio, 350.0)
        self.assertEqual(novo_ponto.bloco_medio, 782.0)
        self.assertEqual(novo_ponto.diferenca, 432.0)

    # ── 3. Fator de conversão calculado dinamicamente ────────────────────────
    def test_03_fator_conversao_calculado_dinamicamente(self):
        # Média das diferenças = 433.5 unidades
        # Bloco = 30 mm -> Fator = 30.0 / 433.5 ≈ 0.06920415225
        fator_esperado = 30.0 / 433.5
        self.assertAlmostEqual(self.calib_inicial.fator_conversao, fator_esperado, places=8)

        # Alterar espessura do bloco padrão para 20.0 mm
        self.calib_inicial.espessura_bloco_padrao_mm = 20.0
        self.calib_inicial.save()
        fator_novo = self.calib_inicial.calcular_fator_conversao(salvar=True)
        self.assertAlmostEqual(fator_novo, 20.0 / 433.5, places=8)

        # Restaura
        self.calib_inicial.espessura_bloco_padrao_mm = 30.0
        self.calib_inicial.calcular_fator_conversao(salvar=True)

    # ── 4. Interpolação do zero em posições intermediárias ───────────────────
    def test_04_interpolacao_zero_posicoes_intermediarias(self):
        # Exemplo da especificação:
        # Posição 357 -> zero = 345.5
        # Posição 450 -> zero = 341.5
        # Posição 400 -> zero interpolado = 345.5 + ((400-357)/(450-357)) * (341.5-345.5) ≈ 343.65
        zero_400, err, faixa = CalibrationService.interpolar_zero(400.0, calibracao=self.calib_inicial)
        self.assertIsNone(err)
        self.assertAlmostEqual(zero_400, 343.6505376, places=4)

        # Teste na exata posição de calibração 260
        zero_260, err, _ = CalibrationService.interpolar_zero(260.0, calibracao=self.calib_inicial)
        self.assertIsNone(err)
        self.assertEqual(zero_260, 348.0)

    # ── 5. Medição com bloco de 30 mm nos seis pontos de referência ─────────
    def test_05_medicao_com_bloco_30mm_nos_seis_pontos(self):
        # Valores de leitura com bloco informados na especificação
        pontos_teste = [
            (80.0, 779.0),   # Zero 345.5 -> diff 433.5 -> 30.00 mm
            (170.0, 783.0),  # Zero 350.0 -> diff 433.0 -> 29.965 mm
            (260.0, 782.0),  # Zero 348.0 -> diff 434.0 -> 30.035 mm
            (357.0, 779.0),  # Zero 345.5 -> diff 433.5 -> 30.00 mm
            (450.0, 775.0),  # Zero 341.5 -> diff 433.5 -> 30.00 mm
            (543.0, 771.0),  # Zero 337.5 -> diff 433.5 -> 30.00 mm
        ]

        for pos_x, raw_bloco in pontos_teste:
            res = CalibrationService.calcular_espessura(pos_x, raw_bloco, calibracao=self.calib_inicial)
            self.assertTrue(res["valido"])
            self.assertEqual(res["status"], "VALIDO")
            self.assertIsNotNone(res["espessura_mm"])
            # Todos os pontos devem resultar em aproximadamente 30.00 mm (desvio metrológico < 0.05 mm)
            self.assertAlmostEqual(res["espessura_mm"], 30.0, delta=0.05,
                msg=f"Falha no ponto X={pos_x}: esperado ~30mm, obtido {res['espessura_mm']}")

    # ── 6. Leitura equivalente a zero milímetros ────────────────────────────
    def test_06_leitura_equivalente_a_zero_milimetros(self):
        # Na posição 80, zero médio é 345.5. Se o sensor ler 345.5, a espessura deve ser 0.00 mm
        res_80 = CalibrationService.calcular_espessura(80.0, 345.5, calibracao=self.calib_inicial)
        self.assertTrue(res_80["valido"])
        self.assertAlmostEqual(res_80["espessura_mm"], 0.0, places=5)

        # Na posição intermediária 400, o zero é 343.6505... Se ler esse valor exato, deve ser 0.00 mm
        zero_400, _, _ = CalibrationService.interpolar_zero(400.0, calibracao=self.calib_inicial)
        res_400 = CalibrationService.calcular_espessura(400.0, zero_400, calibracao=self.calib_inicial)
        self.assertTrue(res_400["valido"])
        self.assertAlmostEqual(res_400["espessura_mm"], 0.0, places=5)

    # ── 7. Posição fora da faixa calibrada ──────────────────────────────────
    def test_07_posicao_fora_da_faixa_calibrada(self):
        # Faixa calibrada é [80.0, 543.0]
        # Sensor viaja de 50 a 600. Posições 50, 79 e 600 estão FORA da faixa
        res_abaixo = CalibrationService.calcular_espessura(50.0, 779.0, calibracao=self.calib_inicial)
        self.assertFalse(res_abaixo["valido"])
        self.assertEqual(res_abaixo["status"], "FORA_DA_FAIXA")
        self.assertIsNone(res_abaixo["espessura_mm"], "Não deve salvar espessura artificial de zero.")

        res_79 = CalibrationService.calcular_espessura(79.9, 779.0, calibracao=self.calib_inicial)
        self.assertFalse(res_79["valido"])
        self.assertEqual(res_79["status"], "FORA_DA_FAIXA")

        res_acima = CalibrationService.calcular_espessura(600.0, 771.0, calibracao=self.calib_inicial)
        self.assertFalse(res_acima["valido"])
        self.assertEqual(res_acima["status"], "FORA_DA_FAIXA")
        self.assertIsNone(res_acima["espessura_mm"])

    # ── 8. Ausência de calibração ativa ─────────────────────────────────────
    def test_08_ausencia_de_calibracao_ativa(self):
        # Desativa todas as calibrações
        CalibracaoEspessura.objects.update(ativa=False)
        CalibrationService.clear_cache()

        res = CalibrationService.calcular_espessura(80.0, 779.0)
        self.assertFalse(res["valido"])
        self.assertEqual(res["status"], "SEM_CALIBRACAO")
        self.assertIsNone(res["espessura_mm"], "Sem calibração ativa não deve salvar 0.0 artificial.")

        # Reativa calibração inicial
        self.calib_inicial.ativa = True
        self.calib_inicial.save()
        CalibrationService.clear_cache()

    # ── 9. Tentativa de ativar calibração inválida ───────────────────────────
    def test_09_tentativa_de_ativar_calibracao_invalida(self):
        # 1. Calibração sem pontos suficientes (apenas 1 ponto)
        calib_incompleta = CalibracaoEspessura.objects.create(
            descricao="Calibração Incompleta (1 ponto)",
            espessura_bloco_padrao_mm=30.0,
            ativa=False,
        )
        PontoCalibracao.objects.create(
            calibracao=calib_incompleta,
            posicao_x=100.0,
            leituras_zero_input="345",
            leituras_bloco_input="780",
        )
        valida, msg = calib_incompleta.is_valid_for_activation()
        self.assertFalse(valida)
        with self.assertRaises(ValidationError):
            calib_incompleta.ativar()

        # 2. Calibração com posições duplicadas
        calib_duplicada = CalibracaoEspessura.objects.create(
            descricao="Calibração Posições Duplicadas",
            espessura_bloco_padrao_mm=30.0,
            ativa=False,
        )
        p1 = PontoCalibracao(calibracao=calib_duplicada, posicao_x=100.0, leituras_zero_input="345", leituras_bloco_input="780")
        p1.save()
        # Ao tentar salvar outro ponto com mesma posicao_x na mesma calibração, unique_together bloqueia
        p2_dup = PontoCalibracao(calibracao=calib_duplicada, posicao_x=100.0, leituras_zero_input="350", leituras_bloco_input="785")
        with self.assertRaises(Exception):
            p2_dup.save()

        # 3. Leitura do bloco menor ou igual ao zero
        p_invalido = PontoCalibracao(
            calibracao=calib_duplicada,
            posicao_x=200.0,
            leituras_zero_input="500",
            leituras_bloco_input="400",  # Bloco menor que zero -> fisicamente impossível
        )
        with self.assertRaises(ValidationError):
            p_invalido.clean()

    # ── 10. Troca de calibração sem alterar dados históricos ─────────────────
    def test_10_troca_de_calibracao_preserva_historico(self):
        # 1. Salva uma leitura histórica no scan 1 com a calibração inicial (fator ~0.0692)
        scan_id_antigo = "SCAN_HISTORICO_001"
        res_antigo = CalibrationService.calcular_espessura(80.0, 779.0, calibracao=self.calib_inicial)
        leitura_hist = LeituraScada.objects.create(
            scan_id=scan_id_antigo,
            xid="DP_648291",
            eixo_x=80.0,
            eixo_y=res_antigo["espessura_mm"],
            data_leitura=timezone.now(),
            valor_bruto=779.0,
            posicao_bruta=80.0,
            zero_interpolado=res_antigo["zero_interpolado"],
            fator_conversao=res_antigo["fator_conversao"],
            calibracao=self.calib_inicial,
            status_validade="VALIDO",
        )
        fator_antigo_salvo = leitura_hist.fator_conversao
        espessura_antiga_salva = leitura_hist.eixo_y

        # 2. Cria e ativa uma segunda calibração com bloco de 60 mm (fator dobra)
        calib_nova = CalibracaoEspessura.objects.create(
            descricao="Calibração Nova Bloco 60mm",
            espessura_bloco_padrao_mm=60.0,
            ativa=False,
        )
        PontoCalibracao.objects.create(calibracao=calib_nova, posicao_x=80.0, leituras_zero_input="345", leituras_bloco_input="778.5")
        PontoCalibracao.objects.create(calibracao=calib_nova, posicao_x=500.0, leituras_zero_input="340", leituras_bloco_input="773.5")
        calib_nova = CalibrationService.ativar_calibracao(calib_nova.pk)

        # 3. Verifica se apenas a calib_nova está ativa
        self.assertTrue(calib_nova.ativa)
        self.assertFalse(CalibracaoEspessura.objects.get(pk=self.calib_inicial.pk).ativa)

        # 4. Verifica que a leitura do scan histórico NÃO FOI ALTERADA
        leitura_hist.refresh_from_db()
        self.assertEqual(leitura_hist.fator_conversao, fator_antigo_salvo)
        self.assertEqual(leitura_hist.eixo_y, espessura_antiga_salva)
        self.assertEqual(leitura_hist.calibracao_id, self.calib_inicial.pk)

        # Restaura calibração inicial
        CalibrationService.ativar_calibracao(self.calib_inicial.pk)

    # ── 11. Recebimento dos novos Data Points (DP_632601 e DP_648291) ────────
    def test_11_recebimento_novos_datapoints_calibrados(self):
        service = ScadaAcquisitionService(override_source="mysql")
        service.is_scanning = True
        service.current_scan_id = "SCAN_TESTE_DP"
        service.last_reading_id = 0
        service.last_reading_ts = 1700000000000
        service.trigger_start_ts = 1700000000000

        # Simula retorno incremental do MySQL contendo amostras de DP_632601 e DP_648291
        sample_readings = [
            # Ponto 1: Espessura bruta = 779.0 (ts=1000)
            {
                "xid": "DP_632601",
                "point_value_id": 1,
                "data_type": 3,
                "value": 779.0,
                "str_value": "779.0",
                "ts": 1700000001000,
                "datetime": timezone.now(),
            },
            # Ponto 2: Posição X = 80.0 (ts=1005) -> Deve parear com 779.0
            {
                "xid": "DP_648291",
                "point_value_id": 2,
                "data_type": 3,
                "value": 80.0,
                "str_value": "80.0",
                "ts": 1700000001005,
                "datetime": timezone.now(),
            },
            # Ponto 3: Posição X = 400.0 (ts=2000) e Espessura = 777.15 (ts=2002)
            {
                "xid": "DP_632601",
                "point_value_id": 3,
                "data_type": 3,
                "value": 777.15,
                "str_value": "777.15",
                "ts": 1700000002000,
                "datetime": timezone.now(),
            },
            {
                "xid": "DP_648291",
                "point_value_id": 4,
                "data_type": 3,
                "value": 400.0,
                "str_value": "400.0",
                "ts": 1700000002002,
                "datetime": timezone.now(),
            },
        ]

        with patch.object(service.repo, "get_incremental_readings_multi") as mock_multi:
            mock_multi.return_value = (sample_readings, 4, 1700000002002)
            saved = service._collect_measurements_mysql(self.config)

        self.assertEqual(saved, 2, "Devem ser salvas 2 leituras calibradas (para X=80 e X=400).")
        leituras = list(LeituraScada.objects.filter(scan_id="SCAN_TESTE_DP").order_by("eixo_x"))
        self.assertEqual(len(leituras), 2)

        # Validação do ponto X=80
        l80 = leituras[0]
        self.assertEqual(l80.eixo_x, 80.0)
        self.assertEqual(l80.valor_bruto, 779.0)
        self.assertEqual(l80.status_validade, "VALIDO")
        self.assertAlmostEqual(l80.eixo_y, 30.0, delta=0.05)

        # Validação do ponto X=400
        l400 = leituras[1]
        self.assertEqual(l400.eixo_x, 400.0)
        self.assertEqual(l400.valor_bruto, 777.15)
        self.assertEqual(l400.status_validade, "VALIDO")
        self.assertIsNotNone(l400.zero_interpolado)
        self.assertAlmostEqual(l400.zero_interpolado, 343.65, delta=0.05)

    # ── 12. Preservação do início e fim dos ciclos ───────────────────────────
    def test_12_preservacao_inicio_e_fim_dos_ciclos(self):
        service = ScadaAcquisitionService(override_source="rest_api")

        with patch.object(service, "read_trigger") as mock_trig, \
             patch.object(service, "_collect_calibrated_rest") as mock_col:

            mock_col.return_value = 1

            # 1. Gatilho inativo (0) -> IDLE
            mock_trig.return_value = False
            res1 = service.process_cycle()
            self.assertEqual(res1["status"], "IDLE")
            self.assertFalse(service.is_scanning)

            # 2. Borda de subida (0 -> 1) -> SCANNING e novo scan_id
            mock_trig.return_value = True
            res2 = service.process_cycle()
            self.assertEqual(res2["status"], "SCANNING")
            self.assertTrue(service.is_scanning)
            scan_ativo = service.current_scan_id
            self.assertIsNotNone(scan_ativo)
            self.assertTrue(scan_ativo.startswith("SCAN_"))

            # 3. Permanece ativo
            mock_trig.return_value = True
            res3 = service.process_cycle()
            self.assertEqual(res3["status"], "SCANNING")
            self.assertEqual(service.current_scan_id, scan_ativo)

            # 4. Borda de descida (1 -> 0) -> Finaliza scan e chama análise dimensional
            mock_trig.return_value = False
            res4 = service.process_cycle()
            self.assertEqual(res4["status"], "IDLE")
            self.assertFalse(service.is_scanning)
            self.assertEqual(service.last_completed_scan_id, scan_ativo)

    # ── 13. Ausência de duplicidade de gravações ─────────────────────────────
    def test_13_ausencia_duplicidade_gravacoes_e_trava_processo(self):
        # 1. Teste de trava exclusiva entre processos
        lock1 = CrossProcessLock()
        self.assertTrue(lock1.acquire(), "Primeira trava deve ser adquirida.")
        try:
            lock2 = CrossProcessLock()
            self.assertFalse(lock2.acquire(), "Segunda trava simultânea deve ser bloqueada.")
        finally:
            lock1.release()

        # 2. Teste de deduplicação por chave (ts, x, y)
        service = ScadaAcquisitionService(override_source="mysql")
        service.is_scanning = True
        service.current_scan_id = "SCAN_DEDUP"
        service.trigger_start_ts = 1700000000000

        readings_com_duplicata = [
            {"xid": "DP_632601", "point_value_id": 1, "data_type": 3, "value": 779.0, "str_value": "779.0", "ts": 1700000001000, "datetime": timezone.now()},
            {"xid": "DP_648291", "point_value_id": 2, "data_type": 3, "value": 80.0, "str_value": "80.0", "ts": 1700000001000, "datetime": timezone.now()},
            # Duplicata idêntica gerada por poll concorrente ou reenvio
            {"xid": "DP_648291", "point_value_id": 3, "data_type": 3, "value": 80.0, "str_value": "80.0", "ts": 1700000001000, "datetime": timezone.now()},
        ]

        with patch.object(service.repo, "get_incremental_readings_multi") as mock_multi:
            mock_multi.return_value = (readings_com_duplicata, 3, 1700000001000)
            saved = service._collect_measurements_mysql(self.config)

        self.assertEqual(saved, 1, "A duplicata deve ser descartada, gravando apenas 1 leitura.")
        self.assertEqual(LeituraScada.objects.filter(scan_id="SCAN_DEDUP").count(), 1)

    # ── 14. Erros de comunicação e dados desatualizados ──────────────────────
    def test_14_erros_comunicacao_e_amostras_desatualizadas(self):
        service = ScadaAcquisitionService(override_source="mysql")

        # 1. Falha de comunicação (gatilho retorna None)
        with patch.object(service, "read_trigger", return_value=None):
            res = service.process_cycle()
            self.assertEqual(res["status"], "NO_COMMUNICATION")
            self.assertEqual(service._consecutive_comm_errors, 1)

        # 2. Amostra desatualizada (stale/incoerente temporalmente)
        service.is_scanning = True
        service.current_scan_id = "SCAN_STALE"
        service.trigger_start_ts = 1700000000000

        # Leitura de espessura de 10 minutos atrás (600.000 ms antes)
        leitura_incoerente = [
            {"xid": "DP_632601", "point_value_id": 1, "data_type": 3, "value": 779.0, "str_value": "779.0", "ts": 1700000000000 - 600000, "datetime": timezone.now()},
            {"xid": "DP_648291", "point_value_id": 2, "data_type": 3, "value": 100.0, "str_value": "100.0", "ts": 1700000001000, "datetime": timezone.now()},
        ]

        with patch.object(service.repo, "get_incremental_readings_multi") as mock_multi:
            mock_multi.return_value = (leitura_incoerente, 2, 1700000001000)
            service._collect_measurements_mysql(self.config)

        rec = LeituraScada.objects.filter(scan_id="SCAN_STALE").first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status_validade, "AMOSTRA_INCOERENTE")
        self.assertIsNone(rec.eixo_y, "Leitura incoerente não pode ter espessura calculada nem zero artificial.")

    # ── 15. Preservação das APIs, dashboard e CSV ────────────────────────────
    def test_15_preservacao_apis_dashboard_e_csv(self):
        client = Client()

        # Cria ciclo completo de teste
        scan_id = "SCAN_API_TEST_01"
        for i, pos_x in enumerate([80.0, 170.0, 260.0, 357.0, 450.0, 543.0]):
            res_calc = CalibrationService.calcular_espessura(pos_x, 779.0, calibracao=self.calib_inicial)
            LeituraScada.objects.create(
                scan_id=scan_id,
                xid="DP_648291",
                eixo_x=pos_x,
                eixo_y=res_calc["espessura_mm"],
                data_leitura=timezone.now(),
                valor_bruto=779.0,
                posicao_bruta=pos_x,
                zero_interpolado=res_calc["zero_interpolado"],
                fator_conversao=res_calc["fator_conversao"],
                calibracao=self.calib_inicial,
                status_validade="VALIDO",
            )

        # 1. Teste Dashboard View
        resp_dash = client.get("/")
        self.assertEqual(resp_dash.status_code, 200)
        self.assertIn("calibracao_ativa", resp_dash.context)

        # 2. Teste API de Dados
        resp_api = client.get(f"/api/dados/?scan_id={scan_id}")
        self.assertEqual(resp_api.status_code, 200)
        data_json = resp_api.json()
        self.assertEqual(data_json["scan_id"], scan_id)
        self.assertEqual(data_json["count"], 6)
        self.assertIn("calibracao", data_json)
        self.assertEqual(data_json["calibracao"]["id"], self.calib_inicial.pk)
        self.assertEqual(data_json["calibracao"]["faixa_x"], [80.0, 543.0])

        # 3. Teste Listar Scans
        resp_scans = client.get("/api/scans/")
        self.assertEqual(resp_scans.status_code, 200)
        self.assertIn(scan_id, resp_scans.json()["scans"])

        # 4. Teste Exportar CSV
        resp_csv = client.get(f"/exportar/csv/?scan_id={scan_id}")
        self.assertEqual(resp_csv.status_code, 200)
        self.assertEqual(resp_csv["Content-Type"], "text/csv")
        csv_content = resp_csv.content.decode("utf-8")
        self.assertIn("Scan ID;X (mm);Y (mm);Status;Zona;Valor Bruto;Zero Interp;Fator Conv;Validade", csv_content)
        self.assertIn(scan_id, csv_content)
        self.assertIn("80,0", csv_content)
        self.assertIn("VALIDO", csv_content)
