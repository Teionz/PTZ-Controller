@echo off
chcp 65001 >nul
title Instalador - Rastreador PTZ
echo ============================================================
echo   INSTALADOR DO RASTREADOR PTZ
echo ============================================================
echo.
echo Este passo baixa os componentes necessarios (IA e video).
echo Precisa de internet. Pode demorar alguns minutos na 1a vez.
echo.
pause

echo.
echo [1/4] Verificando o Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERRO: Python nao encontrado.
    echo Instale o Python 3.11 em https://www.python.org/downloads/
    echo IMPORTANTE: na instalacao, marque a caixa "Add Python to PATH".
    echo.
    pause
    exit /b 1
)
python --version

echo.
echo [2/4] Atualizando o instalador de pacotes (pip)...
python -m pip install --upgrade pip

echo.
echo [3/4] Instalando a IA (versao para CPU, mais leve)...
echo       Isso e a maior parte do download. Aguarde.
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
    echo.
    echo Aviso: a instalacao da versao CPU falhou. Tentando a versao padrao...
    python -m pip install torch torchvision
)

echo.
echo [4/4] Instalando o restante (video, IA de pessoa, rede)...
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo ERRO na instalacao. Confira a internet e rode este arquivo de novo.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   INSTALACAO CONCLUIDA
echo ============================================================
echo Agora abra o arquivo "2-INICIAR.bat" para usar.
echo.
pause
