import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .errors import SyncError


def _as_bool(value, key):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False
    raise SyncError("{} must be true or false".format(key))


AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
BASE_SCOPES = (
    "playlist-read-private",
    "playlist-modify-private",
    "ugc-image-upload",
)


class _CallbackHandler(BaseHTTPRequestHandler):
    callback_state = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        values = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        state = (values.get("state") or [""])[0]
        if state != self.callback_state["expected_state"]:
            self.callback_state["result"] = {"error": "OAuth state did not match"}
        elif "error" in values:
            self.callback_state["result"] = {"error": values["error"][0]}
        else:
            self.callback_state["result"] = {"code": (values.get("code") or [""])[0]}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Spotify authorization received.</h2>"
            b"<p>You can close this window.</p></body></html>"
        )
        self.callback_state["event"].set()

    def log_message(self, fmt, *args):
        return


class SpotifyTokenManager:
    def __init__(
        self,
        config_dir=None,
        redirect_uri="http://127.0.0.1:8888/callback",
        public_playlists=False,
        opener=webbrowser.open,
    ):
        self.config_dir = Path(
            config_dir or "~/.config/discogs2spotify"
        ).expanduser()
        self.client_id_path = self.config_dir / "spotify-client-id"
        self.token_path = self.config_dir / "spotify-token"
        self.redirect_uri = redirect_uri
        self.public_playlists = _as_bool(public_playlists, "public_playlists")
        self.opener = opener

    def _client_id(self):
        try:
            client_id = self.client_id_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise SyncError(
                "Create {} with your Spotify app client ID".format(self.client_id_path)
            ) from error
        if not client_id:
            raise SyncError("Spotify client ID file is empty: {}".format(self.client_id_path))
        return client_id

    def _read_token(self):
        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise SyncError("Cannot read Spotify token file: {}".format(error)) from error
        if not isinstance(token, dict):
            raise SyncError("Spotify token file is not a JSON object")
        return token

    def _save_token(self, token):
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".spotify-token.", dir=str(self.config_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(token, file)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.token_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _post_token(self, values):
        body = urllib.parse.urlencode(values).encode("ascii")
        request = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise SyncError(
                "Spotify OAuth token request failed (HTTP {})".format(error.code)
            )
        except urllib.error.URLError as error:
            raise SyncError("Spotify OAuth token request failed: {}".format(error.reason))
        except (UnicodeDecodeError, ValueError) as error:
            raise SyncError("Spotify OAuth returned invalid token data: {}".format(error))
        if not result.get("access_token"):
            raise SyncError("Spotify OAuth response did not contain an access token")
        return result

    def _scopes(self):
        scopes = list(BASE_SCOPES)
        if self.public_playlists:
            scopes.append("playlist-modify-public")
        return scopes

    def _check_scopes(self, token):
        granted = token.get("scope")
        if granted is None:
            return
        missing = set(self._scopes()) - set(str(granted).split())
        if missing:
            raise SyncError(
                "Spotify token is missing required scopes: {}. Run --auth to grant them.".format(
                    ", ".join(sorted(missing))
                )
            )

    def _authorize(self):
        client_id = self._client_id()
        redirect = urllib.parse.urlparse(self.redirect_uri)
        if redirect.hostname not in ("127.0.0.1", "localhost") or not redirect.port:
            raise SyncError("OAuth redirect URI must use localhost and an explicit port")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self._scopes()),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
        callback_state = {"result": None, "event": threading.Event(), "expected_state": state}
        handler = type(
            "CallbackHandler",
            (_CallbackHandler,),
            {"callback_state": callback_state},
        )
        try:
            server = HTTPServer((redirect.hostname, redirect.port), handler)
        except OSError as error:
            raise SyncError(
                "Cannot start Spotify OAuth callback at {}: {}".format(
                    self.redirect_uri, error
                )
            )
        server.timeout = 1
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                opened = self.opener(url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                print("Open this Spotify authorization URL in a browser:\n{}".format(url))
            if not callback_state["event"].wait(180):
                raise SyncError("Timed out waiting for Spotify authorization callback")
            result = callback_state["result"] or {}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        if result.get("error"):
            raise SyncError("Spotify authorization failed: {}".format(result["error"]))
        values = self._post_token(
            {
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": result.get("code", ""),
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            }
        )
        values["expires_at"] = int(time.time()) + int(values.get("expires_in", 3600))
        self._save_token(values)
        return values

    def _refresh(self, token):
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            return self._authorize()
        values = self._post_token(
            {
                "client_id": self._client_id(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        values["refresh_token"] = values.get("refresh_token", refresh_token)
        values["scope"] = values.get("scope", token.get("scope", ""))
        values["expires_at"] = int(time.time()) + int(values.get("expires_in", 3600))
        self._save_token(values)
        return values

    def access_token(self, force_auth=False):
        token = None if force_auth else self._read_token()
        if token is None:
            token = self._authorize()
        elif float(token.get("expires_at", 0)) <= time.time() + 60:
            token = self._refresh(token)
        self._check_scopes(token)
        return token["access_token"]
