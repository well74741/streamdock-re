@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title M18 控制台 - 打包

echo ==========================================
echo   Stream Dock M18 控制台 · 一键打包
echo ==========================================
echo.

rem ---- 找一个真正能用的 Python（WindowsApps 里的是应用商店占位程序，不算）----
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PY (
    "%%i" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=%%i"
  )
)
if not defined PY (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PY if exist "%%d\python.exe" set "PY=%%d\python.exe"
  )
)
if not defined PY goto nopython

echo 使用 Python：%PY%
echo.
if "%PY%"=="py -3" (
  py -3 packaging\build.py %*
) else (
  "%PY%" packaging\build.py %*
)
if errorlevel 1 goto failed

set "CHECKONLY="
for %%a in (%*) do if /i "%%a"=="--check" set "CHECKONLY=1"
if defined CHECKONLY (
  echo.
  echo 环境检查通过，去掉 --check 再运行就会开始打包。
  pause
  exit /b 0
)

echo.
echo 打包完成，正在打开输出目录…
start "" explorer "%~dp0dist\release"
pause
exit /b 0

:failed
echo.
echo 打包没有完成，请按上面的提示处理后重新双击「打包.bat」。
pause
exit /b 1

:nopython
echo 没有找到 Python 3.9 或更高版本。
echo.
where winget >nul 2>nul
if errorlevel 1 goto manual
choice /c YN /m "要用 winget 自动安装 Python 3.12 吗"
if errorlevel 2 goto manual
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto manual
echo.
echo Python 已安装。请关闭这个窗口，重新双击「打包.bat」（新装的 Python 要在新窗口里才能找到）。
pause
exit /b 0

:manual
echo 请手动安装：https://www.python.org/downloads/
echo 安装时勾选「Add python.exe to PATH」和「tcl/tk and IDLE」，装好后重新双击「打包.bat」。
start "" https://www.python.org/downloads/
pause
exit /b 1
