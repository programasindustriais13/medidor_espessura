# Espessura fornecida pelo PLC

O modo de aquisição padrão usa `DP_578391` (D4) para espessura e `DP_648291` para posição X. O Scada-LTS já entrega `DP_578391` em milímetros; o Django valida e utiliza esse número diretamente, sem divisão adicional. A indicação instantânea independe do gatilho `DP_887366`; o histórico recebe somente pares X/D4 coerentes dentro de uma varredura. `DP_632601` continua disponível na ferramenta de diagnóstico do Admin e `DP_747174` fica restrito ao modo JSON legado. Os dados históricos continuam armazenados e apresentados em milímetros, sem nova conversão.

## Validação necessária no equipamento

1. A medição física com bloco padrão de 20 mm mostrou `20.0` no Scada-LTS e `2,0 mm` no Django antes desta correção. Após a correção, conferir que a indicação Django mostra `20,0 mm` para a mesma leitura e acompanha as oscilações `19,9` e `20,1 mm` sem suavização.
2. Verificar separadamente no PLC/Scada-LTS se a medição satura em 20 mm. Se a tag não ultrapassar `20.0` para uma espessura física maior, a limitação é do escalonamento do PLC e não é corrigida no Django.
3. Confirmar os carimbos de tempo de X e D4 durante uma varredura. Pares com diferença superior à tolerância configurada não entram no histórico.

O valor `20.0` no Scada-LTS foi informado pelo teste físico. A saturação ainda não foi verificada. Neste ambiente, o MySQL Scada-LTS recusou a conexão do usuário `scada_monitor_ro` sem senha configurada, e a consulta REST direta retornou uma resposta que não era JSON. Não houve alteração no Ladder.

No banco SQLite local, a consulta de `LeituraScada` por `xid=DP_578391` retornou zero registros em 11/11/2026. Os 3.084 registros existentes usam `DP_747174` e não foram alterados. É preciso consultar separadamente qualquer outro banco de produção para identificar ciclos que tenham sido gravados pela versão anterior com a divisão indevida.
