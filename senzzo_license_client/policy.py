"""
Política de bloqueio do client de licença.

Grupo A — Tolerância: o produto continua rodando (com aviso) por uma
janela de graça, porque a causa provavelmente não é fraude, é falha
temporária (rede fora do ar, ou o cliente do produto atrasou o pagamento
mas ainda está em negociação).

Grupo B — Bloqueio imediato: motivo claro de que a licença não deve ser
usada agora, sem tolerância nenhuma.
"""

from datetime import datetime, timedelta, timezone

GRACE_DAYS_EXPIRED = 5
GRACE_DAYS_SERVER_UNREACHABLE = 7

GRUPO_A_TOLERANCIA = {"expired", "server_unreachable"}
GRUPO_B_BLOQUEIO_IMEDIATO = {
    "revoked",
    "invalid_signature",
    "missing",
    "malformed",
    "config_error",
    "not_found",
    "blocked",
    "device_limit_reached",
}


def janela_de_graca(reason_code: str) -> timedelta | None:
    if reason_code == "expired":
        return timedelta(days=GRACE_DAYS_EXPIRED)
    if reason_code == "server_unreachable":
        return timedelta(days=GRACE_DAYS_SERVER_UNREACHABLE)
    return None


def dentro_da_graca(reason_code: str, last_known_good_at: datetime | None) -> bool:
    """Decide se, mesmo sem licença válida agora, o produto pode continuar
    rodando com aviso — baseado em quando foi a última vez que o servidor
    confirmou a licença como válida."""
    if reason_code not in GRUPO_A_TOLERANCIA:
        return False
    if last_known_good_at is None:
        # Nunca teve uma confirmação válida registrada — não há graça a dar.
        return False
    janela = janela_de_graca(reason_code)
    agora = datetime.now(timezone.utc)
    return (agora - last_known_good_at) <= janela
