from django.db import models


class ScadaDataPoint(models.Model):
    """
    Mapeamento da tabela 'datapoints' do Scada-LTS (somente-leitura).
    Armazena o cadastro e metadados de cada ponto de telemetria.
    """
    id = models.IntegerField(primary_key=True)
    xid = models.CharField(max_length=50, unique=True)
    data_source_id = models.IntegerField(db_column="dataSourceId")
    point_name = models.CharField(max_length=250, null=True, blank=True, db_column="pointName")
    plc_alarm_level = models.IntegerField(null=True, blank=True, db_column="plcAlarmLevel")

    class Meta:
        managed = False
        db_table = "datapoints"
        verbose_name = "Data Point (Scada-LTS)"
        verbose_name_plural = "Data Points (Scada-LTS)"
        ordering = ["xid"]

    def __str__(self):
        return f"{self.xid} ({self.point_name or 'Sem nome'})"


class ScadaPointValue(models.Model):
    """
    Mapeamento da tabela 'pointvalues' do Scada-LTS (somente-leitura).
    Tabela principal de séries temporais contendo histórico de medições.
    """
    id = models.BigAutoField(primary_key=True)
    data_point_id = models.IntegerField(db_column="dataPointId", db_index=True)
    data_type = models.IntegerField(db_column="dataType")
    point_value = models.FloatField(null=True, blank=True, db_column="pointValue")
    ts = models.BigIntegerField(db_index=True)

    class Meta:
        managed = False
        db_table = "pointvalues"
        verbose_name = "Point Value (Scada-LTS)"
        verbose_name_plural = "Point Values (Scada-LTS)"
        ordering = ["-ts", "-id"]

    def __str__(self):
        return f"DP {self.data_point_id}: {self.point_value} (ts={self.ts})"


class ScadaPointValueAnnotation(models.Model):
    """
    Mapeamento da tabela 'pointvalueannotations' do Scada-LTS (somente-leitura).
    Tabela auxiliar 1:1 utilizada quando a variável é do tipo texto/string (dataType = 4).
    """
    point_value_id = models.BigIntegerField(primary_key=True, db_column="pointValueId")
    text_short = models.CharField(
        max_length=128, null=True, blank=True, db_column="textPointValueShort"
    )
    text_long = models.TextField(null=True, blank=True, db_column="textPointValueLong")

    class Meta:
        managed = False
        db_table = "pointvalueannotations"
        verbose_name = "Point Value Annotation (Scada-LTS)"
        verbose_name_plural = "Point Value Annotations (Scada-LTS)"

    def __str__(self):
        return f"Annotation PV {self.point_value_id}: {self.text_short or (self.text_long[:30] if self.text_long else '')}"
