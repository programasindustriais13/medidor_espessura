import os
from django.apps import AppConfig

class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Apenas iniciar o worker se formos o processo principal do runserver (ou prod)
        # Evita duplicidade com o auto-reloader do Django
        if os.environ.get('RUN_MAIN') == 'true':
            from .scada_worker import ScadaWorker
            
            # Verifica se já não existe thread rodando (para segurança em reloads)
            # Nota: Em reload total o processo morre, mas precaução nunca é demais.
            
            worker = ScadaWorker()
            worker.start()
