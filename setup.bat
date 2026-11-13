@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   StreamDock M18 console - setup
echo ==========================================
echo.

set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY goto nopython

echo [1/3] Python found: %PY%
"%PY%" -c "import PIL" >nul 2>nul
if errorlevel 1 (
  echo       Installing Pillow...
  "%PY%" -m pip install --upgrade pip >nul
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 goto pipfail
) else (
  echo       Pillow already installed.
)

echo [2/3] Checking official SDK...
if exist "ws-sdk\StreamDockWebsocketSDK.exe" (
  echo       OK: ws-sdk\StreamDockWebsocketSDK.exe
) else (
  echo       [!] Missing ws-sdk\StreamDockWebsocketSDK.exe
  echo           Unzip the official WebSocket SDK Windows package into .\ws-sdk\
)

echo [3/3] Checking config...
if exist "app\config.json" (
  echo       OK: app\config.json
) else (
  copy "app\config.example.json" "app\config.json" >nul
  echo       Created app\config.json from template.
)

echo.
echo Done. Double-click "启动 M18 控制台.bat" to start the console.
echo.
pause
exit /b 0

:pipfail
echo.
echo   Pillow installation failed. Check your network / pip mirror.
pause
exit /b 1

:nopython
echo.
echo   Python 3.9+ not found.
echo   Install from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH".
echo.
pause
exit /b 1
