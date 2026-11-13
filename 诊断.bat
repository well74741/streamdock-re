@echo off
chcp 65001 >nul
title M18 控制台 - 安装 / 运行问题诊断
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\diagnose.ps1" %*
echo.
pause
