import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.conf import settings
from django.db import OperationalError, connections
from django.utils import timezone

from .models import LeituraScada, SystemConfiguration
from .scada_repository import ScadaRepository

logger = logging.getLogger(__name__)


class CrossProcessLock:
    """
    Trava exclusiva cross-process para garantir apenas uma instância do coletor em execução.
    Compatível com Windows (msvcrt) e Linux (fcntl).
    """

    def __init__(self, lock_file_path: Optional[Path] = None):
        if lock_file_path is None:
            base_dir = getattr(settings, "BASE_DIR", Path.cwd())
            lock_file_path = base_dir / "scada_collector.lock"
        self.lock_file_path = Path(lock_file_path)
        self.file_handle = None
        self.locked = False

    def acquire(self) -> bool:
        try:
            self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_handle = open(self.lock_file_path, "a+")

            if os.name == "nt":
                import msvcrt
                # Tenta travar exclusivamente o primeiro byte sem bloquear (LK_NBLCK)
                self.file_handle.seek(0)
                msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Grava PID do processo atual
            self.file_handle.seek(0)
            self.file_handle.truncate()
            self.file_handle.write(f"{os.getpid()}\n")
            self.file_handle.flush()
            self.locked = True
            return True

        except (BlockingIOError, PermissionError, OSError) as e:
            logger.warning(f"[CROSS PROCESS LOCK] Outra instância do coletor já está em execução: {e}")
            if self.file_handle:
                try:
                    self.file_handle.close()
                except Exception:
                    pass
                self.file_handle = None
            return False

    def release(self):
        if not self.locked or not self.file_handle:
            return

        try:
            if os.name == "nt":
                import msvcrt
                self.file_handle.seek(0)
                msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.debug(f"[CROSS PROCESS LOCK] Erro ao destravar: {e}")

        try:
            self.file_handle.close()
        except Exception:
            pass

        try:
            if self.lock_file_path.exists():
                self.lock_file_path.unlink()
        except Exception:
            pass

        self.file_handle = None
        self.locked = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Não foi possível adquirir a trava exclusiva do coletor (instância duplicada).")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class ScadaAcquisitionService:
    """
    Motor canônico e unificado de aquisição e reconstrução de ciclos de varredura (Scans).
    Suporta alternância transparente entre 'mysql' e 'rest_api' via SystemConfiguration ou settings.
    """

    def __init__(self, override_source: Optional[str] = None):
        self.override_source = override_source
        self.repo = ScadaRepository()
        self._stop_requested = False

        # Estado da Máquina de Scan
        self.is_scanning = False
        self.current_scan_id: Optional[str] = None
        self.last_valid_x: float = -999.0

        # Cursor para Leitura Incremental MySQL
        self.last_reading_id: Optional[int] = None
        self.last_reading_ts: Optional[int] = None

        # Sessão HTTP REST para Fallback / Origem REST
        self.http_session = requests.Session()
        self.http_session.headers.update({
            "User-Agent": "MonitorEspessura/CanonicalWorker",
        })

    def request_stop(self):
        """Solicita a interrupção segura do loop."""
        self._stop_requested = True

    def get_effective_config(self) -> SystemConfiguration:
        """Obtém a configuração do sistema do banco ou defaults."""
        config = SystemConfiguration.get_config()
        if self.override_source:
            config.data_source = self.override_source
        return config

    def _ensure_rest_login(self, base_url: str, user: str, pswd: str) -> bool:
        """Autentica na API REST do Scada-LTS sob demanda."""
        login_url = f"{base_url}/login.htm"
        try:
            self.http_session.get(login_url, timeout=4)
            resp = self.http_session.post(
                login_url,
                data={"username": user, "password": pswd, "submit": "Login"},
                timeout=5,
            )
            return "login.htm" not in resp.url and resp.status_code == 200
        except Exception as e:
            logger.warning(f"[SCADA REST] Falha ao efetuar login: {e}")
            return False

    def _read_trigger_rest(self, config: SystemConfiguration) -> Optional[bool]:
        """Lê o gatilho booleano via REST API."""
        base_url = getattr(settings, "SCADA_REST_BASE_URL", "http://192.168.0.202:8080/Scada-LTS")
        user = getattr(settings, "SCADA_REST_USERNAME", "teste")
        pswd = getattr(settings, "SCADA_REST_PASSWORD", "teste1")
        url_gatilho = f"{base_url}/api/point_value/getValue/{config.xid_gatilho}"

        try:
            resp = self.http_session.get(url_gatilho, timeout=2)
            if resp.status_code != 200 or "login.htm" in resp.url:
                if not self._ensure_rest_login(base_url, user, pswd):
                    return None
                resp = self.http_session.get(url_gatilho, timeout=2)

            if resp.status_code == 200:
                data = resp.json()
                raw_val = data.get("value")
                if isinstance(raw_val, str):
                    return raw_val.lower() == "true"
                return bool(raw_val)
        except Exception as e:
            logger.warning(f"[SCADA REST] Falha na leitura do gatilho: {e}")
            return None

        return None

    def _read_trigger_mysql(self, config: SystemConfiguration) -> Optional[bool]:
        """Lê o gatilho booleano diretamente do MySQL com normalização estrita."""
        try:
            val_info = self.repo.get_last_value(config.xid_gatilho)
            if not val_info:
                return None

            # val_info['value'] é bool normalizado (dataType=1)
            return bool(val_info["value"])
        except OperationalError as oe:
            logger.error(f"[SCADA MYSQL] Erro ao ler gatilho no MySQL: {oe}")
            return None
        except Exception as e:
            logger.error(f"[SCADA MYSQL] Erro inesperado no gatilho: {e}")
            return None

    def read_trigger(self, config: SystemConfiguration) -> Optional[bool]:
        """Lê o gatilho conforme a fonte configurada."""
        if config.data_source == "mysql":
            return self._read_trigger_mysql(config)
        else:
            return self._read_trigger_rest(config)

    def parse_coordinate_payload(self, raw_str: str) -> List[Tuple[float, float]]:
        """
        Interpreta o JSON de coordenadas retornado pela medição industrial.
        Ex: '[{"x": 12.4, "y": 2.35}]' ou dicionário único.
        """
        if not raw_str or not raw_str.strip():
            return []

        try:
            parsed = json.loads(raw_str)
        except Exception:
            return []

        coords: List[Tuple[float, float]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    x = item.get("x")
                    y = item.get("y")
                    if x is not None and y is not None:
                        try:
                            coords.append((float(x), float(y)))
                        except (ValueError, TypeError):
                            pass
        elif isinstance(parsed, dict):
            x = parsed.get("x")
            y = parsed.get("y")
            if x is not None and y is not None:
                try:
                    coords.append((float(x), float(y)))
                except (ValueError, TypeError):
                    pass

        return coords

    def _collect_measurements_rest(self, config: SystemConfiguration) -> int:
        """Coleta medição via REST e persiste no banco default."""
        base_url = getattr(settings, "SCADA_REST_BASE_URL", "http://192.168.0.202:8080/Scada-LTS")
        url_valor = f"{base_url}/api/point_value/getValue/{config.xid_medicao}"

        try:
            resp = self.http_session.get(url_valor, timeout=2)
            if resp.status_code != 200:
                return 0

            raw_data = resp.json()
            val_str = raw_data.get("value")
            ts_ms = raw_data.get("ts") or int(time.time() * 1000)
            dt_aware = self.repo.ts_to_datetime(ts_ms)

            coords = self.parse_coordinate_payload(val_str)
            saved_count = 0

            for vx, vy in coords:
                if abs(vx - self.last_valid_x) > config.delta_x_minimo:
                    LeituraScada.objects.create(
                        scan_id=self.current_scan_id,
                        xid=raw_data.get("xid", config.xid_medicao),
                        eixo_x=vx,
                        eixo_y=vy,
                        data_leitura=dt_aware,
                    )
                    self.last_valid_x = vx
                    saved_count += 1

            return saved_count
        except Exception as e:
            logger.warning(f"[SCADA REST] Erro ao coletar medição: {e}")
            return 0

    def _collect_measurements_mysql(self, config: SystemConfiguration) -> int:
        """
        Coleta medições incrementais a partir de pointvalues via MySQL direto.
        Utiliza cursor (last_ts, last_id) para não perder nenhum ponto gerado entre polls.
        """
        try:
            readings, new_last_id, new_last_ts = self.repo.get_incremental_readings(
                xid=config.xid_medicao,
                last_id=self.last_reading_id,
                last_ts=self.last_reading_ts,
                limit=config.tamanho_lote_incremental,
            )

            if not readings:
                return 0

            saved_count = 0

            for sample in readings:
                raw_payload = sample["value"]
                dt_aware = sample["datetime"]

                coords = self.parse_coordinate_payload(str(raw_payload))
                for vx, vy in coords:
                    if abs(vx - self.last_valid_x) > config.delta_x_minimo:
                        LeituraScada.objects.create(
                            scan_id=self.current_scan_id,
                            xid=config.xid_medicao,
                            eixo_x=vx,
                            eixo_y=vy,
                            data_leitura=dt_aware,
                        )
                        self.last_valid_x = vx
                        saved_count += 1

            # Atualiza o cursor incremental para o próximo poll
            self.last_reading_id = new_last_id
            self.last_reading_ts = new_last_ts

            return saved_count

        except OperationalError as oe:
            logger.error(f"[SCADA MYSQL] Erro ao obter medições incrementais: {oe}")
            return 0
        except Exception as e:
            logger.error(f"[SCADA MYSQL] Erro inesperado ao coletar medições: {e}")
            return 0

    def process_cycle(self) -> Dict[str, Any]:
        """
        Executa uma única iteração do ciclo de aquisição.
        Atualiza a máquina de estados e processa medições.
        """
        config = self.get_effective_config()

        if not config.aquisicao_ativa:
            return {"status": "PAUSED", "scan_id": self.current_scan_id, "saved": 0}

        trigger_active = self.read_trigger(config)

        # Se falhou a leitura de comunicação, aguarda
        if trigger_active is None:
            return {"status": "NO_COMMUNICATION", "scan_id": self.current_scan_id, "saved": 0}

        saved_count = 0

        # Máquina de Estados: Início / Fim de Varredura
        if trigger_active and not self.is_scanning:
            # Borda de Subida: Inicia novo Scan
            self.is_scanning = True
            self.current_scan_id = f"SCAN_{timezone.localtime().strftime('%Y%m%d_%H%M%S')}"
            self.last_valid_x = -999.0

            # Se for MySQL, inicializa o cursor na última amostra antes do início do scan
            if config.data_source == "mysql":
                last_sample = self.repo.get_last_value(config.xid_medicao)
                if last_sample:
                    self.last_reading_id = last_sample.get("point_value_id")
                    self.last_reading_ts = last_sample.get("ts")
                else:
                    self.last_reading_id = None
                    self.last_reading_ts = None

            logger.info(f">>> [INÍCIO DE SCAN] {self.current_scan_id} (Origem: {config.data_source})")

        elif not trigger_active and self.is_scanning:
            # Borda de Descida: Encerra Scan
            logger.info(f">>> [FIM DE SCAN] {self.current_scan_id}")
            self.is_scanning = False
            self.current_scan_id = None
            self.last_valid_x = -999.0

        # Se estiver em varredura ativa, coleta os pontos
        if self.is_scanning:
            if config.data_source == "mysql":
                saved_count = self._collect_measurements_mysql(config)
            else:
                saved_count = self._collect_measurements_rest(config)

        return {
            "status": "SCANNING" if self.is_scanning else "IDLE",
            "scan_id": self.current_scan_id,
            "saved": saved_count,
        }

    def run_forever(self, callback_progress=None):
        """
        Loop contínuo de aquisição industrial.
        Trata sinais de encerramento, fecha conexões ociosas e respeita intervalos.
        """
        logger.info(">>> Iniciando motor canônico de aquisição Scada...")

        while not self._stop_requested:
            try:
                loop_start = time.time()
                config = self.get_effective_config()

                result = self.process_cycle()

                if callback_progress:
                    callback_progress(result)

                # Fecha conexões com o banco para evitar vazamentos e timeout no MySQL
                connections.close_all()

                elapsed = time.time() - loop_start
                sleep_time = max(0.005, config.intervalo_consulta - elapsed)
                time.sleep(sleep_time)

            except KeyboardInterrupt:
                logger.info("Interrupção manual (KeyboardInterrupt) recebida.")
                break
            except Exception as e:
                logger.error(f"[SCADA SERVICE] Erro inesperado no loop de aquisição: {e}")
                time.sleep(2.0)

        logger.info(">>> Motor de aquisição Scada encerrado com sucesso.")
