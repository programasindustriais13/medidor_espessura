"""Regressões para patamares e leituras isoladas na análise pós-varredura."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .dimensional_analysis import DimensionalAnalyzer
from .models import LeituraScada, ResultadoDimensionalScan, SystemConfiguration


# Ensaio informado pelo usuário: blocos de 3, 8 e 1 mm, nesta ordem.
SCAN_PADROES = """
67 3.1
68 3.1
70 3.0
68 3.0
74 2.9
73 2.9
77 3.1
74 3.1
78 3.0
79 3.1
78 3.1
80 3.1
81 2.9
80 2.9
88 3.1
89 3.1
92 3.3
91 3.3
92 3.5
93 3.6
92 3.6
93 3.9
94 4.7
93 4.7
94 5.3
95 6.4
94 6.4
95 7.7
98 7.9
95 7.9
99 8.0
100 8.0
101 7.9
100 7.9
102 7.9
102 8.0
102 7.9
103 8.0
105 8.0
112 7.9
110 7.9
114 7.9
116 8.0
117 7.9
116 7.9
119 8.0
117 8.0
119 7.9
120 7.9
126 8.0
124 8.0
126 8.3
126 8.0
127 6.1
127 4.8
128 3.5
127 3.5
128 2.4
130 1.6
128 1.6
130 1.3
131 1.1
134 1.1
135 1.0
137 1.0
137 0.9
137 1.0
142 1.0
148 1.1
149 1.0
148 1.0
150 1.1
149 1.1
151 1.0
150 1.0
151 1.1
152 1.1
154 1.0
152 1.0
155 1.0
155 1.1
156 1.0
155 1.0
157 1.1
156 1.1
158 0.8
157 0.8
156 1.1
158 1.1
"""


class RobustProfileTestCase(TestCase):
    def setUp(self):
        self.config = SystemConfiguration.get_config()

    @staticmethod
    def pontos_padrao():
        return [tuple(map(float, linha.split())) for linha in SCAN_PADROES.strip().splitlines()]

    def test_mediana_por_x_independe_da_ordem_das_leituras(self):
        pts = [(95.0, 6.4), (95.0, 7.7), (95.0, 7.9)]
        self.assertEqual(DimensionalAnalyzer.consolidar_posicoes(pts), [(95.0, 7.7)])
        self.assertEqual(DimensionalAnalyzer.consolidar_posicoes(list(reversed(pts))), [(95.0, 7.7)])

    def test_pico_unico_e_sinalizado_e_degrau_real_e_preservado(self):
        pts = list(enumerate([3.0, 3.1, 3.0, 3.4, 3.0, 3.1, 3.0]))
        aceitos, suspeitos = DimensionalAnalyzer.separar_picos_isolados(pts)
        self.assertEqual(suspeitos, [(3, 3.4)])
        self.assertEqual(len(aceitos), 6)

        degrau = list(enumerate([3.0, 3.0, 3.0, 8.0, 8.0, 8.0, 8.0]))
        aceitos, suspeitos = DimensionalAnalyzer.separar_picos_isolados(degrau)
        self.assertEqual(aceitos, degrau)
        self.assertEqual(suspeitos, [])

        trecho_curto = list(enumerate([3.0, 3.0, 3.0, 3.4, 3.4, 3.0, 3.0, 3.0]))
        aceitos, suspeitos = DimensionalAnalyzer.separar_picos_isolados(trecho_curto)
        self.assertEqual(aceitos, trecho_curto)
        self.assertEqual(suspeitos, [])

    def test_ensaio_3_8_1_reconhece_patamares_sem_alterar_pontos(self):
        pontos = [{"x": x, "y": y} for x, y in self.pontos_padrao()]
        original = [dict(p) for p in pontos]
        resultado = DimensionalAnalyzer.analisar_perfil(pontos, config=self.config)

        self.assertEqual(resultado["status"], "CONCLUIDO")
        self.assertAlmostEqual(resultado["espessura_lateral_esq"], 3.0, delta=0.15)
        self.assertAlmostEqual(resultado["espessura_media_coroa"], 8.0, delta=0.15)
        self.assertAlmostEqual(resultado["espessura_lateral_dir"], 1.0, delta=0.15)
        self.assertGreaterEqual(resultado["x_inicio_coroa"], 98.0)
        self.assertAlmostEqual(resultado["mediana_lateral_dir"], 1.0, delta=0.1)
        self.assertEqual(resultado["pontos_suspeitos"], 1)
        self.assertEqual(resultado["posicoes_suspeitas"], [126.0])
        self.assertEqual(pontos, original)

    def test_resultado_robusto_persiste_e_csv_preserva_leituras_brutas(self):
        scan_id = "SCAN_ENSAIO_3_8_1"
        instante = timezone.now()
        pontos = self.pontos_padrao()
        LeituraScada.objects.bulk_create([
            LeituraScada(scan_id=scan_id, xid="DP_578391", eixo_x=x,
                         eixo_y=y, data_leitura=instante)
            for x, y in pontos
        ])

        resultado = DimensionalAnalyzer.obter_ou_calcular_dimensoes(scan_id, config=self.config)
        self.assertEqual(resultado["status"], "CONCLUIDO")
        self.assertEqual(LeituraScada.objects.filter(scan_id=scan_id).count(), len(pontos))
        self.assertTrue(LeituraScada.objects.filter(scan_id=scan_id, eixo_y=0.8).exists())
        salvo = ResultadoDimensionalScan.objects.get(scan_id=scan_id)
        self.assertIn("analise_robusta", salvo.parametros_calculo)
        self.assertAlmostEqual(resultado["mediana_lateral_dir"], 1.0, delta=0.1)

        salvo.espessura_media_coroa = 7.92
        salvo.save(update_fields=["espessura_media_coroa"])
        call_command("recalcular_dimensoes", scan_id, stdout=StringIO())
        salvo.refresh_from_db()
        self.assertAlmostEqual(salvo.espessura_media_coroa, 7.95, delta=0.01)
        self.assertEqual(LeituraScada.objects.filter(scan_id=scan_id).count(), len(pontos))

        resposta = self.client.get(f"/api/dados/?scan_id={scan_id}").json()
        self.assertEqual(resposta["count"], len(pontos))
        self.assertAlmostEqual(resposta["dimensoes"]["mediana_lateral_dir"], 1.0, delta=0.1)
        csv = self.client.get(f"/exportar/csv/?scan_id={scan_id}").content.decode("utf-8")
        self.assertIn("0,8", csv)
        self.assertIn("# MEDIANA DA COROA", csv)
        self.assertIn("# X Suspeitos (mm): 126,0", csv)

    def test_limites_manuais_nao_sao_encolhidos_pela_analise_do_patamar(self):
        self.config.modo_deteccao_coroa = "manual"
        self.config.manual_x_inicio_coroa = 95.0
        self.config.manual_x_fim_coroa = 127.0
        pontos = [{"x": x, "y": y} for x, y in self.pontos_padrao()]
        resultado = DimensionalAnalyzer.analisar_perfil(pontos, config=self.config)
        self.assertEqual(resultado["status"], "CONCLUIDO")
        self.assertEqual((resultado["x_inicio_coroa"], resultado["x_fim_coroa"]), (95.0, 127.0))

    def test_resumo_historico_persistido_nao_e_recalculado_automaticamente(self):
        ResultadoDimensionalScan.objects.create(
            scan_id="SCAN_HISTORICO", status_leitura="CONCLUIDO",
            espessura_media_coroa=7.92, parametros_calculo={},
        )
        resultado = DimensionalAnalyzer.obter_ou_calcular_dimensoes("SCAN_HISTORICO")
        self.assertEqual(resultado["espessura_media_coroa"], 7.92)
        self.assertNotIn("mediana_coroa", resultado)
