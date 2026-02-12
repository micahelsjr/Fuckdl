@echo off
echo ================================
echo fuckdl v1.0.0
echo DRM Downloader
echo ================================
echo.

set PYTHON_PATH=C:\Program Files\Python312\python.exe

echo Checking Python installation...
if exist "%PYTHON_PATH%" (
    "%PYTHON_PATH%" --version
    echo [OK] Python is installed
) else (
    echo [ERROR] Python not found!
    echo Please install Python from python.org
    pause
    exit /b 1
)
echo.

echo Checking Poetry installation...
echo.

poetry --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Poetry is not installed!
    echo Please run install.bat first.
    pause
    exit /b 1
)

echo [OK] Poetry is installed
echo.

if exist "fuckdl\fuckdl.py" (
    echo [OK] Main application found
) else (
    echo [ERROR] Main application not found!
    pause
    exit /b 1
)

echo.
echo Installation appears to be correct!
echo.
echo Available commands:
echo   poetry run fuckdl dl [options] SERVICE URL
echo.
echo Example services: Netflix, DisneyPlus, Amazon, Max, Hulu, Peacock
echo.
echo Or use the pre-configured batch files like:
echo   download.Netflix.bat
echo   download.DisneyPlus.bat
echo   etc.
echo.
pause
