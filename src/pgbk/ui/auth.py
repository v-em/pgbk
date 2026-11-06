import hmac
import secrets
import time
from pathlib import Path

import requests

from .common import Problem, sha


class Vault:
    """No credential cache. Every authorization reads the current KV v2 version."""
    def __init__(self, config):
        self.config = config

    def current(self):
        c = self.config
        try:
            # Read the projected SA token afresh to survive Kubernetes rotation.
            login = requests.post(c["address"].rstrip("/") + "/v1/auth/" + c.get("authMount", "kubernetes") + "/login",
                                  json={"role": c["role"], "jwt": Path(c.get("tokenFile", "/var/run/secrets/kubernetes.io/serviceaccount/token")).read_text()},
                                  timeout=5, verify=c.get("caFile", True), allow_redirects=False)
            login.raise_for_status()
            response = requests.get(c["address"].rstrip("/") + "/v1/" + c["secretPath"],
                                    headers={"X-Vault-Token": login.json()["auth"]["client_token"]},
                                    timeout=5, verify=c.get("caFile", True), allow_redirects=False)
            response.raise_for_status()
            data = response.json()["data"]
            credentials = data["data"]
            if not credentials["username"] or not credentials["password"] or data["metadata"].get("deletion_time"):
                raise ValueError()
            return credentials["username"], credentials["password"], data["metadata"]["version"]
        except Exception:
            raise Problem("AUTHENTICATION_UNAVAILABLE") from None


class Auth:
    def __init__(self, journal, vault, lifetime=3600):
        self.journal, self.vault, self.lifetime = journal, vault, lifetime

    def login(self, username, password, peer, request_id):
        self.journal.rate_limit(peer)
        current_user, current_password, version = self.vault.current()
        valid = hmac.compare_digest(username.encode(), current_user.encode()) & hmac.compare_digest(password.encode(), current_password.encode())
        if not valid:
            self.journal.audit("anonymous", None, "LOGIN_REJECTED", request_id)
            raise Problem("INVALID_CREDENTIALS", 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.journal.transaction() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (sha(token.encode()), current_user, version, time.time() + self.lifetime, csrf))
        self.journal.audit(current_user, None, "LOGIN_ACCEPTED", request_id)
        return token, {"subject": current_user, "role": "Administrator", "csrfToken": csrf}

    def authorize(self, token):
        if not token:
            raise Problem("SESSION_REQUIRED", 401)
        with self.journal.lock:
            row = self.journal.db.execute("SELECT * FROM sessions WHERE id=?", (sha(token.encode()),)).fetchone()
        if not row or row["expires"] <= time.time():
            raise Problem("SESSION_EXPIRED", 401)
        username, _, version = self.vault.current()
        if row["version"] != version or row["subject"] != username:
            raise Problem("SESSION_ROTATED", 401)
        return dict(row)

    def logout(self, token):
        with self.journal.transaction() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (sha(token.encode()),))
