document.addEventListener('DOMContentLoaded', function () {
    const ctx = document.getElementById('profileChart').getContext('2d');

    // Elementos de UI
    const scanSelect = document.createElement('select');
    scanSelect.className = 'input-control';
    scanSelect.style.maxWidth = '300px';

    // Inserir seletor no header
    const pageHeaderDiv = document.querySelector('.page-header > div');
    if (pageHeaderDiv) {
        pageHeaderDiv.appendChild(document.createElement('br'));
        pageHeaderDiv.appendChild(scanSelect);
    }

    const statusText = document.getElementById('scan-status-text');
    const scanIdText = document.getElementById('current-scan-id');
    const exportBtn = document.querySelector('a[href*="exportar"]');

    // Elementos de Largura / Tamanho do Material
    const widthValElem = document.getElementById('material-width-val');
    const widthSubtextElem = document.getElementById('material-range-subtext');
    const badgeWidthElem = document.getElementById('badge-width-val');

    let currentChart = null;
    let selectedScanId = null;

    // Função utilitária universal para desenhar retângulos arredondados no canvas
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

    // Carregar lista de Scans
    async function loadScanList() {
        try {
            const res = await fetch('/api/scans/');
            const json = await res.json();

            scanSelect.innerHTML = '<option value="">-- Monitoramento Ao Vivo --</option>';
            if (json.scans && Array.isArray(json.scans)) {
                json.scans.forEach(sid => {
                    const opt = document.createElement('option');
                    opt.value = sid;
                    opt.textContent = sid.replace('SCAN_', '') + ' (Histórico)';
                    scanSelect.appendChild(opt);
                });
            }
        } catch (e) {
            console.error('Erro loading scans', e);
        }
    }

    // Plugin customizado para desenhar a linha tracejada e a caixa com a espessura atual em tempo real
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

            // Validar limites visíveis da área de dados
            if (isNaN(px) || isNaN(py) || px < chartArea.left - 5 || px > chartArea.right + 5 || py < chartArea.top - 5 || py > chartArea.bottom + 5) {
                return;
            }

            c.save();

            // Dimensões da caixa de medição atual
            const boxWidth = 145;
            const boxHeight = 44;
            const boxY = chartArea.top + 14;

            // Clampar boxX para ficar sempre totalmente dentro da área visível do gráfico
            let boxX = px - boxWidth / 2;
            if (boxX < chartArea.left + 8) boxX = chartArea.left + 8;
            if (boxX + boxWidth > chartArea.right - 8) boxX = chartArea.right - boxWidth - 8;

            // 1. Linha tracejada subindo da medição atual até a base da caixa flutuante
            const lineEndY = boxY + boxHeight;
            c.beginPath();
            c.setLineDash([4, 4]);
            c.strokeStyle = tracker.isScanning ? '#38bdf8' : '#94a3b8';
            c.lineWidth = 1.8;
            c.moveTo(px, py);
            c.lineTo(px, lineEndY);
            c.stroke();

            // 2. Ponto pulsante no gráfico na coordenada exata medida
            c.setLineDash([]);
            // Halo suave ao redor do ponto medido
            c.beginPath();
            c.arc(px, py, 7, 0, 2 * Math.PI);
            c.fillStyle = pt.is_ok ? 'rgba(34, 197, 94, 0.35)' : 'rgba(239, 68, 68, 0.35)';
            c.fill();

            // Círculo central nítido
            c.beginPath();
            c.arc(px, py, 4, 0, 2 * Math.PI);
            c.fillStyle = pt.is_ok ? '#22c55e' : '#ef4444';
            c.strokeStyle = '#ffffff';
            c.lineWidth = 1.5;
            c.fill();
            c.stroke();

            // 3. Caixa flutuante no topo do gráfico
            c.shadowColor = 'rgba(0, 0, 0, 0.5)';
            c.shadowBlur = 8;
            c.shadowOffsetY = 3;

            c.fillStyle = '#0f172a';
            c.strokeStyle = pt.is_ok ? '#22c55e' : (tracker.isScanning ? '#38bdf8' : '#64748b');
            c.lineWidth = 1.5;

            drawRoundedRect(c, boxX, boxY, boxWidth, boxHeight, 6);
            c.fill();
            c.stroke();

            // Reset de sombra para os textos
            c.shadowColor = 'transparent';

            // Ponteiro triangular apontando para a linha da coordenada X
            const pointerX = Math.min(Math.max(px, boxX + 12), boxX + boxWidth - 12);
            c.beginPath();
            c.moveTo(pointerX - 5, boxY + boxHeight);
            c.lineTo(pointerX + 5, boxY + boxHeight);
            c.lineTo(pointerX, boxY + boxHeight + 5);
            c.closePath();
            c.fillStyle = pt.is_ok ? '#22c55e' : (tracker.isScanning ? '#38bdf8' : '#64748b');
            c.fill();

            // 4. Textos internos na caixa
            c.textAlign = 'center';
            c.font = '600 9px Inter, sans-serif';
            c.fillStyle = '#94a3b8';
            const headerLabel = tracker.isScanning ? '● MEDIÇÃO ATUAL' : 'ÚLTIMA MEDIÇÃO';
            c.fillText(headerLabel, boxX + boxWidth / 2, boxY + 14);

            // Valor da espessura atual (destaque grande)
            c.font = 'bold 16px Inter, sans-serif';
            c.fillStyle = pt.is_ok ? '#22c55e' : '#f8fafc';
            c.fillText(`${pt.y.toFixed(2)} mm`, boxX + boxWidth / 2, boxY + 33);

            c.restore();
        }
    };

    // Configuração Inicial do Gráfico
    function initChart() {
        currentChart = new Chart(ctx, {
            type: 'line',
            plugins: [realtimeTrackerPlugin],
            data: {
                datasets: [
                    {
                        label: 'Medido',
                        data: [],
                        type: 'line',
                        borderColor: '#ef4444', // Linha base vermelha (fora de tolerância)
                        borderWidth: 2.5,
                        pointRadius: 0, // Sem pontos dispersos
                        pointHoverRadius: 6,
                        pointHitRadius: 10,
                        tension: 0,
                        fill: false,
                        segment: {
                            borderColor: ctxSeg => {
                                const p = ctxSeg.p1?.raw;
                                return p?.is_ok ? '#22c55e' : '#ef4444';
                            }
                        },
                        order: 1
                    },
                    {
                        label: 'Limite Max',
                        data: [],
                        type: 'line',
                        borderColor: '#fbbf24', // Amarelo
                        borderDash: [5, 5],
                        pointRadius: 0,
                        borderWidth: 1,
                        fill: false,
                        order: 2
                    },
                    {
                        label: 'Alvo',
                        data: [],
                        type: 'line',
                        borderColor: '#94a3b8',
                        borderWidth: 1,
                        pointRadius: 0,
                        fill: false,
                        order: 3
                    },
                    {
                        label: 'Limite Min',
                        data: [],
                        type: 'line',
                        borderColor: '#fbbf24', // Amarelo
                        borderDash: [5, 5],
                        pointRadius: 0,
                        borderWidth: 1,
                        fill: false,
                        order: 4
                    }
                ]
            },
            options: {
                animation: false,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        type: 'linear',
                        position: 'bottom',
                        title: { display: true, text: 'Largura (mm)' },
                        grid: { color: '#334155' }
                    },
                    y: {
                        title: { display: true, text: 'Espessura (mm)' },
                        min: 0,
                        max: 30, // Escala aumentada de 15 para 30mm conforme solicitado
                        grid: { color: '#334155' },
                        ticks: {
                            stepSize: 5
                        }
                    }
                },
                plugins: {
                    realtimeTracker: {
                        active: false,
                        isScanning: false,
                        point: null
                    },
                    tooltip: {
                        callbacks: {
                            label: function (context) {
                                const p = context.raw;
                                if (!p) return '';
                                return `Esp: ${p.y}mm [${p.zona}] (${p.is_ok ? 'OK' : 'NOK'})`;
                            },
                            afterLabel: function (context) {
                                const p = context.raw;
                                if (!p) return '';
                                return `Pos: ${p.x}mm | Alvo: ${p.target}mm`;
                            }
                        }
                    }
                }
            }
        });
    }

    async function fetchData() {
        try {
            // Coletar parâmetros de configuração da UI
            const params = new URLSearchParams();
            if (selectedScanId) params.append('scan_id', selectedScanId);

            // Parâmetros de Lógica (enviados para o backend recalcular na hora)
            params.append('esp_borda', (document.getElementById('logic-esp-borda')?.value || 2.0).toString().replace(',', '.'));
            params.append('centro', (document.getElementById('logic-centro')?.value || 10).toString().replace(',', '.'));
            params.append('tol', (document.getElementById('logic-tol')?.value || 0.5).toString().replace(',', '.'));

            const response = await fetch(`/api/dados/?${params.toString()}`);
            const json = await response.json();

            const collStatus = json.collector_status || 'IDLE';

            if (json.scan_id) {
                // Atualizar UI
                const displayId = json.scan_id.replace('SCAN_', '');
                if (scanIdText) scanIdText.textContent = displayId;

                // Formar URL de exportação com os mesmos parâmetros para manter coerência
                if (exportBtn) {
                    const csvParams = new URLSearchParams(params);
                    exportBtn.href = `/exportar/csv/?${csvParams.toString()}`;
                }

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

                // Atualizar Informações de Tamanho/Largura do Material (calculado com corte > 0.9 mm)
                let mat = json.material;
                if (!mat || mat.largura === null || mat.largura === undefined) {
                    // Fallback de cálculo direto no cliente caso o payload ainda não contenha material
                    const validPoints = points.filter(p => p.y > 0.9);
                    if (validPoints.length > 0) {
                        const xs = validPoints.map(p => p.x);
                        const minX = Math.min(...xs);
                        const maxX = Math.max(...xs);
                        mat = {
                            largura: Math.round((maxX - minX) * 100) / 100,
                            x_inicial: minX,
                            x_final: maxX
                        };
                    }
                }

                if (mat && mat.largura !== null && mat.largura !== undefined) {
                    const formattedWidth = `${mat.largura.toFixed(1)} mm`;
                    if (widthValElem) widthValElem.textContent = formattedWidth;
                    if (badgeWidthElem) badgeWidthElem.textContent = formattedWidth;
                    if (widthSubtextElem) {
                        widthSubtextElem.textContent = `Início: ${mat.x_inicial.toFixed(1)} mm | Fim: ${mat.x_final.toFixed(1)} mm`;
                    }
                } else {
                    if (widthValElem) widthValElem.textContent = '-- mm';
                    if (badgeWidthElem) badgeWidthElem.textContent = '-- mm';
                    if (widthSubtextElem) widthSubtextElem.textContent = '';
                }

                // Mapear datasets
                if (points.length > 0) {
                    // Ordenar por X para formar uma linha contínua e sem cruzamento
                    const sortedPoints = [...points].sort((a, b) => a.x - b.x);

                    currentChart.data.datasets[0].data = sortedPoints.map(p => ({
                        x: p.x, y: p.y, is_ok: p.is_ok, target: p.target, zona: p.zona
                    }));

                    const minX = sortedPoints[0].x;
                    const maxX = sortedPoints[sortedPoints.length - 1].x;

                    if (minX === maxX) {
                        const spanMin = minX === 0 ? 0 : minX - 5;
                        const spanMax = minX === 0 ? 10 : minX + 5;
                        const p0 = points[0];
                        currentChart.data.datasets[1].data = [{ x: spanMin, y: p0.max }, { x: spanMax, y: p0.max }];
                        currentChart.data.datasets[2].data = [{ x: spanMin, y: p0.target }, { x: spanMax, y: p0.target }];
                        currentChart.data.datasets[3].data = [{ x: spanMin, y: p0.min }, { x: spanMax, y: p0.min }];
                    } else {
                        currentChart.data.datasets[1].data = sortedPoints.map(p => ({ x: p.x, y: p.max }));
                        currentChart.data.datasets[2].data = sortedPoints.map(p => ({ x: p.x, y: p.target }));
                        currentChart.data.datasets[3].data = sortedPoints.map(p => ({ x: p.x, y: p.min }));
                    }

                    // Garantir escala mínima de 30mm conforme solicitado (e expandir se passar de 30mm)
                    const maxY = Math.max(...points.map(p => p.y || 0));
                    currentChart.options.scales.y.min = 0;
                    currentChart.options.scales.y.max = Math.max(30, Math.ceil((maxY + 2) / 5) * 5);

                    // Indicador em tempo real: linha tracejada subindo para caixa com espessura atual
                    const isScanning = collStatus === 'SCANNING' || json.is_live_cycle;
                    const latestPoint = points[points.length - 1]; // Ponto mais recente na ordem cronológica de varredura
                    if (latestPoint && (isScanning || !selectedScanId)) {
                        currentChart.options.plugins.realtimeTracker = {
                            active: true,
                            isScanning: isScanning,
                            point: latestPoint
                        };
                    } else {
                        currentChart.options.plugins.realtimeTracker = {
                            active: false,
                            isScanning: false,
                            point: null
                        };
                    }
                } else {
                    currentChart.data.datasets.forEach(d => d.data = []);
                    currentChart.options.plugins.realtimeTracker = {
                        active: false,
                        isScanning: false,
                        point: null
                    };
                }

                currentChart.update();
            } else {
                if (scanIdText) scanIdText.textContent = "--";
                if (widthValElem) widthValElem.textContent = '-- mm';
                if (badgeWidthElem) badgeWidthElem.textContent = '-- mm';
                if (widthSubtextElem) widthSubtextElem.textContent = '';
                if (currentChart && currentChart.options.plugins.realtimeTracker) {
                    currentChart.options.plugins.realtimeTracker.active = false;
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

    // Inicialização
    initChart();
    loadScanList();
    fetchData();

    // Recarregar se mudar config
    ['logic-esp-borda', 'logic-centro', 'logic-tol'].forEach(id => {
        const elem = document.getElementById(id);
        if (elem) elem.addEventListener('change', () => fetchData());
    });

    // Eventos
    scanSelect.addEventListener('change', (e) => {
        selectedScanId = e.target.value;
        // Limpar gráfico visualmente ao trocar
        currentChart.data.datasets.forEach(d => d.data = []);
        currentChart.update();
        fetchData();
    });

    // Auto-refresh somente se estiver no modo "Ao Vivo" (value vazio)
    setInterval(() => {
        if (!selectedScanId) {
            fetchData();
            // Também recarrega a lista de scans de tempos em tempos para aparecer novos
            if (Math.random() < 0.1) loadScanList();
        }
    }, 1000);
});
