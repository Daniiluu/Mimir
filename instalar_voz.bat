@echo off
title Instalador de Dependencias de Voz para Mimir
chcp 65001 > nul
echo.
echo ================================================
echo   Instalando dependencias Python para Mimir Voz
echo ================================================
echo.

:: Verificar Python
python --version 2>nul
if errorlevel 1 (
    echo [ERROR] Python no encontrado. Instala Python 3.11+ desde https://python.org
    pause & exit /b 1
)

:: Instalar dependencias
echo Instalando paquetes Python...
pip install -r requirements_voz.txt

if errorlevel 1 (
    echo.
    echo [ERROR] Fallo al instalar dependencias. Revisa que tengas los C++ Build Tools.
    echo         Descarga desde: https://visualstudio.microsoft.com/visual-cpp-build-tools/
    pause & exit /b 1
)

echo.
echo [OK] Dependencias instaladas correctamente.
echo Los modelos de Whisper se descargan automaticamente en la primera ejecucion.
echo.
pause
