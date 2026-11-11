"""Contrato de identificação e exportação de ciclos."""

import csv
import io
import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from openpyxl import load_workbook

from .dimensional_analysis import DimensionalAnalyzer
from .models import LeituraScada, ResultadoDimensionalScan, SystemConfiguration
from .scan_material import vincular_material_preparado


class MaterialEExportacaoTest(TestCase):
    def setUp(self):
        self.old = 'SCAN_20261111_115733'
        self.current = 'SCAN_20261111_120000'
        base = timezone.make_aware(datetime(2026, 11, 11, 11, 57, 33))
        for sid, offset, thickness in [
            (self.old, 0, 3.0), (self.old, 1, 3.1),
            (self.current, 147, 8.0), (self.current, 148, 7.9),
        ]:
            LeituraScada.objects.create(
                scan_id=sid, xid='DP_578391', eixo_x=float(offset),
                eixo_y=thickness, data_leitura=base + timedelta(seconds=offset),
            )
        self.idle = {'is_scanning': False, 'scan_id': self.current}
        self.active = {'is_scanning': True, 'scan_id': self.current}

    def test_nome_preparado_vincula_ao_proximo_ciclo_e_persiste_no_recalculo(self):
        with patch('core.views.get_collector_status', return_value=self.idle):
            response = self.client.post('/api/material/', data=json.dumps({
                'nome_material': 'Banda padrão 3 mm',
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['pendente'])
        config = SystemConfiguration.get_config()
        self.assertEqual(config.nome_material_proximo_ciclo, 'Banda padrão 3 mm')
        vincular_material_preparado('SCAN_20261111_130000')
        config.refresh_from_db()
        self.assertEqual(config.nome_material_proximo_ciclo, '')
        self.assertEqual(ResultadoDimensionalScan.objects.get(scan_id='SCAN_20261111_130000').nome_material, 'Banda padrão 3 mm')
        DimensionalAnalyzer.calcular_e_persistir_scan('SCAN_20261111_130000')
        self.assertEqual(ResultadoDimensionalScan.objects.get(scan_id='SCAN_20261111_130000').nome_material, 'Banda padrão 3 mm')

    def test_nome_do_ciclo_ativo_e_busca_por_nome_data_e_id(self):
        with patch('core.views.get_collector_status', return_value=self.active):
            response = self.client.post('/api/material/', data=json.dumps({
                'nome_material': 'Banda Azul',
            }), content_type='application/json')
        self.assertEqual(response.json()['scan_id'], self.current)
        for query in ('Banda Azul', '11/11/2026', '2026-11-11', '11/11/2026 12:00', '12:00:00', '120000'):
            response = self.client.get('/api/scans/', {'q': query})
            self.assertEqual(response.status_code, 200)
            self.assertIn(self.current, response.json()['scans'])
        self.assertEqual(self.client.get('/api/scans/', {'q': 'Banda Azul'}).json()['items'][0]['nome_material'], 'Banda Azul')
        self.assertEqual(self.client.get('/api/scans/', {'q': 'Não existe'}).json()['scans'], [])

    def test_nome_em_historico_sem_resumo_nao_impede_calculo(self):
        with patch('core.views.get_collector_status', return_value=self.idle):
            response = self.client.post('/api/material/', data=json.dumps({
                'scan_id': self.old, 'nome_material': 'Bloco 3 mm',
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ResultadoDimensionalScan.objects.get(scan_id=self.old).status_leitura, 'EM_ANDAMENTO')
        DimensionalAnalyzer.obter_ou_calcular_dimensoes(self.old)
        resultado = ResultadoDimensionalScan.objects.get(scan_id=self.old)
        self.assertEqual(resultado.nome_material, 'Bloco 3 mm')
        self.assertNotEqual(resultado.status_leitura, 'EM_ANDAMENTO')

    def test_excel_ao_vivo_exporta_ultimo_ciclo_concluido(self):
        ResultadoDimensionalScan.objects.create(scan_id=self.old, nome_material='Bloco 3 mm')
        with patch('core.views.get_collector_status', return_value=self.active):
            response = self.client.get('/exportar/excel/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.old, response['Content-Disposition'])
        wb = load_workbook(io.BytesIO(response.content))
        self.assertEqual(wb.sheetnames, ['Resumo', 'Leituras', 'Perfil'])
        self.assertEqual(wb['Resumo']['B2'].value, 'Bloco 3 mm')
        self.assertEqual(wb['Resumo']['B3'].value, self.old)
        self.assertEqual(wb['Leituras']['D2'].value, 3.0)
        self.assertEqual(wb['Leituras']['D3'].value, 3.1)
        self.assertEqual(wb['Leituras']['D2'].data_type, 'n')
        self.assertEqual(wb['Leituras'].max_row, 3)
        self.assertEqual([cell.value for cell in wb['Leituras'][1]], ['Nº', 'Data/Hora', 'X (mm)', 'Espessura (mm)'])

    def test_historico_selecionado_e_csv_por_ciclo(self):
        with patch('core.views.get_collector_status', return_value=self.idle):
            excel = self.client.get('/exportar/excel/', {'scan_id': self.old})
            csv_response = self.client.get('/exportar/csv/')
        self.assertEqual(excel.status_code, 200)
        self.assertIn(self.old, excel['Content-Disposition'])
        self.assertIn(self.current, csv_response['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(csv_response.content.decode()), delimiter=';'))
        header_index = next(i for i, row in enumerate(rows) if row and row[0] == 'Scan ID')
        self.assertEqual(rows[header_index], ['Scan ID', 'Data/Hora', 'X (mm)', 'Y (mm)'])
        self.assertEqual({row[0] for row in rows[header_index + 1:]}, {self.current})

    def test_ciclo_ativo_e_id_inexistente_nao_exportam(self):
        with patch('core.views.get_collector_status', return_value=self.active):
            self.assertEqual(self.client.get('/exportar/excel/', {'scan_id': self.current}).status_code, 404)
            self.assertEqual(self.client.get('/exportar/csv/', {'scan_id': 'SCAN_INEXISTENTE'}).status_code, 404)

    def test_nome_invalido_e_injecao_de_formula(self):
        response = self.client.post('/api/material/', data=json.dumps({'nome_material': 'X' * 151}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        ResultadoDimensionalScan.objects.create(scan_id=self.old, nome_material='=1+1')
        with patch('core.views.get_collector_status', return_value=self.idle):
            wb = load_workbook(io.BytesIO(self.client.get('/exportar/excel/', {'scan_id': self.old}).content))
        self.assertEqual(wb['Resumo']['B2'].data_type, 's')
        self.assertEqual(wb['Resumo']['B2'].value, '=1+1')
