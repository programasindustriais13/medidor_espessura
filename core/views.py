import csv
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from .models import LeituraScada
from django.db.models import Max
from .profile_logic import ProfileLogic

def dashboard(request):
    """
    Renderiza a interface principal do medidor.
    """
    return render(request, 'core/dashboard.html')




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
    Retorna os dados enriquecidos com a lógica de perfil.
    Parâmetro opcional GET 'scan_id'.
    """
    scan_id = request.GET.get('scan_id')

    # Parâmetros de Lógica da UI
    p_esp_borda = request.GET.get('esp_borda')
    p_centro = request.GET.get('centro')
    p_tol = request.GET.get('tol')

    if not scan_id:
        # Se não informado, pega o mais recente
        scan_id = LeituraScada.objects.all().aggregate(Max('scan_id'))['scan_id__max']
    
    if not scan_id:
        return JsonResponse({'scan_id': None, 'data': []})

    # Buscar dados ordenados por Data Leitura (para manter ordem cronológica de varredura)
    # ou por Eixo X se quiséssemos plotar puramente posicional, mas time é source of truth da coleta.
    leituras = LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura')
    
    data = []
    for l in leituras:
        # Aplica a Regra de Negócio (Geometria + Tolerância)
        # Passando os parâmetros dinâmicos se existirem
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
    
    return JsonResponse({
        'scan_id': scan_id,
        'count': len(data),
        'data': data
    })

def exportar_csv(request):
    """
    Gera relatório CSV ajustado conforme regras industriais.
    """
    scan_id = request.GET.get('scan_id')
    
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
