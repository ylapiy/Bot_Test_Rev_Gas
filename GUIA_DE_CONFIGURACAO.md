# Guia de configuração — o que preencher, passo a passo

## Comece aqui

1. Para testar no simulador, mantenha `WHATSAPP_PROVEDOR=nenhum`, `ERP_MODO=mock`, `ATENDIMENTO_MODO=auto` e `LLM_PROVEDOR=nenhum`. Siga a Etapa 0. Os registros, preços e pedidos da demonstração são fictícios.
2. Para testar a Evolution, configure `WHATSAPP_PROVEDOR=evolution`, `EVOLUTION_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCIA` e um `EVOLUTION_WEBHOOK_TOKEN` próprio. Na instância, aponte o evento `MESSAGES_UPSERT` para `/webhooks/evolution` no servidor do bot e envie o token no cabeçalho `X-Webhook-Token`. Mantenha `WHATSAPP_ENVIO_REAL=false` para conferir as respostas na aba **Saída WhatsApp** do simulador.
3. Ao ativar `WHATSAPP_ENVIO_REAL=true` na Evolution com `ERP_MODO=mock`, o bot encaminha as opções do menu e as solicitações reconhecidas pela IA à fila de atendimento humano. A equipe acompanha em `/atendente`. As consultas, cotações e confirmações reais dependem da integração descrita na Etapa 5.
4. Para testar texto livre, mantenha `ATENDIMENTO_MODO=auto` e preencha o provedor de IA na Etapa 8. Desative a IA e repita o teste pelo menu. A queda do provedor durante um fluxo já aberto preserva a etapa atual.

As etapas da Cloud API e os dados da Meta abaixo servem de referência para o adaptador oficial existente. A configuração da Evolution usa somente os campos acima.

Este guia diz **exatamente o que vocês precisam preencher** para o bot funcionar com o WhatsApp, o ERP RevGás e o conteúdo real da empresa.

- **Parte A** — o que vocês precisam **fornecer** e as **decisões que dependem da aprovação de vocês** (nada disso foi ligado sem vocês).
- **Parte B** — passo a passo de cada arquivo/campo.
- **Parte C** — tabela de referência de todas as variáveis do `.env`.

Onde se preenche:

| Arquivo | Para quê |
|---|---|
| `.env` (cópia de `.env.example`) | chaves, senhas, endereços e ligar/desligar recursos |
| `config/regras_comerciais.json` | regras que o bot é obrigado a seguir (taxa, limites, prazos, promoções) |
| `config/erp_revgas.json` | IDs, bairros e preços por estabelecimento RevGás |
| `config/sql_permissoes.json` | o que o Text-to-SQL pode consultar no banco |
| `knowledge/*.md` | catálogo, políticas, garantia, FAQ, manual de segurança |

> Dica: depois de cada etapa, rode `python scripts/verificar_config.py` — ele diz em português o que ainda falta.

---

## PARTE A — O que precisamos de vocês

### A1. Itens a fornecer

| # | Item | Quem normalmente tem | Bloqueia o quê |
|---|---|---|---|
| 1 | **Chave da API RevGás** e **conta de teste autorizada para vendas** | fornecedor do RevGás / TI | Validar acesso aos dados e gravação na central de vendas |
| 2 | IDs dos produtos, **preços aprovados** e **bairros atendidos** por estabelecimento | operação da loja | Cotação e roteamento de entrega |
| 3 | Se o RevGás/App Entrega consegue **chamar uma URL quando o status do pedido muda** (webhook) | fornecedor / App Entrega | Avisos automáticos "saiu para entrega" e "entregue" |
| 4 | Acesso à conta **Meta Business** (Business Manager) com permissão de administrador | dono/marketing | API oficial do WhatsApp |
| 5 | O **número de WhatsApp** que será usado no bot e onde ele está hoje (WhatsApp comum, WhatsApp Business no celular, outro provedor) | operação | API oficial do WhatsApp |
| 6 | **Servidor com HTTPS** (ou autorização para contratarmos um) e um domínio/subdomínio (ex.: `bot.suaempresa.com.br`) | TI | Webhook da Meta (exige HTTPS público) |
| 7 | Conteúdo real: **catálogo, política de troca/devolução, garantia, FAQ** (horários, bairros, formas de pagamento, taxa) | comercial / operação | Respostas do RAG (hoje são exemplos) |
| 8 | **Manual de segurança revisado** pelo responsável técnico da revenda | responsável técnico | Orientações de emergência |
| 9 | **Regras comerciais**: taxa de entrega, limites por pedido, prazo de troca, promoções vigentes, formas de pagamento por tipo de cliente | comercial | Guardrails de preço/desconto |
| 10 | **Bairros atendidos por filial** e **prazo de entrega por filial** | operação | Definição da filial e da previsão |
| 11 | Quem atende as transferências e **horário do atendimento humano**; opcional: telefone de plantão para emergência | operação | Handoff |
| 12 | (Opcional) Chave de IA (Anthropic ou OpenAI) | financeiro/TI | Recursos de IA (seção 9 do README) |
| 13 | (Opcional) Usuário **somente leitura** no banco + views para histórico | TI / fornecedor | Text-to-SQL em dados reais |

### A2. Decisões que precisam da aprovação de vocês

Nada abaixo foi ligado/definido de forma definitiva — está com o padrão indicado até vocês decidirem.

| # | Decisão | Padrão atual | Nossa recomendação |
|---|---|---|---|
| D1 | Configurar a ponte Evolution | **Desligada** | Validar primeiro com envio real desligado. A ponte usa WhatsApp Web e pode levar a restrições na conta. |
| D2 | Ligar a IA para interpretar mensagens livres | **Desligada** (menu guiado) | Validar primeiro o menu. Considere o custo por uso e o envio de contexto ao provedor. |
| D3 | Exigir **CPF/CNPJ para cadastrar cliente novo** pelo WhatsApp | **Exige** | Manter (nota fiscal e segurança) — confirmar com o fiscal |
| D4 | Identidade: **telefone do WhatsApp = cliente verificado**; número diferente precisa de **CPF + CEP** do cadastro | Ativo | Manter |
| D5 | **Fora do horário**, o bot continua atendendo e a transferência fica aberta para o dia seguinte (emergência: orienta e manda ligar 193) | Ativo | Manter e informar um **telefone de plantão** se existir |
| D6 | Mostrar estoque como **disponível/indisponível** (sem quantidade) | Ativo | Manter |
| D7 | Cancelamento pelo WhatsApp só até **"em separação"**; depois disso vai para atendente | Ativo | Manter |
| D8 | Pagamento **na entrega** (Pix/dinheiro/cartão; boleto só PJ aprovado). O bot não gera cobrança | Ativo | Pix antecipado exige integração com banco — avaliar depois |
| D9 | O bot **não concede desconto**; pedidos de desconto vão para o comercial | Ativo | Manter |
| D10 | Para avisar status fora da janela de 24h é preciso **template aprovado pela Meta** (pode ter custo por conversa iniciada pela empresa) | Sem template = só avisa dentro de 24h | Criar o template `atualizacao_pedido` (Etapa 6.11) |
| D11 | Por quanto tempo **guardar as conversas** (LGPD) | Sem exclusão automática | Definir prazo (ex.: 180 dias); a limpeza automática é simples de adicionar depois da decisão |
| D12 | Onde hospedar (VPS, nuvem) | — | Qualquer servidor com Docker + HTTPS |

---

## PARTE B — Passo a passo

### Etapa 0 — Rodar a demonstração (antes de tudo)
1. Instale o Python 3.11+.
2. No terminal, dentro da pasta do projeto:
   ```bash
   python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env                                    # Windows: copy .env.example .env
   uvicorn app.main:app --port 8000
   ```
3. Abra `http://localhost:8000/simulador` e converse. Valide o comportamento com a equipe **antes** de configurar o resto — ajustes de fluxo são mais baratos agora.

### Etapa 1 — Dados da empresa (`.env`)
| Campo | O que colocar | Exemplo |
|---|---|---|
| `EMPRESA_NOME` | Nome que o cliente vê | `Gás Bom Preço` |
| `BOT_NOME` | Nome da assistente | `Gabi` |
| `PRAZO_ENTREGA_MINUTOS` | Prazo padrão (cada filial pode ter o seu no ERP) | `20` |
| `HORARIO_ATENDIMENTO_INICIO` / `_FIM` | Horário dos **atendentes humanos** | `08:00` / `20:00` |
| `DIAS_ATENDIMENTO` | Dias com atendente: `seg,ter,qua,qui,sex,sab,dom` | `seg,ter,qua,qui,sex,sab` |
| `TELEFONE_PLANTAO_EMERGENCIA` | (opcional) telefone 24h para emergências | `(11) 99999-0000` |

### Etapa 2 — Segurança do painel (`.env`)
| Campo | O que colocar |
|---|---|
| `PAINEL_ATENDENTE_SENHA` | Senha forte do painel `/atendente` (troque o padrão!) |
| `SIMULADOR_ATIVO` | `true` em testes; **`false` em produção** |

### Etapa 3 — Base de conhecimento (`knowledge/*.md`)
Os arquivos atuais são **exemplos** (marcados com ⚠️). Substitua pelo conteúdo real mantendo o formato:
```markdown
---
categoria: politica          # catalogo | politica | garantia | faq | manual | regras
titulo: Política de troca e devolução
---
## Quando posso trocar o botijão
Texto curto e objetivo...

## Botijão com vazamento
...
```
Regras para o bot responder bem:
- **Uma pergunta = uma seção `##`** com título claro (o título pesa na busca).
- Frases diretas, com os termos que o cliente usa ("botijão", "gás", "troco", "frete").
- **Não coloque preços** nos documentos (o bot sempre busca o preço no ERP).
- Pode criar novos arquivos `.md`; reinicie o bot para reindexar.

### Etapa 4 — Regras comerciais (`config/regras_comerciais.json`)
| Campo | Significado | Exemplo |
|---|---|---|
| `taxa_entrega` | Taxa somada ao pedido | `0.0` |
| `formas_pagamento` | Formas aceitas (código → nome exibido) | manter códigos `pix`, `dinheiro`, `cartao_debito`, `cartao_credito`, `boleto` |
| `formas_pagamento_somente_pj` | Formas exclusivas de CNPJ | `["boleto"]` |
| `quantidade_maxima_por_item` | Acima disso, vai para o comercial | `{"P13": 5, "P45": 4}` |
| `prazo_troca_dias` | Prazo da política de troca | `7` |
| `promocoes_vigentes` | Únicas promoções que o bot pode citar (`ativa: true/false`) | ver arquivo |
| `chave_pix_exibida` | (opcional) chave Pix mostrada após o pedido | `pix@empresa.com.br` |
| `mensagem_boas_vindas_extra` | (opcional) aviso na saudação | `Hoje entregamos até as 22h!` |
| `desconto_maximo_bot_percentual` | Deixe `0` (o bot não dá desconto) | `0` |

### Etapa 5 — ERP RevGás

1. No `.env` da instância, configure `ERP_MODO=revgas`, `ERP_BASE_URL=https://api.revgas.com/v1` e `ERP_TOKEN` com a chave fornecida pelo suporte da RevGás. A API recebe a chave diretamente em `Authorization`. [Autenticação da RevGás](https://revgas.stoplight.io/docs/revgas-api/en39g0vb0qwsb-autenticacao-e-permissao)
2. Rode `python scripts/verificar_config.py --online` para conferir a chave, ver os IDs dos estabelecimentos e listar os produtos ativos. O diagnóstico usa consultas de leitura. A checagem de configuração fica pendente até que a tabela abaixo seja preenchida.
3. Preencha `config/erp_revgas.json` com a área atendida e o preço unitário aprovado para cada código do bot. Os IDs e valores abaixo são exemplos de preenchimento.

```json
{
  "filiais": {
    "42": {
      "bairros": ["Centro", "Vila Nova"],
      "produtos": {
        "P13": {"id": 101, "preco": 120.00},
        "AGUA20": {"id": 205, "preco": 15.00}
      }
    }
  }
}
```

Por padrão, o modo RevGás usa o banco local `data/bot-revgas.db` para conversas, histórico e atendimentos. Se definir `BOT_DB_PATH` manualmente, use um banco exclusivo da operação real.

O bot usa o estoque e os IDs dos produtos retornados pela API. O contrato público de produtos não expõe o preço de venda. Mantenha os preços aprovados da loja nesse arquivo e reinicie o bot após uma alteração. A venda automática suporta Pix, dinheiro e cartão conforme as formas ativas da filial. A equipe trata trocas, compra com boleto e pedidos com taxa de entrega fora de zero. Para segunda via, o bot consulta faturas já emitidas e verifica o titular antes de buscar o link. Sem um período explícito, as listagens de vendas e boletos consultam os últimos 365 dias.

Com o catálogo preenchido, rode novamente o diagnóstico e valide pelo WhatsApp de teste a identificação, a consulta de preço e estoque, a confirmação do pedido e o registro da venda na central. O `POST /sales` na URL de produção grava uma venda. Combine com a RevGás uma conta de teste para homologar as escritas. A especificação pública marca o servidor de homologação listado como “Não utilizar”. [Contrato RevGás v1](https://revgas.stoplight.io/docs/revgas-api/9ba9854ecfbc9-rev-gas)

**Avisos de status.** O bot aceita `POST /webhooks/erp/status` com `X-Webhook-Token` igual a `ERP_WEBHOOK_TOKEN` e JSON `{"numero_pedido":"PED-123"}`. Configure o envio em uma ponte compatível se a operação contar com esse serviço. O contrato público de vendas consultado não inclui webhook de entrega.

### Etapa 6 — WhatsApp oficial (Cloud API da Meta)
> Os nomes dos menus da Meta mudam com frequência; se algum não bater, procure pelo nome em inglês entre parênteses.

**Antes de começar:** o número escolhido **não pode estar ativo** no WhatsApp do celular. Opções: (a) usar um número novo; (b) apagar a conta do WhatsApp/WhatsApp Business desse número (perde o histórico); ou (c) usar a **coexistência** (WhatsApp Business no celular + API no mesmo número), que hoje é feita por meio de um parceiro (BSP) que ofereça esse cadastro.

1. **Business Manager** (business.facebook.com): confirme que a empresa existe e, de preferência, está **verificada** (Central de Segurança → Verificação da empresa). Sem verificação há limites menores de envio.
2. **Criar o app**: developers.facebook.com → Meus apps → Criar app → tipo **Empresa (Business)** → vincule ao Business Manager → adicione o produto **WhatsApp**.
3. **Adicionar o número**: WhatsApp → Configuração da API (*API Setup*) → Adicionar número de telefone → nome de exibição, verificação por SMS/ligação. (O número de teste da Meta serve para testes, mas só envia para até 5 números cadastrados.)
4. **Copiar para o `.env`** (na mesma tela *API Setup*):
   - `WHATSAPP_PHONE_NUMBER_ID` = "Identificação do número de telefone" (*Phone number ID*)
   - `WHATSAPP_BUSINESS_ACCOUNT_ID` = "Identificação da conta do WhatsApp Business" (*WhatsApp Business Account ID*)
5. **Chave secreta do app**: Configurações do app → Básico → **Chave secreta do aplicativo** (*App secret*) → `WHATSAPP_APP_SECRET`.
6. **Token permanente** (o token da tela de testes expira em ~24h):
   Configurações do negócio → Usuários → **Usuários do sistema** → Adicionar (Admin) → Atribuir ativos: o **app** e a **conta do WhatsApp** (controle total) → **Gerar token** → escolha o app, validade **Nunca**, permissões `whatsapp_business_messaging` e `whatsapp_business_management` → cole em `WHATSAPP_TOKEN`.
7. **Inventar o token de verificação**: crie uma senha longa qualquer e coloque em `WHATSAPP_VERIFY_TOKEN` (você vai digitar a mesma na Meta).
8. **Publicar o bot com HTTPS** (Etapa 10 / README seção 12). Para testar do seu computador: `ngrok http 8000` (ou `cloudflared tunnel --url http://localhost:8000`) e use a URL `https://...` gerada.
9. **Configurar o webhook**: WhatsApp → Configuração (*Configuration*) → Webhook → Editar:
   - URL de callback: `https://SEU-DOMINIO/webhooks/whatsapp`
   - Token de verificação: o mesmo de `WHATSAPP_VERIFY_TOKEN`
   - Em **Campos do webhook**, assine **`messages`**.
   (Antes, teste localmente: `python scripts/simular_webhook.py --verificar`.)
10. **Inscrever o app na conta WhatsApp** (causa nº 1 de "não chegam mensagens"): `python scripts/testar_whatsapp.py --assinar-waba`.
11. **Template para avisos de status** (fora da janela de 24h): WhatsApp Manager → Modelos de mensagem → Criar → categoria **Utilidade**, idioma **Português (BR)**, nome `atualizacao_pedido`, corpo com 2 variáveis, ex.: `Seu pedido {{1}} está: {{2}}.` → após aprovado, `WHATSAPP_TEMPLATE_STATUS_PEDIDO=atualizacao_pedido`.
12. **Modo Live**: no app, preencha a URL da **política de privacidade** e mude para **Live/Publicado** (algumas notificações não chegam em modo desenvolvimento).
13. **Forma de pagamento**: cadastre no Business Manager (conta do WhatsApp → Pagamentos), necessária para conversas iniciadas pela empresa.
14. **Ligar no `.env`**:
    ```
    WHATSAPP_PROVEDOR=cloud
    WHATSAPP_ENVIO_REAL=false     # 1º: sandbox — recebe de verdade, responde só no simulador
    ```
    Mande "oi" do seu celular para o número e veja a resposta na aba *Saída WhatsApp (sandbox)* do simulador. Estando tudo certo:
    ```
    WHATSAPP_ENVIO_REAL=true
    ```
15. **Conferir**: `python scripts/testar_whatsapp.py --diagnostico` e `python scripts/verificar_config.py --online`.

Se travar em algum erro, veja a tabela "Problemas comuns" no README (seção 13) — o bot também escreve no log a explicação em português de cada código de erro da Meta.

### Etapa 7 — Text-to-SQL em banco real (opcional)
1. Peça ao TI/fornecedor um **usuário somente leitura** com acesso só às tabelas/views necessárias.
2. `.env`: `SQL_URL=postgresql+psycopg://usuario:senha@host:5432/banco` (ou o equivalente do banco usado), `SQL_DIALETO=postgres` (ou `tsql`, `mysql`...). Instale: `pip install sqlalchemy psycopg[binary]`.
3. Em `config/sql_permissoes.json`, reescreva cada `fonte` com o SQL real que **filtra pelo cliente** (`:cliente_id`) e liste só as colunas permitidas, com descrição clara (a IA usa essas descrições).

### Etapa 8 — IA (opcional, depende da decisão D2)

Para usar a OpenAI somente como intérprete, use `ATENDIMENTO_MODO=auto`, `LLM_PROVEDOR=openai`, `OPENAI_BASE_URL=https://api.openai.com/v1` e `LLM_MODELO=gpt-4.1-mini`. Preencha `OPENAI_API_KEY` no `.env` local e reinicie o bot. O interpretador seleciona uma opção do menu e o fluxo envia as mensagens prontas. Sem chave ou se a classificação falhar, o menu continua disponível.

| Campo | O que colocar |
|---|---|
| `LLM_PROVEDOR` | `anthropic` ou `openai` |
| `ANTHROPIC_API_KEY` | chave em console.anthropic.com (se Anthropic) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | se OpenAI ou compatível |
| `LLM_MODELO` / `LLM_MODELO_RAPIDO` | Anthropic: pode deixar vazio (padrão `claude-sonnet-5-5` e `claude-haiku-5-5`). OpenAI: obrigatório informar |
| `EMBEDDINGS_PROVEDOR` | `local` (padrão) ou `openai` para busca semântica melhor (+ `EMBEDDINGS_API_KEY`) |

### Etapa 9 — Atendimento humano
| Campo | O que colocar |
|---|---|
| `HANDOFF_WEBHOOK_URL` | (opcional) URL que recebe cada transferência em JSON (central de pedidos, Slack via n8n/Zapier...) |
| `FALHAS_PARA_TRANSFERIR` | Quantas mensagens não entendidas seguidas antes de transferir (padrão 2) |
| `HANDOFF_ESPERA_MAXIMA_MINUTOS` | Se ninguém assumir nesse tempo, o bot volta a ajudar (padrão 10) |

Os atendentes usam `https://SEU-DOMINIO/atendente` com a senha da Etapa 2.

### Etapa 10 — Conferência final (go-live)
```bash
python -m pytest -q
python scripts/verificar_config.py --online      # confira se há linhas [ERRO]
```
- [ ] Conteúdo real nos `knowledge/*.md` e regras reais no JSON
- [ ] Para automatizar as operações do ERP, valide em homologação um pedido completo, status, boleto e troca.
- [ ] Valide o webhook Evolution e a saída no sandbox antes de ativar `WHATSAPP_ENVIO_REAL=true`.
- [ ] `SIMULADOR_ATIVO=false` e senha do painel trocada
- [ ] Equipe treinada no painel `/atendente`

---

## PARTE C — Referência de todas as variáveis do `.env`

| Variável | Obrigatória? | Onde conseguir / o que é |
|---|---|---|
| `EMPRESA_NOME`, `BOT_NOME` | sim | vocês definem |
| `PRAZO_ENTREGA_MINUTOS` | sim | operação (padrão 20) |
| `HORARIO_ATENDIMENTO_INICIO/FIM`, `DIAS_ATENDIMENTO` | sim | operação |
| `FUSO_HORARIO` | sim | `America/Sao_Paulo` (ou o da região) |
| `TELEFONE_PLANTAO_EMERGENCIA` | não | operação |
| `PAINEL_ATENDENTE_SENHA` | **sim** | vocês definem (forte) |
| `SIMULADOR_ATIVO` | sim | `false` em produção |
| `WHATSAPP_PROVEDOR` | sim | `nenhum` / `cloud` / `evolution` (D1) |
| `WHATSAPP_ENVIO_REAL` | sim | `false` (sandbox) → `true` |
| `WHATSAPP_TOKEN` | p/ cloud | Etapa 6.6 (token permanente) |
| `WHATSAPP_PHONE_NUMBER_ID` | p/ cloud | Etapa 6.4 |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | p/ cloud | Etapa 6.4 |
| `WHATSAPP_APP_SECRET` | p/ cloud | Etapa 6.5 |
| `WHATSAPP_VERIFY_TOKEN` | p/ cloud | vocês inventam (Etapa 6.7) |
| `WHATSAPP_API_VERSION` | não | padrão `v26.0` |
| `WHATSAPP_TEMPLATE_STATUS_PEDIDO`, `WHATSAPP_TEMPLATE_IDIOMA` | recomendado | Etapa 6.11 |
| `WHATSAPP_MARCAR_COMO_LIDO` | não | `true` |
| `EVOLUTION_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCIA`, `EVOLUTION_WEBHOOK_TOKEN` | só se D1 aprovada | servidor Evolution |
| `LLM_PROVEDOR`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODELO`, `LLM_MODELO_RAPIDO` | só se D2 aprovada | Etapa 8 |
| `ATENDIMENTO_MODO` | opcional | `auto` (padrão) ou `legado` para os fluxos anteriores |
| `LLM_TIMEOUT_SEGUNDOS`, `AGENTE_MAX_PASSOS`, `RERANK_COM_LLM` | não | ajustes finos da IA |
| `EMBEDDINGS_PROVEDOR`, `EMBEDDINGS_MODELO`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_BASE_URL` | não | Etapa 8 |
| `ERP_MODO` | sim | `mock` → `revgas` |
| `ERP_BASE_URL`, `ERP_TOKEN` | p/ revgas | fornecedor do RevGás (Etapa 5) |
| `ERP_TIMEOUT_SEGUNDOS` | não | padrão 15 |
| `ERP_WEBHOOK_TOKEN` | recomendado | vocês inventam (Etapa 5.3) |
| `SQL_URL`, `SQL_DIALETO`, `SQL_TIMEOUT_SEGUNDOS` | não | Etapa 7 |
| `HANDOFF_WEBHOOK_URL`, `FALHAS_PARA_TRANSFERIR`, `HANDOFF_ESPERA_MAXIMA_MINUTOS` | não | Etapa 9 |
| `MEMORIA_EXPIRA_MINUTOS`, `MEMORIA_MAX_MENSAGENS` | não | padrão 60 / 20 |
| `LOG_NIVEL` | não | `INFO` |
