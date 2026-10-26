# ANPA Comedor

Aplicación web para la gestión del comedor escolar de ANPA.

## Estructura del proyecto

```text
.
├── src/
├── templates/
├── tests/
├── data/                  # persistencia local para SQLite
├── deploy/
│   └── docker/
│       ├── Dockerfile
│       └── docker-compose.yml
├── .env.example
├── .gitignore
├── docker-compose.yml     # compatibilidad local / versión rápida
├── requirements.txt
├── main.py
└── README.md
```

La idea es separar:
- `src/`: código de la aplicación
- `templates/`: plantillas HTML
- `tests/`: pruebas
- `data/`: base de datos SQLite persistente local
- `deploy/docker/`: assets de despliegue, Docker y Compose

## Variables de entorno

Copia `.env.example` a `.env` y ajusta los valores:

```bash
cp .env.example .env
```

Ejemplo mínimo:

```dotenv
APP_SECRET=tu-clave-muy-segura
COOKIE_SECURE=true
APP_VERSION=latest
ANPA_IMAGE=ghcr.io/tu-usuario/anpa
```

## Ejecutar localmente

```bash
docker compose up -d --build
```

Abrir la app en:

```text
http://localhost:8000/login
```

Parar:

```bash
docker compose down
```

## Persistencia de datos

La base de datos SQLite se guarda en un volumen persistente `anpa_data` dentro del contenedor en `/data/comedor.db`.

En local, la carpeta `data/` se usa como punto de almacenamiento del host para evitar que la DB quede mezclada con el código fuente.

## Versionado y despliegue con Docker Compose

El proyecto está preparado para trabajar con versiones de imagen y despliegues reproducibles.

### Opción 1: usar la última imagen publicada

```bash
APP_VERSION=latest docker compose up -d
```

### Opción 2: usar una versión concreta

```bash
APP_VERSION=1.2.0 docker compose up -d
```

### Opción 3: usar una imagen de GHCR

```bash
ANPA_IMAGE=ghcr.io/tu-usuario/anpa APP_VERSION=1.2.0 docker compose up -d
```

## Publicación automática de versiones

El workflow de GitHub Actions en `.github/workflows/docker-release.yml` construye y publica imágenes a GHCR cuando:
- haces push a `main`, o
- creas una etiqueta tipo `v1.2.0`

Esto permite que el compose use `latest` o una etiqueta concreta según quieras.

## Portainer / NAS / servidor remoto

En Portainer o en un servidor se puede desplegar con `docker compose` usando la imagen del registro y configurando las variables de entorno del `.env`.

Si usas GitHub Container Registry (GHCR), el acceso dependerá de si el repositorio y el paquete son públicos o privados.

## Recomendaciones

- Nunca subas secretos al repositorio.
- Mantén `.env` local y fuera de Git.
- Haz backups del volumen `/data` o de la carpeta `data/`.
- Revisa el changelog y etiqueta cada versión antes de publicarla.
