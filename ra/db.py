"""DB connection config."""

import psycopg2

DB_DSN = "host=127.0.0.1 port=5433 dbname=openshift_cluster user=postgres"


def connect(dsn: str = DB_DSN):
    """Return a psycopg2 connection."""
    return psycopg2.connect(dsn)
