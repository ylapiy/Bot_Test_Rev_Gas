"""Intent Classification.

Modo regras (sempre disponível): padrões com pesos sobre o texto normalizado.
Modo IA (se configurada e as regras ficarem em dúvida): a IA classifica em JSON.
Detecta também intenções secundárias ("quero trocar o botijão e saber o preço
do P45") — usadas pelo Agentic RAG.

Para ajustar: edite REGRAS abaixo (padrão regex, peso).
"""
from __future__ import annotations

import re

from app.util.texto import expandir_girias

INTENCOES = {
    "emergencia_vazamento": "Cheiro de gás, vazamento, fogo — prioridade máxima",
    "fazer_pedido": "Comprar/pedir gás, água ou acessório (inclui 'troca do vazio')",
    "consultar_preco": "Perguntar preço/valor",
    "consultar_estoque": "Perguntar se tem/disponibilidade",
    "status_pedido": "Onde está/chegou/previsão do pedido",
    "cancelar_pedido": "Cancelar pedido",
    "boleto": "Boleto, 2ª via, débitos em aberto",
    "troca": "Troca/devolução por defeito, garantia, reembolso",
    "consulta_historico": "Perguntas sobre histórico (quantos pedidos, quanto gastei)",
    "duvida_informacao": "Horário, área de entrega, pagamento, segurança, instalação",
    "pedir_desconto": "Pedir desconto/negociar preço",
    "reclamacao": "Reclamação, insatisfação",
    "falar_atendente": "Pedir humano",
    "saudacao": "Oi, bom dia",
    "agradecimento": "Obrigado, valeu",
    "despedida": "Tchau",
    "confirmar": "Sim, confirmo",
    "negar": "Não",
    "fora_escopo": "Nada a ver com a loja",
}

REGRAS: dict[str, list[tuple[str, float]]] = {
    "emergencia_vazamento": [
        (r"cheiro (forte |muito forte )?de gas|cheirando gas|cheiro de gas", 3), (r"\bvazament", 3),
        (r"\bvazando\b|\bvazou\b|\bescapando\b", 3), (r"\bchiando\b|\bchiado\b", 2),
        (r"pegou fogo|\bfogo no\b|\bincendio\b|\bexplod|\bexplos", 3), (r"\bpassando mal com o gas\b|\bintoxic", 3),
    ],
    "fazer_pedido": [
        (r"\b(quero|queria|gostaria|preciso|vou querer|manda|mande|traz|traga|me ve|ve pra mim)\b"
         r"(?!( de)? (saber|ver|consultar|tirar|entender|falar|trocar|devolver|reclamar|cancelar))(?! que).{0,25}"
         r"\b(gas|botijao|botijoes|p ?13|p ?45|p ?20|p ?5|agua|galao|galoes|recarga|kit|registro)\b", 2.5),
        (r"\b(pedir|encomendar|comprar|fazer (um |o )?pedido|novo pedido|fechar (o )?pedido)\b", 2),
        (r"\bacabou (o |meu )?(gas|botijao|agua)\b|\bfiquei sem (gas|agua)\b|\bgas acabou\b", 2.5),
        (r"\btroca(r)? (o |do |meu |um )?(botijao |gas |galao )?vazio\b", 2.5),
        (r"\b(de sempre|mesmo de sempre|igual (a|da) ultima|repetir (o )?(ultimo )?pedido|mais um igual)\b", 2.5),
        (r"\bentrega(m)? (um|uma|\d) ", 1.5), (r"\bpode (mandar|trazer|entregar)\b", 1.5),
    ],
    "consultar_preco": [
        (r"\bquanto (custa|ta|esta|e|fica|sai|que e|que ta|que custa)\b", 2.5), (r"\b(qual|quanto) (o |e o )?(preco|valor)\b", 2.5),
        (r"\bprecos?\b|\bvalor(es)?\b|\btabela\b", 1.5), (r"\bquanto\b.{0,15}\b(gas|botijao|p ?\d+|agua|galao|kit)\b", 1.5),
    ],
    "consultar_estoque": [
        (r"\btem\b.{0,15}\b(gas|botijao|p ?\d+|agua|galao|kit|registro)\b.{0,15}\?", 1.6),
        (r"\b(tem|ainda tem|possui|ha)\b.{0,25}\b(estoque|disponivel|disponibilidade)\b", 2.5),
        (r"\bestoque\b|\bdisponivel\b|\bdisponibilidade\b", 2), (r"\bacabou (o )?(p ?45|p ?20|p ?5|p ?13) (ai|na loja)\b", 2),
        (r"^tem (gas|botijao|p ?\d+|agua|galao|kit)\b", 1.6),
    ],
    "status_pedido": [
        (r"\bmeu pedido\b|\bo pedido\b|\bpedido \d|\bped-?\d", 1.2),
        (r"\bchegou\b|\bcade\b|\bja saiu\b|\bsaiu (pra|para) entrega\b|\besta (a |)caminho\b|\bta vindo\b|\bvai demorar\b", 2.2),
        (r"\bprevisao\b|\brastre|\bstatus\b|\bentregador\b|\bquanto tempo (falta|ainda)\b|\bdemorando\b", 2),
        (r"\bnao chegou\b|\bainda nao (chegou|veio)\b|\besperando (o )?(gas|pedido|entregador)\b", 2.5),
    ],
    "cancelar_pedido": [
        (r"\bcancel(a|ar|e|ei|amento)\b", 3), (r"\bdesist(o|i|ir)\b", 2.5), (r"\bnao quero mais (o pedido|o gas|a agua)\b", 3),
    ],
    "boleto": [
        (r"\bboletos?\b", 3), (r"\bsegunda via\b", 3), (r"\bfatura\b|\blinha digitavel\b|\bcodigo de barras\b", 2.5),
        (r"\b(debitos?|contas?|titulos?|valores?) (em aberto|vencid)", 2.5), (r"\bfinanceiro\b|\bquanto (eu )?devo\b|\bminha divida\b", 2),
    ],
    "troca": [
        (r"\btroca(r)?\b.{0,40}\b(defeito|vazamento|vazando|amassad|enferrujad|errad|quebrad|estragad|problema|lacre|valvula|peso|leve|vazio)\b", 3),
        (r"\b(veio|chegou|entregaram|ta|esta)\b.{0,20}\b(errad[oa]|com defeito|amassad[oa]|sem lacre|lacre (rompido|violado)|vazio|leve|faltando)\b", 3),
        (r"\b(defeito|amassad[oa]|enferrujad[oa]|errad[oa]|quebrad[oa]|sem lacre|estragad[oa])\b.{0,40}\btroca(r)?\b", 2),
        (r"\bdevolu|\bdevolver\b|\breembols|\bestorno\b|\bgarantia\b", 2.5),
        (r"\b(registro|regulador|mangueira|botijao|galao)\b.{0,20}\b(defeito|nao funciona|quebrad|estragad)\b", 2.5),
        (r"\bagua\b.{0,20}\b(gosto|cheiro|cor) (estranh|ruim)", 3),
    ],
    "consulta_historico": [
        (r"\bquant[oa]s (pedidos|vezes|botijoes|galoes|compras)\b", 3), (r"\bquanto (eu )?(gastei|paguei|comprei)\b", 3),
        (r"\bhistorico\b", 2.5), (r"\b(meus|ultimos) pedidos\b|\bultima compra\b|\bo que (eu )?comprei\b", 2.2),
        (r"\b(total|media) (que |de )?(gastei|gasto|compras|pedidos)\b", 2.5), (r"\bquando (foi|eu fiz) (o |meu )?ultimo pedido\b", 2.5),
    ],
    "duvida_informacao": [
        (r"\bhorario\b|\bque horas (abre|fecha)\b|\b(abre|fecha|funciona|abrem|fecham|funcionam)\b.{0,20}\b(domingo|feriado|sabado|hoje|que horas|ate)\b", 2.5),
        (r"\bforma(s)? de pagamento\b|\baceita(m)?\b.{0,15}\b(cartao|pix|vale|ticket|credito|debito)\b", 2.5),
        (r"\b(area|regiao|bairros?) (de )?entrega\b|\bentrega(m)? (no|na|em|pro|pra)\b|\batende(m)? (no|na|o bairro)\b", 2.2),
        (r"\btaxa de entrega\b|\bfrete\b|\bprazo de entrega\b|\bquanto tempo (demora|leva) (a|pra|para) entrega", 2.2),
        (r"\bcomo (instalar|instalo|troco|trocar o registro|ligar|testar|faco para)\b|\binstalacao\b", 2.5),
        (r"\bseguranca\b|\bvalidade\b|\brequalifica|\bnota fiscal\b|\bonde fica\b|\bendereco da loja\b|\bdiferenca entre\b", 2),
        (r"\bpara que serve\b|\bqual (a )?diferenca\b|\bdura quanto\b|\brende quanto\b|\bquanto tempo dura\b", 2),
    ],
    "pedir_desconto": [
        (r"\bdesconto\b|\bdescontinho\b|\bchorinho\b|\bcupom\b|\bpromocao\b", 2.5),
        (r"\b(abaixa|abaixar|baixa|baixar|melhora|melhorar)\b.{0,15}\b(preco|valor|pra mim)\b", 2.5),
        (r"\bfaz\b.{0,20}\b(por|a) (r\$ ?)?\d|\bfaz mais barato\b|\bmais em conta\b|\bpreco especial\b|\bconsegue (fazer )?(por|um preco)\b"
         r"|\b(deixa|sai|fica)\b.{0,15}\bpor (r\$ ?)?\d", 2.5),
    ],
    "reclamacao": [
        (r"\breclama|\bpessim|\bhorrivel\b|\babsurdo\b|\bdescaso\b|\bfalta de respeito\b|\bprocon\b|\bnunca mais\b", 2.5),
        (r"\b(demorou|atrasou|atrasad[oa])\b.{0,20}\b(demais|muito|mais de|horas?)\b|\bmal atendid|\bgrosseir|\bmal educad", 2.5),
        (r"\binsatisfeit|\bdecepcionad|\bvergonha\b|\bridicul", 2),
    ],
    "falar_atendente": [
        (r"\batendente\b|\bhumano\b|\bpessoa (de verdade|real)\b|\balguem de verdade\b|\bfuncionario\b", 3),
        (r"\bfalar com (alguem|uma pessoa|o gerente|gerente|voces|o dono|a loja|o comercial|o financeiro)\b", 3),
        (r"\bnao quero (falar com )?(robo|bot|maquina)\b|\bvoce e (um )?robo\b|\bme liga\b|\bligar (pra|para) voces\b", 2.5),
    ],
    "saudacao": [(r"^(oi+|ola|bom dia|boa tarde|boa noite|e ai|eai|opa|hello|alo|oie|salve|hey)\b", 1.5)],
    "agradecimento": [(r"\bobrigad|\bvaleu\b|\bagradec|\bmuito bom\b|\bperfeito\b|\bshow\b|\bfoi otimo\b", 1.5)],
    "despedida": [(r"\btchau\b|\bate mais\b|\bate logo\b|\bfalou\b|\babracos?\b", 1.5)],
    "confirmar": [(r"^(sim|s|isso|isso mesmo|confirmo|confirmar|confirma|confirmado|pode ser|pode|ok|okay|certo|correto|claro|"
                   r"com certeza|positivo|beleza|blz|bora|fechado|isso ai|exato|perfeito|manda|pode mandar|esse mesmo|este mesmo)[.! ]*$", 3)],
    "negar": [(r"^(nao|n|negativo|errado|nao e|nao quero|nenhum|nada|deixa|deixa pra la|agora nao)[.! ]*$", 3)],
}

# assuntos que encerram a análise (não geram subtarefas)
SEM_SECUNDARIAS = {"reclamacao", "falar_atendente", "emergencia_vazamento", "pedir_desconto", "cancelar_pedido"}
# combinações que são falso positivo (ex.: "quanto gastei com P45" não é pergunta de preço)
EXCLUI = {
    "consulta_historico": {"consultar_preco", "status_pedido", "boleto", "fazer_pedido", "consultar_estoque"},
    "boleto": {"consultar_preco", "consulta_historico"},
    "troca": {"fazer_pedido", "status_pedido"},
    "fazer_pedido": {"consultar_estoque"},
    "status_pedido": {"fazer_pedido"},
}

# saudação/agradecimento só "ganham" se a mensagem for curta
CURTAS = {"saudacao", "agradecimento", "despedida"}


_INFORMATIVO = (r"\b(como (faco|faz|faço|testar|testo|saber|sei|identificar|identifico|verificar|verifico|evitar|evito|detectar|prevenir)"
               r"|o que (fazer|faco|devo fazer) (em caso|se|quando)|em caso de|se (tiver|houver|tivesse|der|sentir|acontecer)"
               r"|para (evitar|testar|prevenir)|dicas?|prevenir|teste de)\b")
_ALARME = (r"\b(esta|ta|estou sentindo|to sentindo|sinto|sentindo|ta com|esta com|tem um|tem uma) (um |uma )?(forte |muito )?"
           r"(vazando|cheiro|vazamento|chiando|chiado)\b|\bvazando\b|\bvazou\b|\bescapando\b|\bpegou fogo\b|\bsocorro\b"
           r"|\burgente\b|\bagora\b|\bpegando fogo\b|\bexplodi")


def _negacao_emergencia(t: str) -> bool:
    if re.search(r"\bnao (esta |ta |tem |sinto )?(mais )?(vazando|cheiro|vazamento|chiando)\b", t):
        return True
    # pergunta hipotética/informativa ("como testar se tem vazamento?") sem sinal de risco agora
    return bool(re.search(_INFORMATIVO, t)) and not re.search(_ALARME, t)


def classificar_regras(texto: str) -> dict:
    t = expandir_girias(texto)
    pontos: dict[str, float] = {}
    gatilhos: dict[str, list[str]] = {}
    for intencao, regras in REGRAS.items():
        for padrao, peso in regras:
            m = re.search(padrao, t)
            if m:
                pontos[intencao] = pontos.get(intencao, 0) + peso
                gatilhos.setdefault(intencao, []).append(m.group().strip())
    if "emergencia_vazamento" in pontos and _negacao_emergencia(t):
        pontos.pop("emergencia_vazamento")
        pontos["duvida_informacao"] = pontos.get("duvida_informacao", 0) + 2.5
        gatilhos.setdefault("duvida_informacao", []).append("pergunta informativa sobre segurança")
    n_palavras = len(t.split())
    for c in CURTAS:
        if c in pontos and n_palavras > 6:
            pontos[c] *= 0.3
    # "troca do vazio" é pedido, não devolução
    if "troca" in pontos and re.search(r"\btroca(r)? (o |do |meu |um )?(botijao |gas |galao )?vazio\b", t) \
            and not re.search(r"defeito|vazando|amassad|errad|quebrad|lacre", t):
        pontos.pop("troca")
    # status com "meu pedido" sozinho é fraco; se houver intenção mais forte, ela ganha
    if not pontos:
        return {"intencao": "fora_escopo", "confianca": 0.3, "secundarias": [], "gatilhos": {}, "origem": "regras"}
    if "emergencia_vazamento" in pontos:
        principal = "emergencia_vazamento"
    else:
        principal = max(pontos, key=lambda k: pontos[k])
    score = pontos[principal]
    confianca = round(min(0.99, 0.45 + 0.18 * score), 2)
    secundarias = [k for k, v in sorted(pontos.items(), key=lambda kv: -kv[1])
                   if k != principal and v >= 1.5 and k not in CURTAS | {"confirmar", "negar"}]
    if principal in SEM_SECUNDARIAS:
        secundarias = []
    secundarias = [s for s in secundarias if s not in EXCLUI.get(principal, set())]
    return {"intencao": principal, "confianca": confianca, "secundarias": secundarias,
            "gatilhos": gatilhos, "pontos": pontos, "origem": "regras"}


def classificar(texto: str, contexto: str = "", llm=None, *, permitidas: tuple[str, ...] | None = None) -> dict:
    r = classificar_regras(texto)
    precisa_ia = llm is not None and (permitidas is not None or r["confianca"] < 0.75 or r["intencao"] == "fora_escopo")
    if not precisa_ia:
        return r
    opcoes = permitidas or INTENCOES
    lista = "\n".join(f"- {k}: {INTENCOES[k]}" for k in opcoes)
    formato = ('{"intencao": "...", "confianca": 0.0-1.0, "entidades": {"produto": '
               '"P13|P13C|P45|P20|P5|AGUA20|KITREG|null", "quantidade": null}}' if permitidas else
               '{"intencao": "...", "confianca": 0.0-1.0, "secundarias": ["..."], '
               '"entidades": {"produto": "P13|P13C|P45|P20|P5|AGUA20|KITREG|null", "quantidade": null, '
               '"numero_pedido": null, "documento": null, "forma_pagamento": null}}')
    j = llm.gerar_json(
        "Você é o classificador de intenções do WhatsApp de uma revenda de gás (GLP) e água.",
        f"Intenções possíveis:\n{lista}\n\nContexto recente da conversa:\n{contexto or '(nenhum)'}\n\n"
        f"Mensagem do cliente: \"{texto}\"\n\n"
        f"Classifique a mensagem em uma das opções. Use fora_escopo quando nenhuma se aplicar. "
        f"Extraia apenas dados explícitos da mensagem e use null quando ausentes. Formato: {formato}",
    )
    if isinstance(j, dict) and isinstance(j.get("intencao"), str) and j["intencao"] in opcoes:
        # emergência detectada por regra NUNCA é rebaixada pela IA
        if r["intencao"] == "emergencia_vazamento":
            return r
        confianca = j.get("confianca")
        secundarias = j.get("secundarias")
        entidades = j.get("entidades")
        if type(confianca) not in (int, float) or not 0 <= confianca <= 1:
            return r
        return {
            "intencao": j["intencao"], "confianca": confianca,
            "secundarias": [s for s in secundarias if isinstance(s, str) and s in INTENCOES and s != j["intencao"]]
            if isinstance(secundarias, list) else [],
            "gatilhos": r.get("gatilhos", {}), "origem": "ia", "regras": r["intencao"],
            "entidades_ia": entidades if isinstance(entidades, dict) else {},
        }
    return r
