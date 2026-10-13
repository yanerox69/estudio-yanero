#!/usr/bin/env bash
# Instalador de Estudio Yanero (Linux / macOS). Requiere: git, ffmpeg, Python 3.12, GPU NVIDIA para entrenar.
set -euo pipefail
cd "$(dirname "$0")"
APPLIO_COMMIT=94f5d69

command -v ffmpeg >/dev/null || { echo "Falta ffmpeg (Ubuntu: sudo apt install ffmpeg | Mac: brew install ffmpeg)"; exit 1; }
command -v git >/dev/null || { echo "Falta git"; exit 1; }

if [ ! -d Applio ]; then
  echo "== Descargando Applio (motor de entrenamiento RVC) =="
  git clone https://github.com/IAHispano/Applio.git
  git -C Applio checkout -q "$APPLIO_COMMIT"
fi

echo "== Creando entorno de Python 3.12 =="
if command -v uv >/dev/null; then
  uv venv .venv --python 3.12
  source .venv/bin/activate
  PIP="uv pip install --index-strategy unsafe-best-match"
else
  python3.12 -m venv .venv || { echo "Instala Python 3.12 o uv (https://docs.astral.sh/uv/)"; exit 1; }
  source .venv/bin/activate
  python -m pip install -q --upgrade pip
  PIP="pip install"
fi

echo "== Instalando dependencias (varios GB, paciencia) =="
$PIP -r Applio/requirements-engine.txt --extra-index-url https://download.pytorch.org/whl/cu128
$PIP -r requirements.txt

echo "== Descargando modelos base de Applio =="
(cd Applio && python core.py prerequisites --pretraineds-hifigan --models --exe)

echo
echo "Listo. Arranca el estudio con:  ./iniciar.sh"
