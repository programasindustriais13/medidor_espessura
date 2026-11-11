import csv
import json
import re
from datetime import datetime, timedelta
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from .models import LeituraScada, SystemConfiguration, CalibracaoEspessura, ResultadoDimensionalScan
from django.db.models import Max, Q
from .profile_logic import ProfileLogic
from .dimensional_analysis import DimensionalAnalyzer
from .scada_service import get_collector_status


def _busca_scan(query):
    """Aceita data/hora no formato exibido ao operador ou no formato do ID."""
    match = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?', query)
    if match:
        day, month, year, hour, minute, second = match.groups()
        return f'{year}{month}{day}' + (f'_{hour}{minute}{second or ""}' if hour else '')
    match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?', query)
    if match:
        year, month, day, hour, minute, second = match.groups()
        return f'{year}{month}{day}' + (f'_{hour}{minute}{second or ""}' if hour else '')
    match = re.fullmatch(r'(\d{2}):(\d{2})(?::(\d{2}))?', query)
    if match:
        return ''.join(part for part in match.groups() if part)
    return query


def _filtro_data_leitura(query):
    """Permite buscar também pela data/hora real das leituras do ciclo."""
    formats = (
        ('%d/%m/%Y', timedelta(days=1)),
        ('%Y-%m-%d', timedelta(days=1)),
        ('%d/%m/%Y %H:%M', timedelta(minutes=1)),
        ('%Y-%m-%d %H:%M', timedelta(minutes=1)),
        ('%Y-%m-%dT%H:%M', timedelta(minutes=1)),
        ('%d/%m/%Y %H:%M:%S', timedelta(seconds=1)),
        ('%Y-%m-%d %H:%M:%S', timedelta(seconds=1)),
        ('%Y-%m-%dT%H:%M:%S', timedelta(seconds=1)),
    )
    for format_string, duration in formats:
        try:
            start = timezone.make_aware(datetime.strptime(query, format_string))
        except ValueError:
            continue
        return Q(data_leitura__gte=start, data_leitura__lt=start + duration)
    time_match = re.fullmatch(r'(\d{2}):(\d{2})(?::(\d{2}))?', query)
    if time_match:
        hour, minute, second = time_match.groups()
        if int(hour) < 24 and int(minute) < 60 and (second is None or int(second) < 60):
            result = Q(data_leitura__hour=int(hour), data_leitura__minute=int(minute))
            if second is not None:
                result &= Q(data_leitura__second=int(second))
            return result
    return None

@ensure_csrf_cookie
def dashboard(request):
    """
    Renderiza a interface principal do medidor.
    """
    config = SystemConfiguration.get_config()
    return render(request, 'core/dashboard.html', {
        'config': config,
    })


def listar_scans(request):
    """
    Retorna a lista de Scan IDs disponíveis (para o dropdown de histórico).
    Ordenados do mais recente para o mais antigo.
    """
    query = request.GET.get('q', '').strip()[:150]
    scans = LeituraScada.objects.exclude(scan_id__isnull=True).exclude(scan_id='')
    if query:
        scan_query = _busca_scan(query)
        matching_names = ResultadoDimensionalScan.objects.filter(
            nome_material__icontains=query
        ).values('scan_id')
        search_filter = Q(scan_id__icontains=scan_query) | Q(scan_id__in=matching_names)
        date_filter = _filtro_data_leitura(query)
        if date_filter is not None:
            search_filter |= date_filter
        scans = scans.filter(search_filter)
    total = scans.values('scan_id').distinct().count()
    rows = list(scans.values('scan_id').annotate(data_hora=Max('data_leitura')).order_by('-scan_id')[:200])
    names = dict(ResultadoDimensionalScan.objects.filter(
        scan_id__in=[row['scan_id'] for row in rows]
    ).values_list('scan_id', 'nome_material'))
    items = [{
        'scan_id': row['scan_id'],
        'nome_material': names.get(row['scan_id'], ''),
        'data_hora': timezone.localtime(row['data_hora']).isoformat() if row['data_hora'] else None,
    } for row in rows]
    return JsonResponse({'scans': [row['scan_id'] for row in rows], 'items': items, 'total': total})


@require_http_methods(['POST'])
def salvar_nome_material(request):
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'erro': 'JSON inválido.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'erro': 'JSON inválido.'}, status=400)
    nome = payload.get('nome_material', '')
    if not isinstance(nome, str) or len(nome.strip()) > 150:
        return JsonResponse({'erro': 'O nome deve ter até 150 caracteres.'}, status=400)
    nome = nome.strip()
    scan_id = payload.get('scan_id')
    if scan_id is not None and not isinstance(scan_id, str):
        return JsonResponse({'erro': 'Ciclo inválido.'}, status=400)
    if not scan_id:
        status = get_collector_status()
        if status.get('is_scanning') and status.get('scan_id'):
            scan_id = status['scan_id']
    if scan_id:
        status = get_collector_status()
        is_active = status.get('is_scanning') and scan_id == status.get('scan_id')
        if not is_active and not LeituraScada.objects.filter(scan_id=scan_id).exists():
            return JsonResponse({'erro': 'Ciclo não encontrado.'}, status=404)
        result, _ = ResultadoDimensionalScan.objects.get_or_create(
            scan_id=scan_id, defaults={'status_leitura': 'EM_ANDAMENTO'}
        )
        ResultadoDimensionalScan.objects.filter(pk=result.pk).update(nome_material=nome)
        return JsonResponse({'scan_id': scan_id, 'nome_material': nome, 'pendente': False})
    SystemConfiguration.objects.filter(pk=SystemConfiguration.get_config().pk).update(
        nome_material_proximo_ciclo=nome
    )
    return JsonResponse({'scan_id': None, 'nome_material': nome, 'pendente': True})


def ciclo_para_exportar(request):
    """O modo ao vivo sempre usa o último ciclo encerrado com leituras."""
    requested = request.GET.get('scan_id')
    status = get_collector_status()
    if requested:
        if status.get('is_scanning') and requested == status.get('scan_id'):
            return None
        return requested if LeituraScada.objects.filter(scan_id=requested).exists() else None
    scans = LeituraScada.objects.exclude(scan_id__isnull=True).exclude(scan_id='')
    if status.get('is_scanning') and status.get('scan_id'):
        scans = scans.exclude(scan_id=status['scan_id'])
    return scans.order_by('-scan_id').values_list('scan_id', flat=True).first()


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
    espessura_atual = collector_info.get("espessura_atual")

    # Fallback se o coletor estiver offline ou não reportou espessura_atual ainda
    if not espessura_atual or collector_status == "OFFLINE":
        try:
            from .scada_service import ScadaAcquisitionService
            espessura_atual = ScadaAcquisitionService.obter_leitura_instantanea_direta(config)
        except Exception:
            espessura_atual = {
                "valido": False,
                "espessura_mm": None,
                "posicao_x": None,
                "valor_bruto": None,
                "status": "OFFLINE",
                "motivo": "Coletor e Scada indisponíveis",
            }

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
            'nome_material': '',
            'nome_material_proximo_ciclo': config.nome_material_proximo_ciclo,
            'collector_status': collector_status,
            'is_live_cycle': False,
            'espessura_atual': espessura_atual,
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
    
    # Metadados de calibração pertencem somente ao ciclo histórico que os gravou.
    calib_id = leituras.exclude(calibracao_id__isnull=True).values_list('calibracao_id', flat=True).first()
    calib = CalibracaoEspessura.objects.filter(pk=calib_id).first() if calib_id else None
    faixa_calib = calib.get_faixa_calibrada() if calib else None

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
            'status_validade': getattr(l, 'status_validade', 'VALIDO'),
            'valor_bruto': l.valor_bruto,
            'zero_interpolado': l.zero_interpolado,
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
        'nome_material': ResultadoDimensionalScan.objects.filter(scan_id=scan_id).values_list('nome_material', flat=True).first() or '',
        'nome_material_proximo_ciclo': config.nome_material_proximo_ciclo,
        'collector_status': collector_status,
        'is_live_cycle': is_live_cycle,
        'espessura_atual': espessura_atual,
        'count': len(data),
        'data': data,
        'material': info_material,
        'dimensoes': dimensoes,
        'calibracao': {
            'id': calib.id if calib else None,
            'descricao': calib.descricao if calib else "Sem calibração ativa",
            'ativa': calib.ativa if calib else False,
            'fator_conversao': calib.fator_conversao if calib else None,
            'faixa_x': [faixa_calib[0], faixa_calib[1]] if faixa_calib else None,
        }
    })


def exportar_csv(request):
    """
    Gera relatório CSV ajustado conforme regras industriais.
    Inclui cabeçalho dimensional e dados de calibração metrológica.
    """
    scan_id = ciclo_para_exportar(request)
    if not scan_id:
        return HttpResponse('Nenhum ciclo concluído encontrado.', status=404)
    config = SystemConfiguration.get_config()

    leituras = LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura')
    filename = f"relatorio_{scan_id}.csv"

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response, delimiter=';')
    nome = ResultadoDimensionalScan.objects.filter(scan_id=scan_id).values_list('nome_material', flat=True).first() or ''
    # Impede que um nome fornecido pelo operador seja interpretado como fórmula no Excel.
    if nome.startswith(('=', '+', '-', '@')):
        nome = "'" + nome
    writer.writerow([f'# Material: {nome}'])

    if scan_id:
        dim = DimensionalAnalyzer.obter_ou_calcular_dimensoes(scan_id, config=config)
        if dim and dim.get("status") == "CONCLUIDO":
            writer.writerow(["# RELATORIO DIMENSIONAL DA BANDA DE RODAGEM"])
            writer.writerow([f"# Ciclo Scan ID: {scan_id}"])
            writer.writerow([f"# Largura Total: {str(dim.get('largura_total')).replace('.', ',')} mm"])
            writer.writerow([f"# Largura Coroa: {str(dim.get('largura_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# ESPESSURA MEDIA DA COROA: {str(dim.get('espessura_media_coroa')).replace('.', ',')} mm"])
            if dim.get('mediana_coroa') is not None:
                writer.writerow([f"# MEDIANA DA COROA: {str(dim['mediana_coroa']).replace('.', ',')} mm"])
            writer.writerow([f"# Espessura Min Coroa: {str(dim.get('espessura_min_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# Espessura Max Coroa: {str(dim.get('espessura_max_coroa')).replace('.', ',')} mm"])
            writer.writerow([f"# Pontos Validos Coroa: {dim.get('pontos_coroa', 0)}"])
            writer.writerow([f"# Espessura Lateral Esquerda: {str(dim.get('espessura_lateral_esq')).replace('.', ',')} mm"])
            writer.writerow([f"# Espessura Lateral Direita: {str(dim.get('espessura_lateral_dir')).replace('.', ',')} mm"])
            if dim.get('mediana_lateral_esq') is not None:
                writer.writerow([f"# Mediana Lateral Esquerda: {str(dim['mediana_lateral_esq']).replace('.', ',')} mm"])
            if dim.get('mediana_lateral_dir') is not None:
                writer.writerow([f"# Mediana Lateral Direita: {str(dim['mediana_lateral_dir']).replace('.', ',')} mm"])
            writer.writerow([])

    writer.writerow([
        'Scan ID', 
        'Data/Hora',
        'X (mm)', 
        'Y (mm)', 
    ])

    for l in leituras:
        y_str = str(round(l.eixo_y, 3)).replace('.', ',') if l.eixo_y is not None else "N/A"
        writer.writerow([
            l.scan_id,
            timezone.localtime(l.data_leitura).strftime('%d/%m/%Y %H:%M:%S'),
            str(l.eixo_x).replace('.', ','),
            y_str,
        ])

    return response


def exportar_excel(request):
    from .excel_export import criar_relatorio_excel

    scan_id = ciclo_para_exportar(request)
    if not scan_id:
        return HttpResponse('Nenhum ciclo concluído encontrado.', status=404)
    return criar_relatorio_excel(scan_id)
