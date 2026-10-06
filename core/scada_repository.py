import logging
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import OperationalError, connections
from django.utils import timezone

from .models_scada import ScadaDataPoint, ScadaPointValue, ScadaPointValueAnnotation

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

# Códigos de Tipo de Dados do Scada-LTS
DATA_TYPE_BINARY = 1
DATA_TYPE_MULTISTATE = 2
DATA_TYPE_NUMERIC = 3
DATA_TYPE_ALPHANUMERIC = 4

# Estados de Telemetria e Diagnóstico
STATUS_OK = "OK"
STATUS_EMPTY = "EMPTY"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_NO_READINGS = "NO_READINGS"
STATUS_SCADA_OFFLINE = "SCADA_OFFLINE"
STATUS_STALE = "STALE"


class ScadaRepository:
    """
    Camada de repositório e acesso a dados de alta performance para o Scada-LTS (somente-leitura).
    Opera contra a conexão 'scada' com proteções industriais contra quedas de conexão,
    normalização tipada estrita e suporte a leitura incremental por cursor (ts, id).
    """

    def __init__(self, xid_cache_ttl: int = 900, failed_xid_ttl: int = 10):
        self._xid_cache_ttl = xid_cache_ttl
        self._failed_xid_ttl = failed_xid_ttl
        self._xid_to_dp_cache: Dict[str, Tuple[int, int, str, float]] = {}  # {xid: (dp_id, data_type, name, expiry)}
        self._failed_xids: Dict[str, float] = {}  # {xid: expiry}

    def clear_cache(self):
        """Limpa todos os caches em memória."""
        self._xid_to_dp_cache.clear()
        self._failed_xids.clear()

    def resolve_xid(self, xid: str) -> Optional[Dict[str, Any]]:
        """
        Resolve um único XID para seus metadados (id, nome, data_source_id).
        Utiliza cache com TTL e quarentena de falhas.
        """
        if not xid or not xid.strip():
            return None

        resolved = self.resolve_xids([xid])
        return resolved.get(xid.strip())

    def resolve_xids(self, xids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Resolve em lote uma lista de XIDs para id interno e metadados.
        Consulta parametrizada roteada para o banco 'scada'.
        """
        now = time.time()
        clean_xids = list({x.strip() for x in xids if x and x.strip()})
        results: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []

        for x in clean_xids:
            # Checa se está em quarentena de falhas
            if x in self._failed_xids:
                if now < self._failed_xids[x]:
                    continue
                else:
                    del self._failed_xids[x]

            # Checa cache de sucesso
            if x in self._xid_to_dp_cache:
                dp_id, dt_source, name, exp = self._xid_to_dp_cache[x]
                if now < exp:
                    results[x] = {
                        "id": dp_id,
                        "xid": x,
                        "data_source_id": dt_source,
                        "point_name": name,
                    }
                    continue
                else:
                    del self._xid_to_dp_cache[x]

            missing.append(x)

        if not missing:
            return results

        try:
            qs = (
                ScadaDataPoint.objects.using("scada")
                .filter(xid__in=missing)
                .values("id", "xid", "data_source_id", "point_name")
            )

            found_xids = set()
            for row in qs:
                x = row["xid"]
                dp_id = row["id"]
                dt_source = row["data_source_id"]
                name = row["point_name"] or ""

                self._xid_to_dp_cache[x] = (dp_id, dt_source, name, now + self._xid_cache_ttl)
                results[x] = {
                    "id": dp_id,
                    "xid": x,
                    "data_source_id": dt_source,
                    "point_name": name,
                }
                found_xids.add(x)

            # Marca XIDs não encontrados para quarentena
            for x in missing:
                if x not in found_xids:
                    self._failed_xids[x] = now + self._failed_xttl

        except OperationalError as oe:
            logger.error(f"[SCADA REPO] Erro operacional ao resolver XIDs no MySQL: {oe}")
            raise
        except Exception as e:
            logger.error(f"[SCADA REPO] Erro inesperado ao resolver XIDs: {e}")
            raise

        return results

    @property
    def _failed_xttl(self) -> int:
        return self._failed_xid_ttl

    @staticmethod
    def normalize_value(
        data_type: int,
        point_value: Optional[float],
        text_short: Optional[str] = None,
        text_long: Optional[str] = None,
    ) -> Tuple[Any, str]:
        """
        Normaliza os valores brutos do Scada-LTS segundo a especificação industrial:
        - dataType 1 (Binary): True/False (preserva False como valor ativo legítimo)
        - dataType 2 (Multistate): int (preserva 0 como valor ativo legítimo)
        - dataType 3 (Numeric): float/int (preserva 0.0 como medição de pressão/cota legítima)
        - dataType 4 (Alphanumeric): string (preserva "" como string legítima)

        Retorna: (normalized_value, string_representation)
        """
        if data_type == DATA_TYPE_BINARY:
            norm_val = bool(point_value == 1.0) if point_value is not None else False
            str_val = "1" if norm_val else "0"
            return norm_val, str_val

        elif data_type == DATA_TYPE_MULTISTATE:
            norm_val = int(point_value) if point_value is not None else 0
            return norm_val, str(norm_val)

        elif data_type == DATA_TYPE_NUMERIC:
            if point_value is None:
                return 0.0, "0"
            if hasattr(point_value, "is_integer") and point_value.is_integer():
                norm_val = int(point_value)
                return norm_val, str(norm_val)
            norm_val = round(point_value, 3)
            return norm_val, f"{point_value:.3f}"

        elif data_type == DATA_TYPE_ALPHANUMERIC:
            text = text_short or text_long or ""
            return text, text

        else:
            str_v = str(point_value) if point_value is not None else ""
            return str_v, str_v

    @staticmethod
    def ts_to_datetime(ts_ms: int) -> datetime:
        """
        Converte timestamp epoch em milissegundos para objeto datetime timezone-aware em America/Sao_Paulo.
        """
        dt_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=dt_timezone.utc)
        return dt_utc.astimezone(LOCAL_TZ)

    def get_last_value(self, xid: str) -> Optional[Dict[str, Any]]:
        """
        Obtém o último valor persistido de um único XID.
        """
        batch = self.get_last_values_batch([xid])
        return batch.get(xid)

    def get_last_values_batch(self, xids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Consulta em lote os registros mais recentes dos XIDs informados.
        Utiliza subquery indexada na tabela pointvalues com desempate determinístico por MAX(id).
        """
        resolved = self.resolve_xids(xids)
        if not resolved:
            return {}

        dp_id_to_xid = {meta["id"]: xid for xid, meta in resolved.items()}
        dp_ids = list(dp_id_to_xid.keys())

        if not dp_ids:
            return {}

        placeholders = ", ".join(["%s"] * len(dp_ids))

        # Query indexada com desempate determinístico (maior ts, maior id)
        sql = f"""
            SELECT 
                pv.id AS pv_id,
                pv.dataPointId,
                pv.dataType,
                pv.pointValue,
                pv.ts,
                pva.textPointValueShort,
                pva.textPointValueLong
            FROM pointvalues pv
            LEFT JOIN pointvalueannotations pva ON pva.pointValueId = pv.id
            INNER JOIN (
                SELECT dataPointId, MAX(ts) AS max_ts
                FROM pointvalues
                WHERE dataPointId IN ({placeholders})
                GROUP BY dataPointId
            ) latest ON latest.dataPointId = pv.dataPointId AND latest.max_ts = pv.ts
            ORDER BY pv.ts DESC, pv.id DESC;
        """

        results: Dict[str, Dict[str, Any]] = {}
        processed_dps = set()

        try:
            with connections["scada"].cursor() as cursor:
                cursor.execute(sql, dp_ids)
                rows = cursor.fetchall()

                for row in rows:
                    pv_id, dp_id, dt, raw_val, ts, short_txt, long_txt = row
                    if dp_id in processed_dps:
                        continue
                    processed_dps.add(dp_id)

                    xid = dp_id_to_xid.get(dp_id)
                    if not xid:
                        continue

                    norm_val, str_val = self.normalize_value(dt, raw_val, short_txt, long_txt)
                    dt_aware = self.ts_to_datetime(ts)

                    results[xid] = {
                        "xid": xid,
                        "data_point_id": dp_id,
                        "point_value_id": pv_id,
                        "data_type": dt,
                        "value": norm_val,
                        "str_value": str_val,
                        "ts": ts,
                        "datetime": dt_aware,
                        "read_at": time.time(),
                    }

        except OperationalError as oe:
            logger.error(f"[SCADA REPO] OperationalError em get_last_values_batch: {oe}")
            raise
        except Exception as e:
            logger.error(f"[SCADA REPO] Erro em get_last_values_batch: {e}")
            raise

        return results

    def get_incremental_readings(
        self,
        xid: str,
        last_id: Optional[int] = None,
        last_ts: Optional[int] = None,
        limit: int = 500,
    ) -> Tuple[List[Dict[str, Any]], Optional[int], Optional[int]]:
        """
        Consumo incremental de medições a partir de um cursor (last_ts, last_id).
        Retorna: (lista_de_amostras, novo_last_id, novo_last_ts)

        Garante:
        - Ordem estritamente cronológica (ts ASC, id ASC);
        - Zero pontos perdidos entre polls;
        - Zero pontos duplicados;
        - Resiliência contra empates de timestamp no mesmo milissegundo.
        """
        meta = self.resolve_xid(xid)
        if not meta:
            return [], last_id, last_ts

        dp_id = meta["id"]

        params: List[Any] = [dp_id]
        where_clause = "pv.dataPointId = %s"

        if last_ts is not None and last_id is not None:
            where_clause += " AND (pv.ts > %s OR (pv.ts = %s AND pv.id > %s))"
            params.extend([last_ts, last_ts, last_id])
        elif last_id is not None:
            where_clause += " AND pv.id > %s"
            params.append(last_id)
        elif last_ts is not None:
            where_clause += " AND pv.ts > %s"
            params.append(last_ts)

        params.append(int(limit))

        sql = f"""
            SELECT 
                pv.id AS pv_id,
                pv.dataType,
                pv.pointValue,
                pv.ts,
                pva.textPointValueShort,
                pva.textPointValueLong
            FROM pointvalues pv
            LEFT JOIN pointvalueannotations pva ON pva.pointValueId = pv.id
            WHERE {where_clause}
            ORDER BY pv.ts ASC, pv.id ASC
            LIMIT %s;
        """

        readings: List[Dict[str, Any]] = []
        new_last_id = last_id
        new_last_ts = last_ts

        try:
            with connections["scada"].cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()

                for row in rows:
                    pv_id, dt, raw_val, ts, short_txt, long_txt = row
                    norm_val, str_val = self.normalize_value(dt, raw_val, short_txt, long_txt)
                    dt_aware = self.ts_to_datetime(ts)

                    readings.append({
                        "xid": xid,
                        "data_point_id": dp_id,
                        "point_value_id": pv_id,
                        "data_type": dt,
                        "value": norm_val,
                        "str_value": str_val,
                        "ts": ts,
                        "datetime": dt_aware,
                    })

                    new_last_id = pv_id
                    new_last_ts = ts

        except OperationalError as oe:
            logger.error(f"[SCADA REPO] OperationalError em get_incremental_readings para {xid}: {oe}")
            raise
        except Exception as e:
            logger.error(f"[SCADA REPO] Erro em get_incremental_readings: {e}")
            raise

        return readings, new_last_id, new_last_ts

    def diagnosticar_xid(self, xid: str, stale_limit_seconds: int = 30) -> Dict[str, Any]:
        """
        Diagnostica um Data Point individual sem efetuar nenhuma escrita no Scada.
        Retorna informações detalhadas para a área operacional do Django Admin.
        """
        clean_xid = (xid or "").strip()
        if not clean_xid:
            return {
                "xid": "",
                "status": STATUS_EMPTY,
                "mensagem": "Nenhum XID fornecido.",
                "data_point_id": None,
                "point_name": None,
                "data_type": None,
                "valor": None,
                "str_valor": "-",
                "timestamp": None,
                "idade_segundos": None,
            }

        try:
            meta = self.resolve_xid(clean_xid)
        except OperationalError as oe:
            return {
                "xid": clean_xid,
                "status": STATUS_SCADA_OFFLINE,
                "mensagem": f"Banco Scada MySQL inacessível ou offline: {oe}",
                "data_point_id": None,
                "point_name": None,
                "data_type": None,
                "valor": None,
                "str_valor": "-",
                "timestamp": None,
                "idade_segundos": None,
            }

        if not meta:
            return {
                "xid": clean_xid,
                "status": STATUS_NOT_FOUND,
                "mensagem": f"XID '{clean_xid}' não encontrado na tabela 'datapoints'.",
                "data_point_id": None,
                "point_name": None,
                "data_type": None,
                "valor": None,
                "str_valor": "-",
                "timestamp": None,
                "idade_segundos": None,
            }

        try:
            reading = self.get_last_value(clean_xid)
        except OperationalError as oe:
            return {
                "xid": clean_xid,
                "status": STATUS_SCADA_OFFLINE,
                "mensagem": f"Falha de comunicação MySQL ao obter valor: {oe}",
                "data_point_id": meta["id"],
                "point_name": meta["point_name"],
                "data_type": None,
                "valor": None,
                "str_valor": "-",
                "timestamp": None,
                "idade_segundos": None,
            }

        if not reading:
            return {
                "xid": clean_xid,
                "status": STATUS_NO_READINGS,
                "mensagem": "Ponto cadastrado em 'datapoints', mas sem nenhum registro histórico na tabela 'pointvalues'.",
                "data_point_id": meta["id"],
                "point_name": meta["point_name"],
                "data_type": None,
                "valor": None,
                "str_valor": "Sem leituras",
                "timestamp": None,
                "idade_segundos": None,
            }

        now_ms = int(time.time() * 1000)
        idade_segundos = max(0, int((now_ms - reading["ts"]) / 1000))
        is_stale = idade_segundos > stale_limit_seconds

        status = STATUS_STALE if is_stale else STATUS_OK
        msg = f"Amostra com {idade_segundos}s (acima do limite de {stale_limit_seconds}s)." if is_stale else "Operação normal e recente."

        return {
            "xid": clean_xid,
            "status": status,
            "mensagem": msg,
            "data_point_id": reading["data_point_id"],
            "point_name": meta["point_name"],
            "data_type": reading["data_type"],
            "valor": reading["value"],
            "str_valor": reading["str_value"],
            "timestamp": reading["datetime"],
            "idade_segundos": idade_segundos,
        }
