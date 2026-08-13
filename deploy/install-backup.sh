#!/usr/bin/env bash
# Instala en cartagena el backup diario de shade: directorio de destino,
# unidad y timer. Idempotente, se puede repetir sin efectos raros.
#
# Se ejecuta EN el VPS y necesita sudo, porque escribe en /srv y en
# /etc/systemd/system:
#
#   ssh -t cartagena 'bash /opt/shade/deploy/install-backup.sh'
#
# Requiere que el repo ya este desplegado en /opt/shade. Las unidades se
# copian a /etc en vez de enlazarse: el deploy hace `git reset --hard` y un
# enlace al checkout se quedaria a merced de esa rama.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/shade}"
BACKUP_DIR="${BACKUP_DIR:-/srv/shade-backups}"
UNIT_DIR=/etc/systemd/system
OWNER="${OWNER:-$(id -un)}"

[ -x "$APP_DIR/deploy/backup.sh" ] || { echo "falta $APP_DIR/deploy/backup.sh (despliega primero)" >&2; exit 1; }

sudo install -d -o "$OWNER" -g "$OWNER" -m 0750 "$BACKUP_DIR"
sudo install -m0644 "$APP_DIR/deploy/systemd/shade-backup.service" "$UNIT_DIR/shade-backup.service"
sudo install -m0644 "$APP_DIR/deploy/systemd/shade-backup.timer" "$UNIT_DIR/shade-backup.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now shade-backup.timer

echo "--- timer ---"
systemctl list-timers shade-backup.timer --no-pager | head -3

echo
echo "--- primera ejecucion, para comprobar dump y subida ---"
sudo systemctl start shade-backup.service || true
journalctl -q -u shade-backup.service -n 20 --no-pager -o cat
echo
echo "resultado: $(systemctl show shade-backup.service -p Result --value)"
ls -la "$BACKUP_DIR/daily" 2>/dev/null || true
