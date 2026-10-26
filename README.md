# ANPA Comedor

[![Tests](https://github.com/ccouso/ANPA/actions/workflows/tests.yml/badge.svg)](https://github.com/ccouso/ANPA/actions/workflows/tests.yml)
[![Docker publish](https://github.com/ccouso/ANPA/actions/workflows/docker-release.yml/badge.svg)](https://github.com/ccouso/ANPA/actions/workflows/docker-release.yml)
[![Release](https://img.shields.io/github/v/release/ccouso/ANPA?display_name=tag)](https://github.com/ccouso/ANPA/releases)
[![Dependencies](https://img.shields.io/badge/dependencies-11-blue)](./requirements.txt)
[![Python](https://img.shields.io/badge/python-3.12-3776AB)](https://www.python.org/)

Aplicación web para la gestión del comedor escolar de ANPA.

## Métricas visibles en este README

- **Versión**: badge de release (`vX.Y.Z`) desde GitHub Releases.
- **Testeado**: badge del workflow `Tests` (pytest en cada push/PR).
- **Publicación Docker**: badge del workflow `docker-release`.
- **Dependencias**: badge con el total actual de `requirements.txt`.

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
APP_PORT=8000
APP_VERSION=latest
ANPA_IMAGE=ghcr.io/ccouso/anpa
```

## Ejecutar localmente

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

Abrir la app en:

```text
http://localhost:8000/login
```

Parar:

```bash
docker compose -f deploy/docker/docker-compose.yml down
```

## Configuración inicial de administrador

En un despliegue nuevo (base de datos vacía), la aplicación bloquea el login normal y muestra un formulario para crear el primer usuario administrador.

- Hasta crear ese usuario no se puede iniciar sesión.
- El administrador se crea con el email/contraseña que introduzcas en esa pantalla inicial.

## Persistencia de datos

La base de datos SQLite se guarda en un volumen persistente `anpa_data` dentro del contenedor en `/data/comedor.db`.

En local, la carpeta `data/` se usa como punto de almacenamiento del host para evitar que la DB quede mezclada con el código fuente.

Si arrancas la app fuera de Docker (por ejemplo con `uvicorn`), SQLite también usa por defecto `./data/comedor.db`.

## Versionado y despliegue con Docker Compose

El proyecto está preparado para trabajar con versiones de imagen y despliegues reproducibles.

### Opción 1: usar la última imagen publicada

```bash
APP_VERSION=latest docker compose -f deploy/docker/docker-compose.yml up -d
```

### Opción 2: usar una versión concreta

```bash
APP_VERSION=1.2.0 docker compose -f deploy/docker/docker-compose.yml up -d
```

### Opción 3: usar una imagen de GHCR

```bash
ANPA_IMAGE=ghcr.io/ccouso/anpa APP_VERSION=1.2.0 docker compose -f deploy/docker/docker-compose.yml up -d
```

## Publicación automática de versiones

El workflow de GitHub Actions en `.github/workflows/docker-release.yml` construye y publica imágenes a GHCR cuando:
- haces push a `main`, o
- creas una etiqueta tipo `v1.2.0`

Esto permite que el compose use `latest` o una etiqueta concreta según quieras.

## Portainer (NAS) con repositorio público

Como ahora el repositorio e imagen son públicos, puedes desplegar sin credenciales.

1. Ve a **Stacks > Add stack > Repository**.
2. Repo URL: `https://github.com/ccouso/ANPA.git`
3. Branch/Reference: `main` (o la tag/branch que quieras).
4. Compose path: `deploy/docker/docker-compose.yml`
5. En variables del stack define:
   - `APP_SECRET=...` (obligatorio)
   - `COOKIE_SECURE=false` (si accedes por HTTP local; con HTTPS usa `true`)
   - `APP_PORT=8050` (o el puerto local que quieras publicar)
   - `ANPA_IMAGE=ghcr.io/ccouso/anpa`
   - `APP_VERSION=latest` o una versión concreta como `v1.2.0`
6. Deploy.

Acceso local:

```text
http://IP_DE_TU_NAS:8050/login
```

### Cambiar de versión en Portainer

- Edita solo `APP_VERSION` y redeploy:
  - Última: `latest`
  - Fija: `v1.2.0` (o la que exista en Releases)

## Recomendaciones

- Nunca subas secretos al repositorio.
- Mantén `.env` local y fuera de Git.
- Haz backups del volumen `/data` o de la carpeta `data/`.
- Revisa el changelog y etiqueta cada versión antes de publicarla.
