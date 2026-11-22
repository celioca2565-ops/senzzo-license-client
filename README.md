# senzzo_license_client

Biblioteca usada por cada produto Senzzo (Atendimento, Fiscal, CRM, etc.)
pra consultar o `license-server-v2` e decidir se a instalação atual pode
continuar rodando.

Não é um serviço — não entra no Easypanel, não tem porta própria. É
instalada dentro do produto que vai usá-la.

## Instalação

```
pip install git+https://github.com/celioca2565-ops/senzzo-license-client.git
```

## Uso

```python
from senzzo_license_client import LicenseClient

client = LicenseClient(
    license_server_url="https://painel-licencas.senzzoautomacao.com.br",
    token=os.environ["LICENSE_TOKEN"],       # emitido pelo Senzzo Admin Licenças
    dsn=os.environ["DATABASE_URL"],          # banco do próprio produto
)

resultado = client.verificar()

if not resultado.licensed:
    # bloquear o produto. resultado.reason_code diz o motivo exato.
    ...

if resultado.aviso:
    # a checagem passou por tolerância (licença expirada há pouco, ou
    # servidor de licença temporariamente fora do ar) — logar/alertar,
    # mas deixar o produto continuar rodando.
    ...
```

## O que acontece por baixo

- Na primeira execução, gera um fingerprint novo (UUID) e salva numa
  tabela própria (`senzzo_license_cache`) criada automaticamente no banco
  do produto. Esse fingerprint começa como **não confirmado**
  (`confidence_level: "low"` enviado ao servidor).
- Na segunda execução em diante, se o fingerprint sobreviveu (realmente
  persistiu no banco), ele é promovido a **confirmado**
  (`confidence_level: "high"`).
- O resultado de cada checagem é salvo nessa mesma tabela — isso permite
  tolerância quando o servidor está fora do ar ou a licença acabou de
  expirar, sem perder o último plano/funcionalidades conhecidos.

## Política de bloqueio

**Grupo A — tolerância (continua rodando, com aviso):**
- `expired` — até 5 dias desde a última confirmação válida
- `server_unreachable` — até 7 dias desde a última confirmação válida

**Grupo B — bloqueio imediato, sem tolerância:**
`revoked`, `blocked`, `not_found`, `invalid_signature`,
`device_limit_reached`, `missing`, `malformed`, `config_error`

Ver `senzzo_license_client/policy.py` pros valores exatos das janelas de
graça.

## Testando

```
pip install -e .
pip install fastapi uvicorn httpx psycopg2-binary
python tests/test_client_real.py
```

O teste sobe um servidor de licença falso de verdade (processo HTTP
separado, não é mock de função) e roda contra um Postgres real — nenhuma
parte da lógica de rede ou banco é simulada em memória.
