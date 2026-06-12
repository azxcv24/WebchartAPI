"""ClickHouse ingest backend (clickhouse-connect).

Unlike the file connectors, ClickHouse is a query source: it runs SQL and
materializes the result (as an Arrow table) into Parquet.

URI:    clickhouse://host[:port]/database   (clickhouses:// for TLS)
creds:  username, password
options: query (SQL) OR table (-> SELECT * FROM table), limit, secure
"""

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import require_lib
from webchart.services.ingest.upload import write_table_to_parquet

logger = logging.getLogger("webchart.ingest.clickhouse")


class ClickHouseIngestBackend:
    """Run a ClickHouse query and persist the result as Parquet."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["clickhouse", "clickhouses"]

    @staticmethod
    def _resolve_query(spec: RemoteSourceSpec) -> tuple[str, str]:
        """Return (sql, display_name)."""
        query = spec.options.get("query")
        table = spec.options.get("table")
        if query:
            return query, (table or "clickhouse_query")
        if table:
            return f"SELECT * FROM {table}", table
        raise ConnectorError(
            "ClickHouse connector requires either options.query or options.table"
        )

    async def validate_source(self, spec: RemoteSourceSpec) -> bool:
        try:
            self._resolve_query(spec)
            urlparse(spec.uri)
            return True
        except Exception:
            return False

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        cc = require_lib("clickhouse_connect", "clickhouse")

        dataset_id = dest.name
        sql, name = self._resolve_query(spec)

        parsed = urlparse(spec.uri)
        secure = spec.protocol == "clickhouses" or spec.options.get("secure") == "true"
        port = parsed.port or (8443 if secure else 8123)
        database = unquote(parsed.path).lstrip("/") or spec.options.get(
            "database", "default"
        )
        try:
            limit = int(spec.options.get("limit", settings.clickhouse_max_rows))
        except ValueError:
            limit = settings.clickhouse_max_rows

        creds = spec.credentials

        def _fetch():
            client = cc.get_client(
                host=parsed.hostname,
                port=port,
                username=creds.get("username", "default"),
                password=creds.get("password", ""),
                database=database,
                secure=secure,
            )
            try:
                # max_result_rows caps the server-side result set.
                return client.query_arrow(
                    sql, settings={"max_result_rows": limit, "result_overflow_mode": "throw"}
                )
            finally:
                client.close()

        try:
            table = await run_in_thread(_fetch)
        except Exception as e:
            raise ConnectorError(f"ClickHouse query failed: {e}") from e

        return await run_in_thread(
            write_table_to_parquet, table, dataset_id, name, "clickhouse"
        )
