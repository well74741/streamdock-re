@echo off
setlocal
cd /d "%~dp0"

set "PYW="
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe" set "PYW=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
if defined PYW goto run

for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW goto nopython

:run
start "" "%PYW%" "%~dp0app\server.py"
exit /b 0

:nopython
echo.
echo   Python 3.9+ not found.
echo   Install Python and make sure "python" works in cmd, then run this again.
echo.
pause
exit /b 1
