"""CPF, CNPJ e telefone: validação e máscara (LGPD)."""
from __future__ import annotations

import re


def so_digitos(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def cpf_valido(cpf: str) -> bool:
    d = so_digitos(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for i in (9, 10):
        soma = sum(int(d[n]) * ((i + 1) - n) for n in range(i))
        dv = (soma * 10) % 11 % 10
        if dv != int(d[i]):
            return False
    return True


def cnpj_valido(cnpj: str) -> bool:
    d = so_digitos(cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    for pesos, pos in ((pesos1, 12), (pesos2, 13)):
        soma = sum(int(d[i]) * pesos[i] for i in range(len(pesos)))
        resto = soma % 11
        dv = 0 if resto < 2 else 11 - resto
        if dv != int(d[pos]):
            return False
    return True


def formatar_cpf(cpf: str) -> str:
    d = so_digitos(cpf)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else cpf


def formatar_cnpj(cnpj: str) -> str:
    d = so_digitos(cnpj)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}" if len(d) == 14 else cnpj


def mascarar_documento(doc: str | None) -> str:
    """CPF -> ***.982.247-**   CNPJ -> **.***.333/0001-**"""
    d = so_digitos(doc)
    if len(d) == 11:
        return f"***.{d[3:6]}.{d[6:9]}-**"
    if len(d) == 14:
        return f"**.***.{d[5:8]}/{d[8:12]}-**"
    return "***"


def mascarar_telefone(tel: str | None) -> str:
    d = so_digitos(tel)
    if len(d) < 6:
        return "****"
    return f"(**) *****-{d[-4:]}"


def variantes_telefone_br(tel: str) -> list[str]:
    """O WhatsApp às vezes entrega números brasileiros SEM o 9º dígito
    (ex.: 551199990001 em vez de 5511999990001). Gera as duas formas."""
    d = so_digitos(tel)
    variantes = [d]
    if d.startswith("55") and len(d) == 13 and d[4] == "9":
        variantes.append(d[:4] + d[5:])
    elif d.startswith("55") and len(d) == 12:
        variantes.append(d[:4] + "9" + d[4:])
    if not d.startswith("55") and len(d) in (10, 11):
        variantes += variantes_telefone_br("55" + d)
    return list(dict.fromkeys(variantes))


_RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


def mascarar_pii_texto(texto: str) -> str:
    """Mascara CPF/CNPJ que apareçam em texto livre (logs, resumos)."""
    texto = _RE_CNPJ.sub(lambda m: mascarar_documento(m.group()) if cnpj_valido(m.group()) else m.group(), texto)
    texto = _RE_CPF.sub(lambda m: mascarar_documento(m.group()) if cpf_valido(m.group()) else m.group(), texto)
    return texto
