"""Arranque: python -m estudio [--host 127.0.0.1] [--port 7860]"""
import argparse

import uvicorn

from .app import crear_app

p = argparse.ArgumentParser(description="Estudio Yanero")
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=7860)
a = p.parse_args()
print(f"\n  Estudio Yanero listo: abre http://{a.host}:{a.port}\n")
uvicorn.run(crear_app(), host=a.host, port=a.port, log_level="warning")
