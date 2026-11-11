@echo off
chcp 65001 >nul
cd /d "%~dp0"
python qci_check.py %*
if errorlevel 9009 py -3 qci_check.py %*
echo.
pause
