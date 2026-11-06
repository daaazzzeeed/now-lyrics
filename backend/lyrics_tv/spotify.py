"""Spotify Web API: OAuth (код + refresh), опрос /me/player."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse

import httpx

from . import config as C

log = logging.getLogger("spotify")

SCOPES = "user-read-currently-playing user-read-playback-state"
SAFE_ID = re.compile(r"[A-Za-z0-9]{1,64}")


class AuthRequired(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(retry_after)
        self.retry_after = retry_after


class Spotify:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.lock = asyncio.Lock()
        self.tok = self._load()
        self.oauth_state: str | None = None

    @staticmethod
    def _load() -> dict:
        try:
            return json.loads(C.TOKEN_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        C.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not self.tok:
            C.TOKEN_FILE.unlink(missing_ok=True)
            return
        tmp = C.TOKEN_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.tok, f)
        os.chmod(tmp, 0o600)
        tmp.replace(C.TOKEN_FILE)

    @property
    def configured(self) -> bool:
        return bool(C.CLIENT_ID and C.CLIENT_SECRET)

    @property
    def logged_in(self) -> bool:
        return bool(self.tok.get("refresh_token"))

    def days_left(self) -> int | None:
        at = self.tok.get("authorized_at")
        if not at:
            return None
        return max(0, int(C.TOKEN_LIFETIME_DAYS - (time.time() - at) / 86400))

    def authorize_url(self) -> str:
        self.oauth_state = secrets.token_urlsafe(16)
        return f"{C.SPOTIFY_ACCOUNTS}/authorize?" + urllib.parse.urlencode({
            "client_id": C.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": C.REDIRECT_URI,
            "scope": SCOPES,
            "state": self.oauth_state,
        })

    async def _token_request(self, form: dict) -> tuple[int, dict]:
        basic = base64.b64encode(f"{C.CLIENT_ID}:{C.CLIENT_SECRET}".encode()).decode()
        r = await self.client.post(f"{C.SPOTIFY_ACCOUNTS}/api/token", data=form,
                                   headers={"Authorization": f"Basic {basic}"})
        try:
            js = r.json()
        except ValueError:
            js = {}
        return r.status_code, js if isinstance(js, dict) else {}

    async def finish_login(self, code: str, state: str) -> None:
        if not self.oauth_state or not hmac.compare_digest(state or "", self.oauth_state):
            raise ValueError("state не совпал — начни вход заново с /login")
        self.oauth_state = None
        st, js = await self._token_request({"grant_type": "authorization_code", "code": code,
                                            "redirect_uri": C.REDIRECT_URI})
        if st != 200 or "refresh_token" not in js:
            raise RuntimeError(f"обмен кода не удался: HTTP {st} {js.get('error', '')}")
        async with self.lock:
            self.tok = {
                "access_token": js["access_token"],
                "refresh_token": js["refresh_token"],
                "expires_at": time.time() + int(js.get("expires_in", 3600)) - 60,
                "authorized_at": time.time(),
            }
            self._save()
        log.info("вход выполнен")

    async def _access_token(self, force: bool = False) -> str:
        async with self.lock:
            if not self.tok.get("refresh_token"):
                raise AuthRequired()
            if not force and time.time() < self.tok.get("expires_at", 0):
                return self.tok["access_token"]
            st, js = await self._token_request({"grant_type": "refresh_token",
                                                "refresh_token": self.tok["refresh_token"]})
            if st == 400 and js.get("error") == "invalid_grant":
                log.warning("refresh-токен истёк или отозван — нужен повторный вход")
                self.tok = {}
                self._save()
                raise AuthRequired()
            if st != 200 or "access_token" not in js:
                raise RuntimeError(f"обновление токена: HTTP {st}")
            self.tok["access_token"] = js["access_token"]
            self.tok["expires_at"] = time.time() + int(js.get("expires_in", 3600)) - 60
            if js.get("refresh_token"):  # authorized_at не трогаем: срок жизни входа не продлевается
                self.tok["refresh_token"] = js["refresh_token"]
            self._save()
            return self.tok["access_token"]

    async def player(self) -> dict | None:
        for attempt in (0, 1):
            token = await self._access_token(force=attempt == 1)
            r = await self.client.get(f"{C.SPOTIFY_API}/me/player",
                                      params={"additional_types": "track,episode"},
                                      headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code == 204 or (r.status_code == 200 and not r.content):
                return None
            if r.status_code == 429:
                try:
                    retry = float(r.headers.get("Retry-After") or 10)
                except ValueError:
                    retry = 10.0
                raise RateLimited(retry)
            if r.status_code != 200:
                raise RuntimeError(f"/me/player → HTTP {r.status_code}")
            return r.json()
        raise AuthRequired()


def parse_item(js: dict | None) -> dict | None:
    item = (js or {}).get("item")
    if not item:
        return None
    if item.get("type") == "episode":
        show = item.get("show") or {}
        images = item.get("images") or show.get("images") or []
        artists = [show.get("publisher") or show.get("name", "")]
        album = show.get("name", "")
    else:
        alb = item.get("album") or {}
        images = alb.get("images") or []
        artists = [a.get("name", "") for a in item.get("artists") or []]
        album = alb.get("name", "")
    tid = item.get("id") or ""
    if not SAFE_ID.fullmatch(tid):  # локальные файлы приходят без id
        tid = hashlib.sha1((item.get("uri") or item.get("name", "")).encode()).hexdigest()[:22]
    # ~640 px: самая крупная не больше 640, иначе самая крупная вообще
    images = sorted((i for i in images if i.get("url")), key=lambda i: i.get("width") or 0, reverse=True)
    fit = [i for i in images if (i.get("width") or 0) <= 640] or images
    return {
        "id": tid,
        "title": item.get("name", ""),
        "artists": [a for a in artists if a],
        "album": album,
        "duration_ms": int(item.get("duration_ms") or 0),
        "images": [i["url"] for i in fit],
    }
