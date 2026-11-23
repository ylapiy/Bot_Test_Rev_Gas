"""Entity Extraction: CPF/CNPJ, nº do pedido, produto(s) e quantidade, filial/bairro,
período, status, forma de pagamento, troco, CEP, endereço, valores e percentuais.

Regras (regex + validação de dígito verificador) sempre rodam. Se houver IA, ela
complementa o que as regras não acharam (mas CPF/CNPJ só valem se passarem na
validação matemática)."""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

from app.db import agora
from app.util.documentos import cnpj_valido, cpf_valido, so_digitos
from app.util.texto import expandir_girias

NUMEROS_EXTENSO = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
    "sete": 7, "oito": 8, "nove": 9, "dez": 10, "meia duzia": 6, "uma duzia": 12,
}

# (regex sobre texto normalizado, código). Ordem importa: mais específico primeiro.
PRODUTOS = [
    (r"\bp ?13 ?c\b|\bp13 completo\b|botijao (novo )?completo|com (o )?vasilhame|com casco|botijao novo|vasilhame novo", "P13C"),
    (r"\bp ?45\b|\b45 ?(kg|quilos?)\b|botijao grande|cilindro grande|\bde 45\b", "P45"),
    (r"\bp ?20\b|\b20 ?(kg|quilos?)\b|empilhadeira", "P20"),
    (r"\bp ?0?5\b|\b5 ?(kg|quilos?)\b|botijao pequeno|botijaozinho|\bde 5\b", "P5"),
    (r"\bp ?13\b|\b13 ?(kg|quilos?)\b|gas de cozinha|\bde 13\b", "P13"),
    (r"\bagua\b|\bgala[o]\b|\bgaloes\b|garrafao|garrafoes", "AGUA20"),
    (r"\bregistro\b|regulador|mangueira|\bkit\b", "KITREG"),
    (r"\bbotijao\b|\bbotijoes\b|\bgas\b|\bgases\b|\brecarga\b|\bcarga de gas\b", "P13"),  # genérico -> P13
]
GAS_GENERICO = r"\bbotijao\b|\bbotijoes\b|\bgas\b|\bgases\b|\brecarga\b|\bcarga de gas\b"

MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro"]

STATUS = [
    (r"\bentregues?\b", "entregue"), (r"\bem rota\b|saiu para entrega|a caminho", "em_rota"),
    (r"\bcancelad[oa]s?\b", "cancelado"), (r"\bvencid[oa]s?\b|\batrasad[oa]s? (no pagamento)?\b", "vencido"),
    (r"\bem aberto\b|\bpendentes?\b|\bnao pag[oa]s?\b", "em_aberto"), (r"\bpag[oa]s\b", "pago"),
]

PAGAMENTO = [
    (r"\bpix\b", "pix"), (r"\bdinheiro\b|\bespecie\b|\bcash\b", "dinheiro"),
    (r"cartao de credito|\bcredito\b", "cartao_credito"), (r"cartao de debito|\bdebito\b", "cartao_debito"),
    (r"\bcartao\b|maquininha", "cartao_debito"), (r"\bboleto\b|\bfaturad[oa]\b", "boleto"),
]

RE_PEDIDO = re.compile(r"(?:\bped(?:ido)?\b[\s\-#:.nº°o]*|#\s?)(\d{3,7})\b")
RE_PEDIDO_COD = re.compile(r"\bped-?(\d{3,7})\b")
RE_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
RE_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
RE_CEP = re.compile(r"(?<!\d)(\d{5})-?(\d{3})(?!\d)")
RE_VALOR = re.compile(r"r\$\s?(\d{1,6}(?:[.,]\d{1,2})?)|\bpor\s(\d{2,5}(?:[.,]\d{1,2})?)\s?(?:reais|conto|pila|real)?\b")
RE_PERCENT = re.compile(r"(\d{1,2})\s?(?:%|por cento)")
RE_TROCO = re.compile(r"troco\s(?:para|pra|de)\s(?:r\$\s?)?(\d{1,4}(?:[.,]\d{1,2})?)")
RE_ENDERECO = re.compile(
    r"\b(rua|r\.|avenida|av\.?|travessa|tv\.?|alameda|al\.?|estrada|rodovia|praca|largo|viela)\s+"
    r"([a-z0-9 .'ºª]+?)[,\s]+(?:n[º°o]?\.?\s*)?(\d{1,6}|s/?n)\b(?:[,\s-]+(.+))?")


def _num(txt: str) -> int | None:
    txt = txt.strip()
    if txt.isdigit():
        return int(txt)
    return NUMEROS_EXTENSO.get(txt)


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def extrair_periodo(t: str) -> dict | None:
    h = agora().replace(tzinfo=None)
    hoje = h.replace(hour=0, minute=0, second=0)
    fim_dia = lambda d: d.replace(hour=23, minute=59, second=59)  # noqa: E731
    if re.search(r"\bhoje\b", t):
        return {"inicio": _fmt(hoje), "fim": _fmt(fim_dia(hoje)), "descricao": "hoje"}
    if re.search(r"\bontem\b", t):
        d = hoje - timedelta(days=1)
        return {"inicio": _fmt(d), "fim": _fmt(fim_dia(d)), "descricao": "ontem"}
    m = re.search(r"\bultim[oa]s? (\d{1,3}) dias\b", t)
    if m:
        return {"inicio": _fmt(hoje - timedelta(days=int(m.group(1)))), "fim": _fmt(h), "descricao": f"últimos {m.group(1)} dias"}
    if re.search(r"semana passada", t):
        ini = hoje - timedelta(days=hoje.weekday() + 7)
        return {"inicio": _fmt(ini), "fim": _fmt(fim_dia(ini + timedelta(days=6))), "descricao": "semana passada"}
    if re.search(r"(esta|essa|nesta|nessa) semana", t):
        ini = hoje - timedelta(days=hoje.weekday())
        return {"inicio": _fmt(ini), "fim": _fmt(h), "descricao": "esta semana"}
    if re.search(r"mes passado", t):
        fim_ant = hoje.replace(day=1) - timedelta(days=1)
        return {"inicio": _fmt(fim_ant.replace(day=1)), "fim": _fmt(fim_dia(fim_ant)), "descricao": "mês passado"}
    if re.search(r"(este|esse|neste|nesse) mes|\bno mes\b", t):
        return {"inicio": _fmt(hoje.replace(day=1)), "fim": _fmt(h), "descricao": "este mês"}
    if re.search(r"(este|esse|neste|nesse) ano", t):
        return {"inicio": _fmt(hoje.replace(month=1, day=1)), "fim": _fmt(h), "descricao": "este ano"}
    for i, nome in enumerate(MESES, start=1):
        if re.search(rf"\b(em|de|no mes de) {nome}\b", t):
            ano = h.year if i <= h.month else h.year - 1
            ultimo = calendar.monthrange(ano, i)[1]
            return {"inicio": f"{ano}-{i:02d}-01 00:00:00", "fim": f"{ano}-{i:02d}-{ultimo} 23:59:59", "descricao": f"{nome}/{ano}"}
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        ano = int(m.group(3)) if m.group(3) else h.year
        ano = ano + 2000 if ano < 100 else ano
        try:
            d = datetime(ano, mes, dia)
            return {"inicio": _fmt(d), "fim": _fmt(fim_dia(d)), "descricao": d.strftime("%d/%m/%Y")}
        except ValueError:
            return None
    return None


def extrair_produtos(t: str) -> list[dict]:
    """Encontra produtos e a quantidade logo antes de cada um ("2 gás e 1 água")."""
    achados: list[tuple[int, int, str]] = []
    ocupado: list[tuple[int, int]] = []
    for padrao, cod in PRODUTOS:
        for m in re.finditer(padrao, t):
            if any(a <= m.start() < b or a < m.end() <= b for a, b in ocupado):
                continue
            if cod == "P13" and re.fullmatch(GAS_GENERICO, m.group()):
                # "gás" genérico não vale se outro gás específico já foi citado
                if any(c in ("P13C", "P45", "P20", "P5", "P13") for _, _, c in achados):
                    continue
                # "cheiro de gás", "vazamento de gás" não são pedidos de produto
                if re.search(r"(cheiro|vazamento|vazando|escapando|chiando) (forte )?de $", t[max(0, m.start() - 22):m.start()]):
                    continue
            achados.append((m.start(), m.end(), cod))
            ocupado.append((m.start(), m.end()))
    achados.sort()
    itens: dict[str, int] = {}
    for ini, _fim, cod in achados:
        antes = t[max(0, ini - 22):ini]
        m = re.search(r"(\d{1,3}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|meia duzia)\s+"
                      r"(?:(?:botijao|botijoes|unidades?|galao|galoes|cilindros?|recargas?|kits?|do|de|da|o|a)\s+){0,2}$", antes)
        qtd = _num(m.group(1)) if m else None
        if qtd is not None and qtd > 99:
            qtd = None
        if cod not in itens:
            itens[cod] = qtd or 0
        elif qtd and not itens[cod]:
            itens[cod] = qtd
    return [{"codigo": c, "quantidade": q or None} for c, q in itens.items()]


def extrair(texto: str, filiais: list[dict] | None = None) -> dict:
    t = expandir_girias(texto)
    bruto = (texto or "").lower()
    e: dict = {}

    # ---- documentos (com validação do dígito verificador) ----
    for m in RE_CNPJ.finditer(bruto):
        if cnpj_valido(m.group()):
            e["cnpj"] = e["documento"] = so_digitos(m.group())
            break
        e["documento_invalido"] = so_digitos(m.group())
    if "documento" not in e:
        for m in RE_CPF.finditer(bruto):
            if cpf_valido(m.group()):
                e["cpf"] = e["documento"] = so_digitos(m.group())
                e.pop("documento_invalido", None)
                break
            if len(so_digitos(m.group())) == 11:
                e["documento_invalido"] = so_digitos(m.group())
    sem_docs = RE_CPF.sub(" ", RE_CNPJ.sub(" ", bruto))

    # ---- pedido ----
    m = RE_PEDIDO_COD.search(t) or RE_PEDIDO.search(t)
    if m:
        e["numero_pedido"] = f"PED-{m.group(1)}"

    # ---- produtos e quantidade ----
    prods = extrair_produtos(t)
    if prods:
        e["produtos"] = prods
        e["produto"] = prods[0]["codigo"]
        if prods[0]["quantidade"]:
            e["quantidade"] = prods[0]["quantidade"]
    elif re.fullmatch(r"\s*(\d{1,2}|um|uma|dois|duas|tres|quatro|cinco)\s*", t):
        e["quantidade"] = _num(t.strip())

    # ---- filial / bairro ----
    for f in filiais or []:
        if re.search(rf"\b{expandir_girias(f['nome'])}\b", t):
            e["filial_id"], e["filial_nome"] = f["id"], f["nome"]
        for b in f.get("bairros", []):
            if re.search(rf"\b{expandir_girias(b)}\b", t):
                e["bairro"], e["filial_id"], e["filial_nome"] = b, f["id"], f["nome"]
                break

    # ---- período / status / pagamento ----
    per = extrair_periodo(t)
    if per:
        e["periodo"] = per
    for padrao, st in STATUS:
        if re.search(padrao, t):
            e["status"] = st
            break
    for padrao, fp in PAGAMENTO:
        if re.search(padrao, t):
            e["forma_pagamento"] = fp
            break
    m = RE_TROCO.search(t)
    if m:
        e["troco_para"] = float(m.group(1).replace(",", "."))
    elif re.search(r"sem troco|nao (precisa|preciso) de troco|valor certo|trocado", t):
        e["troco_para"] = 0.0

    # ---- CEP (depois de remover CPF/CNPJ) ----
    m = RE_CEP.search(sem_docs)
    if m:
        e["cep"] = f"{m.group(1)}-{m.group(2)}"

    # ---- endereço ----
    m = RE_ENDERECO.search(t)
    if m:
        resto = (m.group(4) or "").strip(" ,.-")
        e["endereco"] = {
            "logradouro": f"{m.group(1).title().rstrip('.')} {m.group(2).strip().title()}",
            "numero": m.group(3), "complemento_ou_bairro": resto.title(),
        }

    # ---- valores (negociação de preço) ----
    m = RE_PERCENT.search(t)
    if m:
        e["percentual"] = int(m.group(1))
    m = RE_VALOR.search(t)
    if m and "troco" not in t[max(0, m.start() - 10):m.start() + 1]:
        v = m.group(1) or m.group(2)
        e["valor"] = float(v.replace(".", "").replace(",", ".")) if v.count(",") == 1 else float(v)
    return e


def mesclar_llm(regras: dict, llm_ent: dict | None) -> dict:
    """Completa as entidades com o que a IA achou, sem sobrescrever o que as regras validaram."""
    if not llm_ent:
        return regras
    out = dict(regras)
    doc = so_digitos(str(llm_ent.get("documento") or ""))
    if doc and "documento" not in out and (cpf_valido(doc) or cnpj_valido(doc)):
        out["documento"] = doc
    for k in ("numero_pedido", "produto", "quantidade", "forma_pagamento", "status", "bairro", "troco_para"):
        v = llm_ent.get(k)
        if v not in (None, "", []) and k not in out:
            out[k] = v
    if "produto" in out and "produtos" not in out:
        out["produtos"] = [{"codigo": str(out["produto"]).upper(), "quantidade": out.get("quantidade")}]
    if out.get("numero_pedido") and not str(out["numero_pedido"]).upper().startswith("PED-"):
        out["numero_pedido"] = "PED-" + so_digitos(str(out["numero_pedido"]))
    return out
