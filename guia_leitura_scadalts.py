#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
================================================================================
GUIA DE INTEGRAÇÃO E LEITURA - SCADA-LTS
================================================================================
Este arquivo serve como um guia técnico e boilerplate (código modelo) pronto para
uso em novos projetos que necessitem ler dados de sensores/variáveis do Scada-LTS.

Arquitetura e Princípios de Funcionamento:
------------------------------------------
1. Mantenção de Sessão (requests.Session):
   O Scada-LTS requer autenticação via cookie de sessão. Usamos requests.Session()
   para persistir os cookies automaticamente entre as chamadas da API.

2. Reconexão Automática (Autenticação sob Demanda):
   Antes de ler qualquer dado, verificamos se a sessão expirou ou redirecionou 
   para a página de login ('login.htm'). Se sim, o script realiza um POST 
   automático de login e restabelece a conexão sem interromper o serviço.

3. Máquina de Estados por Gatilho (Trigger System):
   O monitoramento roda continuamente (polling). A leitura de dados em alta 
   frequência só é ativada quando o "Gatilho" (Data Point de controle, ex: sensor 
   de presença física da chapa) vai para True (borda de subida). Quando ele vai 
   para False (borda de descida), o ciclo de medição (Scan) é encerrado.

4. Formato de Dados Complexo (Parsing de JSON aninhado):
   O Scada-LTS armazena o valor do sensor de espessura como uma string JSON que 
   contém uma lista de coordenadas X (posição) e Y (espessura) em milímetros:
   Exemplo de valor bruto retornado pela API:
   '[{"x": 10.0, "y": 2.34}, {"x": 12.5, "y": 2.35}]'

5. Filtro de Repetição/Ruído:
   Para otimizar o banco de dados e evitar duplicidade desnecessária, aplicamos
   um filtro de sensibilidade espacial de 0.1mm no deslocamento do eixo X.
================================================================================
"""

import os
import sys
import time
import json
import requests
from datetime import datetime

# ================================================================================
# CONFIGURAÇÕES E CREDENCIAIS AUTORIZADAS (COPIAR E ADAPTAR)
# ================================================================================
SCADA_CONFIG = {
    # URL base do servidor Scada-LTS na rede local
    'BASE_URL': 'http://192.168.0.202:8080/Scada-LTS',
    
    # Credenciais de acesso verificadas e ativas
    'USERNAME': 'teste',
    'PASSWORD': 'teste1',
    
    # Identificadores (XIDs) dos Data Points no Scada-LTS
    'XID_VALOR': 'DP_747174',    # Data Point que armazena a lista de medições [X, Y]
    'XID_GATILHO': 'DP_887366',  # Data Point booleano que inicia/para o ciclo (Scan)
    
    # Parâmetros de Frequência e Filtro
    'POLLING_INTERVAL': 0.05,    # Tempo de espera em segundos entre leituras (50ms)
    'X_SENSITIVITY': 0.1,        # Distância mínima em X para gravar um novo ponto (em mm)
}

# ================================================================================
# MOCK / MODO SIMULAÇÃO (Para testar o script fora da rede física do Scada)
# ================================================================================
USE_MOCK_FALLBACK = True  # Se True, entra em simulação caso o servidor físico esteja offline

class MockScadaResponse:
    """Classe auxiliar para simular respostas HTTP do Scada-LTS caso esteja offline."""
    def __init__(self, url, username=None):
        self.url = url
        self.status_code = 200
        
        # Simulação de variação temporal para gerar um comportamento realista
        t = time.time()
        
        # O gatilho liga por 15 segundos, desliga por 10 segundos
        cycle_period = 25
        time_in_cycle = t % cycle_period
        trigger_state = time_in_cycle < 15
        
        if 'DP_887366' in url:
            # Resposta simulada do Gatilho
            self._data = {
                "xid": "DP_887366",
                "value": str(trigger_state).lower(),
                "ts": int(t * 1000)
            }
        elif 'DP_747174' in url:
            # Resposta simulada de Medição: gera posições X progressivas e espessuras Y próximas a 2.0mm
            x_pos = float(f"{time_in_cycle * 10:.1f}")
            # Ruído simulado no Y
            y_val = float(f"{2.0 + (t % 0.1):.3f}")
            self._data = {
                "xid": "DP_747174",
                "value": json.dumps([{"x": x_pos, "y": y_val}]),
                "ts": int(t * 1000)
            }
        else:
            self._data = {"status": "success"}

    def json(self):
        return self._data


# ================================================================================
# CLASSE CORE DE LEITURA (STANDALONE - INDEPENDENTE DE DJANGO)
# ================================================================================
class ScadaLTSConnector:
    def __init__(self, config):
        self.config = config
        self.base_url = config['BASE_URL']
        self.login_url = f"{self.base_url}/login.htm"
        self.api_valor = f"{self.base_url}/api/point_value/getValue/{config['XID_VALOR']}"
        self.api_gatilho = f"{self.base_url}/api/point_value/getValue/{config['XID_GATILHO']}"
        
        self.username = config['USERNAME']
        self.password = config['PASSWORD']
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'ScadaLTSConnectorGuide/2.0'
        })
        
        self.is_mock_active = False

    def realizar_login(self) -> bool:
        """Efetua login no painel do Scada-LTS e atualiza a sessão."""
        if self.is_mock_active:
            return True
            
        print("[SCADA] Estabelecendo nova conexão...")
        try:
            # O Scada-LTS precisa de um GET inicial para gerar o cookie CSRF/Session
            self.session.get(self.login_url, timeout=3)
            
            # Executa o POST de autenticação
            response = self.session.post(
                self.login_url,
                data={
                    'username': self.username,
                    'password': self.password,
                    'submit': 'Login'
                },
                timeout=3
            )
            
            # Se a resposta contiver 'login.htm' na URL final, significa que o login falhou
            if 'login.htm' in response.url:
                print(f"[ERRO] Credenciais inválidas ou erro no portal do Scada-LTS!")
                return False
                
            print("[SUCESSO] Conectado e autenticado com sucesso.")
            return True
        except Exception as e:
            print(f"[ERRO] Falha de comunicação física ao tentar fazer login: {e}")
            return False

    def obter_gatilho(self) -> bool:
        """Lê o estado atual do gatilho físico."""
        try:
            if self.is_mock_active:
                resp = MockScadaResponse(self.api_gatilho)
            else:
                resp = self.session.get(self.api_gatilho, timeout=2)
                
                # Trata expiração de sessão / redirecionamentos
                if resp.status_code != 200 or 'login.htm' in resp.url:
                    if self.realizar_login():
                        resp = self.session.get(self.api_gatilho, timeout=2)
                    else:
                        raise ConnectionError("Impossível restabelecer sessão.")

            data = resp.json()
            val = data.get('value')
            
            # Converte string ('true' / 'false') ou tipo booleano bruto
            if isinstance(val, str):
                return val.lower() == 'true'
            return bool(val)
            
        except Exception as e:
            if USE_MOCK_FALLBACK and not self.is_mock_active:
                print(f"\n[Aviso] Servidor offline. Entrando em Modo de Simulação (Mock)...")
                self.is_mock_active = True
                return self.obter_gatilho()
            raise e

    def obter_dados_medicao(self) -> list:
        """Lê e realiza o parse dos valores de medição."""
        try:
            if self.is_mock_active:
                resp = MockScadaResponse(self.api_valor)
            else:
                resp = self.session.get(self.api_valor, timeout=2)
                
            if resp.status_code == 200:
                raw_data = resp.json()
                val_str = raw_data.get('value')
                ts_ms = raw_data.get('ts') # Timestamp em milissegundos
                xid = raw_data.get('xid')
                
                if val_str:
                    # Converte timestamp UNIX em objeto datetime do Python (local/UTC)
                    dt_leitura = datetime.fromtimestamp(ts_ms / 1000.0)
                    
                    # Converte a string JSON interna contendo a lista de coordenadas
                    pontos_lista = json.loads(val_str)
                    
                    retorno = []
                    if isinstance(pontos_lista, list):
                        for item in pontos_lista:
                            x = item.get('x')
                            y = item.get('y')
                            if x is not None and y is not None:
                                retorno.append({
                                    'xid': xid,
                                    'x': float(x),
                                    'y': float(y),
                                    'data_leitura': dt_leitura
                                })
                    return retorno
            return []
        except Exception as e:
            print(f"[ERRO] Erro ao obter dados de medição: {e}")
            return []


# ================================================================================
# LAÇO PRINCIPAL DE EXECUÇÃO E FILTRAGEM (RUNNER)
# ================================================================================
def executar_monitoramento():
    connector = ScadaLTSConnector(SCADA_CONFIG)
    
    # Tenta conexão inicial
    connector.realizar_login()
    
    is_scanning = False
    current_scan_id = None
    last_valid_x = -9999.0
    
    print("\n" + "="*80)
    print(" MONITORAMENTO SCADA-LTS INICIADO".center(80))
    print(" Aperte Ctrl+C para encerrar".center(80))
    print("="*80 + "\n")
    
    while True:
        try:
            loop_start = time.time()
            
            # 1. Checa estado do Gatilho
            trigger_active = connector.obter_gatilho()
            
            # 2. Máquina de Estados do Ciclo de Medição
            if trigger_active and not is_scanning:
                # Borda de Subida: Início da Chapa / Ciclo
                is_scanning = True
                current_scan_id = f"SCAN_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                last_valid_x = -9999.0
                print(f"\n[>>> INÍCIO DE CICLO DETECTADO: {current_scan_id} <<<]")
                
            elif not trigger_active and is_scanning:
                # Borda de Descida: Fim da Chapa / Ciclo
                is_scanning = False
                print(f"[<<< FIM DE CICLO DETECTADO: {current_scan_id} >>>]")
                current_scan_id = None
                print("Aguardando próxima chapa...\n")
                
            # 3. Processamento e Filtragem (Caso ativo)
            if is_scanning:
                pontos = connector.obter_dados_medicao()
                for p in pontos:
                    vx = p['x']
                    vy = p['y']
                    
                    # Filtro de Distância (Evita poluir o banco com dados repetidos no mesmo X)
                    if abs(vx - last_valid_x) >= SCADA_CONFIG['X_SENSITIVITY']:
                        
                        # --- DIRETRIZ PARA GRAVAR EM BANCO DE DADOS ---
                        # Se você estiver usando DJANGO:
                        # LeituraScada.objects.create(
                        #     scan_id=current_scan_id,
                        #     xid=p['xid'],
                        #     eixo_x=vx,
                        #     eixo_y=vy,
                        #     data_leitura=p['data_leitura']
                        # )
                        #
                        # Se você estiver usando SQLITE/POSTGRESQL puro:
                        # cursor.execute(
                        #     "INSERT INTO leitura (scan_id, xid, eixo_x, eixo_y, data_leitura) VALUES (?, ?, ?, ?, ?)",
                        #     (current_scan_id, p['xid'], vx, vy, p['data_leitura'])
                        # )
                        # -----------------------------------------------
                        
                        # Exibe o ponto no console
                        sim_indicator = " [MOCK]" if connector.is_mock_active else ""
                        sys.stdout.write(f"\r{sim_indicator}[{current_scan_id}] Ponto Coletado: Posição X = {vx:5.1f}mm | Espessura Y = {vy:.3f}mm")
                        sys.stdout.flush()
                        
                        last_valid_x = vx
            
            # Controle preciso da frequência de polling
            elapsed = time.time() - loop_start
            sleep_time = max(0.005, SCADA_CONFIG['POLLING_INTERVAL'] - elapsed)
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            print("\n\n[INFO] Monitoramento interrompido pelo usuário.")
            break
        except ConnectionError as ce:
            print(f"\n[AVISO DE REDE] Erro de rede temporário: {ce}. Tentando reconectar...")
            time.sleep(5)
        except Exception as e:
            print(f"\n[ERRO CRÍTICO] Erro inesperado: {e}")
            time.sleep(2)

if __name__ == '__main__':
    executar_monitoramento()
