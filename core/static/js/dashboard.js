document.addEventListener('DOMContentLoaded', function () {
    const ctx = document.getElementById('profileChart').getContext('2d');

    // Elementos de UI do Header
    const scanSelect = document.getElementById('scan-select');
    const scanSearch = document.getElementById('scan-search');
    const scanSearchSubmit = document.getElementById('scan-search-submit');
    const scanSearchResults = document.getElementById('scan-search-results');
    const scanSearchStatus = document.getElementById('scan-search-status');
    const scanSearchWrapper = document.querySelector('.scan-search-wrapper');
    const materialName = document.getElementById('material-name');
    const saveMaterialName = document.getElementById('save-material-name');
    const materialSaveStatus = document.getElementById('material-save-status');

    const statusText = document.getElementById('scan-status-text');
    const scanIdText = document.getElementById('current-scan-id');
    const headerScanIdText = document.getElementById('header-scan-id');
    const exportBtn = document.getElementById('export-excel');

    // Elementos de Largura / Tamanho do Material
    const widthValElem = document.getElementById('material-width-val');
    const widthSubtextElem = document.getElementById('material-range-subtext');

    // Elementos do Resumo Dimensional Automático da Banda
    const dimStatusBadge = document.getElementById('dim-status-badge');
    const dimStatusLabel = document.getElementById('dim-status-label');
    const dimLarguraTotal = document.getElementById('dim-largura-total');
    const dimLarguraTotalSub = document.getElementById('dim-largura-total-sub');
    const dimLarguraCoroa = document.getElementById('dim-largura-coroa');
    const dimLarguraCoroaSub = document.getElementById('dim-largura-coroa-sub');
    const dimEspMediaCoroa = document.getElementById('dim-esp-media-coroa');
    const dimEspMediaCoroaSub = document.getElementById('dim-esp-media-coroa-sub');
    const dimEspLateralMedia = document.getElementById('dim-esp-lateral-media');
    const dimEspLateralSub = document.getElementById('dim-esp-lateral-sub');
    const dimMotivoBox = document.getElementById('dim-motivo-box');

    // Botões de Controle do Gráfico
    const btnToggleCotas = document.getElementById('btn-toggle-cotas');
    const statusCotasTxt = document.getElementById('status-cotas-txt');
    const btnTogglePosicao = document.getElementById('btn-toggle-posicao');
    const statusPosicaoTxt = document.getElementById('status-posicao-txt');
    const btnToggleEscala = document.getElementById('btn-toggle-escala');
    const statusEscalaTxt = document.getElementById('status-escala-txt');

    // Elementos do Indicador Permanente de Espessura Atual
    const liveThicknessVal = document.getElementById('live-thickness-val');
    const livePosX = document.getElementById('live-pos-x');
    const liveRawInfo = document.getElementById('live-raw-info');
    const liveStatusBadge = document.getElementById('live-status-badge');
    const liveStatusDot = document.getElementById('live-status-dot');
    const liveStatusLabel = document.getElementById('live-status-label');
    const liveIndicatorDot = document.getElementById('live-indicator-dot');

    // Estados de Exibição
    let showTechnicalDimensions = true;
    let thicknessPlacement = 'above'; // 'above' (Acima da Linha) ou 'below' (Abaixo da Linha)
    let scaleMode = 'optimized'; // 'optimized' (zoom limpo no perfil) ou 'expanded' (30mm)

    let currentChart = null;
    let selectedScanId = null;
    let searchTimer = null;
    let searchRequestId = 0;

    function csrfToken() {
        const cookie = document.cookie.split('; ').find(part => part.startsWith('csrftoken='));
        return cookie ? decodeURIComponent(cookie.split('=').slice(1).join('=')) : '';
    }

    function updateMaterialField(json) {
        const target = selectedScanId || (json.is_live_cycle ? json.scan_id : 'pending');
        const value = target === 'pending' ? json.nome_material_proximo_ciclo : json.nome_material;
        if (materialName.dataset.target !== target || materialName.dataset.dirty !== 'true') {
            materialName.value = value || '';
            materialName.dataset.dirty = 'false';
        }
        materialName.dataset.target = target || 'pending';
        materialName.placeholder = target === 'pending' ? 'Nome para o próximo ciclo' : 'Nome deste ciclo';
    }

    // Função utilitária para desenhar retângulos arredondados no canvas
    function drawRoundedRect(canvasCtx, x, y, width, height, radius) {
        canvasCtx.beginPath();
        canvasCtx.moveTo(x + radius, y);
        canvasCtx.lineTo(x + width - radius, y);
        canvasCtx.quadraticCurveTo(x + width, y, x + width, y + radius);
        canvasCtx.lineTo(x + width, y + height - radius);
        canvasCtx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
        canvasCtx.lineTo(x + radius, y + height);
        canvasCtx.quadraticCurveTo(x, y + height, x, y + height - radius);
        canvasCtx.lineTo(x, y + radius);
        canvasCtx.quadraticCurveTo(x, y, x + radius, y);
        canvasCtx.closePath();
    }

    // Função utilitária para desenhar pontas de setas dimensionais
    function drawArrowHead(c, x, y, angle, size = 5.5, color = '#38bdf8') {
        c.save();
        c.translate(x, y);
        c.rotate(angle);
        c.beginPath();
        c.moveTo(0, 0);
        c.lineTo(-size, -size * 0.45);
        c.lineTo(-size, size * 0.45);
        c.closePath();
        c.fillStyle = color;
        c.fill();
        c.restore();
    }

    // Desenha badge flutuante de cota dimensional com fundo sólido escuro para não vazar linhas
    function drawDimensionBadge(c, text, centerX, centerY, bgColor, textColor, borderColor) {
        c.save();
        c.font = 'bold 10.5px Inter, sans-serif';
        c.textAlign = 'center';
        c.textBaseline = 'middle';
        const textWidth = c.measureText(text).width;
        const padX = 7;
        const padY = 3;
        const w = textWidth + padX * 2;
        const h = 18;
        const bx = centerX - w / 2;
        const by = centerY - h / 2;

        c.fillStyle = bgColor;
        c.strokeStyle = borderColor || 'transparent';
        c.lineWidth = 1;
        drawRoundedRect(c, bx, by, w, h, 4);
        c.fill();
        if (borderColor) c.stroke();

        c.fillStyle = textColor;
        c.fillText(text, centerX, centerY);
        c.restore();
    }

    function selectScan(scanId) {
        selectedScanId = scanId;
        if (scanId && !Array.from(scanSelect.options).some(opt => opt.value === scanId)) {
            scanSelect.add(new Option(scanId.replace('SCAN_', '') + ' (Histórico)', scanId));
        }
        scanSelect.value = scanId;
        scanSearchResults.hidden = true;
        materialSaveStatus.textContent = '';
        currentChart.data.datasets.forEach(d => d.data = []);
        currentChart.update();
        fetchData();
    }

    function showSearchResults(items, total) {
        scanSearchResults.replaceChildren();
        if (!items.length) {
            scanSearchResults.hidden = true;
            scanSearchStatus.textContent = 'Nenhum ciclo encontrado. Para buscar por material, salve o nome no ciclo.';
            return;
        }
        const limitMessage = total > 20 ? ' Exibindo os primeiros 20; os demais estão no seletor.' : '';
        scanSearchStatus.textContent = `${total} ciclo${total === 1 ? '' : 's'} encontrado${total === 1 ? '' : 's'}. Clique em um resultado ou pressione Enter.${limitMessage}`;
        items.slice(0, 20).forEach(item => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'scan-search-result';
            const title = document.createElement('strong');
            title.textContent = item.nome_material || 'Material sem nome';
            const details = document.createElement('small');
            const date = item.data_hora ? new Date(item.data_hora).toLocaleString('pt-BR') : '';
            details.textContent = `${date ? date + ' · ' : ''}${item.scan_id}`;
            button.append(title, details);
            button.addEventListener('click', () => selectScan(item.scan_id));
            scanSearchResults.appendChild(button);
        });
        scanSearchResults.hidden = false;
    }

    // Carrega a lista e apresenta resultados clicáveis quando há texto de busca.
    async function loadScanList() {
        const query = scanSearch.value.trim();
        const requestId = ++searchRequestId;
        if (query) scanSearchStatus.textContent = 'Buscando...';
        try {
            const res = await fetch(`/api/scans/${query ? `?q=${encodeURIComponent(query)}` : ''}`);
            if (!res.ok) throw new Error('Falha ao consultar o histórico.');
            const json = await res.json();
            if (requestId !== searchRequestId || query !== scanSearch.value.trim()) return [];
            const items = Array.isArray(json.items) ? json.items : [];

            scanSelect.innerHTML = '<option value="">-- Monitoramento Ao Vivo --</option>';
            items.forEach(item => {
                const opt = document.createElement('option');
                opt.value = item.scan_id;
                opt.textContent = `${item.nome_material ? `${item.nome_material} — ` : ''}${item.scan_id.replace('SCAN_', '')} (Histórico)`;
                scanSelect.appendChild(opt);
            });
            if (selectedScanId && !Array.from(scanSelect.options).some(opt => opt.value === selectedScanId)) {
                scanSelect.add(new Option(`${selectedScanId.replace('SCAN_', '')} (Selecionado)`, selectedScanId));
            }
            scanSelect.value = selectedScanId || '';
            if (query) showSearchResults(items, json.total ?? items.length);
            else {
                scanSearchResults.hidden = true;
                scanSearchStatus.textContent = '';
            }
            return items;
        } catch (e) {
            if (requestId === searchRequestId) {
                scanSearchResults.hidden = true;
                scanSearchStatus.textContent = 'Erro ao buscar ciclos. Tente novamente.';
            }
            console.error('Erro ao buscar ciclos', e);
            return [];
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // PLUGIN 1: Tracker de Ponto Atual em Tempo Real (Apenas Durante Varredura)
    // ─────────────────────────────────────────────────────────────────────────
    const realtimeTrackerPlugin = {
        id: 'realtimeTracker',
        afterDatasetsDraw(chart) {
            const tracker = chart.options?.plugins?.realtimeTracker;
            if (!tracker || !tracker.active || !tracker.point) return;

            const { ctx: c, chartArea, scales: { x, y } } = chart;
            const pt = tracker.point;

            if (pt.x === undefined || pt.y === undefined) return;

            const px = x.getPixelForValue(pt.x);
            const py = y.getPixelForValue(pt.y);

            if (isNaN(px) || isNaN(py) || px < chartArea.left - 5 || px > chartArea.right + 5 || py < chartArea.top - 5 || py > chartArea.bottom + 5) {
                return;
            }

            c.save();

            const boxWidth = 145;
            const boxHeight = 44;
            const boxY = chartArea.top + 8;

            let boxX = px - boxWidth / 2;
            if (boxX < chartArea.left + 8) boxX = chartArea.left + 8;
            if (boxX + boxWidth > chartArea.right - 8) boxX = chartArea.right - boxWidth - 8;

            const lineEndY = boxY + boxHeight;
            c.beginPath();
            c.setLineDash([4, 4]);
            c.strokeStyle = tracker.isScanning ? '#38bdf8' : '#94a3b8';
            c.lineWidth = 1.8;
            c.moveTo(px, py);
            c.lineTo(px, lineEndY);
            c.stroke();

            c.setLineDash([]);
            c.beginPath();
            c.arc(px, py, 7, 0, 2 * Math.PI);
            c.fillStyle = 'rgba(56, 189, 248, 0.35)';
            c.fill();

            c.beginPath();
            c.arc(px, py, 4, 0, 2 * Math.PI);
            c.fillStyle = '#38bdf8';
            c.strokeStyle = '#ffffff';
            c.lineWidth = 1.5;
            c.fill();
            c.stroke();

            c.fillStyle = '#0f172a';
            c.strokeStyle = '#38bdf8';
            c.lineWidth = 1.5;

            drawRoundedRect(c, boxX, boxY, boxWidth, boxHeight, 6);
            c.fill();
            c.stroke();

            const pointerX = Math.min(Math.max(px, boxX + 12), boxX + boxWidth - 12);
            c.beginPath();
            c.moveTo(pointerX - 5, boxY + boxHeight);
            c.lineTo(pointerX + 5, boxY + boxHeight);
            c.lineTo(pointerX, boxY + boxHeight + 5);
            c.closePath();
            c.fillStyle = '#38bdf8';
            c.fill();

            c.textAlign = 'center';
            c.font = '600 9px Inter, sans-serif';
            c.fillStyle = '#94a3b8';
            const headerLabel = tracker.isScanning ? '● MEDIÇÃO ATUAL' : 'ÚLTIMA MEDIÇÃO';
            c.fillText(headerLabel, boxX + boxWidth / 2, boxY + 14);

            c.font = 'bold 16px Inter, sans-serif';
            c.fillStyle = '#f8fafc';
            c.fillText(`${pt.y.toFixed(2)} mm`, boxX + boxWidth / 2, boxY + 33);

            c.restore();
        }
    };

    // ─────────────────────────────────────────────────────────────────────────
    // PLUGIN 2: Cotas Técnicas Dimensionais Sem Linhas de Limites e Valores Fora da Linha
    // ─────────────────────────────────────────────────────────────────────────
    const technicalDimensionsPlugin = {
        id: 'technicalDimensions',
        afterDatasetsDraw(chart) {
            if (!showTechnicalDimensions) return;

            const pluginOpts = chart.options?.plugins?.technicalDimensions;
            if (!pluginOpts || !pluginOpts.active || !pluginOpts.dimensoes) return;

            const dim = pluginOpts.dimensoes;
            if (dim.status !== 'CONCLUIDO') return;

            const { ctx: c, chartArea, scales: { x, y } } = chart;

            const xEsq = dim.x_esquerda;
            const xDir = dim.x_direita;
            const xIniCoroa = dim.x_inicio_coroa;
            const xFimCoroa = dim.x_fim_coroa;
            const espMediaCoroa = dim.espessura_media_coroa;
            const espLatEsq = dim.espessura_lateral_esq;
            const espLatDir = dim.espessura_lateral_dir;

            if (xEsq === null || xDir === null) return;

            const pxEsq = x.getPixelForValue(xEsq);
            const pxDir = x.getPixelForValue(xDir);

            if (isNaN(pxEsq) || isNaN(pxDir) || pxDir <= pxEsq) return;

            c.save();

            // Faixas horizontais para as cotas superiores (topo da área de desenho)
            const yTopTotal = chartArea.top + 16;
            const yTopCoroa = chartArea.top + 38;
            const yWitnessBase = chartArea.top + 46;

            // 1. Cota Horizontal: LARGURA TOTAL DA BANDA (Faixa 1 - Topo)
            const corTotal = '#38bdf8';
            // Linhas de chamada curtas no topo (sem descer para o perfil)
            c.beginPath();
            c.strokeStyle = 'rgba(56, 189, 248, 0.35)';
            c.lineWidth = 1;
            c.moveTo(pxEsq, yWitnessBase);
            c.lineTo(pxEsq, yTopTotal - 5);
            c.moveTo(pxDir, yWitnessBase);
            c.lineTo(pxDir, yTopTotal - 5);
            c.stroke();

            // Linha da cota com setas
            c.beginPath();
            c.strokeStyle = corTotal;
            c.lineWidth = 1.4;
            c.moveTo(pxEsq, yTopTotal);
            c.lineTo(pxDir, yTopTotal);
            c.stroke();

            drawArrowHead(c, pxEsq, yTopTotal, Math.PI, 5.5, corTotal);
            drawArrowHead(c, pxDir, yTopTotal, 0, 5.5, corTotal);

            const midXTotal = (pxEsq + pxDir) / 2;
            drawDimensionBadge(c, `Largura Total: ${dim.largura_total.toFixed(1)} mm`, midXTotal, yTopTotal, '#0b1329', corTotal, 'rgba(56, 189, 248, 0.5)');

            // 2. Cota Horizontal: LARGURA DA COROA (Faixa 2)
            if (xIniCoroa !== null && xFimCoroa !== null && dim.largura_coroa !== null) {
                const pxCoroaIni = x.getPixelForValue(xIniCoroa);
                const pxCoroaFim = x.getPixelForValue(xFimCoroa);
                const corCoroa = '#c084fc';

                if (pxCoroaFim > pxCoroaIni + 15) {
                    // Linhas de chamada curtas no topo
                    c.beginPath();
                    c.strokeStyle = 'rgba(192, 132, 252, 0.35)';
                    c.lineWidth = 1;
                    c.moveTo(pxCoroaIni, yWitnessBase);
                    c.lineTo(pxCoroaIni, yTopCoroa - 5);
                    c.moveTo(pxCoroaFim, yWitnessBase);
                    c.lineTo(pxCoroaFim, yTopCoroa - 5);
                    c.stroke();

                    // Linha da cota com setas
                    c.beginPath();
                    c.strokeStyle = corCoroa;
                    c.lineWidth = 1.4;
                    c.moveTo(pxCoroaIni, yTopCoroa);
                    c.lineTo(pxCoroaFim, yTopCoroa);
                    c.stroke();

                    drawArrowHead(c, pxCoroaIni, yTopCoroa, Math.PI, 5.5, corCoroa);
                    drawArrowHead(c, pxCoroaFim, yTopCoroa, 0, 5.5, corCoroa);

                    const midXCoroa = (pxCoroaIni + pxCoroaFim) / 2;
                    drawDimensionBadge(c, `Largura Coroa: ${dim.largura_coroa.toFixed(1)} mm`, midXCoroa, yTopCoroa, '#0b1329', corCoroa, 'rgba(192, 132, 252, 0.5)');
                }
            }

            // 3. ESPESSURA MÉDIA DA COROA (mm) — Posicionada com folga clara (Acima ou Abaixo), SEM tocar na linha vermelha
            if (espMediaCoroa !== null && xIniCoroa !== null && xFimCoroa !== null) {
                const pxCoroaIni = x.getPixelForValue(xIniCoroa);
                const pxCoroaFim = x.getPixelForValue(xFimCoroa);
                const pyCoroaMedia = y.getPixelForValue(espMediaCoroa);

                if (!isNaN(pyCoroaMedia) && pyCoroaMedia >= chartArea.top && pyCoroaMedia <= chartArea.bottom) {
                    // Linha guia discreta no nível médio físico da coroa
                    c.beginPath();
                    c.setLineDash([3, 4]);
                    c.strokeStyle = 'rgba(34, 197, 94, 0.45)';
                    c.lineWidth = 1.1;
                    c.moveTo(pxCoroaIni, pyCoroaMedia);
                    c.lineTo(pxCoroaFim, pyCoroaMedia);
                    c.stroke();

                    // Batentes verticais nos limites da coroa
                    c.setLineDash([]);
                    c.beginPath();
                    c.strokeStyle = '#22c55e';
                    c.lineWidth = 1.4;
                    c.moveTo(pxCoroaIni, pyCoroaMedia - 4);
                    c.lineTo(pxCoroaIni, pyCoroaMedia + 4);
                    c.moveTo(pxCoroaFim, pyCoroaMedia - 4);
                    c.lineTo(pxCoroaFim, pyCoroaMedia + 4);
                    c.stroke();

                    const midXCoroaVal = (pxCoroaIni + pxCoroaFim) / 2;
                    let badgeYCoroa;

                    if (thicknessPlacement === 'below') {
                        // Posiciona ABAIXO da linha vermelha (na área livre entre 0 e a coroa)
                        badgeYCoroa = Math.min(chartArea.bottom - 16, pyCoroaMedia + 38);

                        // Haste indicadora vertical apontando para CIMA em direção ao nível médio
                        c.beginPath();
                        c.strokeStyle = 'rgba(34, 197, 94, 0.7)';
                        c.lineWidth = 1.2;
                        c.moveTo(midXCoroaVal, badgeYCoroa - 9);
                        c.lineTo(midXCoroaVal, pyCoroaMedia + 2);
                        c.stroke();

                        // Seta apontando para cima
                        drawArrowHead(c, midXCoroaVal, pyCoroaMedia, -Math.PI / 2, 4.5, '#22c55e');
                    } else {
                        // Posiciona ACIMA da linha vermelha (com folga ampla e limpa)
                        badgeYCoroa = pyCoroaMedia - 34;

                        // Haste indicadora vertical apontando para BAIXO em direção ao nível médio
                        c.beginPath();
                        c.strokeStyle = 'rgba(34, 197, 94, 0.7)';
                        c.lineWidth = 1.2;
                        c.moveTo(midXCoroaVal, badgeYCoroa + 9);
                        c.lineTo(midXCoroaVal, pyCoroaMedia - 2);
                        c.stroke();

                        // Seta apontando para baixo
                        drawArrowHead(c, midXCoroaVal, pyCoroaMedia, Math.PI / 2, 4.5, '#22c55e');
                    }

                    // Badge com o valor da Espessura Média
                    drawDimensionBadge(
                        c,
                        `ESP. MÉDIA COROA: ${espMediaCoroa.toFixed(2)} mm`,
                        midXCoroaVal,
                        badgeYCoroa,
                        '#0b1329',
                        '#86efac',
                        '#22c55e'
                    );
                }
            }

            // 4. ESPESSURA LATERAL ESQUERDA — Posicionada com folga clara (Acima ou Abaixo), SEM tocar na linha vermelha
            if (espLatEsq !== null && xIniCoroa !== null) {
                const pxLatIni = pxEsq;
                const pxLatFim = x.getPixelForValue(xIniCoroa);
                const pyLat = y.getPixelForValue(espLatEsq);

                if (!isNaN(pyLat) && pyLat <= chartArea.bottom && pxLatFim > pxLatIni + 15) {
                    // Linha guia discreta no nível lateral esquerdo
                    c.beginPath();
                    c.setLineDash([3, 4]);
                    c.strokeStyle = 'rgba(245, 158, 11, 0.45)';
                    c.lineWidth = 1.1;
                    c.moveTo(pxLatIni, pyLat);
                    c.lineTo(pxLatFim, pyLat);
                    c.stroke();

                    c.setLineDash([]);
                    const midLatEsq = (pxLatIni + pxLatFim) / 2;
                    let badgeYLat;

                    if (thicknessPlacement === 'below') {
                        badgeYLat = Math.min(chartArea.bottom - 16, pyLat + 28);

                        c.beginPath();
                        c.strokeStyle = 'rgba(245, 158, 11, 0.7)';
                        c.lineWidth = 1.2;
                        c.moveTo(midLatEsq, badgeYLat - 9);
                        c.lineTo(midLatEsq, pyLat + 2);
                        c.stroke();

                        drawArrowHead(c, midLatEsq, pyLat, -Math.PI / 2, 4.5, '#f59e0b');
                    } else {
                        badgeYLat = pyLat - 30;

                        c.beginPath();
                        c.strokeStyle = 'rgba(245, 158, 11, 0.7)';
                        c.lineWidth = 1.2;
                        c.moveTo(midLatEsq, badgeYLat + 9);
                        c.lineTo(midLatEsq, pyLat - 2);
                        c.stroke();

                        drawArrowHead(c, midLatEsq, pyLat, Math.PI / 2, 4.5, '#f59e0b');
                    }

                    drawDimensionBadge(
                        c,
                        `Esp. Lateral: ${espLatEsq.toFixed(2)} mm`,
                        midLatEsq,
                        badgeYLat,
                        '#0b1329',
                        '#fbbf24',
                        '#f59e0b'
                    );
                }
            }

            // 5. ESPESSURA LATERAL DIREITA — Posicionada com folga clara (Acima ou Abaixo), SEM tocar na linha vermelha
            if (espLatDir !== null && xFimCoroa !== null) {
                const pxLatDirIni = x.getPixelForValue(xFimCoroa);
                const pxLatDirFim = pxDir;
                const pyLatDir = y.getPixelForValue(espLatDir);

                if (!isNaN(pyLatDir) && pyLatDir <= chartArea.bottom && pxLatDirFim > pxLatDirIni + 15) {
                    c.beginPath();
                    c.setLineDash([3, 4]);
                    c.strokeStyle = 'rgba(245, 158, 11, 0.45)';
                    c.lineWidth = 1.1;
                    c.moveTo(pxLatDirIni, pyLatDir);
                    c.lineTo(pxLatDirFim, pyLatDir);
                    c.stroke();

                    c.setLineDash([]);
                    const midLatDir = (pxLatDirIni + pxLatDirFim) / 2;
                    let badgeYLatDir;

                    if (thicknessPlacement === 'below') {
                        badgeYLatDir = Math.min(chartArea.bottom - 16, pyLatDir + 28);

                        c.beginPath();
                        c.strokeStyle = 'rgba(245, 158, 11, 0.7)';
                        c.lineWidth = 1.2;
                        c.moveTo(midLatDir, badgeYLatDir - 9);
                        c.lineTo(midLatDir, pyLatDir + 2);
                        c.stroke();

                        drawArrowHead(c, midLatDir, pyLatDir, -Math.PI / 2, 4.5, '#f59e0b');
                    } else {
                        badgeYLatDir = pyLatDir - 30;

                        c.beginPath();
                        c.strokeStyle = 'rgba(245, 158, 11, 0.7)';
                        c.lineWidth = 1.2;
                        c.moveTo(midLatDir, badgeYLatDir + 9);
                        c.lineTo(midLatDir, pyLatDir - 2);
                        c.stroke();

                        drawArrowHead(c, midLatDir, pyLatDir, Math.PI / 2, 4.5, '#f59e0b');
                    }

                    drawDimensionBadge(
                        c,
                        `Esp. Lateral: ${espLatDir.toFixed(2)} mm`,
                        midLatDir,
                        badgeYLatDir,
                        '#0b1329',
                        '#fbbf24',
                        '#f59e0b'
                    );
                }
            }

            c.restore();
        }
    };

    // Configuração Inicial do Gráfico Chart.js (Apenas o perfil medido, SEM linhas de limites)
    function initChart() {
        currentChart = new Chart(ctx, {
            type: 'line',
            plugins: [realtimeTrackerPlugin, technicalDimensionsPlugin],
            data: {
                datasets: [
                    {
                        label: 'Perfil Medido',
                        data: [],
                        type: 'line',
                        borderColor: '#ef4444', // Linha contínua do perfil medido
                        borderWidth: 2.5,
                        pointRadius: 0,
                        pointHoverRadius: 6,
                        pointHitRadius: 10,
                        tension: 0,
                        fill: false,
                        order: 1
                    }
                ]
            },
            options: {
                animation: false,
                maintainAspectRatio: false,
                layout: {
                    padding: {
                        top: 15,
                        bottom: 5,
                        left: 10,
                        right: 10
                    }
                },
                scales: {
                    x: {
                        type: 'linear',
                        position: 'bottom',
                        title: { display: true, text: 'Largura (mm)', font: { size: 12 } },
                        grid: { color: '#334155' }
                    },
                    y: {
                        title: { display: true, text: 'Espessura (mm)', font: { size: 12 } },
                        min: 0,
                        max: 14,
                        grid: { color: '#334155' },
                        ticks: {
                            stepSize: 2
                        }
                    }
                },
                plugins: {
                    legend: {
                        display: false // Oculta a legenda de limites removida
                    },
                    realtimeTracker: {
                        active: false,
                        isScanning: false,
                        point: null
                    },
                    technicalDimensions: {
                        active: false,
                        dimensoes: null
                    },
                    tooltip: {
                        callbacks: {
                            label: function (context) {
                                const p = context.raw;
                                if (!p) return '';
                                return `Espessura: ${p.y.toFixed(2)} mm [${p.zona}]`;
                            },
                            afterLabel: function (context) {
                                const p = context.raw;
                                if (!p) return '';
                                return `Posição X: ${p.x.toFixed(2)} mm`;
                            }
                        }
                    }
                }
            }
        });
    }

    // Atualiza o painel superior de resumo dimensional
    function updateDimensionalUI(dim, isScanning) {
        if (!dim) {
            if (dimStatusBadge) {
                dimStatusBadge.style.background = '#334155';
                dimStatusBadge.style.color = '#94a3b8';
            }
            if (dimStatusLabel) dimStatusLabel.textContent = 'Aguardando Ciclo';
            if (dimLarguraTotal) dimLarguraTotal.textContent = '-- mm';
            if (dimLarguraTotalSub) dimLarguraTotalSub.textContent = 'X: -- a -- mm';
            if (dimLarguraCoroa) dimLarguraCoroa.textContent = '-- mm';
            if (dimLarguraCoroaSub) dimLarguraCoroaSub.textContent = 'X: -- a -- mm';
            if (dimEspMediaCoroa) dimEspMediaCoroa.textContent = '-- mm';
            if (dimEspMediaCoroaSub) dimEspMediaCoroaSub.textContent = 'Mín: -- | Máx: -- | -- pts';
            if (dimEspLateralMedia) dimEspLateralMedia.textContent = '-- mm';
            if (dimEspLateralSub) dimEspLateralSub.textContent = 'Esq: -- mm | Dir: -- mm';
            if (dimMotivoBox) dimMotivoBox.style.display = 'none';
            return;
        }

        const status = dim.status || 'DADOS_INSUFICIENTES';

        if (isScanning || status === 'EM_ANDAMENTO') {
            if (dimStatusBadge) {
                dimStatusBadge.style.background = 'rgba(56, 189, 248, 0.2)';
                dimStatusBadge.style.color = '#38bdf8';
                dimStatusBadge.style.border = '1px solid rgba(56, 189, 248, 0.4)';
            }
            if (dimStatusLabel) dimStatusLabel.textContent = '● Varredura em Andamento...';
            if (dimMotivoBox) {
                dimMotivoBox.style.display = 'block';
                dimMotivoBox.textContent = 'O cálculo dimensional e a espessura média serão apresentados automaticamente ao término do ciclo.';
            }
            return;
        }

        if (status === 'CONCLUIDO') {
            if (dimStatusBadge) {
                dimStatusBadge.style.background = 'rgba(34, 197, 94, 0.2)';
                dimStatusBadge.style.color = '#4ade80';
                dimStatusBadge.style.border = '1px solid rgba(34, 197, 94, 0.4)';
            }
            if (dimStatusLabel) dimStatusLabel.textContent = '✔ Ciclo Concluído com Sucesso';

            if (dimLarguraTotal) dimLarguraTotal.textContent = `${dim.largura_total.toFixed(1)} mm`;
            if (dimLarguraTotalSub) dimLarguraTotalSub.textContent = `X: ${dim.x_esquerda.toFixed(1)} a ${dim.x_direita.toFixed(1)} mm`;

            if (dimLarguraCoroa) dimLarguraCoroa.textContent = `${dim.largura_coroa.toFixed(1)} mm`;
            if (dimLarguraCoroaSub) dimLarguraCoroaSub.textContent = `X: ${dim.x_inicio_coroa.toFixed(1)} a ${dim.x_fim_coroa.toFixed(1)} mm`;

            if (dimEspMediaCoroa) dimEspMediaCoroa.textContent = `${dim.espessura_media_coroa.toFixed(2)} mm`;
            if (dimEspMediaCoroaSub) {
                const mediana = dim.mediana_coroa !== null && dim.mediana_coroa !== undefined
                    ? `Mediana: ${dim.mediana_coroa.toFixed(2)} mm | ` : '';
                dimEspMediaCoroaSub.textContent = `${mediana}Mín: ${dim.espessura_min_coroa.toFixed(2)} mm | Máx: ${dim.espessura_max_coroa.toFixed(2)} mm | ${dim.pontos_coroa} posições`;
            }

            if (dimEspLateralMedia) {
                const esq = dim.espessura_lateral_esq !== null ? dim.espessura_lateral_esq.toFixed(2) : '--';
                const dir = dim.espessura_lateral_dir !== null ? dim.espessura_lateral_dir.toFixed(2) : '--';
                dimEspLateralMedia.textContent = `${esq} / ${dir} mm`;
            }
            if (dimEspLateralSub) {
                const esq = dim.espessura_lateral_esq !== null ? `${dim.espessura_lateral_esq.toFixed(2)} mm` : 'Não detectada';
                const dir = dim.espessura_lateral_dir !== null ? `${dim.espessura_lateral_dir.toFixed(2)} mm` : 'Não detectada';
                const medEsq = dim.mediana_lateral_esq !== null && dim.mediana_lateral_esq !== undefined
                    ? ` (med. ${dim.mediana_lateral_esq.toFixed(2)})` : '';
                const medDir = dim.mediana_lateral_dir !== null && dim.mediana_lateral_dir !== undefined
                    ? ` (med. ${dim.mediana_lateral_dir.toFixed(2)})` : '';
                dimEspLateralSub.textContent = `Esq: ${esq}${medEsq} | Dir: ${dir}${medDir}`;
            }

            if (dimMotivoBox) {
                dimMotivoBox.style.display = 'block';
                const locais = Array.isArray(dim.posicoes_suspeitas) && dim.posicoes_suspeitas.length
                    ? ` em X=${dim.posicoes_suspeitas.slice(0, 5).join(', ')} mm` : '';
                const suspeitos = dim.pontos_suspeitos !== null && dim.pontos_suspeitos !== undefined
                    ? ` ${dim.pontos_suspeitos} leitura(s) suspeita(s)${locais} fora das médias; leituras brutas preservadas.` : '';
                dimMotivoBox.textContent = `Varredura concluída com ${dim.total_pontos} medições físicas. Coroa calculada com ${dim.pontos_coroa} posições do patamar.${suspeitos}`;
            }
        } else {
            const cores = {
                'DADOS_INSUFICIENTES': { bg: 'rgba(245, 158, 11, 0.2)', text: '#fbbf24', border: '#f59e0b', label: 'Dados Insuficientes' },
                'SEM_COROA_CLARA': { bg: 'rgba(168, 85, 247, 0.2)', text: '#c084fc', border: '#a855f7', label: 'Sem Coroa Identificável' },
                'INCOMPLETO': { bg: 'rgba(239, 68, 68, 0.2)', text: '#f87171', border: '#ef4444', label: 'Ciclo Incompleto' },
            };
            const cfg = cores[status] || { bg: '#334155', text: '#94a3b8', border: '#475569', label: status };

            if (dimStatusBadge) {
                dimStatusBadge.style.background = cfg.bg;
                dimStatusBadge.style.color = cfg.text;
                dimStatusBadge.style.border = `1px solid ${cfg.border}`;
            }
            if (dimStatusLabel) dimStatusLabel.textContent = cfg.label;

            if (dimLarguraTotal) {
                dimLarguraTotal.textContent = dim.largura_total !== null ? `${dim.largura_total.toFixed(1)} mm` : 'Não determinada';
            }
            if (dimLarguraTotalSub) {
                dimLarguraTotalSub.textContent = dim.x_esquerda !== null ? `X: ${dim.x_esquerda.toFixed(1)} a ${dim.x_direita.toFixed(1)} mm` : '';
            }

            if (dimLarguraCoroa) dimLarguraCoroa.textContent = 'Não determinada';
            if (dimLarguraCoroaSub) dimLarguraCoroaSub.textContent = 'Geometria de patamar não encontrada';

            if (dimEspMediaCoroa) dimEspMediaCoroa.textContent = '-- mm';
            if (dimEspMediaCoroaSub) dimEspMediaCoroaSub.textContent = 'Aguardando novo ciclo completo';

            if (dimEspLateralMedia) dimEspLateralMedia.textContent = '-- mm';
            if (dimEspLateralSub) dimEspLateralSub.textContent = '';

            if (dimMotivoBox) {
                dimMotivoBox.style.display = 'block';
                dimMotivoBox.textContent = `⚠️ ${dim.motivo || 'Não foi possível determinar dimensões com confiabilidade.'}`;
            }
        }
    }

    /**
     * Atualiza o indicador permanente de Espessura Atual no Dashboard.
     * Funciona em tempo real com sensor em repouso (gatilho=0) ou durante varredura.
     * NUNCA confunde ausência de leitura/fora da faixa com espessura 0.
     */
    function updateLiveThicknessUI(espAtual) {
        if (!liveThicknessVal) return;

        if (!espAtual || espAtual.status === 'OFFLINE' || espAtual.status === 'SEM_LEITURA') {
            liveThicknessVal.textContent = '-- mm';
            liveThicknessVal.style.color = '#94a3b8';
            if (livePosX) livePosX.textContent = '-- mm';
            if (liveRawInfo) liveRawInfo.textContent = '';
            if (liveStatusLabel) {
                liveStatusLabel.textContent = espAtual && espAtual.status === 'OFFLINE' ? 'Coletor Offline' : 'Aguardando Leitura';
            }
            if (liveStatusBadge) {
                liveStatusBadge.style.background = 'rgba(148, 163, 184, 0.1)';
                liveStatusBadge.style.borderColor = 'rgba(148, 163, 184, 0.25)';
                liveStatusBadge.style.color = '#94a3b8';
            }
            if (liveStatusDot) liveStatusDot.textContent = '●';
            if (liveIndicatorDot) {
                liveIndicatorDot.style.backgroundColor = '#94a3b8';
                liveIndicatorDot.classList.remove('live-pulse-dot');
            }
            return;
        }

        // Posição X do Sensor
        if (espAtual.posicao_x !== null && espAtual.posicao_x !== undefined) {
            const posXFormatted = Number(espAtual.posicao_x).toFixed(1).replace('.', ',');
            if (livePosX) livePosX.textContent = `${posXFormatted} mm`;
        } else {
            if (livePosX) livePosX.textContent = '-- mm';
        }

        // Valor bruto auxiliar para diagnóstico
        if (liveRawInfo && espAtual.valor_bruto !== null && espAtual.valor_bruto !== undefined) {
            liveRawInfo.textContent = `(Bruto: ${espAtual.valor_bruto})`;
        } else if (liveRawInfo) {
            liveRawInfo.textContent = '';
        }

        // Espessura Calculada (mm)
        if (espAtual.valido && espAtual.espessura_mm !== null && espAtual.espessura_mm !== undefined) {
            const espFormatted = Number(espAtual.espessura_mm).toFixed(1).replace('.', ',');
            liveThicknessVal.textContent = `${espFormatted} mm`;
            liveThicknessVal.style.color = '#38bdf8';

            if (liveStatusLabel) liveStatusLabel.textContent = 'Ao Vivo';
            if (liveStatusBadge) {
                liveStatusBadge.style.background = 'rgba(34, 197, 94, 0.15)';
                liveStatusBadge.style.borderColor = 'rgba(34, 197, 94, 0.4)';
                liveStatusBadge.style.color = '#4ade80';
            }
            if (liveStatusDot) liveStatusDot.textContent = '●';
            if (liveIndicatorDot) {
                liveIndicatorDot.style.backgroundColor = '#22c55e';
                liveIndicatorDot.classList.add('live-pulse-dot');
            }
        } else {
            // Condição inválida ou fora da faixa física:
            // REGRA MANDATÓRIA: NUNCA exibir 0,00 mm quando inválido; apresentar '-- mm'
            liveThicknessVal.textContent = '-- mm';
            liveThicknessVal.style.color = '#f59e0b';

            const statusMap = {
                'FORA_DA_FAIXA': 'Fora da Faixa Calibrada',
                'SEM_CALIBRACAO': 'Sem Calibração Ativa',
                'AMOSTRA_INCOERENTE': 'Amostra Incoerente',
                'STALE': 'Leitura Obsoleta',
                'PONTOS_INSUFICIENTES': 'Pontos Insuficientes'
            };
            const labelText = statusMap[espAtual.status] || espAtual.status || 'Leitura Inválida';

            if (liveStatusLabel) liveStatusLabel.textContent = labelText;
            if (liveStatusBadge) {
                liveStatusBadge.style.background = 'rgba(245, 158, 11, 0.15)';
                liveStatusBadge.style.borderColor = 'rgba(245, 158, 11, 0.4)';
                liveStatusBadge.style.color = '#fbbf24';
            }
            if (liveStatusDot) liveStatusDot.textContent = '⚠️';
            if (liveIndicatorDot) {
                liveIndicatorDot.style.backgroundColor = '#f59e0b';
                liveIndicatorDot.classList.remove('live-pulse-dot');
            }
        }
    }

    async function fetchData() {
        try {
            const params = new URLSearchParams();
            if (selectedScanId) params.append('scan_id', selectedScanId);

            params.append('esp_borda', (document.getElementById('logic-esp-borda')?.value || 2.0).toString().replace(',', '.'));
            params.append('centro', (document.getElementById('logic-centro')?.value || 10).toString().replace(',', '.'));
            params.append('tol', (document.getElementById('logic-tol')?.value || 0.5).toString().replace(',', '.'));

            const response = await fetch(`/api/dados/?${params.toString()}`);
            const json = await response.json();
            updateMaterialField(json);
            if (exportBtn) {
                exportBtn.href = selectedScanId
                    ? `/exportar/excel/?scan_id=${encodeURIComponent(selectedScanId)}`
                    : '/exportar/excel/';
            }

            // Atualiza sempre o indicador permanente de Espessura Atual (independente de varredura ou scan_id)
            updateLiveThicknessUI(json.espessura_atual);

            const collStatus = json.collector_status || 'IDLE';
            const isScanning = collStatus === 'SCANNING' || json.is_live_cycle;

            if (json.scan_id) {
                const displayId = json.scan_id.replace('SCAN_', '');
                if (scanIdText) scanIdText.textContent = displayId;
                if (headerScanIdText) headerScanIdText.textContent = displayId;

                if (statusText) {
                    if (selectedScanId) {
                        statusText.textContent = "Modo: Visualização de Histórico";
                        statusText.style.color = '#fbbf24';
                    } else {
                        if (collStatus === 'SCANNING') {
                            statusText.textContent = `● Medindo — Ciclo em andamento (${json.count} pts)`;
                            statusText.style.color = '#22c55e';
                        } else if (collStatus === 'NO_COMMUNICATION') {
                            statusText.textContent = "⚠️ Sem comunicação com o Scada-LTS";
                            statusText.style.color = '#ef4444';
                        } else if (collStatus === 'OFFLINE') {
                            statusText.textContent = `Coletor offline (Último ciclo: ${displayId})`;
                            statusText.style.color = '#f59e0b';
                        } else {
                            statusText.textContent = `Aguardando novo ciclo (Último medido: ${displayId})`;
                            statusText.style.color = '#94a3b8';
                        }
                    }
                }

                const points = json.data || [];
                const dim = json.dimensoes;

                updateDimensionalUI(dim, isScanning);

                let mat = json.material;
                if (mat && mat.largura !== null && mat.largura !== undefined) {
                    const formattedWidth = `${mat.largura.toFixed(1)} mm`;
                    if (widthValElem) widthValElem.textContent = formattedWidth;
                    if (widthSubtextElem && mat.x_inicial !== null && mat.x_final !== null) {
                        widthSubtextElem.textContent = `Início: ${mat.x_inicial.toFixed(1)} mm | Fim: ${mat.x_final.toFixed(1)} mm`;
                    }
                } else {
                    if (widthValElem) widthValElem.textContent = '-- mm';
                    if (widthSubtextElem) widthSubtextElem.textContent = '';
                }

                if (points.length > 0) {
                    const sortedPoints = [...points].sort((a, b) => a.x - b.x);

                    currentChart.data.datasets[0].data = sortedPoints.map(p => ({
                        x: p.x, y: p.y, is_ok: p.is_ok, target: p.target, zona: p.zona
                    }));

                    // Ajuste Dinâmico e Limpo da Escala Y
                    const maxMeasuredY = Math.max(...points.map(p => p.y || 0));

                    if (scaleMode === 'optimized') {
                        // Escala otimizada focada na geometria real da borracha (~14 a 16mm)
                        const targetMax = Math.max(14, Math.ceil((maxMeasuredY + 4) / 2) * 2);
                        currentChart.options.scales.y.min = 0;
                        currentChart.options.scales.y.max = targetMax;
                        currentChart.options.scales.y.ticks.stepSize = 2;
                    } else {
                        // Escala expandida (30mm)
                        currentChart.options.scales.y.min = 0;
                        currentChart.options.scales.y.max = 30;
                        currentChart.options.scales.y.ticks.stepSize = 5;
                    }

                    // Tracker em tempo real durante varredura ativa
                    const latestPoint = points[points.length - 1];
                    if (latestPoint && isScanning) {
                        currentChart.options.plugins.realtimeTracker = {
                            active: true,
                            isScanning: true,
                            point: latestPoint
                        };
                    } else {
                        currentChart.options.plugins.realtimeTracker = {
                            active: false,
                            isScanning: false,
                            point: null
                        };
                    }

                    // Cotas Técnicas Dimensionais
                    if (showTechnicalDimensions && !isScanning && dim && dim.status === 'CONCLUIDO') {
                        currentChart.options.plugins.technicalDimensions = {
                            active: true,
                            dimensoes: dim
                        };
                    } else {
                        currentChart.options.plugins.technicalDimensions = {
                            active: false,
                            dimensoes: null
                        };
                    }
                } else {
                    currentChart.data.datasets.forEach(d => d.data = []);
                    currentChart.options.plugins.realtimeTracker = { active: false, isScanning: false, point: null };
                    currentChart.options.plugins.technicalDimensions = { active: false, dimensoes: null };
                }

                currentChart.update();
            } else {
                if (scanIdText) scanIdText.textContent = "--";
                if (headerScanIdText) headerScanIdText.textContent = "--";
                if (widthValElem) widthValElem.textContent = '-- mm';
                if (widthSubtextElem) widthSubtextElem.textContent = '';
                updateDimensionalUI(null, false);

                if (currentChart) {
                    if (currentChart.options.plugins.realtimeTracker) currentChart.options.plugins.realtimeTracker.active = false;
                    if (currentChart.options.plugins.technicalDimensions) currentChart.options.plugins.technicalDimensions.active = false;
                    currentChart.update();
                }

                if (!selectedScanId && statusText) {
                    if (collStatus === 'NO_COMMUNICATION') {
                        statusText.textContent = "⚠️ Sem comunicação com o Scada-LTS";
                        statusText.style.color = '#ef4444';
                    } else if (collStatus === 'OFFLINE') {
                        statusText.textContent = "Coletor offline (Aguardando inicialização)";
                        statusText.style.color = '#f59e0b';
                    } else {
                        statusText.textContent = "Aguardando novo ciclo...";
                        statusText.style.color = '#94a3b8';
                    }
                }
            }

        } catch (error) {
            console.error('Erro fetch:', error);
            if (statusText) statusText.textContent = "Erro JS: " + error.message;
        }
    }

    // Botão Alternar Cotas
    if (btnToggleCotas) {
        btnToggleCotas.addEventListener('click', () => {
            showTechnicalDimensions = !showTechnicalDimensions;
            if (statusCotasTxt) {
                statusCotasTxt.textContent = showTechnicalDimensions ? 'Visíveis' : 'Ocultas';
                statusCotasTxt.style.color = showTechnicalDimensions ? '#38bdf8' : '#94a3b8';
            }
            if (currentChart) currentChart.update();
        });
    }

    // Botão Alternar Posição das Cotas de Espessura (Acima / Abaixo da Linha)
    if (btnTogglePosicao) {
        btnTogglePosicao.addEventListener('click', () => {
            thicknessPlacement = (thicknessPlacement === 'above') ? 'below' : 'above';
            if (statusPosicaoTxt) {
                statusPosicaoTxt.textContent = (thicknessPlacement === 'above') ? 'Acima da Linha' : 'Abaixo da Linha';
                statusPosicaoTxt.style.color = (thicknessPlacement === 'above') ? '#4ade80' : '#38bdf8';
            }
            if (currentChart) currentChart.update();
        });
    }

    // Botão Alternar Escala Y
    if (btnToggleEscala) {
        btnToggleEscala.addEventListener('click', () => {
            scaleMode = (scaleMode === 'optimized') ? 'expanded' : 'optimized';
            if (statusEscalaTxt) {
                statusEscalaTxt.textContent = (scaleMode === 'optimized') ? 'Otimizada' : '30 mm';
                statusEscalaTxt.style.color = (scaleMode === 'optimized') ? '#22c55e' : '#f59e0b';
            }
            fetchData();
        });
    }

    // Inicialização
    initChart();
    loadScanList();
    fetchData();

    scanSearch.addEventListener('input', () => {
        clearTimeout(searchTimer);
        scanSearchResults.replaceChildren();
        scanSearchResults.hidden = true;
        scanSearchStatus.textContent = scanSearch.value.trim() ? 'Buscando...' : '';
        searchTimer = setTimeout(loadScanList, 250);
    });

    scanSearch.addEventListener('keydown', async (event) => {
        if (event.key === 'Escape') {
            scanSearchResults.hidden = true;
            return;
        }
        if (event.key === 'Enter' && scanSearch.value.trim()) {
            event.preventDefault();
            clearTimeout(searchTimer);
            const items = await loadScanList();
            if (items.length) selectScan(items[0].scan_id);
        }
    });

    scanSearchSubmit.addEventListener('click', async () => {
        clearTimeout(searchTimer);
        const items = await loadScanList();
        if (items.length === 1) selectScan(items[0].scan_id);
        else scanSearch.focus();
    });

    scanSearch.addEventListener('focus', () => {
        if (scanSearch.value.trim() && scanSearchResults.childElementCount) {
            scanSearchResults.hidden = false;
        }
    });

    document.addEventListener('click', (event) => {
        if (!scanSearchWrapper.contains(event.target)) scanSearchResults.hidden = true;
    });

    materialName.addEventListener('input', () => {
        materialName.dataset.dirty = 'true';
        materialSaveStatus.textContent = '';
    });

    saveMaterialName.addEventListener('click', async () => {
        saveMaterialName.disabled = true;
        materialSaveStatus.textContent = 'Salvando...';
        try {
            const response = await fetch('/api/material/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
                body: JSON.stringify({scan_id: selectedScanId || null, nome_material: materialName.value}),
            });
            const json = await response.json();
            if (!response.ok) throw new Error(json.erro || 'Não foi possível salvar.');
            materialSaveStatus.textContent = json.pendente ? 'Preparado para o próximo ciclo.' : 'Nome salvo.';
            materialName.dataset.target = json.pendente ? 'pending' : json.scan_id;
            materialName.dataset.dirty = 'false';
            loadScanList();
        } catch (error) {
            materialSaveStatus.textContent = error.message;
        } finally {
            saveMaterialName.disabled = false;
        }
    });

    // Recarregar se mudar parâmetros nominais
    ['logic-esp-borda', 'logic-centro', 'logic-tol'].forEach(id => {
        const elem = document.getElementById(id);
        if (elem) elem.addEventListener('change', () => fetchData());
    });

    // Eventos do Dropdown de Histórico
    scanSelect.addEventListener('change', (e) => selectScan(e.target.value));

    // Polling Automático para Modo "Ao Vivo"
    setInterval(() => {
        if (!selectedScanId) {
            fetchData();
            if (!scanSearch.value.trim() && Math.random() < 0.1) loadScanList();
        }
    }, 1000);
});
