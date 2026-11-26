# MIRAI-ANALISI

Pipeline local para analizar mamografías DICOM con el modelo de riesgo **MIRAI** (MIT JCLINIC), con un backend propio de ingesta/orquestación y un scraper de Playwright para exportar estudios desde el visor MedDream.

Todo corre en contenedores Docker: ni las imágenes DICOM ni los resultados salen de la máquina.

## Arquitectura

| Servicio | Descripción | Puerto host → contenedor |
|---|---|---|
| `mirai` | Imagen oficial `mitjclinic/mirai`, sirve el modelo de riesgo vía Flask | `8885` → `5000` |
| `dicom-analyzer` | Panel Streamlit para explorar/anonimizar estudios y ver resultados | `8887` → `8501` |
| `dicom-analyzer-api` | API FastAPI que ingiere lotes DICOM y los reenvía a `mirai` | `8889` → `8000` |

Definidos en `docker-compose.yml`, en la raíz del repo.

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
# editar .env: apuntar DICOM_DATA_DIR a la carpeta local con tus estudios DICOM

# Levantar mirai + backend (build de las imágenes propias + pull de mirai)
docker compose up -d --build
```

Servicios disponibles:

- MIRAI: http://localhost:8885
- Panel Streamlit (`dicom-analyzer`): http://localhost:8887
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
| `DICOM_DATA_DIR` | Carpeta local (montada de solo lectura) con los DICOM a analizar | ruta del repo |
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

## 4. Probar la instalación

MIT JCLINIC provee estudios de demostración (una mamografía de ejemplo y un estudio LDCT) para validar que MIRAI responde. No se versionan en este repo por tamaño: descargalos desde los links de la guía oficial de instalación y apuntá `DICOM_DATA_DIR` a esa carpeta, o probá directo contra el contenedor:

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
