@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Passo 1 - Compilar o Rastreador PTZ (gerar o .exe)

echo ============================================================
echo   PASSO 1 de 2  -  COMPILAR O PROGRAMA (gerar o .exe)
echo ============================================================
echo.
echo Isto transforma o programa em um .exe (dentro da pasta "dist").
echo Precisa de internet na 1a vez e pode demorar bastante (baixa a IA).
echo NAO precisa fazer isso no PC da igreja - faca aqui e leve pronto.
echo.
pause

echo.
echo [1/6] Verificando o Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado. Instale o Python 3.11 em
    echo https://www.python.org/downloads/  ^(marque "Add Python to PATH"^).
    pause & exit /b 1
)
python --version

echo.
echo [2/6] Atualizando o pip...
python -m pip install --upgrade pip

echo.
echo [3/6] Instalando a IA (torch - versao CPU, mais leve)...
python -c "import torch" >nul 2>&1
if errorlevel 1 (
    python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    if errorlevel 1 python -m pip install torch torchvision
)

echo.
echo [4/6] Instalando as demais dependencias + ferramenta de empacotar...
python -m pip install -r requirements.txt
if errorlevel 1 ( echo ERRO instalando dependencias. Veja a internet. & pause & exit /b 1 )
python -m pip install pyinstaller pillow
if errorlevel 1 ( echo ERRO instalando o PyInstaller. & pause & exit /b 1 )

echo.
echo [5/6] Baixando o modelo de IA para embutir no programa...
python -c "from ultralytics import YOLO; YOLO('yolov8n-pose.pt')"
if not exist "yolov8n-pose.pt" (
    echo Aviso: nao consegui baixar o modelo agora. O programa ainda funciona,
    echo mas vai baixar a IA na 1a vez que abrir ^(precisa de internet la^).
)
if not exist "icone.ico" (
    python -c "from PIL import Image,ImageDraw;S=256;im=Image.new('RGBA',(S,S),(23,74,140,255));d=ImageDraw.Draw(im);d.ellipse([44,44,212,212],outline=(55,214,122,255),width=10);d.ellipse([98,98,158,158],outline=(55,214,122,255),width=8);im.save('icone.ico',sizes=[(256,256),(64,64),(32,32),(16,16)])" 2>nul
)

echo.
echo [6/6] Empacotando o .exe (pode demorar varios minutos)...
python -m PyInstaller RastreadorPTZ.spec --noconfirm --clean
if errorlevel 1 ( echo. & echo ERRO ao empacotar. Anote a mensagem acima. & pause & exit /b 1 )

echo.
echo ============================================================
echo   PRONTO! O programa foi gerado em:
echo   %cd%\dist\RastreadorPTZ\RastreadorPTZ.exe
echo ============================================================
echo Agora rode o "2-CRIAR-INSTALADOR.bat" para gerar o instalador.
echo.
pause
