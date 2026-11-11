"""Relatório Excel de um único ciclo concluído."""

from io import BytesIO

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.chart import ScatterChart, Reference, Series
from openpyxl.styles import Font, PatternFill

from .dimensional_analysis import DimensionalAnalyzer
from .models import LeituraScada, ResultadoDimensionalScan, SystemConfiguration


def _estilizar_cabecalho(sheet, row=1):
    for cell in sheet[row]:
        cell.fill = PatternFill('solid', fgColor='17324D')
        cell.font = Font(color='FFFFFF', bold=True)


def criar_relatorio_excel(scan_id):
    leituras = list(LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura', 'pk'))
    dimensoes = DimensionalAnalyzer.obter_ou_calcular_dimensoes(
        scan_id, config=SystemConfiguration.get_config()
    )
    nome = ResultadoDimensionalScan.objects.filter(scan_id=scan_id).values_list(
        'nome_material', flat=True
    ).first() or ''

    wb = Workbook()
    resumo = wb.active
    resumo.title = 'Resumo'
    resumo.append(['Campo', 'Valor'])
    _estilizar_cabecalho(resumo)
    campos = [
        ('Material', nome),
        ('Ciclo', scan_id),
        ('Início', timezone.localtime(leituras[0].data_leitura).replace(tzinfo=None)),
        ('Fim', timezone.localtime(leituras[-1].data_leitura).replace(tzinfo=None)),
        ('Leituras', len(leituras)),
        ('Largura total (mm)', dimensoes.get('largura_total')),
        ('Largura da coroa (mm)', dimensoes.get('largura_coroa')),
        ('Espessura média da coroa (mm)', dimensoes.get('espessura_media_coroa')),
        ('Espessura mínima da coroa (mm)', dimensoes.get('espessura_min_coroa')),
        ('Espessura máxima da coroa (mm)', dimensoes.get('espessura_max_coroa')),
        ('Espessura lateral esquerda (mm)', dimensoes.get('espessura_lateral_esq')),
        ('Espessura lateral direita (mm)', dimensoes.get('espessura_lateral_dir')),
        ('Espessura média das laterais (mm)', dimensoes.get('espessura_lateral_media')),
    ]
    for label, value in campos:
        resumo.append([label, value])
        cell = resumo.cell(resumo.max_row, 2)
        if label == 'Material':
            cell.data_type = 's'  # nomes iniciados por '=' não viram fórmulas
        elif label in ('Início', 'Fim'):
            cell.number_format = 'dd/mm/yyyy hh:mm:ss'
        elif isinstance(value, float):
            cell.number_format = '0.00'
    resumo.column_dimensions['A'].width = 38
    resumo.column_dimensions['B'].width = 30
    resumo.freeze_panes = 'A2'

    dados = wb.create_sheet('Leituras')
    dados.append(['Nº', 'Data/Hora', 'X (mm)', 'Espessura (mm)'])
    _estilizar_cabecalho(dados)
    for number, leitura in enumerate(leituras, 1):
        dados.append([
            number,
            timezone.localtime(leitura.data_leitura).replace(tzinfo=None),
            leitura.eixo_x,
            leitura.eixo_y,
        ])
        row = dados.max_row
        dados.cell(row, 2).number_format = 'dd/mm/yyyy hh:mm:ss'
        for col in (3, 4):
            dados.cell(row, col).number_format = '0.00'
    dados.freeze_panes = 'A2'
    dados.auto_filter.ref = f'A1:D{dados.max_row}'
    for col, width in {'A': 8, 'B': 23, 'C': 16, 'D': 21}.items():
        dados.column_dimensions[col].width = width

    perfil = wb.create_sheet('Perfil')
    perfil.append(['X (mm)', 'Espessura (mm)'])
    _estilizar_cabecalho(perfil)
    for leitura in sorted(leituras, key=lambda l: (l.eixo_x, l.data_leitura)):
        if leitura.eixo_y is not None:
            perfil.append([leitura.eixo_x, leitura.eixo_y])
            perfil.cell(perfil.max_row, 1).number_format = '0.00'
            perfil.cell(perfil.max_row, 2).number_format = '0.00'
    perfil.column_dimensions['A'].width = 16
    perfil.column_dimensions['B'].width = 22
    perfil.freeze_panes = 'A2'
    if perfil.max_row > 1:
        chart = ScatterChart()
        chart.title = 'Perfil de espessura'
        chart.x_axis.title = 'Posição X (mm)'
        chart.y_axis.title = 'Espessura (mm)'
        chart.width = 20
        chart.height = 10
        chart.series.append(Series(
            Reference(perfil, min_col=2, min_row=2, max_row=perfil.max_row),
            Reference(perfil, min_col=1, min_row=2, max_row=perfil.max_row),
            title='Espessura',
        ))
        perfil.add_chart(chart, 'D2')

    buffer = BytesIO()
    wb.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="relatorio_{scan_id}.xlsx"'
    return response
