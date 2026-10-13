Vas a integrar en mi app **Ensamble** (este repositorio) las voces que crea **Estudio Yanero**, mi servicio de clonación de voz cantada: https://github.com/yanerox69/estudio-yanero. Estudio Yanero entrena y guarda las voces; Ensamble las muestra a sus usuarios y las usa para cantar canciones. Trabaja en español: mensajes de la interfaz, comentarios y commits.

## 0. Antes de escribir código

1. Explora el repositorio de Ensamble y resume en 10 líneas: framework del frontend, del backend, base de datos y ORM, autenticación (de dónde sale el id del usuario), dónde se guardan los archivos de audio, cómo se despliega, cómo se ejecutan las pruebas, y si ya existe un módulo "Voice Studio" o similar.
2. Lee el README de Estudio Yanero, sección "Modo servicio", y el archivo `estudio/app.py` de ese repo. Es el contrato de la API: no lo inventes ni lo cambies.
3. Muéstrame un plan corto de archivos a crear y modificar, y espera mi OK antes de implementar.

## 1. Contrato de la API de Estudio Yanero (ya existe)

- Base URL en `ESTUDIO_YANERO_URL`. Cada petición lleva `X-Api-Key: ESTUDIO_YANERO_API_KEY` y `X-Usuario: <id del usuario de Ensamble>`.
- `POST /entrenar` (multipart: `archivos` repetido, `nombre`, `separar_voz=true|false`, `consentimiento=true`) → objeto trabajo `{id, estado, progreso, mensaje, datos}`.
- `POST /modelos/importar` (multipart: `archivo` .zip o .pth, `nombre`) → ficha de la voz. Es para voces entrenadas en Google Colab.
- `GET /modelos` → `[{nombre, epocas, minutos, registro, creado, importado}]`
- `GET /modelos/{nombre}/descargar` → zip con .pth + .index
- `DELETE /modelos/{nombre}`
- `POST /cantar` (multipart: `modelo`, `cancion`, `es_mezcla=true|false`, `semitonos` opcional de -24 a 24) → trabajo.
- `GET /trabajos/{id}` → `estado` en `en cola | corriendo | listo | error | cancelado`, `progreso` de 0 a 1, `mensaje`. Cuando está listo: `datos.resultado = {voz: "<archivo>.wav", mezcla?: "<archivo>.wav"}` (entrenar devuelve `{modelo}`).
- `GET /resultados/{archivo}` → WAV. `POST /trabajos/{id}/cancelar`. `GET /estado` → salud del servicio (sin clave).
- Errores: 400 datos inválidos, 401 clave, 404 no existe o no es de ese usuario, 409 nombre repetido, 413 archivo grande, 422 audio ilegible. El campo `detail` trae un mensaje en español apto para mostrar al usuario.
- Entrenar tarda de 1 a 4 horas y cantar varios minutos. Todo es asíncrono: se consulta el trabajo, no hay webhooks.

## 2. Reglas que no se negocian

- La clave `ESTUDIO_YANERO_API_KEY` vive **solo en el backend** de Ensamble. El navegador nunca llama a Estudio Yanero directo: todo pasa por rutas propias de Ensamble que agregan los encabezados.
- `X-Usuario` siempre sale de la sesión autenticada del servidor, nunca de un parámetro que mande el cliente.
- Antes de entrenar o importar, el usuario marca obligatoriamente: "Confirmo que esta voz es mía o que tengo permiso escrito de su dueño". Guarda en la base de datos quién lo aceptó y cuándo.
- Todo audio generado con una voz clonada se etiqueta como "Voz generada con IA", en la interfaz y en el nombre del archivo descargado.
- No reproduzcas letras de canciones ni contenido protegido en la interfaz, en los textos de ejemplo ni en los datos de prueba.
- Si Estudio Yanero no responde, Ensamble sigue funcionando: el módulo muestra "El estudio de voz no está disponible ahora" y nada más se rompe.

## 3. Qué construir en Ensamble

**Base de datos** (migración con el ORM que ya use el proyecto):
- `voces`: id, usuario_id, nombre (el `nombre` de Estudio Yanero), origen (`entrenada` | `importada` | `destacada`), minutos, registro, consentimiento_aceptado_en, creada_en, borrada_en.
- `trabajos_voz`: id, usuario_id, tipo (`entrenar` | `cantar`), id_remoto, estado, progreso, mensaje, voz_id, archivo_entrada, archivos_resultado (json), creado_en, actualizado_en.

**Backend** (rutas propias bajo `/api/voces`, con autenticación):
- Cliente de Estudio Yanero en un solo módulo: timeouts de 30 s en llamadas normales y de 10 min en subidas, 2 reintentos solo en errores de red o 5xx, y traducción de errores a mensajes para el usuario.
- `GET /api/voces`: las voces del usuario más las voces destacadas (abajo).
- `POST /api/voces/entrenar`, `POST /api/voces/importar`, `DELETE /api/voces/:id`, `GET /api/voces/:id/descargar`.
- `POST /api/voces/cantar`: recibe una canción del usuario o el id de una pista que ya existe en Ensamble (reutiliza la separación de voz de Ensamble si ya la tiene, y en ese caso manda solo la voz con `es_mezcla=false` y mezcla en Ensamble).
- `GET /api/voces/trabajos/:id`: lee el estado local. Un proceso en segundo plano (el sistema de colas o cron que ya tenga el proyecto; si no hay, uno simple) consulta cada 10 s los trabajos activos en Estudio Yanero, actualiza la tabla y, al quedar listo, descarga los WAV resultantes al almacenamiento de audio de Ensamble para no depender del estudio después.
- Límites por usuario configurables por variables de entorno: entrenamientos por mes (por defecto 2), canciones cantadas por día (por defecto 10), tamaño máximo de subida (por defecto 500 MB). Responde 429 con un mensaje claro al pasarlos.

**Voces destacadas (mi voz oficial):**
- Variable `VOCES_DESTACADAS` con una lista de pares `usuario_estudio:nombre_voz` (ejemplo: `yanero-oficial:mi-voz`). Esas voces aparecen para todos los usuarios con la etiqueta "Voz oficial" y solo sirven para cantar: no se descargan ni se borran.
- Al cantar con una voz destacada, el backend llama a Estudio Yanero con `X-Usuario` del dueño de la voz, pero guarda el trabajo y el resultado a nombre del usuario que lo pidió.
- Límite aparte para voces destacadas (por defecto 3 por día y usuario), porque es mi voz y no quiero que la usen sin control.

**Frontend** (dentro del módulo Voice Studio si existe; si no, una sección nueva "Mis voces"), con el mismo estilo visual que ya tiene Ensamble:
1. **Mis voces:** lista con nombre, minutos, registro y origen, más los botones Cantar, Descargar y Borrar (Borrar pide confirmación).
2. **Crear voz**, con dos pestañas:
   - "Entrenar aquí": subida múltiple de audios y videos, casilla "mis grabaciones tienen música", casilla de consentimiento obligatoria, y aviso de que tarda de 1 a 4 horas y que hacen falta de 10 a 30 minutos de voz.
   - "Entrené en Colab": enlace al cuaderno https://colab.research.google.com/github/yanerox69/estudio-yanero/blob/main/Entrenar_en_Colab.ipynb, instrucciones en 3 pasos y subida del zip.
3. **Cantar con una voz:** elegir la voz, subir una canción o elegir una pista de Ensamble, interruptor "trae instrumental", tono (Automático, −12, 0, +12), barra de progreso que consulta cada 5 s, y reproductor con descarga de la mezcla y de la voz sola.
4. **Estados vacíos y errores:** sin voces, servicio caído, límite alcanzado, entrenamiento fallido (muestra el `mensaje` que devuelve el estudio).

## 4. Configuración y despliegue

- Agrega a `.env.example`: `ESTUDIO_YANERO_URL`, `ESTUDIO_YANERO_API_KEY`, `VOCES_DESTACADAS` y los límites, cada una con un comentario de una línea.
- Escribe `docs/estudio-yanero.md` con:
  - cómo levantar Estudio Yanero en modo servicio (`ESTUDIO_API_KEY=... python -m estudio --host 0.0.0.0`);
  - opciones de servidor con GPU (un PC propio con NVIDIA expuesto con Cloudflare Tunnel, o un servidor GPU por horas), con lo que cuesta cada una;
  - que el servicio procesa un trabajo a la vez, así que con muchos usuarios simultáneos los trabajos esperan en cola.

## 5. Pruebas y entrega

- Pruebas del backend con el cliente de Estudio Yanero **simulado** (sin red): flujo entrenar → listo, cantar → resultado descargado, aislamiento entre usuarios, voz destacada con dueño distinto, límites 429, servicio caído y clave nunca expuesta en respuestas al navegador.
- Corre todas las pruebas del proyecto (las nuevas y las que ya existían) y que pasen.
- Commits pequeños y descriptivos. Al final dame:
  - lista de archivos cambiados;
  - variables de entorno a configurar;
  - pasos exactos para probarlo de punta a punta con un Estudio Yanero real;
  - lo que quedó pendiente o dudoso.

Si algo de este pedido choca con cómo está hecho Ensamble, para y pregúntame en vez de suponer.
