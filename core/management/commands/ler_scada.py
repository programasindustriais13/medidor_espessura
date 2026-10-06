import json
import time
import requests
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import LeituraScada

class Command(BaseCommand):
    help = 'Lê dados do Scada-LTS baseado em gatilho e salva no banco de dados'

    def handle(self, *args, **options):
        # Configurações
        BASE_URL = 'http://192.168.0.202:8080/Scada-LTS'
        LOGIN_URL = f'{BASE_URL}/login.htm'
        
        # Endpoints dos Data Points
        API_VALOR = f'{BASE_URL}/api/point_value/getValue/DP_747174'
        API_GATILHO = f'{BASE_URL}/api/point_value/getValue/DP_887366'
        
        USERNAME = 'teste'
        PASSWORD = 'teste'

        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) MonitorEspessura/1.0'
        })

        # Estado do Sistema
        is_scanning = False
        current_scan_id = None

        self.stdout.write(self.style.SUCCESS('Iniciando monitoramento de Scada-LTS...'))
        self.stdout.write(self.style.NOTICE(f'Gatilho configurado em: {API_GATILHO}'))
        
        while True:
            try:
                loop_start = time.time()

                # ---------------------------------------------------------
                # 1. Verificar Autenticação / Relogin se necessário
                # ---------------------------------------------------------
                # Uma forma simples de checar é tentar acessar o gatilho.
                # Se falhar com erro de auth, refazer login.
                try:
                    resp_gatilho = session.get(API_GATILHO, timeout=2)
                except requests.RequestException:
                    # Falha de rede ou timeout, tenta reconectar session
                    pass
                
                if resp_gatilho.status_code != 200 or 'login.htm' in resp_gatilho.url:
                     self.stdout.write(self.style.WARNING("Sessão inválida. Realizando login..."))
                     # Flow de Login
                     session.get(LOGIN_URL) # Get cookie
                     res_login = session.post(LOGIN_URL, data={'username': USERNAME, 'password': PASSWORD, 'submit': 'Login'})
                     if 'login.htm' in res_login.url:
                         self.stdout.write(self.style.ERROR("Falha no Login. Verifique credenciais. Retentando em 5s..."))
                         time.sleep(5)
                         continue
                     else:
                         self.stdout.write(self.style.SUCCESS("Login reconectado."))
                         # Refazer a request do gatilho após login
                         resp_gatilho = session.get(API_GATILHO, timeout=2)

                # ---------------------------------------------------------
                # 2. Lógica do Gatilho
                # ---------------------------------------------------------
                try:
                    data_gatilho = resp_gatilho.json()
                    trigger_value = data_gatilho.get('value') # Esperado 'true' ou 'false' (string ou bool)
                    
                    # Normalizar para bool Python
                    if isinstance(trigger_value, str):
                        trigger_active = trigger_value.lower() == 'true'
                    else:
                        trigger_active = bool(trigger_value)

                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Erro ao ler gatilho: {e}"))
                    time.sleep(1)
                    continue

                # ---------------------------------------------------------
                # 3. Máquina de Estados (Start/Stop Scan)
                # ---------------------------------------------------------
                if trigger_active and not is_scanning:
                    # Borda de Subida -> Iniciar Novo Scan
                    is_scanning = True
                    current_scan_id = f"SCAN_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    self.stdout.write(self.style.SUCCESS(f"\n>>> INÍCIO DE CICLO DETECTADO [{current_scan_id}] <<<"))
                
                elif not trigger_active and is_scanning:
                    # Borda de Descida -> Finalizar Scan
                    is_scanning = False
                    self.stdout.write(self.style.SUCCESS(f"\n>>> FIM DE CICLO [{current_scan_id}] <<<"))
                    current_scan_id = None
                    self.stdout.write("Aguardando próximo ciclo...")

                # ---------------------------------------------------------
                # 4. Leitura de Dados (Apenas se Scanning for True)
                # ---------------------------------------------------------
                if is_scanning:
                    resp_valor = session.get(API_VALOR, timeout=2)
                    if resp_valor.status_code == 200:
                        raw_data = resp_valor.json()
                        
                        # Parse
                        xid_val = raw_data.get('xid')
                        ts_ms = raw_data.get('ts')
                        val_str = raw_data.get('value')
                        
                        if val_str:
                            try:
                                dt_aware = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                                inner_list = json.loads(val_str)
                                
                                if isinstance(inner_list, list):
                                    for item in inner_list:
                                        val_x = item.get('x')
                                        val_y = item.get('y')
                                        
                                        if val_x is not None and val_y is not None:
                                            # Salvar no Banco vinculado ao Scan ID
                                            LeituraScada.objects.create(
                                                scan_id=current_scan_id,
                                                xid=xid_val,
                                                eixo_x=float(val_x),
                                                eixo_y=float(val_y),
                                                data_leitura=dt_aware
                                            )
                                            # Feedback visual de progresso
                                            print(f"\r[{current_scan_id}] Pontos Coletados: X={val_x}, Y={val_y}", end="", flush=True)
                            
                            except Exception as e:
                                print(f" Erro parse valor: {e}", end="")
                
                # Controle de Loop (200ms)
                elapsed = time.time() - loop_start
                sleep_cmd = max(0, 0.2 - elapsed)
                time.sleep(sleep_cmd)

            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("\nParando manualmente..."))
                break
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"\nErro fatal no loop: {e}"))
                time.sleep(2)
