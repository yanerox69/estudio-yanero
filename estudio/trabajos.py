"""Cola de trabajos en segundo plano: uno a la vez (una sola GPU)."""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path


class Trabajo:
    def __init__(self, carpeta: Path, tipo: str, titulo: str, id: str | None = None):
        self.id = id or uuid.uuid4().hex[:10]
        self.carpeta = carpeta
        self.tipo, self.titulo = tipo, titulo
        self.estado = "en cola"          # en cola | corriendo | listo | error | cancelado
        self.mensaje, self.progreso = "Esperando turno", 0.0
        self.datos: dict = {}
        self.creado, self.actualizado = time.time(), time.time()
        self.cancelado = False
        self._lock = threading.Lock()
        carpeta.mkdir(parents=True, exist_ok=True)
        self.guardar()

    @property
    def ruta_log(self) -> Path:
        return self.carpeta / f"{self.id}.log"

    def log(self, texto: str) -> None:
        with open(self.ruta_log, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {texto}\n")

    def paso(self, mensaje: str, progreso: float) -> None:
        with self._lock:
            if mensaje != self.mensaje:
                self.log(mensaje)
            self.mensaje, self.progreso = mensaje, round(min(1.0, max(0.0, progreso)), 3)
            self.guardar()

    def dato(self, clave: str, valor) -> None:
        with self._lock:
            self.datos[clave] = valor
            self.guardar()

    def a_dict(self) -> dict:
        return {"id": self.id, "tipo": self.tipo, "titulo": self.titulo, "estado": self.estado,
                "mensaje": self.mensaje, "progreso": self.progreso, "datos": self.datos,
                "creado": self.creado, "actualizado": self.actualizado}

    def guardar(self) -> None:
        self.actualizado = time.time()
        tmp = self.carpeta / f"{self.id}.json.tmp"
        tmp.write_text(json.dumps(self.a_dict(), ensure_ascii=False), "utf-8")
        tmp.replace(self.carpeta / f"{self.id}.json")


class Cola:
    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.trabajos: dict[str, Trabajo] = {}
        self._q: queue.Queue = queue.Queue()
        self._recuperar()
        threading.Thread(target=self._bucle, daemon=True).start()

    def _recuperar(self) -> None:
        """Al reiniciar, los trabajos que quedaron a medias se marcan como interrumpidos."""
        self.carpeta.mkdir(parents=True, exist_ok=True)
        for f in self.carpeta.glob("*.json"):
            try:
                d = json.loads(f.read_text("utf-8"))
            except ValueError:
                continue
            t = Trabajo.__new__(Trabajo)
            t.__dict__.update(id=d["id"], carpeta=self.carpeta, tipo=d["tipo"], titulo=d["titulo"],
                              estado=d["estado"], mensaje=d["mensaje"], progreso=d["progreso"],
                              datos=d.get("datos", {}), creado=d["creado"], actualizado=d["actualizado"],
                              cancelado=False, _lock=threading.Lock())
            if t.estado in ("en cola", "corriendo"):
                t.estado, t.mensaje = "error", "Interrumpido: el programa se cerró. Vuelve a lanzarlo."
                t.guardar()
            self.trabajos[t.id] = t

    def agregar(self, tipo: str, titulo: str, funcion) -> Trabajo:
        t = Trabajo(self.carpeta, tipo, titulo)
        self.trabajos[t.id] = t
        self._q.put((t, funcion))
        return t

    def listar(self) -> list[dict]:
        return [t.a_dict() for t in sorted(self.trabajos.values(), key=lambda t: t.creado, reverse=True)]

    def obtener(self, id: str) -> Trabajo:
        return self.trabajos[id]

    def cancelar(self, id: str) -> None:
        t = self.trabajos[id]
        t.cancelado = True
        if t.estado == "en cola":
            t.estado, t.mensaje = "cancelado", "Cancelado"
            t.guardar()

    def _bucle(self) -> None:
        while True:
            t, funcion = self._q.get()
            if t.cancelado:
                continue
            t.estado = "corriendo"
            t.guardar()
            try:
                resultado = funcion(t)
                t.datos["resultado"] = resultado
                t.estado, t.progreso = "listo", 1.0
            except Exception as e:  # noqa: BLE001
                t.log(traceback.format_exc())
                t.estado = "cancelado" if t.cancelado else "error"
                t.mensaje = str(e) or e.__class__.__name__
            t.guardar()
