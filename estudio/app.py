"""Estudio Yanero: subes tus grabaciones, entrena tu voz, la descargas y canta con ella.

Modo local (por defecto): sin clave, un solo usuario ("local"), con la página web incluida.
Modo servicio (para Ensamble): define ESTUDIO_API_KEY; cada petición trae
  X-Api-Key: <clave>      (obligatoria)
  X-Usuario: <id>         (el id del usuario en Ensamble; cada uno ve solo sus voces)
y opcionalmente ESTUDIO_CORS="https://tu-dominio,https://otro" para llamadas desde el navegador.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from . import pipeline
from .trabajos import Cola

DATOS = Path(os.environ.get("ESTUDIO_DATOS", Path.home() / "estudio-yanero-datos"))
MAX_MB = int(os.environ.get("ESTUDIO_MAX_MB", "2000"))
EXT_OK = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
LIBRES = {"/", "/estado"}          # rutas sin clave en modo servicio


def slug(nombre: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", nombre.strip().lower()).strip("-")[:40]
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        raise HTTPException(400, "Ponle un nombre a la voz (letras o números).")
    return s


def usuario_de(x_usuario: str | None) -> str:
    if not x_usuario:
        return "local"
    u = re.sub(r"[^a-z0-9_-]+", "-", x_usuario.strip().lower()).strip("-")[:64]
    u = re.sub(r"-{2,}", "-", u)
    if not u:
        raise HTTPException(400, "Encabezado X-Usuario inválido.")
    return u


def interno(usuario: str, nom: str) -> str:
    """Nombre en disco y en Applio. Un '--' nunca sale de slug(), así que no hay choques."""
    return nom if usuario == "local" else f"{usuario}--{nom}"


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


def crear_app(datos: Path = DATOS, pipe=pipeline, api_key: str | None = None,
              cors: str | None = None) -> FastAPI:
    api_key = api_key if api_key is not None else os.environ.get("ESTUDIO_API_KEY", "")
    cors = cors if cors is not None else os.environ.get("ESTUDIO_CORS", "")

    app = FastAPI(title="Estudio Yanero", version="1.1")
    cola = Cola(datos / "trabajos")
    modelos_dir, resultados_dir = datos / "modelos", datos / "resultados"
    for d in (modelos_dir, resultados_dir, datos / "subidas"):
        d.mkdir(parents=True, exist_ok=True)

    if cors:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in cors.split(",") if o.strip()],
                           allow_methods=["*"], allow_headers=["X-Api-Key", "X-Usuario", "Content-Type"])

    if api_key:
        @app.middleware("http")
        async def exigir_clave(request: Request, call_next):
            if request.method != "OPTIONS" and request.url.path not in LIBRES:
                dada = request.headers.get("x-api-key", "")
                if not hmac.compare_digest(dada.encode(), api_key.encode()):
                    return JSONResponse({"detail": "Clave de acceso inválida."}, status_code=401)
            return await call_next(request)

    def leer_modelo(nombre: str, usuario: str) -> dict:
        f = modelos_dir / interno(usuario, slug(nombre)) / "estudio.json"
        if not f.exists():
            raise HTTPException(404, "Modelo no encontrado")
        info = json.loads(f.read_text("utf-8"))
        if info.get("usuario", "local") != usuario:
            raise HTTPException(404, "Modelo no encontrado")
        return info

    def guardar_info(info: dict) -> None:
        (modelos_dir / info["nombre"] / "estudio.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), "utf-8")

    def zip_de(info: dict) -> Path:
        z = modelos_dir / info["nombre"] / f"{info.get('publico', info['nombre'])}_voz_entrenada.zip"
        if not z.exists():
            pipe.empaquetar_modelo({**info, "nombre": info.get("publico", info["nombre"])}, z)
        return z

    def ficha_publica(d: dict) -> dict:
        return {"nombre": d.get("publico", d["nombre"]),
                **{k: d.get(k) for k in ("epocas", "minutos", "registro", "creado", "importado")}}

    def trabajo_de(id: str, usuario: str):
        try:
            t = cola.obtener(id)
        except KeyError:
            raise HTTPException(404, "No existe")
        if t.datos.get("usuario", "local") != usuario:
            raise HTTPException(404, "No existe")
        return t

    @app.get("/", response_class=HTMLResponse)
    def inicio():
        return (Path(__file__).parent / "web" / "index.html").read_text("utf-8")

    @app.get("/estado")
    def estado():
        import importlib.util
        return {"gpu": pipe.hay_gpu(), "applio": (pipe.APPLIO / "core.py").exists(),
                "demucs": importlib.util.find_spec("demucs") is not None,
                "modo": "servicio" if api_key else "local"}

    # ------------------------------------------------ entrenar
    @app.post("/entrenar")
    async def entrenar(archivos: list[UploadFile] = File(...), nombre: str = Form(...),
                       separar_voz: bool = Form(False), consentimiento: bool = Form(False),
                       x_usuario: str | None = Header(None)):
        u = usuario_de(x_usuario)
        if not consentimiento:
            raise HTTPException(400, "Confirma que las grabaciones son de tu propia voz.")
        nom = slug(nombre)
        clave = interno(u, nom)
        if (modelos_dir / clave / "estudio.json").exists():
            raise HTTPException(409, f"Ya existe una voz llamada «{nom}». Usa otro nombre o bórrala.")
        tmp = Path(tempfile.mkdtemp(dir=datos / "subidas"))
        rutas = [await guardar_subida(a, tmp) for a in archivos]

        def tarea(t):
            try:
                info = pipe.entrenar(t, datos, clave, rutas, separar_voz)
                info.update(usuario=u, publico=nom)
                guardar_info(info)
                zip_de(info)
                return {"modelo": nom}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return cola.agregar("entrenar", f"Entrenar «{nom}»", tarea, {"usuario": u}).a_dict()

    # ------------------------------------------------ modelos
    @app.get("/modelos")
    def modelos(x_usuario: str | None = Header(None)):
        u = usuario_de(x_usuario)
        out = []
        for f in sorted(modelos_dir.glob("*/estudio.json")):
            d = json.loads(f.read_text("utf-8"))
            if d.get("usuario", "local") == u:
                out.append(ficha_publica(d))
        return out

    @app.get("/modelos/{nombre}/descargar")
    def descargar(nombre: str, x_usuario: str | None = Header(None)):
        z = zip_de(leer_modelo(nombre, usuario_de(x_usuario)))
        return FileResponse(z, media_type="application/zip", filename=z.name)

    @app.post("/modelos/importar")
    async def importar(archivo: UploadFile = File(...), nombre: str = Form(...),
                       x_usuario: str | None = Header(None)):
        """Importa una voz entrenada fuera (por ejemplo en Colab): zip con .pth y .index."""
        u = usuario_de(x_usuario)
        nom = slug(nombre)
        clave = interno(u, nom)
        destino = modelos_dir / clave
        if (destino / "estudio.json").exists():
            raise HTTPException(409, f"Ya existe «{nom}».")
        with tempfile.TemporaryDirectory() as tmp:
            sub = await guardar_subida(archivo, Path(tmp), {".zip", ".pth"})
            destino.mkdir(parents=True, exist_ok=True)
            ficha = {}
            if sub.suffix == ".pth":
                shutil.copy(sub, destino / f"{clave}.pth")
            else:
                try:
                    with zipfile.ZipFile(sub) as z:
                        for m in z.namelist():
                            ext = Path(m).suffix.lower()
                            if ext in (".pth", ".index") and not m.startswith("__MACOSX"):
                                (destino / f"{clave}{ext}").write_bytes(z.read(m))
                            elif Path(m).name == "ficha.json":
                                ficha = json.loads(z.read(m))
                except (zipfile.BadZipFile, ValueError):
                    shutil.rmtree(destino)
                    raise HTTPException(422, "El zip está dañado.")
            if not (destino / f"{clave}.pth").exists():
                shutil.rmtree(destino)
                raise HTTPException(422, "No encontré ningún archivo .pth dentro.")
            idx = destino / f"{clave}.index"
            ficha = {k: v for k, v in ficha.items() if k in ("epocas", "minutos", "registro", "tono_mediano", "creado")}
            info = {**ficha, "nombre": clave, "publico": nom, "usuario": u,
                    "pth": str(destino / f"{clave}.pth"),
                    "index": str(idx) if idx.exists() else "", "importado": True}
            guardar_info(info)
        return ficha_publica(info)

    @app.delete("/modelos/{nombre}")
    def borrar_modelo(nombre: str, x_usuario: str | None = Header(None)):
        info = leer_modelo(nombre, usuario_de(x_usuario))
        shutil.rmtree(modelos_dir / info["nombre"])
        return {"ok": True}

    # ------------------------------------------------ cantar
    @app.post("/cantar")
    async def cantar(modelo: str = Form(...), cancion: UploadFile = File(...),
                     es_mezcla: bool = Form(False), semitonos: int | None = Form(None),
                     x_usuario: str | None = Header(None)):
        u = usuario_de(x_usuario)
        info = leer_modelo(modelo, u)
        if semitonos is not None and not -24 <= semitonos <= 24:
            raise HTTPException(400, "El ajuste de tono va de -24 a +24 semitonos.")
        tmp = Path(tempfile.mkdtemp(dir=datos / "subidas"))
        ruta = await guardar_subida(cancion, tmp)
        salida = resultados_dir / u

        def tarea(t):
            try:
                return pipe.cantar(t, datos, info, ruta, es_mezcla=es_mezcla,
                                   semitonos=semitonos, salida_dir=salida)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        titulo = f"«{ruta.stem}» con {info.get('publico', info['nombre'])}"
        return cola.agregar("cantar", titulo, tarea, {"usuario": u}).a_dict()

    @app.get("/resultados/{archivo}")
    def resultado(archivo: str, x_usuario: str | None = Header(None)):
        f = resultados_dir / usuario_de(x_usuario) / Path(archivo).name
        if not f.is_file():
            raise HTTPException(404, "No existe")
        return FileResponse(f, media_type="audio/wav", filename=f.name)

    # ------------------------------------------------ trabajos
    @app.get("/trabajos")
    def trabajos(x_usuario: str | None = Header(None)):
        u = usuario_de(x_usuario)
        return [t for t in cola.listar() if t["datos"].get("usuario", "local") == u]

    @app.get("/trabajos/{id}")
    def trabajo(id: str, x_usuario: str | None = Header(None)):
        return trabajo_de(id, usuario_de(x_usuario)).a_dict()

    @app.get("/trabajos/{id}/log", response_class=PlainTextResponse)
    def log(id: str, x_usuario: str | None = Header(None)):
        t = trabajo_de(id, usuario_de(x_usuario))
        return t.ruta_log.read_text("utf-8")[-8000:] if t.ruta_log.exists() else ""

    @app.post("/trabajos/{id}/cancelar")
    def cancelar(id: str, x_usuario: str | None = Header(None)):
        t = trabajo_de(id, usuario_de(x_usuario))
        cola.cancelar(t.id)
        return {"ok": True}

    return app
