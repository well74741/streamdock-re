@echo off
chcp 65001 >nul
title M18 Install Check
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\install-check.ps1" %*
echo.
pause
