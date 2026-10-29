import logging
from django.contrib import admin
from django.db import OperationalError
from django.shortcuts import render
from django.urls import path
from django.utils.html import format_html

from .models import (
    LeituraScada,
    SystemConfiguration,
    ResultadoDimensionalScan,
    ScadaDataPoint,
)
from .scada_repository import ScadaRepository

logger = logging.getLogger(__name__)


@admin.register(LeituraScada)
class LeituraScadaAdmin(admin.ModelAdmin):
    """
    Administração e auditoria das medições reais gravadas pela instrumentação.
    Campos estritamente protegidos como somente-leitura para garantir integridade metrológica.
    """
    list_display = (
        "scan_id",
        "xid",
        "format_eixo_x",
        "format_eixo_y",
        "data_leitura",
        "criado_em",
    )
    list_filter = ("scan_id", "data_leitura")
    search_fields = ("scan_id", "xid")
    date_hierarchy = "data_leitura"
    ordering = ("-data_leitura",)
    list_per_page = 50

    readonly_fields = (
        "scan_id",
        "xid",
        "eixo_x",
        "eixo_y",
        "data_leitura",
        "criado_em",
    )

    def format_eixo_x(self, obj):
        return f"{obj.eixo_x:.2f} mm"
    format_eixo_x.short_description = "Posição X"

    def format_eixo_y(self, obj):
        return f"{obj.eixo_y:.3f} mm"
    format_eixo_y.short_description = "Espessura Y"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(ResultadoDimensionalScan)
class ResultadoDimensionalScanAdmin(admin.ModelAdmin):
    """
    Auditoria e consulta das cotas dimensionais calculadas para cada ciclo de varredura.
    Com destaque para a ESPESSURA MÉDIA DA COROA (mm), larguras e abas laterais.
    """
    list_display = (
        "scan_id",
        "status_badge",
        "format_largura_total",
        "format_largura_coroa",
        "format_espessura_media_coroa",
        "format_laterais",
        "total_pontos",
        "calculado_em",
    )
    list_filter = ("status_leitura", "calculado_em")
    search_fields = ("scan_id", "motivo_status")
    ordering = ("-scan_id",)
    list_per_page = 40

    readonly_fields = (
        "scan_id",
        "status_leitura",
        "motivo_status",
        "largura_total",
        "largura_coroa",
        "espessura_media_coroa",
        "espessura_min_coroa",
        "espessura_max_coroa",
        "pontos_coroa",
        "media_aritmetica_coroa",
        "espessura_lateral_esq",
        "espessura_lateral_dir",
        "espessura_lateral_media",
        "x_esquerda",
        "x_direita",
        "x_inicio_coroa",
        "x_fim_coroa",
        "total_pontos",
        "parametros_calculo",
        "calculado_em",
    )

    def status_badge(self, obj):
        cores = {
            "CONCLUIDO": "#16a34a",
            "INCOMPLETO": "#dc2626",
            "DADOS_INSUFICIENTES": "#d97706",
            "SEM_COROA_CLARA": "#9333ea",
            "EM_ANDAMENTO": "#0284c7",
        }
        cor = cores.get(obj.status_leitura, "#64748b")
        return format_html(
            f'<span style="background-color: {cor}; color: white; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem;">{obj.get_status_leitura_display()}</span>'
        )
    status_badge.short_description = "Situação"

    def format_largura_total(self, obj):
        return f"{obj.largura_total:.2f} mm" if obj.largura_total is not None else "--"
    format_largura_total.short_description = "Largura Total"

    def format_largura_coroa(self, obj):
        return f"{obj.largura_coroa:.2f} mm" if obj.largura_coroa is not None else "--"
    format_largura_coroa.short_description = "Largura Coroa"

    def format_espessura_media_coroa(self, obj):
        if obj.espessura_media_coroa is not None:
            return format_html(
                f'<strong style="color: #22c55e; font-size: 0.95rem;">{obj.espessura_media_coroa:.2f} mm</strong> '
                f'<span style="color: #94a3b8; font-size: 0.8rem;">({obj.pontos_coroa} pts)</span>'
            )
        return "--"
    format_espessura_media_coroa.short_description = "Esp. Média Coroa"

    def format_laterais(self, obj):
        esq = f"{obj.espessura_lateral_esq:.2f}" if obj.espessura_lateral_esq is not None else "--"
        dir_ = f"{obj.espessura_lateral_dir:.2f}" if obj.espessura_lateral_dir is not None else "--"
        return f"Esq: {esq} mm | Dir: {dir_} mm"
    format_laterais.short_description = "Laterais (Esq / Dir)"

    def has_add_permission(self, request):
        return False


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    """
    Área operacional para parametrização do coletor Scada, qualidade e análise geométrica.
    Padrão Singleton: impede exclusão e impede criação de registros duplicados.
    """
    fieldsets = (
        (
            "Origem e Parâmetros de Aquisição Scada",
            {
                "fields": (
                    "data_source",
                    "xid_gatilho",
                    "xid_medicao",
                    "aquisicao_ativa",
                    "intervalo_consulta",
                    "limite_stale_segundos",
                    "delta_x_minimo",
                    "tamanho_lote_incremental",
                ),
                "description": (
                    "Configurações do protocolo e comportamento do coletor. "
                    "A alteração da origem (MySQL ou REST) aplica-se em tempo real sem interrupção."
                ),
            },
        ),
        (
            "Parâmetros de Processo e Classificação de Qualidade",
            {
                "fields": (
                    "espessura_borda_nominal",
                    "espessura_centro_nominal",
                    "tolerancia_positiva",
                ),
                "description": (
                    "Valores nominais e janela de tolerância (+) utilizados pelo algoritmo ProfileLogic "
                    "para validação dimensional do perfil de borracha."
                ),
            },
        ),
        (
            "Parâmetros de Análise Dimensional da Banda de Rodagem (Cálculo Automático)",
            {
                "fields": (
                    "corte_min_espessura_banda",
                    "limiar_coroa_relativo",
                    "min_delta_altura_coroa",
                    "min_pontos_scan",
                    "modo_deteccao_coroa",
                    "manual_x_inicio_coroa",
                    "manual_x_fim_coroa",
                ),
                "description": (
                    "Parâmetros de detecção geométrica da coroa, descarte de carro em vazio "
                    "e cálculo da ESPESSURA MÉDIA DA COROA (mm) ao término de cada varredura."
                ),
            },
        ),
        (
            "Informações de Auditoria e Diagnóstico",
            {
                "fields": ("atualizado_em", "link_diagnostico"),
            },
        ),
    )

    readonly_fields = ("atualizado_em", "link_diagnostico")

    def link_diagnostico(self, obj):
        return format_html(
            '<a class="button" href="/admin/scada-diagnostico/" style="background: #0284c7; color: white; padding: 6px 12px; border-radius: 4px; text-decoration: none; font-weight: bold;">🔍 Abrir Ferramenta de Diagnóstico de Tags</a>'
        )
    link_diagnostico.short_description = "Diagnóstico Scada"

    def has_add_permission(self, request):
        return not SystemConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScadaDataPoint)
class ScadaDataPointAdmin(admin.ModelAdmin):
    """
    Visualização somente-leitura dos pontos cadastrados no Scada-LTS.
    Bloqueio total de escritas, migrações e ações destrutivas.
    """
    list_display = ("xid", "point_name", "data_source_id", "plc_alarm_level")
    search_fields = ("xid", "point_name")
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        try:
            return super().changelist_view(request, extra_context)
        except OperationalError as oe:
            logger.warning(f"[ADMIN] Scada MySQL offline ao abrir ScadaDataPoint: {oe}")
            from django.contrib import messages
            messages.error(
                request,
                f"Banco MySQL do Scada-LTS inacessível no momento ({oe}). "
                "Verifique a conectividade de rede com o servidor Scada."
            )
            return render(request, "admin/core/scada_offline.html", {"error": str(oe)})


# ── Ferramenta Administrativa de Diagnóstico Scada ─────────────────────────

def scada_diagnostico_view(request):
    """
    View administrativa personalizada para testar XIDs e exibir metadados/valores
    sem qualquer risco de escrita no Scada.
    """
    config = SystemConfiguration.get_config()
    xid_param = request.GET.get("xid", "").strip()
    diag = None

    if xid_param:
        repo = ScadaRepository()
        diag = repo.diagnosticar_xid(xid_param, stale_limit_seconds=config.limite_stale_segundos)

    context = {
        **admin.site.each_context(request),
        "title": "Diagnóstico de Tags do Scada-LTS",
        "config": config,
        "xid": xid_param,
        "diag": diag,
    }
    return render(request, "admin/core/scada_diagnostico.html", context)


# Injeção da URL de diagnóstico no Django Admin
original_get_urls = admin.site.get_urls

def get_admin_urls():
    custom_urls = [
        path("scada-diagnostico/", admin.site.admin_view(scada_diagnostico_view), name="scada_diagnostico"),
    ]
    return custom_urls + original_get_urls()

admin.site.get_urls = get_admin_urls
