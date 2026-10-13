"""Entrenar sin interfaz (para Google Colab o un servidor).

    python -m estudio.entrenar_cli CARPETA_GRABACIONES --nombre mi-voz-tenor \
        --datos CARPETA_DATOS [--separar] [--salida voz.zip]

Si se corta a la mitad, vuelve a ejecutar el mismo comando: reanuda desde el último checkpoint.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import pipeline
from .dataset import EXT


class TrabajoConsola:
    cancelado = False

    def __init__(self, ruta_log: Path):
        self.ruta_log = ruta_log
        self._ultimo = ""

    def log(self, texto: str) -> None:
        with open(self.ruta_log, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {texto}\n")

    def paso(self, mensaje: str, progreso: float) -> None:
        if mensaje != self._ultimo:
            self._ultimo = mensaje
            print(f"[{time.strftime('%H:%M')}] {progreso*100:5.1f}%  {mensaje}", flush=True)
            self.log(mensaje)

    def dato(self, clave, valor) -> None:
        if clave == "dataset":
            print(f"   Dataset: {valor['minutos']} min útiles, {valor['clips']} clips, "
                  f"registro {valor.get('registro')}\n   {valor['veredicto']}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Entrena tu voz con RVC y la empaqueta en un zip")
    ap.add_argument("grabaciones", type=Path)
    ap.add_argument("--nombre", default="mi-voz-tenor")
    ap.add_argument("--datos", type=Path, default=Path.home() / "estudio-yanero-datos")
    ap.add_argument("--separar", action="store_true", help="Quitar música de fondo con Demucs")
    ap.add_argument("--salida", type=Path, default=None, help="Dónde dejar el zip de la voz entrenada")
    a = ap.parse_args(argv)

    archivos = sorted(p for p in a.grabaciones.rglob("*") if p.suffix.lower() in EXT)
    if not archivos and not (a.datos / "entrenos" / a.nombre / "dataset_reporte.json").exists():
        print(f"No hay audios ni videos en {a.grabaciones}")
        return 1
    a.datos.mkdir(parents=True, exist_ok=True)
    t = TrabajoConsola(a.datos / f"entreno_{a.nombre}.log")
    print(f"{len(archivos)} archivos encontrados. Registro detallado: {t.ruta_log}\n", flush=True)
    try:
        info = pipeline.entrenar(t, a.datos, a.nombre, archivos, a.separar)
    except pipeline.ErrorPaso as e:
        print(f"\nERROR: {e}")
        if t.ruta_log.exists():
            print("Últimas líneas del registro:")
            print("".join(t.ruta_log.read_text("utf-8").splitlines(True)[-25:]))
        return 1
    zip_ = a.salida or (a.datos / f"{a.nombre}_voz_entrenada.zip")
    pipeline.empaquetar_modelo(info, zip_)
    print(f"\nVoz entrenada lista: {zip_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
