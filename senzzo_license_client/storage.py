"""
Armazenamento do client de licença, no banco do próprio produto que o usa.

Decisão registrada (ver documento de contexto): nenhum produto Senzzo tem
volume de container persistente hoje — só os bancos têm volume de
verdade. Por isso o fingerprint e o cache de tolerância offline vivem na
mesma tabela, dentro do banco do produto (não em arquivo local).

Reescrito pra asyncpg (21/11/2026): o ecossistema Senzzo tem decisão de
arquitetura registrada (core/database.py) de convergir todos os serviços
pra asyncpg + pool. Este client abre sua própria conexão curta e
independente — não reaproveita o pool do produto hospedeiro, pra não se
acoplar a como cada produto gerencia o próprio pool — mas usa a mesma
tecnologia de driver, em vez de introduzir um terceiro padrão (psycopg2)
no ecossistema.

A tabela fica sempre no schema "public", mesmo em produtos que usam outro
schema pra dados de negócio (ex: Atendimento usa "contabil") — é dado de
infraestrutura do client, não dado de negócio do produto, e fica fácil de
achar sempre no mesmo lugar em qualquer produto.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

TABELA = "public.senzzo_license_cache"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABELA} (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    fingerprint TEXT NOT NULL,
    fingerprint_confirmado BOOLEAN NOT NULL DEFAULT FALSE,
    licensed BOOLEAN NOT NULL,
    reason_code TEXT,
    plan TEXT,
    features JSONB,
    expires_at TIMESTAMPTZ,
    last_check_at TIMESTAMPTZ NOT NULL,
    last_known_good_at TIMESTAMPTZ
);
"""


@dataclass
class EstadoCache:
    fingerprint: str
    fingerprint_confirmado: bool
    licensed: bool | None
    reason_code: str | None
    plan: str | None
    features: list | None
    expires_at: datetime | None
    last_check_at: datetime | None
    last_known_good_at: datetime | None


class ArmazenamentoLicenca:
    """Conecta direto via asyncpg — conexão própria e curta, independente
    do pool do produto hospedeiro."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    async def _conectar(self) -> asyncpg.Connection:
        conn = await asyncpg.connect(self.dsn)
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
        )
        return conn

    async def garantir_tabela(self) -> None:
        conn = await self._conectar()
        try:
            await conn.execute(_DDL)
        finally:
            await conn.close()

    async def carregar(self) -> EstadoCache | None:
        await self.garantir_tabela()
        conn = await self._conectar()
        try:
            row = await conn.fetchrow(f"SELECT * FROM {TABELA} WHERE id = 1")
        finally:
            await conn.close()
        if not row:
            return None
        return EstadoCache(
            fingerprint=row["fingerprint"],
            fingerprint_confirmado=row["fingerprint_confirmado"],
            licensed=row["licensed"],
            reason_code=row["reason_code"],
            plan=row["plan"],
            features=row["features"],
            expires_at=row["expires_at"],
            last_check_at=row["last_check_at"],
            last_known_good_at=row["last_known_good_at"],
        )

    async def obter_ou_criar_fingerprint(self) -> tuple[str, bool]:
        """Retorna (fingerprint, confirmado). confirmado=True só depois de já
        ter sobrevivido a pelo menos uma leitura anterior (prova de persistência
        de verdade, não assumida)."""
        estado = await self.carregar()
        if estado:
            if not estado.fingerprint_confirmado:
                conn = await self._conectar()
                try:
                    await conn.execute(f"UPDATE {TABELA} SET fingerprint_confirmado = TRUE WHERE id = 1")
                finally:
                    await conn.close()
            return estado.fingerprint, True

        novo_fingerprint = str(uuid.uuid4())
        agora = datetime.now(timezone.utc)
        conn = await self._conectar()
        try:
            await conn.execute(
                f"""
                INSERT INTO {TABELA} (id, fingerprint, fingerprint_confirmado, licensed, last_check_at)
                VALUES (1, $1, FALSE, FALSE, $2)
                ON CONFLICT (id) DO NOTHING
                """,
                novo_fingerprint, agora,
            )
        finally:
            await conn.close()
        # Relê para cobrir corrida entre dois processos subindo ao mesmo tempo.
        estado = await self.carregar()
        return estado.fingerprint, estado.fingerprint_confirmado

    async def salvar_resultado(
        self,
        *,
        fingerprint: str,
        licensed: bool,
        reason_code: str | None,
        plan: str | None,
        features: list | None,
        expires_at: datetime | None,
        last_known_good_at: datetime | None,
    ) -> None:
        agora = datetime.now(timezone.utc)
        conn = await self._conectar()
        try:
            await conn.execute(
                f"""
                INSERT INTO {TABELA}
                    (id, fingerprint, fingerprint_confirmado, licensed, reason_code,
                     plan, features, expires_at, last_check_at, last_known_good_at)
                VALUES (1, $1, FALSE, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO UPDATE SET
                    licensed = EXCLUDED.licensed,
                    reason_code = EXCLUDED.reason_code,
                    plan = COALESCE(EXCLUDED.plan, {TABELA}.plan),
                    features = COALESCE(EXCLUDED.features, {TABELA}.features),
                    expires_at = COALESCE(EXCLUDED.expires_at, {TABELA}.expires_at),
                    last_check_at = EXCLUDED.last_check_at,
                    last_known_good_at = COALESCE(EXCLUDED.last_known_good_at, {TABELA}.last_known_good_at)
                """,
                fingerprint, licensed, reason_code, plan, features, expires_at, agora, last_known_good_at,
            )
        finally:
            await conn.close()
