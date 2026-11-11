# Espessura fornecida pelo PLC

O modo de aquisição padrão usa `DP_578391` (D4) para espessura e `DP_648291` para posição X. O Scada-LTS já entrega `DP_578391` em milímetros; o Django valida e utiliza esse número diretamente, sem divisão adicional. A indicação instantânea independe do gatilho `DP_887366`; o histórico recebe somente pares X/D4 coerentes dentro de uma varredura. `DP_632601` continua disponível na ferramenta de diagnóstico do Admin e `DP_747174` fica restrito ao modo JSON legado. Os dados históricos continuam armazenados e apresentados em milímetros, sem nova conversão.

## Validação necessária no equipamento

1. A medição física com bloco padrão de 20 mm mostrou `20.0` no Scada-LTS e `2,0 mm` no Django antes desta correção. Após a correção, conferir que a indicação Django mostra `20,0 mm` para a mesma leitura e acompanha as oscilações `19,9` e `20,1 mm` sem suavização.
2. Verificar separadamente no PLC/Scada-LTS se a medição satura em 20 mm. Se a tag não ultrapassar `20.0` para uma espessura física maior, a limitação é do escalonamento do PLC e não é corrigida no Django.
3. Confirmar os carimbos de tempo de X e D4 durante uma varredura. Pares com diferença superior à tolerância configurada não entram no histórico.

O valor `20.0` no Scada-LTS foi informado pelo teste físico. A saturação ainda não foi verificada. Neste ambiente, o MySQL Scada-LTS recusou a conexão do usuário `scada_monitor_ro` sem senha configurada, e a consulta REST direta retornou uma resposta que não era JSON. Não houve alteração no Ladder.

No banco SQLite local, a consulta de `LeituraScada` por `xid=DP_578391` retornou zero registros em 11/11/2026. Os 3.084 registros existentes usam `DP_747174` e não foram alterados. É preciso consultar separadamente qualquer outro banco de produção para identificar ciclos que tenham sido gravados pela versão anterior com a divisão indevida.

## Análise robusta após a varredura

As leituras X/Y originais continuam no histórico, no gráfico, na classificação por ponto e no CSV. O resumo dimensional é calculado após o ciclo:

- O corte mínimo configurado define a extensão da peça. Dentro dessa extensão, leituras até 0,2 mm abaixo do corte entram na análise; assim, a oscilação de 0,9 mm sobre um bloco de 1 mm não é descartada por um corte de 1,0 mm.
- Leituras repetidas na mesma posição X são representadas por sua mediana, sem depender da ordem de chegada. As rampas nas extremidades dos patamares são excluídas das médias.
- Uma posição com desvio de pelo menos 0,3 mm em relação às posições vizinhas é sinalizada quando os vizinhos concordam entre si até 0,2 mm. Uma leitura divergente entre pelo menos três leituras da mesma posição também é sinalizada. Transições sustentadas entre patamares permanecem.
- `espessura_media_coroa` e as médias laterais usam as posições aceitas. A mediana da coroa e o número de leituras suspeitas aparecem no resumo; os metadados são gravados em `parametros_calculo.analise_robusta`. Registros de resumo já persistidos não são recalculados automaticamente.

Os limiares de 0,2 e 0,3 mm são critérios iniciais para o ensaio apresentado, não uma garantia de que toda leitura isolada seja erro. Uma característica física estreita também pode produzir uma leitura isolada. Nenhuma leitura original é apagada ou alterada.

Para atualizar o resumo de uma varredura já concluída, execute `python manage.py recalcular_dimensoes SCAN_20261111_115733` no ambiente que contém esse ciclo. O comando recalcula apenas `ResultadoDimensionalScan` para o ID informado; as linhas de `LeituraScada` permanecem inalteradas. Se o ID não existir nesse banco, o comando informa o erro sem criar um resumo.
