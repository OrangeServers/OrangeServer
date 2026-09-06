"""Serve the bundled SPA from every WSGI startup mode."""
from pathlib import Path

from flask import abort, send_from_directory


STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"
API_PREFIXES = {
    "account", "ai", "apidocs", "apispec_1.json", "auth", "local", "mail",
    "openapi.json", "openapi.yaml", "server",
}


def register_spa(app, *, missing_text=None):
    @app.get("/")
    def spa_index():
        if (STATIC_DIR / "index.html").is_file():
            return send_from_directory(STATIC_DIR, "index.html")
        if missing_text is not None:
            return missing_text, 200
        abort(404)

    @app.get("/<path:path>")
    def spa_asset_or_route(path):
        candidate = STATIC_DIR / path
        if candidate.is_file() and STATIC_DIR in candidate.resolve().parents:
            return send_from_directory(STATIC_DIR, path)
        if (
            path.split("/", 1)[0] in API_PREFIXES
            or path == "setup/api"
            or path.startswith("setup/api/")
        ):
            abort(404)
        if (STATIC_DIR / "index.html").is_file():
            return send_from_directory(STATIC_DIR, "index.html")
        abort(404)
