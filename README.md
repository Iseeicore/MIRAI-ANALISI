# MIRAI-ANALISI

Pipeline local para analizar mamografías DICOM con el modelo de riesgo **MIRAI** (MIT JCLINIC), con un backend propio de ingesta/orquestación y un scraper de Playwright para exportar estudios desde el visor MedDream.

Todo corre en contenedores Docker: ni las imágenes DICOM ni los resultados salen de la máquina.

## Arquitectura

| Servicio | Descripción | Puerto host → contenedor |
|---|---|---|
| `mirai` | Imagen oficial `mitjclinic/mirai`, sirve el modelo de riesgo vía Flask | `8885` → `5000`, `8886` → `8000` |
| `dicom-analyzer` | Panel Streamlit para explorar/anonimizar estudios y ver resultados | `8890` → `8501` |
| `dicom-analyzer-api` | API FastAPI que ingiere lotes DICOM y los reenvía a `mirai` | `8889` → `8000` |

Definidos en `docker-compose.yml`, en la raíz del repo.

Los puertos `8885`/`8886` son los que indica la guía oficial para MIRAI; `8887`/`8888` quedan reservados para Sybil, por eso los servicios propios usan `8889` y `8890`.

## Requisitos

- Docker Desktop instalado (en Windows, con WSL2 habilitado)
- 15 GB de almacenamiento libre
- 8 GB de RAM
- Procesador moderno (i5/i7 o equivalente)

(Especificaciones según la guía oficial *Instalación de MIRAI/Sybil* de MIT JCLINIC.)

## 1. Descargar la imagen de MIRAI

**Opción A — Docker Desktop (interfaz gráfica):**
1. Abrir Docker Desktop.
2. Ir a la pestaña "Docker Hub" y buscar `mitjclinic/mirai`.
3. Abrir la tarjeta `mitjclinic/mirai` y hacer clic en "Pull".

**Opción B — línea de comandos:**

```bash
docker pull mitjclinic/mirai:latest
```

La imagen pesa ~3.7 GB. Una vez descargada, no necesita conexión a internet para correr la inferencia.

## 2. Levantar el backend completo con docker-compose

```bash
# Clonar el repo
git clone https://github.com/Iseeicore/MIRAI-ANALISI.git
cd MIRAI-ANALISI

# Copiar y completar las variables de entorno del backend
cp .env.example .env
# editar .env: apuntar DICOM_HOST_DIR a la carpeta local con tus estudios DICOM (default ./data)

# Levantar mirai + backend (build de las imágenes propias + pull de mirai)
docker compose up -d --build
```

Servicios disponibles:

- MIRAI: http://localhost:8885
- Panel Streamlit (`dicom-analyzer`): http://localhost:8890
- API FastAPI (`dicom-analyzer-api`): http://localhost:8889

Otros comandos útiles:

```bash
docker compose logs -f mirai        # ver logs de un servicio
docker compose ps                   # estado de los contenedores
docker compose down                 # apagar todo
```

Si un contenedor quedó detenido, no hace falta recrearlo: `docker compose start <servicio>` lo reanuda.

## 3. Variables de entorno

### Backend (`.env` en la raíz — ver `.env.example`)

| Variable | Uso | Default |
|---|---|---|
| `DICOM_HOST_DIR` | Carpeta del host (ruta Windows o Linux) con los DICOM; se monta de solo lectura en `/dicom` dentro del panel | `./data` |
| `INGEST_BLOCK_SIZE` | Tamaño de lote que procesa `dicom-analyzer-api` por request | `50` |

### Scraper de Playwright (MedDream)

El scraper vive en `MIRAI-ANALITIC/scripts/meddream_scraper/` y usa Playwright para loguearse en el visor MedDream y exportar el primer estudio de una lista. **Requiere estar conectado a la VPN del hospital.**

Variables de entorno (archivo `.env` local dentro de esa carpeta — ver `.env.example`, **nunca se commitea**):

| Variable | Uso |
|---|---|
| `MEDDREAM_URL` | URL del visor MedDream dentro de la VPN |
| `MEDDREAM_USER` | Usuario de MedDream |
| `MEDDREAM_PASSWORD` | Contraseña de MedDream |

```bash
cd MIRAI-ANALITIC/scripts/meddream_scraper
npm install
npx playwright install chromium
cp .env.example .env    # completar con las credenciales reales (no compartir, no commitear)
node scrape_first_page.js
```

> [!WARNING]
> Las credenciales de MedDream son internas del hospital y no se comparten ni se suben a git. El `.gitignore` ya excluye `.env` en todo el repo; igual conviene correr `git status` antes de cada commit para confirmar que no quedó ninguno agregado por error.

## Batch analysis by URL

Procesa árboles grandes de estudios (`PACIENTE_FECHA/series-NNNNN/image-NNNNN.dcm`) en segundo plano: `worker-crawl` registra cada carpeta de paciente, `worker-fetch` descarga, seudonimiza y arma las 4 vistas, y `worker-score` las envía a MIRAI de a una. El estado vive en MySQL (`mysql`, publicado solo en `127.0.0.1:3307`).

Variables requeridas (en el entorno o en `.env`; sin ellas `docker compose` no arranca):

| Variable | Uso | Default |
|---|---|---|
| `MYSQL_ROOT_PASSWORD` | Contraseña root de MySQL | — (obligatoria) |
| `MYSQL_PASSWORD` | Contraseña del usuario de la app | — (obligatoria) |
| `MYSQL_DATABASE` / `MYSQL_USER` | Base y usuario de la app | `mirai_ingest` / `mirai` |
| `INGEST_ALLOWED_HOSTS` | Hosts permitidos como origen URL, separados por coma (`host` = cualquier puerto, `host:puerto` = solo ese). Vacío = no se aceptan URLs | vacío |
| `INGEST_MAX_FILE_BYTES` | Tamaño máximo por archivo descargado | `104857600` (100 MB) |
| `STAGING_MAX_STUDIES` | Estudios en staging a la vez (contrapresión) | `20` |

El origen puede ser un índice HTTP (Apache, nginx o `python -m http.server`) en un host permitido, o una carpeta bajo `/dicom` (lo que esté en `DICOM_HOST_DIR`):

```bash
# crear un lote -> 201 {"batch_id": 2}; 400 con el motivo si el host no está permitido o la ruta sale de /dicom
curl -s -X POST localhost:8889/batches -H 'Content-Type: application/json' \
  -d '{"source":"http://servidor-dicom:8080/estudios/"}'

# progreso: estado del lote, conteo por estado, scores de los DONE y motivos de INCOMPLETE/FAILED (sin datos del paciente)
curl -s localhost:8889/batches/2

# reportes generados al momento desde MySQL (carpeta + seudónimo de cada problema, archivos ignorados)
curl -s -o fallos_batch_2.txt  localhost:8889/batches/2/fallos.txt
curl -s -o report_batch_2.xlsx localhost:8889/batches/2/report.xlsx
```

Los estudios se identifican por el StudyInstanceUID de las cabeceras, no por la carpeta: el árbol puede tener cualquier profundidad y nombres arbitrarios. El panel Streamlit tiene la vista "Lotes" con el progreso en vivo y los mismos reportes.

Lo mismo por consola: `docker compose exec worker-crawl python -m ingest.cli create-batch /dicom/mi-carpeta` y `... python -m ingest.cli status [batch_id]`.

### Seudonimización (`PSEUDONYM_SECRET`)

Toda anonimización (lotes, `/ingest`, modo .zip del panel) reemplaza PatientID/PatientName y los UIDs de estudio, serie e imagen por seudónimos deterministas HMAC-SHA256 con la clave `PSEUDONYM_SECRET` (obligatoria, mínimo 32 caracteres; `docker compose` no arranca sin ella). Generala una sola vez con `python -c "import secrets;print(secrets.token_hex(32))"` y guardala fuera de git (`.env` local o gestor de secretos). Mantenela estable: si cambia, el mismo paciente o estudio recibe otro seudónimo y se pierde el vínculo con los lotes anteriores. Nunca la subas al repositorio.

## 4. Probar la instalación

MIT JCLINIC provee estudios de demostración (una mamografía de ejemplo y un estudio LDCT) para validar que MIRAI responde. No se versionan en este repo por tamaño: descargalos desde los links de la guía oficial de instalación y descomprimilos en `./data/mirai_demo_data` (el panel la propone por defecto como `/dicom/mirai_demo_data`), o probá directo contra el contenedor:

```bash
curl -s -X POST \
  -F 'data={"metadata":{"mrn":"test","accession":"test","patient_name":"demo"}}' \
  -F 'dicom=@/ruta/a/ccr1.dcm' \
  -F 'dicom=@/ruta/a/ccl1.dcm' \
  -F 'dicom=@/ruta/a/mlor2.dcm' \
  -F 'dicom=@/ruta/a/mlol2.dcm' \
  http://localhost:8885/dicom/files
```

MIRAI requiere las 4 proyecciones estándar (L-CC, L-MLO, R-CC, R-MLO); no procesa estudios parciales.

## Estructura del repositorio

```
IA-MAMOGRAFIA/
├── docker-compose.yml              # orquesta mirai + backend
├── .env.example                    # variables del backend
└── MIRAI-ANALITIC/
    ├── app/                        # FastAPI + Streamlit (backend)
    ├── RETROSPECTIVE/              # pipeline batch de anonimización/envío a MIRAI
    └── scripts/meddream_scraper/   # scraper Playwright del visor MedDream
```

## Problemas comunes

- **No se puede acceder a `localhost:8885`**: confirmar que el contenedor `mirai` esté corriendo (`docker compose ps`) y que se esté usando `http`, no `https`.
- **Puerto ya en uso**: cambiar el mapeo de puertos (`"8885:5000"`, etc.) en `docker-compose.yml`.
- **"Out of memory" al iniciar MIRAI**: Docker necesita al menos 8 GB de RAM asignados (Docker Desktop → Settings → Resources).
- **Disco lleno**: MIRAI necesita 15 GB libres.

---

Referencia: *Instalación de MIRAI/Sybil – MIT JCLINIC* (traducción al español, octubre 2026).
