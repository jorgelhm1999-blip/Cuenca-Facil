@echo off
setlocal
cd /d "%~dp0"
if not exist "bin\whitebox_tools.exe" (
  echo Falta bin\whitebox_tools.exe. Usa el flujo de GitHub Actions o descárgalo desde WhiteboxTools.
  exit /b 1
)
py -3.12 -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
py -3.12 -m PyInstaller --noconfirm --clean --onedir --windowed --name CuencaFacil --add-binary "bin\whitebox_tools.exe;bin" --collect-all rasterio --collect-all fiona app.py
if errorlevel 1 exit /b 1
echo Ejecutable: dist\CuencaFacil\CuencaFacil.exe
