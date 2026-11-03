import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from django.conf import settings
from django.db import OperationalError, connections
from django.utils import timezone

from .models import LeituraScada, SystemConfiguration
from .scada_repository import ScadaRepository

logger = logging.getLogger(__name__)


def get_status_file_path() -> Path:
    """Retorna o caminho do arquivo JSON de status do coletor."""
    base_dir = getattr(settings, "BASE_DIR", Path.cwd())
    return Path(base_dir) / "scada_collector_status.json"


def get_collector_status() -> Dict[str, Any]:
    """
    Lê o status atual do processo coletor a partir do arquivo de status e trava.
    Se o processo estiver inativo ou com heartbeat obsoleto (>6s), retorna status OFFLINE.
    """
    status_path = get_status_file_path()
    if not status_path.exists():
        return {
            "status": "OFFLINE",
            "scan_id": None,
            "is_scanning": False,
            "last_heartbeat": 0,
            "data_source": "mysql",
        }

    try:
        with open(status_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        now = time.time()
        heartbeat = float(data.get("last_heartbeat", 0))
        # Se o heartbeat tiver mais de 6 segundos, consideramos o processo inativo
        if (now - heartbeat) > 6.0:
            data["status"] = "OFFLINE"
            data["is_scanning"] = False

        return data
    except Exception:
        return {
            "status": "OFFLINE",
            "scan_id": None,
            "is_scanning": False,
            "last_heartbeat": 0,
            "data_source": "mysql",
        }


def update_collector_status(status_data: Dict[str, Any]):
    """Escreve atomicamente o status do coletor no arquivo JSON."""
    status_path = get_status_file_path()
    try:
        temp_path = status_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(status_data, f, ensure_ascii=False)
        temp_path.replace(status_path)
    except Exception as e:
        logger.debug(f"[STATUS] Falha ao atualizar status do coletor: {e}")


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

        # Marca status como OFFLINE ao liberar a trava
        try:
            update_collector_status({
                "status": "OFFLINE",
                "scan_id": None,
                "is_scanning": False,
                "last_heartbeat": time.time(),
                "pid": None,
            })
        except Exception:
            pass

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
    Garante fidelidade metrológica das coordenadas (X/Y), deduplicação robusta de amostras gêmeas
    e transições de ciclo sem perda de pontos.
    """

    def __init__(self, override_source: Optional[str] = None):
        self.override_source = override_source
        self.repo = ScadaRepository()
        self._stop_requested = False

        # Estado da Máquina de Scan
        self._first_run = True
        self.is_scanning = False
        self.current_scan_id: Optional[str] = None
        self.last_completed_scan_id: Optional[str] = None
        self.last_valid_x: float = -999.0
        self.last_valid_y: float = -999.0

        # Rastreamento de pontos já salvos no ciclo ativo para deduplicação:
        # chave: (ts_ms, round(vx, 3), round(vy, 3))
        self._seen_points_in_scan: Set[Tuple[int, float, float]] = set()
        self._scan_points_count: int = 0

        # Cursor para Leitura Incremental MySQL
        self.last_reading_id: Optional[int] = None
        self.last_reading_ts: Optional[int] = None

        # Rastreamento de Pareamento para Calibração Multiponto (DP_648291 + DP_632601)
        self.last_raw_thickness: Optional[float] = None
        self.last_raw_thickness_ts: Optional[int] = None
        self.last_raw_thickness_dt: Optional[datetime] = None
        self.last_pos_x: Optional[float] = None
        self.last_pos_x_ts: Optional[int] = None
        self.last_pos_x_dt: Optional[datetime] = None
        self.trigger_start_ts: Optional[int] = None

        # Histórico recente do gatilho (para sincronização e desempate temporal)
        self._last_trigger_info: Optional[Dict[str, Any]] = None
        self._consecutive_comm_errors: int = 0

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

    def generate_scan_id(self, dt: Optional[datetime] = None) -> str:
        """
        Gera um identificador único para o ciclo.
        Utiliza o carimbo de tempo da borda de subida e desempata caso haja repetição no mesmo segundo.
        """
        if dt is None:
            dt = timezone.localtime()
        base = f"SCAN_{dt.strftime('%Y%m%d_%H%M%S')}"
        if LeituraScada.objects.filter(scan_id=base).exists():
            base = f"SCAN_{dt.strftime('%Y%m%d_%H%M%S_%f')[:24]}"
        return base

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

    def get_trigger_info(self, config: SystemConfiguration) -> Optional[Dict[str, Any]]:
        """
        Lê informações detalhadas do gatilho (estado booleano, timestamp e datetime).
        Retorna dicionário estruturado ou None em caso de falha de comunicação.
        """
        if config.data_source == "mysql":
            try:
                val_info = self.repo.get_last_value(config.xid_gatilho)
                if not val_info:
                    return None
                return {
                    "active": bool(val_info["value"]),
                    "ts": int(val_info["ts"]),
                    "datetime": val_info["datetime"],
                    "pv_id": val_info.get("point_value_id"),
                }
            except OperationalError as oe:
                logger.error(f"[SCADA MYSQL] Erro operacional ao ler gatilho: {oe}")
                return None
            except Exception as e:
                logger.error(f"[SCADA MYSQL] Erro inesperado ao ler gatilho: {e}")
                return None
        else:
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
                    ts_ms = data.get("ts") or int(time.time() * 1000)
                    dt_aware = self.repo.ts_to_datetime(ts_ms)
                    if isinstance(raw_val, str):
                        active = raw_val.lower() == "true"
                    else:
                        active = bool(raw_val)
                    return {
                        "active": active,
                        "ts": int(ts_ms),
                        "datetime": dt_aware,
                        "pv_id": None,
                    }
            except Exception as e:
                logger.warning(f"[SCADA REST] Falha na leitura do gatilho: {e}")
                return None

            return None

    def read_trigger(self, config: SystemConfiguration) -> Optional[bool]:
        """
        Lê o gatilho booleano conforme a fonte configurada.
        Mantido para compatibilidade e testes unitários existentes.
        """
        info = self.get_trigger_info(config)
        if info is not None:
            self._last_trigger_info = info
            return info["active"]
        return None

    def parse_coordinate_payload(self, raw_str: str) -> List[Tuple[float, float]]:
        """
        Interpreta o JSON de coordenadas retornado pela medição industrial.
        Ex: '[{"x": 12.4, "y": 2.35}]' ou dicionário único.
        Preserva as coordenadas físicas reais sem alterações.
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

    def _collect_calibrated_rest(self, config: SystemConfiguration) -> int:
        """
        Coleta medições calibradas via REST API a partir de dois Data Points independentes:
          - config.xid_posicao (DP_648291): Posição física X
          - config.xid_bruto_espessura (DP_632601): Valor bruto do sensor de espessura
        """
        base_url = getattr(settings, "SCADA_REST_BASE_URL", "http://192.168.0.202:8080/Scada-LTS")
        user = getattr(settings, "SCADA_REST_USERNAME", "teste")
        pswd = getattr(settings, "SCADA_REST_PASSWORD", "teste1")

        url_pos = f"{base_url}/api/point_value/getValue/{config.xid_posicao}"
        url_raw = f"{base_url}/api/point_value/getValue/{config.xid_bruto_espessura}"

        try:
            resp_pos = self.http_session.get(url_pos, timeout=2)
            if resp_pos.status_code != 200 or "login.htm" in resp_pos.url:
                if not self._ensure_rest_login(base_url, user, pswd):
                    return 0
                resp_pos = self.http_session.get(url_pos, timeout=2)
                if resp_pos.status_code != 200:
                    return 0

            resp_raw = self.http_session.get(url_raw, timeout=2)
            if resp_raw.status_code != 200:
                return 0

            data_pos = resp_pos.json()
            data_raw = resp_raw.json()

            val_pos_raw = data_pos.get("value")
            val_raw_raw = data_raw.get("value")

            if val_pos_raw is None or val_raw_raw is None:
                return 0

            try:
                pos_x = float(val_pos_raw)
                raw_esp = float(val_raw_raw)
            except (ValueError, TypeError):
                return 0

            ts_pos = int(data_pos.get("ts") or time.time() * 1000)
            ts_raw = int(data_raw.get("ts") or time.time() * 1000)
            dt_aware = self.repo.ts_to_datetime(ts_pos)

            max_time_diff_ms = getattr(config, "tolerancia_temporal_ms", 500)
            limite_stale_ms = getattr(config, "limite_stale_segundos", 30) * 1000

            # Filtro espacial em X (ruído de encoder)
            is_first = (self.last_valid_x == -999.0)
            x_constant = (abs(pos_x - self.last_valid_x) < 0.0001)
            x_displaced = (abs(pos_x - self.last_valid_x) >= config.delta_x_minimo)

            if not (is_first or x_constant or x_displaced):
                return 0

            # Coerência temporal entre as duas amostras
            delta_t = abs(ts_pos - ts_raw)
            cycle_ref_ts = (self.trigger_start_ts or ts_pos) - max_time_diff_ms
            is_coherent = (delta_t <= max_time_diff_ms) or (
                ts_raw >= cycle_ref_ts and delta_t <= limite_stale_ms
            )

            coord_key = (ts_pos, round(pos_x, 3), round(raw_esp, 3))
            if coord_key in self._seen_points_in_scan:
                return 0
            self._seen_points_in_scan.add(coord_key)

            from .calibration_service import CalibrationService
            active_cal = CalibrationService.get_active_calibration()

            eixo_y = None
            zero_interp = None
            fator = None
            calib_obj = active_cal
            status_validade = "AMOSTRA_INCOERENTE"

            if is_coherent:
                calc = CalibrationService.calcular_espessura(pos_x, raw_esp, calibracao=active_cal)
                if calc["valido"]:
                    eixo_y = calc["espessura_mm"]
                    zero_interp = calc["zero_interpolado"]
                    fator = calc["fator_conversao"]
                    status_validade = "VALIDO"
                else:
                    eixo_y = None
                    zero_interp = calc.get("zero_interpolado")
                    fator = calc.get("fator_conversao")
                    status_validade = calc["status"]

            rec = LeituraScada(
                scan_id=self.current_scan_id,
                xid=config.xid_posicao,
                eixo_x=pos_x,
                eixo_y=eixo_y,
                data_leitura=dt_aware,
                valor_bruto=raw_esp,
                posicao_bruta=pos_x,
                zero_interpolado=zero_interp,
                fator_conversao=fator,
                calibracao=calib_obj,
                status_validade=status_validade,
            )
            rec.save()

            self.last_valid_x = pos_x
            if eixo_y is not None:
                self.last_valid_y = eixo_y
            self._scan_points_count += 1
            return 1

        except Exception as e:
            logger.warning(f"[SCADA REST] Erro ao coletar medição calibrada: {e}")
            return 0

    def _collect_calibrated_mysql(
        self,
        config: SystemConfiguration,
        max_ts: Optional[int] = None,
    ) -> int:
        """
        Coleta medições industriais a partir de dois Data Points independentes:
          - config.xid_posicao (DP_648291): Coordenada X da posição do sensor
          - config.xid_bruto_espessura (DP_632601): Leitura bruta do sensor de espessura
        
        Aplica pareamento com tolerância temporal, interpolação linear do zero
        em função da posição X, rejeição de extrapolações fora da faixa física
        e gravação das coordenadas metrológicas (X, Y).
        """
        xids = [config.xid_posicao, config.xid_bruto_espessura]
        try:
            readings, new_last_id, new_last_ts = self.repo.get_incremental_readings_multi(
                xids=xids,
                last_id=self.last_reading_id,
                last_ts=self.last_reading_ts,
                limit=config.tamanho_lote_incremental,
                max_ts=max_ts,
            )

            if not readings:
                return 0

            from .calibration_service import CalibrationService
            active_cal = CalibrationService.get_active_calibration()

            saved_count = 0
            records_to_create = []
            max_time_diff_ms = getattr(config, "tolerancia_temporal_ms", 500)
            limite_stale_ms = getattr(config, "limite_stale_segundos", 30) * 1000

            for sample in readings:
                xid = sample["xid"]
                val = sample["value"]
                ts_ms = int(sample["ts"])
                dt_aware = sample["datetime"]

                if xid == config.xid_bruto_espessura:
                    try:
                        self.last_raw_thickness = float(val)
                        self.last_raw_thickness_ts = ts_ms
                        self.last_raw_thickness_dt = dt_aware
                    except (ValueError, TypeError):
                        pass

                elif xid == config.xid_posicao:
                    try:
                        pos_x = float(val)
                    except (ValueError, TypeError):
                        continue

                    # Filtro espacial em X (ruído de encoder)
                    is_first = (self.last_valid_x == -999.0)
                    x_constant = (abs(pos_x - self.last_valid_x) < 0.0001)
                    x_displaced = (abs(pos_x - self.last_valid_x) >= config.delta_x_minimo)

                    if not (is_first or x_constant or x_displaced):
                        continue

                    # Pareamento temporal com leitura bruta de espessura
                    if self.last_raw_thickness is None or self.last_raw_thickness_ts is None:
                        # Tenta obter último valor do Scada caso não haja leitura no batch atual
                        last_raw_info = self.repo.get_last_value(config.xid_bruto_espessura)
                        if last_raw_info and last_raw_info.get("value") is not None:
                            try:
                                self.last_raw_thickness = float(last_raw_info["value"])
                                self.last_raw_thickness_ts = int(last_raw_info["ts"])
                                self.last_raw_thickness_dt = last_raw_info["datetime"]
                            except (ValueError, TypeError):
                                pass

                    is_coherent = False
                    if self.last_raw_thickness is not None and self.last_raw_thickness_ts is not None:
                        delta_t = abs(ts_ms - self.last_raw_thickness_ts)
                        cycle_ref_ts = (self.trigger_start_ts or ts_ms) - max_time_diff_ms
                        if delta_t <= max_time_diff_ms or (
                            self.last_raw_thickness_ts >= cycle_ref_ts and delta_t <= limite_stale_ms
                        ):
                            is_coherent = True

                    # Deduplicação
                    coord_key = (ts_ms, round(pos_x, 3), round(self.last_raw_thickness or 0.0, 3))
                    if coord_key in self._seen_points_in_scan:
                        continue
                    self._seen_points_in_scan.add(coord_key)

                    eixo_y = None
                    zero_interp = None
                    fator = None
                    calib_obj = active_cal
                    status_validade = "AMOSTRA_INCOERENTE"

                    if is_coherent and self.last_raw_thickness is not None:
                        calc = CalibrationService.calcular_espessura(
                            pos_x, self.last_raw_thickness, calibracao=active_cal
                        )
                        if calc["valido"]:
                            eixo_y = calc["espessura_mm"]
                            zero_interp = calc["zero_interpolado"]
                            fator = calc["fator_conversao"]
                            status_validade = "VALIDO"
                        else:
                            # Fora da faixa física ou sem calibração ativa:
                            # REGRA INDUSTRIAL: NÃO SALVAR VALOR ARTIFICIAL ZERO
                            eixo_y = None
                            zero_interp = calc.get("zero_interpolado")
                            fator = calc.get("fator_conversao")
                            status_validade = calc["status"]

                    records_to_create.append(
                        LeituraScada(
                            scan_id=self.current_scan_id,
                            xid=config.xid_posicao,
                            eixo_x=pos_x,
                            eixo_y=eixo_y,
                            data_leitura=dt_aware,
                            valor_bruto=self.last_raw_thickness,
                            posicao_bruta=pos_x,
                            zero_interpolado=zero_interp,
                            fator_conversao=fator,
                            calibracao=calib_obj,
                            status_validade=status_validade,
                        )
                    )
                    self.last_valid_x = pos_x
                    if eixo_y is not None:
                        self.last_valid_y = eixo_y
                    saved_count += 1

            if records_to_create:
                LeituraScada.objects.bulk_create(records_to_create)

            self.last_reading_id = new_last_id
            self.last_reading_ts = new_last_ts
            self._scan_points_count += saved_count
            return saved_count

        except OperationalError as oe:
            logger.error(f"[SCADA MYSQL] Erro operacional ao coletar medições calibradas: {oe}")
            return 0
        except Exception as e:
            logger.error(f"[SCADA MYSQL] Erro inesperado ao coletar medições calibradas: {e}")
            return 0

    def _collect_measurements_rest(self, config: SystemConfiguration) -> int:
        """
        Coleta medição via REST e persiste no banco default.
        Aplica deduplicação por identificador/timestamp e conteúdo para evitar
        gravação redundante decorrente da taxa de polling.
        """
        if getattr(config, "modo_aquisicao", "calibrado") == "calibrado":
            return self._collect_calibrated_rest(config)

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
            records_to_create = []

            for vx, vy in coords:
                # 1. Deduplicação por identificador/timestamp e conteúdo (Method 2):
                coord_key = (int(ts_ms), round(vx, 3), round(vy, 3))
                if coord_key in self._seen_points_in_scan:
                    continue

                # 2. Critério metrológico e filtro de ruído espacial em X:
                is_first = (self.last_valid_x == -999.0)
                x_constant = (abs(vx - self.last_valid_x) < 0.0001)
                x_displaced = (abs(vx - self.last_valid_x) >= config.delta_x_minimo)

                if not (is_first or x_constant or x_displaced):
                    # Movimento em X abaixo do delta_x_minimo (ruído de encoder)
                    continue

                self._seen_points_in_scan.add(coord_key)

                records_to_create.append(
                    LeituraScada(
                        scan_id=self.current_scan_id,
                        xid=raw_data.get("xid", config.xid_medicao),
                        eixo_x=vx,
                        eixo_y=vy,
                        data_leitura=dt_aware,
                    )
                )
                self.last_valid_x = vx
                self.last_valid_y = vy
                saved_count += 1

            if records_to_create:
                LeituraScada.objects.bulk_create(records_to_create)

            self._scan_points_count += saved_count
            return saved_count

        except Exception as e:
            logger.warning(f"[SCADA REST] Erro ao coletar medição: {e}")
            return 0

    def _collect_measurements_mysql(
        self,
        config: SystemConfiguration,
        max_ts: Optional[int] = None,
    ) -> int:
        """
        Coleta medições incrementais a partir de pointvalues via MySQL direto.
        Utiliza cursor (last_ts, last_id) para não perder nenhum ponto gerado entre polls.
        Deduplica amostras gêmeas geradas pelo Scada no mesmo ts com mesmo conteúdo.
        Preserva integralmente variações de espessura Y mesmo com X constante.
        """
        if getattr(config, "modo_aquisicao", "calibrado") == "calibrado":
            return self._collect_calibrated_mysql(config, max_ts=max_ts)

        try:
            readings, new_last_id, new_last_ts = self.repo.get_incremental_readings(
                xid=config.xid_medicao,
                last_id=self.last_reading_id,
                last_ts=self.last_reading_ts,
                limit=config.tamanho_lote_incremental,
                max_ts=max_ts,
            )

            if not readings:
                return 0

            saved_count = 0
            records_to_create = []

            for sample in readings:
                raw_payload = sample["value"]
                dt_aware = sample["datetime"]
                ts_ms = int(sample["ts"])

                coords = self.parse_coordinate_payload(str(raw_payload))
                for vx, vy in coords:
                    # 1. Deduplicação por identificador de amostra e conteúdo (Method 2):
                    coord_key = (ts_ms, round(vx, 3), round(vy, 3))
                    if coord_key in self._seen_points_in_scan:
                        continue

                    # 2. Critério metrológico e filtro de ruído espacial em X:
                    is_first = (self.last_valid_x == -999.0)
                    x_constant = (abs(vx - self.last_valid_x) < 0.0001)
                    x_displaced = (abs(vx - self.last_valid_x) >= config.delta_x_minimo)

                    if not (is_first or x_constant or x_displaced):
                        # Movimento em X abaixo do delta_x_minimo (ruído de encoder)
                        continue

                    self._seen_points_in_scan.add(coord_key)

                    records_to_create.append(
                        LeituraScada(
                            scan_id=self.current_scan_id,
                            xid=config.xid_medicao,
                            eixo_x=vx,
                            eixo_y=vy,
                            data_leitura=dt_aware,
                        )
                    )
                    self.last_valid_x = vx
                    self.last_valid_y = vy
                    saved_count += 1

            if records_to_create:
                LeituraScada.objects.bulk_create(records_to_create)

            # Atualiza o cursor incremental para o próximo poll
            self.last_reading_id = new_last_id
            self.last_reading_ts = new_last_ts
            self._scan_points_count += saved_count

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
        Atualiza a máquina de estados, processa medições e sincroniza o status do sistema.
        """
        config = self.get_effective_config()

        if not config.aquisicao_ativa:
            status_res = {
                "status": "PAUSED",
                "scan_id": self.current_scan_id or self.last_completed_scan_id,
                "is_scanning": self.is_scanning,
                "saved": 0,
                "total_in_scan": self._scan_points_count,
                "data_source": config.data_source,
                "last_heartbeat": time.time(),
            }
            update_collector_status(status_res)
            return status_res

        # Leitura com fallback para compatibilidade de testes que mockam read_trigger
        trigger_active = self.read_trigger(config)

        # Se falhou a comunicação com o Scada
        if trigger_active is None:
            self._consecutive_comm_errors += 1
            if self._consecutive_comm_errors == 1:
                logger.warning(f"[SCADA SERVICE] Falha de comunicação com o Scada-LTS ({config.data_source}).")

            status_res = {
                "status": "NO_COMMUNICATION",
                "scan_id": self.current_scan_id or self.last_completed_scan_id,
                "is_scanning": self.is_scanning,
                "saved": 0,
                "total_in_scan": self._scan_points_count,
                "data_source": config.data_source,
                "last_heartbeat": time.time(),
                "error": "Comunicação indisponível com o Scada-LTS",
            }
            update_collector_status(status_res)
            return status_res

        # Recuperação de comunicação
        if self._consecutive_comm_errors > 0:
            logger.info(f"[SCADA SERVICE] Comunicação restabelecida com o Scada-LTS ({config.data_source}).")
            self._consecutive_comm_errors = 0

        # Obtém timestamp do gatilho atual
        trigger_ts = None
        trigger_dt = None
        if self._last_trigger_info:
            trigger_ts = self._last_trigger_info.get("ts")
            trigger_dt = self._last_trigger_info.get("datetime")

        if trigger_ts is None:
            trigger_ts = int(time.time() * 1000)
            trigger_dt = timezone.localtime()

        saved_count = 0

        # ── Tratamento Explícito de Inicialização ───────────────────────────
        if self._first_run:
            self._first_run = False
            if trigger_active:
                logger.warning(
                    f"[SCADA SERVICE] Coletor inicializado com gatilho já em nível ALTO (1). "
                    f"Assumindo ciclo em andamento a partir de ts={trigger_ts}."
                )
                self.is_scanning = True
                self.current_scan_id = self.generate_scan_id(trigger_dt)
                self.last_valid_x = -999.0
                self.last_valid_y = -999.0
                self._seen_points_in_scan.clear()
                self._scan_points_count = 0
                self.trigger_start_ts = trigger_ts
                self.last_raw_thickness = None
                self.last_raw_thickness_ts = None
                self.last_pos_x = None
                self.last_pos_x_ts = None

                # Inicializa cursor a partir do carimbo do gatilho
                if config.data_source == "mysql":
                    self.last_reading_ts = trigger_ts
                    self.last_reading_id = 0
            else:
                logger.info(
                    "[SCADA SERVICE] Coletor inicializado com gatilho em repouso (0). "
                    "Aguardando borda de subida (0 -> 1)."
                )
                self.is_scanning = False
                self.current_scan_id = None

        # ── Máquina de Estados: Borda de Subida (0 -> 1) ────────────────────
        elif trigger_active and not self.is_scanning:
            self.is_scanning = True
            self.current_scan_id = self.generate_scan_id(trigger_dt)
            self.last_valid_x = -999.0
            self.last_valid_y = -999.0
            self._seen_points_in_scan.clear()
            self._scan_points_count = 0
            self.trigger_start_ts = trigger_ts
            self.last_raw_thickness = None
            self.last_raw_thickness_ts = None
            self.last_pos_x = None
            self.last_pos_x_ts = None

            # Inicializa cursor no exato timestamp de início do ciclo
            # (id=0 garante que medições com ts == trigger_ts sejam lidas sem perda)
            if config.data_source == "mysql":
                self.last_reading_ts = trigger_ts
                self.last_reading_id = 0

            logger.info(
                f">>> [INÍCIO DE SCAN] {self.current_scan_id} "
                f"(Gatilho ativo em ts={trigger_ts}, Origem: {config.data_source})"
            )

        # ── Máquina de Estados: Borda de Descida (1 -> 0) ───────────────────
        elif not trigger_active and self.is_scanning:
            closing_scan_id = self.current_scan_id
            logger.info(
                f">>> [BORDA DE DESCIDA] Gatilho retornou a 0 (ts={trigger_ts}). "
                f"Concluindo coleta residual para {closing_scan_id}..."
            )

            # Drena medições restantes geradas até o encerramento do gatilho (max_ts = trigger_ts)
            # Evita perda dos últimos pontos por latência de gravação no Scada
            drain_saved = 0
            if config.data_source == "mysql":
                drain_saved = self._collect_measurements_mysql(config, max_ts=trigger_ts)
            else:
                drain_saved = self._collect_measurements_rest(config)

            total_points = self._scan_points_count
            logger.info(
                f">>> [FIM DE SCAN] {closing_scan_id} finalizado com sucesso. "
                f"Total de pontos no ciclo: {total_points} (+{drain_saved} no encerramento)."
            )

            # ── Cálculo Dimensional Automático da Banda de Rodagem ──────────
            try:
                from .dimensional_analysis import DimensionalAnalyzer
                dim_res = DimensionalAnalyzer.calcular_e_persistir_scan(closing_scan_id, config=config)
                if dim_res and dim_res.espessura_media_coroa is not None:
                    logger.info(
                        f">>> [DIMENSÕES AUTOMÁTICAS] {closing_scan_id}: "
                        f"Largura Total={dim_res.largura_total}mm, "
                        f"Largura Coroa={dim_res.largura_coroa}mm, "
                        f"ESPESSURA MÉDIA COROA={dim_res.espessura_media_coroa}mm ({dim_res.pontos_coroa} pts)."
                    )
                else:
                    logger.info(
                        f">>> [DIMENSÕES AUTOMÁTICAS] {closing_scan_id}: "
                        f"Status={dim_res.status_leitura if dim_res else 'DADOS_INSUFICIENTES'} "
                        f"({dim_res.motivo_status if dim_res else ''})"
                    )
            except Exception as e_dim:
                logger.error(f"[DIMENSÕES AUTOMÁTICAS] Erro ao calcular dimensões de {closing_scan_id}: {e_dim}")

            self.last_completed_scan_id = closing_scan_id
            self.is_scanning = False
            self.current_scan_id = None
            self.last_valid_x = -999.0
            self.last_valid_y = -999.0
            self._seen_points_in_scan.clear()
            self.last_reading_id = None
            self.last_reading_ts = None
            self.trigger_start_ts = None
            self.last_raw_thickness = None
            self.last_raw_thickness_ts = None
            self.last_pos_x = None
            self.last_pos_x_ts = None

        # ── Varredura Ativa (Nível Alto) ───────────────────────────────────
        if self.is_scanning:
            if config.data_source == "mysql":
                saved_count = self._collect_measurements_mysql(config)
            else:
                saved_count = self._collect_measurements_rest(config)

        cycle_status = "SCANNING" if self.is_scanning else "IDLE"
        status_res = {
            "status": cycle_status,
            "scan_id": self.current_scan_id or self.last_completed_scan_id,
            "is_scanning": self.is_scanning,
            "saved": saved_count,
            "total_in_scan": self._scan_points_count,
            "data_source": config.data_source,
            "last_heartbeat": time.time(),
        }
        update_collector_status(status_res)
        return status_res

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

        # Encerramento limpo do status
        try:
            update_collector_status({
                "status": "OFFLINE",
                "scan_id": self.last_completed_scan_id,
                "is_scanning": False,
                "last_heartbeat": time.time(),
            })
        except Exception:
            pass

        logger.info(">>> Motor de aquisição Scada encerrado com sucesso.")
