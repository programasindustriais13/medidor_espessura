@echo off
title Medidor de Espessura - Inicializador
cls
echo ======================================================================
echo    INICIALIZANDO SISTEMA - MEDIDOR DE ESPESSURA INDUSTRIAL
echo ======================================================================
echo.

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERRO] Ambiente virtual nao encontrado em venv\Scripts\python.exe!
    pause
    exit /b 1
)

echo [1/3] Verificando e aplicando migracoes pendentes no banco de dados...
.\venv\Scripts\python.exe manage.py migrate --noinput
if errorlevel 1 (
    echo [AVISO] Houve um problema ao aplicar as migracoes.
)

echo [2/3] Iniciando Servidor Web Django na porta 8282...
start "Servidor Web [Porta 8282]" cmd /k "title Servidor Web [Porta 8282] & .\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8282"

ping -n 3 127.0.0.1 >nul

echo [3/3] Iniciando Coletor Industrial Scada-LTS...
start "Coletor Scada-LTS" cmd /k "title Coletor Scada-LTS & .\venv\Scripts\python.exe manage.py collect_scada"

echo.
echo ======================================================================
echo  Sistema iniciado com sucesso!
echo.
echo  Acessos no navegador:
echo  - Dashboard:    http://127.0.0.1:8282/
echo  - Django Admin: http://127.0.0.1:8282/admin/
echo.
echo  Janelas abertas:
echo   1. Servidor Web (Porta 8282)
echo   2. Coletor Scada-LTS
echo ======================================================================
echo.
ping -n 5 127.0.0.1 >nul
