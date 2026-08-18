"""
Conector MCP para a API Olist/Tiny (ERP) - Pliar
==================================================

Servidor MCP remoto (HTTP) que expõe ferramentas de leitura e escrita sobre
a API pública v3 da Tiny/Olist (pedidos, produtos, estoque, preço, contatos
e ordem de compra), para uso pelo Claude como um conector custom.

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

Ordem de compra e contatos (adicionado em 17/08/2026, deployment da Plana):
confirmados contra a documentação oficial https://api-docs.erp.olist.com/
(seções "Ordem de Compra" e "Contatos"), não são mais "validar contra docs".
"""

import base64
import contextlib
import datetime
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
from mcp.server.transport_security import TransportSecuritySettings

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

mcp = FastMCP(
    "olist-tiny-pliar",
    # Sem isso, o SDK do MCP só aceita requisições com Host: localhost —
    # em produção, atrás do domínio real do Railway, toda chamada do
    # Claude cai com 421 "Invalid Host header". A proteção contra DNS
    # rebinding não se aplica aqui (não é um servidor local de dev) e já
    # temos o BearerAuthMiddleware protegendo o acesso.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def listar_pedidos(situacao: str = "", data_inicial: str = "", data_final: str = "", pagina: int = 1) -> dict:
    """Lista pedidos de venda na Olist/Tiny. Filtros opcionais: situacao
    (ex: 'aberto', 'aprovado', 'faturado'), data_inicial e data_final
    (formato AAAA-MM-DD), pagina para paginação.

    Confirmado em produção: a API da Tiny/Olist EXIGE um período de datas
    para listar pedidos (sem isso ela responde 400 Bad Request). Se
    data_inicial/data_final não forem informados, usamos como padrão os
    últimos 30 dias até hoje, pra essa ferramenta funcionar mesmo sem
    filtro explícito de data.
    """
    if not data_inicial and not data_final:
        hoje = datetime.date.today()
        data_final = hoje.isoformat()
        data_inicial = (hoje - datetime.timedelta(days=30)).isoformat()
    params = {"pagina": pagina}
    if situacao:
        params["situacao"] = situacao
    if data_inicial:
        params["dataInicial"] = data_inicial
    if data_final:
        params["dataFinal"] = data_final
    return await _tiny_request("GET", "/pedidos", params=params)  # confirmado: exige dataInicial/dataFinal


@mcp.tool()
async def obter_pedido(id_pedido: str) -> dict:
    """Retorna os detalhes completos de um pedido específico pelo ID."""
    return await _tiny_request("GET", f"/pedidos/{id_pedido}")


@mcp.tool()
async def listar_produtos(pesquisa: str = "", codigo: str = "", pagina: int = 1,
                          limite: int = 100) -> dict:
    """Lista produtos cadastrados na Olist/Tiny.

    pesquisa : filtra pelo nome do produto
    codigo   : filtra pelo SKU exato (o mesmo codigo usado no anuncio do ML)
    pagina   : 1, 2, 3... (ate 100 produtos por pagina)
    limite   : quantos por pagina, no maximo 100

    A API v3 da Tiny pagina por offset/limit e filtra por nome/codigo. Enviar
    "pagina"/"pesquisa" fazia a Tiny ignorar os dois e devolver sempre os
    mesmos 100 primeiros produtos, de um total de 1.339.
    """
    limite = max(1, min(limite, 100))
    params = {"limit": limite, "offset": (max(1, pagina) - 1) * limite}
    if pesquisa:
        params["nome"] = pesquisa
    if codigo:
        params["codigo"] = codigo
    return await _tiny_request("GET", "/produtos", params=params)


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
async def criar_produto(
    sku: str,
    descricao: str,
    tipo: str = "S",
    unidade: str = "",
    ncm: str = "",
    gtin: str = "",
    observacoes: str = "",
    id_categoria: str = "",
    id_marca: str = "",
    id_fornecedor: str = "",
    codigo_produto_no_fornecedor: str = "",
    preco: float = None,
    preco_promocional: float = None,
    preco_custo: float = None,
    estoque_inicial: float = None,
    estoque_minimo: float = None,
    estoque_maximo: float = None,
    localizacao: str = "",
    grade: list[str] = None,
    variacoes: list[dict] = None,
) -> dict:
    """Cria um novo produto na Olist/Tiny.

    Confirmado contra a documentação oficial
    https://api-docs.erp.olist.com/api-reference/produtos/criar-produto
    (POST /produtos).

    sku / descricao: obrigatórios sempre.
    tipo: 'S' Simples (default), 'K' Kit, 'V' Com Variações, 'F' Fabricado,
      'M' Matéria-prima. Kit/Fabricado precisam de parâmetros adicionais que
      esta ferramenta ainda não cobre (usar o painel da Tiny nesses casos).
    id_fornecedor: ID do contato fornecedor (use listar_contatos pra achar).
      Vincula o fornecedor ao produto (marcado como fornecedor padrão).
    grade: obrigatório quando tipo='V'. Lista das chaves da grade de
      variação, ex.: ["Cor"] ou ["Tamanho", "Cor"].
    variacoes: obrigatório quando tipo='V'. Lista de dicts, cada um com:
        {"sku": "...", "grade": [{"chave": "Cor", "valor": "Branco"}],
         "preco": 119.90, "estoque_inicial": 10, "gtin": "..." (opcional)}
      As chaves em cada "grade" de variação devem bater com o parâmetro
      `grade` do produto pai.

    Retorna: id, codigo e descricao do produto criado. Se tipo='V', também
      retorna a lista de variacoes criadas (cada uma com seu próprio id).
    """
    if tipo not in ("S", "K", "V", "F", "M"):
        raise ValueError("tipo deve ser um de: S, K, V, F, M")
    if tipo == "V" and (not grade or not variacoes):
        raise ValueError("tipo='V' exige 'grade' e 'variacoes' preenchidos.")

    body: dict = {"sku": sku, "descricao": descricao, "tipo": tipo}
    if unidade:
        body["unidade"] = unidade
    if ncm:
        body["ncm"] = ncm
    if gtin:
        body["gtin"] = gtin
    if observacoes:
        body["observacoes"] = observacoes
    if id_categoria:
        body["categoria"] = {"id": id_categoria}
    if id_marca:
        body["marca"] = {"id": id_marca}

    precos = {}
    if preco is not None:
        precos["preco"] = preco
    if preco_promocional is not None:
        precos["precoPromocional"] = preco_promocional
    if preco_custo is not None:
        precos["precoCusto"] = preco_custo
    if precos:
        body["precos"] = precos

    if id_fornecedor:
        fornecedor = {"id": id_fornecedor, "padrao": True}
        if codigo_produto_no_fornecedor:
            fornecedor["codigoProdutoNoFornecedor"] = codigo_produto_no_fornecedor
        body["fornecedores"] = [fornecedor]

    estoque = {}
    if estoque_inicial is not None:
        estoque["inicial"] = estoque_inicial
    if estoque_minimo is not None:
        estoque["minimo"] = estoque_minimo
    if estoque_maximo is not None:
        estoque["maximo"] = estoque_maximo
    if localizacao:
        estoque["localizacao"] = localizacao
    if estoque:
        body["estoque"] = estoque

    if tipo == "V":
        body["grade"] = grade
        body_variacoes = []
        for var in variacoes:
            if "sku" not in var or "grade" not in var:
                raise ValueError(f"Variação sem 'sku' ou 'grade': {var}")
            body_var: dict = {"sku": var["sku"], "grade": var["grade"]}
            if var.get("gtin"):
                body_var["gtin"] = var["gtin"]
            var_precos = {}
            if var.get("preco") is not None:
                var_precos["preco"] = var["preco"]
            if var.get("preco_promocional") is not None:
                var_precos["precoPromocional"] = var["preco_promocional"]
            if var_precos:
                body_var["precos"] = var_precos
            if var.get("estoque_inicial") is not None:
                body_var["estoque"] = {"inicial": var["estoque_inicial"]}
            body_variacoes.append(body_var)
        body["variacoes"] = body_variacoes

    return await _tiny_request("POST", "/produtos", json=body)


# --------------------------------------------------------------------------
# Contatos (fornecedores e clientes) — adicionado 17/08/2026, deployment Plana
# Endpoints confirmados contra https://api-docs.erp.olist.com/api-reference/contatos/
# --------------------------------------------------------------------------

@mcp.tool()
async def listar_contatos(nome: str = "", codigo: str = "", situacao: str = "", pagina: int = 1,
                          limite: int = 100) -> dict:
    """Lista contatos cadastrados na Olist/Tiny. A Tiny não separa clientes de
    fornecedores nessa listagem — use 'nome' pra procurar o fornecedor desejado
    antes de criar uma ordem de compra (precisa do id do contato).

    situacao: 'A' ativo, 'I' inativo, 'B' bloqueado, 'E' excluido.
    """
    limite = max(1, min(limite, 100))
    params = {"limit": limite, "offset": (max(1, pagina) - 1) * limite}
    if nome:
        params["nome"] = nome
    if codigo:
        params["codigo"] = codigo
    if situacao:
        params["situacao"] = situacao
    return await _tiny_request("GET", "/contatos", params=params)


@mcp.tool()
async def obter_contato(id_contato: str) -> dict:
    """Retorna os detalhes completos de um contato (cliente ou fornecedor) pelo ID,
    incluindo CPF/CNPJ, endereço e dados de contato."""
    return await _tiny_request("GET", f"/contatos/{id_contato}")


# --------------------------------------------------------------------------
# Ordem de compra — adicionado 17/08/2026, deployment Plana (Fase 1)
# Endpoints confirmados contra https://api-docs.erp.olist.com/api-reference/ordem-de-compra/
# --------------------------------------------------------------------------

@mcp.tool()
async def criar_ordem_compra(
    id_fornecedor: str,
    itens: list[dict],
    data: str = "",
    data_prevista: str = "",
    condicao: str = "",
    observacoes: str = "",
    observacoes_internas: str = "",
    frete_por_conta: str = "",
    transportador: str = "",
    frete: float = None,
    desconto: float = None,
    id_categoria: str = "",
) -> dict:
    """Cria uma ordem (pedido) de compra na Olist/Tiny para um fornecedor.

    id_fornecedor: ID do contato fornecedor (use listar_contatos/obter_contato
        pra achar o id antes de chamar isso).
    itens: lista de dicts, cada um com pelo menos:
        {"id_produto": "123", "quantidade": 10, "valor": 25.90}
      campos opcionais por item: "tipo" ("P" produto ou "S" servico, default
      produto), "informacoes_adicionais", "aliquota_ipi", "valor_icms".
    data / data_prevista: formato AAAA-MM-DD.
    frete_por_conta: 'R' remetente, 'D' destinatario, 'T' terceiros,
      '3' proprio remetente, '4' proprio destinatario, 'S' sem transporte.
    id_categoria: id da categoria financeira da ordem de compra (opcional).

    Retorna: id da ordem criada, numeroPedido, data e situacao
      ('0'=Em Aberto, '1'=Atendido, '2'=Cancelado, '3'=Em Andamento).
    """
    if not itens:
        raise ValueError("Informe pelo menos um item: {'id_produto', 'quantidade', 'valor'}.")

    body_itens = []
    for item in itens:
        if "id_produto" not in item:
            raise ValueError(f"Item sem 'id_produto': {item}")
        produto = {"id": item["id_produto"]}
        if item.get("tipo"):
            produto["tipo"] = item["tipo"]
        body_item = {"produto": produto}
        if "quantidade" in item:
            body_item["quantidade"] = item["quantidade"]
        if "valor" in item:
            body_item["valor"] = item["valor"]
        if item.get("informacoes_adicionais"):
            body_item["informacoesAdicionais"] = item["informacoes_adicionais"]
        if item.get("aliquota_ipi") is not None:
            body_item["aliquotaIPI"] = item["aliquota_ipi"]
        if item.get("valor_icms") is not None:
            body_item["valorICMS"] = item["valor_icms"]
        body_itens.append(body_item)

    body = {"contato": {"id": id_fornecedor}, "itens": body_itens}
    if data:
        body["data"] = data
    if data_prevista:
        body["dataPrevista"] = data_prevista
    if condicao:
        body["condicao"] = condicao
    if observacoes:
        body["observacoes"] = observacoes
    if observacoes_internas:
        body["observacoesInternas"] = observacoes_internas
    if frete_por_conta:
        body["fretePorConta"] = frete_por_conta
    if transportador:
        body["transportador"] = transportador
    if frete is not None:
        body["frete"] = frete
    if desconto is not None:
        body["desconto"] = desconto
    if id_categoria:
        body["categoria"] = {"id": id_categoria}

    return await _tiny_request("POST", "/ordem-compra", json=body)


@mcp.tool()
async def listar_ordens_compra(
    numero: str = "",
    data_inicial: str = "",
    data_final: str = "",
    situacao: str = "",
    nome_fornecedor: str = "",
    codigo_fornecedor: str = "",
    pagina: int = 1,
    limite: int = 100,
) -> dict:
    """Lista ordens de compra cadastradas na Olist/Tiny.
    situacao: '0' Em Aberto, '1' Atendido, '2' Cancelado, '3' Em Andamento.
    nome_fornecedor / codigo_fornecedor: filtra pelo fornecedor.
    """
    limite = max(1, min(limite, 100))
    params = {"limit": limite, "offset": (max(1, pagina) - 1) * limite}
    if numero:
        params["numero"] = numero
    if data_inicial:
        params["dataInicial"] = data_inicial
    if data_final:
        params["dataFinal"] = data_final
    if situacao:
        params["situacao"] = situacao
    if nome_fornecedor:
        params["nomeFornecedor"] = nome_fornecedor
    if codigo_fornecedor:
        params["codigoFornecedor"] = codigo_fornecedor
    return await _tiny_request("GET", "/ordem-compra", params=params)


@mcp.tool()
async def obter_ordem_compra(id_ordem_compra: str) -> dict:
    """Retorna os detalhes completos de uma ordem de compra pelo ID: itens,
    fornecedor, parcelas, frete e situação."""
    return await _tiny_request("GET", f"/ordem-compra/{id_ordem_compra}")


@mcp.tool()
async def atualizar_situacao_ordem_compra(id_ordem_compra: str, situacao: int) -> dict:
    """Atualiza a situação de uma ordem de compra existente.
    situacao: 0 Em Aberto, 1 Atendido, 2 Cancelado, 3 Em Andamento."""
    return await _tiny_request(
        "PUT", f"/ordem-compra/{id_ordem_compra}/situacao", json={"situacao": situacao}
    )


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
