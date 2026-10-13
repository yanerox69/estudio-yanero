@echo off
rem Instalador de Estudio Yanero (Windows). Requiere: git, ffmpeg, Python 3.12, GPU NVIDIA para entrenar.
setlocal
cd /d "%~dp0"
set APPLIO_COMMIT=94f5d69

where ffmpeg >nul 2>nul || (echo Falta ffmpeg. Instalalo con: winget install Gyan.FFmpeg & exit /b 1)
where git >nul 2>nul || (echo Falta git. Instalalo con: winget install Git.Git & exit /b 1)

if not exist Applio (
  echo == Descargando Applio ==
  git clone https://github.com/IAHispano/Applio.git || exit /b 1
  git -C Applio checkout -q %APPLIO_COMMIT%
)

echo == Creando entorno de Python 3.12 ==
py -3.12 -m venv .venv || (echo Instala Python 3.12 desde python.org & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip

echo == Instalando dependencias (varios GB, paciencia) ==
pip install -r Applio\requirements-engine.txt --extra-index-url https://download.pytorch.org/whl/cu128 || exit /b 1
pip install -r requirements.txt || exit /b 1

echo == Descargando modelos base de Applio ==
pushd Applio
python core.py prerequisites --pretraineds-hifigan --models --exe || exit /b 1
popd

echo.
echo Listo. Arranca el estudio con doble clic en iniciar.bat
