# Despliegue y prueba en un servidor Linux

Guía completa para instalar MIRAI-ANALISI en una PC Linux (Ubuntu 22.04/24.04) dentro de la red del hospital, probarla y entregarle el `curl` a quien envía los lotes. Cada comando indica qué hace. Para volver a levantar el servicio después de apagar el equipo, ver [reinicio.md](reinicio.md).

> [!IMPORTANT]
> El servidor tiene que estar **en la red del hospital** (por cable o Wi-Fi de la institución). Una PC conectada al hospital solo por VPN de cliente no puede recibir conexiones de los equipos del hospital.

## 1. Verificar la máquina

```bash
free -h      # RAM: MIRAI necesita unos 6-8 GB libres (recomendado 16 GB en total)
df -h /      # disco: mínimo 20 GB libres (imagen de MIRAI ~3.7 GB + staging de lotes ~2 GB)
nproc        # núcleos de CPU: en CPU cada estudio tarda ~30-60 s
lsb_release -a   # versión de Ubuntu
```

## 2. Red: cable o Wi-Fi

```bash
nmcli device status   # interfaces y cuál está conectada (ethernet = cable, wifi = Wi-Fi)
ip -4 addr show       # IP de cada interfaz (cable: enp*/eth*, Wi-Fi: wlp*/wlan*)
hostname -I           # solo las IPs del equipo
ip link show          # direcciones MAC (para pedir una IP fija a TI)
```

- **Cable (recomendado):** conexión estable y alcanzable. Anotar la IP de `enp*`/`eth*`.
- **Wi-Fi:** algunas redes aíslan a los equipos entre sí y la IP puede cambiar. Anotar la IP de `wlp*`/`wlan*` y probar desde otro equipo antes de compartirla.
- **IP fija:** pedir a soporte de TI una **reserva DHCP** para la MAC del equipo. Si la IP cambia, el `curl` compartido deja de funcionar.

## 3. Instalar Docker Engine y Compose

Método oficial de Docker para Ubuntu:

```bash
# Herramientas base y clave GPG del repositorio de Docker
sudo apt update
sudo apt install -y ca-certificates curl git unzip
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

Después de volver a entrar:

```bash
docker --version          # debe mostrar la versión de Docker
docker compose version    # debe mostrar la versión de Compose
docker run --rm hello-world   # prueba que Docker funciona sin sudo
```

## 4. Obtener el proyecto

```bash
git clone https://github.com/Iseeicore/MIRAI-ANALISI.git   # descarga el repositorio
cd MIRAI-ANALISI
git switch feat/url-batch-ingestion                          # rama con la ingesta por URL (hasta que se fusione a main)
```

Si el repositorio es privado, Git pide credenciales: usar un token personal de GitHub como contraseña, o configurar una clave SSH.

## 5. Crear el `.env`

```bash
ip -4 route   # redes en uso en este equipo
docker network ls -q | xargs -r docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}}'   # redes de Docker
```

`COMPOSE_SUBNET` no debe coincidir con ninguna de las redes listadas. Generar el archivo (las claves se escriben sin mostrarse):

```bash
python3 - <<'EOF' > .env
import secrets
print("COMPOSE_SUBNET=172.30.0.0/24")                      # cambiar si choca con una red listada arriba
print("MYSQL_ROOT_PASSWORD=" + secrets.token_urlsafe(24))
print("MYSQL_PASSWORD=" + secrets.token_urlsafe(24))
print("PSEUDONYM_SECRET=" + secrets.token_hex(32))          # no cambiarla nunca después de procesar datos reales
print("INGEST_API_KEYS=" + secrets.token_urlsafe(32))       # la clave que se entrega a quien envía lotes
EOF
chmod 600 .env                         # solo el dueño puede leer el archivo
docker compose config -q && echo OK    # valida la configuración sin mostrar valores
```

Opcional: para analizar carpetas locales además de URLs, agregar `DICOM_HOST_DIR=/ruta/a/los/dicom` (esa carpeta se ve como `/dicom` dentro de los contenedores).

Reglas del `.env`: una variable por línea; si una variable aparece dos veces, vale la última. Hacer una copia de seguridad del `.env` en un lugar seguro: si se pierde `PSEUDONYM_SECRET`, los seudónimos de lotes futuros no coinciden con los anteriores.

## 6. Levantar el stack

```bash
docker compose up -d --build          # descarga MIRAI (~3.7 GB la primera vez), construye las imágenes y arranca todo
docker compose ps                     # todos "Up"; mysql "healthy"
docker compose logs -f worker-score   # seguir un servicio (Ctrl+C para salir)
```

Los servicios tienen `restart: unless-stopped`: vuelven solos al reiniciar el equipo, porque Docker arranca con el sistema.

## 7. Verificar

```bash
curl -s http://localhost:8889/health; echo           # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8889/docs   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8890        # 200 (panel)
```

Desde otro equipo de la red: `curl -m 5 http://<IP-DEL-SERVIDOR>:8889/health`.

## 8. Probar con la mamografía de demostración

```bash
# Descargar la demostración oficial (4 proyecciones, ~60 MB)
mkdir -p data/mirai_demo_data
curl -L -o /tmp/mirai_demo_data.zip https://github.com/reginabarzilaygroup/Mirai/releases/download/v0.14.1/mirai_demo_data.zip
unzip -o /tmp/mirai_demo_data.zip -d data/mirai_demo_data

# Lanzar un lote con la API key y seguirlo
KEY=$(grep '^INGEST_API_KEYS=' .env | tail -1 | cut -d= -f2- | cut -d, -f1)
curl -s -X POST http://localhost:8889/batches -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"source":"/dicom/mirai_demo_data"}'; echo                  # -> {"batch_id": N}
watch -n 5 "curl -s -H 'X-API-Key: $KEY' http://localhost:8889/batches/N"   # reemplazar N; Ctrl+C para salir
```

Resultado esperado en unos 45-60 s: `"DONE":1` con `Year 1` 0.0298, `Year 2` 0.0483, `Year 3` 0.0684, `Year 4` 0.09 y `Year 5` 0.1016.

Reportes del lote:

```bash
curl -s -H "X-API-Key: $KEY" -o fallos.txt   http://localhost:8889/batches/N/fallos.txt
curl -s -H "X-API-Key: $KEY" -o reporte.xlsx http://localhost:8889/batches/N/report.xlsx
cat fallos.txt
```

## 9. Entregar el acceso a quien envía los lotes

1. Leer la clave y pasarla por un canal privado (nunca por chat ni correo abierto):

   ```bash
   grep '^INGEST_API_KEYS=' .env | tail -1 | cut -d= -f2-
   ```

2. Compartir este `curl`, en el que solo se cambia la URL de las imágenes:

   ```bash
   curl -s -X POST http://<IP-DEL-SERVIDOR>:8889/batches \
     -H "X-API-Key: <API-KEY>" -H "Content-Type: application/json" \
     -d '{"source":"http://<IP-SERVIDOR-IMAGENES>:<PUERTO>/<ruta>/"}'
   ```

3. O compartir la página `http://<IP-DEL-SERVIDOR>:8889/docs`: **Authorize** → pegar la clave → `POST /batches` → **Try it out** → reemplazar el ejemplo por la URL real → **Execute**.

Respuestas posibles:

| Código | Significado |
|---|---|
| `201 {"batch_id": N}` | Lote creado; se procesa en segundo plano |
| `401` | Clave faltante, mal copiada o rotada |
| `400` | La URL apunta a una red no permitida (internet, localhost, red de Docker) |
| `422` | La URL no responde, no es un listado de carpetas o está vacía |

La URL de las imágenes debe mostrar un listado de carpetas por HTTP (nginx, Apache o similar) y estar en una IP privada (`10.x`, `172.16-31.x`, `192.168.x`). Si el hospital usa otro rango, agregarlo en `INGEST_ALLOWED_NETWORKS` del `.env` y ejecutar `docker compose up -d`.

## 10. Firewall en Linux

`ufw` viene desactivado en Ubuntu (`sudo ufw status`). Aunque esté activo, **los puertos publicados por Docker no pasan por `ufw`** (Docker redirige el tráfico antes de sus reglas):

- No hace falta `ufw allow` para 8889 y 8890.
- `ufw deny` no los cierra. MIRAI en 8885/8886 también queda accesible desde la red y no exige API key.
- Para limitar un puerto al propio servidor, publicarlo con IP en `docker-compose.yml` (por ejemplo `"127.0.0.1:8885:5000"`, como está MySQL). Para filtrar por origen, usar la cadena `DOCKER-USER` de iptables o el firewall de red del hospital.

## 11. Operación

| Acción | Comando |
|---|---|
| Estado | `docker compose ps` |
| Apagar sin borrar datos | `docker compose stop` |
| Prender | `docker compose up -d` |
| Logs | `docker compose logs -f <servicio>` |
| Uso de RAM/CPU | `docker stats --no-stream` |
| Actualizar el código | `git pull && docker compose up -d --build` |
| Rotar la API key | `python3 -c "import secrets;print('\nINGEST_API_KEYS='+secrets.token_urlsafe(32))" >> .env && docker compose up -d` |
| Ver la API key activa | `grep '^INGEST_API_KEYS=' .env \| tail -1 \| cut -d= -f2-` |
| Test de MySQL | `docker run --rm --network mirai-analisi_default -e MYSQL_HOST=mysql -e MYSQL_USER=root -e MYSQL_PASSWORD="$(docker exec mirai-ingest-mysql printenv MYSQL_ROOT_PASSWORD)" mirai-analisi-dicom-analyzer python test_ingest_mysql.py` |

Al rotar la clave, la anterior deja de funcionar al instante; en Swagger hay que hacer **Authorize → Logout** y cargar la nueva.

## 12. Problemas comunes

| Síntoma | Causa y solución |
|---|---|
| `permission denied ... docker.sock` | El usuario no está en el grupo `docker`: cerrar sesión y volver a entrar después de `usermod` (paso 3). |
| `required variable ... is missing a value` | Falta una variable en `.env` o quedó pegada a otra línea. |
| `Pool overlaps with other one on this address space` | `COMPOSE_SUBNET` choca con otra red: elegir otra y repetir `docker compose up -d`. |
| Workers con `Access denied for user` | Se cambiaron las contraseñas de MySQL después de crear el volumen: `docker compose down && docker volume rm mirai-analisi_ingest_mysql && docker compose up -d` (se pierden los lotes registrados). |
| Estudio `FAILED` con timeout o MIRAI reiniciado | Falta RAM: `docker inspect mirai-container --format '{{.State.OOMKilled}}'` devuelve `true`. Liberar memoria o usar un equipo con más RAM. |
| Otro equipo no llega a `:8889` | Redes distintas o aislamiento de Wi-Fi: verificar con `hostname -I` y probar por cable. |
| `401` / `400` / `422` al crear un lote | Ver la tabla del paso 9. |
