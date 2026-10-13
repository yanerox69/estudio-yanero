"""Flujo completo con un pipeline simulado: entrenar -> descargar -> importar -> cantar."""

import io
import json
import time
import types
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from estudio import pipeline as real
from estudio.app import crear_app


def falso_pipe(tmp: Path):
    p = types.SimpleNamespace()
    p.APPLIO = tmp / "Applio"
    p.hay_gpu = lambda: True
    p.empaquetar_modelo = real.empaquetar_modelo

    def entrenar(t, datos, nombre, archivos, separar_voz):
        assert archivos and all(a.exists() for a in archivos)
        t.paso("Entrenando tu voz: época 100 de 200", 0.5)
        t.dato("dataset", {"minutos": 14.2, "clips": 120, "registro": ["Do3", "Si4"]})
        d = datos / "modelos" / nombre
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{nombre}_200e_4000s.pth").write_bytes(b"PESOS")
        (d / f"{nombre}.index").write_bytes(b"INDICE")
        info = {"nombre": nombre, "pth": str(d / f"{nombre}_200e_4000s.pth"),
                "index": str(d / f"{nombre}.index"), "epocas": 200, "minutos": 14.2,
                "registro": ["Do3", "Si4"], "tono_mediano": 220.0}
        (d / "estudio.json").write_text(json.dumps(info))
        return info

    def cantar(t, datos, modelo, cancion, *, es_mezcla, semitonos, salida_dir):
        assert Path(modelo["pth"]).exists()
        salida_dir.mkdir(parents=True, exist_ok=True)
        sf.write(salida_dir / "tema_voz.wav", np.zeros(4410, np.float32), 44100)
        out = {"voz": "tema_voz.wav"}
        if es_mezcla:
            sf.write(salida_dir / "tema_mezcla.wav", np.zeros(4410, np.float32), 44100)
            out["mezcla"] = "tema_mezcla.wav"
        t.dato("semitonos", semitonos)
        return out

    p.entrenar, p.cantar = entrenar, cantar
    return p


def esperar(c, id, seg=10):
    fin = time.time() + seg
    while time.time() < fin:
        t = next(x for x in c.get("/trabajos").json() if x["id"] == id)
        if t["estado"] in ("listo", "error", "cancelado"):
            return t
        time.sleep(0.05)
    raise AssertionError("el trabajo no terminó")


@pytest.fixture
def c(tmp_path):
    return TestClient(crear_app(datos=tmp_path / "datos", pipe=falso_pipe(tmp_path)))


def wav_bytes():
    b = io.BytesIO()
    sf.write(b, np.zeros(44100, np.float32), 44100, format="WAV")
    return b.getvalue()


def test_entrenar_descargar_cantar(c):
    r = c.post("/entrenar", files=[("archivos", ("ensayo 1.wav", wav_bytes())),
                                   ("archivos", ("concierto.mp4", b"x" * 2000))],
               data={"nombre": "Mi Voz Tenor", "separar_voz": "true", "consentimiento": "true"})
    assert r.status_code == 200, r.text
    t = esperar(c, r.json()["id"])
    assert t["estado"] == "listo", t
    assert t["datos"]["resultado"] == {"modelo": "mi-voz-tenor"}

    assert [m["nombre"] for m in c.get("/modelos").json()] == ["mi-voz-tenor"]
    z = c.get("/modelos/mi-voz-tenor/descargar")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    nombres = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert {"mi-voz-tenor.pth", "mi-voz-tenor.index", "ficha.json", "LEEME.txt"} <= set(nombres)
    ficha = json.loads(zipfile.ZipFile(io.BytesIO(z.content)).read("ficha.json"))
    assert "pth" not in ficha and ficha["minutos"] == 14.2

    # nombre repetido
    r = c.post("/entrenar", files=[("archivos", ("a.wav", wav_bytes()))],
               data={"nombre": "mi voz tenor", "consentimiento": "true"})
    assert r.status_code == 409

    # cantar: con mezcla y sin ella (checkbox ausente = sin instrumental)
    r = c.post("/cantar", files={"cancion": ("tema.mp3", b"x" * 3000)},
               data={"modelo": "mi-voz-tenor", "es_mezcla": "true"})
    t = esperar(c, r.json()["id"])
    assert t["estado"] == "listo" and set(t["datos"]["resultado"]) == {"voz", "mezcla"}
    assert t["datos"]["semitonos"] is None
    assert c.get("/resultados/tema_mezcla.wav").status_code == 200

    r = c.post("/cantar", files={"cancion": ("guia.wav", wav_bytes())},
               data={"modelo": "mi-voz-tenor", "semitonos": "-12"})
    t = esperar(c, r.json()["id"])
    assert set(t["datos"]["resultado"]) == {"voz"} and t["datos"]["semitonos"] == -12

    # importar el zip descargado con otro nombre
    r = c.post("/modelos/importar", files={"archivo": ("v.zip", z.content)}, data={"nombre": "copia"})
    assert r.status_code == 200 and r.json()["index"]
    assert len(c.get("/modelos").json()) == 2
    assert c.delete("/modelos/copia").status_code == 200


def test_validaciones(c):
    assert c.post("/entrenar", files=[("archivos", ("a.wav", wav_bytes()))],
                  data={"nombre": "x"}).status_code == 400            # sin consentimiento
    assert c.post("/entrenar", files=[("archivos", ("a.exe", b"zz"))],
                  data={"nombre": "x", "consentimiento": "true"}).status_code == 400
    assert c.post("/cantar", files={"cancion": ("a.wav", wav_bytes())},
                  data={"modelo": "nada"}).status_code == 404
    assert c.post("/modelos/importar", files={"archivo": ("v.zip", b"no zip")},
                  data={"nombre": "x"}).status_code == 422
    assert c.get("/resultados/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert c.get("/modelos/..%2F..%2Fx/descargar").status_code == 404


def test_paquete_real(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "v_100e.pth").write_bytes(b"p")
    info = {"nombre": "v", "pth": str(d / "v_100e.pth"), "index": "", "minutos": 9}
    z = real.empaquetar_modelo(info, tmp_path / "out.zip")
    assert set(zipfile.ZipFile(z).namelist()) == {"v.pth", "ficha.json", "LEEME.txt"}


def test_tono_dataset(tmp_path):
    sr = 40000
    t = np.arange(sr * 4) / sr
    for i in range(5):
        sf.write(tmp_path / f"c{i}.wav", (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)
    f = real.tono_dataset(tmp_path)
    assert 210 < f < 230
