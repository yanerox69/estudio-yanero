"""Estudio Yanero: subes tus grabaciones, entrena tu voz, la descargas y canta con ella."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from . import pipeline
from .trabajos import Cola

DATOS = Path(os.environ.get("ESTUDIO_DATOS", Path.home() / "estudio-yanero-datos"))
MAX_MB = int(os.environ.get("ESTUDIO_MAX_MB", "2000"))
EXT_OK = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def slug(nombre: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", nombre.strip().lower()).strip("-")[:40]
    if not s:
        raise HTTPException(400, "Ponle un nombre a la voz (letras o números).")
    return s


async def guardar_subida(archivo: UploadFile, carpeta: Path, permitidas=EXT_OK) -> Path:
    nombre = Path(archivo.filename or "archivo").name
    if Path(nombre).suffix.lower() not in permitidas:
        raise HTTPException(400, f"Formato no admitido: {nombre}")
    destino = carpeta / re.sub(r"[^\w.\-]+", "_", nombre)
    total = 0
    with destino.open("wb") as f:
        while bloque := await archivo.read(1 << 20):
            total += len(bloque)
            if total > MAX_MB << 20:
                raise HTTPException(413, f"{nombre} supera {MAX_MB} MB.")
            f.write(bloque)
    return destino


def crear_app(datos: Path = DATOS, pipe=pipeline) -> FastAPI:
    app = FastAPI(title="Estudio Yanero", version="1.0")
    cola = Cola(datos / "trabajos")
    modelos_dir, resultados_dir = datos / "modelos", datos / "resultados"
    for d in (modelos_dir, resultados_dir, datos / "subidas"):
        d.mkdir(parents=True, exist_ok=True)

    def leer_modelo(nombre: str) -> dict:
        f = modelos_dir / slug(nombre) / "estudio.json"
        if not f.exists():
            raise HTTPException(404, "Modelo no encontrado")
        return json.loads(f.read_text("utf-8"))

    @app.get("/", response_class=HTMLResponse)
    def inicio():
        return (Path(__file__).parent / "web" / "index.html").read_text("utf-8")

    @app.get("/estado")
    def estado():
        import importlib.util
        return {"gpu": pipe.hay_gpu(), "applio": (pipe.APPLIO / "core.py").exists(),
                "demucs": importlib.util.find_spec("demucs") is not None}

    # ------------------------------------------------ entrenar
    @app.post("/entrenar")
    async def entrenar(archivos: list[UploadFile] = File(...), nombre: str = Form(...),
                       separar_voz: bool = Form(False), consentimiento: bool = Form(False)):
        if not consentimiento:
            raise HTTPException(400, "Confirma que las grabaciones son de tu propia voz.")
        nom = slug(nombre)
        if (modelos_dir / nom / "estudio.json").exists():
            raise HTTPException(409, f"Ya existe una voz llamada «{nom}». Usa otro nombre o bórrala.")
        tmp = Path(tempfile.mkdtemp(dir=datos / "subidas"))
        rutas = [await guardar_subida(a, tmp) for a in archivos]

        def tarea(t):
            try:
                info = pipe.entrenar(t, datos, nom, rutas, separar_voz)
                pipe.empaquetar_modelo(info, modelos_dir / nom / f"{nom}_voz_entrenada.zip")
                return {"modelo": nom}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return cola.agregar("entrenar", f"Entrenar «{nom}»", tarea).a_dict()

    # ------------------------------------------------ modelos
    @app.get("/modelos")
    def modelos():
        out = []
        for f in sorted(modelos_dir.glob("*/estudio.json")):
            d = json.loads(f.read_text("utf-8"))
            out.append({k: d.get(k) for k in ("nombre", "epocas", "minutos", "registro", "creado", "importado")})
        return out

    @app.get("/modelos/{nombre}/descargar")
    def descargar(nombre: str):
        info = leer_modelo(nombre)
        z = modelos_dir / info["nombre"] / f"{info['nombre']}_voz_entrenada.zip"
        if not z.exists():
            pipe.empaquetar_modelo(info, z)
        return FileResponse(z, media_type="application/zip", filename=z.name)

    @app.post("/modelos/importar")
    async def importar(archivo: UploadFile = File(...), nombre: str = Form(...)):
        """Importa una voz entrenada fuera (por ejemplo en Colab): zip con .pth y .index."""
        nom = slug(nombre)
        destino = modelos_dir / nom
        if (destino / "estudio.json").exists():
            raise HTTPException(409, f"Ya existe «{nom}».")
        with tempfile.TemporaryDirectory() as tmp:
            sub = await guardar_subida(archivo, Path(tmp), {".zip", ".pth"})
            destino.mkdir(parents=True, exist_ok=True)
            ficha = {}
            if sub.suffix == ".pth":
                shutil.copy(sub, destino / f"{nom}.pth")
            else:
                try:
                    with zipfile.ZipFile(sub) as z:
                        for m in z.namelist():
                            ext = Path(m).suffix.lower()
                            if ext in (".pth", ".index") and not m.startswith("__MACOSX"):
                                (destino / f"{nom}{ext}").write_bytes(z.read(m))
                            elif Path(m).name == "ficha.json":
                                ficha = json.loads(z.read(m))
                except (zipfile.BadZipFile, ValueError):
                    shutil.rmtree(destino)
                    raise HTTPException(422, "El zip está dañado.")
            if not (destino / f"{nom}.pth").exists():
                shutil.rmtree(destino)
                raise HTTPException(422, "No encontré ningún archivo .pth dentro.")
            idx = destino / f"{nom}.index"
            info = {**ficha, "nombre": nom, "pth": str(destino / f"{nom}.pth"),
                    "index": str(idx) if idx.exists() else "", "importado": True}
            (destino / "estudio.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
        return info

    @app.delete("/modelos/{nombre}")
    def borrar_modelo(nombre: str):
        info = leer_modelo(nombre)
        shutil.rmtree(modelos_dir / info["nombre"])
        return {"ok": True}

    # ------------------------------------------------ cantar
    @app.post("/cantar")
    async def cantar(modelo: str = Form(...), cancion: UploadFile = File(...),
                     es_mezcla: bool = Form(False), semitonos: int | None = Form(None)):
        info = leer_modelo(modelo)
        if semitonos is not None and not -24 <= semitonos <= 24:
            raise HTTPException(400, "El ajuste de tono va de -24 a +24 semitonos.")
        tmp = Path(tempfile.mkdtemp(dir=datos / "subidas"))
        ruta = await guardar_subida(cancion, tmp)

        def tarea(t):
            try:
                return pipe.cantar(t, datos, info, ruta, es_mezcla=es_mezcla,
                                   semitonos=semitonos, salida_dir=resultados_dir)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return cola.agregar("cantar", f"«{ruta.stem}» con {info['nombre']}", tarea).a_dict()

    @app.get("/resultados/{archivo}")
    def resultado(archivo: str):
        f = resultados_dir / Path(archivo).name
        if not f.exists():
            raise HTTPException(404, "No existe")
        return FileResponse(f, media_type="audio/wav", filename=f.name)

    # ------------------------------------------------ trabajos
    @app.get("/trabajos")
    def trabajos():
        return cola.listar()

    @app.get("/trabajos/{id}/log", response_class=PlainTextResponse)
    def log(id: str):
        try:
            t = cola.obtener(id)
        except KeyError:
            raise HTTPException(404, "No existe")
        return t.ruta_log.read_text("utf-8")[-8000:] if t.ruta_log.exists() else ""

    @app.post("/trabajos/{id}/cancelar")
    def cancelar(id: str):
        try:
            cola.cancelar(id)
        except KeyError:
            raise HTTPException(404, "No existe")
        return {"ok": True}

    return app
