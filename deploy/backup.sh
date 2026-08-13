#!/usr/bin/env bash
# Backup diario de shade: dump logico de la BD, retencion local y copia
# off-site en R2. La BD es el unico activo con estado: la api se reconstruye
# desde la imagen y no monta volumenes propios (comprobado el 2026-08-13).
#
# Retencion: 7 diarios + 4 semanales (el semanal se puebla los domingos). La
# retencion remota la gobierna la lifecycle rule del bucket.
#
# Sin cifrar, a diferencia del backup de ductual: shade guarda geometrias y
# resultados derivados de LiDAR publico, no datos personales. Si algun dia
# guarda algo sensible, el patron a copiar es el `gpg --symmetric
# --passphrase-file` de /opt/ductual/ops/staging/backup.sh.
#
# Config por entorno (todo con defaults de produccion):
#   BACKUP_DIR    destino, FUERA del checkout (git reset --hard no lo toca)
#   ENV_FILE      .env de runtime del que se leen POSTGRES_USER/POSTGRES_DB
#   DB_CONTAINER  contenedor de Postgres
#   R2_ENV_FILE   credenciales del bucket de backups
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/shade}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
BACKUP_DIR="${BACKUP_DIR:-/srv/shade-backups}"
DB_CONTAINER="${DB_CONTAINER:-shade-db-1}"
# Las credenciales R2 son del host, compartidas con los demas backups. Viven
# bajo /etc/ductual por razones historicas: ductual fue el primero en tenerlas.
R2_ENV_FILE="${R2_ENV_FILE:-/etc/ductual/backup.env}"
R2_PREFIX="${R2_PREFIX:-shade}"

log() { echo "[backup $(date -Iseconds)] $*"; }
die() { log "ERROR: $*"; exit 1; }

# El usuario y la base salen del .env de runtime (nunca viven en el repo). Se
# pueden forzar por entorno para probar contra una BD de desarrollo.
read_env() {
  local key="$1" val
  [ -f "$ENV_FILE" ] || return 0
  val="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
  val="${val%\"}"; val="${val#\"}"
  printf '%s' "$val"
}
DB_USER="${DB_USER:-$(read_env POSTGRES_USER)}"
DB_NAME="${DB_NAME:-$(read_env POSTGRES_DB)}"
[ -n "$DB_USER" ] || die "POSTGRES_USER no resuelto (revisa $ENV_FILE)"
[ -n "$DB_NAME" ] || die "POSTGRES_DB no resuelto (revisa $ENV_FILE)"

# Compresor: zstd si esta (mejor ratio), gzip como fallback universal.
if command -v zstd >/dev/null 2>&1; then
  COMPRESS=(zstd -q -T0); EXT="zst"
else
  COMPRESS=(gzip); EXT="gz"
fi

STAMP="$(date +%Y-%m-%d)"
DAILY="$BACKUP_DIR/daily"
WEEKLY="$BACKUP_DIR/weekly"
mkdir -p "$DAILY" "$WEEKLY"

docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || die "contenedor $DB_CONTAINER no existe"

# Dump logico. --no-owner/--no-acl: el arbol tiene un solo rol de app, asi el
# restore es portable a cualquier destino. --clean/--if-exists: restaurable
# sobre una BD ya poblada de forma idempotente.
db_out="$DAILY/shade-${STAMP}.sql.${EXT}"
log "pg_dump $DB_NAME -> $db_out"
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --no-acl --clean --if-exists \
  | "${COMPRESS[@]}" > "$db_out"
[ -s "$db_out" ] || die "el dump quedo vacio: $db_out"

# Off-site a R2 (S3-compatible). Es la unica parte que protege de perder el
# VPS entero: una copia en /srv vive en el mismo disco que la BD.
if [ -f "$R2_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$R2_ENV_FILE"
  set +a
  if [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_BUCKET_NAME:-}" ]; then
    log "off-site -> R2:${R2_BUCKET_NAME}/${R2_PREFIX}/daily/$(basename "$db_out")"
    docker run --rm -v "$BACKUP_DIR:/data:ro" \
      -e RCLONE_CONFIG_R2_TYPE=s3 \
      -e RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
      -e RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
      -e RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
      -e RCLONE_CONFIG_R2_ENDPOINT="$R2_ENDPOINT_URL" \
      rclone/rclone copyto --s3-no-check-bucket \
      "/data/daily/$(basename "$db_out")" \
      "R2:${R2_BUCKET_NAME}/${R2_PREFIX}/daily/$(basename "$db_out")" \
      || die "subida a R2 fallida"
  else
    log "WARN: $R2_ENV_FILE sin R2_ACCESS_KEY_ID/R2_BUCKET_NAME; omito off-site"
  fi
else
  log "WARN: no existe $R2_ENV_FILE; backup solo local (sin off-site)"
fi

# Semanal: los domingos, promociona el diario de hoy a weekly/.
if [ "$(date +%u)" = "7" ]; then
  log "domingo: promociono a weekly/"
  cp -f "$db_out" "$WEEKLY/"
fi

# Retencion por antiguedad: 7 dias en daily, 28 en weekly.
find "$DAILY" -type f -name 'shade-*' -mtime +7 -delete
find "$WEEKLY" -type f -name 'shade-*' -mtime +28 -delete

log "backup ok: $(du -sh "$BACKUP_DIR" | cut -f1) en $BACKUP_DIR"
