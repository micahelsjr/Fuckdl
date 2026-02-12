@echo off
chcp 65001 >nul
echo Running comprehensive cleanup and update operations...
echo.

REM Delete files
if exist ".\\fuckdl\\cookies\Max" (
    echo Deleting .\\fuckdl\\cookies\Max
    del /q ".\\fuckdl\\cookies\Max"
) else (
    echo File .\\fuckdl\\cookies\Max does not exist
)

if exist ".\download.Max.bat" (
    echo Deleting .\download.Max.bat
    del /q ".\download.Max.bat"
) else (
    echo File .\download.Max.bat does not exist
)

if exist ".\\fuckdl\\services\max.py" (
    echo Deleting .\\fuckdl\\services\max.py
    del /q ".\\fuckdl\\services\max.py"
) else (
    echo File .\\fuckdl\\services\max.py does not exist
)

if exist ".\\fuckdl\\services\peacock.py" (
    echo Deleting .\\fuckdl\\services\peacock.py
    del /q ".\\fuckdl\\services\peacock.py"
) else (
    echo File .\\fuckdl\\services\peacock.py does not exist
)

if exist ".\\fuckdl\\config\services\peacock.yml" (
    echo Deleting .\\fuckdl\\config\services\peacock.yml
    del /q ".\\fuckdl\\config\services\peacock.yml"
) else (
    echo File .\\fuckdl\\config\services\peacock.yml does not exist
)

if exist ".\download.Peacock.bat" (
    echo Deleting .\download.Peacock.bat
    del /q ".\download.Peacock.bat"
) else (
    echo File .\download.Peacock.bat does not exist
)

if exist ".\download.Peacock4k.bat" (
    echo Deleting .\download.Peacock4k.bat
    del /q ".\download.Peacock4k.bat"
) else (
    echo File .\download.Peacock4k.bat does not exist
)

if exist ".\download.Crunchyroll.bat" (
    echo Deleting .\download.Crunchyroll.bat
    del /q ".\download.Crunchyroll.bat"
) else (
    echo File .\download.Crunchyroll.bat does not exist
)

if exist ".\\fuckdl\\services\crunchyroll.py" (
    echo Deleting .\\fuckdl\\services\crunchyroll.py
    del /q ".\\fuckdl\\services\crunchyroll.py"
) else (
    echo File .\\fuckdl\\services\crunchyroll.py does not exist
)

if exist ".\\fuckdl\\config\services\crunchyroll.yml" (
    echo Deleting .\\fuckdl\\config\services\crunchyroll.yml
    del /q ".\\fuckdl\\config\services\crunchyroll.yml"
) else (
    echo File .\\fuckdl\\config\services\crunchyroll.yml does not exist
)

if exist ".\\fuckdl\\services\netflix.py" (
    echo Deleting .\\fuckdl\\services\netflix.py
    del /q ".\\fuckdl\\services\netflix.py"
) else (
    echo File .\\fuckdl\\services\netflix.py does not exist
)

REM Rename Netflix.new.py to netflix.py (note the case difference)
if exist ".\\fuckdl\\services\Netflix.new.py" (
    echo Renaming .\\fuckdl\\services\Netflix.new.py to .\\fuckdl\\services\netflix.py
    ren ".\\fuckdl\\services\Netflix.new.py" "netflix.py"
) else (
    echo File .\\fuckdl\\services\Netflix.new.py does not exist
)

REM Delete netflix.yml
if exist ".\\fuckdl\\config\services\netflix.yml" (
    echo Deleting .\\fuckdl\\config\services\netflix.yml
    del /q ".\\fuckdl\\config\services\netflix.yml"
) else (
    echo File .\\fuckdl\\config\services\netflix.yml does not exist
)

REM Rename netflix - Copy.yml. to netflix.yml (with trailing dot as shown)
if exist ".\\fuckdl\\config\services\netflix - Copy.yml" (
    echo Renaming .\\fuckdl\\config\services\netflix - Copy.yml to .\\fuckdl\\config\services\netflix.yml
    ren ".\\fuckdl\\config\services\netflix - Copy.yml" "netflix.yml"
) else (
    echo File .\\fuckdl\\config\services\netflix - Copy.yml does not exist
)

REM Alternative if the file has a trailing dot
if exist ".\\fuckdl\\config\services\netflix - Copy.yml." (
    echo Renaming .\\fuckdl\\config\services\netflix - Copy.yml. to .\\fuckdl\\config\services\netflix.yml
    ren ".\\fuckdl\\config\services\netflix - Copy.yml." "netflix.yml"
)

if exist ".\download.DiscoveryPlusUS.bat" (
    echo Deleting .\download.DiscoveryPlusUS.bat
    del /q ".\download.DiscoveryPlusUS.bat"
) else (
    echo File .\download.DiscoveryPlusUS.bat does not exist
)

echo.
echo All operations completed.
echo.
pause