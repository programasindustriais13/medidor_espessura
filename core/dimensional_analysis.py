import logging
import math
from typing import Any, Dict, List, Optional, Tuple, Union

from django.db import transaction

logger = logging.getLogger(__name__)


class DimensionalAnalyzer:
    """
    Motor metrológico de análise geométrica e cálculo dimensional da banda de rodagem.
    
    Identifica de forma automática e robusta:
      1. Extremidades da peça e Largura Total da banda (x_dir - x_esq).
      2. Rampas de transição geométrica entre as abas laterais e a coroa central.
      3. Limites físicos da coroa elevada (x_inicio_coroa, x_fim_coroa) e Largura da Coroa.
      4. Espessura MÉDIA da coroa calculada sobre todos os pontos válidos do patamar central
         via integração espacial ponderada por intervalo (dx) / trapezoidal, excluindo rampas.
      5. Espessura mínima, máxima e contagem de pontos da coroa.
      6. Espessuras laterais independentes (aba esquerda e aba direita).
    """

    @classmethod
    def extrair_coordenadas(cls, pontos: List[Any]) -> List[Tuple[float, float]]:
        """
        Converte pontos em tuplas (x, y) numéricas válidas e ordenadas.
        Suporta dicionários ({'x': ..., 'y': ...} ou {'eixo_x': ..., 'eixo_y': ...})
        ou instâncias de modelo (LeituraScada).
        """
        coords: List[Tuple[float, float]] = []
        for p in pontos:
            if isinstance(p, dict):
                x = p.get('x') if 'x' in p else p.get('eixo_x')
                y = p.get('y') if 'y' in p else p.get('eixo_y')
            else:
                x = getattr(p, 'eixo_x', None)
                y = getattr(p, 'eixo_y', None)

            if x is not None and y is not None:
                try:
                    fx, fy = float(x), float(y)
                    if not (math.isnan(fx) or math.isnan(fy) or math.isinf(fx) or math.isinf(fy)):
                        coords.append((fx, fy))
                except (ValueError, TypeError):
                    continue

        return coords

    @classmethod
    def suavizar_perfil(cls, ys: List[float], janela: int = 3) -> List[float]:
        """
        Aplica suavização por média móvel centrada com tratamento de bordas.
        Reduz ruídos pontuais de leitura sem alterar a posição das transições.
        """
        n = len(ys)
        if n <= 2 or janela <= 1:
            return list(ys)

        suavizados: List[float] = []
        raio = janela // 2
        for i in range(n):
            ini = max(0, i - raio)
            fim = min(n, i + raio + 1)
            janela_vals = ys[ini:fim]
            suavizados.append(sum(janela_vals) / len(janela_vals))
        return suavizados

    @classmethod
    def calcular_media_espacial(cls, pts: List[Tuple[float, float]]) -> float:
        """
        Calcula a média espacial contínua (integral trapezoidal dividida pela largura):
            y_medio = [ integral(y * dx) ] / (x_fim - x_ini)
        Garante que regiões com maior concentração ou adensamento de leituras (amostragem irregular)
        não tenham peso artificialmente inflado em relação a regiões com amostras mais espaçadas.
        Se os pontos forem uniformemente distribuídos, equivale à média aritmética.
        """
        if not pts:
            return 0.0
        if len(pts) == 1:
            return pts[0][1]

        # Garantir ordenação por X
        pts_sorted = sorted(pts, key=lambda p: p[0])
        x_span = pts_sorted[-1][0] - pts_sorted[0][0]
        if x_span <= 0.0001:
            # Posição X constante ou praticamente constante
            return sum(p[1] for p in pts_sorted) / len(pts_sorted)

        integral = 0.0
        for i in range(len(pts_sorted) - 1):
            dx = pts_sorted[i + 1][0] - pts_sorted[i][0]
            if dx > 0:
                y_medio_degrau = (pts_sorted[i][1] + pts_sorted[i + 1][1]) / 2.0
                integral += y_medio_degrau * dx

        return integral / x_span

    @classmethod
    def analisar_perfil(
        cls,
        pontos: List[Any],
        config: Optional[Any] = None,
        is_scanning: bool = False,
    ) -> Dict[str, Any]:
        """
        Executa a análise dimensional geométrica completa do conjunto de pontos.
        """
        # Extração de parâmetros de configuração
        corte_min = getattr(config, 'corte_min_espessura_banda', 1.0) if config else 1.0
        limiar_relativo = getattr(config, 'limiar_coroa_relativo', 0.75) if config else 0.75
        min_dh = getattr(config, 'min_delta_altura_coroa', 0.8) if config else 0.8
        min_pts = getattr(config, 'min_pontos_scan', 15) if config else 15
        modo_coroa = getattr(config, 'modo_deteccao_coroa', 'auto') if config else 'auto'
        manual_x_ini = getattr(config, 'manual_x_inicio_coroa', None) if config else None
        manual_x_fim = getattr(config, 'manual_x_fim_coroa', None) if config else None

        coords_brutas = cls.extrair_coordenadas(pontos)
        total_bruto = len(coords_brutas)

        # 1. Se ciclo estiver ativo em tempo real
        if is_scanning:
            return {
                "status": "EM_ANDAMENTO",
                "motivo": "Ciclo de varredura em andamento pelo sensor.",
                "total_pontos": total_bruto,
                "pontos_validos": total_bruto,
                "largura_total": None,
                "largura_coroa": None,
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": None,
                "x_direita": None,
                "x_inicio_coroa": None,
                "x_fim_coroa": None,
            }

        # 2. Validação de dados mínimos
        if total_bruto == 0:
            return {
                "status": "DADOS_INSUFICIENTES",
                "motivo": "Nenhuma medição registrada no ciclo.",
                "total_pontos": 0,
                "pontos_validos": 0,
                "largura_total": None,
                "largura_coroa": None,
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": None,
                "x_direita": None,
                "x_inicio_coroa": None,
                "x_fim_coroa": None,
            }

        # 3. Filtrar leituras fora da chapa / retorno do carro em vazio
        # Desconsidera pontos com Y <= corte_min (ex: carro em vazio no ar ou retorno)
        valid_coords = [(x, y) for (x, y) in coords_brutas if y >= corte_min]

        if len(valid_coords) < min_pts:
            return {
                "status": "DADOS_INSUFICIENTES",
                "motivo": (
                    f"Quantidade de medições válidas insuficiente ({len(valid_coords)} pts com espessura >= {corte_min:.1f} mm; "
                    f"mínimo exigido: {min_pts}). Ciclo potencialmente interrompido ou sensor fora de peça."
                ),
                "total_pontos": total_bruto,
                "pontos_validos": len(valid_coords),
                "largura_total": None,
                "largura_coroa": None,
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": None,
                "x_direita": None,
                "x_inicio_coroa": None,
                "x_fim_coroa": None,
            }

        # 4. Ordenar pontos por X e consolidar duplicidades espaciais estritas
        valid_coords.sort(key=lambda p: p[0])

        clean_pts: List[Tuple[float, float]] = []
        for x, y in valid_coords:
            if clean_pts and abs(clean_pts[-1][0] - x) < 0.005:
                # Média ponderada de espessura para a mesma coordenada X
                prev_x, prev_y = clean_pts[-1]
                clean_pts[-1] = (prev_x, (prev_y + y) / 2.0)
            else:
                clean_pts.append((x, y))

        xs = [p[0] for p in clean_pts]
        ys = [p[1] for p in clean_pts]

        x_esq = xs[0]
        x_dir = xs[-1]
        largura_total = x_dir - x_esq

        if largura_total < 0.5:
            return {
                "status": "SEM_COROA_CLARA",
                "motivo": f"Deslocamento transversal estático ({largura_total:.2f} mm). Não há varredura de largura suficiente.",
                "total_pontos": total_bruto,
                "pontos_validos": len(clean_pts),
                "largura_total": round(largura_total, 2),
                "largura_coroa": None,
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": round(x_esq, 2),
                "x_direita": round(x_dir, 2),
                "x_inicio_coroa": None,
                "x_fim_coroa": None,
            }

        # 5. Análise de patamar e detecção da coroa elevada
        x_inicio_coroa: Optional[float] = None
        x_fim_coroa: Optional[float] = None

        # Suavização preliminar para filtrar ruído de alta frequência
        ys_smooth = cls.suavizar_perfil(ys, janela=3)

        # Determinar linha de base inferior (laterais) e nível de crista (coroa)
        sorted_ys = sorted(ys_smooth)
        n_pts = len(sorted_ys)
        k_slice = max(3, n_pts // 5)
        y_base = sum(sorted_ys[:k_slice]) / k_slice
        y_peak = sum(sorted_ys[-k_slice:]) / k_slice
        delta_h = y_peak - y_base

        # Verificação do modo manual
        if (
            modo_coroa == "manual"
            and manual_x_ini is not None
            and manual_x_fim is not None
            and manual_x_ini < manual_x_fim
        ):
            x_inicio_coroa = max(x_esq, float(manual_x_ini))
            x_fim_coroa = min(x_dir, float(manual_x_fim))
        else:
            # Modo Automático: Baseado na geometria real de transição
            if delta_h < min_dh:
                # Perfil plano ou sem degrau identificável entre centro e laterais
                return {
                    "status": "SEM_COROA_CLARA",
                    "motivo": (
                        f"Perfil com espessura uniforme sem coroa claramente identificável "
                        f"(variação vertical pico-base de {delta_h:.2f} mm inferior ao limiar de {min_dh:.2f} mm)."
                    ),
                    "total_pontos": total_bruto,
                    "pontos_validos": len(clean_pts),
                    "largura_total": round(largura_total, 2),
                    "largura_coroa": None,
                    "espessura_media_coroa": None,
                    "espessura_min_coroa": None,
                    "espessura_max_coroa": None,
                    "pontos_coroa": 0,
                    "media_aritmetica_coroa": None,
                    "espessura_lateral_esq": round(y_base, 2),
                    "espessura_lateral_dir": round(y_base, 2),
                    "espessura_lateral_media": round(y_base, 2),
                    "x_esquerda": round(x_esq, 2),
                    "x_direita": round(x_dir, 2),
                    "x_inicio_coroa": None,
                    "x_fim_coroa": None,
                }

            # Limiar para estar no topo do patamar da coroa (excluindo rampas)
            y_patamar = y_base + limiar_relativo * delta_h

            indices_patamar = [i for i, y in enumerate(ys_smooth) if y >= y_patamar]
            if not indices_patamar:
                # Fallback com limiar mais permissivo (0.60 * delta_h)
                y_patamar = y_base + 0.60 * delta_h
                indices_patamar = [i for i, y in enumerate(ys_smooth) if y >= y_patamar]

            if not indices_patamar or (indices_patamar[-1] == indices_patamar[0]):
                return {
                    "status": "SEM_COROA_CLARA",
                    "motivo": "Não foi possível delimitar o patamar elevado da coroa a partir da curvatura do perfil.",
                    "total_pontos": total_bruto,
                    "pontos_validos": len(clean_pts),
                    "largura_total": round(largura_total, 2),
                    "largura_coroa": None,
                    "espessura_media_coroa": None,
                    "espessura_min_coroa": None,
                    "espessura_max_coroa": None,
                    "pontos_coroa": 0,
                    "media_aritmetica_coroa": None,
                    "espessura_lateral_esq": None,
                    "espessura_lateral_dir": None,
                    "espessura_lateral_media": None,
                    "x_esquerda": round(x_esq, 2),
                    "x_direita": round(x_dir, 2),
                    "x_inicio_coroa": None,
                    "x_fim_coroa": None,
                }

            x_inicio_coroa = xs[indices_patamar[0]]
            x_fim_coroa = xs[indices_patamar[-1]]

        # Validação final dos limites da coroa
        if x_inicio_coroa is None or x_fim_coroa is None or (x_fim_coroa <= x_inicio_coroa):
            return {
                "status": "SEM_COROA_CLARA",
                "motivo": "Limites de coroa inconsistentes na geometria detectada.",
                "total_pontos": total_bruto,
                "pontos_validos": len(clean_pts),
                "largura_total": round(largura_total, 2),
                "largura_coroa": None,
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": round(x_esq, 2),
                "x_direita": round(x_dir, 2),
                "x_inicio_coroa": None,
                "x_fim_coroa": None,
            }

        largura_coroa = x_fim_coroa - x_inicio_coroa

        # 6. Selecionar pontos da coroa e calcular ESPESSURA MÉDIA DA COROA (mm)
        # Importante: utiliza os valores REAIS do eixo Y de todos os pontos no intervalo
        pts_coroa = [p for p in clean_pts if x_inicio_coroa <= p[0] <= x_fim_coroa]
        if not pts_coroa:
            return {
                "status": "SEM_COROA_CLARA",
                "motivo": "Nenhum ponto registrado no intervalo delimitado para a coroa.",
                "total_pontos": total_bruto,
                "pontos_validos": len(clean_pts),
                "largura_total": round(largura_total, 2),
                "largura_coroa": round(largura_coroa, 2),
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": round(x_esq, 2),
                "x_direita": round(x_dir, 2),
                "x_inicio_coroa": round(x_inicio_coroa, 2),
                "x_fim_coroa": round(x_fim_coroa, 2),
            }

        # Cálculo da média espacial (ponderada pela distância transversal X)
        espessura_media_coroa = cls.calcular_media_espacial(pts_coroa)

        # Média aritmética e estatísticas auxiliares
        ys_coroa = [p[1] for p in pts_coroa]
        media_aritmetica = sum(ys_coroa) / len(ys_coroa)
        espessura_min_coroa = min(ys_coroa)
        espessura_max_coroa = max(ys_coroa)

        # 7. Cálculo das espessuras laterais (excluindo rampas de transição)
        # Limiar de corte para desconsiderar a subida da rampa: Y <= y_base + 0.30 * delta_h
        limiar_lateral = y_base + 0.30 * delta_h

        pts_lat_esq = [p for p in clean_pts if p[0] < x_inicio_coroa and p[1] <= limiar_lateral]
        pts_lat_dir = [p for p in clean_pts if p[0] > x_fim_coroa and p[1] <= limiar_lateral]

        esp_lat_esq: Optional[float] = None
        if pts_lat_esq:
            esp_lat_esq = cls.calcular_media_espacial(pts_lat_esq)

        esp_lat_dir: Optional[float] = None
        if pts_lat_dir:
            esp_lat_dir = cls.calcular_media_espacial(pts_lat_dir)

        # Média geral lateral
        if esp_lat_esq is not None and esp_lat_dir is not None:
            esp_lat_media = (esp_lat_esq + esp_lat_dir) / 2.0
        elif esp_lat_esq is not None:
            esp_lat_media = esp_lat_esq
        elif esp_lat_dir is not None:
            esp_lat_media = esp_lat_dir
        else:
            esp_lat_media = None

        return {
            "status": "CONCLUIDO",
            "motivo": "Cálculo dimensional executado com sucesso.",
            "total_pontos": total_bruto,
            "pontos_validos": len(clean_pts),
            "largura_total": round(largura_total, 2),
            "largura_coroa": round(largura_coroa, 2),
            "espessura_media_coroa": round(espessura_media_coroa, 2),
            "espessura_media_coroa_precisa": espessura_media_coroa,
            "espessura_min_coroa": round(espessura_min_coroa, 2),
            "espessura_max_coroa": round(espessura_max_coroa, 2),
            "pontos_coroa": len(pts_coroa),
            "media_aritmetica_coroa": round(media_aritmetica, 2),
            "espessura_lateral_esq": round(esp_lat_esq, 2) if esp_lat_esq is not None else None,
            "espessura_lateral_dir": round(esp_lat_dir, 2) if esp_lat_dir is not None else None,
            "espessura_lateral_media": round(esp_lat_media, 2) if esp_lat_media is not None else None,
            "x_esquerda": round(x_esq, 2),
            "x_direita": round(x_dir, 2),
            "x_inicio_coroa": round(x_inicio_coroa, 2),
            "x_fim_coroa": round(x_fim_coroa, 2),
        }

    @classmethod
    def calcular_e_persistir_scan(cls, scan_id: str, config: Optional[Any] = None) -> Any:
        """
        Executa a análise dimensional sobre todos os pontos do scan_id e persiste
        o resultado em ResultadoDimensionalScan.
        """
        if not scan_id:
            return None

        from .models import LeituraScada, ResultadoDimensionalScan, SystemConfiguration

        if config is None:
            config = SystemConfiguration.get_config()

        leituras = list(
            LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura').values('eixo_x', 'eixo_y')
        )

        resultado = cls.analisar_perfil(leituras, config=config, is_scanning=False)

        # Salva ou atualiza atomicamente no banco local
        with transaction.atomic():
            obj, _ = ResultadoDimensionalScan.objects.update_or_create(
                scan_id=scan_id,
                defaults={
                    "largura_total": resultado.get("largura_total"),
                    "largura_coroa": resultado.get("largura_coroa"),
                    "espessura_media_coroa": resultado.get("espessura_media_coroa"),
                    "espessura_min_coroa": resultado.get("espessura_min_coroa"),
                    "espessura_max_coroa": resultado.get("espessura_max_coroa"),
                    "pontos_coroa": resultado.get("pontos_coroa", 0),
                    "media_aritmetica_coroa": resultado.get("media_aritmetica_coroa"),
                    "espessura_lateral_esq": resultado.get("espessura_lateral_esq"),
                    "espessura_lateral_dir": resultado.get("espessura_lateral_dir"),
                    "espessura_lateral_media": resultado.get("espessura_lateral_media"),
                    "x_esquerda": resultado.get("x_esquerda"),
                    "x_direita": resultado.get("x_direita"),
                    "x_inicio_coroa": resultado.get("x_inicio_coroa"),
                    "x_fim_coroa": resultado.get("x_fim_coroa"),
                    "total_pontos": resultado.get("total_pontos", 0),
                    "status_leitura": resultado.get("status", "CONCLUIDO"),
                    "motivo_status": resultado.get("motivo", ""),
                    "parametros_calculo": {
                        "corte_min": getattr(config, 'corte_min_espessura_banda', 1.0),
                        "limiar_coroa": getattr(config, 'limiar_coroa_relativo', 0.75),
                        "min_dh": getattr(config, 'min_delta_altura_coroa', 0.8),
                    },
                }
            )

        return obj

    @classmethod
    def obter_ou_calcular_dimensoes(
        cls,
        scan_id: str,
        config: Optional[Any] = None,
        force_recalc: bool = False,
        is_live_cycle: bool = False,
    ) -> Dict[str, Any]:
        """
        Obtém os resultados dimensionais do scan_id a partir do banco ou calcula sob demanda.
        """
        if not scan_id:
            return {
                "status": "DADOS_INSUFICIENTES",
                "motivo": "ID do ciclo não informado.",
                "largura_total": None,
                "largura_coroa": None,
                "espessura_media_coroa": None,
                "espessura_min_coroa": None,
                "espessura_max_coroa": None,
                "pontos_coroa": 0,
                "media_aritmetica_coroa": None,
                "espessura_lateral_esq": None,
                "espessura_lateral_dir": None,
                "espessura_lateral_media": None,
                "x_esquerda": None,
                "x_direita": None,
                "x_inicio_coroa": None,
                "x_fim_coroa": None,
            }

        if is_live_cycle:
            # Em modo ao vivo enquanto medindo, não salva no banco ainda
            from .models import LeituraScada, SystemConfiguration
            if config is None:
                config = SystemConfiguration.get_config()
            leituras = list(
                LeituraScada.objects.filter(scan_id=scan_id).order_by('data_leitura').values('eixo_x', 'eixo_y')
            )
            return cls.analisar_perfil(leituras, config=config, is_scanning=True)

        from .models import ResultadoDimensionalScan

        if not force_recalc:
            existente = ResultadoDimensionalScan.objects.filter(scan_id=scan_id).first()
            if existente:
                return {
                    "status": existente.status_leitura,
                    "motivo": existente.motivo_status,
                    "total_pontos": existente.total_pontos,
                    "largura_total": existente.largura_total,
                    "largura_coroa": existente.largura_coroa,
                    "espessura_media_coroa": existente.espessura_media_coroa,
                    "espessura_min_coroa": existente.espessura_min_coroa,
                    "espessura_max_coroa": existente.espessura_max_coroa,
                    "pontos_coroa": existente.pontos_coroa,
                    "media_aritmetica_coroa": existente.media_aritmetica_coroa,
                    "espessura_lateral_esq": existente.espessura_lateral_esq,
                    "espessura_lateral_dir": existente.espessura_lateral_dir,
                    "espessura_lateral_media": existente.espessura_lateral_media,
                    "x_esquerda": existente.x_esquerda,
                    "x_direita": existente.x_direita,
                    "x_inicio_coroa": existente.x_inicio_coroa,
                    "x_fim_coroa": existente.x_fim_coroa,
                }

        # Se não existe em cache ou se forçamos recalculo
        obj = cls.calcular_e_persistir_scan(scan_id, config=config)
        if obj:
            return {
                "status": obj.status_leitura,
                "motivo": obj.motivo_status,
                "total_pontos": obj.total_pontos,
                "largura_total": obj.largura_total,
                "largura_coroa": obj.largura_coroa,
                "espessura_media_coroa": obj.espessura_media_coroa,
                "espessura_min_coroa": obj.espessura_min_coroa,
                "espessura_max_coroa": obj.espessura_max_coroa,
                "pontos_coroa": obj.pontos_coroa,
                "media_aritmetica_coroa": obj.media_aritmetica_coroa,
                "espessura_lateral_esq": obj.espessura_lateral_esq,
                "espessura_lateral_dir": obj.espessura_lateral_dir,
                "espessura_lateral_media": obj.espessura_lateral_media,
                "x_esquerda": obj.x_esquerda,
                "x_direita": obj.x_direita,
                "x_inicio_coroa": obj.x_inicio_coroa,
                "x_fim_coroa": obj.x_fim_coroa,
            }

        return {
            "status": "DADOS_INSUFICIENTES",
            "motivo": "Falha ao processar ciclo.",
            "largura_total": None,
            "largura_coroa": None,
            "espessura_media_coroa": None,
            "espessura_min_coroa": None,
            "espessura_max_coroa": None,
            "pontos_coroa": 0,
            "media_aritmetica_coroa": None,
            "espessura_lateral_esq": None,
            "espessura_lateral_dir": None,
            "espessura_lateral_media": None,
            "x_esquerda": None,
            "x_direita": None,
            "x_inicio_coroa": None,
            "x_fim_coroa": None,
        }

