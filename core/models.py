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
            )

    def __str__(self):
        return f"Configuração Geral (Origem: {self.get_data_source_display()})"

