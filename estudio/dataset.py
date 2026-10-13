#!/usr/bin/env python3
"""Prepara un dataset RVC a partir de tus grabaciones (audio o video).

Uso:
    python preparar_dataset.py MIS_GRABACIONES/ dataset/ [--separar] [--sr 40000]

Qué hace con cada archivo:
  1. ffmpeg extrae el audio (mp3, wav, flac, m4a, mp4, mov, mkv, webm...).
  2. --separar: Demucs quita la pista/instrumental y deja solo la voz.
  3. Corta en clips de 3 a 10 s por los silencios (respiraciones entre frases).
  4. Descarta clips saturados, casi mudos o demasiado cortos.
  5. Normaliza cada clip a -1 dBFS y lo guarda en mono WAV.
Al final imprime cuántos minutos útiles tienes y qué tan amplio es tu registro.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
       ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MIN_CLIP, MAX_CLIP = 3.0, 10.0
NOTAS = ["Do", "Do#", "Re", "Re#", "Mi", "Fa", "Fa#", "Sol", "Sol#", "La", "La#", "Si"]


def nota(hz: float) -> str:
    midi = int(round(69 + 12 * np.log2(hz / 440.0)))
    return f"{NOTAS[midi % 12]}{midi // 12 - 1}"


def ffmpeg_a_wav(origen: Path, destino: Path, sr: int) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(origen),
         "-vn", "-ac", "1", "-ar", str(sr), "-af", "highpass=f=60", "-c:a", "pcm_s16le", str(destino)],
        capture_output=True, text=True)
    return r.returncode == 0 and destino.exists() and destino.stat().st_size > 1000


def separar_voz(wav: Path, tmp: Path, sr: int) -> Path | None:
    r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
                        "-o", str(tmp / "demucs"), str(wav)], capture_output=True, text=True)
    voces = list((tmp / "demucs").rglob("vocals.wav"))
    if r.returncode != 0 or not voces:
        print("   ! Demucs falló:", r.stderr.strip()[-200:])
        return None
    salida = tmp / "solo_voz.wav"
    return salida if ffmpeg_a_wav(voces[0], salida, sr) else None


def niveles_db(y: np.ndarray, hop: int) -> np.ndarray:
    n = len(y) // hop
    tramas = y[: n * hop].reshape(n, hop)
    return 20 * np.log10(np.sqrt((tramas ** 2).mean(axis=1)) + 1e-9)


def cortar(y: np.ndarray, sr: int) -> list[tuple[int, int]]:
    """Corta por silencios; une frases cortas y parte las largas en el silencio más profundo."""
    hop = int(0.02 * sr)
    db = niveles_db(y, hop)
    if len(db) == 0:
        return []
    umbral = np.percentile(db, 90) - 35
    voz = db > umbral

    # tramos con voz, tolerando huecos de < 0.3 s (consonantes, ataques)
    tramos, ini, hueco = [], None, 0
    for i, v in enumerate(voz):
        if v:
            if ini is None:
                ini = i
            hueco = 0
        elif ini is not None:
            hueco += 1
            if hueco * hop / sr > 0.3:
                tramos.append([ini, i - hueco + 1])
                ini, hueco = None, 0
    if ini is not None:
        tramos.append([ini, len(voz)])

    # une tramos vecinos mientras quepan en MAX_CLIP
    unidos: list[list[int]] = []
    for t in tramos:
        if unidos and (t[1] - unidos[-1][0]) * hop / sr <= MAX_CLIP and (t[0] - unidos[-1][1]) * hop / sr < 1.0:
            unidos[-1][1] = t[1]
        else:
            unidos.append(list(t))

    # parte los largos en el punto más silencioso
    final: list[tuple[int, int]] = []
    pend = unidos[:]
    while pend:
        a, b = pend.pop(0)
        if (b - a) * hop / sr <= MAX_CLIP:
            final.append((a, b))
            continue
        lo, hi = a + int(MIN_CLIP * sr / hop), min(b - int(MIN_CLIP * sr / hop), a + int(MAX_CLIP * sr / hop))
        if hi <= lo:
            corte = a + int(MAX_CLIP * sr / hop)
        else:
            corte = lo + int(np.argmin(db[lo:hi]))
        pend[:0] = [(a, corte), (corte, b)]

    margen = int(0.1 * sr)
    return [(max(0, a * hop - margen), min(len(y), b * hop + margen))
            for a, b in final if (b - a) * hop / sr >= MIN_CLIP]


def tono_medio(y: np.ndarray, sr: int) -> list[float]:
    """Estimación rápida de f0 por autocorrelación (solo para el reporte de registro)."""
    marco, paso = int(0.04 * sr), int(0.02 * sr)
    fmin, fmax = 70, 1100
    f0s = []
    for i in range(0, len(y) - marco, paso * 4):
        x = y[i:i + marco]
        if np.sqrt((x ** 2).mean()) < 0.02:
            continue
        x = x - x.mean()
        ac = np.correlate(x, x, "full")[marco - 1:]
        lo, hi = int(sr / fmax), int(sr / fmin)
        if hi >= len(ac):
            continue
        # salta el lóbulo inicial (ac decrece desde 0) hasta el primer cruce por cero
        neg = np.where(ac[lo:hi] < 0)[0]
        if len(neg) == 0:
            continue
        lo = lo + int(neg[0])
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.45 * ac[0]:
            f0s.append(sr / k)
    return f0s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prepara tu dataset de voz para RVC")
    ap.add_argument("entrada", type=Path, help="Carpeta con tus grabaciones (audio o video)")
    ap.add_argument("salida", type=Path, help="Carpeta donde dejar los clips")
    ap.add_argument("--separar", action="store_true", help="Quitar instrumental con Demucs")
    ap.add_argument("--sr", type=int, default=40000, choices=[32000, 40000, 48000])
    a = ap.parse_args(argv)

    if not shutil.which("ffmpeg"):
        print("Falta ffmpeg. Instálalo primero.")
        return 1
    if a.separar:
        try:
            import demucs  # noqa: F401
        except ImportError:
            print("Para --separar instala demucs:  pip install demucs")
            return 1

    archivos = sorted(p for p in a.entrada.rglob("*") if p.suffix.lower() in EXT)
    if not archivos:
        print(f"No encontré audios ni videos en {a.entrada}")
        return 1
    a.salida.mkdir(parents=True, exist_ok=True)

    total_s, n_clips, desc = 0.0, 0, {"saturado": 0, "debil": 0}
    f0_todo: list[float] = []
    reporte = []
    for n, arch in enumerate(archivos, 1):
        print(f"[{n}/{len(archivos)}] {arch.name}")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wav = tmp / "audio.wav"
            if not ffmpeg_a_wav(arch, wav, a.sr):
                print("   ! sin audio legible, se omite")
                continue
            if a.separar:
                sep = separar_voz(wav, tmp, a.sr)
                if sep is None:
                    continue
                wav = sep
            y, sr = sf.read(wav, dtype="float32")
            if y.ndim > 1:
                y = y.mean(axis=1)

        clips_archivo, seg_archivo = 0, 0.0
        for i, (s, e) in enumerate(cortar(y, sr)):
            c = y[s:e]
            pico = np.abs(c).max()
            if (np.abs(c) > 0.995).mean() > 0.001:
                desc["saturado"] += 1
                continue
            if pico < 0.03:
                desc["debil"] += 1
                continue
            c = c / pico * 0.89
            f = int(0.02 * sr)
            c[:f] *= np.linspace(0, 1, f)
            c[-f:] *= np.linspace(1, 0, f)
            sf.write(a.salida / f"{arch.stem[:40]}_{i:04d}.wav", c, sr, subtype="PCM_16")
            f0_todo += tono_medio(c, sr)
            clips_archivo += 1
            seg_archivo += len(c) / sr
        total_s += seg_archivo
        n_clips += clips_archivo
        reporte.append({"archivo": arch.name, "clips": clips_archivo, "segundos": round(seg_archivo, 1)})
        print(f"   {clips_archivo} clips, {seg_archivo:.0f} s")

    minutos = total_s / 60
    print("\n=== RESUMEN ===")
    print(f"Clips útiles: {n_clips}  |  Voz total: {minutos:.1f} min")
    print(f"Descartados: {desc['saturado']} saturados, {desc['debil']} demasiado débiles")
    registro = None
    if len(f0_todo) > 20:
        bajo, alto = np.percentile(f0_todo, 5), np.percentile(f0_todo, 95)
        registro = [nota(bajo), nota(alto)]
        print(f"Registro aproximado cubierto: {registro[0]} – {registro[1]}")
        print("   El modelo solo cantará bien dentro de ese rango. Incluye tus agudos (Sol4–Do5) si quieres usarlos.")

    if minutos < 5:
        veredicto = "INSUFICIENTE: con menos de 5 min el modelo sale inestable. Junta más material."
    elif minutos < 10:
        veredicto = "JUSTO: entrena, pero espera artefactos. 10–30 min es lo recomendado."
    elif minutos <= 40:
        veredicto = "BIEN: cantidad adecuada. La calidad del audio pesa más que sumar minutos."
    else:
        veredicto = "MUCHO: más de 40 min alarga el entrenamiento sin mejorar mucho. Quédate con lo más limpio."
    print(veredicto)
    epocas = 300 if minutos < 10 else 250 if minutos < 25 else 200
    print(f"Épocas sugeridas: {epocas} (revisa a partir de la mitad y quédate con la que mejor suene)")

    (a.salida.parent / f"{a.salida.name}_reporte.json").write_text(json.dumps({
        "minutos": round(minutos, 2), "clips": n_clips, "descartados": desc,
        "registro": registro, "sample_rate": a.sr, "epocas_sugeridas": epocas,
        "veredicto": veredicto, "archivos": reporte}, ensure_ascii=False, indent=2), "utf-8")
    return 0 if minutos >= 5 else 2


if __name__ == "__main__":
    sys.exit(main())
