"""Tool Calling seguro.

Cada ferramenta declara:
  - esquema dos parâmetros (o mesmo é enviado à IA)
  - nível de identificação exigido (0 público, 2 cliente verificado)
  - se a IA pode chamá-la diretamente (ações de escrita NUNCA são expostas à IA:
    ela só pode "preparar" a ação; o cliente confirma com um botão)
Proteções do executor: lista fechada de ferramentas, validação dos argumentos,
o cliente_id vem SEMPRE da sessão (nunca da IA), checagem de dono do pedido/boleto,
remoção de campos internos, auditoria e registro no rastreio.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.config import config
from app.core import guardrails
from app.core.memoria import Conversa
from app.core.modelos import Rastreio
from app.db import agora, auditar
from app.erp import ErroERP, obter_erp

log = logging.getLogger("ferramentas")


@dataclass
class Contexto:
    conv: Conversa
    rastreio: Rastreio
    llm: Any = None
    evidencias: set = field(default_factory=set)   # valores em R$ vindos do ERP neste turno
    pedidos_handoff: list = field(default_factory=list)
    acao_preparada: dict | None = None
    entidades: dict = field(default_factory=dict)
    intencao: str | None = None

    @property
    def cliente_id(self):
        return (self.conv.cliente or {}).get("id")


@dataclass
class Ferramenta:
    nome: str
    descricao: str
    parametros: dict
    funcao: Callable[[dict, Contexto], Any]
    nivel: int = 0
    exposta_ia: bool = True
    obrigatorios: list[str] = field(default_factory=list)

    def esquema(self) -> dict:
        return {"name": self.nome, "description": self.descricao,
                "input_schema": {"type": "object", "properties": self.parametros, "required": self.obrigatorios}}


# ======================================================================= funções
def _filial_padrao(ctx: Contexto, filial_id=None):
    return filial_id or ctx.conv.slots.get("filial_id") or (1 if config.erp_modo == "mock" else None)


def f_consultar_preco(a, ctx):
    erp = obter_erp()
    r = erp.consultar_preco(a["produto_codigo"].upper(), _filial_padrao(ctx, a.get("filial_id")))
    return r or {"erro": "Produto não encontrado no catálogo."}


def f_consultar_estoque(a, ctx):
    return {"estoque": obter_erp().consultar_estoque(a["produto_codigo"].upper(), a.get("filial_id"))}


def f_listar_produtos(a, ctx):
    return {"produtos": obter_erp().listar_produtos()}


def f_listar_filiais(a, ctx):
    return {"filiais": obter_erp().listar_filiais()}


def _pedido_do_cliente(numero: str | None, ctx: Contexto) -> dict | None:
    erp = obter_erp()
    if numero:
        p = erp.consultar_pedido(numero)
        # dono do pedido: nunca mostra pedido de outro cliente
        if p and str(p.get("cliente_id")) != str(ctx.cliente_id):
            auditar("acesso_negado_pedido_de_outro_cliente", {"pedido": numero}, ctx.conv.id)
            return None
        return p
    abertos = [p for p in erp.listar_pedidos(ctx.cliente_id, limite=5) if p["status"] not in ("cancelado",)]
    return abertos[0] if abertos else None


def f_consultar_pedido(a, ctx):
    p = _pedido_do_cliente(a.get("numero_pedido"), ctx)
    return p or {"erro": "Pedido não encontrado para este cliente."}


def f_listar_pedidos(a, ctx):
    return {"pedidos": obter_erp().listar_pedidos(ctx.cliente_id, a.get("inicio"), a.get("fim"), a.get("status"),
                                                  int(a.get("limite") or 5))}


def f_consultar_financeiro(a, ctx):
    return {"titulos": obter_erp().listar_titulos(ctx.cliente_id, a.get("status") or "em_aberto")}


def f_segunda_via(a, ctx):
    t = obter_erp().segunda_via(a["titulo_numero"], cliente_id=ctx.cliente_id)
    if not t or str(t.get("cliente_id")) != str(ctx.cliente_id):
        return {"erro": "Título não encontrado para este cliente."}
    return t


def f_buscar_documentos(a, ctx):
    from app.rag.hibrido import obter_busca
    from app.rag.reranker import reranquear

    from app.nlu.reescrita import expandir_para_busca

    consulta, extras = expandir_para_busca(a["consulta"])
    if extras:
        ctx.rastreio.add("Query Rewriting", "expansão de consulta com sinônimos do domínio (para o RAG)", extras)
    cats = [a["categoria"]] if a.get("categoria") else None
    candidatos = obter_busca().buscar(consulta, k=8, categorias=cats)
    ctx.rastreio.add("RAG híbrido", "BM25 + vetorial + RRF", [
        {"trecho": c["trecho"].id, "bm25": c["bm25"], "vetor": c["vetor"], "pos_bm25": c["pos_bm25"],
         "pos_vetor": c["pos_vetor"], "rrf": c["rrf"]} for c in candidatos])
    prods = [p["codigo"] for p in ctx.entidades.get("produtos", [])]
    top = reranquear(consulta, candidatos, a.get("intencao") or ctx.intencao or ctx.conv.slots.get("ultima_intencao"), prods,
                     llm=ctx.llm, top_n=int(a.get("top_n") or 3))
    ctx.rastreio.add("Reranking", "trechos escolhidos", [
        {"trecho": c["trecho"].id, "score": c["score"], "relevancia": c["relevancia"], "ia": c.get("score_llm")} for c in top])
    return {"trechos": [{"fonte": f"{c['trecho'].titulo} › {c['trecho'].secao}", "texto": c["trecho"].texto,
                         "relevancia": c["relevancia"]} for c in top]}


def f_consultar_historico_sql(a, ctx):
    from app.sql.text_to_sql import TextToSQL

    r = TextToSQL().responder(a["pergunta"], ctx.cliente_id, ctx.entidades, ctx.llm)
    ctx.rastreio.add("Text-to-SQL controlado", "geração/validação/execução", {
        "origem": r.get("origem"), "sql_gerado": r.get("sql_gerado"), "sql_executado": r.get("sql_executado"),
        "recusas": r.get("tentativas"), "linhas": len(r.get("linhas") or [])})
    if not r["ok"]:
        return {"erro": "Não consegui responder essa pergunta com segurança a partir do histórico."}
    return {"descricao": r.get("descricao"), "linhas": r["linhas"]}


def f_verificar_regras_troca(a, ctx):
    regras = config.json_config("regras_comerciais.json")
    prazo = int(regras.get("prazo_troca_dias", 7))
    p = _pedido_do_cliente(a.get("numero_pedido"), ctx) if a.get("numero_pedido") else None
    if not p:
        per = a.get("inicio") and {"inicio": a["inicio"], "fim": a.get("fim")} or (ctx.entidades.get("periodo") or {})
        entregues = [x for x in obter_erp().listar_pedidos(ctx.cliente_id, per.get("inicio"), per.get("fim"), limite=10)
                     if x["status"] == "entregue"]
        if not entregues and per:
            entregues = [x for x in obter_erp().listar_pedidos(ctx.cliente_id, limite=10) if x["status"] == "entregue"]
        prod = (a.get("produto_codigo") or "").upper()
        if prod and prod != "P13":  # "botijão" genérico vira P13; só filtra quando o produto é específico
            entregues = [x for x in entregues if any(i["produto_codigo"] == prod for i in x["itens"])]
            if not entregues:
                return {"elegivel": False, "motivo": f"Não encontrei pedido entregue com {prod} no seu cadastro.", "prazo_dias": prazo}
        p = entregues[0] if entregues else None
    if not p:
        return {"elegivel": False, "motivo": "Não encontrei pedido entregue para este cliente.", "prazo_dias": prazo}
    if p["status"] != "entregue":
        return {"elegivel": False, "pedido": p["numero"], "motivo": f"O pedido ainda está '{p['status_texto']}'.", "prazo_dias": prazo}
    data = datetime.strptime((p.get("entregue_em") or p["criado_em"])[:19], "%Y-%m-%d %H:%M:%S")
    dias = (agora().replace(tzinfo=None) - data).days
    return {"elegivel": dias <= prazo, "pedido": p["numero"], "dias_desde_entrega": dias, "prazo_dias": prazo,
            "itens": [f"{i['quantidade']}x {i['descricao']}" for i in p["itens"]],
            "obs": "Vazamento é troca prioritária independentemente do prazo."}


def f_solicitar_atendente(a, ctx):
    ctx.pedidos_handoff.append({"motivo": a.get("motivo") or "Solicitado pela IA", "prioridade": a.get("prioridade") or "normal"})
    return {"ok": True, "mensagem": "Transferência registrada; informe ao cliente que um atendente vai continuar."}


def f_preparar_acao(a, ctx):
    acao = a.get("acao")
    if acao not in ("abrir_troca", "cancelar_pedido"):
        return {"erro": "Ação não permitida."}
    ctx.acao_preparada = {"acao": acao, "numero_pedido": a.get("numero_pedido"), "produto_codigo": a.get("produto_codigo"),
                          "motivo": a.get("motivo") or ""}
    return {"ok": True, "mensagem": "Ação preparada. O cliente receberá botões para CONFIRMAR. Não diga que já foi feito."}


# --- ações de escrita (somente fluxos determinísticos, após confirmação) ---
def f_abrir_troca(a, ctx):
    return obter_erp().abrir_troca(ctx.cliente_id, a.get("numero_pedido"), a.get("produto_codigo"), a.get("motivo", ""))


def f_cancelar_pedido(a, ctx):
    p = _pedido_do_cliente(a["numero_pedido"], ctx)
    if not p:
        return {"erro": "Pedido não encontrado para este cliente."}
    return obter_erp().cancelar_pedido(p["numero"], a.get("motivo") or "Solicitado pelo cliente no WhatsApp")


def f_criar_pedido(a, ctx):
    return obter_erp().criar_pedido(ctx.cliente_id, a["endereco_id"], a["itens"], a["forma_pagamento"], a["filial_id"],
                                    a.get("troco_para"), a.get("observacao", ""), ctx.conv.canal,
                                    endereco_avulso=a.get("endereco_avulso"))


S, I, N = {"type": "string"}, {"type": "integer"}, {"type": "number"}
PROD = {"type": "string", "description": "Código: P13, P13C, P45, P20, P5, AGUA20 ou KITREG"}

FERRAMENTAS: dict[str, Ferramenta] = {f.nome: f for f in [
    Ferramenta("consultar_preco", "Preço atual de um produto na filial (fonte: ERP).", {"produto_codigo": PROD, "filial_id": I},
               f_consultar_preco, obrigatorios=["produto_codigo"]),
    Ferramenta("consultar_estoque", "Disponibilidade de um produto por filial (fonte: ERP).", {"produto_codigo": PROD, "filial_id": I},
               f_consultar_estoque, obrigatorios=["produto_codigo"]),
    Ferramenta("listar_produtos", "Catálogo de produtos ativos.", {}, f_listar_produtos),
    Ferramenta("listar_filiais", "Filiais, bairros atendidos e prazo de entrega.", {}, f_listar_filiais),
    Ferramenta("consultar_pedido", "Detalhes/status de um pedido do cliente atual. Sem número = pedido mais recente.",
               {"numero_pedido": {"type": "string", "description": "Ex.: PED-1003"}}, f_consultar_pedido, nivel=2),
    Ferramenta("listar_pedidos", "Pedidos do cliente atual (filtros opcionais).",
               {"inicio": {"type": "string", "description": "AAAA-MM-DD HH:MM:SS"}, "fim": S, "status": S, "limite": I},
               f_listar_pedidos, nivel=2),
    Ferramenta("consultar_financeiro", "Boletos/títulos do cliente atual. status: em_aberto | vencido | pago.", {"status": S},
               f_consultar_financeiro, nivel=2),
    Ferramenta("segunda_via_boleto", "Linha digitável e link de um boleto do cliente atual.", {"titulo_numero": S},
               f_segunda_via, nivel=2, obrigatorios=["titulo_numero"]),
    Ferramenta("buscar_documentos", "Busca na base de conhecimento (catálogo, políticas, garantia, manuais, FAQ, regras comerciais).",
               {"consulta": S, "categoria": {"type": "string", "enum": ["catalogo", "politica", "garantia", "faq", "manual", "regras"]}},
               f_buscar_documentos, obrigatorios=["consulta"]),
    Ferramenta("consultar_historico_sql", "Perguntas analíticas sobre o histórico do cliente atual (quantos pedidos, quanto gastou...).",
               {"pergunta": S}, f_consultar_historico_sql, nivel=2, obrigatorios=["pergunta"]),
    Ferramenta("verificar_regras_troca", "Verifica se um pedido entregue está dentro do prazo da política de troca.",
               {"numero_pedido": S, "produto_codigo": PROD, "inicio": {"type": "string", "description": "AAAA-MM-DD HH:MM:SS"},
                "fim": S}, f_verificar_regras_troca, nivel=2),
    Ferramenta("solicitar_atendente", "Transfere a conversa para um atendente humano.",
               {"motivo": S, "prioridade": {"type": "string", "enum": ["normal", "alta", "urgente"]}}, f_solicitar_atendente,
               obrigatorios=["motivo"]),
    Ferramenta("preparar_acao", "Prepara abertura de troca ou cancelamento; o cliente confirma por botão.",
               {"acao": {"type": "string", "enum": ["abrir_troca", "cancelar_pedido"]}, "numero_pedido": S,
                "produto_codigo": PROD, "motivo": S}, f_preparar_acao, nivel=2, obrigatorios=["acao"]),
    # escrita — não expostas à IA
    Ferramenta("abrir_troca", "Abre protocolo de troca no ERP.", {"numero_pedido": S, "produto_codigo": S, "motivo": S},
               f_abrir_troca, nivel=2, exposta_ia=False),
    Ferramenta("cancelar_pedido", "Cancela pedido no ERP.", {"numero_pedido": S, "motivo": S}, f_cancelar_pedido, nivel=2,
               exposta_ia=False, obrigatorios=["numero_pedido"]),
    Ferramenta("criar_pedido", "Cria pedido no ERP (central de pedidos).", {}, f_criar_pedido, nivel=2, exposta_ia=False),
]}

TIPOS = {"string": str, "integer": int, "number": (int, float)}


def esquemas_para_ia() -> list[dict]:
    return [f.esquema() for f in FERRAMENTAS.values() if f.exposta_ia]


def executar(nome: str, args: dict, ctx: Contexto, origem: str = "regras") -> dict:
    f = FERRAMENTAS.get(nome)
    if not f:
        return {"erro": f"Ferramenta inexistente: {nome}"}
    if origem == "ia" and not f.exposta_ia:
        auditar("ia_tentou_ferramenta_bloqueada", {"ferramenta": nome}, ctx.conv.id)
        return {"erro": "Ferramenta não permitida para a IA."}
    args = {k: v for k, v in (args or {}).items() if k in f.parametros or not f.parametros}
    args.pop("cliente_id", None)  # cliente SEMPRE vem da sessão
    for k in f.obrigatorios:
        if args.get(k) in (None, ""):
            return {"erro": f"Parâmetro obrigatório ausente: {k}"}
    for k, v in list(args.items()):
        tipo = f.parametros.get(k, {}).get("type")
        if v is not None and tipo in TIPOS and not isinstance(v, TIPOS[tipo]):
            try:
                args[k] = int(v) if tipo == "integer" else float(v) if tipo == "number" else str(v)
            except (TypeError, ValueError):
                return {"erro": f"Parâmetro inválido: {k}"}
    if ctx.conv.nivel < f.nivel:
        ctx.rastreio.add("Guardrails", f"bloqueado: {nome} exige cliente verificado", {"nivel_atual": ctx.conv.nivel})
        return {"erro": "identificacao_necessaria",
                "mensagem": "Para consultar dados da conta, o cliente precisa se identificar (CPF/CNPJ)."}
    try:
        bruto = f.funcao(args, ctx)
    except ErroERP as e:
        bruto = {"erro": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("Falha na ferramenta %s", nome)
        bruto = {"erro": "Falha técnica ao consultar o sistema."}
        auditar("erro_ferramenta", {"ferramenta": nome, "erro": str(e)}, ctx.conv.id)
    resultado = guardrails.sanitizar(bruto)
    ctx.evidencias |= guardrails.valores_monetarios(resultado)
    auditar("ferramenta", {"nome": nome, "args": args, "origem": origem,
                           "erro": resultado.get("erro") if isinstance(resultado, dict) else None}, ctx.conv.id)
    ctx.rastreio.add("Tool Calling", f"{nome} ({origem})", {"args": args, "resultado": _resumir(resultado)})
    return resultado


def _resumir(r: Any, limite: int = 600) -> Any:
    import json

    s = json.dumps(r, ensure_ascii=False, default=str)
    return r if len(s) <= limite else s[:limite] + "…"
