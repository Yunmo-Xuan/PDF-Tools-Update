@echo off
setlocal enableextensions
cd /d "%~dp0"

rem ============================================================================
rem  PDF Toolkit build (64-bit only).
rem
rem      build.bat   ->  dist\PDFToolkit\PDFToolkit.exe
rem                       then: iscc installer\pdf-toolkit.iss
rem                             ->  dist\PDFToolkit-<version>-Setup.exe
rem
rem  The interpreter on PATH is bare (no PyInstaller/pywebview/pymupdf), so the
rem  build runs inside its own venv (.venv-x64). The venv is reused only when it
rem  is actually importable, so a half-installed one heals instead of failing
rem  later at PyInstaller.
rem
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat in the OEM codepage
rem  (936 on Chinese Windows), so UTF-8 comments get misparsed and break the build.
rem ============================================================================

if not "%~1"=="" (
    echo [ERROR] The 32-bit build is gone - there is only one target now.
    echo         Usage: build.bat
    pause
    exit /b 1
)

set "DISTPATH=dist"
set "WORKPATH=build"
set "REQFILE=requirements.txt"
set "PY=.venv-x64\Scripts\python.exe"

"%PY%" -c "import webview, pymupdf, PyInstaller" >nul 2>nul
if not errorlevel 1 goto :header
echo [1/6] Creating 64-bit virtualenv .venv-x64 ...
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No base 64-bit Python found on PATH to create .venv-x64.
    pause
    exit /b 1
)
python -m venv .venv-x64
if errorlevel 1 goto :fail
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -r "%REQFILE%"
"%PY%" -m pip install --force-reinstall --no-deps -q "opencv-contrib-python==4.10.0.84"
if errorlevel 1 goto :fail

:header
echo ==========================================
echo   PDF Toolkit - Build  (x64)
echo ==========================================
echo.

echo [2/6] Generating version resources from core\constants.py ...
"%PY%" installer\make_version_info.py
if errorlevel 1 goto :fail
rem Do NOT quote the interpreter inside the backticked for /f command: cmd splits it
rem at the first space and yields an empty APPVER. The venv path is relative and
rem space-free, so leaving it unquoted is both safe and required.
for /f "usebackq delims=" %%v in (`%PY% -c "from core.constants import CURRENT_VERSION; print(CURRENT_VERSION)"`) do set "APPVER=%%v"
echo.

echo [3/6] Static check (pyflakes) ...
rem Why a gate: every bridge method runs inside a try/except, so a typo like a
rem missing import is swallowed into {"success": false} and the app still starts.
rem 6.0.4 hit exactly that (an interface reported success after a NameError), so
rem lint findings now stop the build instead of being caught by hand.
call :find_pyflakes
if errorlevel 1 goto :no_pyflakes
"%FLAKEPY%" -m pyflakes api core main.py installer
if errorlevel 1 (
    echo [ERROR] pyflakes reported problems above. Fix them before building.
    goto :fail
)
echo   pyflakes clean (using: %FLAKEPY%)
echo.

echo [4/6] Cleaning previous build ...
rem Only the unpacked app folder is removed - NOT the whole %DISTPATH% tree, which
rem also holds the built *-Setup.exe installers. Wiping dist here would destroy the
rem installer from the previous run before this one has produced a replacement.
if exist "%WORKPATH%" rmdir /s /q "%WORKPATH%"
if exist "%DISTPATH%\PDFToolkit" rmdir /s /q "%DISTPATH%\PDFToolkit"
if exist PDFToolkit.spec del /q PDFToolkit.spec
echo.

echo [5/6] Running PyInstaller ...
rem --onedir suits an installer build: faster startup, no per-launch extraction
rem to a temp dir, and far less likely to be flagged by antivirus than --onefile.
rem
rem The --exclude-module list matters a lot for size. PyInstaller follows lazy
rem imports (e.g. pdfplumber/display.py -> pandas, fontTools -> scipy,
rem webview/cef.py -> cefpython3) and otherwise drags in torch/paddle/scipy/cef,
rem which turns a ~150 MB build into ~1.2 GB. None of them are imported at
rem runtime by this app, so excluding them is safe.
rem
rem The nine --hidden-import lines are not decoration: core/i18n.py loads the nine
rem non-default dictionaries via importlib.import_module(<dict lookup>), which
rem PyInstaller's static graph cannot follow. Without them the installed exe shows
rem Simplified Chinese no matter which language the user picks. Keep in sync with
rem _MODULE in core/i18n.py (tests/e2e_installed.py LG2/LG3 go red if one is missing).
"%PY%" -m PyInstaller --noconfirm --clean --onedir --windowed ^
    --name "PDFToolkit" ^
    --distpath "%DISTPATH%" ^
    --workpath "%WORKPATH%" ^
    --icon "resources\pdf_icon.ico" ^
    --version-file "installer\version_info.txt" ^
    --add-data "assets;assets" ^
    --add-data "resources;resources" ^
    --additional-hooks-dir hooks ^
    --hidden-import core.lang_zh_tw ^
    --hidden-import core.lang_en ^
    --hidden-import core.lang_ja ^
    --hidden-import core.lang_ko ^
    --hidden-import core.lang_fr ^
    --hidden-import core.lang_ru ^
    --hidden-import core.lang_de ^
    --hidden-import core.lang_es ^
    --hidden-import core.lang_it ^
    --collect-all paddlex ^
    --collect-all paddleocr ^
    --collect-all chardet ^
    --copy-metadata paddlepaddle ^
    --copy-metadata paddleocr ^
    --copy-metadata paddlex ^
    --copy-metadata numpy ^
    --copy-metadata opencv-contrib-python ^
    --copy-metadata opencv-python-headless ^
    --exclude-module tkinter ^
    --exclude-module cefpython3 ^
    --exclude-module torch ^
    --exclude-module scipy ^
    --exclude-module matplotlib ^
    --exclude-module IPython ^
    --exclude-module notebook ^
    --exclude-module PyQt5 ^
    --exclude-module PyQt6 ^
    --exclude-module PySide2 ^
    --exclude-module PySide6 ^
    --exclude-module pytest ^
    main.py
if errorlevel 1 goto :fail
echo.

rem Paddle is collected by hooks\hook-paddle.py: enumerating paddle.jit.sot via
rem PyInstaller's isolated import crashes with 0xC0000005 on this machine.
rem PaddleX checks extras through importlib.metadata; PyInstaller only copies a few
rem dist-info folders. Copy all package metadata so frozen OCR can see dependencies.
echo   Copying package metadata ...
for /d %%D in (".venv-x64\Lib\site-packages\*.dist-info") do xcopy /E /I /Y "%%D" "%DISTPATH%\PDFToolkit\_internal\%%~nxD" >nul

echo [6/6] Build done.
echo   Portable  : %DISTPATH%\PDFToolkit\PDFToolkit.exe
echo   Installer : iscc installer\pdf-toolkit.iss
echo               -^> %DISTPATH%\PDFToolkit-%APPVER%-Setup.exe
echo.
pause
exit /b 0

rem Find an interpreter that actually has pyflakes. The build venv does not ship
rem it (it is a runtime env, and adding a lint tool there changes what gets
rem frozen into the package), so fall back to the interpreter on PATH.
rem Set FLAKEPY to point at a specific one when neither works.
:find_pyflakes
if not defined FLAKEPY set "FLAKEPY=%PY%"
"%FLAKEPY%" -m pyflakes --version >nul 2>nul
if not errorlevel 1 exit /b 0
set "FLAKEPY=python"
"%FLAKEPY%" -m pyflakes --version >nul 2>nul
if not errorlevel 1 exit /b 0
rem Neither the build venv nor PATH has it: put it into the build venv rather than
rem skipping the gate. It is a lint tool that nothing imports at runtime, so
rem PyInstaller does not freeze it in and requirements.txt stays the runtime list.
"%PY%" -m pip install -q pyflakes >nul 2>nul
set "FLAKEPY=%PY%"
"%FLAKEPY%" -m pyflakes --version >nul 2>nul
if not errorlevel 1 exit /b 0
exit /b 1

:no_pyflakes
echo [ERROR] pyflakes is not available, so the static check cannot run.
echo         The build venv and PATH were both probed, and installing it into the
echo         build venv failed - check that pip can reach its index.
echo         The gate is not skipped. Either way, point FLAKEPY at an interpreter
echo         that has it, e.g.
echo             set FLAKEPY=C:\path\to\venv\Scripts\python.exe
echo         then run build.bat again.
goto :fail

:fail
echo [ERROR] Build failed. See the output above.
pause
exit /b 1
