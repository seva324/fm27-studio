@echo off
setlocal
cd /d "%~dp0"

set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
    "%PY312%" -S "%~dp0launch_studio.py" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 -S "%~dp0launch_studio.py" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python -S "%~dp0launch_studio.py" %*
    exit /b %ERRORLEVEL%
)

echo [FM27 Studio] Python 3.10+ was not found on PATH.
echo Please install Python 3.12 from https://www.python.org/downloads/ and try again.
pause
exit /b 1
