@echo off
cd /d D:\lazyscholar
"C:\Users\ASUS\AppData\Local\Programs\Python\Python313\python.exe" -m PyInstaller --noconfirm --windowed --onefile --name LazyScholar --icon lazyscholar.ico --collect-all webview --collect-all pystray --add-data "vendor;vendor" --add-data "lazyscholar.ico;." lazyscholar.py > D:\lazyscholar\build.log 2>&1
echo BUILD_EXIT=%ERRORLEVEL% >> D:\lazyscholar\build.log
