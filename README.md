# MIRAI-ANALISI

Pipeline local para analizar mamografías DICOM con el modelo de riesgo **MIRAI** (MIT JCLINIC), con un backend propio de ingesta/orquestación y un scraper de Playwright para exportar estudios desde el visor MedDream.

Todo corre en contenedores Docker: ni las imágenes DICOM ni los resultados salen de la máquina.

**Inicio rápido:**

- **Servidor Linux de la red del hospital (uso real):** sección [Despliegue en un servidor Linux de la red](#despliegue-en-un-servidor-linux-de-la-red-paso-a-paso).
- **PC Windows de desarrollo:** secciones [1](#1-descargar-la-imagen-de-mirai) y [2](#2-levantar-el-backend-completo-con-docker-compose).
- **Enviar un lote desde otro equipo (supervisor):** sección [Probar desde otra máquina de la red](#probar-desde-otra-máquina-de-la-red-paso-a-paso).
- **Guías independientes en `docs/`:** [docs/despliegue-linux.md](docs/despliegue-linux.md) (instalar y probar en Linux de principio a fin) y [docs/reinicio.md](docs/reinicio.md) (qué hacer después de apagar o reiniciar el equipo).

## Arquitectura

| Servicio | Descripción | Puerto host → contenedor |
|---|---|---|
| `mirai` | Imagen oficial `mitjclinic/mirai`, sirve el modelo de riesgo vía Flask | `8885` → `5000`, `8886` → `8000` |
| `dicom-analyzer` | Panel Streamlit para explorar/anonimizar estudios y ver el progreso de los lotes | `8890` → `8501` |
| `dicom-analyzer-api` | API FastAPI: lotes por URL (`/batches`, con API key), documentación en `/docs` y el endpoint `/ingest` (.zip) | `8889` → `8000` |
| `mysql` | Estado y cola de trabajos de los lotes | `127.0.0.1:3307` → `3306` (solo local) |
| `worker-crawl` | Recorre el árbol (carpeta o URL) y agrupa las imágenes por estudio | — |
| `worker-fetch` | Descarga, seudonimiza y arma las 4 vistas de cada estudio | — |
| `worker-score` | Envía cada estudio completo a MIRAI (uno a la vez) | — |

Definidos en `docker-compose.yml`, en la raíz del repo.

Los puertos `8885`/`8886` son los que indica la guía oficial para MIRAI; `8887`/`8888` quedan reservados para Sybil, por eso los servicios propios usan `8889` y `8890`.

## Requisitos

- Docker: Docker Desktop en Windows (con WSL2) o Docker Engine + Compose en Linux
- 15 GB de almacenamiento libre (20 GB recomendado con staging de lotes)
- 8 GB de RAM libres para MIRAI (16 GB de RAM total recomendado)
- Procesador moderno (i5/i7 o equivalente)

(Especificaciones según la guía oficial *Instalación de MIRAI/Sybil* de MIT JCLINIC.)

En Windows, Docker Desktop limita la RAM de la VM de WSL: si MIRAI se cae con `OOMKilled` (ver `docker inspect mirai-container --format '{{.State.OOMKilled}}'`), detener otros contenedores o subir `memory=` en `%UserProfile%\.wslconfig`. En Linux no hay ese límite.

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

En Windows, ejecutar estos comandos en **Git Bash**; en Linux, en la terminal.

```bash
# Clonar el repo y usar la rama con la ingesta por URL (hasta que se fusione a main)
git clone https://github.com/Iseeicore/MIRAI-ANALISI.git
cd MIRAI-ANALISI
git switch feat/url-batch-ingestion

# Crear el .env con todas las claves obligatorias (se escriben en el archivo, no se muestran)
python - <<'EOF' > .env
import secrets
print("COMPOSE_SUBNET=172.30.0.0/24")
print("MYSQL_ROOT_PASSWORD=" + secrets.token_urlsafe(24))
print("MYSQL_PASSWORD=" + secrets.token_urlsafe(24))
print("PSEUDONYM_SECRET=" + secrets.token_hex(32))
print("INGEST_API_KEYS=" + secrets.token_urlsafe(32))
EOF

# Validar la configuración sin mostrar valores
docker compose config -q && echo OK

# Levantar todo (descarga MIRAI la primera vez y construye las imágenes propias)
docker compose up -d --build
```

En Linux usar `python3` si `python` no existe. `.env.example` no incluye las variables obligatorias de los lotes: no usarlo como base. Si `docker compose up` falla con `Pool overlaps with other one`, cambiar `COMPOSE_SUBNET` por una subred libre (ver la tabla de la sección 3).

Servicios disponibles:

- MIRAI: http://localhost:8885
- Panel Streamlit (`dicom-analyzer`): http://localhost:8890 (vista "Lotes" en la barra lateral)
- API FastAPI (`dicom-analyzer-api`): http://localhost:8889, con documentación interactiva en http://localhost:8889/docs

Otros comandos útiles:

```bash
docker compose ps                   # estado de los contenedores
docker compose logs -f mirai        # ver logs de un servicio
docker compose stop                 # apagar sin borrar datos
docker compose start                # volver a prender
docker compose down                 # eliminar los contenedores (los volúmenes y datos se conservan)
```

> [!IMPORTANT]
> MySQL toma las contraseñas del `.env` solo la primera vez que crea su volumen. Si se cambian después, hay que recrearlo (se pierden los lotes registrados): `docker compose down && docker volume rm mirai-analisi_ingest_mysql && docker compose up -d`.

## 3. Variables de entorno

### Backend (`.env` en la raíz)

Obligatorias (sin ellas `docker compose` no arranca): `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `PSEUDONYM_SECRET`, `INGEST_API_KEYS`. El detalle de las variables de lotes está en la sección "Batch analysis by URL".

| Variable | Uso | Default |
|---|---|---|
| `COMPOSE_SUBNET` | Subred interna de Docker para este proyecto; debe no coincidir con otras redes (`ip -4 route` en Linux, `docker network ls`) | `172.30.0.0/24` |
| `DICOM_HOST_DIR` | Carpeta del host (ruta Windows o Linux) con los DICOM; se monta de solo lectura en `/dicom` | `./data` |
| `INGEST_BLOCK_SIZE` | Tamaño de bloque que procesa `/ingest` (.zip) por request | `50` |

Reglas del `.env`: una variable por línea, sin comillas; si una variable aparece dos veces, vale la última. Al agregar líneas con `>>`, empezar con un salto de línea (`'\n'`) para no pegarlas a la línea anterior. Nunca subir `.env` a git ni pegar sus valores en un chat.

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

Procesa árboles grandes de estudios en segundo plano: `worker-crawl` recorre el árbol y agrupa las imágenes por estudio, `worker-fetch` descarga, seudonimiza y arma las 4 vistas, y `worker-score` las envía a MIRAI de a una. El estado vive en MySQL (`mysql`, publicado solo en `127.0.0.1:3307`).

Variables (en el entorno o en `.env`; las obligatorias sin valor impiden que `docker compose` arranque):

| Variable | Uso | Default |
|---|---|---|
| `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` | Contraseñas de MySQL | — (obligatorias) |
| `MYSQL_DATABASE` / `MYSQL_USER` | Base y usuario de la app | `mirai_ingest` / `mirai` |
| `INGEST_API_KEYS` | Claves aceptadas en la cabecera `X-API-Key` de `/batches*`, separadas por coma, de 32+ caracteres. Sin claves válidas, `/batches*` responde 503 | — (obligatoria) |
| `INGEST_ALLOWED_NETWORKS` | Redes (CIDR) desde donde se aceptan servidores de imágenes | `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` |
| `INGEST_ALLOWED_HOSTS` | Excepción explícita para hosts con nombre (`host` o `host:puerto`); no pasan el chequeo de red | vacío |
| `INGEST_MAX_FILE_BYTES` | Tamaño máximo por archivo descargado | `104857600` (100 MB) |
| `STAGING_MAX_STUDIES` | Estudios en staging a la vez (contrapresión) | `20` |

**Qué URLs se aceptan.** Cualquier servidor de la red interna del hospital, sin configurarlo antes: el nombre se resuelve y todas sus IPs deben caer en `INGEST_ALLOWED_NETWORKS`. Siempre se rechazan loopback, link-local (incluida la metadata de nube `169.254.169.254`), multicast, broadcast, la red de Docker (`172.17.0.0/16`) y la red de este compose (`COMPOSE_SUBNET`), para que nadie use la API para llegar a MySQL, MIRAI u otros servicios internos. En `http` la conexión va a la IP validada. Antes de crear el lote se lista la URL: si no responde (422 "could not connect to ..."), si no es un listado de carpetas o está vacía (422) o si la red no está permitida (400), el lote no se crea.

**Clave de API.** Generá una por persona o equipo con `python -c "import secrets;print(secrets.token_urlsafe(32))"` y agregala a `INGEST_API_KEYS` (separadas por coma). Para revocar una, quitala y reiniciá `dicom-analyzer-api`. En `http://<IP-DEL-SERVIDOR-MIRAI>:8889/docs` el botón "Authorize" permite cargar la clave y probar desde el navegador.

Curl listo para compartir (el supervisor solo reemplaza los tres valores):

```bash
curl -s -X POST http://<IP-DEL-SERVIDOR-MIRAI>:8889/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <API-KEY>' \
  -d '{"source":"<URL-DE-LAS-IMAGENES>"}'
# -> 201 {"batch_id": 7}
```

Seguimiento y reportes (misma clave):

```bash
curl -s -H 'X-API-Key: <API-KEY>' http://<IP-DEL-SERVIDOR-MIRAI>:8889/batches/7
curl -s -H 'X-API-Key: <API-KEY>' -o fallos_batch_7.txt  http://<IP-DEL-SERVIDOR-MIRAI>:8889/batches/7/fallos.txt
curl -s -H 'X-API-Key: <API-KEY>' -o report_batch_7.xlsx http://<IP-DEL-SERVIDOR-MIRAI>:8889/batches/7/report.xlsx
```

También acepta una carpeta montada bajo `/dicom` (lo que esté en `DICOM_HOST_DIR`) como `source`.

Los estudios se identifican por el StudyInstanceUID de las cabeceras, no por la carpeta: el árbol puede tener cualquier profundidad y nombres arbitrarios. El panel Streamlit tiene la vista "Lotes" con el progreso en vivo y los mismos reportes.

Lo mismo por consola: `docker compose exec worker-crawl python -m ingest.cli create-batch /dicom/mi-carpeta` y `... python -m ingest.cli status [batch_id]`.

### Despliegue en un servidor Linux de la red (paso a paso)

Para que otros equipos del hospital usen el servicio, el stack debe correr en una máquina **dentro de la red del hospital** (ver `docs/knowledge/constraints/deployment-network-reachability.md`). Esta guía usa Ubuntu 22.04/24.04. Cada comando indica qué hace.

#### 0. Requisitos de la máquina

```bash
free -h            # RAM disponible: MIRAI necesita unos 6-8 GB libres (recomendado 16 GB de RAM total)
df -h /            # disco libre: mínimo 20 GB (imagen de MIRAI ~3.7 GB + staging de hasta ~2 GB)
nproc              # núcleos de CPU: en CPU cada estudio tarda ~30-60 s
```

#### 1. Red: cable o Wi-Fi

```bash
nmcli device status   # lista las interfaces y cuál está conectada (ethernet = cable, wifi = Wi-Fi)
ip -4 addr show       # muestra la IP de cada interfaz (cable: enp*/eth*, Wi-Fi: wlp*/wlan*)
hostname -I           # muestra solo las IPs del equipo
```

- **Cable (recomendado para un servidor).** La conexión es estable y los demás equipos pueden llegar a ella. Anotar la IP de la interfaz `enp*`/`eth*`.
- **Wi-Fi.** Funciona, pero algunas redes Wi-Fi aíslan a los equipos entre sí (*client isolation*) y la IP puede cambiar. Anotar la IP de `wlp*`/`wlan*` y probar el acceso desde otro equipo antes de compartirla.
- **IP fija.** Si la IP cambia, el `curl` compartido deja de funcionar. Lo correcto es pedir a soporte de TI una **reserva DHCP** para la MAC del equipo (`ip link show` muestra la MAC). No configurar una IP estática manual sin coordinarlo con TI.

#### 2. Instalar Docker Engine y Compose (método oficial de Docker)

```bash
# Herramientas base y clave GPG del repositorio de Docker
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Registrar el repositorio de Docker para esta versión de Ubuntu
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# Instalar Docker Engine y el plugin de Compose
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Arrancar Docker al encender el equipo
sudo systemctl enable --now docker

# Usar docker sin sudo (cerrar sesión y volver a entrar para que aplique)
sudo usermod -aG docker $USER
```

Verificar con `docker --version` y `docker compose version`.

#### 3. Obtener el proyecto

```bash
git clone https://github.com/Iseeicore/MIRAI-ANALISI.git   # descarga el repositorio
cd MIRAI-ANALISI
git switch feat/url-batch-ingestion                          # rama con la ingesta por URL (hasta que se fusione a main)
```

#### 4. Crear el `.env` con claves nuevas

```bash
ip -4 route   # redes en uso en este equipo; COMPOSE_SUBNET no debe coincidir con ninguna
docker network ls -q | xargs -r docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}}'   # redes de Docker existentes

python3 - <<'EOF' > .env
import secrets
print("COMPOSE_SUBNET=172.30.0.0/24")                      # cambiar si choca con una red listada arriba
print("MYSQL_ROOT_PASSWORD=" + secrets.token_urlsafe(24))
print("MYSQL_PASSWORD=" + secrets.token_urlsafe(24))
print("PSEUDONYM_SECRET=" + secrets.token_hex(32))          # no cambiarla nunca después de procesar datos reales
print("INGEST_API_KEYS=" + secrets.token_urlsafe(32))       # la clave que se entrega a quien envía lotes
EOF
chmod 600 .env   # solo el dueño del archivo puede leerlo
```

El script escribe las claves directamente en `.env` sin mostrarlas. Para ver la API key y entregarla por un canal privado: `grep '^INGEST_API_KEYS=' .env | cut -d= -f2-`.

Para analizar carpetas locales además de URLs, agregar `DICOM_HOST_DIR=/ruta/a/los/dicom` al `.env`; esa carpeta se monta como `/dicom`.

#### 5. Levantar el stack

```bash
docker compose up -d --build   # descarga MIRAI (~3.7 GB la primera vez), construye las imágenes propias y arranca todo
docker compose ps              # todos los servicios deben estar "Up"; mysql debe estar "healthy"
docker compose logs -f worker-score   # seguir los logs de un servicio (Ctrl+C para salir)
```

Los servicios tienen `restart: unless-stopped`: vuelven solos después de reiniciar el equipo, porque Docker arranca con el sistema (paso 2).

#### 6. Verificar

```bash
curl -s http://localhost:8889/health                       # en el propio servidor: {"status":"ok",...}
curl -s http://<IP-DEL-SERVIDOR>:8889/health               # desde otro equipo de la red: mismo resultado
```

Después seguir la sección "Probar desde otra máquina de la red" con la IP de este servidor.

#### 7. Firewall en Linux: atención

En Ubuntu, `ufw` viene desactivado por defecto (`sudo ufw status`). Aunque esté activo, **los puertos publicados por Docker no pasan por `ufw`**: Docker redirige el tráfico en la tabla `nat` antes de que se apliquen sus reglas (documentado por Docker). En consecuencia:

- No hace falta `ufw allow` para 8889 y 8890: ya son accesibles desde la red.
- `ufw deny` **no** los cierra. Todo puerto publicado sin IP (`"8885:5000"`) queda expuesto en todas las interfaces, incluido MIRAI en 8885/8886, que no exige API key.
- Para limitar un puerto al propio servidor, publicarlo con IP: `"127.0.0.1:8885:5000"` en `docker-compose.yml` (así está MySQL). Para filtrar por origen, usar la cadena `DOCKER-USER` de iptables o el firewall de red del hospital.

#### 8. Operación diaria

| Acción | Comando |
|---|---|
| Ver estado | `docker compose ps` |
| Apagar (conserva datos) | `docker compose stop` |
| Prender | `docker compose start` (o `docker compose up -d`) |
| Ver logs | `docker compose logs -f <servicio>` |
| Actualizar el código | `git pull && docker compose up -d --build` |
| Uso de RAM/CPU por contenedor | `docker stats --no-stream` |
| Revocar una API key | editarla en `INGEST_API_KEYS` del `.env` y ejecutar `docker compose up -d` |

#### Equivalencias Windows ↔ Linux

| Tarea | Windows (servidor de prueba) | Linux (servidor de la red) |
|---|---|---|
| Ver la IP | `ipconfig` | `ip -4 addr show` / `hostname -I` |
| Ver tipo de red | `Get-NetConnectionProfile` | `nmcli device status` |
| Abrir puertos | `New-NetFirewallRule ... -LocalPort 8889,8890 -Profile Any` (PowerShell admin) | No hace falta: Docker publica por encima de `ufw` |
| Leer la API key | `(Select-String -Path .env -Pattern '^INGEST_API_KEYS=' \| Select-Object -Last 1).Line.Split('=', 2)[1]` | `grep '^INGEST_API_KEYS=' .env \| cut -d= -f2-` |
| Probar conectividad | `Test-NetConnection <IP> -Port 8889` | `curl -m 5 http://<IP>:8889/health` |
| Límite de RAM de Docker | Docker Desktop/WSL: `%UserProfile%\.wslconfig` | Sin límite: usa la RAM del equipo |

### Probar desde otra máquina de la red (paso a paso)

Se llama **servidor MIRAI** al equipo Windows donde corre `docker compose`, y **cliente** al equipo desde donde se envía el lote (por ejemplo, el de un supervisor). El cliente debe estar en la **misma red local** que el servidor MIRAI. Si el servidor MIRAI entra a la red del hospital por una VPN de cliente (por ejemplo, SonicWall NetExtender), los equipos del hospital normalmente no pueden conectarse a él: la VPN solo permite tráfico saliente. Las descargas de imágenes sí salen por la VPN.

#### En el servidor MIRAI (Windows)

**1. Obtener la IP** (PowerShell o CMD):

```powershell
ipconfig
```

Usar la "Dirección IPv4" del adaptador **Wi-Fi** o **Ethernet** (por ejemplo `192.168.1.50`). No usar las de `vEthernet (WSL)` ni la de la VPN.

**2. Abrir los puertos en el firewall** (PowerShell **como administrador**):

```powershell
New-NetFirewallRule -DisplayName "MIRAI API" -Direction Inbound -Protocol TCP -LocalPort 8889,8890 -Action Allow -Profile Any
```

Si la regla ya existe con otros perfiles, se corrige con `Set-NetFirewallRule -DisplayName "MIRAI API" -Profile Any`. Para ver si la red es Pública o Privada: `Get-NetConnectionProfile`. Solo hay que abrir 8889 (API) y 8890 (panel); MySQL queda en `127.0.0.1` y no se expone.

**3. Leer la API key** para entregarla al cliente por un canal privado (nunca por chat ni correo sin cifrar). Hay que estar en la carpeta del proyecto, que es donde está `.env`:

- PowerShell:

  ```powershell
  cd "C:\ruta\a\MIRAI-ANALISI"
  (Select-String -Path .env -Pattern '^INGEST_API_KEYS=' | Select-Object -Last 1).Line.Split('=', 2)[1]
  ```

- Git Bash:

  ```bash
  cd /c/ruta/a/MIRAI-ANALISI
  grep '^INGEST_API_KEYS=' .env | tail -1 | cut -d= -f2-
  ```

`grep`, `tail` y `cut` existen en Git Bash, no en PowerShell.

#### En el cliente Ubuntu/Linux (terminal)

Reemplazar `192.168.1.50` por la IP del paso 1.

```bash
# 1. curl instalado
sudo apt update && sudo apt install -y curl

# 2. Conectividad: debe responder {"status":"ok",...}
curl -m 5 http://192.168.1.50:8889/health

# 3. Guardar la clave sin que quede en el historial
read -s -p "API key: " KEY; echo

# 4. Probar la clave: debe devolver JSON (o 404 si el lote no existe), nunca 401
curl -s -H "X-API-Key: $KEY" http://192.168.1.50:8889/batches/1; echo

# 5. Lanzar un lote (carpeta de prueba, o la URL real del servidor de imágenes)
curl -s -X POST http://192.168.1.50:8889/batches \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"source":"/dicom/mirai_demo_data"}'; echo
#    con la URL real: -d '{"source":"http://IP-SERVIDOR:PUERTO/ruta/"}'

# 6. Ver el avance cada 5 s (Ctrl+C para salir); N = batch_id devuelto
watch -n 5 "curl -s -H 'X-API-Key: $KEY' http://192.168.1.50:8889/batches/N"

# 7. Descargar los reportes
curl -s -H "X-API-Key: $KEY" -o fallos.txt   http://192.168.1.50:8889/batches/N/fallos.txt
curl -s -H "X-API-Key: $KEY" -o reporte.xlsx http://192.168.1.50:8889/batches/N/report.xlsx
```

También se puede usar el navegador, incluso desde un celular en la misma red:

1. Abrir `http://192.168.1.50:8889/docs`.
2. **Authorize** → pegar la clave → **Authorize** → **Close**.
3. `POST /batches` → **Try it out** → **reemplazar el ejemplo** del body por la ruta real (o `{"source": "/dicom/mirai_demo_data"}` para una prueba) → **Execute**.
4. Respuesta esperada: `201` con `{"batch_id": N}`. Seguir el avance con `GET /batches/{batch_id}`.

Si la clave se cambia en el servidor, Swagger conserva la anterior: hacer **Authorize → Logout** y cargar la nueva. Un `401` con una clave que antes funcionaba indica que fue rotada.

#### Rotar la API key (por ejemplo, si se filtró)

```bash
python -c "import secrets;print('\nINGEST_API_KEYS='+secrets.token_urlsafe(32))" >> .env   # agrega una clave nueva; vale la última línea
docker compose up -d                                                                     # recrea la API con la clave nueva
```

La clave anterior deja de funcionar al instante (`401`). Para tener varias claves activas a la vez (una por persona), separarlas con coma en una sola línea `INGEST_API_KEYS=clave1,clave2`.

#### En un cliente Windows (PowerShell)

En PowerShell, `curl` es un alias de otro comando: usar `curl.exe`, o `Invoke-RestMethod` para enviar JSON sin problemas de comillas.

```powershell
Test-NetConnection 192.168.1.50 -Port 8889          # TcpTestSucceeded : True
$KEY = Read-Host "API key"
$body = @{ source = "/dicom/mirai_demo_data" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://192.168.1.50:8889/batches" -Headers @{ "X-API-Key" = $KEY } -ContentType "application/json" -Body $body
Invoke-RestMethod -Uri "http://192.168.1.50:8889/batches/N" -Headers @{ "X-API-Key" = $KEY }
```

#### Si algo falla

| Síntoma | Causa probable |
|---|---|
| El paso 2 queda colgado o da timeout | Firewall del servidor MIRAI (repetir el paso 2 del servidor con `-Profile Any`), o el cliente no está en la misma red: revisar su IP con `ip a` (Linux) o `ipconfig` (Windows). Algunas redes Wi-Fi aíslan a los equipos entre sí; probar por cable. |
| `Connection refused` | El stack está detenido: en el servidor MIRAI, `docker compose up -d`. |
| `{"detail":"invalid or missing API key"}` (401) | La clave está mal copiada o tiene espacios. |
| 422 al crear el lote | La carpeta o la URL no existe, no responde o no es un listado de carpetas; el mensaje indica cuál. |
| 400 al crear el lote | La URL apunta a una red no permitida (internet, localhost, red de Docker). |

### Seudonimización (`PSEUDONYM_SECRET`)

Toda anonimización (lotes, `/ingest`, modo .zip del panel) reemplaza PatientID/PatientName y los UIDs de estudio, serie e imagen por seudónimos deterministas HMAC-SHA256 con la clave `PSEUDONYM_SECRET` (obligatoria, mínimo 32 caracteres; `docker compose` no arranca sin ella). Generala una sola vez con `python -c "import secrets;print(secrets.token_hex(32))"` y guardala fuera de git (`.env` local o gestor de secretos). Mantenela estable: si cambia, el mismo paciente o estudio recibe otro seudónimo y se pierde el vínculo con los lotes anteriores. Nunca la subas al repositorio.

## 4. Probar la instalación

MIT JCLINIC publica una mamografía de demostración (4 proyecciones) para validar que MIRAI responde. No se versiona en este repo por tamaño (~60 MB).

**1. Descargar la mamografía de demostración** (Linux o Git Bash; en Linux instalar `unzip` con `sudo apt install -y unzip` si falta):

```bash
mkdir -p data/mirai_demo_data
curl -L -o /tmp/mirai_demo_data.zip https://github.com/reginabarzilaygroup/Mirai/releases/download/v0.14.1/mirai_demo_data.zip
unzip -o /tmp/mirai_demo_data.zip -d data/mirai_demo_data   # deja ccl1.dcm, ccr1.dcm, mlol2.dcm y mlor2.dcm
```

`data/` está en `.gitignore`. Dentro de los contenedores esa carpeta se ve como `/dicom/mirai_demo_data`.

**2. Probar MIRAI directo** (sin API key; el modelo tarda ~45 s en CPU):

```bash
cd data/mirai_demo_data
curl -s -X POST \
  -F 'data={"metadata":{"mrn":"test","accession":"test","patient_name":"demo"}}' \
  -F 'dicom=@ccr1.dcm' -F 'dicom=@ccl1.dcm' -F 'dicom=@mlor2.dcm' -F 'dicom=@mlol2.dcm' \
  http://localhost:8885/dicom/files
cd ../..
```

Resultado esperado: `"predictions":{"Year 1":0.0298,"Year 2":0.0483,"Year 3":0.0684,"Year 4":0.09,"Year 5":0.1016}`.

**3. Probar el flujo completo por lotes** (con la API key del `.env`):

```bash
KEY=$(grep '^INGEST_API_KEYS=' .env | tail -1 | cut -d= -f2- | cut -d, -f1)
curl -s -X POST http://localhost:8889/batches -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"source":"/dicom/mirai_demo_data"}'; echo          # -> {"batch_id": N}
curl -s -H "X-API-Key: $KEY" http://localhost:8889/batches/N; echo   # repetir hasta ver "DONE":1 con los mismos scores
```

MIRAI requiere las 4 proyecciones estándar (L-CC, L-MLO, R-CC, R-MLO); un estudio con menos queda como `INCOMPLETE` en `fallos.txt`.

## Estructura del repositorio

```
MIRAI-ANALISI/
├── docker-compose.yml              # mirai + panel + API + mysql + workers
├── .env                            # claves locales (no se versiona; ver sección 2)
├── data/                           # DICOM locales montados en /dicom (no se versiona)
├── docs/knowledge/                 # decisiones, features y restricciones del proyecto
└── MIRAI-ANALITIC/
    ├── app/
    │   ├── api.py                  # FastAPI: /batches, /ingest, /docs
    │   ├── streamlit_app.py        # panel (análisis directo y vista "Lotes")
    │   ├── ingest/                 # lotes: fuentes, workers, MySQL, reportes
    │   └── test_*.py               # tests (python test_x.py dentro de la imagen)
    ├── RETROSPECTIVE/              # escaneo, saneo y anonimización HMAC de DICOM
    └── scripts/meddream_scraper/   # scraper Playwright del visor MedDream
```

Para correr los tests: `docker compose build dicom-analyzer` y luego, por ejemplo, `docker run --rm mirai-analisi-dicom-analyzer python test_ingest_api.py`.

## Problemas comunes

| Síntoma | Causa y solución |
|---|---|
| `required variable ... is missing a value` al usar `docker compose` | Falta una variable obligatoria en `.env` o quedó pegada a otra línea. Revisar que cada variable esté en su propia línea (sección 2). |
| `Pool overlaps with other one on this address space` | `COMPOSE_SUBNET` coincide con otra red. Elegir una libre (por ejemplo `172.32.0.0/24`) y repetir `docker compose up -d`. |
| Workers con `Access denied for user` en los logs | Se cambiaron las contraseñas de MySQL después de crear el volumen: recrearlo (sección 2, aviso de MySQL). |
| MIRAI no responde o el estudio queda `FAILED` con un timeout | MIRAI se quedó sin memoria: `docker inspect mirai-container --format '{{.State.OOMKilled}}'` devuelve `true`. Liberar RAM (sección Requisitos). |
| No se puede acceder a `localhost:8885` | Confirmar que `mirai` esté corriendo (`docker compose ps`) y usar `http`, no `https`. |
| Otro equipo no llega a `:8889` | Firewall del servidor o redes distintas: ver "Probar desde otra máquina de la red". |
| `401 invalid or missing API key` | Clave mal copiada o rotada; en Swagger, **Logout** y volver a autorizar. |
| `422` al crear un lote | La URL o carpeta no existe, no responde o no es un listado de carpetas; el mensaje indica cuál. |
| `400` al crear un lote | La URL apunta a una red no permitida (internet, localhost, red de Docker). |
| Puerto ya en uso | Cambiar el mapeo (`"8885:5000"`, etc.) en `docker-compose.yml`. |
| Disco lleno | MIRAI necesita 15 GB libres, más hasta ~2 GB de staging de lotes. |

---

Referencia: *Instalación de MIRAI/Sybil – MIT JCLINIC* (traducción al español, octubre 2026).
