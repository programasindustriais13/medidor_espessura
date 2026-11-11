@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Medidor de Espessura - Inicializador

cd /d "%~dp0"
if errorlevel 1 goto erro_diretorio

set "PYTHON=venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERRO] Ambiente virtual nao encontrado em %PYTHON%.
    exit /b 1
)

if not "%~1"=="" if /I not "%~1"=="--deploy" (
    echo Uso: iniciar_sistema.bat [--deploy]
    echo Sem --deploy, inicia a versao ja presente no servidor.
    exit /b 2
)

echo ======================================================================
echo    MEDIDOR DE ESPESSURA INDUSTRIAL
echo ======================================================================
echo.

rem Evita alterar arquivos enquanto o servidor anterior ainda esta atendendo.
"%PYTHON%" -c "import socket; s=socket.socket(); s.settimeout(0.5); ocupado=s.connect_ex(('127.0.0.1',8282)) == 0; s.close(); raise SystemExit(1 if ocupado else 0)"
if errorlevel 1 (
    echo [ERRO] A porta 8282 ja esta em uso. Encerre o sistema anterior antes de atualizar ou iniciar.
    exit /b 1
)

if /I "%~1"=="--deploy" (
    echo [1/5] Atualizando codigo do GitHub com avancamento rapido...
    where git >nul 2>&1
    if errorlevel 1 (
        echo [ERRO] Git nao encontrado no PATH do servidor.
        exit /b 1
    )
    git pull --ff-only origin main
    if errorlevel 1 (
        echo [ERRO] Falha ao atualizar o codigo. Nenhum processo foi iniciado.
        exit /b 1
    )
) else (
    echo [1/5] Usando codigo ja instalado. Para atualizar do GitHub, use --deploy.
)

echo [2/5] Instalando dependencias e preservando a versao atual do Django...
rem A exigencia exata de Django impede que pip altere a versao instalada.
"%PYTHON%" -c "import django, subprocess, sys; versao=django.get_version(); print('Django preservado:', versao, flush=True); raise SystemExit(subprocess.call([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt', 'Django==' + versao]))"
if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias. Confira requirements.txt e a versao instalada do Django.
    exit /b 1
)

echo [3/5] Validando o projeto e aplicando migracoes...
"%PYTHON%" manage.py check
if errorlevel 1 (
    echo [ERRO] A verificacao do Django falhou. Nenhum processo foi iniciado.
    exit /b 1
)
"%PYTHON%" manage.py migrate --noinput
if errorlevel 1 (
    echo [ERRO] A migracao falhou. Nenhum processo foi iniciado.
    exit /b 1
)

echo [4/5] Iniciando servidor web na porta 8282...
start "Servidor Web [Porta 8282]" cmd /k "title Servidor Web [Porta 8282] & .\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8282 --noreload"
if errorlevel 1 (
    echo [ERRO] Nao foi possivel abrir a janela do servidor web.
    exit /b 1
)

rem Aguarda o servidor responder antes de iniciar o coletor.
for /l %%I in (1,1,10) do (
    "%PYTHON%" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8282/', timeout=2).close()" >nul 2>&1
    if not errorlevel 1 goto servidor_pronto
    ping -n 2 127.0.0.1 >nul
)
echo [ERRO] Servidor web nao respondeu na porta 8282. Consulte a janela do servidor.
exit /b 1

:servidor_pronto
echo [5/5] Iniciando coletor industrial Scada-LTS...
start "Coletor Scada-LTS" cmd /k "title Coletor Scada-LTS & .\venv\Scripts\python.exe manage.py collect_scada"
if errorlevel 1 (
    echo [ERRO] Nao foi possivel abrir a janela do coletor.
    exit /b 1
)

echo.
echo Codigo e banco preparados; servidor web respondeu. Coletor iniciado.
echo Dashboard: http://127.0.0.1:8282/
echo Admin:     http://127.0.0.1:8282/admin/
exit /b 0

:erro_diretorio
echo [ERRO] Nao foi possivel acessar a pasta do sistema.
exit /b 1
