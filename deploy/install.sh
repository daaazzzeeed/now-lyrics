#!/usr/bin/env bash
# Установка/обновление на ВМ (Debian/Ubuntu). Запуск: sudo ./deploy/install.sh  из корня репозитория.
set -euo pipefail
DEST=/opt/lyrics-tv
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get install -y python3 python3-venv rsync >/dev/null
mkdir -p "$DEST"
rsync -a --delete --exclude .venv --exclude data --exclude __pycache__ "$SRC/backend/" "$DEST/backend/"
[ -d "$DEST/venv" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/backend/requirements.txt"

if [ ! -f "$DEST/.env" ]; then
  install -m 600 "$SRC/deploy/env.example" "$DEST/.env"
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sed -i "s/^ACCESS_KEY=CHANGE_ME$/ACCESS_KEY=$KEY/" "$DEST/.env"
  echo "Создан $DEST/.env — впиши ключи Spotify и Gemini, затем: systemctl restart lyrics-tv"
fi

install -m 644 "$SRC/deploy/lyrics-tv.service" /etc/systemd/system/lyrics-tv.service
systemctl daemon-reload
systemctl enable --now lyrics-tv
systemctl restart lyrics-tv
sleep 2
PORT="$(grep -E '^PORT=' "$DEST/.env" | cut -d= -f2)"
curl -fsS "http://127.0.0.1:${PORT:-8787}/health" && echo " — сервис работает"
echo "Адрес для ТВ: http://<IP ВМ>:${PORT:-8787}/?key=$(grep -E '^ACCESS_KEY=' "$DEST/.env" | cut -d= -f2)"
