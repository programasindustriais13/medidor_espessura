import csv
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from .models import LeituraScada, SystemConfiguration
from django.db.models import Max
from .profile_logic import ProfileLogic
from .dimensional_analysis import DimensionalAnalyzer
from .scada_service import get_collector_status

def dashboard(request):
    """
    Renderiza a interface principal do medidor.
    """
    config = SystemConfiguration.get_config()
    return render(request, 'core/dashboard.html', {'config': config})


def listar_scans(request):
    """
    Retorna a lista de Scan IDs disponíveis (para o dropdown de histórico).
    Ordenados do mais recente para o mais antigo.
    """
    scans = LeituraScada.objects.values_list('scan_id', flat=True).distinct().order_by('-scan_id')
    # Filtra None se houver
    scans = [s for s in scans if s]
    return JsonResponse({'scans': scans})


def api_dados_scada(request):
    """
    Retorna os dados enriquecidos com a lógica de perfil e cotas dimensionais calculadas.
    Parâmetro opcional GET 'scan_id'.
    Fornece metadados operacionais de status do coletor em tempo real.
    """
    req_scan_id = request.GET.get('scan_id')

    # Parâmetros de Lógica da UI
    p_esp_borda = request.GET.get('esp_borda')
    p_centro = request.GET.get('centro')
    p_tol = request.GET.get('tol')

    # Configuração do sistema
    config = SystemConfiguration.get_config()

    # Status do coletor em tempo de execução
    collector_info = get_collector_status()
    collector_status = collector_info.get("status", "OFFLINE")
    live_scan_id = collector_info.get("scan_id")
    is_scanning = collector_info.get("is_scanning", False)

    if req_scan_id:
        scan_id = req_scan_id
        is_live_cycle = (scan_id == live_scan_id and is_scanning)
    else:
        # Modo Ao Vivo: prioriza ciclo ativo se estiver medindo, senão último ciclo concluído
        if is_scanning and live_scan_id:
            scan_id = live_scan_id
            is_live_cycle = True
        else:
            scan_id = LeituraScada.objects.all().aggregate(Max('scan_id'))['scan_id__max']
            is_live_cycle = False

    if not scan_id:
        return JsonResponse({
            'scan_id': None,
            'collector_status': collector_status,
            'is_live_cycle': False,
            'count': 0,
            'data': [],
            'material': {
                'largura': None,
                'x_inicial': None,
                'x_final': None,
                'pontos_validos': 0,
                'corte_min': 0.9
            },
            'dimensoes': {
                'status': 'DADOS_INSUFICIENTES',
                'motivo': 'Nenhum ciclo encontrado.',
                'largura_total': None,
                'largura_coroa': None,
                'espessura_media_coroa': None,
                'espessura_min_coroa': None,
                'espessura_max_coroa': None,
                'pontos_coroa': 0,
                'media_aritmetica_coroa': None,
                'espessura_lateral_esq': None,
                'espessura_lateral_dir': None,
                'espessura_lateral_media': None,
                'x_esquerda': None,
                'x_direita': None,
                'x_inicio_coroa': None,
                'x_fim_coroa': None,
            }
        })

    # Buscar dados ordenados por Data Leitura (para manter ordem cronológica de varredura)
    leituras = LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura')
    
    data = []
    for l in leituras:
        validacao = ProfileLogic.validar_ponto(
            l.eixo_x, l.eixo_y, 
            esp_borda=p_esp_borda, centro=p_centro, tol=p_tol
        )
        
        data.append({
            'x': l.eixo_x,
            'y': l.eixo_y,
            'target': validacao['alvo'],
            'min': validacao['lim_min'],
            'max': validacao['lim_max'],
            'is_ok': validacao['is_ok'],
            'zona': validacao['zona'],
            'ts': l.data_leitura.strftime('%H:%M:%S')
        })
    
    # Cálculo do tamanho total do material (compatibilidade com corte > 0.9mm)
    info_material = ProfileLogic.calcular_tamanho_material(data, corte_min=0.9)

    # Análise Dimensional Automática da Banda de Rodagem
    dimensoes = DimensionalAnalyzer.obter_ou_calcular_dimensoes(
        scan_id=scan_id,
        config=config,
        is_live_cycle=is_live_cycle
    )

    return JsonResponse({
        'scan_id': scan_id,
        'collector_status': collector_status,
        'is_live_cycle': is_live_cycle,
        'count': len(data),
        'data': data,
        'material': info_material,
        'dimensoes': dimensoes
    })


def exportar_csv(request):
    """
    Gera relatório CSV ajustado conforme regras industriais.
    Inclui cabeçalho dimensional se gerado a partir de um scan_id individual.
    """
    scan_id = request.GET.get('scan_id')
    config = SystemConfiguration.get_config()
    
    # Parâmetros de Lógica da UI
    p_esp_borda = request.GET.get('esp_borda')
    p_centro = request.GET.get('centro')
    p_tol = request.GET.get('tol')

    if scan_id:
        leituras = LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura')
        filename = f"relatorio_{scan_id}.csv"
    else:
        leituras = LeituraScada.objects.all().order_by('-scan_id', 'data_leitura')
        filename = f"relatorio_geral_{timezone.now().strftime('%Y%m%d_%H%M')}.csv"

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response, delimiter=';')

    if scan_id:
        dim = DimensionalAnalyzer.obter_ou_calcular_dimensoes(scan_id, config=config)
        if dim and dim.get("status") == "CONCLUIDO":
            writer.writerow(["# RELATORIO DIMENSIONAL DA BANDA DE RODAGEM"])
            writer.writerow([f"# Ciclo Scan ID: {scan_id}"])
            writer.writerow([f"# Largura Total: {str(dim.get('largura_total')).replace('.', ',')} mm"])
            writer.writerow([f"# Largura Coroa: {str(dim.get('largura_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# ESPESSURA MEDIA DA COROA: {str(dim.get('espessura_media_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# Espessura Min Coroa: {str(dim.get('espessura_min_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# Espessura Max Coroa: {str(dim.get('espessura_max_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# Pontos Validos Coroa: {dim.get('pontos_coroa', 0)}"])
            writer.writerow([f"# Espessura Lateral Esquerda: {str(dim.get('espessura_lateral_esq')).replace('.', ',')} mm"])
            writer.writerow([f"# Espessura Lateral Direita: {str(dim.get('espessura_lateral_dir')).replace('.', ',')} mm"])
            writer.writerow([f"# Situacao: {dim.get('status')}"])
            writer.writerow([])

    writer.writerow([
        'Scan ID', 
        'X (mm)', 
        'Y (mm)', 
        'Status', 
        'Zona'
    ])

    for l in leituras:
        res = ProfileLogic.validar_ponto(
            l.eixo_x, l.eixo_y,
            esp_borda=p_esp_borda, centro=p_centro, tol=p_tol
        )
        status_str = "OK" if res['is_ok'] else "NOK"
        
        writer.writerow([
            l.scan_id,
            str(l.eixo_x).replace('.', ','),
            str(l.eixo_y).replace('.', ','),
            status_str,
            res['zona']
        ])

    return response
