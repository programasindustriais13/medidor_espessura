import threading
import time
import json
import requests
from datetime import datetime
from django.utils import timezone
from django.conf import settings
from core.models import LeituraScada

class ScadaWorker(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True # Encerra a thread se o processo principal morrer
        self._stop_event = threading.Event()
        
        # Configurações
        self.base_url = 'http://192.168.0.202:8080/Scada-LTS'
        self.login_url = f'{self.base_url}/login.htm'
        self.api_valor = f'{self.base_url}/api/point_value/getValue/DP_747174'
        self.api_gatilho = f'{self.base_url}/api/point_value/getValue/DP_887366'
        self.username = 'teste'
        self.password = 'teste1' # Atualizado conforme verificado anteriormente
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'MonitorEspessura/IntegratedWorker'
        })

    def stop(self):
        self._stop_event.set()

    def run(self):
        print(">>> [SCADA WORKER] Serviço de monitoramento iniciado em background.")
        
        is_scanning = False
        current_scan_id = None

        while not self._stop_event.is_set():
            try:
                loop_start = time.time()

                # 1. Autenticação / Checagem de Sessão
                try:
                    resp_gatilho = self.session.get(self.api_gatilho, timeout=2)
                except requests.RequestException:
                    pass 
                
                if 'resp_gatilho' not in locals() or resp_gatilho.status_code != 200 or 'login.htm' in resp_gatilho.url:
                    print(">>> [SCADA WORKER] Sessão expirada. Reconectando...")
                    try:
                        self.session.get(self.login_url, timeout=5)
                        res_login = self.session.post(self.login_url, 
                                                    data={'username': self.username, 'password': self.password, 'submit': 'Login'},
                                                    timeout=5)
                        if 'login.htm' in res_login.url:
                            print(f">>> [SCADA WORKER] Falha de login. Tentando novamente em 10s...")
                            time.sleep(10)
                            continue
                        else:
                            print(">>> [SCADA WORKER] Login reconectado com sucesso.")
                            resp_gatilho = self.session.get(self.api_gatilho, timeout=2)
                    except Exception as e:
                        time.sleep(5)
                        continue

                # 2. Ler Gatilho
                try:
                    data_gatilho = resp_gatilho.json()
                    val_gatilho = data_gatilho.get('value')
                    
                    if isinstance(val_gatilho, str):
                        current_trigger = val_gatilho.lower() == 'true'
                    else:
                        current_trigger = bool(val_gatilho)
                except Exception as e:
                    print(f">>> [SCADA WORKER] Erro parse gatilho: {e}")
                    time.sleep(1)
                    continue

                # 3. Controle de Estado (Lógica Padrão: Start se True, Stop se False)
                if current_trigger and not is_scanning:
                    is_scanning = True
                    current_scan_id = f"SCAN_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    last_valid_x = -999.0
                    print(f"\n>>> [SCADA WORKER] INÍCIO CICLO: {current_scan_id}")
                
                elif not current_trigger and is_scanning:
                    is_scanning = False
                    print(f"\n>>> [SCADA WORKER] FIM CICLO: {current_scan_id}")
                    current_scan_id = None

                # 4. Leitura de Dados
                if is_scanning:
                    try:
                        resp_valor = self.session.get(self.api_valor, timeout=2)
                        if resp_valor.status_code == 200:
                            raw_data = resp_valor.json()
                            val_str = raw_data.get('value')
                            
                            if val_str:
                                dt_aware = datetime.fromtimestamp(raw_data.get('ts') / 1000.0, tz=timezone.utc)
                                inner_list = json.loads(val_str)
                                
                                if isinstance(inner_list, list):
                                    for item in inner_list:
                                        val_x = item.get('x')
                                        val_y = item.get('y')
                                        
                                        if val_x is not None and val_y is not None:
                                            vx = float(val_x)
                                            vy = float(val_y)
                                            
                                            # Filtro de Repetidos (Aumentada sensibilidade para 0.1mm)
                                            if abs(vx - last_valid_x) > 0.1:
                                                LeituraScada.objects.create(
                                                    scan_id=current_scan_id,
                                                    xid=raw_data.get('xid'),
                                                    eixo_x=vx,
                                                    eixo_y=vy,
                                                    data_leitura=dt_aware
                                                )
                                                print(f">>> [SALVO] X={vx:.1f} Y={vy:.1f} (Scan: {current_scan_id})")
                                                last_valid_x = vx
                    except Exception as e:
                        print(f">>> [SCADA WORKER] Erro leitura dados: {e}")

                # Aumentada frequencia de Polling (50ms)
                time.sleep(0.05)
                
            except Exception as e:
                print(f">>> [SCADA WORKER] Erro Loop: {e}")
                time.sleep(5)
