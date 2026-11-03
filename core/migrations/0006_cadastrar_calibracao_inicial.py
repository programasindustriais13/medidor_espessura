from django.db import migrations


def cadastrar_calibracao_inicial(apps, schema_editor):
    CalibracaoEspessura = apps.get_model("core", "CalibracaoEspessura")
    PontoCalibracao = apps.get_model("core", "PontoCalibracao")

    # Verifica idempotência
    descricao = "Calibração Inicial Multiponto - Padrão 30mm"
    calib, created = CalibracaoEspessura.objects.get_or_create(
        descricao=descricao,
        defaults={
            "espessura_bloco_padrao_mm": 30.0,
            "ativa": True,
            "responsavel": "Engenharia de Instrumentação / Metrologia",
            "observacoes": (
                "Calibração multiponto inicial com bloco padrão de 30,00 mm para compensação "
                "do zero mecânico ao longo da base de fixação por vácuo (posições 80 a 543 mm)."
            ),
        },
    )

    # Desativa outras calibrações se esta for ativada
    CalibracaoEspessura.objects.exclude(pk=calib.pk).update(ativa=False)

    pontos_dados = [
        {
            "posicao_x": 80.0,
            "leituras_zero_input": "345, 346",
            "leituras_bloco_input": "778, 780",
            "leituras_zero_list": [345.0, 346.0],
            "leituras_bloco_list": [778.0, 780.0],
            "zero_medio": 345.5,
            "bloco_medio": 779.0,
            "diferenca": 433.5,
        },
        {
            "posicao_x": 170.0,
            "leituras_zero_input": "350",
            "leituras_bloco_input": "783",
            "leituras_zero_list": [350.0],
            "leituras_bloco_list": [783.0],
            "zero_medio": 350.0,
            "bloco_medio": 783.0,
            "diferenca": 433.0,
        },
        {
            "posicao_x": 260.0,
            "leituras_zero_input": "348",
            "leituras_bloco_input": "782",
            "leituras_zero_list": [348.0],
            "leituras_bloco_list": [782.0],
            "zero_medio": 348.0,
            "bloco_medio": 782.0,
            "diferenca": 434.0,
        },
        {
            "posicao_x": 357.0,
            "leituras_zero_input": "345, 346",
            "leituras_bloco_input": "778, 780",
            "leituras_zero_list": [345.0, 346.0],
            "leituras_bloco_list": [778.0, 780.0],
            "zero_medio": 345.5,
            "bloco_medio": 779.0,
            "diferenca": 433.5,
        },
        {
            "posicao_x": 450.0,
            "leituras_zero_input": "341, 342",
            "leituras_bloco_input": "775",
            "leituras_zero_list": [341.0, 342.0],
            "leituras_bloco_list": [775.0],
            "zero_medio": 341.5,
            "bloco_medio": 775.0,
            "diferenca": 433.5,
        },
        {
            "posicao_x": 543.0,
            "leituras_zero_input": "337, 338",
            "leituras_bloco_input": "771",
            "leituras_zero_list": [337.0, 338.0],
            "leituras_bloco_list": [771.0],
            "zero_medio": 337.5,
            "bloco_medio": 771.0,
            "diferenca": 433.5,
        },
    ]

    difs = []
    for pd in pontos_dados:
        ponto, _ = PontoCalibracao.objects.update_or_create(
            calibracao=calib,
            posicao_x=pd["posicao_x"],
            defaults=pd,
        )
        difs.append(pd["diferenca"])

    if difs:
        media_dif = sum(difs) / len(difs)
        calib.fator_conversao = calib.espessura_bloco_padrao_mm / media_dif
        calib.ativa = True
        calib.save()


def reverter_calibracao_inicial(apps, schema_editor):
    CalibracaoEspessura = apps.get_model("core", "CalibracaoEspessura")
    descricao = "Calibração Inicial Multiponto - Padrão 30mm"
    CalibracaoEspessura.objects.filter(descricao=descricao).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_calibracaoespessura_leiturascada_fator_conversao_and_more"),
    ]

    operations = [
        migrations.RunPython(cadastrar_calibracao_inicial, reverter_calibracao_inicial),
    ]

