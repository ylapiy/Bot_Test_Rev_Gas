"""Normalização de texto em português (acentos, gírias, radicais)."""
from __future__ import annotations

import re
import unicodedata

_STOP = """a o as os um uma uns umas de da do das dos em no na nos nas num numa por pra pro
para com sem e ou que se me te lhe vos eu tu ele ela eles elas voce voces vc vcs meu minha
meus minhas seu sua seus suas teu tua e eh ser ta esta este isso isto essa esse esses essas
aquele aquela ao aos pelo pela pelos pelas mais menos muito ja nao sim qual quais quando
onde como porque pq oi ola bom boa dia tarde noite tudo bem ai la aqui so tambem entao
gostaria queria quero poderia pode podem posso favor obrigado obrigada tem ter tenho quanto quanta quantos
quantas custa faz faco fazer saber sobre vai vou acho gente sera seria preciso dizer fala falar""".split()


def sem_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalizar(s: str) -> str:
    s = sem_acentos((s or "").lower())
    s = re.sub(r"\s+", " ", s)
    return s.strip()


STOPWORDS = {normalizar(w) for w in _STOP}

# gírias / abreviações comuns no WhatsApp -> forma canônica
GIRIAS = {
    r"\bvc\b": "voce", r"\bvcs\b": "voces", r"\bq\b": "que", r"\bpq\b": "porque",
    r"\bpfv\b": "por favor", r"\bpf\b": "por favor", r"\bpls\b": "por favor",
    r"\btb\b": "tambem", r"\btbm\b": "tambem", r"\bblz\b": "beleza", r"\bvlw\b": "valeu",
    r"\bobg\b": "obrigado", r"\bmsg\b": "mensagem", r"\bhj\b": "hoje", r"\bqdo\b": "quando",
    r"\bqnt\b": "quanto", r"\bqto\b": "quanto", r"\bcade\b": "cade", r"\bnd\b": "nada",
    r"\bbujao\b": "botijao", r"\bbutijao\b": "botijao", r"\bbotija\b": "botijao",
    r"\bbutija\b": "botijao", r"\bbujoes\b": "botijoes", r"\bgaz\b": "gas",
    r"\b2a via\b": "segunda via", r"\b2 via\b": "segunda via", r"\b2ª via\b": "segunda via",
    r"\bzap\b": "whatsapp", r"\bwpp\b": "whatsapp",
}


def expandir_girias(s: str) -> str:
    t = normalizar(s)
    for padrao, troca in GIRIAS.items():
        t = re.sub(padrao, troca, t)
    return t


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalizar(s))


_SUFIXOS = (
    "amentos", "imentos", "amento", "imento", "acoes", "icoes", "acao", "icao", "mente",
    "idades", "idade", "ando", "endo", "indo", "aram", "eram", "iram", "ados", "adas",
    "idos", "idas", "ado", "ada", "ido", "ida", "oes", "aes", "ais", "eis", "ar", "er",
    "ir", "es", "os", "as", "ao", "s", "a", "e", "o",
)


def radical(t: str) -> str:
    """Stemmer leve para português (suficiente para BM25 em base pequena)."""
    if len(t) <= 3 or t.isdigit():
        return t
    for suf in _SUFIXOS:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def termos(s: str) -> list[str]:
    """Tokens sem stopwords e reduzidos ao radical."""
    return [radical(t) for t in tokens(expandir_girias(s)) if t not in STOPWORDS]


def contem(texto_norm: str, *expressoes: str) -> bool:
    return any(re.search(e, texto_norm) for e in expressoes)


def formatar_moeda(v: float | int | None) -> str:
    if v is None:
        return "-"
    s = f"{float(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
