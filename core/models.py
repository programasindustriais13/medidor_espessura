from django.db import models

class LeituraScada(models.Model):
    # Identificador único do ciclo de escaneamento (ex: timestamp do início)
    scan_id = models.CharField(max_length=50, verbose_name="ID do Ciclo", db_index=True, null=True, blank=True)
    
    xid = models.CharField(max_length=100, verbose_name="Identificador do Ponto")
    eixo_x = models.FloatField(verbose_name="Largura (X)")
    eixo_y = models.FloatField(verbose_name="Espessura (Y)")
    data_leitura = models.DateTimeField(verbose_name="Data da Leitura")
    criado_em = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")

    class Meta:
        verbose_name = "Leitura Scada"
        verbose_name_plural = "Leituras Scada"
        ordering = ['data_leitura'] # Ordem crescente para desenhar o perfil corretamente

    def __str__(self):
        return f"Scan {self.scan_id} - {self.xid} - {self.data_leitura}"
