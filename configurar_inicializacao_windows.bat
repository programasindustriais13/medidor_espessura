@echo off
title Configuracao de Inicializacao Automatica - Medidor de Espessura
cls

:: Verificar privilegios de administrador
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Este script precisa ser executado como Administrador!
    echo Clique com o botao direito nele e selecione "Executar como Administrador".
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0"
set "BAT_PATH=%~dp0iniciar_sistema.bat"

echo ======================================================================
echo    CONFIGURAR INICIALIZACAO AUTOMATICA - MEDIDOR DE ESPESSURA
echo ======================================================================
echo Caminho do script: "%BAT_PATH%"
echo.
echo Escolha o metodo desejado:
echo.
echo [1] Agendador de Tarefas - Ao INICIALIZAR o Windows (Boot / sem login)
echo [2] Agendador de Tarefas - Ao FAZER LOGON (Abre as janelas no Desktop ao logar)
echo [3] Criar atalho na pasta Inicializar do Windows (shell:common startup)
echo [4] Desinstalar / Remover inicializacao automatica
echo [0] Sair
echo.
set /p OPCAO="Digite a opcao desejada (0-4): "

if "%OPCAO%"=="1" (
    echo.
    echo Criando tarefa agendada no Boot (ONSTART / SYSTEM)...
    schtasks /create /tn "MedidorEspessura_AutoStart" /tr "\"%BAT_PATH%\"" /sc onstart /ru SYSTEM /rl HIGHEST /f
    if %errorlevel% equ 0 (
        echo.
        echo [SUCESSO] Tarefa criada! O sistema iniciara automaticamente ao ligar o Windows.
    ) else (
        echo.
        echo [FALHA] Nao foi possivel criar a tarefa agendada.
    )
    goto fim
)

if "%OPCAO%"=="2" (
    echo.
    echo Criando tarefa agendada no Logon (ONLOGON)...
    schtasks /create /tn "MedidorEspessura_AutoStart" /tr "\"%BAT_PATH%\"" /sc onlogon /rl HIGHEST /f
    if %errorlevel% equ 0 (
        echo.
        echo [SUCESSO] Tarefa criada! O sistema iniciara ao fazer login no Windows.
    ) else (
        echo.
        echo [FALHA] Nao foi possivel criar a tarefa agendada.
    )
    goto fim
)

if "%OPCAO%"=="3" (
    echo.
    echo Criando atalho na pasta de inicializacao comum...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp\MedidorEspessura.lnk'); $s.TargetPath = '%BAT_PATH%'; $s.WorkingDirectory = '%~dp0'; $s.Save()"
    if %errorlevel% equ 0 (
        echo.
        echo [SUCESSO] Atalho criado em C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp!
    ) else (
        echo.
        echo [FALHA] Nao foi possivel criar o atalho.
    )
    goto fim
)

if "%OPCAO%"=="4" (
    echo.
    echo Removendo tarefa agendada...
    schtasks /delete /tn "MedidorEspessura_AutoStart" /f >nul 2>&1
    echo Removendo atalho de inicializacao...
    if exist "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp\MedidorEspessura.lnk" (
        del /f /q "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp\MedidorEspessura.lnk" >nul 2>&1
    )
    echo.
    echo [SUCESSO] Inicializacao automatica removida!
    goto fim
)

:fim
echo.
pause

