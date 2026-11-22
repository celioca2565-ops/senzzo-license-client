"""
Teste funcional REAL do senzzo_license_client (versão asyncpg).
- Postgres real (sem mock de banco)
- Servidor HTTP real rodando em processo separado (sem mock de rede)
Só o conteúdo da resposta do servidor é controlado via sua rota de teste
(/__definir_resposta__), não a camada de rede em si.
"""

import asyncio
import os
import subprocess
import sys
import time

import asyncpg
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from senzzo_license_client import LicenseClient  # noqa: E402

DSN = "postgresql://postgres:postgres@localhost:5432/senzzo_license_test"
SERVER_PORT = 8911
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
PORTA_SEM_NADA_ESCUTANDO = f"http://127.0.0.1:{SERVER_PORT + 1}"  # ninguém escuta aqui -> server_unreachable de verdade

falhas = []


def checar(descricao, condicao):
    status = "OK " if condicao else "FALHOU"
    print(f"[{status}] {descricao}")
    if not condicao:
        falhas.append(descricao)


def definir_resposta_servidor(**kwargs):
    httpx.post(f"{SERVER_URL}/__definir_resposta__", json=kwargs, timeout=5)


async def limpar_cache_banco():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS public.senzzo_license_cache")
    finally:
        await conn.close()


async def forcar_last_known_good_no_passado(dias_atras: int):
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "UPDATE public.senzzo_license_cache SET last_known_good_at = NOW() - make_interval(days => $1)",
            dias_atras,
        )
    finally:
        await conn.close()


async def ler_fingerprint_confirmado() -> bool:
    conn = await asyncpg.connect(DSN)
    try:
        return await conn.fetchval("SELECT fingerprint_confirmado FROM public.senzzo_license_cache WHERE id = 1")
    finally:
        await conn.close()


async def main():
    processo = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "fake_license_server:app", "--port", str(SERVER_PORT)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    try:
        await limpar_cache_banco()

        # --- 1) config ausente -> bloqueio imediato, sem nem tentar rede ---
        client_sem_config = LicenseClient(license_server_url=None, token=None, dsn=None)
        resultado = await client_sem_config.verificar()
        checar("Config ausente bloqueia na hora com config_error", not resultado.licensed and resultado.reason_code == "config_error")

        client = LicenseClient(license_server_url=SERVER_URL, token="token-fake", dsn=DSN)

        # --- 2) primeira execução: fingerprint ainda não confirmado (confidence_level=low) ---
        definir_resposta_servidor(licensed=True, plan="pro", features=["a", "b"], expires_at="2030-01-01T00:00:00Z")
        resultado = await client.verificar()
        checar("Primeira checagem licenciada retorna licensed=True", resultado.licensed)
        checar("Plan retornado corretamente", resultado.plan == "pro")

        confirmado_apos_primeira = await ler_fingerprint_confirmado()
        checar("Fingerprint NÃO confirmado logo após a 1ª execução (low)", confirmado_apos_primeira is False)

        # --- 3) segunda execução: fingerprint já confirmado (persistiu de verdade no banco) ---
        resultado2 = await client.verificar()
        checar("Segunda checagem (fingerprint já confirmado) ainda licensed=True", resultado2.licensed)

        confirmado_apos_segunda = await ler_fingerprint_confirmado()
        checar("Fingerprint CONFIRMADO após sobreviver a uma 2ª execução (high)", confirmado_apos_segunda is True)

        # --- 4) licença revogada -> bloqueio imediato (Grupo B), mesmo tendo histórico bom ---
        definir_resposta_servidor(licensed=False, reason_code="revoked")
        resultado = await client.verificar()
        checar("Revoked bloqueia imediatamente, mesmo com histórico bom", not resultado.licensed and resultado.reason_code == "revoked")

        # --- 5) device_limit_reached -> bloqueio imediato (decisão confirmada por Célio) ---
        definir_resposta_servidor(licensed=False, reason_code="device_limit_reached")
        resultado = await client.verificar()
        checar("device_limit_reached bloqueia imediatamente", not resultado.licensed and resultado.reason_code == "device_limit_reached")

        # --- 6) expired DENTRO da janela de graça -> continua rodando com aviso ---
        definir_resposta_servidor(licensed=True, plan="pro", features=["a"], expires_at="2030-01-01T00:00:00Z")
        await client.verificar()  # garante last_known_good_at = agora
        definir_resposta_servidor(licensed=False, reason_code="expired")
        resultado = await client.verificar()
        checar("Expired dentro da janela de graça continua licensed=True", resultado.licensed and resultado.aviso)
        checar("Plan preservado do último bom durante a graça", resultado.plan == "pro")

        # --- 7) expired FORA da janela de graça -> bloqueia ---
        await forcar_last_known_good_no_passado(dias_atras=10)
        definir_resposta_servidor(licensed=False, reason_code="expired")
        resultado = await client.verificar()
        checar("Expired fora da janela de graça bloqueia", not resultado.licensed and resultado.reason_code == "expired")

        # --- 8) servidor fora do ar (de verdade, ninguém escutando na porta) ---
        definir_resposta_servidor(licensed=True, plan="pro", features=["a"], expires_at="2030-01-01T00:00:00Z")
        await client.verificar()
        client_servidor_fora = LicenseClient(license_server_url=PORTA_SEM_NADA_ESCUTANDO, token="token-fake", dsn=DSN)
        resultado = await client_servidor_fora.verificar()
        checar(
            "Servidor inacessível dentro da janela de graça continua rodando com aviso",
            resultado.licensed and resultado.aviso and resultado.reason_code == "server_unreachable",
        )

        # --- 9) servidor fora do ar, FORA da janela de graça -> bloqueia ---
        await forcar_last_known_good_no_passado(dias_atras=30)
        resultado = await client_servidor_fora.verificar()
        checar(
            "Servidor inacessível fora da janela de graça bloqueia",
            not resultado.licensed and resultado.reason_code == "server_unreachable",
        )

        # --- 10) resposta malformada do servidor -> bloqueia (Grupo B), não usa graça ---
        httpx.post(f"{SERVER_URL}/__responder_lixo__", timeout=5)
        resultado = await client.verificar()
        checar("Resposta malformada do servidor bloqueia (não usa tolerância)", not resultado.licensed and resultado.reason_code == "malformed")

    finally:
        processo.terminate()
        processo.wait(timeout=5)


asyncio.run(main())

print()
if falhas:
    print(f"RESULTADO: {len(falhas)} verificação(ões) falharam: {falhas}")
    sys.exit(1)
else:
    print("RESULTADO: todas as verificações passaram.")
