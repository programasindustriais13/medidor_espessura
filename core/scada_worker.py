import logging
import threading
import time
from .scada_service import CrossProcessLock, ScadaAcquisitionService

logger = logging.getLogger(__name__)


class ScadaWorker(threading.Thread):
    """
    Worker em thread daemon compatível com execução em background.
    Delega o ciclo de aquisição para ScadaAcquisitionService e respeita CrossProcessLock.
    """

    def __init__(self, override_source=None):
        super().__init__()
        self.daemon = True
        self._stop_event = threading.Event()
        self.service = ScadaAcquisitionService(override_source=override_source)
        self.lock = CrossProcessLock()

    def stop(self):
        self._stop_event.set()
        self.service.request_stop()

    def run(self):
        if not self.lock.acquire():
            logger.warning("[SCADA WORKER] Não foi possível adquirir trava de processo. Outro coletor já ativo.")
            return

        logger.info("[SCADA WORKER] Coletor em thread iniciado com sucesso.")
        try:
            while not self._stop_event.is_set():
                config = self.service.get_effective_config()
                loop_start = time.time()

                self.service.process_cycle()

                from django.db import connections
                connections.close_all()

                elapsed = time.time() - loop_start
                sleep_time = max(0.005, config.intervalo_consulta - elapsed)
                time.sleep(sleep_time)

        except Exception as e:
            logger.error(f"[SCADA WORKER] Erro no loop do worker: {e}")
        finally:
            self.lock.release()
            logger.info("[SCADA WORKER] Worker encerrado e trava liberada.")
