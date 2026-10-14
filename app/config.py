"""Where the app finds its database.

Locally and in tests that is DATABASE_URL (SQLite). On EC2 it is the RDS endpoint
in DB_HOST plus a username and password. The password is never put on the
instance: RDS keeps it in Secrets Manager and the instance role may read it, so
DB_SECRET_ARN is all that is stored in /etc/webapp/env.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Mapping, Optional

from sqlalchemy.engine import URL, make_url


def _secrets_client(region: Optional[str]):
    import boto3  # imported lazily so local runs and tests do not need AWS

    return boto3.client("secretsmanager", region_name=region)


def database_url(
    env: Mapping[str, str] = os.environ,
    secrets_client: Optional[Callable[[Optional[str]], object]] = None,
) -> URL:
    explicit = env.get("DATABASE_URL")
    if explicit:
        return make_url(explicit)

    host = env.get("DB_HOST")
    if not host:
        raise RuntimeError("Set DATABASE_URL, or DB_HOST with DB_SECRET_ARN (or DB_USER and DB_PASSWORD)")

    secret_arn = env.get("DB_SECRET_ARN")
    if secret_arn:
        client = (secrets_client or _secrets_client)(env.get("AWS_REGION"))
        secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
        user, password = secret["username"], secret["password"]
    else:
        user, password = env["DB_USER"], env["DB_PASSWORD"]

    return URL.create(
        "mysql+pymysql",
        username=user,
        password=password,  # URL.create escapes it, so symbols in the generated password are safe
        host=host,
        port=int(env.get("DB_PORT", "3306")),
        database=env.get("DB_NAME", "webapp"),
        query={"charset": "utf8mb4"},
    )
