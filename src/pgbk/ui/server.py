"""Same-origin HTTPS server; intentionally no browser-side cloud credentials."""
import argparse
import hmac
import json
import re
import secrets
import ssl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlsplit

from .adapters import Cluster, catalogs, validate_config
from .admission import validate
from .auth import Auth, Vault
from .common import Problem, encode, utcnow
from .index import MetadataIndex
from .journal import Journal
from .service import Service


def handler(service, auth):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pgbk-ui"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass  # Never log URLs, request bodies, headers or credentials.

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise Problem("INVALID_BODY", 400)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 1024 * 1024:
                    raise ValueError()
                raw = self.rfile.read(size)
                if raw and self.headers.get_content_type() != "application/json":
                    raise ValueError()
                value = json.loads(raw) if raw else {}
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, UnicodeDecodeError):
                raise Problem("INVALID_BODY", 400) from None

        def reply(self, status, value, content_type="application/json; charset=utf-8", cookie=None):
            data = value if isinstance(value, bytes) else encode(value)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.send_header("X-Request-ID", self.request_id)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_DELETE(self):
            self.dispatch("DELETE")

        def dispatch(self, method):
            self.request_id = secrets.token_hex(16)
            session = None
            try:
                parsed = urlsplit(self.path)
                path = unquote(parsed.path)
                if path == "/healthz" and method == "GET":
                    return self.reply(200, {"status": "ok"})
                if path == "/admission/jobs" and method == "POST":
                    return self.reply(200, validate(service, self.body()))
                if method == "GET" and not path.startswith("/api/"):
                    asset = {"/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}.get(path, ("index.html", "text/html"))
                    return self.reply(200, files("pgbk.ui").joinpath("static", asset[0]).read_bytes(), asset[1] + "; charset=utf-8")
                # Every write, including login, must originate on the configured HTTPS origin.
                if method != "GET" and self.headers.get("Origin") != service.config["origin"]:
                    raise Problem("ORIGIN_DENIED", 403)
                if path == "/api/v1/session" and method == "POST":
                    body = self.body()
                    if set(body) != {"username", "password"} or not all(isinstance(v, str) and len(v) <= 4096 for v in body.values()):
                        raise Problem("INVALID_BODY", 400)
                    token, result = auth.login(body["username"], body["password"], self.client_address[0], self.request_id)
                    return self.reply(201, result, cookie=f"__Host-pgbk={token}; Path=/; Max-Age={auth.lifetime}; Secure; HttpOnly; SameSite=Strict")
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie["__Host-pgbk"].value if "__Host-pgbk" in cookie else None
                session = auth.authorize(token)
                subject = session["subject"]
                if method != "GET" and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                    raise Problem("CSRF_DENIED", 403)
                if path == "/api/v1/session":
                    if method == "GET":
                        return self.reply(200, {"subject": subject, "role": "Administrator", "csrfToken": session["csrf"], "installationId": service.config["installationId"]})
                    if method == "DELETE":
                        auth.logout(token)
                        service.journal.audit(subject, None, "LOGOUT", self.request_id)
                        return self.reply(200, {"status": "logged-out"}, cookie="__Host-pgbk=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
                query_lists = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(v) != 1 for v in query_lists.values()):
                    raise Problem("INVALID_QUERY", 400)
                query = {k: v[0] for k, v in query_lists.items()}
                if method == "GET":
                    if path in ("/api/v1/profiles", "/api/v1/schedules"):
                        schedules = service.schedules()
                        errors = [{"profileId": item["profileId"], "reasonCode": item["reasonCode"]} for item in schedules if item.get("reasonCode")]
                        return self.reply(200, service.page(schedules, query, subject, path, {"partialErrors": errors, "partial": bool(errors)}))
                    if path == "/api/v1/backups":
                        return self.reply(200, service.backups(query, subject))
                    if path == "/api/v1/jobs":
                        return self.reply(200, service.jobs(query, subject))
                    match = re.fullmatch(r"/api/v1/jobs/([^/]+)/([^/]+)", path)
                    if match:
                        return self.reply(200, service.job(*match.groups(), subject))
                    match = re.fullmatch(r"/api/v1/profiles/([^/]+)/launch-token", path)
                    if match:
                        return self.reply(200, service.issue(match[1], subject))
                    match = re.fullmatch(r"/api/v1/profiles/([^/]+)/sources/([^/]+)/backups/([^/]+)(?:/recovery/(en|ru))?", path)
                    if match:
                        profile, source, run, language = match.groups()
                        service.profile(profile, source)
                        return self.reply(200, service.index.recovery(profile, run, language) if language else service.detail(profile, source, run))
                match = re.fullmatch(r"/api/v1/profiles/([^/]+)/backups", path)
                if match and method == "POST":
                    if self.body() or query:
                        raise Problem("EXTRA_FIELDS", 400)
                    status, result = service.launch(match[1], self.headers.get("Idempotency-Key"), subject, self.request_id)
                    return self.reply(status, result)
                raise Problem("NOT_FOUND", 404)
            except Problem as error:
                if method != "GET":
                    try:
                        service.journal.audit(session["subject"] if session else "anonymous", None, error.code, self.request_id)
                    except Problem:
                        pass
                self.reply(error.status, {"reasonCode": error.code, "requestId": self.request_id})
            except Exception:
                self.reply(503, {"reasonCode": "DEPENDENCY_UNAVAILABLE", "requestId": self.request_id})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/ui.json")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    with open(args.config) as stream:
        config = validate_config(json.load(stream))
    journal = Journal(config.get("journal", "/data/ui.sqlite"))
    index = MetadataIndex(catalogs(config), journal)
    service = Service(config, Cluster(config), index, journal)
    auth = Auth(journal, Vault(config["vault"]), config.get("sessionSeconds", 3600))
    server = ThreadingHTTPServer((args.listen, args.port), handler(service, auth))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config["tls"]["certificate"], config["tls"]["key"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    index.start(config.get("refreshSeconds", 30))
    try:
        server.serve_forever()
    finally:
        index.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
