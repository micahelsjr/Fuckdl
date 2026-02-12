@echo off
echo =========================================
echo Python Windows Store Alias Fix
echo =========================================
echo.
echo This script will help you disable the Windows Store Python alias
echo that interferes with the real Python installation.
echo.
echo MANUAL STEPS:
echo.
echo 1. Press Windows Key + R
echo 2. Type: ms-settings:appsfeatures-app
echo 3. Press Enter
echo.
echo 4. Click on "App execution aliases" on the right side
echo.
echo 5. Find these two items and turn them OFF:
echo    - App Installer python.exe
echo    - App Installer python3.exe
echo.
echo 6. Close Settings and run install.bat again
echo.
echo =========================================
echo.
echo Alternative: We can also verify Python is working...
echo.

set PYTHON_PATH=C:\Program Files\Python312\python.exe

if exist "%PYTHON_PATH%" (
    echo [OK] Python found at: %PYTHON_PATH%
    "%PYTHON_PATH%" --version
    echo.
    echo You can use this Python directly by running:
    echo   "%PYTHON_PATH%" -m pip install poetry
    echo   poetry config virtualenvs.in-project true
    echo   poetry install
) else (
    echo [ERROR] Python not found at: %PYTHON_PATH%
    echo.
    echo Please install Python 3.10+ from:
    echo https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during installation!
)

echo.
pause
