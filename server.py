"""
Conector MCP para a API Olist/Tiny (ERP) - Pliar
==================================================

Servidor MCP remoto (HTTP) que expõe ferramentas de leitura e escrita sobre
a API pública v3 da Tiny/Olist (pedidos, produtos, estoque, preço), para uso
pelo Claude como um conector custom.

Todo o código fica aqui, em texto simples, versionado no repositório. Nada é
escondido em variáveis de ambiente codificadas.

Endpoints da Tiny confirmados em produção (via logs do nf-automatica):
  - Token OAuth2:  POST https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/token
  - Pedido:        GET  https://api.tiny.com.br/public-api/v3/pedidos/{id}
  - Produto:       GET  https://api.tiny.com.br/public-api/v3/produtos/{id}

Os demais endpoints (listagem de pedidos/produtos, atualização de estoque e
preço) seguem a convenção pública documentada pela Tiny (v3) e estão
marcados com comentário "# validar contra docs" — ajustar assim que
tivermos o link oficial da documentação em mãos.
"""

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

TINY_CLIENT_ID = os.environ["TINY_CLIENT_ID"]
TINY_CLIENT_SECRET = os.environ["TINY_CLIENT_SECRET"]
PUBLIC_URL = os.environ["PUBLIC_URL"]  # ex: https://olist-mcp-pliar-production.up.railway.app
MCP_SECRET = os.environ.get("MCP_SECRET", "")  # bearer token que protege as ferramentas MCP

# Credenciais OAuth que o Claude usa pra se autenticar com ESTE servidor (não
# confundir com TINY_CLIENT_ID/SECRET, que são pra autenticar com a Olist).
# O Client ID pode ser fixo; o Client Secret reaproveita o MCP_SECRET pra não
# precisar de mais uma variável.
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "olist-mcp-pliar")
OAUTH_CLIENT_SECRET = MCP_SECRET

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKENS_PATH = DATA_DIR / "tiny_tokens.json"

TINY_AUTH_BASE = "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect"
TINY_API_BASE = "https://api.tiny.com.br/public-api/v3"
REDIRECT_URI = f"{PUBLIC_URL}/callback"


def _load_tokens():
    if TOKENS_PATH.exists():
        return json.loads(TOKENS_PATH.read_text())
    return None


def _save_tokens(tokens: dict):
    tokens["obtained_at"] = time.time()
    TOKENS_PATH.write_text(json.dumps(tokens))


async def _exchange_code_for_tokens(code: str):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TINY_AUTH_BASE}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": TINY_CLIENT_ID,
                "client_secret": TINY_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        _save_tokens(resp.json())


async def _refresh_tokens():
    tokens = _load_tokens()
    if not tokens or "refresh_token" not in tokens:
        raise RuntimeError(
            "Sem refresh_token salvo. É preciso autorizar uma vez em "
            f"{PUBLIC_URL}/authorize antes de usar as ferramentas."
        )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TINY_AUTH_BASE}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": TINY_CLIENT_ID,
                "client_secret": TINY_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        new_tokens = resp.json()
        # Tiny nem sempre devolve um novo refresh_token; mantém o antigo se faltar
        new_tokens.setdefault("refresh_token", tokens["refresh_token"])
        _save_tokens(new_tokens)
        return new_tokens


async def _get_access_token() -> str:
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError(
            f"Conector ainda não autorizado. Acesse {PUBLIC_URL}/authorize "
            "uma vez para conceder acesso (login na Olist/Tiny)."
        )
    # renova um pouco antes de expirar (expires_in vem em segundos)
    expires_in = tokens.get("expires_in", 14400)
    if time.time() - tokens.get("obtained_at", 0) > expires_in - 120:
        tokens = await _refresh_tokens()
    return tokens["access_token"]


async def _tiny_request(method: str, path: str, **kwargs) -> dict:
    token = await _get_access_token()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"{TINY_API_BASE}{path}", headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Ferramentas MCP
# --------------------------------------------------------------------------

mcp = FastMCP("olist-tiny-pliar")


@mcp.tool()
async def listar_pedidos(situacao: str = "", data_inicial: str = "", data_final: str = "", pagina: int = 1) -> dict:
    """Lista pedidos de venda na Olist/Tiny. Filtros opcionais: situacao
    (ex: 'aberto', 'aprovado', 'faturado'), data_inicial e data_final
    (formato AAAA-MM-DD), pagina para paginação."""
    params = {"pagina": pagina}
    if situacao:
        params["situacao"] = situacao
    if data_inicial:
        params["dataInicial"] = data_inicial
    if data_final:
        params["dataFinal"] = data_final
    return await _tiny_request("GET", "/pedidos", params=params)  # validar contra docs


@mcp.tool()
async def obter_pedido(id_pedido: str) -> dict:
    """Retorna os detalhes completos de um pedido específico pelo ID."""
    return await _tiny_request("GET", f"/pedidos/{id_pedido}")


@mcp.tool()
async def listar_produtos(pesquisa: str = "", pagina: int = 1) -> dict:
    """Lista produtos cadastrados na Olist/Tiny. 'pesquisa' filtra por
    nome/SKU. 'pagina' para paginação."""
    params = {"pagina": pagina}
    if pesquisa:
        params["pesquisa"] = pesquisa
    return await _tiny_request("GET", "/produtos", params=params)  # validar contra docs


@mcp.tool()
async def obter_produto(id_produto: str) -> dict:
    """Retorna os detalhes completos de um produto (inclui estoque e preço) pelo ID."""
    return await _tiny_request("GET", f"/produtos/{id_produto}")


@mcp.tool()
async def atualizar_estoque(id_produto: str, quantidade: float, tipo: str = "B", deposito: str = "") -> dict:
    """Atualiza o estoque de um produto.
    tipo: 'B' = define o saldo (balanço), 'E' = entrada, 'S' = saída.
    deposito: nome/ID do depósito, se a conta usar múltiplos depósitos."""
    body = {"produto": {"id": id_produto}, "tipo": tipo, "quantidade": quantidade}
    if deposito:
        body["deposito"] = {"nome": deposito}
    return await _tiny_request("POST", f"/estoque/{id_produto}", json=body)  # validar contra docs


@mcp.tool()
async def atualizar_preco(id_produto: str, preco: float, preco_promocional: float = None) -> dict:
    """Atualiza o preço de venda (e opcionalmente o preço promocional) de um produto."""
    body = {"preco": preco}
    if preco_promocional is not None:
        body["precoPromocional"] = preco_promocional
    return await _tiny_request("PUT", f"/produtos/{id_produto}/preco", json=body)  # validar contra docs


@mcp.tool()
async def status_conexao() -> dict:
    """Verifica se o conector já foi autorizado com a Olist/Tiny e se o
    token está válido. Use isso primeiro se outra ferramenta falhar."""
    tokens = _load_tokens()
    if not tokens:
        return {"autorizado": False, "acao_necessaria": f"Acesse {PUBLIC_URL}/authorize"}
    expires_in = tokens.get("expires_in", 14400)
    restante = expires_in - (time.time() - tokens.get("obtained_at", 0))
    return {"autorizado": True, "token_expira_em_segundos": max(0, int(restante))}


# --------------------------------------------------------------------------
# Rotas HTTP auxiliares (fluxo OAuth de autorização única)
# --------------------------------------------------------------------------

async def authorize(request):
    url = (
        f"{TINY_AUTH_BASE}/auth?response_type=code"
        f"&client_id={TINY_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=openid"
    )
    return RedirectResponse(url)


async def callback(request):
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h1>Erro: código de autorização não recebido.</h1>", status_code=400)
    await _exchange_code_for_tokens(code)
    return HTMLResponse("<h1>Conectado com sucesso à Olist/Tiny. Pode fechar esta aba.</h1>")


async def health(request):
    return JSONResponse({"status": "ok"})


# --------------------------------------------------------------------------
# OAuth2 (Authorization Code + PKCE) para o PRÓPRIO Claude autenticar com
# este servidor. Fica em /oauth/* de propósito, pra não colidir com
# /authorize e /callback usados acima no fluxo com a Olist/Tiny.
# --------------------------------------------------------------------------

# code -> {"code_challenge": str, "redirect_uri": str, "expires_at": float}
# Armazenamento em memória: cada code é de uso único e expira em minutos,
# então não precisa persistir em disco.
_AUTH_CODES: dict[str, dict] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def oauth_metadata(request):
    return JSONResponse(
        {
            "issuer": PUBLIC_URL,
            "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize",
            "token_endpoint": f"{PUBLIC_URL}/oauth/token",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
        }
    )


async def oauth_protected_resource(request):
    return JSONResponse(
        {
            "resource": f"{PUBLIC_URL}/mcp",
            "authorization_servers": [PUBLIC_URL],
        }
    )


async def oauth_authorize(request):
    """Primeira perna do Authorization Code + PKCE. Como este servidor é de
    uso pessoal (um único usuário/empresa), aprova automaticamente — não há
    tela de login/consentimento separada."""
    q = request.query_params
    if q.get("client_id") != OAUTH_CLIENT_ID:
        return PlainTextResponse("client_id inválido", status_code=400)
    redirect_uri = q.get("redirect_uri")
    if not redirect_uri:
        return PlainTextResponse("redirect_uri obrigatório", status_code=400)

    code = secrets.token_urlsafe(32)
    _AUTH_CODES[code] = {
        "code_challenge": q.get("code_challenge", ""),
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + 600,
    }
    sep = "&" if "?" in redirect_uri else "?"
    dest = f"{redirect_uri}{sep}code={code}"
    if q.get("state"):
        dest += f"&state={q['state']}"
    return RedirectResponse(dest)


async def oauth_token(request):
    """Segunda perna: troca o code (+ code_verifier do PKCE) por um access
    token. Também aceita grant_type=refresh_token (devolve o mesmo token,
    já que ele não expira de fato — simplificação razoável pra um servidor
    de uso interno de uma pessoa só)."""
    form = await request.form()
    grant_type = form.get("grant_type")

    client_id = None
    client_secret = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            client_id, _, client_secret = decoded.partition(":")
        except Exception:
            pass
    if client_id is None:
        client_id = form.get("client_id")
        client_secret = form.get("client_secret")

    if client_id != OAUTH_CLIENT_ID or not OAUTH_CLIENT_SECRET or client_secret != OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "refresh_token":
        return JSONResponse(
            {"access_token": MCP_SECRET, "token_type": "Bearer", "expires_in": 34560000}
        )

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = form.get("code")
    entry = _AUTH_CODES.pop(code, None)
    if not entry or entry["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    code_verifier = form.get("code_verifier", "")
    expected_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    if entry["code_challenge"] and expected_challenge != entry["code_challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE mismatch"}, status_code=400)

    return JSONResponse(
        {
            "access_token": MCP_SECRET,
            "token_type": "Bearer",
            "expires_in": 34560000,
            "refresh_token": MCP_SECRET,
        }
    )


# Rotas que não exigem o bearer token: healthcheck, o fluxo OAuth com a
# Olist/Tiny, e a negociação OAuth do próprio Claude com este servidor.
PUBLIC_PATHS = {
    "/health",
    "/authorize",
    "/callback",
    "/favicon.ico",
    "/oauth/authorize",
    "/oauth/token",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Protege as ferramentas MCP com um token fixo (MCP_SECRET). Sem isso,
    qualquer pessoa que descobrisse a URL do serviço conseguiria consultar e
    alterar pedidos/estoque/preço na Olist sem nenhuma autenticação."""

    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if not MCP_SECRET:
            return PlainTextResponse(
                "MCP_SECRET não configurado no servidor.", status_code=500
            )
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {MCP_SECRET}":
            return PlainTextResponse(
                "Unauthorized",
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource"'
                    )
                },
            )
        return await call_next(request)


@contextlib.asynccontextmanager
async def lifespan(app):
    # Sem isso, o session_manager do FastMCP nunca inicializa seu task
    # group interno e toda chamada em /mcp cai com "Task group is not
    # initialized" — só funciona quando o lifespan é propagado assim.
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/authorize", authorize),
        Route("/callback", callback),
        Route("/health", health),
        Route("/oauth/authorize", oauth_authorize),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/.well-known/oauth-authorization-server", oauth_metadata),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    ],
    lifespan=lifespan,
)
app.add_middleware(BearerAuthMiddleware)

# Monta o servidor MCP (transporte HTTP em streaming) na mesma app, em /mcp
app.mount("/", mcp.streamable_http_app())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
