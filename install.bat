@echo off
echo Installing fuckdl...
echo.

REM Use full path to Python to avoid Windows Store alias
set PYTHON_PATH=C:\Program Files\Python312\python.exe

REM Check if Python exists
if not exist "%PYTHON_PATH%" (
    echo ERROR: Python not found at %PYTHON_PATH%
    echo Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

echo Using Python: %PYTHON_PATH%
"%PYTHON_PATH%" --version
echo.

echo Installing Poetry...
"%PYTHON_PATH%" -m pip install poetry
if errorlevel 1 (
    echo Failed to install Poetry
    pause
    exit /b 1
)

echo Configuring Poetry...
poetry config virtualenvs.in-project true

echo Installing dependencies...
poetry install

echo.
echo Installation complete!
pause