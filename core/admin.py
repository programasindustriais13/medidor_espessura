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
    CalibracaoEspessura,
    PontoCalibracao,
)
from .calibration_service import CalibrationService
from .scada_repository import ScadaRepository

logger = logging.getLogger(__name__)


class PontoCalibracaoInline(admin.TabularInline):
    """
    Tabela inline para cadastro de pontos físicos de calibração.
    Permite registrar múltiplas leituras separadas por vírgula ou espaço
    e calcula automaticamente as médias e a diferença.
    """
    model = PontoCalibracao
    extra = 1
    fields = (
        "posicao_x",
        "leituras_zero_input",
        "leituras_bloco_input",
        "format_zero_medio",
        "format_bloco_medio",
        "format_diferenca",
    )
    readonly_fields = (
        "format_zero_medio",
        "format_bloco_medio",
        "format_diferenca",
    )

    def format_zero_medio(self, obj):
        return f"{obj.zero_medio:.2f}" if obj.zero_medio is not None else "--"
    format_zero_medio.short_description = "Zero Médio"

    def format_bloco_medio(self, obj):
        return f"{obj.bloco_medio:.2f}" if obj.bloco_medio is not None else "--"
    format_bloco_medio.short_description = "Bloco Médio"

    def format_diferenca(self, obj):
        if obj.diferenca is not None:
            cor = "#16a34a" if obj.diferenca > 0 else "#dc2626"
            return format_html(f'<strong style="color: {cor};">{obj.diferenca:.2f}</strong>')
        return "--"
    format_diferenca.short_description = "Diferença (Δ)"


@admin.register(CalibracaoEspessura)
class CalibracaoEspessuraAdmin(admin.ModelAdmin):
    """
    Interface administrativa para gerenciamento das calibrações multiponto.
    Garante controle transacional de ativação, validação de regras físicas
    e histórico completo de calibrações.
    """
    list_display = (
        "id",
        "descricao",
        "status_badge",
        "espessura_bloco_padrao_mm",
        "format_fator_conversao",
        "format_faixa_calibrada",
        "pontos_count",
        "responsavel",
        "data_calibracao",
        "ativar_link",
    )
    list_filter = ("ativa", "data_calibracao")
    search_fields = ("descricao", "responsavel", "observacoes")
    ordering = ("-ativa", "-data_calibracao")
    inlines = [PontoCalibracaoInline]
    actions = ["acao_ativar_calibracao"]

    fieldsets = (
        (
            "Identificação da Calibração",
            {
                "fields": (
                    "descricao",
                    "data_calibracao",
                    "responsavel",
                    "espessura_bloco_padrao_mm",
                    "ativa",
                ),
                "description": (
                    "Cadastre a calibração com a espessura conhecida do bloco padrão (nominal 30,00 mm). "
                    "Apenas uma calibração pode estar ativa no sistema."
                ),
            },
        ),
        (
            "Resultados Metrológicos Calculados",
            {
                "fields": (
                    "fator_conversao",
                    "faixa_calibrada_display",
                    "validade_display",
                ),
                "description": "Valores derivados automaticamente a partir dos pontos de referência cadastrados abaixo.",
            },
        ),
        (
            "Observações e Auditoria",
            {
                "fields": ("observacoes", "criado_em", "atualizado_em"),
            },
        ),
    )

    readonly_fields = (
        "fator_conversao",
        "faixa_calibrada_display",
        "validade_display",
        "criado_em",
        "atualizado_em",
    )

    def status_badge(self, obj):
        if obj.ativa:
            return format_html(
                '<span style="background-color: #16a34a; color: white; padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 0.8rem;">● ATIVA</span>'
            )
        return format_html(
            '<span style="background-color: #64748b; color: white; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem;">Inativa</span>'
        )
    status_badge.short_description = "Status"

    def format_fator_conversao(self, obj):
        if obj.fator_conversao:
            return f"{obj.fator_conversao:.8f} mm/un"
        return "--"
    format_fator_conversao.short_description = "Fator de Conversão"

    def format_faixa_calibrada(self, obj):
        faixa = obj.get_faixa_calibrada()
        if faixa:
            return f"{faixa[0]:.1f} a {faixa[1]:.1f} mm"
        return "Sem pontos sufic."
    format_faixa_calibrada.short_description = "Faixa Calibrada (X)"

    def faixa_calibrada_display(self, obj):
        faixa = obj.get_faixa_calibrada()
        if faixa:
            return format_html(
                f'<strong style="color: #0284c7; font-size: 1.05rem;">{faixa[0]:.1f} mm a {faixa[1]:.1f} mm</strong> '
                f'(Extrapolações fora desta faixa física serão rejeitadas)'
            )
        return "Cadastre pelo menos 2 pontos para definir a faixa calibrada."
    faixa_calibrada_display.short_description = "Faixa Física Calibrada"

    def validade_display(self, obj):
        valida, msg = obj.is_valid_for_activation()
        cor = "#16a34a" if valida else "#dc2626"
        icon = "✅" if valida else "⚠️"
        return format_html(f'<span style="color: {cor}; font-weight: bold;">{icon} {msg}</span>')
    validade_display.short_description = "Diagnóstico de Validade"

    def pontos_count(self, obj):
        return obj.pontos.count()
    pontos_count.short_description = "Pontos"

    def ativar_link(self, obj):
        if obj.ativa:
            return format_html('<span style="color: #16a34a; font-weight: bold;">Já Ativa</span>')
        return format_html(
            '<a class="button" href="ativar/{}/" style="background: #16a34a; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; text-decoration: none; font-weight: bold;">⚡ Ativar</a>',
            obj.pk,
        )
    ativar_link.short_description = "Ação"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "ativar/<int:calibracao_id>/",
                self.admin_site.admin_view(self.ativar_calibracao_view),
                name="core_calibracao_ativar",
            ),
        ]
        return custom_urls + urls

    def ativar_calibracao_view(self, request, calibracao_id):
        from django.contrib import messages
        from django.http import HttpResponseRedirect
        try:
            calib = CalibrationService.ativar_calibracao(calibracao_id)
            messages.success(request, f"Calibração '{calib.descricao}' ativada com sucesso.")
        except Exception as e:
            messages.error(request, f"Falha ao ativar calibração: {e}")
        return HttpResponseRedirect("../../")

    def acao_ativar_calibracao(self, request, queryset):
        from django.contrib import messages
        if queryset.count() != 1:
            messages.error(request, "Selecione exatamente uma calibração para ativar.")
            return

        calib = queryset.first()
        try:
            CalibrationService.ativar_calibracao(calib.pk)
            messages.success(request, f"Calibração '{calib.descricao}' ativada com sucesso.")
        except Exception as e:
            messages.error(request, f"Falha ao ativar calibração: {e}")
    acao_ativar_calibracao.short_description = "⚡ Ativar Calibração Selecionada"


@admin.register(LeituraScada)
class LeituraScadaAdmin(admin.ModelAdmin):
    """
    Administração e auditoria das medições reais gravadas pela instrumentação.
    Campos estritamente protegidos como somente-leitura para garantir integridade metrológica.
    """
    list_display = (
        "scan_id",
        "status_badge",
        "format_eixo_x",
        "format_eixo_y",
        "format_valor_bruto",
        "format_zero_interpolado",
        "calibracao_link",
        "data_leitura",
    )
    list_filter = ("status_validade", "scan_id", "data_leitura", "calibracao")
    search_fields = ("scan_id", "xid")
    date_hierarchy = "data_leitura"
    ordering = ("-data_leitura",)
    list_per_page = 50

    readonly_fields = (
        "scan_id",
        "xid",
        "eixo_x",
        "eixo_y",
        "valor_bruto",
        "posicao_bruta",
        "zero_interpolado",
        "fator_conversao",
        "calibracao",
        "status_validade",
        "data_leitura",
        "criado_em",
    )

    def status_badge(self, obj):
        cores = {
            "VALIDO": "#16a34a",
            "FORA_DA_FAIXA": "#d97706",
            "SEM_CALIBRACAO": "#dc2626",
            "AMOSTRA_INCOERENTE": "#9333ea",
            "ERRO_LEITURA": "#dc2626",
        }
        cor = cores.get(obj.status_validade, "#64748b")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 2px 7px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">{}</span>',
            cor,
            obj.get_status_validade_display(),
        )
    status_badge.short_description = "Validade"

    def format_eixo_x(self, obj):
        return f"{obj.eixo_x:.2f} mm"
    format_eixo_x.short_description = "Posição X"

    def format_eixo_y(self, obj):
        if obj.eixo_y is not None:
            return f"{obj.eixo_y:.3f} mm"
        return format_html('<span style="color: #94a3b8; font-style: italic;">N/A</span>')
    format_eixo_y.short_description = "Espessura Y"

    def format_valor_bruto(self, obj):
        return f"{obj.valor_bruto:.1f}" if obj.valor_bruto is not None else "--"
    format_valor_bruto.short_description = "Bruto"

    def format_zero_interpolado(self, obj):
        return f"{obj.zero_interpolado:.2f}" if obj.zero_interpolado is not None else "--"
    format_zero_interpolado.short_description = "Zero (Interp.)"

    def calibracao_link(self, obj):
        if obj.calibracao:
            return f"Calib #{obj.calibracao_id}"
        return "--"
    calibracao_link.short_description = "Calibração"

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
                    "modo_aquisicao",
                    "xid_gatilho",
                    "xid_posicao",
                    "xid_bruto_espessura",
                    "xid_medicao",
                    "tolerancia_temporal_ms",
                    "aquisicao_ativa",
                    "intervalo_consulta",
                    "limite_stale_segundos",
                    "delta_x_minimo",
                    "tamanho_lote_incremental",
                ),
                "description": (
                    "Configurações do protocolo e comportamento do coletor industrial. "
                    "O modo 'calibrado' utiliza DP_632601 (bruto) e DP_648291 (posição X) com a calibração multiponto ativa."
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
