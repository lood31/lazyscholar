@echo off
setlocal
cd /d D:\lazyscholar
set PY="%~dp0.venv\Scripts\python.exe"
set ICONARG=
if exist "D:\lazyscholar\lazyscholar.ico" set ICONARG=--icon "D:\lazyscholar\lazyscholar.ico" --add-data "lazyscholar.ico;."
%PY% -m PyInstaller --noconfirm --clean --windowed --onefile --name LazyScholar %ICONARG% --collect-all webview --collect-all pystray --collect-all pdfplumber --collect-all pdfminer --collect-all pypdfium2 --collect-all bleach --collect-all bs4 --collect-all defusedxml --copy-metadata pywin32 --add-data "vendor;vendor" lazyscholar.py > D:\lazyscholar\build.log 2>&1
echo BUILD_EXIT=%ERRORLEVEL% >> D:\lazyscholar\build.log
endlocal
