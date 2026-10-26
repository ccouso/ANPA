# ANPA Comedor - Despliegue con Docker Compose

Este proyecto está preparado para desplegarse en local o en un NAS con Portainer.

## 1) Requisitos

- Docker y Docker Compose
- Un archivo `.env` con al menos:
  - `APP_SECRET=<secreto-largo-y-unico>`
  - `COOKIE_SECURE=true` (recomendado en producción)

## 2) Ejecutar en local

```bash
docker compose up -d --build
```

Abrir: `http://localhost:8000/login`

Parar:

```bash
docker compose down
```

## 3) Persistencia de datos

El `docker-compose.yml` usa un volumen persistente `anpa_data` para SQLite:

- Base de datos en contenedor: `/data/comedor.db`
- `DATABASE_URL` se configura automáticamente como `sqlite:////data/comedor.db`

## 4) Despliegue en NAS + Portainer (recomendado)

### Opción habitual (Stack desde Git)

1. Sube este proyecto a un repositorio GitHub.
2. En Portainer: **Stacks > Add stack > Repository**.
3. Indica:
   - URL del repo
   - Rama (`main` o la que uses)
   - Ruta compose: `docker-compose.yml`
4. Configura variables de entorno seguras (`APP_SECRET`, etc.).
5. Deploy.

### Actualizar con cambios de GitHub

- En Portainer: **Pull and redeploy** del stack, o
- Configura un **webhook** de Portainer y llámalo desde tu flujo CI/CD tras cada push a `main`.

Ese flujo te permite mantener la app con un update sencillo sin tocar manualmente el servidor.

## 5) Notas de producción

- Usa un `APP_SECRET` robusto y privado.
- Publica detrás de un proxy reverso con HTTPS (Nginx/Traefik/Caddy) para que `COOKIE_SECURE=true` funcione correctamente.
- Haz backups periódicos del volumen `anpa_data`.
