import signal
import sys
import time
from django.core.management.base import BaseCommand
from core.scada_service import CrossProcessLock, ScadaAcquisitionService


class Command(BaseCommand):
    help = "Coletor industrial canônico de medições do Scada-LTS (suporta MySQL direto ou REST API)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            type=str,
            choices=["mysql", "rest_api"],
            help="Sobrescreve temporariamente a origem de aquisição (mysql ou rest_api)",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Executa uma única iteração do ciclo para teste e diagnóstico",
        )

    def handle(self, *args, **options):
        source_override = options.get("source")
        run_once = options.get("once", False)

        service = ScadaAcquisitionService(override_source=source_override)
        config = service.get_effective_config()

        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  COLETOR INDUSTRIAL CANÔNICO - MEDIDOR DE ESPESSURA"))
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(f"Origem Efetiva: {config.get_data_source_display()} ({config.data_source})")
        self.stdout.write(f"Gatilho (Start/Stop): {config.xid_gatilho}")
        self.stdout.write(f"Medição Dimensional: {config.xid_medicao}")
        self.stdout.write(f"Frequência Polling: {config.intervalo_consulta * 1000:.0f} ms")
        self.stdout.write(f"Sensibilidade Delta X: {config.delta_x_minimo} mm")
        self.stdout.write(self.style.SUCCESS("-" * 70))

        lock = CrossProcessLock()
        if not lock.acquire():
            self.stdout.write(
                self.style.ERROR(
                    "[BLOQUEIO DE SEGURANÇA] Outra instância do coletor já está ativa.\n"
                    "Apenas uma instância de coleta pode operar por vez para evitar dados duplicados."
                )
            )
            return

        def handle_termination(signum, frame):
            self.stdout.write(self.style.WARNING("\nSinal de encerramento recebido. Liberando trava e conexões..."))
            service.request_stop()
            lock.release()
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_termination)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handle_termination)

        if run_once:
            self.stdout.write("Executando ciclo único de teste (--once)...")
            res = service.process_cycle()
            self.stdout.write(f"Resultado do ciclo: {res}")
            lock.release()
            return

        self.stdout.write(self.style.NOTICE("Aguardando varredura... Pressione Ctrl+C para encerrar.\n"))

        last_status = None
        last_scan = None

        def progress_callback(res):
            nonlocal last_status, last_scan
            status = res.get("status")
            scan = res.get("scan_id")
            saved = res.get("saved", 0)

            if status != last_status or scan != last_scan:
                if status == "SCANNING":
                    self.stdout.write(self.style.SUCCESS(f"\n>>> [CICLO INICIADO] {scan} <<<"))
                elif status == "IDLE" and last_status == "SCANNING":
                    self.stdout.write(self.style.NOTICE(f"\n>>> [CICLO FINALIZADO] {last_scan} <<<"))
                elif status == "NO_COMMUNICATION":
                    self.stdout.write(self.style.WARNING(f"\r[ALERTA] Sem comunicação com o Scada-LTS ({config.data_source})..."), ending="")
                last_status = status
                last_scan = scan

            if status == "SCANNING" and saved > 0:
                self.stdout.write(f"\r[{scan}] Gravados +{saved} ponto(s) | Posição X={service.last_valid_x:.2f}mm", ending="")
                sys.stdout.flush()

        try:
            service.run_forever(callback_progress=progress_callback)
        finally:
            lock.release()
            self.stdout.write(self.style.SUCCESS("\nColetor finalizado com sucesso."))
