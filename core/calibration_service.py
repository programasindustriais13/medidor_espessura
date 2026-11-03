import logging
from typing import Any, Dict, List, Optional, Tuple
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import CalibracaoEspessura, PontoCalibracao

logger = logging.getLogger(__name__)


class CalibrationService:
    """
    Serviço metrológico isolado para calibração e cálculo de espessura real (Y em mm).
    
    Responsabilidades:
      - Gerenciar a calibração ativa e seus pontos de referência físicos;
      - Realizar interpolação linear contínua do zero em função da posição X do sensor;
      - Rejeitar estritamente extrapolações fora da faixa física calibrada (ex: <80 ou >543 mm);
      - Calcular a espessura física sem perda de precisão ou arredondamentos prematuros:
            espessura_mm = (valor_bruto - zero_interpolado) * fator_conversao
      - Prover ativação atômica e transacional de calibrações.
    """

    _cached_calibration: Optional[CalibracaoEspessura] = None

    @classmethod
    def clear_cache(cls):
        """Limpa cache em memória da calibração ativa."""
        cls._cached_calibration = None

    @classmethod
    def get_active_calibration(cls, force_refresh: bool = False) -> Optional[CalibracaoEspessura]:
        """
        Retorna a calibração atualmente ativa no sistema com seus pontos pré-carregados.
        Utiliza cache efêmero invalidável quando solicitado.
        """
        if cls._cached_calibration is not None and not force_refresh:
            return cls._cached_calibration

        try:
            calib = (
                CalibracaoEspessura.objects.filter(ativa=True)
                .prefetch_related("pontos")
                .first()
            )
            cls._cached_calibration = calib
            return calib
        except Exception as e:
            logger.error(f"[CALIB SERVICE] Erro ao carregar calibração ativa: {e}")
            return None

    @classmethod
    def interpolar_zero(
        cls,
        posicao_x: float,
        calibracao: Optional[CalibracaoEspessura] = None,
    ) -> Tuple[Optional[float], Optional[str], Optional[Tuple[float, float]]]:
        """
        Interpola linearmente o valor de referência do zero para a coordenada X informada.
        
        Retorna tupla: (zero_interpolado, status_erro, faixa_calibrada)
        - Se dentro da faixa: (float_zero, None, (min_x, max_x))
        - Se fora da faixa: (None, "FORA_DA_FAIXA", (min_x, max_x))
        - Se sem calibração: (None, "SEM_CALIBRACAO", None)
        """
        calib = calibracao or cls.get_active_calibration()
        if not calib:
            return None, "SEM_CALIBRACAO", None

        pontos: List[PontoCalibracao] = sorted(
            list(calib.pontos.all()), key=lambda p: p.posicao_x
        )
        if len(pontos) < 2:
            return None, "PONTOS_INSUFICIENTES", None

        min_x = pontos[0].posicao_x
        max_x = pontos[-1].posicao_x
        faixa = (min_x, max_x)

        # Regra industrial mandatória: NÃO extrapolar silenciosamente
        if posicao_x < min_x or posicao_x > max_x:
            return None, "FORA_DA_FAIXA", faixa

        # Ponto exato na extremidade inferior
        if abs(posicao_x - min_x) < 1e-9:
            return float(pontos[0].zero_medio), None, faixa

        # Ponto exato na extremidade superior
        if abs(posicao_x - max_x) < 1e-9:
            return float(pontos[-1].zero_medio), None, faixa

        # Localizar intervalo [p_k, p_{k+1}]
        for i in range(len(pontos) - 1):
            p0 = pontos[i]
            p1 = pontos[i + 1]

            if p0.posicao_x <= posicao_x <= p1.posicao_x:
                dx = p1.posicao_x - p0.posicao_x
                if dx <= 0:
                    return float(p0.zero_medio), None, faixa

                # Interpolação linear contínua sem arredondamento intermediário
                t = (posicao_x - p0.posicao_x) / dx
                z_interpolado = p0.zero_medio + t * (p1.zero_medio - p0.zero_medio)
                return float(z_interpolado), None, faixa

        # Fallback de segurança se nenhuma faixa cobrir
        return None, "FORA_DA_FAIXA", faixa

    @classmethod
    def calcular_espessura(
        cls,
        posicao_x: float,
        valor_bruto: float,
        calibracao: Optional[CalibracaoEspessura] = None,
    ) -> Dict[str, Any]:
        """
        Calcula a espessura física Y em milímetros:
            espessura_mm = (valor_bruto - zero_interpolado) * fator_conversao
        
        Garante rastreabilidade completa e identificação de condições inválidas.
        """
        calib = calibracao or cls.get_active_calibration()
        if not calib:
            return {
                "valido": False,
                "status": "SEM_CALIBRACAO",
                "motivo": "Nenhuma calibração ativa configurada no sistema.",
                "posicao_x": posicao_x,
                "valor_bruto": valor_bruto,
                "zero_interpolado": None,
                "fator_conversao": None,
                "espessura_mm": None,
                "calibracao_id": None,
                "faixa_calibrada": None,
            }

        zero_interpolado, err, faixa = cls.interpolar_zero(posicao_x, calibracao=calib)

        if err == "FORA_DA_FAIXA":
            min_x, max_x = faixa if faixa else (0.0, 0.0)
            return {
                "valido": False,
                "status": "FORA_DA_FAIXA",
                "motivo": (
                    f"Posição física X={posicao_x:.2f}mm está fora da faixa calibrada "
                    f"({min_x:.1f} a {max_x:.1f} mm). Extrapolação bloqueada."
                ),
                "posicao_x": posicao_x,
                "valor_bruto": valor_bruto,
                "zero_interpolado": None,
                "fator_conversao": calib.fator_conversao,
                "espessura_mm": None,
                "calibracao_id": calib.id,
                "faixa_calibrada": faixa,
            }

        if err or zero_interpolado is None:
            return {
                "valido": False,
                "status": "SEM_CALIBRACAO" if err == "PONTOS_INSUFICIENTES" else "ERRO_LEITURA",
                "motivo": f"Falha na interpolação do zero: {err}",
                "posicao_x": posicao_x,
                "valor_bruto": valor_bruto,
                "zero_interpolado": None,
                "fator_conversao": calib.fator_conversao,
                "espessura_mm": None,
                "calibracao_id": calib.id,
                "faixa_calibrada": faixa,
            }

        fator = calib.fator_conversao
        if fator is None or fator <= 0:
            fator = calib.calcular_fator_conversao(salvar=True)

        if fator is None or fator <= 0:
            return {
                "valido": False,
                "status": "SEM_CALIBRACAO",
                "motivo": "Fator de conversão da calibração ativa é inválido ou nulo.",
                "posicao_x": posicao_x,
                "valor_bruto": valor_bruto,
                "zero_interpolado": zero_interpolado,
                "fator_conversao": None,
                "espessura_mm": None,
                "calibracao_id": calib.id,
                "faixa_calibrada": faixa,
            }

        # Cálculo de precisão máxima sem arredondamentos prematuros
        espessura_mm = (valor_bruto - zero_interpolado) * fator

        return {
            "valido": True,
            "status": "VALIDO",
            "motivo": "Medição dimensional calibrada com sucesso.",
            "posicao_x": posicao_x,
            "valor_bruto": valor_bruto,
            "zero_interpolado": zero_interpolado,
            "fator_conversao": fator,
            "espessura_mm": espessura_mm,
            "calibracao_id": calib.id,
            "faixa_calibrada": faixa,
        }

    @classmethod
    def ativar_calibracao(cls, calibracao_id: int) -> CalibracaoEspessura:
        """
        Ativa atomicamente uma calibração por ID, assegurando desativação de todas as outras.
        Lança ValidationError se a calibração for inválida.
        """
        try:
            calib = CalibracaoEspessura.objects.prefetch_related("pontos").get(pk=calibracao_id)
        except CalibracaoEspessura.DoesNotExist:
            raise ValidationError(f"Calibração ID {calibracao_id} não encontrada.")

        valida, msg = calib.is_valid_for_activation()
        if not valida:
            raise ValidationError(f"Não é possível ativar a calibração: {msg}")

        with transaction.atomic():
            CalibracaoEspessura.objects.filter(ativa=True).exclude(pk=calib.pk).update(ativa=False)
            calib.ativa = True
            calib.calcular_fator_conversao()
            calib.save()

        cls.clear_cache()
        logger.info(f"[CALIB SERVICE] Calibração ID={calib.id} ('{calib.descricao}') ativada com sucesso.")
        return calib

