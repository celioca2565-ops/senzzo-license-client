"""
Servidor de licença FALSO, mas de verdade (roda HTTP de verdade, não é mock
de função) — implementa o mesmo contrato do /validate-token real, com uma
rota de controle (/__definir_resposta__) pra testes decidirem o que ele
responde em seguida. Usado só nos testes do senzzo_license_client.
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

_proxima_resposta = {"licensed": True, "plan": "pro", "features": ["x"], "expires_at": "2030-01-01T00:00:00Z"}
_responder_lixo = {"ativo": False}


class RespostaControlada(BaseModel):
    licensed: bool
    reason_code: str | None = None
    plan: str | None = None
    features: list | None = None
    expires_at: str | None = None


@app.post("/__definir_resposta__")
def definir_resposta(dados: RespostaControlada):
    global _proxima_resposta
    _responder_lixo["ativo"] = False
    _proxima_resposta = dados.model_dump()
    return {"ok": True}


@app.post("/__responder_lixo__")
def responder_lixo():
    """Faz o /validate-token devolver algo que não é o formato esperado
    (sem o campo 'licensed'), pra testar o caminho de reason_code=malformed."""
    _responder_lixo["ativo"] = True
    return {"ok": True}


@app.post("/validate-token")
def validate_token(dados: dict):
    if _responder_lixo["ativo"]:
        return {"algo": "inesperado"}
    return _proxima_resposta
