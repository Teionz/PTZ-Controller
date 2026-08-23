@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Passo 2 - Criar o instalador (Setup.exe)

echo ============================================================
echo   PASSO 2 de 2  -  CRIAR O INSTALADOR (RastreadorPTZ_Setup.exe)
echo ============================================================
echo.

if not exist "dist\RastreadorPTZ\RastreadorPTZ.exe" (
    echo ERRO: o programa ainda nao foi compilado.
    echo Rode primeiro o "1-COMPILAR.bat".
    echo.
    pause & exit /b 1
)

echo Procurando o Inno Setup (ferramenta que monta o instalador)...
set "ISCC="
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
    "%ProgramFiles%\Inno Setup 5\ISCC.exe"
) do if exist "%%~P" set "ISCC=%%~P"
if not defined ISCC (
    for %%I in (ISCC.exe) do if exist "%%~$PATH:I" set "ISCC=%%~$PATH:I"
)

if not defined ISCC (
    echo.
    echo ------------------------------------------------------------
    echo   O Inno Setup NAO esta instalado nesta maquina.
    echo   Ele e gratuito e so precisa ser instalado uma vez.
    echo.
    echo   1^) Baixe em:  https://jrsoftware.org/isdl.php
    echo      ^(escolha "Inno Setup ... - Stable Release"^)
    echo   2^) Instale normalmente ^(pode aceitar tudo padrao^).
    echo   3^) Rode este "2-CRIAR-INSTALADOR.bat" de novo.
    echo ------------------------------------------------------------
    echo.
    pause & exit /b 1
)

echo Inno Setup encontrado: "%ISCC%"
echo.
echo Gerando o instalador... aguarde.
"%ISCC%" "instalador.iss"
if errorlevel 1 ( echo. & echo ERRO ao gerar o instalador. Anote a mensagem acima. & pause & exit /b 1 )

echo.
echo ============================================================
echo   PRONTO! Seu instalador esta em:
echo   %cd%\instalador_saida\RastreadorPTZ_Setup.exe
echo ============================================================
echo Leve ESSE arquivo para qualquer PC e clique 2x para instalar.
echo.
pause
