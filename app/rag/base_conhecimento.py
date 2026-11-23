"""Carrega os documentos de `knowledge/` (catálogo, políticas, manuais, garantia, FAQ)
e quebra em trechos (chunks) por seção.

Formato dos arquivos .md (veja os exemplos na pasta):
    ---
    categoria: politica        (catalogo | politica | garantia | faq | manual | regras)
    titulo: Política de troca
    ---
    ## Seção 1
    texto...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.util.texto import termos

MAX_CHARS = 1100


@dataclass
class Trecho:
    id: str
    arquivo: str
    titulo: str
    secao: str
    categoria: str
    texto: str
    termos: list[str] = field(default_factory=list)

    def resumo(self, n: int = 160) -> str:
        t = re.sub(r"\s+", " ", self.texto)
        return t[:n] + ("…" if len(t) > n else "")


def _front_matter(conteudo: str) -> tuple[dict, str]:
    meta: dict = {}
    if conteudo.startswith("---"):
        partes = conteudo.split("---", 2)
        if len(partes) == 3:
            for linha in partes[1].strip().splitlines():
                if ":" in linha:
                    k, v = linha.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            conteudo = partes[2]
    return meta, conteudo


def _dividir(texto: str) -> list[str]:
    if len(texto) <= MAX_CHARS:
        return [texto]
    blocos, atual = [], ""
    for par in re.split(r"\n\s*\n", texto):
        if len(atual) + len(par) > MAX_CHARS and atual:
            blocos.append(atual.strip())
            atual = ""
        atual += par + "\n\n"
    if atual.strip():
        blocos.append(atual.strip())
    return blocos


def carregar(pasta: Path) -> list[Trecho]:
    trechos: list[Trecho] = []
    for arq in sorted(pasta.glob("*.md")):
        meta, corpo = _front_matter(arq.read_text(encoding="utf-8"))
        # avisos internos ("> ⚠️ ...") não entram na base
        corpo = "\n".join(l for l in corpo.splitlines() if not l.lstrip().startswith("> ⚠"))
        titulo = meta.get("titulo", arq.stem.replace("_", " ").title())
        categoria = meta.get("categoria", "faq")
        secoes = re.split(r"^##\s+", corpo, flags=re.M)
        introducao = secoes[0].strip()
        if introducao:
            secoes[0] = "Introdução\n" + introducao
        else:
            secoes = secoes[1:]
        for i, sec in enumerate(secoes):
            linhas = sec.strip().split("\n", 1)
            nome_secao = linhas[0].strip("# ").strip()
            conteudo = linhas[1].strip() if len(linhas) > 1 else ""
            if not conteudo:
                continue
            for j, parte in enumerate(_dividir(conteudo)):
                t = Trecho(
                    id=f"{arq.stem}#{i}.{j}", arquivo=arq.name, titulo=titulo, secao=nome_secao,
                    categoria=categoria, texto=parte,
                )
                # título e seção entram na indexação (ajuda muito na busca por palavra-chave)
                t.termos = termos(f"{titulo} {nome_secao} {nome_secao} {parte}")
                trechos.append(t)
    return trechos
