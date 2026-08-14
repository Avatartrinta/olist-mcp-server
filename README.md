"""
Conector MCP Olist/Tiny — PLIAR  (v2)

Mudanca em relacao a v1:
  - NAO anuncia mais OAuth para o cliente MCP. O endpoint /mcp aceita o segredo
    de duas formas: cabecalho "Authorization: Bearer <MCP_SECRET>" OU parametro
    de URL "?t=<MCP_SECRET>". Sem OAuth, sem registro dinamico de cliente.
  - Requisicao sem segredo responde 403 (e nao 401). O 401 e o que faz o cliente
    iniciar a descoberta de OAuth; com 403 ele simplesmente reporta o erro.
  - As rotas de descoberta de OAuth respondem 404 explicitamente, para o caso de
    alguma biblioteca tentar registra-las por conta propria.
  - Ferramentas novas: buscar_produto_por_sku, estrutura_produto (composicao de
    kit) e chamar_api_tiny (passagem direta, para explorar a API v3).

O fluxo OAuth com a propria Olist/Tiny continua igual: /authorize -> /callback.
"""

import json
import os
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
# Configuracao
# --------------------------------------------------------------------------

TINY_CLIENT_ID = os.environ["TINY_CLIENT_ID"]
TINY_CLIENT_SECRET = os.environ["TINY_CLIENT_SECRET"]
PUBLIC_URL = os.environ["PUBLIC_URL"]
MCP_SECRET = os.environ.get("MCP_SECRET", "")
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
            "Sem refresh_token salvo. Autorize uma vez em "
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
        new_tokens.setdefault("refresh_token", tokens["refresh_token"])
        _save_tokens(new_tokens)
        return new_tokens


async def _get_access_token() -> str:
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError(
            f"Conector ainda nao autorizado. Acesse {PUBLIC_URL}/authorize uma vez."
        )
    expires_in = tokens.get("expires_in", 14400)
    if time.time() - tokens.get("obtained_at", 0) > expires_in - 120:
        tokens = await _refresh_tokens()
    return tokens["access_token"]


async def _tiny_request(method: str, path: str, **kwargs):
    token = await _get_access_token()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.request(method, f"{TINY_API_BASE}{path}", headers=headers, **kwargs)
        if resp.status_code >= 400:
            return {"erro_http": resp.status_code, "detalhe": resp.text[:1500],
                    "url": str(resp.request.url)}
        if not resp.content:
            return {"ok": True, "status": resp.status_code}
        try:
            return resp.json()
        except Exception:
            return {"ok": True, "status": resp.status_code, "texto": resp.text[:1500]}


# --------------------------------------------------------------------------
# Ferramentas MCP
# --------------------------------------------------------------------------

# A protecao contra DNS rebinding do SDK valida o cabecalho Host e, com a lista
# vazia, recusa o dominio do Railway. Quem protege este servidor e o MCP_SECRET,
# entao a validacao de Host e desligada de proposito. Versoes antigas do SDK nao
# tem esse parametro — nesse caso cai no construtor simples.
try:
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "olist-tiny-pliar",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    print("[boot] transport_security desligado", flush=True)
except Exception as _e:  # SDK antigo
    print(f"[boot] sem transport_security ({_e})", flush=True)
    mcp = FastMCP("olist-tiny-pliar")


@mcp.tool()
async def status_conexao() -> dict:
    """Verifica se o conector ja foi autorizado com a Olist/Tiny e se o token esta valido.
    Use isso primeiro se qualquer outra ferramenta falhar."""
    tokens = _load_tokens()
    if not tokens:
        return {"autorizado": False, "acao_necessaria": f"Acesse {PUBLIC_URL}/authorize"}
    expires_in = tokens.get("expires_in", 14400)
    restante = expires_in - (time.time() - tokens.get("obtained_at", 0))
    return {"autorizado": True, "token_expira_em_segundos": max(0, int(restante))}


@mcp.tool()
async def listar_produtos(pesquisa: str = "", pagina: int = 1, limite: int = 100) -> dict:
    """Lista produtos cadastrados na Olist/Tiny. 'pesquisa' filtra por nome ou SKU."""
    params = {"pagina": pagina, "limit": limite}
    if pesquisa:
        params["pesquisa"] = pesquisa
    return await _tiny_request("GET", "/produtos", params=params)


@mcp.tool()
async def obter_produto(id_produto: str) -> dict:
    """Retorna os dados completos de um produto pelo ID interno da Olist/Tiny."""
    return await _tiny_request("GET", f"/produtos/{id_produto}")


@mcp.tool()
async def buscar_produto_por_sku(sku: str) -> dict:
    """Localiza um produto pelo codigo/SKU. E o SKU do anuncio do Mercado Livre que
    corresponde ao codigo do produto na Olist — use esta ferramenta para fazer a ponte
    entre um anuncio do ML e o produto no ERP."""
    dados = await _tiny_request("GET", "/produtos", params={"codigo": sku, "limit": 50})
    if isinstance(dados, dict) and dados.get("erro_http"):
        dados = await _tiny_request("GET", "/produtos", params={"pesquisa": sku, "limit": 50})
    itens = dados.get("itens") or dados.get("produtos") or dados.get("data") or []
    exatos = [p for p in itens
              if str(p.get("sku") or p.get("codigo") or "").strip().lower() == sku.strip().lower()]
    return {"sku": sku, "encontrados": len(itens),
            "exatos": exatos, "todos": itens if not exatos else None}


@mcp.tool()
async def estrutura_produto(id_produto: str) -> dict:
    """Retorna a estrutura (composicao) de um produto do tipo kit: quais produtos o
    formam e em que quantidade. Use depois de buscar_produto_por_sku para descobrir
    de que pecas um kit e feito.

    Tenta o endpoint dedicado de estrutura e, se ele nao existir, cai para os campos
    de composicao que vierem dentro do proprio produto.
    """
    est = await _tiny_request("GET", f"/produtos/{id_produto}/estrutura")
    if isinstance(est, dict) and not est.get("erro_http"):
        return {"id_produto": id_produto, "origem": "endpoint_estrutura", "estrutura": est}
    prod = await _tiny_request("GET", f"/produtos/{id_produto}")
    if isinstance(prod, dict) and prod.get("erro_http"):
        return {"id_produto": id_produto, "erro": prod}
    comp = None
    for chave in ("estrutura", "kit", "composicao", "produtosKit", "itensKit"):
        if prod.get(chave):
            comp = {chave: prod[chave]}
            break
    return {"id_produto": id_produto, "origem": "campos_do_produto",
            "tipo": prod.get("tipo"), "composicao": comp,
            "produto_bruto": prod if comp is None else None}


@mcp.tool()
async def listar_pedidos(situacao: str = "", data_inicial: str = "", data_final: str = "",
                         pagina: int = 1) -> dict:
    """Lista pedidos de venda. Datas no formato AAAA-MM-DD."""
    params = {"pagina": pagina}
    if situacao:
        params["situacao"] = situacao
    if data_inicial:
        params["dataInicial"] = data_inicial
    if data_final:
        params["dataFinal"] = data_final
    return await _tiny_request("GET", "/pedidos", params=params)


@mcp.tool()
async def obter_pedido(id_pedido: str) -> dict:
    """Retorna os detalhes completos de um pedido pelo ID."""
    return await _tiny_request("GET", f"/pedidos/{id_pedido}")


@mcp.tool()
async def atualizar_estoque(id_produto: str, quantidade: float, tipo: str = "B",
                            deposito: str = "", confirm: bool = False) -> dict:
    """Atualiza o estoque de um produto. tipo: 'B' saldo, 'E' entrada, 'S' saida.
    So grava com confirm=true."""
    body = {"produto": {"id": id_produto}, "tipo": tipo, "quantidade": quantidade}
    if deposito:
        body["deposito"] = {"nome": deposito}
    if not confirm:
        return {"previa": True, "aviso": "nada gravado — repita com confirm=true", "corpo": body}
    return await _tiny_request("POST", f"/estoque/{id_produto}", json=body)


@mcp.tool()
async def atualizar_preco(id_produto: str, preco: float, preco_promocional: float = None,
                          confirm: bool = False) -> dict:
    """Atualiza o preco de venda (e opcionalmente o promocional). So grava com confirm=true."""
    body = {"preco": preco}
    if preco_promocional is not None:
        body["precoPromocional"] = preco_promocional
    if not confirm:
        return {"previa": True, "aviso": "nada gravado — repita com confirm=true", "corpo": body}
    return await _tiny_request("PUT", f"/produtos/{id_produto}/preco", json=body)


@mcp.tool()
async def chamar_api_tiny(metodo: str, caminho: str, params: dict = None,
                          corpo: dict = None) -> dict:
    """Passagem direta para a API v3 da Olist/Tiny, para explorar endpoints ainda nao
    embrulhados numa ferramenta propria.

    metodo : GET, POST, PUT ou DELETE
    caminho: caminho relativo a https://api.tiny.com.br/public-api/v3
             ex. "/produtos", "/produtos/123/estrutura", "/pedidos"

    Metodos de escrita (POST, PUT, DELETE) exigem que o caminho seja passado
    explicitamente pelo usuario — nunca inferido.
    """
    m = (metodo or "GET").upper()
    if m not in ("GET", "POST", "PUT", "DELETE"):
        return {"erro": f"metodo invalido: {metodo}"}
    if not caminho.startswith("/"):
        caminho = "/" + caminho
    kwargs = {}
    if params:
        kwargs["params"] = params
    if corpo is not None:
        kwargs["json"] = corpo
    return await _tiny_request(m, caminho, **kwargs)


# --------------------------------------------------------------------------
# Rotas HTTP
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
        return HTMLResponse("<h1>Erro: codigo de autorizacao nao recebido.</h1>", status_code=400)
    await _exchange_code_for_tokens(code)
    return HTMLResponse("<h1>Conectado com sucesso a Olist/Tiny. Pode fechar esta aba.</h1>")


async def health(request):
    autorizado = TOKENS_PATH.exists()
    return JSONResponse({"status": "ok", "versao": "v2-sem-oauth", "olist_autorizado": autorizado})


async def sem_oauth(request):
    """As rotas de descoberta de OAuth respondem 404 de proposito: este servidor usa
    segredo fixo, nao OAuth. Sem isso o cliente MCP tenta registro dinamico e falha."""
    return JSONResponse({"erro": "este servidor nao usa OAuth"}, status_code=404)


PUBLIC_PATHS = {"/health", "/authorize", "/callback", "/favicon.ico"}


class SegredoMiddleware(BaseHTTPMiddleware):
    """Protege as ferramentas MCP com um segredo fixo, aceito de duas formas:
      - cabecalho  Authorization: Bearer <MCP_SECRET>
      - parametro  ?t=<MCP_SECRET>   (e o que permite conectar pela claude.ai)

    Responde 403 quando falta o segredo. Nunca 401: o 401 dispara a descoberta de
    OAuth no cliente, que e exatamente o que queremos evitar aqui.
    """

    async def dispatch(self, request, call_next):
        caminho = request.url.path
        if caminho in PUBLIC_PATHS or caminho.startswith("/.well-known") or caminho.startswith("/oauth"):
            return await call_next(request)
        if not MCP_SECRET:
            return PlainTextResponse("MCP_SECRET nao configurado no servidor.", status_code=500)
        cabecalho = request.headers.get("authorization", "")
        na_url = request.query_params.get("t", "")
        if cabecalho == f"Bearer {MCP_SECRET}" or na_url == MCP_SECRET:
            return await call_next(request)
        return PlainTextResponse("Acesso negado: segredo ausente ou invalido.", status_code=403)


from contextlib import asynccontextmanager

_mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app):
    """Sobe o gerenciador de sessoes do MCP junto com a aplicacao. Sem isso o /mcp
    responde 'Task group is not initialized' na primeira requisicao, porque o Mount
    do Starlette nao propaga o lifespan da sub-aplicacao."""
    gerenciador = getattr(mcp, "session_manager", None)
    if gerenciador is None:  # SDK antigo: cai para o lifespan da sub-aplicacao
        async with _mcp_app.router.lifespan_context(_mcp_app):
            yield
        return
    async with gerenciador.run():
        yield


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/authorize", authorize),
        Route("/callback", callback),
        Route("/health", health),
        Route("/.well-known/oauth-authorization-server", sem_oauth),
        Route("/.well-known/oauth-protected-resource", sem_oauth),
        Route("/.well-known/openid-configuration", sem_oauth),
        Route("/oauth/authorize", sem_oauth),
        Route("/oauth/token", sem_oauth),
        Route("/oauth/register", sem_oauth, methods=["GET", "POST"]),
        Route("/register", sem_oauth, methods=["GET", "POST"]),
    ]
)
app.add_middleware(SegredoMiddleware)
app.mount("/", _mcp_app)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
