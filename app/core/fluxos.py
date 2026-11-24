"""Fluxos guiados (determinísticos): identificação, cadastro, pedido e confirmação de ações.

Ações que gravam no ERP (criar pedido, abrir troca, cancelar) só acontecem aqui,
depois de o cliente confirmar explicitamente — a IA nunca grava nada sozinha.
"""
from __future__ import annotations

import re
from datetime import datetime

from app.config import config
from app.core import ferramentas
from app.core.turno import NOMES_PRODUTO, Turno
from app.erp import ErroERP, obter_erp
from app.util.documentos import mascarar_documento, so_digitos, variantes_telefone_br
from app.util.texto import formatar_moeda as moeda, normalizar

# intenções que podem "interromper" um fluxo em andamento
INTERROMPEM = {"status_pedido", "consultar_preco", "consultar_estoque", "boleto", "troca", "duvida_informacao",
               "consulta_historico", "falar_atendente", "reclamacao", "pedir_desconto", "cancelar_pedido"}

BOTOES_PAGAMENTO = [("pag_pix", "Pix"), ("pag_dinheiro", "Dinheiro"), ("pag_cartao", "Cartão")]
PAGAMENTO_POR_BOTAO = {"pag_pix": "pix", "pag_dinheiro": "dinheiro", "pag_cartao": "cartao_debito",
                       "pag_credito": "cartao_credito", "pag_boleto": "boleto"}


def _regras() -> dict:
    return config.json_config("regras_comerciais.json")


def _nome_pagamento(fp: str | None) -> str:
    return _regras().get("formas_pagamento", {}).get(fp or "", fp or "-")


def _quer_mudar_de_assunto(t: Turno) -> bool:
    return t.intencao in INTERROMPEM and t.cls.get("confianca", 0) >= 0.8


def _sim(t: Turno) -> bool:
    return t.botao in ("sim", "acao_ok", "end_ok", "confirmar_pedido") or t.intencao == "confirmar"


def _nao(t: Turno) -> bool:
    return t.botao in ("nao", "acao_nao", "end_outro", "cancelar_fluxo") or t.intencao == "negar"


# ============================================================================ identificação
def carregar_cliente(t: Turno, cliente: dict, nivel: int, origem: str) -> None:
    conv = t.conv
    conv.slots["cliente"] = {k: cliente.get(k) for k in ("id", "nome", "tipo", "documento")}
    conv.slots["nivel_identificacao"] = max(conv.nivel, nivel)
    if not conv.nome:
        conv.nome = cliente.get("nome") or ""
    detalhe = {"cliente": cliente.get("nome"), "documento": mascarar_documento(cliente.get("documento")),
               "nivel": conv.nivel, "origem": origem}
    if conv.nivel >= 2:
        erp = obter_erp()
        try:
            pedidos = erp.listar_pedidos(cliente["id"], limite=3)
            abertos = [p for p in pedidos if p["status"] in ("recebido", "em_separacao", "em_rota")]
            if abertos:
                conv.slots["pedido_atual"] = abertos[0]["numero"]
            validos = [p for p in pedidos if p["status"] != "cancelado"]
            if validos:
                conv.slots["ultimo_pedido_itens"] = [{"produto_codigo": i["produto_codigo"], "quantidade": i["quantidade"]}
                                                     for i in validos[0]["itens"]]
            ends = erp.listar_enderecos(cliente["id"])
            if ends:
                f = erp.filial_por_bairro(ends[0].get("bairro", ""))
                if f:
                    conv.slots["filial_id"] = f["id"]
        except Exception:  # noqa: BLE001 — memória é complemento, não pode travar o atendimento
            pass
        detalhe.update({"pedido_atual": conv.slots.get("pedido_atual"), "filial_id": conv.slots.get("filial_id")})
    t.rastreio.add("Conversation Memory", f"cliente identificado ({origem})", detalhe)


def identificar_por_telefone(t: Turno) -> None:
    conv = t.conv
    if conv.cliente or conv.slots.get("telefone_consultado") or conv.canal == "cli_anonimo":
        return
    try:
        c = obter_erp().buscar_cliente_por_telefone(conv.usuario_id)
    except ErroERP:
        t.rastreio.add("Conversation Memory", "consulta de telefone indisponível no ERP", None)
        return
    conv.slots["telefone_consultado"] = True
    if c:
        carregar_cliente(t, c, 2, "telefone do WhatsApp = telefone do cadastro")
    else:
        t.rastreio.add("Conversation Memory", "telefone não encontrado no ERP", None)


def iniciar_identificacao(t: Turno, pendente: dict | None, motivo: str) -> None:
    conv = t.conv
    if conv.cliente and conv.nivel == 1:
        conv.set_fluxo("identificacao", "cep", pendente=pendente, tentativas=0)
        t.responder(f"{motivo}\nPara sua segurança, me confirme o *CEP* do endereço cadastrado.")
        return
    conv.set_fluxo("identificacao", "documento", pendente=pendente, tentativas=0)
    t.responder(f"{motivo}\nMe informe seu *CPF ou CNPJ*, por favor. 🔒")


def continuar_identificacao(t: Turno) -> dict | bool:
    conv, f = t.conv, t.conv.fluxo
    erp = obter_erp()
    etapa = f.get("etapa")
    if etapa == "documento":
        doc = t.ent.get("documento")
        if not doc:
            if _quer_mudar_de_assunto(t) and not t.ent.get("documento_invalido"):
                conv.set_fluxo(None)
                return False
            f["tentativas"] = f.get("tentativas", 0) + 1
            if f["tentativas"] >= 3:
                conv.set_fluxo(None)
                t.responder("Não consegui validar o documento. Quer que eu chame um atendente?",
                            [("falar_atendente", "Falar com atendente"), ("novo_pedido", "Fazer pedido")])
            elif t.ent.get("documento_invalido"):
                t.responder("Hmm, esse número não parece um CPF/CNPJ válido. 🤔 Pode conferir e enviar de novo?")
            else:
                t.responder("Para continuar, preciso do seu *CPF ou CNPJ* (pode mandar só os números).")
            return True
        cli = erp.buscar_cliente_por_documento(doc)
        t.rastreio.add("Tool Calling", "buscar_cliente_por_documento (fluxo)", {"documento": mascarar_documento(doc),
                                                                              "encontrado": bool(cli)})
        if not cli:
            pend = f.get("pendente") or {}
            if pend.get("intencao") == "retomar_pedido":
                conv.slots["cadastro"] = {"documento": doc}
                conv.set_fluxo("cadastro", "nome", pendente=pend)
                termo = "a *razão social* da empresa" if len(doc) == 14 else "seu *nome completo*"
                t.responder(f"Não encontrei cadastro com esse documento — sem problema, faço seu cadastro agora! 😊\nQual é {termo}?")
            else:
                conv.set_fluxo(None)
                t.responder("Não encontrei cadastro com esse CPF/CNPJ. Se você já é cliente, o cadastro pode estar em outro "
                            "documento. Quer falar com um atendente?",
                            [("falar_atendente", "Falar com atendente"), ("novo_pedido", "Fazer pedido")])
            return True
        if cli.get("telefone") and set(variantes_telefone_br(conv.usuario_id)) & set(variantes_telefone_br(cli["telefone"])):
            carregar_cliente(t, cli, 2, "documento + telefone conferem")
            return _finalizar_identificacao(t, f)
        carregar_cliente(t, cli, 1, "documento informado (aguardando CEP)")
        conv.set_fluxo("identificacao", "cep", pendente=f.get("pendente"), tentativas=0)
        t.responder("Encontrei seu cadastro. ✅ Para sua segurança, me confirme o *CEP* do endereço cadastrado.")
        return True

    if etapa == "cep":
        cep = so_digitos(t.ent.get("cep") or t.texto)
        if len(cep) != 8:
            if _quer_mudar_de_assunto(t):
                conv.set_fluxo(None)
                return False
            t.responder("Me envie o CEP com 8 números, por favor (ex.: 01234-000).")
            return True
        ceps = {so_digitos(e.get("cep")) for e in erp.listar_enderecos(conv.cliente["id"])}
        if cep in ceps:
            conv.slots["nivel_identificacao"] = 2
            carregar_cliente(t, conv.cliente, 2, "CEP confirmado")
            return _finalizar_identificacao(t, f)
        f["tentativas"] = f.get("tentativas", 0) + 1
        if f["tentativas"] >= 2:
            conv.slots.pop("cliente", None)
            conv.slots["nivel_identificacao"] = 0
            conv.set_fluxo(None)
            t.ctx.pedidos_handoff.append({"motivo": "Falha na verificação de identidade (CEP não confere)", "prioridade": "normal"})
            t.responder("O CEP não confere com o cadastro. Por segurança, vou passar seu atendimento para um atendente.")
        else:
            t.responder("Esse CEP não confere com o cadastro. Pode verificar e mandar de novo?")
        return True
    conv.set_fluxo(None)
    return False


def _finalizar_identificacao(t: Turno, f: dict) -> dict | bool:
    nome = t.primeiro_nome()
    t.responder(f"Prontinho{', ' + nome if nome else ''}! Identidade confirmada. ✅")
    pend = f.get("pendente")
    t.conv.set_fluxo(None)
    return {"retomar": pend} if pend else True


# ============================================================================ cadastro de cliente novo
def continuar_cadastro(t: Turno) -> dict | bool:
    conv, f = t.conv, t.conv.fluxo
    cad = conv.slots.setdefault("cadastro", {})
    if f["etapa"] == "nome":
        nome = re.sub(r"[^\w\sÀ-ÿ&.'-]", "", t.texto).strip()
        if len(nome) < 3 or _quer_mudar_de_assunto(t):
            t.responder("Pode me dizer o nome completo, por favor?")
            return True
        cad["nome"] = nome.title() if len(so_digitos(cad.get("documento"))) == 11 else nome
        conv.set_fluxo(None)
        conv.slots["cliente"] = {"id": None, "nome": cad["nome"], "tipo": "PJ" if len(cad.get("documento", "")) == 14 else "PF",
                                 "documento": cad.get("documento"), "novo": True}
        conv.slots["nivel_identificacao"] = 2  # dono do telefone fazendo o próprio cadastro
        t.rastreio.add("Conversation Memory", "cadastro novo em andamento", {"nome": cad["nome"]})
        pend = f.get("pendente")
        return {"retomar": pend} if pend else True
    conv.set_fluxo(None)
    return False


# ============================================================================ pedido
def iniciar_pedido(t: Turno) -> None:
    itens = [{"produto_codigo": p["codigo"], "quantidade": p["quantidade"] or 1} for p in t.ent.get("produtos", [])]
    t.conv.set_fluxo("pedido", None, itens=itens, forma_pagamento=t.ent.get("forma_pagamento"),
                     **({"troco_para": t.ent["troco_para"]} if "troco_para" in t.ent else {}))
    if t.ent.get("endereco"):
        _aplicar_endereco_novo(t, t.conv.fluxo)
    t.rastreio.add("Agentic RAG", "fluxo de pedido iniciado", {"itens": itens})
    avancar_pedido(t)


def _cliente_pronto(t: Turno) -> bool:
    return bool(t.conv.cliente) and t.conv.nivel >= 2


def avancar_pedido(t: Turno) -> None:
    conv, f = t.conv, t.conv.fluxo
    erp = obter_erp()
    if not _cliente_pronto(t):
        iniciar_identificacao(t, {"intencao": "retomar_pedido", "pedido": f}, "Vamos lá! Primeiro preciso te identificar.")
        return
    cli = conv.cliente
    if not f.get("itens"):
        f["etapa"] = "itens"
        t.responder("O que você precisa hoje?", [("prod_P13", "Gás P13"), ("prod_AGUA20", "Água 20 L"), ("prod_outros", "Outros produtos")])
        return
    # endereço
    if not f.get("endereco_id") and not f.get("endereco_novo"):
        ends = erp.listar_enderecos(cli["id"]) if cli.get("id") else []
        if ends and not f.get("endereco_recusado"):
            e = ends[0]
            f.update(etapa="endereco_confirmar", endereco_sugerido={"id": e["id"], "texto": e["texto"], "bairro": e.get("bairro")})
            t.responder(f"Entrego em *{e['texto']}*?", [("end_ok", "Sim, pode entregar"), ("end_outro", "Outro endereço")])
        else:
            f["etapa"] = "endereco_novo"
            t.responder("Qual o endereço de entrega? Mande *rua, número e bairro* (ou envie sua localização 📍).")
        return
    # filial pela área de entrega
    if not f.get("filial_id"):
        bairro = f.get("bairro") or ""
        filial = erp.filial_por_bairro(bairro) if bairro else None
        if not filial:
            f["etapa"] = "bairro"
            if bairro:
                conv.set_fluxo(None)
                t.responder(f"Poxa, ainda não entregamos no bairro *{bairro}* pelo WhatsApp. 😕 Quer que eu chame um atendente para verificar?",
                            [("falar_atendente", "Falar com atendente")])
            else:
                t.responder("Qual é o *bairro* da entrega?")
            return
        f["filial_id"], f["filial_nome"], f["prazo_min"] = filial["id"], filial["nome"], filial.get("prazo_min") or config.prazo_entrega_min
        conv.slots["filial_id"] = filial["id"]
    # limites e estoque
    regras = _regras()
    limites = regras.get("quantidade_maxima_por_item", {})
    for it in f["itens"]:
        cod, qtd = it["produto_codigo"], int(it["quantidade"])
        if cod in limites and qtd > limites[cod]:
            f["etapa"], f["itens_anteriores"], f["itens"] = "itens", f["itens"], []
            t.responder(f"Para mais de {limites[cod]} un. de {NOMES_PRODUTO.get(cod, cod)}, nossa equipe comercial confirma a "
                        "logística da entrega. Quer ajustar a quantidade ou falar com o comercial?",
                        [("falar_atendente", "Falar com comercial"), ("alt_itens", "Ajustar quantidade")])
            return
        est = ferramentas.executar("consultar_estoque", {"produto_codigo": cod, "filial_id": f["filial_id"]}, t.ctx)
        if est.get("erro"):
            t.ctx.pedidos_handoff.append({"motivo": "Falha ao consultar estoque no ERP", "prioridade": "alta"})
            conv.set_fluxo(None)
            t.responder("Não consegui consultar o estoque agora. Chamei um atendente para confirmar a disponibilidade.")
            return
        disp = (est.get("estoque") or [{}])[0].get("quantidade", 0) if isinstance(est, dict) else 0
        if disp < qtd:
            f["etapa"], f["itens_anteriores"], f["itens"] = "itens", f["itens"], []
            outros = ferramentas.executar("consultar_estoque", {"produto_codigo": cod}, t.ctx).get("estoque", [])
            alt = [o["filial_nome"] for o in outros if o["quantidade"] >= qtd and o["filial_id"] != f["filial_id"]]
            extra = f" A filial {alt[0]} tem, mas a entrega depende de confirmação da equipe." if alt else ""
            t.responder(f"No momento estamos sem estoque suficiente de *{NOMES_PRODUTO.get(cod, cod)}* na filial {f['filial_nome']}.{extra}",
                        [("alt_itens", "Escolher outro"), ("falar_atendente", "Falar com atendente")])
            return
    # pagamento
    if not f.get("forma_pagamento"):
        f["etapa"] = "pagamento"
        botoes = list(BOTOES_PAGAMENTO)
        if cli.get("tipo") == "PJ" and config.erp_modo == "mock":
            botoes = [("pag_pix", "Pix"), ("pag_boleto", "Boleto"), ("pag_cartao", "Cartão")]
        t.responder("Como prefere pagar? (o pagamento é na entrega)", botoes)
        return
    if f["forma_pagamento"] == "boleto" and config.erp_modo == "revgas":
        t.ctx.pedidos_handoff.append({"motivo": "Cliente solicitou aprovação de venda com boleto"})
        conv.set_fluxo(None)
        t.responder("Chamei um atendente para verificar a aprovação do pagamento por boleto.")
        return
    if f["forma_pagamento"] in regras.get("formas_pagamento_somente_pj", []) and cli.get("tipo") != "PJ":
        f["forma_pagamento"] = None
        f["etapa"] = "pagamento"
        t.responder("Boleto está disponível apenas para empresas com cadastro aprovado. Como prefere pagar?", BOTOES_PAGAMENTO)
        return
    if f["forma_pagamento"] == "dinheiro" and "troco_para" not in f:
        f["etapa"] = "troco"
        t.responder("Vai precisar de troco? Se sim, *para quanto*?", [("sem_troco", "Não preciso")])
        return
    mostrar_resumo(t)


def _precificar(t: Turno, f: dict) -> tuple[list[dict], float, float]:
    linhas, total = [], 0.0
    for it in f["itens"]:
        p = ferramentas.executar("consultar_preco", {"produto_codigo": it["produto_codigo"], "filial_id": f["filial_id"]}, t.ctx)
        if p.get("erro") or p.get("preco") is None:
            raise ErroERP(f"Preço indisponível para {NOMES_PRODUTO.get(it['produto_codigo'], it['produto_codigo'])}.")
        preco = float(p["preco"])
        sub = preco * int(it["quantidade"])
        total += sub
        linhas.append({**it, "descricao": p.get("produto_nome") or NOMES_PRODUTO.get(it["produto_codigo"]),
                       "preco_unit": preco, "subtotal": sub})
    taxa = float(_regras().get("taxa_entrega", 0))
    return linhas, taxa, total + taxa


def detalhes_pedido(pedido: dict) -> str:
    linhas = []
    for item in pedido.get("itens") or []:
        codigo = item.get("produto_codigo")
        descricao = item.get("descricao") or NOMES_PRODUTO.get(codigo, codigo or "Produto")
        linha = f"• {item['quantidade']}x {descricao}" if item.get("quantidade") is not None else f"• {descricao}"
        if item.get("preco_unit") is not None:
            linha += f" · {moeda(item['preco_unit'])}/un."
        if item.get("subtotal") is not None:
            linha += f" · {moeda(item['subtotal'])}"
        linhas.append(linha)
    for campo, rotulo in (("valor_produtos", "Subtotal"), ("taxa_entrega", "Taxa de entrega"),
                          ("valor_total", "Total")):
        if pedido.get(campo) is not None:
            linha = f"{rotulo}: {moeda(pedido[campo])}"
            linhas.append(f"*{linha}*" if campo == "valor_total" else linha)
    forma = pedido.get("forma_pagamento")
    if forma:
        pagamento = f"Pagamento: {_nome_pagamento(forma)}"
        if forma == "dinheiro" and pedido.get("troco_para") is not None:
            troco = pedido["troco_para"]
            pagamento += f" · troco para {moeda(troco)}" if troco else " · sem troco"
        linhas.append(pagamento)
    for campo, rotulo in (("endereco_texto", "Endereço"), ("filial_nome", "Filial")):
        if pedido.get(campo):
            linhas.append(f"{rotulo}: {pedido[campo]}")
    return "\n".join(linhas)


def mostrar_resumo(t: Turno) -> None:
    f = t.conv.fluxo
    try:
        linhas, taxa, total = _precificar(t, f)
    except ErroERP as exc:
        t.ctx.pedidos_handoff.append({"motivo": str(exc), "prioridade": "alta"})
        t.conv.set_fluxo(None)
        t.responder(f"{exc} Chamei um atendente para confirmar o valor.")
        return
    t.ctx.evidencias.add(f"{total:.2f}")
    end = (f.get("endereco_sugerido") or {}).get("texto") if f.get("endereco_id") else _texto_endereco(f.get("endereco_novo") or {})
    detalhes = detalhes_pedido({**f, "itens": linhas, "valor_produtos": sum(li["subtotal"] for li in linhas),
                                "taxa_entrega": taxa, "valor_total": total, "endereco_texto": end})
    f["etapa"] = "confirmacao"
    f["total"] = total
    f["itens_cotados"] = linhas
    t.responder(f"📝 *Resumo do pedido*\n{detalhes}\n"
                f"🛵 Entrega em até {f.get('prazo_min', config.prazo_entrega_min)} minutos\n\nPosso confirmar?",
                [("confirmar_pedido", "Confirmar pedido"), ("alterar_pedido", "Alterar"), ("cancelar_fluxo", "Cancelar")])


def _texto_endereco(e: dict) -> str:
    comp = f", {e['complemento']}" if e.get("complemento") else ""
    return f"{e.get('logradouro', '')}, {e.get('numero', '')}{comp} - {e.get('bairro', '')}".strip(" ,-")


def _aplicar_endereco_novo(t: Turno, f: dict) -> bool:
    e = t.ent.get("endereco")
    if not e:
        return False
    resto = e.get("complemento_ou_bairro", "")
    bairro = t.ent.get("bairro") or ""
    complemento = resto
    if not bairro and resto:
        # "Rua X, 10, apto 2 - Bairro Tal": o último trecho sem números costuma ser o bairro
        partes = [p.strip() for p in re.split(r"\s[-–]\s|,", resto) if p.strip()]
        if partes and not re.search(r"\d", partes[-1]) and len(partes[-1]) <= 40 \
                and not re.match(r"(?i)(apto|apartamento|casa|bloco|fundos|sala|loja)\b", partes[-1]):
            bairro = partes[-1].title()
            complemento = ", ".join(partes[:-1])
    if bairro and normalizar(bairro) in normalizar(resto):
        complemento = re.sub(re.escape(bairro), "", resto, flags=re.I).strip(" ,-")
    f["endereco_novo"] = {"logradouro": e["logradouro"], "numero": e["numero"], "complemento": complemento,
                          "bairro": bairro, "cidade": "", "cep": t.ent.get("cep", ""), "referencia": ""}
    f["bairro"] = bairro
    return True


def continuar_pedido(t: Turno) -> bool:
    conv, f = t.conv, t.conv.fluxo
    etapa = f.get("etapa")
    n_respostas = len(t.respostas)
    tratado = _continuar_pedido(t)
    # se o bot só repetiu a pergunta, conta; na 3ª vez seguida oferece saída
    if tratado and conv.fluxo and conv.fluxo.get("etapa") == etapa and len(t.respostas) == n_respostas + 1 \
            and t.respostas[-1].texto.startswith(REPERGUNTAS):
        conv.fluxo["repeticoes"] = conv.fluxo.get("repeticoes", 0) + 1
        if conv.fluxo["repeticoes"] >= 2:
            conv.fluxo["repeticoes"] = 0
            t.respostas[-1] = type(t.respostas[-1])(
                "Parece que não estou te entendendo. 😅 Quer continuar o pedido, descartar ou falar com um atendente?",
                [("continuar_pedido", "Continuar pedido"), ("cancelar_fluxo", "Descartar"), ("falar_atendente", "Falar com atendente")])
    elif conv.fluxo:
        conv.fluxo["repeticoes"] = 0
    return tratado


REPERGUNTAS = ("Não entendi o produto", "É nesse endereço mesmo", "Não consegui entender o endereço", "Qual é o *bairro*",
               "Escolha uma forma de pagamento", "Para quanto você precisa de troco", "Posso confirmar o pedido")


def _continuar_pedido(t: Turno) -> bool:
    conv, f = t.conv, t.conv.fluxo
    etapa = f.get("etapa")
    b = t.botao

    if b == "falar_atendente":
        return False  # deixa o orquestrador transferir
    if etapa == "itens":
        if b == "prod_outros":
            prods = ferramentas.executar("listar_produtos", {}, t.ctx).get("produtos", [])
            lista = "\n".join(f"• *{p['codigo']}* — {p['nome']}" for p in prods)
            t.responder(f"Temos:\n{lista}\n\nÉ só me dizer o produto e a quantidade (ex.: *2 P13* ou *1 P45*).")
            return True
        if b and b.startswith("prod_"):
            f["itens"] = [{"produto_codigo": b[5:], "quantidade": 1}]
        elif b == "alt_itens":
            f["itens"] = []
            t.responder("Certo! Qual produto e quantidade você quer?")
            return True
        elif t.ent.get("produtos"):
            f["itens"] = [{"produto_codigo": p["codigo"], "quantidade": p["quantidade"] or 1} for p in t.ent["produtos"]]
        elif t.ent.get("quantidade") and f.get("itens_anteriores"):
            f["itens"] = [{**f["itens_anteriores"][0], "quantidade": t.ent["quantidade"]}]
        else:
            if _quer_mudar_de_assunto(t):
                return False
            t.responder("Não entendi o produto. 🤔 Pode escrever, por exemplo: *1 gás P13* ou *2 águas*.",
                        [("prod_P13", "Gás P13"), ("prod_AGUA20", "Água 20 L"), ("prod_outros", "Outros produtos")])
            return True
        avancar_pedido(t)
        return True

    if etapa == "endereco_confirmar":
        if t.ent.get("endereco"):
            f["endereco_recusado"] = True
            _aplicar_endereco_novo(t, f)
        elif _sim(t):
            sug = f["endereco_sugerido"]
            f.update(endereco_id=sug["id"], bairro=sug.get("bairro"))
        elif _nao(t):
            f["endereco_recusado"] = True
        else:
            if _quer_mudar_de_assunto(t):
                return False
            t.responder("É nesse endereço mesmo?", [("end_ok", "Sim, pode entregar"), ("end_outro", "Outro endereço")])
            return True
        avancar_pedido(t)
        return True

    if etapa == "endereco_novo":
        if t.msg.tipo == "localizacao" and t.msg.localizacao:
            loc = t.msg.localizacao
            f["endereco_novo"] = {"logradouro": loc.get("endereco") or loc.get("nome") or "Localização enviada", "numero": "s/n",
                                  "complemento": "", "bairro": "", "cidade": "", "cep": "",
                                  "referencia": f"GPS {loc.get('latitude')},{loc.get('longitude')}"}
            f["etapa"] = "bairro"
            t.responder("Recebi sua localização 📍. Só me diga o *bairro* e, se puder, um ponto de referência.")
            return True
        if _aplicar_endereco_novo(t, f):
            avancar_pedido(t)
            return True
        if _quer_mudar_de_assunto(t):
            return False
        t.responder("Não consegui entender o endereço. Mande assim: *Rua das Flores, 120 - Vila Nova*.")
        return True

    if etapa == "bairro":
        bairro = t.ent.get("bairro")
        if not bairro:
            txt = t.texto.strip().split(",")[0].strip()
            if 2 < len(txt) <= 40 and not _quer_mudar_de_assunto(t):
                bairro = txt.title()
        if not bairro:
            if _quer_mudar_de_assunto(t):
                return False
            t.responder("Qual é o *bairro*?")
            return True
        f["bairro"] = bairro
        if f.get("endereco_novo"):
            f["endereco_novo"]["bairro"] = bairro
            if "referencia" in f["endereco_novo"] and t.texto.count(",") >= 1:
                f["endereco_novo"]["referencia"] += " | " + t.texto.split(",", 1)[1].strip()
        avancar_pedido(t)
        return True

    if etapa == "pagamento":
        fp = PAGAMENTO_POR_BOTAO.get(b or "") or t.ent.get("forma_pagamento")
        if not fp:
            if _quer_mudar_de_assunto(t):
                return False
            t.responder("Escolha uma forma de pagamento:", BOTOES_PAGAMENTO)
            return True
        f["forma_pagamento"] = fp
        if "troco_para" in t.ent:
            f["troco_para"] = t.ent["troco_para"]
        avancar_pedido(t)
        return True

    if etapa == "troco":
        if b == "sem_troco" or t.intencao == "negar":
            f["troco_para"] = 0.0
        elif "troco_para" in t.ent:
            f["troco_para"] = t.ent["troco_para"]
        else:
            m = re.search(r"(\d{2,4})(?:[.,](\d{2}))?", t.texto)
            if not m:
                if _quer_mudar_de_assunto(t):
                    return False
                t.responder("Para quanto você precisa de troco? (ex.: 200)", [("sem_troco", "Não preciso")])
                return True
            f["troco_para"] = float(f"{m.group(1)}.{m.group(2) or '00'}")
        avancar_pedido(t)
        return True

    if etapa == "confirmacao":
        if b == "confirmar_pedido" or (not b and t.intencao == "confirmar"):
            return _criar_pedido(t)
        if b == "alterar_pedido":
            f["etapa"] = "alterar"
            t.responder("O que você quer alterar?", [("alt_itens", "Produtos"), ("alt_end", "Endereço"), ("alt_pag", "Pagamento")])
            return True
        if b == "cancelar_fluxo" or t.intencao in ("negar", "cancelar_pedido"):
            conv.set_fluxo(None)
            t.responder("Tudo bem, descartei esse pedido. Se precisar, é só chamar! 👋")
            return True
        mudou = False
        if t.ent.get("produtos"):
            f["itens"] = [{"produto_codigo": p["codigo"], "quantidade": p["quantidade"] or 1} for p in t.ent["produtos"]]
            mudou = True
        elif t.ent.get("quantidade") and len(f["itens"]) == 1:
            f["itens"][0]["quantidade"] = t.ent["quantidade"]
            mudou = True
        if t.ent.get("forma_pagamento"):
            f["forma_pagamento"] = t.ent["forma_pagamento"]
            if f["forma_pagamento"] != "dinheiro":
                f.pop("troco_para", None)
            mudou = True
        if mudou:
            avancar_pedido(t)
            return True
        if _quer_mudar_de_assunto(t):
            return False
        t.responder("Posso confirmar o pedido?", [("confirmar_pedido", "Confirmar pedido"), ("alterar_pedido", "Alterar"),
                                                   ("cancelar_fluxo", "Cancelar")])
        return True

    if etapa == "alterar":
        if b == "alt_itens":
            f["itens_anteriores"], f["itens"] = f["itens"], []
            f["etapa"] = "itens"
            t.responder("Qual produto e quantidade você quer?")
            return True
        if b == "alt_end":
            for k in ("endereco_id", "endereco_novo", "endereco_sugerido", "filial_id", "bairro"):
                f.pop(k, None)
            f["endereco_recusado"] = True
            avancar_pedido(t)
            return True
        if b == "alt_pag":
            f["forma_pagamento"] = None
            f.pop("troco_para", None)
            avancar_pedido(t)
            return True
        f["etapa"] = "confirmacao"
        return _continuar_pedido(t)

    conv.set_fluxo(None)
    return False


def _criar_pedido(t: Turno) -> bool:
    conv, f = t.conv, t.conv.fluxo
    erp = obter_erp()
    cli = conv.cliente
    if erp.endereco_avulso_na_venda and not f.get("itens_cotados"):
        mostrar_resumo(t)
        return True
    try:
        if cli.get("novo") or not cli.get("id"):
            cad = conv.slots.get("cadastro", {})
            novo = erp.cadastrar_cliente(cli["nome"], cad.get("documento", ""), conv.usuario_id)
            conv.slots["cliente"] = {k: novo.get(k) for k in ("id", "nome", "tipo", "documento")}
            conv.slots.pop("cadastro", None)
            t.rastreio.add("Tool Calling", "cadastrar_cliente (após confirmação)", {"cliente_id": novo.get("id")})
        if not f.get("endereco_id") and not erp.endereco_avulso_na_venda:
            end = erp.cadastrar_endereco(conv.cliente["id"], f["endereco_novo"])
            f["endereco_id"] = end["id"]
            t.rastreio.add("Tool Calling", "cadastrar_endereco (após confirmação)", {"endereco_id": end["id"]})
    except Exception as e:  # noqa: BLE001
        t.rastreio.add("Tool Calling", "falha no cadastro", str(e))
        t.ctx.pedidos_handoff.append({"motivo": f"Falha ao cadastrar cliente/endereço: {e}", "prioridade": "alta"})
        t.responder("Tive um problema para salvar seu cadastro. 😕 Já chamei um atendente para concluir seu pedido.")
        conv.set_fluxo(None)
        return True
    r = ferramentas.executar("criar_pedido", {
        "endereco_id": f.get("endereco_id"), "itens": f.get("itens_cotados") or f["itens"],
        "endereco_avulso": f.get("endereco_novo") if erp.endereco_avulso_na_venda else None,
        "forma_pagamento": f["forma_pagamento"], "filial_id": f["filial_id"], "troco_para": f.get("troco_para") or None,
        "observacao": "Pedido via chatbot WhatsApp"}, t.ctx)
    if r.get("erro"):
        t.ctx.pedidos_handoff.append({"motivo": f"Falha ao gravar pedido no ERP: {r['erro']}", "prioridade": "alta"})
        t.responder(f"Preciso que a equipe confira o pedido ({r['erro']}). Já chamei um atendente. 🙏")
        conv.set_fluxo(None)
        return True
    conv.slots["pedido_atual"] = r["numero"]
    conv.slots["ultimo_pedido_itens"] = [{"produto_codigo": i["produto_codigo"], "quantidade": i["quantidade"]} for i in r["itens"]]
    conv.set_fluxo(None)
    try:
        previsao = datetime.strptime(r["previsao_entrega"], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except (KeyError, TypeError, ValueError):
        previsao = None
    pix = _regras().get("chave_pix_exibida")
    extra_pix = f"\nChave Pix: {pix}" if r.get("forma_pagamento") == "pix" and pix else ""
    status = f"\nStatus: *{r['status_texto']}*" if r.get("status_texto") else ""
    entrega = f"\nPrevisão de entrega: até *{previsao}*." if previsao else ""
    t.responder(f"✅ Pedido *{r['numero']}* confirmado!{status}\n\n{detalhes_pedido(r)}{extra_pix}{entrega}",
                [("acompanhar", "Acompanhar pedido"), ("menu", "Voltar ao menu")])
    return True


# ============================================================================ confirmação de ações (troca / cancelamento)
def preparar_confirmacao(t: Turno, acao: dict, texto: str, ia: bool = False) -> None:
    t.conv.set_fluxo("confirmar_acao", "confirmar", acao=acao)
    t.responder(texto, [("acao_ok", "Sim, confirmar"), ("acao_nao", "Não")], ia=ia)


def continuar_confirmacao(t: Turno) -> bool:
    conv, f = t.conv, t.conv.fluxo
    acao = f.get("acao") or {}
    if _sim(t):
        conv.set_fluxo(None)
        if acao.get("acao") == "abrir_troca":
            r = ferramentas.executar("abrir_troca", {k: acao.get(k) for k in ("numero_pedido", "produto_codigo", "motivo")}, t.ctx)
            if r.get("erro"):
                t.ctx.pedidos_handoff.append({"motivo": f"Falha ao abrir troca: {r['erro']}", "prioridade": "alta"})
                t.responder("Não consegui registrar a troca no sistema. Já chamei um atendente. 🙏")
            else:
                t.responder(f"✅ Troca registrada! Protocolo *{r.get('protocolo')}*.\nA filial vai entrar em contato para agendar a "
                            "retirada e a entrega do novo produto. Se puder, mande uma foto do problema por aqui. 📷")
        elif acao.get("acao") == "cancelar_pedido":
            r = ferramentas.executar("cancelar_pedido", {"numero_pedido": acao.get("numero_pedido"), "motivo": acao.get("motivo")}, t.ctx)
            if r.get("erro"):
                t.responder(f"Não consegui cancelar: {r['erro']}\nQuer falar com um atendente?", [("falar_atendente", "Falar com atendente")])
            else:
                conv.slots.pop("pedido_atual", None)
                t.responder(f"Pedido *{r['numero']}* cancelado. ✅ Se precisar de algo, é só chamar.")
        return True
    if _nao(t):
        conv.set_fluxo(None)
        t.responder("Tudo bem, não fiz nenhuma alteração. Posso ajudar em algo mais?")
        return True
    if t.botao or _quer_mudar_de_assunto(t) or t.intencao == "fazer_pedido":
        conv.set_fluxo(None)
        return False
    t.responder("Confirma?", [("acao_ok", "Sim, confirmar"), ("acao_nao", "Não")])
    return True


def continuar(t: Turno) -> dict | bool:
    nome = (t.conv.fluxo or {}).get("nome")
    if t.botao == "cancelar_fluxo":
        t.conv.set_fluxo(None)
        t.responder("Tudo bem, descartei. Se precisar, é só chamar! 👋")
        return True
    if t.botao == "continuar_pedido" and nome == "pedido":
        avancar_pedido(t)
        return True
    if nome == "identificacao":
        return continuar_identificacao(t)
    if nome == "cadastro":
        return continuar_cadastro(t)
    if nome == "pedido":
        return continuar_pedido(t)
    if nome == "confirmar_acao":
        return continuar_confirmacao(t)
    t.conv.set_fluxo(None)
    return False
