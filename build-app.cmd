@echo off
setlocal
cd /d D:\lazyscholar
set PY="C:\Users\ASUS\AppData\Local\Programs\Python\Python313\python.exe"
set ICONARG=
if exist "D:\lazyscholar\lazyscholar.ico" set ICONARG=--icon "D:\lazyscholar\lazyscholar.ico" --add-data "lazyscholar.ico;."
%PY% -m PyInstaller --noconfirm --clean --windowed --onefile --name LazyScholar %ICONARG% --collect-all webview --collect-all pystray --add-data "vendor;vendor" lazyscholar.py > D:\lazyscholar\build.log 2>&1
echo BUILD_EXIT=%ERRORLEVEL% >> D:\lazyscholar\build.log
endlocal
