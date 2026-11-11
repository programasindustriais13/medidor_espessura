"""Vincula o nome preparado pelo operador ao próximo ciclo iniciado."""

from django.db import transaction

from .models import ResultadoDimensionalScan, SystemConfiguration


def vincular_material_preparado(scan_id):
    with transaction.atomic():
        config = SystemConfiguration.objects.select_for_update().get(pk=1)
        nome = config.nome_material_proximo_ciclo.strip()
        if nome:
            ResultadoDimensionalScan.objects.update_or_create(
                scan_id=scan_id,
                defaults={"nome_material": nome, "status_leitura": "EM_ANDAMENTO"},
            )
            SystemConfiguration.objects.filter(pk=config.pk).update(nome_material_proximo_ciclo="")
