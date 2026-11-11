# Espessura fornecida pelo PLC

O modo de aquisição padrão usa `DP_578391` (D4) para espessura e `DP_648291` para posição X. O coletor converte D4 de décimos de milímetro para milímetros uma vez, ao adquirir a leitura. A indicação instantânea independe do gatilho `DP_887366`; o histórico recebe somente pares X/D4 coerentes dentro de uma varredura. `DP_632601` continua disponível na ferramenta de diagnóstico do Admin e `DP_747174` fica restrito ao modo JSON legado. Os dados históricos continuam armazenados e apresentados em milímetros, sem nova conversão.

## Validação necessária no equipamento

1. Consultar o valor numérico bruto de `DP_578391` no diagnóstico do Scada-LTS durante a medição com bloco padrão de 20 mm. Confirmar que a tag entrega aproximadamente `199`, `200` e `201`, sem escala adicional no supervisório. A indicação Django deve mostrar `19,9`, `20,0` e `20,1 mm`.
2. Verificar no PLC/Scada-LTS se D4 é limitado a `200` (20 mm). Se a tag nunca ultrapassar `200` para uma espessura física maior, a medição estará saturada em 20 mm. Essa limitação é do escalonamento do PLC e não é corrigida no Django.
3. Confirmar os carimbos de tempo de X e D4 durante uma varredura. Pares com diferença superior à tolerância configurada não entram no histórico.

Não foi possível confirmar a escala real nem a saturação neste ambiente: o MySQL Scada-LTS recusou a conexão do usuário `scada_monitor_ro` sem senha configurada. Não houve alteração no Ladder.
