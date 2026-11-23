"""Guardrails — o que o bot NUNCA pode fazer, verificado em código (não só no prompt).

ENTRADA
  - emergência (vazamento/cheiro de gás/fogo): resposta de segurança imediata + atendente
  - tentativa de manipular a IA ("ignore suas instruções", "mostre o prompt")
  - mensagens enormes (corta)
POLÍTICA
  - descontos: o bot não concede nem negocia; só cita promoções cadastradas
  - acesso a dados: cliente só vê os próprios dados e só depois de verificado
  - ações de escrita (pedido, troca, cancelamento) só com confirmação explícita
SAÍDA
  - preços citados precisam ter vindo do ERP/regras neste turno (sem "inventar" valor)
  - promessas de desconto são bloqueadas
  - CPF/CNPJ completos são mascarados; campos internos (custo, margem...) bloqueados
  - limite de tamanho do WhatsApp
"""
from __future__ import annotations

import re

from app.config import config
from app.util.documentos import mascarar_pii_texto
from app.util.texto import expandir_girias, formatar_moeda

MAX_ENTRADA = 1500
MAX_SAIDA = 3900  # WhatsApp aceita até 4096 caracteres por mensagem de texto

CAMPOS_INTERNOS = {"custo", "margem", "limite_credito", "observacao_interna", "senha", "token", "lucro"}

INJECAO = [
    r"ignor[ea]\w* (as |todas as |suas |essas )?(instruc|regras|orientac)", r"esque[cç]a (as |suas )?(instruc|regras)",
    r"(mostre|revele|imprima|repita|qual e) (o |seu |as |suas )?(prompt|instrucoes do sistema|system prompt|regras internas)",
    r"\bsystem prompt\b|\bprompt do sistema\b", r"voce agora (e|eh|sera)\b", r"\bmodo (desenvolvedor|developer|dan|jailbreak)\b",
    r"\bfinja (que|ser)\b", r"\b(drop|delete|truncate|update|insert)\s+(table|from|into)\b", r";\s*--", r"\bunion\s+select\b",
    r"<\s*/?\s*(system|assistant|tool)\s*>",
]

EMERGENCIA_INSTRUCOES = (
    "⚠️ *Atenção — siga agora estas orientações de segurança:*\n"
    "1. *Não* acenda fósforo, isqueiro ou qualquer chama.\n"
    "2. *Não* acione interruptores de luz nem ligue/desligue aparelhos elétricos.\n"
    "3. Feche o registro do botijão.\n"
    "4. Abra portas e janelas.\n"
    "5. Saia do local com as pessoas e animais.\n"
    "6. Se o vazamento continuar ou houver fogo, ligue *193 (Corpo de Bombeiros)* de um lugar seguro, fora do ambiente."
)


def resposta_emergencia(equipe_disponivel: bool) -> str:
    txt = EMERGENCIA_INSTRUCOES + "\n\n"
    if equipe_disponivel:
        txt += "Já acionei nossa equipe com *prioridade máxima* para falar com você e trocar o botijão sem custo."
    else:
        txt += ("Registrei seu chamado com *prioridade máxima*. Nossa equipe de atendimento está fora do horário agora, "
                "então *não espere por nós se houver risco: ligue 193*.")
    if config.telefone_plantao:
        txt += f"\nPlantão da loja: *{config.telefone_plantao}*."
    return txt


PROMESSA_DESCONTO = re.compile(
    r"(posso|consigo|vou|irei|podemos|conseguimos)\s+(te\s+)?(dar|fazer|conceder|aplicar|liberar)\s+(um\s+|uma\s+)?(desconto|abatimento|preco especial)"
    r"|\bdesconto de\s+\d|\bfa[cç]o\s+(por|a)\s+(r\$\s?)?\d|\bsai por apenas\b|\bte dou\b.{0,20}\bdesconto\b",
    re.I,
)
VAZAMENTO_INTERNO = re.compile(
    r"(?<!sem )\bcusto\b(?! (adicional|extra|nenhum|algum|zero))|\b(margem|lucro|limite de credito|observacao interna|system prompt)\b"
    r"|\binstrucoes do sistema\b|\bselect\b.+\bfrom\b",
    re.I,
)
RE_DINHEIRO = re.compile(r"r\$\s?(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)", re.I)


def verificar_entrada(texto: str) -> dict:
    t = expandir_girias(texto or "")
    flags = []
    limpo = (texto or "")[:MAX_ENTRADA]
    if len(texto or "") > MAX_ENTRADA:
        flags.append("mensagem_cortada")
    injecao = [p for p in INJECAO if re.search(p, t)]
    if injecao:
        flags.append("tentativa_de_manipulacao")
    return {"texto": limpo, "flags": flags, "injecao": bool(injecao)}


def resposta_desconto() -> str:
    regras = config.json_config("regras_comerciais.json")
    promos = [p["descricao"] for p in regras.get("promocoes_vigentes", []) if p.get("ativa")]
    txt = ("Eu não consigo conceder descontos nem negociar preços por aqui — trabalho sempre com a tabela vigente do sistema. 🙏")
    if promos:
        txt += "\n\nPromoções em vigor:\n" + "\n".join(f"• {p}" for p in promos)
    txt += "\n\nSe for uma compra maior ou um caso especial, posso te passar para nossa equipe comercial avaliar."
    return txt


def sanitizar(obj):
    """Remove campos internos de resultados de ferramentas antes de irem para a IA/cliente."""
    if isinstance(obj, dict):
        return {k: sanitizar(v) for k, v in obj.items() if k.lower() not in CAMPOS_INTERNOS}
    if isinstance(obj, list):
        return [sanitizar(x) for x in obj]
    return obj


def valores_monetarios(obj, acc: set | None = None) -> set[str]:
    """Coleta todos os números que podem aparecer como preço (para checar a resposta)."""
    acc = acc if acc is not None else set()
    if isinstance(obj, dict):
        for v in obj.values():
            valores_monetarios(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            valores_monetarios(v, acc)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        acc.add(_norm_valor(float(obj)))
    elif isinstance(obj, str):
        for m in RE_DINHEIRO.finditer(obj):
            acc.add(_norm_valor(_parse_valor(m.group(1))))
    return acc


def _parse_valor(s: str) -> float:
    """Aceita '1.234,56', '115,00', '115' e também '115.00' (formato americano)."""
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "." in s and len(s.rsplit(".", 1)[1]) == 3:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return -1.0


def _norm_valor(v: float) -> str:
    return f"{v:.2f}"


def valores_regras() -> set[str]:
    regras = config.json_config("regras_comerciais.json")
    acc = valores_monetarios(regras)
    return acc


def verificar_saida(texto: str, permitidos: set[str] | None = None, gerado_por_ia: bool = False) -> tuple[str, list[str]]:
    flags: list[str] = []
    saida = mascarar_pii_texto(texto or "")
    if saida != texto:
        flags.append("pii_mascarada")

    if gerado_por_ia:
        # 1) promessa de desconto
        if PROMESSA_DESCONTO.search(expandir_girias(saida)):
            flags.append("bloqueado_promessa_desconto")
            return resposta_desconto(), flags
        # 2) vazamento de informação interna
        if VAZAMENTO_INTERNO.search(expandir_girias(saida)):
            flags.append("bloqueado_informacao_interna")
            return ("Desculpe, não posso compartilhar essa informação. Posso ajudar com pedidos, preços, "
                    "entregas, boletos ou trocas. 🙂"), flags
        # 3) todo valor em R$ precisa ter vindo do ERP/regras
        permitidos = (permitidos or set()) | valores_regras()
        inventados = [m.group(0) for m in RE_DINHEIRO.finditer(saida) if _norm_valor(_parse_valor(m.group(1))) not in permitidos]
        if inventados:
            flags.append(f"bloqueado_preco_nao_confirmado:{inventados}")
            return ("Não consegui confirmar esse valor no sistema agora. Posso consultar o preço atualizado "
                    "de algum produto para você?"), flags

    if len(saida) > MAX_SAIDA:
        saida = saida[:MAX_SAIDA - 20].rsplit("\n", 1)[0] + "\n(…)"
        flags.append("resposta_cortada")
    return saida, flags


def dinheiro(v) -> str:
    return formatar_moeda(v)
