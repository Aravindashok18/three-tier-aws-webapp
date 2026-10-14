"""Create the tables, waiting for the database if needed. systemd runs this before gunicorn starts."""
import os

from app import config, db

if __name__ == "__main__":
    engine = db.make_engine(config.database_url(), os.environ.get("DB_SSL_CA"))
    db.init_schema(engine, attempts=int(os.environ.get("DB_INIT_ATTEMPTS", "20")))
    print("schema ready")
