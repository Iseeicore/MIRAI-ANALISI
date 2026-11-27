# Volver a levantar el servicio después de apagar el equipo

Lista de pasos para cuando se apaga o reinicia la PC y hay que dejar MIRAI-ANALISI funcionando otra vez. Los datos (lotes, resultados, claves) se conservan: viven en el `.env` y en los volúmenes de Docker.

## Qué se conserva y qué no

| Elemento | ¿Sobrevive al apagado? | Acción |
|---|---|---|
| `.env` (claves y API key) | Sí | Ninguna |
| Lotes y resultados en MySQL | Sí (volumen `mirai-analisi_ingest_mysql`) | Ninguna |
| Imagen de MIRAI y las propias | Sí | Ninguna |
| Regla de firewall de Windows "MIRAI API" | Sí | Ninguna |
| Contenedores en marcha | Arrancan solos con Docker, salvo que se hayan detenido con `docker compose stop` | Paso 2 |
| IP del equipo en la red | **Puede cambiar** (DHCP) | Paso 4 |
| Clave cargada en Swagger (navegador) | Sí, mientras no se rote | Si se rotó: **Authorize → Logout** |
| Lote que estaba en proceso | Se retoma solo: el worker que lo tenía libera la reserva al vencer y otro lo continúa | Ninguna |

## En la PC Windows (desarrollo)

**1. Abrir Docker Desktop** y esperar a que diga *Engine running*. Comprobar en una terminal:

```powershell
docker info --format '{{.ServerVersion}}'   # debe mostrar una versión; si da error, Docker no terminó de arrancar
```

**2. Levantar el proyecto** (PowerShell o Git Bash, en la carpeta del proyecto):

```powershell
cd "C:\Users\<usuario>\Documents\MINSA\MIRAI\MIRAI-ANALISI"
docker compose up -d     # arranca lo que esté detenido; no recrea lo que ya corre
docker compose ps        # los 7 servicios "Up"; mysql "healthy"
```

**3. Liberar memoria para MIRAI.** Docker Desktop tiene un tope de RAM (~7.6 GB en esta PC). Otros proyectos que arrancan solos le quitan memoria a MIRAI. CVAT tiene `restart=always` y vuelve a arrancar con Docker aunque se haya detenido:

```powershell
docker ps --format '{{.Names}}'                     # ver qué más está corriendo
docker stop $(docker ps -q --filter name=cvat_)     # detener CVAT si no se está usando
docker stats --no-stream                            # revisar el uso de memoria
```

Para que CVAT no arranque solo en el próximo reinicio (decisión del dueño de ese proyecto): `docker update --restart=no $(docker ps -aq --filter name=cvat_)`. Para volver a prenderlo cuando se necesite: `docker start $(docker ps -aq --filter name=cvat_)`.

**4. Revisar la IP**, porque puede haber cambiado:

```powershell
ipconfig     # "Dirección IPv4" del adaptador Wi-Fi o Ethernet
```

Si cambió, avisar la nueva IP a quien usa el `curl` o la página `/docs`.

**5. Verificar:**

```powershell
curl.exe -s http://localhost:8889/health            # {"status":"ok",...}
curl.exe -s -o NUL -w "%{http_code}" http://localhost:8889/docs   # 200
```

Desde otro equipo o el celular en la misma red: abrir `http://<IP>:8889/health`.

**6. Prueba rápida opcional** (Git Bash, en la carpeta del proyecto):

```bash
KEY=$(grep '^INGEST_API_KEYS=' .env | tail -1 | cut -d= -f2- | cut -d, -f1)
curl -s -X POST http://localhost:8889/batches -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"source":"/dicom/mirai_demo_data"}'; echo     # -> {"batch_id": N}
curl -s -H "X-API-Key: $KEY" http://localhost:8889/batches/N; echo   # en ~1 min: "DONE":1
```

## En el servidor Linux

Docker arranca con el sistema (`systemctl enable docker`) y los servicios tienen `restart: unless-stopped`, así que normalmente no hay que hacer nada. Verificar:

```bash
cd ~/MIRAI-ANALISI           # carpeta donde se clonó el proyecto
docker compose ps            # todos "Up"; si alguno está detenido: docker compose up -d
hostname -I                  # confirmar que la IP no cambió
curl -s http://localhost:8889/health; echo
```

Si Docker no arrancó: `sudo systemctl start docker` y luego `docker compose up -d`.

## Antes de apagar (opcional)

No es obligatorio detener nada: los servicios se recuperan solos. Si se quiere apagar ordenadamente:

```bash
docker compose stop    # detiene sin borrar datos; después del reinicio hay que ejecutar docker compose up -d
```

No usar `docker compose down -v`: la opción `-v` borra los volúmenes (lotes, resultados y base de MySQL).

## Si algo falla

| Síntoma | Solución |
|---|---|
| `error during connect` o `Cannot connect to the Docker daemon` | Docker no está corriendo: abrir Docker Desktop (Windows) o `sudo systemctl start docker` (Linux). |
| `required variable ... is missing a value` | El comando se ejecutó fuera de la carpeta del proyecto (no encuentra `.env`): hacer `cd` a la carpeta. |
| MIRAI se cae (`OOMKilled=true`) o los estudios quedan en `FAILED` | Falta memoria: paso 3. |
| Otro equipo ya no llega | La IP cambió (paso 4) o el equipo está en otra red. |
| `401` en Swagger | La clave se rotó: **Authorize → Logout** y cargar la nueva. |

Guía completa de instalación: [despliegue-linux.md](despliegue-linux.md). Referencia general: [README](../README.md).
