@echo off
title Instalador de Inicio Automatico de Mimir

echo ==================================================
echo Configurando Mimir para iniciar con Windows...
echo ==================================================
echo.

set SCRIPT_DIR=%~dp0
set TARGET_BAT=%SCRIPT_DIR%iniciar_mimir.bat
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_VBS=%TEMP%\CreateShortcutMimir.vbs

echo Set oWS = WScript.CreateObject("WScript.Shell") > "%SHORTCUT_VBS%"
echo sLinkFile = "%STARTUP_DIR%\Mimir.lnk" >> "%SHORTCUT_VBS%"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%SHORTCUT_VBS%"
echo oLink.TargetPath = "%TARGET_BAT%" >> "%SHORTCUT_VBS%"
echo oLink.WorkingDirectory = "%SCRIPT_DIR%" >> "%SHORTCUT_VBS%"
echo oLink.Description = "Mimir AI Assistant Autonomous Service" >> "%SHORTCUT_VBS%"
echo oLink.Save >> "%SHORTCUT_VBS%"

cscript //nologo "%SHORTCUT_VBS%"
if exist "%SHORTCUT_VBS%" del "%SHORTCUT_VBS%"

echo.
echo Mimir se ha anadido correctamente a la carpeta de Inicio de Windows!
echo Cada vez que enciendas tu ordenador, Mimir arrancara en espera pasiva.
echo.
