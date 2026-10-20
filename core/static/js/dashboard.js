document.addEventListener('DOMContentLoaded', function () {
    const ctx = document.getElementById('profileChart').getContext('2d');

    // Elementos de UI
    const scanSelect = document.createElement('select'); // Criaremos dinamicamente no header
    scanSelect.className = 'input-control';
    scanSelect.style.maxWidth = '300px';

    // Inserir seletor no header
    const pageHeaderDiv = document.querySelector('.page-header > div');
    pageHeaderDiv.appendChild(document.createElement('br'));
    pageHeaderDiv.appendChild(scanSelect);

    const statusText = document.getElementById('scan-status-text');
    const scanIdText = document.getElementById('current-scan-id');
    const exportBtn = document.querySelector('a[href*="exportar"]');

    let currentChart = null;
    let selectedScanId = null;

    // Carregar lista de Scans
    async function loadScanList() {
        try {
            const res = await fetch('/api/scans/');
            const json = await res.json();

            scanSelect.innerHTML = '<option value="">-- Monitoramento Ao Vivo --</option>';
            json.scans.forEach(sid => {
                const opt = document.createElement('option');
                opt.value = sid;
                opt.textContent = sid.replace('SCAN_', '') + ' (Histórico)';
                scanSelect.appendChild(opt);
            });
        } catch (e) { console.error('Erro loading scans', e); }
    }

    // Configuração Inicial do Gráfico
    function initChart() {
        currentChart = new Chart(ctx, {
            type: 'scatter',
            data: {
                datasets: [
                    {
                        label: 'Medido',
                        data: [],
                        backgroundColor: ctx => ctx.raw?.is_ok ? '#22c55e' : '#ef4444',
                        pointRadius: 4,
                        order: 1
                    },
                    {
                        label: 'Limite Max',
                        data: [],
                        type: 'line',
                        borderColor: '#fbbf24', // Yellow/Orange
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
                        order: 3
                    },
                    {
                        label: 'Limite Min',
                        data: [],
                        type: 'line',
                        borderColor: '#fbbf24',
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
                        // min: 0, max: 600 -- Removido para auto-scale
                        grid: { color: '#334155' }
                    },
                    y: {
                        title: { display: true, text: 'Espessura (mm)' },
                        min: 0,
                        max: 15 // Range típico
                    }
                },
                plugins: {
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
            // Coletar parêmetros de configuração da UI
            const params = new URLSearchParams();
            if (selectedScanId) params.append('scan_id', selectedScanId);

            // Parametros de Lógica (enviados para o backend recalcular na hora)
            // Parametros de Lógica
            params.append('esp_borda', (document.getElementById('logic-esp-borda')?.value || 2.0).toString().replace(',', '.'));
            params.append('centro', (document.getElementById('logic-centro')?.value || 10).toString().replace(',', '.'));
            params.append('tol', (document.getElementById('logic-tol')?.value || 0.5).toString().replace(',', '.'));

            const response = await fetch(`/api/dados/?${params.toString()}`);
            const json = await response.json();
            console.log('Dados recebidos:', json); // Debug

            const collStatus = json.collector_status || 'IDLE';

            if (json.scan_id) {
                // Atualizar UI
                const displayId = json.scan_id.replace('SCAN_', '');
                scanIdText.textContent = displayId;

                // Formar URL de exportação com os mesmos parâmetros para manter coerência
                const csvParams = new URLSearchParams(params);
                exportBtn.href = `/exportar/csv/?${csvParams.toString()}`;

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

                // Parsear Dados
                const points = json.data;

                // Mapear datasets
                // Ponto medido com coordenadas reais
                currentChart.data.datasets[0].data = points.map(p => ({
                    x: p.x, y: p.y, is_ok: p.is_ok, target: p.target, zona: p.zona
                }));

                // Linhas de Referência (construir perfil contínuo ordenado por X)
                if (points.length > 0) {
                    const sortedPoints = [...points].sort((a, b) => a.x - b.x);
                    const minX = sortedPoints[0].x;
                    const maxX = sortedPoints[sortedPoints.length - 1].x;

                    if (minX === maxX) {
                        // Quando todas as coordenadas X são idênticas (ex: X=0 em medição pontual),
                        // traça linhas de referência horizontais visíveis delimitando a faixa
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
                } else {
                    currentChart.data.datasets[1].data = [];
                    currentChart.data.datasets[2].data = [];
                    currentChart.data.datasets[3].data = [];
                }

                currentChart.update();
            } else {
                scanIdText.textContent = "--";
                if (!selectedScanId) {
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
        document.getElementById(id).addEventListener('change', () => fetchData());
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
