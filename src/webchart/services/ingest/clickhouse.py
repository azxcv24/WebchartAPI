"""ClickHouse 인제스트 백엔드 (clickhouse-connect).

파일 커넥터와 달리 ClickHouse는 질의 소스다. SQL을 실행하고 결과(Arrow 테이블)를
Parquet 데이터셋으로 저장한다.

URI:     clickhouse://host[:port]/database   (TLS는 clickhouses://)
creds:   username, password
options: query(SQL) 또는 table(-> SELECT * FROM table), limit, secure
"""

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import require_lib
from webchart.services.ingest.upload import write_table_to_parquet

logger = logging.getLogger("webchart.ingest.clickhouse")

# table 또는 db.table. 따옴표·공백·세미콜론 없는 단순 식별자만 허용한다.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


class ClickHouseIngestBackend:
    """ClickHouse 질의를 실행하고 결과를 Parquet으로 저장한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["clickhouse", "clickhouses"]

    @staticmethod
    def _resolve_query(spec: RemoteSourceSpec) -> tuple[str, str]:
        """(sql, 표시 이름)을 돌려준다.

        options.query는 의도적으로 임의 SQL을 허용한다. options.table은 SQL 문자열에
        끼워 넣기 전에 단순 [db.]table 식별자인지 검사한다. SQL을 몰래 넣지 못하게 하고,
        "table"이 이름 그대로 테이블만 뜻하게 하기 위해서다.
        """
        query = spec.options.get("query")
        table = spec.options.get("table")
        if query:
            return query, (table or "clickhouse_query")
        if table:
            if not _TABLE_RE.match(table):
                raise ConnectorError(
                    f"Invalid table identifier: {table!r} "
                    "(expected [database.]table); use options.query for complex SQL"
                )
            return f"SELECT * FROM {table}", table
        raise ConnectorError("ClickHouse connector requires either options.query or options.table")

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        cc = require_lib("clickhouse_connect", "clickhouse")

        dataset_id = dest.name
        sql, name = self._resolve_query(spec)

        parsed = urlparse(spec.uri)
        secure = spec.protocol == "clickhouses" or spec.options.get("secure") == "true"
        port = parsed.port or (8443 if secure else 8123)
        database = unquote(parsed.path).lstrip("/") or spec.options.get("database", "default")
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
                # 서버 쪽에서 결과 행 수를 제한하고, 넘으면 잘라 내지 않고 오류로 끝낸다
                # (일부만 담긴 데이터셋이 조용히 만들어지지 않도록).
                return client.query_arrow(
                    sql, settings={"max_result_rows": limit, "result_overflow_mode": "throw"}
                )
            finally:
                client.close()

        try:
            table = await run_in_thread(_fetch)
        except Exception as e:
            raise ConnectorError(f"ClickHouse query failed: {e}") from e

        return await run_in_thread(write_table_to_parquet, table, dataset_id, name, "clickhouse")
