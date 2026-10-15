"""The app against a real MySQL server, the way it meets RDS.

Skipped unless these point at scratch servers where the account may create databases and users:

    WEBAPP_MYSQL_ADMIN_URL=mysql+pymysql://root:@127.0.0.1:3306/        a plain server
    WEBAPP_MYSQL_TLS_URL=mysql+pymysql://root:@127.0.0.1:3307/           one started with require_secure_transport=ON
    WEBAPP_MYSQL_TLS_CA=/path/ca.pem                                     the CA that signed that server's certificate
    WEBAPP_MYSQL_WRONG_CA=/path/other-ca.pem                             any other CA, to prove verification is real

The tests create a database called webapp_test and users called webapp_*, and drop them afterwards.
"""
import os
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import config, db
from app.main import create_app

ADMIN_URL = os.environ.get("WEBAPP_MYSQL_ADMIN_URL")
TLS_URL = os.environ.get("WEBAPP_MYSQL_TLS_URL")
TLS_CA = os.environ.get("WEBAPP_MYSQL_TLS_CA")
WRONG_CA = os.environ.get("WEBAPP_MYSQL_WRONG_CA")
DATABASE = "webapp_test"
SPECIAL_PASSWORD = "p@ss/w:rd#1%&"  # the kind of password RDS generates: URL-special characters included

needs_server = pytest.mark.skipif(not ADMIN_URL, reason="set WEBAPP_MYSQL_ADMIN_URL to run the MySQL tests")
needs_tls_server = pytest.mark.skipif(not (TLS_URL and TLS_CA and WRONG_CA), reason="set WEBAPP_MYSQL_TLS_URL, _TLS_CA and _WRONG_CA")


def provision(admin_url, ssl_ca=None):
    """Create the database and two users on a server; returns a function that undoes it."""
    connect_args = {"ssl": {"ca": ssl_ca}} if ssl_ca else {}
    admin = create_engine(admin_url, connect_args=connect_args)
    statements = [
        f"DROP DATABASE IF EXISTS {DATABASE}", "DROP USER IF EXISTS 'webapp_app'@'localhost'", "DROP USER IF EXISTS 'webapp_special'@'localhost'",
        f"CREATE DATABASE {DATABASE} CHARACTER SET utf8mb4",
        "CREATE USER 'webapp_app'@'localhost' IDENTIFIED BY 'app-password'",
        f"CREATE USER 'webapp_special'@'localhost' IDENTIFIED BY '{SPECIAL_PASSWORD.replace('%', '%%')}'",
        f"GRANT ALL ON {DATABASE}.* TO 'webapp_app'@'localhost'", f"GRANT ALL ON {DATABASE}.* TO 'webapp_special'@'localhost'",
    ]
    with admin.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)

    def undo():
        with admin.begin() as conn:
            for statement in statements[:3]:
                conn.exec_driver_sql(statement)
        admin.dispose()

    return undo


def app_url(base_url, user="webapp_app", password="app-password"):
    return make_url(base_url).set(username=user, password=password, database=DATABASE, query={"charset": "utf8mb4"})


@pytest.fixture(scope="module")
def server():
    if not ADMIN_URL:
        pytest.skip("no MySQL server configured")
    undo = provision(ADMIN_URL)
    yield ADMIN_URL
    undo()


@pytest.fixture()
def engine(server):
    engine = db.make_engine(app_url(server))
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS notes")
    yield engine
    engine.dispose()


# --- schema ------------------------------------------------------------------------------------------

@needs_server
def test_the_schema_is_created_with_the_expected_mysql_types(engine):
    db.init_schema(engine)

    with engine.connect() as conn:
        columns = {r[0]: r for r in conn.exec_driver_sql(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{DATABASE}' AND TABLE_NAME = 'notes'")}
    assert columns["id"][1].lower().startswith("int") and "auto_increment" in columns["id"][4].lower()
    assert columns["author"][1].lower() == "varchar(60)" and columns["body"][1].lower() == "varchar(500)"
    assert columns["created_at"][1].lower() == "datetime" and columns["created_at"][3] is not None  # the server fills it in


@needs_server
def test_creating_the_schema_twice_is_harmless(engine):
    db.init_schema(engine)
    db.init_schema(engine)

    assert db.count_notes(engine) == 0


@needs_server
def test_instances_starting_at_the_same_moment_do_not_collide(server, engine):
    # two or more instances boot together under Auto Scaling and all create the table
    barrier, errors = threading.Barrier(8), []

    def start_instance():
        own = db.make_engine(app_url(server))
        try:
            barrier.wait()
            db.init_schema(own)
        except Exception as error:  # noqa: BLE001 - collecting whatever goes wrong in any thread
            errors.append(error)
        finally:
            own.dispose()

    threads = [threading.Thread(target=start_instance) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    assert db.count_notes(engine) == 0


# --- the app on MySQL ------------------------------------------------------------------------------------

@needs_server
def test_notes_can_be_created_listed_and_deleted(engine):
    db.init_schema(engine)
    client = create_app(engine).test_client()

    created = client.post("/api/notes", json={"author": "Asha", "body": "first"}).get_json()
    client.post("/api/notes", json={"author": "Ravi", "body": "second"})
    listed = client.get("/api/notes").get_json()["notes"]

    assert [n["body"] for n in listed] == ["second", "first"]
    assert created["id"] > 0 and len(created["created_at"]) == 19 and created["created_at"][4] == "-"
    assert client.delete(f"/api/notes/{created['id']}").status_code == 204
    assert [n["body"] for n in client.get("/api/notes").get_json()["notes"]] == ["second"]


@needs_server
def test_emoji_and_non_latin_text_survive_the_round_trip(engine):
    db.init_schema(engine)
    client = create_app(engine).test_client()
    body = "Deploy done \U0001F680 — 中文 नमस्ते"

    client.post("/api/notes", json={"author": "Zoë", "body": body})
    note = client.get("/api/notes").get_json()["notes"][0]

    assert note["body"] == body and note["author"] == "Zoë"


@needs_server
def test_a_note_of_exactly_the_maximum_length_is_stored(engine):
    db.init_schema(engine)
    client = create_app(engine).test_client()

    assert client.post("/api/notes", json={"author": "a" * 60, "body": "b" * 500}).status_code == 201
    assert len(client.get("/api/notes").get_json()["notes"][0]["body"]) == 500


@needs_server
def test_sql_metacharacters_are_stored_as_text_on_mysql(engine):
    db.init_schema(engine)
    client = create_app(engine).test_client()
    nasty = "x'); DROP TABLE notes; -- \\' %s %(a)s"

    client.post("/api/notes", json={"author": "mallory", "body": nasty})

    assert client.get("/api/notes").get_json()["notes"][0]["body"] == nasty
    assert db.count_notes(engine) == 1


@needs_server
def test_readiness_reports_the_real_connection_state(server, engine):
    db.init_schema(engine)
    client = create_app(engine).test_client()
    assert client.get("/health/ready").get_json()["database"] == "up"

    wrong = db.make_engine(app_url(server, password="not-the-password"))
    try:
        assert db.ping(wrong) is False
    finally:
        wrong.dispose()


# --- credentials, as RDS and Secrets Manager hand them out ------------------------------------------------

class FakeSecrets:
    def __init__(self, username, password):
        self.payload = '{"username": "%s", "password": "%s"}' % (username, password.replace("\\", "\\\\").replace('"', '\\"'))

    def get_secret_value(self, SecretId):
        return {"SecretString": self.payload}


@needs_server
def test_a_password_full_of_url_characters_works_end_to_end(server):
    base = make_url(server)
    env = {"DB_HOST": base.host, "DB_PORT": str(base.port), "DB_NAME": DATABASE, "DB_SECRET_ARN": "arn:aws:secretsmanager:x:1:secret:y"}

    url = config.database_url(env, secrets_client=lambda region: FakeSecrets("webapp_special", SPECIAL_PASSWORD))
    engine = db.make_engine(url)
    try:
        db.init_schema(engine)
        assert db.ping(engine) and url.password == SPECIAL_PASSWORD
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS notes")
        engine.dispose()


@needs_server
def test_the_default_mysql_login_plugin_works_with_pymysql(server):
    # MySQL 8 and later authenticate with caching_sha2_password, which PyMySQL needs `cryptography` for
    admin = create_engine(server)
    try:
        with admin.connect() as conn:
            plugin = conn.exec_driver_sql("SELECT plugin FROM mysql.user WHERE user = 'webapp_app'").scalar_one()
    finally:
        admin.dispose()
    app = db.make_engine(app_url(server))
    try:
        assert plugin == "caching_sha2_password" and db.ping(app)  # and the app user really logs in with it
    finally:
        app.dispose()


# --- TLS, as the RDS parameter group requires it ------------------------------------------------------------

@pytest.fixture(scope="module")
def tls_server():
    if not (TLS_URL and TLS_CA and WRONG_CA):
        pytest.skip("no TLS-only MySQL server configured")
    undo = provision(TLS_URL, ssl_ca=TLS_CA)
    yield TLS_URL
    undo()


@needs_tls_server
def test_the_app_connects_over_tls_when_given_the_ca(tls_server):
    engine = db.make_engine(app_url(tls_server), ssl_ca=TLS_CA)
    try:
        db.init_schema(engine)
        client = create_app(engine).test_client()
        assert client.post("/api/notes", json={"author": "Asha", "body": "over tls"}).status_code == 201
        with engine.connect() as conn:
            cipher = conn.exec_driver_sql("SHOW SESSION STATUS LIKE 'Ssl_cipher'").one()[1]
        assert cipher.startswith("TLS") or "AES" in cipher or "CHACHA" in cipher
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS notes")
        engine.dispose()


@needs_tls_server
def test_a_plain_connection_is_refused_by_the_server(tls_server):
    engine = db.make_engine(app_url(tls_server))  # no ssl_ca: the app would connect without TLS

    try:
        assert db.ping(engine) is False
        with pytest.raises(Exception, match="(?i)insecure transport|secure"):
            engine.connect()
    finally:
        engine.dispose()


@needs_tls_server
def test_a_certificate_from_the_wrong_ca_is_rejected(tls_server):
    engine = db.make_engine(app_url(tls_server), ssl_ca=WRONG_CA)

    try:
        assert db.ping(engine) is False
        with pytest.raises(Exception, match="(?i)certificate|verify"):
            engine.connect()
    finally:
        engine.dispose()
