from django.db import models
from django.core.exceptions import ValidationError
from django.conf import settings

# Importação dos modelos não-gerenciados do Scada-LTS
from .models_scada import ScadaDataPoint, ScadaPointValue, ScadaPointValueAnnotation

class LeituraScada(models.Model):
    # Identificador único do ciclo de escaneamento (ex: timestamp do início)
    scan_id = models.CharField(max_length=50, verbose_name="ID do Ciclo", db_index=True, null=True, blank=True)
    
    xid = models.CharField(max_length=100, verbose_name="Identificador do Ponto")
    eixo_x = models.FloatField(verbose_name="Largura (X)")
    eixo_y = models.FloatField(verbose_name="Espessura (Y)")
    data_leitura = models.DateTimeField(verbose_name="Data da Leitura")
    criado_em = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")

    class Meta:
        verbose_name = "Leitura Scada"
        verbose_name_plural = "Leituras Scada"
        ordering = ['data_leitura'] # Ordem crescente para desenhar o perfil corretamente

    def __str__(self):
        return f"Scan {self.scan_id} - {self.xid} - {self.data_leitura}"


class SystemConfiguration(models.Model):
    """
    Configurações operacionais e parâmetros de processo do Medidor de Espessura.
    Implementa o padrão Singleton (registro único editável via Django Admin).
    """

    DATA_SOURCE_CHOICES = (
        ("mysql", "MySQL Direto (Somente Leitura)"),
        ("rest_api", "API REST HTTP Legada (Fallback)"),
    )

    # ── Parâmetros de Aquisição Scada ──
    data_source = models.CharField(
        max_length=20,
        choices=DATA_SOURCE_CHOICES,
        default="rest_api",
        verbose_name="Origem de Aquisição",
        help_text="Define se a leitura é realizada diretamente no banco MySQL ou via API REST HTTP."
    )
    xid_gatilho = models.CharField(
        max_length=50,
        default="DP_887366",
        verbose_name="XID do Gatilho",
        help_text="Tag booleana que detecta presença de chapa (início/fim de varredura)."
    )
    xid_medicao = models.CharField(
        max_length=50,
        default="DP_747174",
        verbose_name="XID da Medição Dimensional",
        help_text="Tag alfanumérica/JSON com coordenadas de espessura [{'x': ..., 'y': ...}]."
    )
    aquisicao_ativa = models.BooleanField(
        default=True,
        verbose_name="Aquisição Ativa",
        help_text="Habilita ou pausa temporariamente a coleta de novos dados sem parar o servidor."
    )
    intervalo_consulta = models.FloatField(
        default=0.05,
        verbose_name="Intervalo de Consulta (s)",
        help_text="Tempo de espera entre polls (ex: 0.05 = 50ms, 0.20 = 200ms)."
    )
    limite_stale_segundos = models.IntegerField(
        default=30,
        verbose_name="Limite de Amostra Obsoleta (s)",
        help_text="Tempo máximo antes de classificar um dado como STALE."
    )
    delta_x_minimo = models.FloatField(
        default=0.1,
        verbose_name="Delta X Mínimo (mm)",
        help_text="Filtro de deslocamento espacial: descarta gravações repetidas quando o sensor estiver parado."
    )
    tamanho_lote_incremental = models.IntegerField(
        default=500,
        verbose_name="Tamanho de Lote Incremental",
        help_text="Quantidade máxima de medições obtidas por ciclo no modo MySQL."
    )

    # ── Parâmetros de Processo e Qualidade ──
    espessura_borda_nominal = models.FloatField(
        default=2.0,
        verbose_name="Espessura Nominal de Borda (mm)",
        help_text="Espessura de referência para a região de borda da chapa."
    )
    espessura_centro_nominal = models.FloatField(
        default=10.0,
        verbose_name="Espessura Nominal de Centro (mm)",
        help_text="Espessura de referência para a região central da chapa."
    )
    tolerancia_positiva = models.FloatField(
        default=0.5,
        verbose_name="Tolerância Superior (+) (mm)",
        help_text="Variação máxima aceitável acima do valor nominal (limite superior = nominal + tol)."
    )

    # ── Parâmetros de Análise Dimensional da Banda de Rodagem ──
    corte_min_espessura_banda = models.FloatField(
        default=1.0,
        verbose_name="Corte Mínimo de Espessura da Banda (mm)",
        help_text="Espessura mínima para descartar leituras fora da chapa ou retorno do carro em vazio."
    )
    limiar_coroa_relativo = models.FloatField(
        default=0.75,
        verbose_name="Limiar Relativo de Coroa (0 a 1)",
        help_text="Fator relativo de altura (0.1 a 0.99) para identificar o patamar da coroa acima das laterais."
    )
    min_delta_altura_coroa = models.FloatField(
        default=0.8,
        verbose_name="Degrau Mínimo da Coroa (mm)",
        help_text="Diferença mínima de altura entre topo e abas laterais para classificar coroa."
    )
    min_pontos_scan = models.IntegerField(
        default=15,
        verbose_name="Mínimo de Pontos para Ciclo Concluído",
        help_text="Quantidade mínima de pontos válidos para considerar a varredura como concluída."
    )
    modo_deteccao_coroa = models.CharField(
        max_length=10,
        choices=(("auto", "Automático"), ("manual", "Manual (Limites Fixos)")),
        default="auto",
        verbose_name="Modo de Detecção da Coroa",
        help_text="Detecção automática baseada na geometria ou limites manuais em X."
    )
    manual_x_inicio_coroa = models.FloatField(
        null=True,
        blank=True,
        verbose_name="X Início Coroa Manual (mm)",
        help_text="Coordenada X de início da coroa se modo for manual."
    )
    manual_x_fim_coroa = models.FloatField(
        null=True,
        blank=True,
        verbose_name="X Fim Coroa Manual (mm)",
        help_text="Coordenada X de fim da coroa se modo for manual."
    )

    atualizado_em = models.DateTimeField(auto_now=True, verbose_name="Última Atualização")

    class Meta:
        verbose_name = "Configuração do Sistema"
        verbose_name_plural = "Configuração do Sistema"

    def clean(self):
        errors = {}

        if not self.xid_gatilho or not self.xid_gatilho.strip():
            errors["xid_gatilho"] = "O XID do gatilho não pode ser vazio."

        if not self.xid_medicao or not self.xid_medicao.strip():
            errors["xid_medicao"] = "O XID da medição não pode ser vazio."

        if self.intervalo_consulta is None or self.intervalo_consulta <= 0:
            errors["intervalo_consulta"] = "O intervalo de consulta deve ser estritamente maior que zero."

        if self.delta_x_minimo is None or self.delta_x_minimo < 0:
            errors["delta_x_minimo"] = "O deslocamento espacial (Delta X) não pode ser negativo."

        if self.limite_stale_segundos is None or self.limite_stale_segundos <= 0:
            errors["limite_stale_segundos"] = "O limite de stale deve ser maior que zero."

        if self.tamanho_lote_incremental is None or self.tamanho_lote_incremental <= 0:
            errors["tamanho_lote_incremental"] = "O tamanho do lote deve ser maior que zero."

        if self.espessura_borda_nominal is None or self.espessura_borda_nominal <= 0:
            errors["espessura_borda_nominal"] = "A espessura de borda deve ser maior que zero."

        if self.espessura_centro_nominal is None or self.espessura_centro_nominal <= 0:
            errors["espessura_centro_nominal"] = "A espessura de centro deve ser maior que zero."

        if self.tolerancia_positiva is None or self.tolerancia_positiva < 0:
            errors["tolerancia_positiva"] = "A tolerância positiva não pode ser negativa."

        if self.corte_min_espessura_banda is None or self.corte_min_espessura_banda < 0:
            errors["corte_min_espessura_banda"] = "O corte mínimo de espessura não pode ser negativo."

        if self.limiar_coroa_relativo is None or not (0.1 <= self.limiar_coroa_relativo <= 0.99):
            errors["limiar_coroa_relativo"] = "O limiar relativo da coroa deve estar entre 0.1 e 0.99."

        if self.min_delta_altura_coroa is None or self.min_delta_altura_coroa <= 0:
            errors["min_delta_altura_coroa"] = "O degrau mínimo de coroa deve ser maior que zero."

        if self.min_pontos_scan is None or self.min_pontos_scan < 3:
            errors["min_pontos_scan"] = "O mínimo de pontos por scan deve ser de pelo menos 3."

        if self.modo_deteccao_coroa == "manual":
            if self.manual_x_inicio_coroa is not None and self.manual_x_fim_coroa is not None:
                if self.manual_x_inicio_coroa >= self.manual_x_fim_coroa:
                    errors["manual_x_inicio_coroa"] = "O X de início manual deve ser menor que o X de fim."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        # Força ID fixo para garantir Singleton
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Impede remoção da configuração singleton
        pass

    @classmethod
    def get_config(cls):
        """
        Retorna a configuração persistida ou inicializa defaults seguros em caso de banco inicial.
        """
        try:
            config, _ = cls.objects.get_or_create(
                pk=1,
                defaults={
                    "data_source": getattr(settings, "SCADA_DATA_SOURCE", "rest_api"),
                    "xid_gatilho": getattr(settings, "SCADA_XID_GATILHO", "DP_887366"),
                    "xid_medicao": getattr(settings, "SCADA_XID_MEDICAO", "DP_747174"),
                    "aquisicao_ativa": True,
                    "intervalo_consulta": 0.05,
                    "limite_stale_segundos": 30,
                    "delta_x_minimo": 0.1,
                    "tamanho_lote_incremental": 500,
                    "espessura_borda_nominal": 2.0,
                    "espessura_centro_nominal": 10.0,
                    "tolerancia_positiva": 0.5,
                    "corte_min_espessura_banda": 1.0,
                    "limiar_coroa_relativo": 0.75,
                    "min_delta_altura_coroa": 0.8,
                    "min_pontos_scan": 15,
                    "modo_deteccao_coroa": "auto",
                    "manual_x_inicio_coroa": None,
                    "manual_x_fim_coroa": None,
                }
            )
            return config
        except Exception:
            # Fallback seguro para instâncias efêmeras ou bancos não migrados
            return cls(
                data_source=getattr(settings, "SCADA_DATA_SOURCE", "rest_api"),
                xid_gatilho=getattr(settings, "SCADA_XID_GATILHO", "DP_887366"),
                xid_medicao=getattr(settings, "SCADA_XID_MEDICAO", "DP_747174"),
                aquisicao_ativa=True,
                intervalo_consulta=0.05,
                limite_stale_segundos=30,
                delta_x_minimo=0.1,
                tamanho_lote_incremental=500,
                espessura_borda_nominal=2.0,
                espessura_centro_nominal=10.0,
                tolerancia_positiva=0.5,
                corte_min_espessura_banda=1.0,
                limiar_coroa_relativo=0.75,
                min_delta_altura_coroa=0.8,
                min_pontos_scan=15,
                modo_deteccao_coroa="auto",
                manual_x_inicio_coroa=None,
                manual_x_fim_coroa=None,
            )

    def __str__(self):
        return f"Configuração Geral (Origem: {self.get_data_source_display()})"


class ResultadoDimensionalScan(models.Model):
    """
    Persistência dos cálculos e cotas dimensionais calculadas ao encerramento de cada varredura.
    Vinculado univocamente ao scan_id com integridade e reprodutibilidade metrológica.
    """
    STATUS_CHOICES = (
        ("CONCLUIDO", "Concluído"),
        ("INCOMPLETO", "Incompleto"),
        ("DADOS_INSUFICIENTES", "Dados Insuficientes"),
        ("SEM_COROA_CLARA", "Sem Coroa Identificável"),
        ("EM_ANDAMENTO", "Em Andamento"),
    )

    scan_id = models.CharField(max_length=50, unique=True, db_index=True, verbose_name="ID do Ciclo")
    largura_total = models.FloatField(null=True, blank=True, verbose_name="Largura Total (mm)")
    largura_coroa = models.FloatField(null=True, blank=True, verbose_name="Largura da Coroa (mm)")
    espessura_media_coroa = models.FloatField(null=True, blank=True, verbose_name="Espessura Média da Coroa (mm)")
    espessura_min_coroa = models.FloatField(null=True, blank=True, verbose_name="Espessura Mínima da Coroa (mm)")
    espessura_max_coroa = models.FloatField(null=True, blank=True, verbose_name="Espessura Máxima da Coroa (mm)")
    pontos_coroa = models.IntegerField(default=0, verbose_name="Pontos na Coroa")
    media_aritmetica_coroa = models.FloatField(null=True, blank=True, verbose_name="Média Aritmética da Coroa (mm)")
    espessura_lateral_esq = models.FloatField(null=True, blank=True, verbose_name="Espessura Lateral Esquerda (mm)")
    espessura_lateral_dir = models.FloatField(null=True, blank=True, verbose_name="Espessura Lateral Direita (mm)")
    espessura_lateral_media = models.FloatField(null=True, blank=True, verbose_name="Espessura Lateral Média (mm)")
    x_esquerda = models.FloatField(null=True, blank=True, verbose_name="X Extremidade Esquerda (mm)")
    x_direita = models.FloatField(null=True, blank=True, verbose_name="X Extremidade Direita (mm)")
    x_inicio_coroa = models.FloatField(null=True, blank=True, verbose_name="X Início da Coroa (mm)")
    x_fim_coroa = models.FloatField(null=True, blank=True, verbose_name="X Fim da Coroa (mm)")
    total_pontos = models.IntegerField(default=0, verbose_name="Total de Pontos no Ciclo")
    status_leitura = models.CharField(max_length=30, choices=STATUS_CHOICES, default="CONCLUIDO", verbose_name="Situação da Leitura")
    motivo_status = models.TextField(blank=True, default="", verbose_name="Motivo / Observações")
    parametros_calculo = models.JSONField(default=dict, blank=True, verbose_name="Parâmetros Utilizados")
    calculado_em = models.DateTimeField(auto_now=True, verbose_name="Calculado em")

    class Meta:
        verbose_name = "Resultado Dimensional do Scan"
        verbose_name_plural = "Resultados Dimensionais dos Scans"
        ordering = ['-scan_id']

    def __str__(self):
        return f"Scan {self.scan_id} - {self.status_leitura} (Larg: {self.largura_total}mm, EspCoroa: {self.espessura_media_coroa}mm)"
