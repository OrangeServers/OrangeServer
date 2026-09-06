from pathlib import Path


def test_spa_serves_assets_and_history_without_shadowing_api(monkeypatch, tmp_path):
    from flask import Flask
    from setup import spa

    (tmp_path / "index.html").write_text("spa-index", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("app-js", encoding="utf-8")
    monkeypatch.setattr(spa, "STATIC_DIR", Path(tmp_path))
    app = Flask(__name__)
    spa.register_spa(app)
    client = app.test_client()

    assert client.get("/").data == b"spa-index"
    assert client.get("/setup").data == b"spa-index"
    assert client.get("/ai-runs/demo").data == b"spa-index"
    assert client.get("/assets/app.js").data == b"app-js"
    assert client.get("/ai/missing").status_code == 404
    assert client.get("/setup/api/missing").status_code == 404
