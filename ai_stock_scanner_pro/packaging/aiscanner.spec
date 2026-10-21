# PyInstaller spec – run from the project root:  pyinstaller packaging/aiscanner.spec --noconfirm
# One-folder build (faster start, easier AV scanning than one-file). Output: dist/AIStockScannerPro/AIStockScannerPro.exe (+ AIStockScannerPro-cli.exe)
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(ROOT, "config"), "config"), (os.path.join(ROOT, "examples"), "examples")]
datas += collect_data_files("tzdata")
datas += collect_data_files("yfinance")          # yfinance ships data files (e.g. tz cache helpers)
hidden = collect_submodules("aiscanner") + ["yfinance", "curl_cffi", "multitasking", "peewee", "platformdirs", "websockets"]

a = Analysis([os.path.join(ROOT, "run_app.py")], pathex=[ROOT], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "IPython", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
                       "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
                       "PySide6.QtPdf", "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtBluetooth"])
pyz = PYZ(a.pure)
# GUI exe (no console window) + a console twin for headless jobs / self-test (windowed exes have no stdout)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AIStockScannerPro", console=False, debug=False, upx=False)
exe_cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AIStockScannerPro-cli", console=True, debug=False, upx=False)
coll = COLLECT(exe, exe_cli, a.binaries, a.datas, name="AIStockScannerPro", upx=False)
