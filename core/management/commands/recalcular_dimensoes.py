"""Recalcula o resumo de um ciclo escolhido, sem alterar as leituras Scada."""

from django.core.management.base import BaseCommand, CommandError

from core.dimensional_analysis import DimensionalAnalyzer
from core.models import LeituraScada, SystemConfiguration


class Command(BaseCommand):
    help = "Recalcula somente o resumo dimensional de um scan_id existente."

    def add_arguments(self, parser):
        parser.add_argument("scan_id", help="Identificador exato do ciclo a recalcular")

    def handle(self, *args, **options):
        scan_id = options["scan_id"]
        if not LeituraScada.objects.filter(scan_id=scan_id).exists():
            raise CommandError(f"Nenhuma leitura encontrada para {scan_id}.")

        resultado = DimensionalAnalyzer.calcular_e_persistir_scan(
            scan_id, config=SystemConfiguration.get_config()
        )
        self.stdout.write(self.style.SUCCESS(
            f"{scan_id}: {resultado.status_leitura}; "
            f"coroa={resultado.espessura_media_coroa} mm; "
            f"esquerda={resultado.espessura_lateral_esq} mm; "
            f"direita={resultado.espessura_lateral_dir} mm."
        ))
