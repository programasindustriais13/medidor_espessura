import os
import logging
from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)

class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Apenas dispara worker em segundo plano se expressamente configurado via SCADA_AUTO_WORKER
        # O modo canônico recomendado para ambientes industriais é o processo dedicado via CLI
        auto_worker_enabled = getattr(settings, "SCADA_AUTO_WORKER", False)
        if auto_worker_enabled and os.environ.get('RUN_MAIN') == 'true':
            try:
                from .scada_worker import ScadaWorker
                worker = ScadaWorker()
                worker.start()
                logger.info("[APPS] ScadaWorker em background inicializado com sucesso.")
            except Exception as e:
                logger.warning(f"[APPS] Não foi possível iniciar o worker em background: {e}")

