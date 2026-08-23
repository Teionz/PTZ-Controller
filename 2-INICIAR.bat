@echo off
chcp 65001 >nul
title Rastreador PTZ - Profissional
cd /d "%~dp0"
python ptz_app.py
if errorlevel 1 (
    echo.
    echo O programa fechou com erro. Anote a mensagem acima.
    echo.
    pause
)
