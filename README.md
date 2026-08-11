# olist-mcp-server

Conector MCP dedicado à API Olist/Tiny (ERP) da Pliar — pedidos, produtos,
estoque e preço. Código aberto e legível de propósito (nada escondido em
variáveis codificadas), pra ser auditável por qualquer pessoa da equipe.

## Variáveis de ambiente necessárias

- `TINY_CLIENT_ID` — Client ID do app criado em Olist > Configurações > Aplicativos API.
- `TINY_CLIENT_SECRET` — Client Secret do mesmo app.
- `PUBLIC_URL` — URL pública deste serviço no Railway (ex: `https://olist-mcp-pliar-production.up.railway.app`), **sem barra no final**.
- `MCP_SECRET` — token secreto qualquer (gerado por você) usado para proteger o endpoint MCP.
- `DATA_DIR` — opcional, caminho para persistir o token (default `/app/data`, deve estar num volume).
- `PORT` — definido automaticamente pelo Railway.

## Configuração no painel da Olist

Em Configurações > Aplicativos API, o campo **URL de Redirecionamento**
precisa ser `PUBLIC_URL` + `/callback` (ex:
`https://olist-mcp-pliar-production.up.railway.app/callback`). Atualize isso
depois que o domínio do Railway existir.

Nas **Permissões do aplicativo**, habilite leitura e edição pelo menos em:
Produtos, Vendas/Pedidos e Estoque.

## Autorização (uma vez, depois de cada deploy inicial)

1. Acesse `PUBLIC_URL/authorize` no navegador.
2. Faça login na Olist e autorize o app.
3. Você será redirecionado para `/callback`, que salva o token de acesso e
   o refresh token num volume persistente. A partir daí o token se renova
   solozinho.

## Rodando localmente

```bash
pip install -r requirements.txt
export TINY_CLIENT_ID=... TINY_CLIENT_SECRET=... PUBLIC_URL=http://localhost:8080 MCP_SECRET=troque-isso
python server.py
```

## Observação importante

Os endpoints de listagem de pedidos/produtos e os de atualização de estoque
e preço foram implementados seguindo a convenção pública da API v3 da
Tiny/Olist, mas ainda não foram validados contra a documentação oficial
(pendente de o link ser compartilhado). Eles estão marcados com
`# validar contra docs` no `server.py` — ajustar os nomes de campos/rota se
necessário depois do primeiro teste real.
