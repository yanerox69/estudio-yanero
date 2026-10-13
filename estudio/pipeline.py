"""Los pasos automáticos: preparar dataset -> entrenar RVC (Applio) -> cantar.

Cada paso es una función que recibe un `Trabajo` para reportar avance.
Todo lo pesado corre en subprocesos con el mismo Python del entorno.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from .dataset import tono_medio

APPLIO = Path(os.environ.get("ESTUDIO_APPLIO", Path(__file__).resolve().parent.parent / "Applio"))
SR_ENTRENO = 40000
PY = sys.executable


class ErrorPaso(RuntimeError):
    pass


# ---------------------------------------------------------------- utilidades

def correr(cmd: list[str], trabajo, *, cwd: Path | None = None, env: dict | None = None,
           vigilar=None, cada: float = 15.0) -> None:
    """Ejecuta un comando volcando la salida al log del trabajo.
    `vigilar` es una función opcional que se llama periódicamente para actualizar progreso."""
    trabajo.log(f"$ {' '.join(str(c) for c in cmd)}")
    with open(trabajo.ruta_log, "a", encoding="utf-8") as log:
        p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env, stdout=log,
                             stderr=subprocess.STDOUT)
        while True:
            try:
                rc = p.wait(timeout=cada)
                break
            except subprocess.TimeoutExpired:
                if trabajo.cancelado:
                    p.terminate()
                    raise ErrorPaso("Cancelado por el usuario.")
                if vigilar:
                    vigilar()
    if vigilar:
        vigilar()
    if rc != 0:
        raise ErrorPaso(f"El paso falló (código {rc}). Revisa el registro del trabajo.")


def env_applio(datos: Path) -> dict:
    env = os.environ.copy()
    env["APPLIO_LOGS_DIR"] = str(datos / "modelos")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def lotes_por_vram() -> int:
    try:
        import torch
        if not torch.cuda.is_available():
            return 4
        gb = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
        return 4 if gb < 6 else 8 if gb < 10 else 12 if gb < 16 else 16
    except Exception:
        return 4


def hay_gpu() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def separar(wav: Path, carpeta: Path, trabajo) -> tuple[Path, Path]:
    """Demucs: devuelve (voz, instrumental)."""
    correr([PY, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
            "-o", str(carpeta), str(wav)], trabajo)
    voz = next(carpeta.rglob("vocals.wav"), None)
    inst = next(carpeta.rglob("no_vocals.wav"), None)
    if not voz or not inst:
        raise ErrorPaso("Demucs no generó las pistas separadas.")
    return voz, inst


def a_wav(origen: Path, destino: Path, sr: int = 44100, mono: bool = False) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(origen), "-vn",
           "-ar", str(sr)] + (["-ac", "1"] if mono else []) + ["-c:a", "pcm_s16le", str(destino)]
    if subprocess.run(cmd, capture_output=True).returncode != 0 or not destino.exists():
        raise ErrorPaso(f"No pude leer audio de {origen.name}.")


def mediana_tono(wav: Path) -> float | None:
    y, sr = sf.read(wav, dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = y[: sr * 120]                       # basta con 2 minutos
    f0 = tono_medio(y / (np.abs(y).max() + 1e-9), sr)
    return float(np.median(f0)) if len(f0) > 10 else None


def tono_dataset(clips: Path, muestras: int = 40) -> float | None:
    """Tono mediano de tu voz, a partir de una muestra repartida de clips."""
    todos = sorted(clips.glob("*.wav"))
    f0: list[float] = []
    for f in todos[:: max(1, len(todos) // muestras)]:
        y, sr = sf.read(f, dtype="float32")
        f0 += tono_medio(y, sr)
    return float(np.median(f0)) if len(f0) > 10 else None


def empaquetar_modelo(info: dict, destino: Path) -> Path:
    """Zip descargable con la voz entrenada: .pth + .index + ficha."""
    import zipfile
    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(info["pth"], f"{info['nombre']}.pth")
        if info.get("index"):
            z.write(info["index"], f"{info['nombre']}.index")
        ficha = {k: v for k, v in info.items() if k not in ("pth", "index")}
        z.writestr("ficha.json", json.dumps(ficha, ensure_ascii=False, indent=2))
        z.writestr("LEEME.txt",
                   f"Voz entrenada: {info['nombre']} (RVC v2, {SR_ENTRENO} Hz, f0 rmvpe)\n\n"
                   "Uso en Applio: copia el .pth y el .index a una carpeta dentro de Applio/logs/ "
                   "y elige el modelo en la pestaña de inferencia.\n"
                   "Sirve en cualquier programa compatible con modelos RVC v2.\n"
                   "Uso personal: es tu voz; no la compartas si no quieres que otros canten con ella.\n")
    return destino


# ---------------------------------------------------------------- entrenar

def entrenar(trabajo, datos: Path, nombre: str, archivos: list[Path], separar_voz: bool) -> dict:
    """Pasos 1–5 del entrenamiento. Devuelve la info del modelo resultante."""
    if not (APPLIO / "core.py").exists():
        raise ErrorPaso(f"No encuentro Applio en {APPLIO}. Ejecuta el instalador.")
    if not hay_gpu():
        raise ErrorPaso("No hay GPU NVIDIA disponible. Entrenar en CPU tardaría días: "
                        "usa el cuaderno de Google Colab incluido.")

    base = datos / "entrenos" / nombre
    crudos, clips = base / "grabaciones", base / "dataset"
    carpeta_modelo = datos / "modelos" / nombre
    env = env_applio(datos)
    rep_path = base / "dataset_reporte.json"

    # Reanudar: si ya hay checkpoints de un intento anterior, se salta directo al entrenamiento
    reanudar = rep_path.exists() and any(carpeta_modelo.glob("G_*.pth"))
    if reanudar:
        trabajo.log("Se encontró un entrenamiento previo: se reanuda desde el último checkpoint.")
    else:
        import shutil
        crudos.mkdir(parents=True, exist_ok=True)
        for a in archivos:
            shutil.copy2(a, crudos / a.name)

        # 1. dataset
        trabajo.paso("Preparando tus grabaciones", 0.02)
        correr([PY, "-m", "estudio.dataset", crudos, clips, "--sr", str(SR_ENTRENO)]
               + (["--separar"] if separar_voz else []), trabajo,
               cwd=Path(__file__).resolve().parent.parent)

    reporte = json.loads(rep_path.read_text("utf-8"))
    trabajo.dato("dataset", reporte)
    if reporte["minutos"] < 5:
        raise ErrorPaso(f"Solo hay {reporte['minutos']} min de voz útil; el mínimo es 5 "
                        "(lo ideal 10–30). Sube más grabaciones.")
    epocas = int(reporte["epocas_sugeridas"])

    if not reanudar:
        # 2. preproceso
        trabajo.paso("Procesando audio para el entrenamiento", 0.08)
        correr([PY, "core.py", "preprocess", "--model-name", nombre, "--dataset-path", clips,
                "--sample-rate", str(SR_ENTRENO), "--cut-preprocess", "Skip"], trabajo, cwd=APPLIO, env=env)

        # 3. extracción de tono y rasgos
        trabajo.paso("Analizando tono y timbre", 0.15)
        correr([PY, "core.py", "extract", "--model-name", nombre, "--f0-method", "rmvpe",
                "--sample-rate", str(SR_ENTRENO), "--gpu", "0"], trabajo, cwd=APPLIO, env=env)

    # 4. entrenamiento (+ índice, que Applio genera al terminar)
    patron = re.compile(rf"^{re.escape(nombre)}_(\d+)e_\d+s\.pth$")

    def avance():
        ep = max((int(m.group(1)) for f in carpeta_modelo.glob("*.pth")
                  if (m := patron.match(f.name))), default=0)
        trabajo.paso(f"Entrenando tu voz: época {ep} de {epocas}", 0.2 + 0.75 * ep / epocas)

    trabajo.paso(f"Entrenando tu voz: época 0 de {epocas}", 0.2)
    correr([PY, "core.py", "train", "--model-name", nombre, "--save-every-epoch", "10",
            "--save-only-latest", "--total-epoch", str(epocas), "--sample-rate", str(SR_ENTRENO),
            "--batch-size", str(lotes_por_vram()), "--gpu", "0", "--index-algorithm", "Auto"],
           trabajo, cwd=APPLIO, env=env, vigilar=avance, cada=30)

    # 5. localizar resultado
    pesos = sorted((f for f in carpeta_modelo.glob("*.pth") if patron.match(f.name)),
                   key=lambda f: int(patron.match(f.name).group(1)))
    indice = next(iter(sorted(carpeta_modelo.glob("*.index"))), None)
    if not pesos:
        raise ErrorPaso("El entrenamiento terminó sin generar el modelo.")
    tono = tono_dataset(clips)
    info = {"nombre": nombre, "pth": str(pesos[-1]), "index": str(indice) if indice else "",
            "epocas": epocas, "minutos": reporte["minutos"], "registro": reporte["registro"],
            "tono_mediano": tono, "creado": time.time()}
    (carpeta_modelo / "estudio.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
    trabajo.paso("Modelo listo", 1.0)
    return info


# ---------------------------------------------------------------- cantar

def cantar(trabajo, datos: Path, modelo: dict, cancion: Path, *, es_mezcla: bool,
           semitonos: int | None, salida_dir: Path) -> dict:
    """Convierte la voz de una canción (o guía a capela) a tu timbre y la remezcla."""
    salida_dir.mkdir(parents=True, exist_ok=True)
    env = env_applio(datos)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wav = tmp / "entrada.wav"
        a_wav(cancion, wav)

        if es_mezcla:
            trabajo.paso("Separando la voz del instrumental", 0.1)
            voz, inst = separar(wav, tmp / "sep", trabajo)
        else:
            voz, inst = wav, None

        # tono: con instrumental solo se puede mover por octavas para no desafinar
        if semitonos is None:
            semitonos = 0
            guia, propio = mediana_tono(voz), modelo.get("tono_mediano")
            if guia and propio:
                dif = 12 * np.log2(propio / guia)
                semitonos = int(12 * round(dif / 12)) if es_mezcla else int(round(dif))
                semitonos = max(-24, min(24, semitonos))
            trabajo.log(f"Ajuste de tono automático: {semitonos:+d} semitonos")
        trabajo.dato("semitonos", semitonos)

        trabajo.paso("Cantando con tu voz", 0.45)
        voz_conv = tmp / "voz_convertida.wav"
        correr([PY, "core.py", "infer", "--input-path", voz, "--output-path", voz_conv,
                "--pth-path", modelo["pth"], "--index-path", modelo.get("index") or "",
                "--pitch", str(semitonos), "--f0-method", "rmvpe", "--index-rate", "0.5",
                "--protect", "0.33", "--volume-envelope", "1", "--export-format", "WAV"],
               trabajo, cwd=APPLIO, env=env)
        if not voz_conv.exists():
            raise ErrorPaso("La conversión no produjo audio.")

        base = re.sub(r"[^\w\-]+", "_", cancion.stem)[:50]
        final_voz = salida_dir / f"{base}_voz.wav"
        a_wav(voz_conv, final_voz)
        resultado = {"voz": final_voz.name}

        if inst is not None:
            trabajo.paso("Mezclando con el instrumental", 0.85)
            mezcla = salida_dir / f"{base}_mezcla.wav"
            r = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(final_voz), "-i", str(inst),
                 "-filter_complex",
                 "[0:a]volume=1.0[v];[1:a]volume=0.9[i];[v][i]amix=inputs=2:duration=longest:normalize=0,"
                 "alimiter=limit=0.95",
                 "-ar", "44100", "-c:a", "pcm_s16le", str(mezcla)], capture_output=True, text=True)
            if r.returncode != 0:
                raise ErrorPaso("No pude mezclar: " + r.stderr[-200:])
            resultado["mezcla"] = mezcla.name

    trabajo.paso("Listo", 1.0)
    return resultado
