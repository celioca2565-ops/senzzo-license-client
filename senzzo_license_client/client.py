"""
senzzo_license_client — biblioteca usada por cada produto Senzzo (Atendimento,
Fiscal, CRM, etc.) pra consultar o license-server-v2 e decidir se a
instalação atual pode continuar rodando.

Assíncrono, alinhado à decisão de arquitetura registrada no core/database.py
do ecossistema (convergência pra asyncpg).

Uso típico (no lifespan do FastAPI, e periodicamente):

    from senzzo_license_client import LicenseClient

    client = LicenseClient(
        license_server_url="https://painel-licencas.senzzoautomacao.com.br",
        token="<token da licença, emitido pelo Senzzo Admin Licenças>",
        dsn="postgresql://...",  # banco do próprio produto
    )
    resultado = await client.verificar()
    if not resultado.licensed:
        # bloquear o produto
        ...
    if resultado.aviso:
        # logar/alertar, mas deixar continuar (tolerância)
        ...
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from . import policy
from .storage import ArmazenamentoLicenca

TIMEOUT_SEGUNDOS = 5.0


def _parse_data(valor: str | None) -> datetime | None:
    """O servidor manda expires_at como string ISO (vem do claim do JWT).
    asyncpg exige datetime de verdade pra salvar em coluna TIMESTAMPTZ."""
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class ResultadoVerificacao:
    licensed: bool
    reason_code: str | None
    plan: str | None = None
    features: list | None = None
    expires_at: str | None = None
    aviso: bool = False  # True = passou por tolerância (Grupo A), vale alertar


class LicenseClient:
    def __init__(self, license_server_url: str | None, token: str | None, dsn: str | None):
        self._license_server_url = (license_server_url or "").rstrip("/")
        self._token = token
        self._dsn = dsn

    async def verificar(self) -> ResultadoVerificacao:
        # Config ausente: nem tenta rede, bloqueio imediato (Grupo B).
        if not self._token or not self._license_server_url or not self._dsn:
            return ResultadoVerificacao(licensed=False, reason_code="config_error")

        storage = ArmazenamentoLicenca(self._dsn)
        fingerprint, confirmado = await storage.obter_ou_criar_fingerprint()
        confidence_level = "high" if confirmado else "low"

        estado_anterior = await storage.carregar()
        last_known_good_at = estado_anterior.last_known_good_at if estado_anterior else None

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as http:
                resposta = await http.post(
                    f"{self._license_server_url}/validate-token",
                    json={
                        "token": self._token,
                        "fingerprint": fingerprint,
                        "confidence_level": confidence_level,
                    },
                )
        except httpx.RequestError:
            return await self._tratar_com_cache(storage, fingerprint, "server_unreachable", last_known_good_at)

        try:
            dados = resposta.json()
            licensed = dados["licensed"]
        except (ValueError, KeyError):
            # Resposta não reconhecida — mais seguro bloquear do que assumir.
            await storage.salvar_resultado(
                fingerprint=fingerprint, licensed=False, reason_code="malformed",
                plan=None, features=None, expires_at=None, last_known_good_at=last_known_good_at,
            )
            return ResultadoVerificacao(licensed=False, reason_code="malformed")

        agora = datetime.now(timezone.utc)

        if licensed:
            expires_at_str = dados.get("expires_at")
            expires_at_dt = _parse_data(expires_at_str)
            await storage.salvar_resultado(
                fingerprint=fingerprint, licensed=True, reason_code=None,
                plan=dados.get("plan"), features=dados.get("features"),
                expires_at=expires_at_dt, last_known_good_at=agora,
            )
            return ResultadoVerificacao(
                licensed=True, reason_code=None, plan=dados.get("plan"),
                features=dados.get("features"), expires_at=dados.get("expires_at"),
            )

        reason_code = dados.get("reason_code", "unknown")
        await storage.salvar_resultado(
            fingerprint=fingerprint, licensed=False, reason_code=reason_code,
            plan=None, features=None, expires_at=None, last_known_good_at=last_known_good_at,
        )
        return self._aplicar_politica(reason_code, last_known_good_at, estado_anterior)

    async def _tratar_com_cache(self, storage, fingerprint, reason_code, last_known_good_at) -> ResultadoVerificacao:
        await storage.salvar_resultado(
            fingerprint=fingerprint, licensed=False, reason_code=reason_code,
            plan=None, features=None, expires_at=None, last_known_good_at=last_known_good_at,
        )
        estado_atual = await storage.carregar()
        return self._aplicar_politica(reason_code, last_known_good_at, estado_atual)

    def _aplicar_politica(self, reason_code, last_known_good_at, estado_anterior) -> ResultadoVerificacao:
        if policy.dentro_da_graca(reason_code, last_known_good_at):
            # Usa os últimos dados bons conhecidos (plan/features), com aviso.
            plan = estado_anterior.plan if estado_anterior else None
            features = estado_anterior.features if estado_anterior else None
            return ResultadoVerificacao(
                licensed=True, reason_code=reason_code, plan=plan, features=features, aviso=True,
            )
        return ResultadoVerificacao(licensed=False, reason_code=reason_code)
