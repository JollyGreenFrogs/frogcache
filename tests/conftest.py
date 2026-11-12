"""Test fixtures: a real Postgres for DB tests (CI provides DATABASE_URL; locally we boot one)."""

import os
import socket
import subprocess
import time

import pytest

PG_BIN = "/usr/lib/postgresql/16/bin"


def _free_port() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def _pg_is_ready(port: str, env: dict) -> bool:
    return subprocess.run(
        [f"{PG_BIN}/pg_isready", "-h", "127.0.0.1", "-p", port],
        capture_output=True, env=env,
    ).returncode == 0


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory):
    """Session-scoped Postgres DSN: env DATABASE_URL if set (CI), else a local cluster."""
    if os.environ.get("DATABASE_URL"):
        yield os.environ["DATABASE_URL"]
        return
    base = tmp_path_factory.mktemp("pg")
    port = _free_port()
    # drop LD_LIBRARY_PATH: the bundled libstdc++ breaks the system icu for PG binaries
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    subprocess.run(
        [f"{PG_BIN}/initdb", "-D", str(base / "data"), "-A", "trust"],
        check=True, capture_output=True, env=env,
    )
    subprocess.run(
        [f"{PG_BIN}/pg_ctl", "-D", str(base / "data"), "-o", f"-p {port} -k {base}",
         "-l", str(base / "log"), "-w", "start"],
        check=True, capture_output=True, env=env,
    )
    for _ in range(50):
        if _pg_is_ready(port, env):
            break
        time.sleep(0.2)
    subprocess.run(
        [f"{PG_BIN}/psql", "-h", "127.0.0.1", "-p", port, "-d", "postgres",
         "-c", "CREATE DATABASE frogcache_test"],
        check=True, capture_output=True, env=env,
    )
    user = env.get("USER", "postgres")
    yield f"postgresql://{user}@127.0.0.1:{port}/frogcache_test"
    subprocess.run(
        [f"{PG_BIN}/pg_ctl", "-D", str(base / "data"), "-m", "fast", "stop"],
        capture_output=True, env=env,
    )


@pytest.fixture()
def db_conn(pg_dsn):
    """Function-scoped connection with migrations applied and tables truncated after."""
    from frogcache import db

    conn = db.connect(pg_dsn)
    yield conn
    conn.execute("TRUNCATE fetches, events, meta RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
