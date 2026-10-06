import logging
from django.contrib import admin
from django.db import OperationalError
from django.shortcuts import render
from django.urls import path
from django.utils.html import format_html

from .models import (
    LeituraScada,
    SystemConfiguration,
    ScadaDataPoint,
    ScadaPointValue,
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
        # Medições são geradas exclusivamente pela camada de aquisição industrial
        return False

    def has_delete_permission(self, request, obj=None):
        # Apenas superusuários podem expurgar medições se estritamente necessário
        return request.user.is_superuser


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    """
    Área operacional para parametrização do coletor Scada e regras de qualidade industrial.
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
        # Permite adicionar apenas se não existir nenhum registro (Singleton)
        return not SystemConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Impede exclusão da configuração central
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
            # Apresenta mensagem amigável caso o banco MySQL do Scada esteja inacessível na rede
            logger.warning(f"[ADMIN] Scada MySQL offline ao abrir ScadaDataPoint: {oe}")
            from django.contrib import messages
            messages.error(
                request,
                f"Banco MySQL do Scada-LTS inacessível no momento ({oe}). "
                "Verifique a conectividade de rede com o servidor Scada."
            )
            return render(request, "admin/core/scada_offline.html", {"error": str(oe)})


@admin.register(ScadaPointValue)
class ScadaPointValueAdmin(admin.ModelAdmin):
    """
    Visualização somente-leitura do histórico bruto no Scada-LTS.
    Bloqueio absoluto de escrita e consulta com limite para preservar desempenho.
    """
    list_display = ("id", "data_point_id", "data_type", "point_value", "ts")
    search_fields = ("data_point_id",)
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
            logger.warning(f"[ADMIN] Scada MySQL offline ao abrir ScadaPointValue: {oe}")
            from django.contrib import messages
            messages.error(
                request,
                f"Banco MySQL do Scada-LTS inacessível no momento ({oe})."
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
