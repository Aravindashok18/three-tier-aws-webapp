import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app import config, db
from app.main import create_app


@pytest.fixture()
def engine():
    # one shared in-memory SQLite connection so every request sees the same database
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True)
    db.init_schema(engine)
    return engine


@pytest.fixture()
def client(engine):
    return create_app(engine).test_client()


def post_note(client, **fields):
    payload = {"author": "Asha", "body": "Deploy is at 5pm"}
    payload.update(fields)
    return client.post("/api/notes", json=payload)


# --- notes API ---------------------------------------------------------------------

def test_create_then_list_returns_newest_first(client):
    first = post_note(client, body="first")
    second = post_note(client, body="second")

    assert first.status_code == 201
    assert first.get_json()["author"] == "Asha"
    assert first.get_json()["created_at"]
    bodies = [n["body"] for n in client.get("/api/notes").get_json()["notes"]]
    assert bodies == ["second", "first"]
    assert second.get_json()["id"] > first.get_json()["id"]


def test_delete_removes_the_note(client):
    note_id = post_note(client).get_json()["id"]

    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert client.get("/api/notes").get_json()["notes"] == []
    assert client.delete(f"/api/notes/{note_id}").status_code == 404


@pytest.mark.parametrize("fields", [
    {"author": ""}, {"body": "   "}, {"author": "x" * 61}, {"body": "y" * 501},
])
def test_invalid_notes_are_rejected(client, fields):
    assert post_note(client, **fields).status_code == 400
    assert client.get("/api/notes").get_json()["notes"] == []


def test_body_that_is_not_a_json_object_is_rejected(client):
    assert client.post("/api/notes", data="hello", content_type="text/plain").status_code == 400
    assert client.post("/api/notes", json=["a", "list"]).status_code == 400


def test_sql_metacharacters_are_stored_as_plain_text(client, engine):
    nasty = "x'); DROP TABLE notes; --"
    post_note(client, body=nasty)

    assert client.get("/api/notes").get_json()["notes"][0]["body"] == nasty
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM notes")).scalar_one() == 1


def test_page_escapes_note_text(client):
    post_note(client, body="<script>alert(1)</script>")

    html = client.get("/").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_page_links_static_files_under_the_configured_base(engine, monkeypatch):
    monkeypatch.setenv("STATIC_BASE_URL", "https://assets.example.com/static/")

    html = create_app(engine).test_client().get("/").get_data(as_text=True)

    assert 'href="https://assets.example.com/static/css/site.css"' in html
    assert 'src="https://assets.example.com/static/js/app.js"' in html


# --- health ------------------------------------------------------------------------

def test_health_does_not_touch_the_database(client, monkeypatch):
    # a database outage must not make the load balancer pull healthy instances out of rotation
    def boom(*_args, **_kwargs):
        raise AssertionError("liveness must not query the database")

    monkeypatch.setattr(db, "ping", boom)
    monkeypatch.setattr(db, "list_notes", boom)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_ready_reports_database_state(client, monkeypatch):
    assert client.get("/health/ready").get_json()["database"] == "up"

    monkeypatch.setattr(db, "ping", lambda _engine: False)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.get_json()["database"] == "down"


def test_responses_say_which_instance_served_them(client):
    response = client.get("/health")

    assert response.headers["X-Served-By"] == response.get_json()["host"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# --- schema ------------------------------------------------------------------------

def test_init_schema_can_run_twice(engine):
    db.init_schema(engine)
    db.init_schema(engine)

    assert db.count_notes(engine) == 0


def test_init_schema_retries_until_the_database_answers(monkeypatch):
    calls = {"n": 0}

    class FlakyEngine:
        def begin(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("database is starting")
            return real.begin()

    real = create_engine("sqlite://", future=True)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    db.init_schema(FlakyEngine(), attempts=5)

    assert calls["n"] == 3


def test_init_schema_gives_up_after_the_last_attempt(monkeypatch):
    class DeadEngine:
        def begin(self):
            raise ConnectionError("down")

    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    with pytest.raises(ConnectionError):
        db.init_schema(DeadEngine(), attempts=2)


# --- database URL ------------------------------------------------------------------

class FakeSecrets:
    def __init__(self, secret):
        self.secret, self.asked = secret, []

    def get_secret_value(self, SecretId):
        self.asked.append(SecretId)
        return {"SecretString": self.secret}


def test_database_url_prefers_an_explicit_url():
    url = config.database_url({"DATABASE_URL": "sqlite:///local.db", "DB_HOST": "ignored"})

    assert url.drivername == "sqlite"


def test_database_url_reads_credentials_from_secrets_manager():
    fake = FakeSecrets('{"username": "webapp_admin", "password": "p@ss/w:rd#1"}')
    env = {"DB_HOST": "db.internal", "DB_SECRET_ARN": "arn:aws:secretsmanager:eu-west-1:1:secret:x", "AWS_REGION": "eu-west-1"}

    url = config.database_url(env, secrets_client=lambda region: fake)

    assert fake.asked == [env["DB_SECRET_ARN"]]
    assert (url.host, url.port, url.database, url.username) == ("db.internal", 3306, "webapp", "webapp_admin")
    assert url.password == "p@ss/w:rd#1"
    # the rendered URL must survive a round trip even though the password has URL-special characters
    assert config.make_url(url.render_as_string(hide_password=False)).password == "p@ss/w:rd#1"


def test_database_url_without_a_secret_uses_plain_variables():
    url = config.database_url({"DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "pw", "DB_NAME": "n", "DB_PORT": "3307"})

    assert (url.username, url.password, url.database, url.port) == ("u", "pw", "n", 3307)
    assert url.drivername == "mysql+pymysql"


def test_database_url_with_nothing_configured_explains_what_to_set():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        config.database_url({})


# --- engine ------------------------------------------------------------------------

def test_mysql_engine_verifies_the_rds_certificate(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "create_engine", lambda url, **kwargs: seen.update(kwargs))

    db.make_engine(config.make_url("mysql+pymysql://u:p@db.internal/webapp"), ssl_ca="/etc/webapp/rds-ca.pem")

    assert seen["connect_args"] == {"ssl": {"ca": "/etc/webapp/rds-ca.pem"}}
    assert seen["pool_pre_ping"] is True


def test_sqlite_engine_gets_no_ssl_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "create_engine", lambda url, **kwargs: seen.update(kwargs))

    db.make_engine(config.make_url("sqlite://"), ssl_ca="/ignored.pem")

    assert seen["connect_args"] == {}
