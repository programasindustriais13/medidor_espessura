import logging
import math
from statistics import median
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
        Com espaçamento uniforme, os pontos extremos recebem metade do peso dos internos.
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

    @staticmethod
    def consolidar_posicoes(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Representa cada posição X pela mediana de suas leituras, sem alterar os registros."""
        if not pts:
            return []
        agrupados = []
        grupo = [pts[0]]
        for ponto in sorted(pts, key=lambda p: p[0])[1:]:
            if abs(ponto[0] - grupo[0][0]) < 0.005:
                grupo.append(ponto)
            else:
                agrupados.append((median(p[0] for p in grupo), median(p[1] for p in grupo)))
                grupo = [ponto]
        agrupados.append((median(p[0] for p in grupo), median(p[1] for p in grupo)))
        return agrupados

    @staticmethod
    def aparar_rampa(pts: List[Tuple[float, float]], lado: str) -> List[Tuple[float, float]]:
        """Remove apenas a extremidade que ainda difere do patamar local."""
        pts = list(pts)
        if lado in ("esquerda", "ambos"):
            while len(pts) >= 3 and abs(pts[0][1] - median(p[1] for p in pts[:3])) > 0.15:
                pts.pop(0)
        if lado in ("direita", "ambos"):
            while len(pts) >= 3 and abs(pts[-1][1] - median(p[1] for p in pts[-3:])) > 0.15:
                pts.pop()
        return pts

    @staticmethod
    def separar_picos_isolados(
        pts: List[Tuple[float, float]], limite_mm: float = 0.3,
        acordo_vizinhos_mm: float = 0.2,
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Sinaliza desvios isolados dentro do patamar; mantém transições e dados brutos."""
        if len(pts) < 5:
            return list(pts), []
        espacamentos = [pts[i + 1][0] - pts[i][0] for i in range(len(pts) - 1)]
        passo = median(dx for dx in espacamentos if dx > 0)
        aceitos, suspeitos = [], []
        for i, ponto in enumerate(pts):
            if i < 2 or i >= len(pts) - 2:
                aceitos.append(ponto)
                continue
            esq = pts[i - 2:i]
            dir_ = pts[i + 1:i + 3]
            if (ponto[0] - esq[-1][0] > 2.5 * passo or
                    dir_[0][0] - ponto[0] > 2.5 * passo):
                aceitos.append(ponto)
                continue
            med_esq = median(p[1] for p in esq)
            med_dir = median(p[1] for p in dir_)
            referencia = median([med_esq, med_dir])
            if (abs(esq[-1][1] - dir_[0][1]) <= acordo_vizinhos_mm + 1e-9 and
                    abs(med_esq - med_dir) <= acordo_vizinhos_mm + 1e-9 and
                    abs(ponto[1] - referencia) >= limite_mm - 1e-9):
                suspeitos.append(ponto)
            else:
                aceitos.append(ponto)
        return aceitos, suspeitos

    @staticmethod
    def picos_em_posicoes_repetidas(
        pontos_brutos: List[Tuple[float, float]],
        posicoes_patamar: List[Tuple[float, float]],
    ) -> List[Tuple[float, float]]:
        """Sinaliza uma leitura divergente quando outras na mesma posição concordam."""
        xs_patamar = [p[0] for p in posicoes_patamar]
        suspeitos = []
        ordenados = sorted(pontos_brutos, key=lambda p: p[0])
        grupo = []
        for ponto in ordenados + [(float("inf"), 0.0)]:
            if grupo and abs(ponto[0] - grupo[0][0]) >= 0.005:
                x = grupo[0][0]
                if len(grupo) >= 3 and any(abs(x - xp) < 0.005 for xp in xs_patamar):
                    for indice, (_, y) in enumerate(grupo):
                        outros = [p[1] for j, p in enumerate(grupo) if j != indice]
                        if (max(outros) - min(outros) <= 0.2 + 1e-9 and
                                abs(y - median(outros)) >= 0.3 - 1e-9):
                            suspeitos.append((x, y))
                grupo = []
            grupo.append(ponto)
        return suspeitos

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

        # O corte detecta a extensão da peça; dentro dela aceitamos também a
        # oscilação natural até 0,2 mm abaixo do corte (ex.: 0,9 mm no bloco de 1 mm).
        pontos_material = [(x, y) for x, y in coords_brutas if y >= corte_min]
        if pontos_material:
            x_material_ini = min(p[0] for p in pontos_material)
            x_material_fim = max(p[0] for p in pontos_material)
            piso_interno = max(0.0, corte_min - 0.2)
            valid_coords = [
                (x, y) for x, y in coords_brutas
                if x_material_ini <= x <= x_material_fim and y >= piso_interno - 1e-9
            ]
        else:
            valid_coords = []

        if len(valid_coords) < min_pts:
            return {
                "status": "DADOS_INSUFICIENTES",
                "motivo": (
                    f"Quantidade de medições válidas insuficiente ({len(valid_coords)} pts na extensão da peça; "
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

        # Uma posição repetida contribui uma vez; a mediana não depende da ordem
        # em que as leituras chegaram ao Scada.
        clean_pts = cls.consolidar_posicoes(valid_coords)

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
        limites_manuais = (
            modo_coroa == "manual"
            and manual_x_ini is not None
            and manual_x_fim is not None
            and manual_x_ini < manual_x_fim
        )
        if limites_manuais:
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

        # 6. Selecionar pontos da coroa e calcular ESPESSURA MÉDIA DA COROA (mm)
        # As extremidades do intervalo detectado podem conter parte da rampa.
        pts_coroa = cls.aparar_rampa(
            [p for p in clean_pts if x_inicio_coroa <= p[0] <= x_fim_coroa], "ambos"
        )
        if pts_coroa and not limites_manuais:
            x_inicio_coroa, x_fim_coroa = pts_coroa[0][0], pts_coroa[-1][0]
        largura_coroa = x_fim_coroa - x_inicio_coroa
        if len(pts_coroa) < 2:
            return {
                "status": "SEM_COROA_CLARA",
                "motivo": "Pontos estáveis insuficientes no intervalo delimitado para a coroa.",
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

        pts_coroa, suspeitos_coroa = cls.separar_picos_isolados(pts_coroa)
        # Média espacial dos pontos aceitos; a mediana registra o nível típico.
        espessura_media_coroa = cls.calcular_media_espacial(pts_coroa)
        mediana_coroa = median(p[1] for p in pts_coroa)

        # Média aritmética e estatísticas auxiliares
        ys_coroa = [p[1] for p in pts_coroa]
        media_aritmetica = sum(ys_coroa) / len(ys_coroa)
        espessura_min_coroa = min(ys_coroa)
        espessura_max_coroa = max(ys_coroa)

        # 7. Cálculo das espessuras laterais (excluindo rampas de transição)
        # Limiar de corte para desconsiderar a subida da rampa: Y <= y_base + 0.30 * delta_h
        limiar_lateral = y_base + 0.30 * delta_h

        pts_lat_esq = cls.aparar_rampa(
            [p for p in clean_pts if p[0] < x_inicio_coroa and p[1] <= limiar_lateral], "direita"
        )
        pts_lat_dir = cls.aparar_rampa(
            [p for p in clean_pts if p[0] > x_fim_coroa and p[1] <= limiar_lateral], "esquerda"
        )
        pts_lat_esq, suspeitos_esq = cls.separar_picos_isolados(pts_lat_esq)
        pts_lat_dir, suspeitos_dir = cls.separar_picos_isolados(pts_lat_dir)

        esp_lat_esq: Optional[float] = None
        if pts_lat_esq:
            esp_lat_esq = cls.calcular_media_espacial(pts_lat_esq)

        esp_lat_dir: Optional[float] = None
        if pts_lat_dir:
            esp_lat_dir = cls.calcular_media_espacial(pts_lat_dir)

        suspeitos = suspeitos_esq + suspeitos_coroa + suspeitos_dir
        suspeitos += cls.picos_em_posicoes_repetidas(
            valid_coords, pts_lat_esq + pts_coroa + pts_lat_dir
        )

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
            "mediana_coroa": round(mediana_coroa, 2),
            "mediana_lateral_esq": round(median(p[1] for p in pts_lat_esq), 2) if pts_lat_esq else None,
            "mediana_lateral_dir": round(median(p[1] for p in pts_lat_dir), 2) if pts_lat_dir else None,
            "pontos_suspeitos": len(suspeitos),
            "posicoes_suspeitas": sorted({round(p[0], 3) for p in suspeitos}),
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
                        "analise_robusta": {
                            campo: resultado.get(campo) for campo in (
                                "mediana_coroa", "mediana_lateral_esq", "mediana_lateral_dir",
                                "pontos_suspeitos", "posicoes_suspeitas",
                            )
                        },
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

        def metadados_robustos(obj):
            # Resultados antigos não têm estes metadados e permanecem intactos.
            return (obj.parametros_calculo or {}).get("analise_robusta", {})

        if not force_recalc:
            existente = ResultadoDimensionalScan.objects.filter(scan_id=scan_id).first()
            if existente and existente.status_leitura != 'EM_ANDAMENTO':
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
                    **metadados_robustos(existente),
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
                **metadados_robustos(obj),
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

