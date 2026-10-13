# Estudio Yanero

[![Abrir en Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/yanerox69/estudio-yanero/blob/main/Entrenar_en_Colab.ipynb)

Subes tus grabaciones cantando y el sistema hace todo lo demás:

1. **Prepara el dataset.** Saca el audio de videos, separa la voz de la música, corta en clips de 3–10 s, descarta lo saturado y te dice cuántos minutos útiles tienes y qué registro cubren.
2. **Entrena tu voz** con RVC v2 (motor Applio). Ajusta el lote a tu GPU, calcula las épocas según tus minutos y genera el índice.
3. **Te deja descargar la voz entrenada**: un zip con `.pth` + `.index` estándar, que sirve en Applio y en cualquier programa RVC.
4. **Canta con tu voz.** Subes una canción ya cantada (o una guía tuya), el sistema separa voz e instrumental, cambia la voz por la tuya y vuelve a mezclar.

## Lo que necesitas saber antes

- **RVC no inventa melodías ni letra.** Convierte una voz cantada existente en la tuya. Para un tema nuevo necesitas una guía cantada: puedes grabarla tú rápido y sin cuidar la técnica, porque el modelo pone el timbre.
- **Material:** 10–30 min de tu voz limpia. Menos de 5 min no entrena (el sistema se niega). Incluye graves y agudos, porque el modelo solo canta bien dentro del registro que escuchó.
- **Calidad:** una grabación seca de estudio vale más que una hora de conciertos con reverb y público.
- **GPU NVIDIA** (al menos 6 GB) para entrenar. El entrenamiento tarda de 1 a 4 h. Sin GPU, usa Colab (abajo). Cantar sí puede ir en CPU, aunque tarda varios minutos por canción.

## Opción A: en tu PC (con GPU NVIDIA)

Requisitos: Python 3.12, git y ffmpeg.

```bash
./instalar.sh        # Windows: instalar.bat   (descarga unos 6–8 GB)
./iniciar.sh         # Windows: iniciar.bat
```

Abre `http://127.0.0.1:7860`, sube tus grabaciones y pulsa **Entrenar**. Puedes cerrar la página: el avance sigue y se ve en *Trabajos*. Al terminar aparece **Descargar voz entrenada**.

## Opción B: sin GPU, en Google Colab (gratis)

1. En tu Google Drive crea `EstudioYanero/grabaciones` y sube ahí tus grabaciones.
2. Abre el cuaderno con el botón **Abrir en Colab** de arriba (https://colab.research.google.com/github/yanerox69/estudio-yanero/blob/main/Entrenar_en_Colab.ipynb).
3. Elige GPU T4 y pulsa *Ejecutar todas*. El cuaderno descarga el código solo; no subes ningún zip.
4. La voz entrenada se descarga sola y además queda en `EstudioYanero/voces/` de tu Drive.

Colab gratis se desconecta tras unas horas. Si pasa, vuelve a ejecutar todo y **reanuda desde el último checkpoint**.
Después, en el estudio de tu PC: *Mis voces entrenadas → Importar*.

Sin interfaz (servidor o Colab):

```bash
python -m estudio.entrenar_cli MIS_GRABACIONES/ --nombre mi-voz-tenor --separar
```

## Cantar

- **Con instrumental** (marcado por defecto): separa la voz, la convierte y la vuelve a mezclar. El ajuste automático de tono solo mueve octavas (−12, 0 o +12) para no desafinar con la pista.
- **Guía a capela** (desmarcado): convierte solo la voz y el ajuste automático puede mover cualquier número de semitonos.
- Si la guía la canta una mujer o un barítono, el ajuste automático lleva la melodía a tu zona de tenor.

Recibes dos archivos: la mezcla final y la voz sola, para que la mezcles tú en tu DAW.

## Variables

| Variable | Por defecto | Para qué |
|---|---|---|
| `ESTUDIO_DATOS` | `~/estudio-yanero-datos` | Dónde se guardan modelos, trabajos y resultados |
| `ESTUDIO_APPLIO` | `./Applio` | Ruta de Applio |
| `ESTUDIO_MAX_MB` | `2000` | Tamaño máximo por archivo subido |

## Pruebas

```bash
pip install pytest httpx
python -m pytest tests
```

## Licencias y uso

- Este proyecto: MIT. Applio (motor de entrenamiento): MIT, © AI Hispano. Demucs: MIT, © Meta.
- Entrena solo con tu propia voz. Para publicar versiones de canciones ajenas necesitas los derechos de la obra (licencia mecánica), aunque la voz sea tuya. Si lo publicas, indica que la voz es generada con IA donde lo exija la plataforma.

## Modo servicio (para conectar con Ensamble u otra app)

```bash
ESTUDIO_API_KEY="una-clave-larga-y-secreta" ESTUDIO_CORS="https://tu-dominio.com" python -m estudio --host 0.0.0.0
```

Cada petición lleva `X-Api-Key: <clave>` y `X-Usuario: <id del usuario en tu app>`. Cada usuario ve, descarga y usa solo sus propias voces. Sin `ESTUDIO_API_KEY` funciona en modo local, de un solo usuario y con la página web incluida.

| Método y ruta | Para qué |
|---|---|
| `POST /entrenar` (multipart: `archivos[]`, `nombre`, `separar_voz`, `consentimiento=true`) | Entrena una voz → devuelve un trabajo |
| `POST /modelos/importar` (multipart: `archivo` .zip o .pth, `nombre`) | Importa una voz entrenada en Colab |
| `GET /modelos` · `GET /modelos/{nombre}/descargar` · `DELETE /modelos/{nombre}` | Voces del usuario |
| `POST /cantar` (multipart: `modelo`, `cancion`, `es_mezcla`, `semitonos` opcional) | Canta con la voz → devuelve un trabajo |
| `GET /trabajos/{id}` | Estado: `en cola`, `corriendo`, `listo`, `error`, `cancelado`; `progreso` 0–1; `datos.resultado` |
| `GET /resultados/{archivo}` | Descarga el WAV resultante (`datos.resultado.mezcla` / `datos.resultado.voz`) |
| `GET /estado` | Salud: GPU, Applio, separación (sin clave) |
