@echo off
title Medidor de Espessura - Encerramento
cls
echo ======================================================================
echo    ENCERRANDO PROCESSOS - MEDIDOR DE ESPESSURA INDUSTRIAL
echo ======================================================================
echo.

cd /d "%~dp0"

echo [1/2] Fechando janelas do Servidor Web e Coletor...
taskkill /F /FI "WINDOWTITLE eq Servidor Web [Porta 8282]*" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Coletor Scada-LTS*" /T >nul 2>&1

echo [2/2] Liberando arquivo de trava...
if exist "scada_collector.lock" (
    del /f /q "scada_collector.lock" >nul 2>&1
)

echo.
echo ======================================================================
echo  Processos encerrados com sucesso!
echo ======================================================================
echo.
ping -n 3 127.0.0.1 >nul
