@echo off
setlocal
pushd "%~dp0"
python -m PyInstaller --noconfirm SceneGrid.spec
if errorlevel 1 (
    echo Build failed. Close SceneGrid.exe and run this script again.
    popd
    exit /b 1
)
popd
