from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Lê dados do Scada-LTS (Comando legado mantido para compatibilidade; encaminha para collect_scada)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            type=str,
            choices=["mysql", "rest_api"],
            help="Origem de leitura (mysql ou rest_api)",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Executa uma única iteração para diagnóstico",
        )

    def handle(self, *args, **options):
        # Encaminha diretamente para o comando canônico collect_scada
        call_command("collect_scada", *args, **options)
