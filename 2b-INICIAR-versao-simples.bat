@echo off
chcp 65001 >nul
title Rastreador PTZ - versao simples (reserva)
cd /d "%~dp0"
echo Abrindo a versao SIMPLES (so a janela de video, sem paineis).
echo Use esta se a versao profissional der problema.
echo.
python ptz_tracker.py
if errorlevel 1 (
    echo.
    echo O programa fechou com erro. Anote a mensagem acima.
    echo.
    pause
)
