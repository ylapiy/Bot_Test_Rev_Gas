"""Query Rewriting.

1) expandir(): resolve gírias, elipses e referências usando a memória da conversa.
   "e o de 45?"        -> "quanto custa o P45"         (se o assunto era preço)
   "chegou?"           -> "chegou o pedido PED-1003"
   "o de sempre"       -> "quero fazer um pedido igual ao último: 1x P13"
2) estruturar(): transforma a mensagem numa consulta estruturada para as
   ferramentas. Ex.: "meu pedido chegou?" ->
   {"acao": "consultar_status_pedido", "pedido": "PED-1003", "cliente_id": 1}
"""
from __future__ import annotations

import re

from app.util.texto import expandir_girias

NOMES_PRODUTO = {"P13": "P13", "P13C": "P13 completo", "P45": "P45", "P20": "P20", "P5": "P5",
                 "AGUA20": "água 20 L", "KITREG": "kit registro"}

_REF_PRODUTO = r"(p ?\d{1,2}c?|agua|galao|kit|registro|completo|de \d{1,2}|o de \d{1,2}|\d{1,2} ?kg)"
_NAO_E_STATUS = r"troc|defeito|amassad|vazand|errad|devolv|reclam|cancel|quebrad|lacre|grosseir|pessim"
_MARCADORES_REFERENCIA = r"\b(ele|ela|isso|esse|essa|desse|dessa|mesmo|mesma|tambem|de novo|outro|outra|aquele|aquela)\b"


def expandir(texto: str, slots: dict, llm=None, historico: str = "") -> dict:
    t = expandir_girias(texto)
    original = t
    regras: list[str] = []
    ultima = slots.get("ultima_intencao")
    produto = slots.get("produto")
    pedido = slots.get("pedido_atual")

    # 1) elipse de produto: "e o de 45?", "e a água?", "e o P20?"
    if ultima in ("consultar_preco", "consultar_estoque") and re.fullmatch(rf"(e )?(o |a )?(do |da )?{_REF_PRODUTO}\s*\??", t):
        verbo = "quanto custa" if ultima == "consultar_preco" else "tem estoque de"
        t = f"{verbo} {re.sub(r'^(e )?(o |a )?', '', t).rstrip('?').strip()}"
        regras.append(f"elipse de produto resolvida pelo assunto anterior ({ultima})")

    # 2) "e na zona norte?" / "e no centro?"
    if ultima in ("consultar_preco", "consultar_estoque") and produto and re.fullmatch(r"e (na|no) [a-z ]+\??", t):
        verbo = "quanto custa" if ultima == "consultar_preco" else "tem estoque de"
        t = f"{verbo} {NOMES_PRODUTO.get(produto, produto)} {t[2:].rstrip('?')}"
        regras.append("filial nova aplicada ao produto em contexto")

    # 3) preço/estoque sem produto: usa o produto da memória
    if produto and not regras and re.search(r"\bquanto (custa|ta|esta|e|fica|sai)\b|^(e )?tem\s*\??$|\btem (no|em) estoque\b", t) \
            and not re.search(r"\b(gas|botijao|p ?\d|agua|galao|kit|registro|de \d{1,2}|\d{1,2} ?kg)\b", t):
        t = f"{t.rstrip('?')} o {NOMES_PRODUTO.get(produto, produto)}"
        regras.append(f"produto da memória: {produto}")

    # 4) status sem número: pedido atual da memória
    if re.search(r"\b(chegou|cade|ja saiu|ta vindo|vai demorar|previsao|demorando|esta a caminho|meu pedido|o pedido|entregador)\b", t) \
            and not re.search(r"\bped(ido)?[- ]?\d{3,}", t) and not re.search(_NAO_E_STATUS, t):
        if pedido:
            t = re.sub(r"\b(meu|o) pedido\b", f"o pedido {pedido}", t) if re.search(r"\b(meu|o) pedido\b", t) else f"{t.rstrip('?')} (pedido {pedido})"
            regras.append(f"pedido atual da memória: {pedido}")

    # 5) "cancela ele/esse"
    if pedido and re.search(r"\bcancel", t) and not re.search(r"\d{3,}", t):
        t = f"cancelar o pedido {pedido}"
        regras.append(f"cancelamento referente ao pedido atual {pedido}")

    # 6) "o de sempre" / "igual da última vez"
    if re.search(r"\b(de sempre|mesmo de sempre|igual (a|da|ao) ultim|repetir (o )?(ultimo )?pedido|mais um igual)\b", t):
        itens = slots.get("ultimo_pedido_itens") or []
        if itens:
            desc = " e ".join(f"{i['quantidade']} {NOMES_PRODUTO.get(i['produto_codigo'], i['produto_codigo'])}" for i in itens)
            t = f"quero fazer um pedido igual ao ultimo: {desc}"
            regras.append("repetição do último pedido do histórico")

    # 7) IA: referências que as regras não resolveram ("quero desse também")
    if llm is not None and t == original and historico and re.search(_MARCADORES_REFERENCIA, t) and len(t) < 80:
        j = llm.gerar_json(
            "Você reescreve mensagens de clientes de uma revenda de gás para que fiquem completas e independentes do contexto.",
            f"Conversa recente:\n{historico}\n\nÚltima mensagem: \"{texto}\"\n"
            'Reescreva a última mensagem resolvendo pronomes e referências. Não invente informações. '
            'Formato: {"reescrita": "..."}',
        )
        if j and j.get("reescrita"):
            t = expandir_girias(j["reescrita"])
            regras.append("reescrita pela IA com o contexto da conversa")

    return {"original": texto, "texto": t, "alterado": t != original, "regras": regras}


# Expansão de consulta para o RAG: vocabulário do cliente -> vocabulário dos documentos
SINONIMOS_RAG = {
    r"\bdura\b|\bdurar\b|\brende\b|\brendimento\b": "rendimento média dias",
    r"\bfrete\b": "taxa de entrega",
    r"\bdevolv|\bdevolu": "troca devolução reembolso",
    r"\bhorario|\babre\b|\bfecha\b|\bfunciona": "horário de funcionamento entregas",
    r"\bentrega(m)? (no|na|em)\b|\batende(m)? (no|na)\b|\bbairro": "área de entrega bairros filial",
    r"\bvazamento|\bvazando|\bcheiro de gas": "vazamento segurança registro",
    r"\bregistro\b|\bregulador\b|\bmangueira\b": "kit registro regulador mangueira",
    r"\bvale gas\b|\bauxilio\b|\bgas do povo\b": "programa gás do povo vale-gás",
    r"\bpagar\b|\bpagamento\b|\bcartao\b|\bpix\b": "formas de pagamento",
    r"\bvasilhame\b|\bcasco\b|\bbotijao vazio\b": "P13 completo vasilhame recarga troca",
}


def expandir_para_busca(consulta: str) -> tuple[str, list[str]]:
    t = expandir_girias(consulta)
    extras = [v for k, v in SINONIMOS_RAG.items() if re.search(k, t)]
    return (f"{consulta} {' '.join(extras)}" if extras else consulta), extras


def estruturar(intencao: str, ent: dict, slots: dict, cliente: dict | None, texto: str) -> dict:
    cid = cliente.get("id") if cliente else None
    prod = ent.get("produto") or slots.get("produto")
    filial = ent.get("filial_id") or slots.get("filial_id")
    if intencao == "status_pedido":
        if ent.get("numero_pedido"):
            ped, origem = ent["numero_pedido"], "mensagem"
        elif slots.get("pedido_atual"):
            ped, origem = slots["pedido_atual"], "memoria"
        else:
            ped, origem = None, "buscar_ultimo_pedido_do_cliente"
        q = {"acao": "consultar_status_pedido", "pedido": ped, "origem_pedido": origem, "cliente_id": cid}
        q["texto"] = f"Status de entrega do {'pedido ' + ped if ped else 'último pedido'} do cliente {cliente['nome'] if cliente else '(não identificado)'}"
    elif intencao in ("consultar_preco", "consultar_estoque"):
        q = {"acao": intencao, "produtos": [p["codigo"] for p in ent.get("produtos", [])] or ([prod] if prod else []),
             "filial_id": filial}
        q["texto"] = f"{'Preço' if intencao == 'consultar_preco' else 'Estoque'} de {', '.join(q['produtos']) or '(produto não informado)'}"
    elif intencao == "fazer_pedido":
        q = {"acao": "iniciar_pedido", "itens": [{"produto": p["codigo"], "quantidade": p["quantidade"] or 1} for p in ent.get("produtos", [])],
             "forma_pagamento": ent.get("forma_pagamento"), "cliente_id": cid}
        q["texto"] = "Novo pedido " + (", ".join(f"{i['quantidade']}x {i['produto']}" for i in q["itens"]) or "(itens a definir)")
    elif intencao == "boleto":
        q = {"acao": "consultar_titulos", "status": ent.get("status") or "em_aberto", "cliente_id": cid}
        q["texto"] = f"Títulos {q['status']} do cliente"
    elif intencao == "troca":
        q = {"acao": "avaliar_troca", "pedido": ent.get("numero_pedido") or slots.get("pedido_atual"), "produto": prod,
             "periodo": ent.get("periodo"), "cliente_id": cid, "motivo": texto}
        q["texto"] = "Avaliar elegibilidade de troca conforme política"
    elif intencao == "cancelar_pedido":
        q = {"acao": "cancelar_pedido", "pedido": ent.get("numero_pedido") or slots.get("pedido_atual"), "cliente_id": cid}
        q["texto"] = f"Cancelar pedido {q['pedido'] or '(a identificar)'}"
    elif intencao == "consulta_historico":
        q = {"acao": "text_to_sql", "pergunta": texto, "periodo": ent.get("periodo"), "cliente_id": cid}
        q["texto"] = "Consulta ao histórico via SQL controlado"
    elif intencao == "duvida_informacao":
        q = {"acao": "buscar_conhecimento", "consulta": texto}
        q["texto"] = "Busca na base de conhecimento"
    else:
        q = {"acao": intencao, "texto": texto}
    return q
