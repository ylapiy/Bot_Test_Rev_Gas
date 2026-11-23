"""Text-to-SQL controlado.

Camadas de proteção (todas em código):
 1. Só existem as "tabelas virtuais" de config/sql_permissoes.json; cada uma é uma
    consulta-fonte que JÁ filtra pelo cliente da conversa (:cliente_id). O SQL
    gerado nunca enxerga dados de outro cliente.
 2. O SQL gerado é analisado (sqlglot): 1 comando, só SELECT, sem CTE própria,
    só tabelas/colunas permitidas, sem esquema explícito, sem funções perigosas.
 3. LIMIT obrigatório (máximo configurável).
 4. Conexão SOMENTE LEITURA + tempo máximo de execução.
 5. O SQL executado é o regenerado a partir da árvore validada (não o texto da IA).

Sem IA: perguntas comuns são atendidas por modelos de consulta prontos
(quantos pedidos, quanto gastei, último pedido, produto mais comprado...).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

import sqlglot
from sqlglot import exp

from app.config import config
from app.db import agora
from app.util.texto import expandir_girias

log = logging.getLogger("text_to_sql")


class SQLRecusado(Exception):
    pass


class TextToSQL:
    def __init__(self) -> None:
        self.perm = config.json_config("sql_permissoes.json")
        self.dialeto = config.sql_dialeto or self.perm.get("dialeto", "sqlite")
        self.limite = int(self.perm.get("limite_linhas", 50))
        self.tabelas: dict = self.perm["tabelas"]
        self.bloqueadas = {f.lower() for f in self.perm.get("funcoes_bloqueadas", [])}

    # ------------------------------------------------------------------ esquema p/ IA
    def esquema_texto(self) -> str:
        linhas = []
        for nome, t in self.tabelas.items():
            cols = ", ".join(f"{c} ({d})" for c, d in t["colunas"].items())
            linhas.append(f"- {nome}: {t.get('descricao', '')}. Colunas: {cols}")
        return "\n".join(linhas)

    # ------------------------------------------------------------------ validação
    def validar(self, sql: str) -> str:
        sql = (sql or "").strip().rstrip(";")
        if not sql:
            raise SQLRecusado("SQL vazio")
        try:
            comandos = sqlglot.parse(sql, read=self.dialeto)
        except sqlglot.errors.ParseError as e:
            raise SQLRecusado(f"SQL inválido: {e}") from e
        if len(comandos) != 1 or comandos[0] is None:
            raise SQLRecusado("Apenas um comando é permitido")
        arvore = comandos[0]
        if not isinstance(arvore, exp.Select):
            raise SQLRecusado("Apenas SELECT é permitido")
        if arvore.find(exp.With):
            raise SQLRecusado("CTE (WITH) não é permitida")
        proibidos = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Merge)
        if any(arvore.find(t) for t in proibidos):
            raise SQLRecusado("Comando de escrita detectado")

        usadas: set[str] = set()
        for tab in arvore.find_all(exp.Table):
            if tab.args.get("db") or tab.args.get("catalog"):
                raise SQLRecusado("Não é permitido informar esquema/banco")
            nome = tab.name.lower()
            if nome not in self.tabelas:
                raise SQLRecusado(f"Tabela não permitida: {nome}")
            usadas.add(nome)
        if not usadas:
            raise SQLRecusado("Nenhuma tabela permitida foi usada")

        colunas_ok = {c.lower() for t in usadas for c in self.tabelas[t]["colunas"]}
        aliases = {a.alias.lower() for a in arvore.find_all(exp.Alias) if a.alias}
        for col in arvore.find_all(exp.Column):
            nome = col.name.lower()
            if nome == "*" or isinstance(col.this, exp.Star):
                continue
            if nome not in colunas_ok and nome not in aliases:
                raise SQLRecusado(f"Coluna não permitida: {nome}")

        for f in arvore.find_all(exp.Func):
            nome_f = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
            if nome_f in self.bloqueadas or nome_f.startswith(("pg_", "sqlite_", "xp_", "sys")):
                raise SQLRecusado(f"Função não permitida: {nome_f}")

        lim = arvore.args.get("limit")
        valor_lim = None
        if lim is not None:
            try:
                valor_lim = int(lim.expression.this) if lim.expression is not None else None
            except (TypeError, ValueError, AttributeError):
                valor_lim = None
        if valor_lim is None or valor_lim > self.limite:
            arvore = arvore.limit(self.limite)

        corpo = arvore.sql(dialect=self.dialeto)
        ctes = ", ".join(f"{nome} AS ({self.tabelas[nome]['fonte']})" for nome in sorted(usadas))
        return f"WITH {ctes} {corpo}"

    # ------------------------------------------------------------------ execução
    def executar(self, sql_final: str, cliente_id) -> list[dict]:
        if config.sql_url and not config.sql_url.startswith("sqlite"):
            return self._executar_sqlalchemy(sql_final, cliente_id)
        caminho = config.sql_url.replace("sqlite:///", "") if config.sql_url else str(config.erp_mock_db)
        uri = Path(caminho).resolve().as_uri()
        con = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=config.sql_timeout)
        con.row_factory = sqlite3.Row
        limite_t = time.monotonic() + config.sql_timeout
        con.set_progress_handler(lambda: 1 if time.monotonic() > limite_t else 0, 10_000)
        try:
            rows = con.execute(sql_final, {"cliente_id": cliente_id}).fetchmany(self.limite)
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            raise SQLRecusado(f"Erro ao executar: {e}") from e
        finally:
            con.close()

    def _executar_sqlalchemy(self, sql_final: str, cliente_id) -> list[dict]:
        from sqlalchemy import create_engine, text  # dependência opcional (ver README)

        eng = create_engine(config.sql_url, pool_pre_ping=True)
        with eng.connect() as con:
            if eng.dialect.name == "postgresql":
                con.execute(text("SET TRANSACTION READ ONLY"))
                con.execute(text(f"SET LOCAL statement_timeout = {config.sql_timeout * 1000}"))
            rows = con.execute(text(sql_final), {"cliente_id": cliente_id}).mappings().fetchmany(self.limite)
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ geração
    def gerar_por_modelo(self, pergunta: str, ent: dict) -> tuple[str | None, str]:
        """Modelos prontos para perguntas comuns (funciona sem IA)."""
        t = expandir_girias(pergunta)
        per = ent.get("periodo")
        filtro = f" WHERE criado_em BETWEEN '{per['inicio']}' AND '{per['fim']}'" if per else ""
        e_filtro = " AND" if per else " WHERE"
        prod = ent.get("produto")
        sufixo_per = f" ({per['descricao']})" if per else ""
        if re.search(r"\bquant[oa]s? (pedidos|vezes|compras)\b", t):
            if prod:
                w = f"{filtro}{e_filtro} produto_codigo = '{prod}' AND status <> 'cancelado'"
                return f"SELECT COUNT(DISTINCT pedido_numero) AS total_pedidos, SUM(quantidade) AS unidades FROM itens_pedido{w}", f"pedidos com {prod}{sufixo_per}"
            return f"SELECT COUNT(*) AS total_pedidos FROM pedidos{filtro}{e_filtro} status <> 'cancelado'", f"total de pedidos{sufixo_per}"
        if re.search(r"\bquant[oa]s? (botijoes|galoes|unidades|botijao)\b", t):
            w = f"{filtro}{e_filtro} status <> 'cancelado'" + (f" AND produto_codigo = '{prod}'" if prod else "")
            return f"SELECT produto_codigo, SUM(quantidade) AS unidades FROM itens_pedido{w} GROUP BY produto_codigo", f"unidades compradas{sufixo_per}"
        if re.search(r"\bquanto (eu )?(gastei|paguei|comprei)\b|\btotal (que )?(gastei|gasto)\b", t):
            if prod:
                w = f"{filtro}{e_filtro} produto_codigo = '{prod}' AND status <> 'cancelado'"
                return f"SELECT SUM(subtotal) AS total_gasto FROM itens_pedido{w}", f"gasto com {prod}{sufixo_per}"
            return f"SELECT SUM(valor_total) AS total_gasto, COUNT(*) AS total_pedidos FROM pedidos{filtro}{e_filtro} status <> 'cancelado'", f"gasto total{sufixo_per}"
        if re.search(r"\bmedia\b", t):
            return f"SELECT AVG(valor_total) AS media_por_pedido, COUNT(*) AS total_pedidos FROM pedidos{filtro}{e_filtro} status <> 'cancelado'", f"média por pedido{sufixo_per}"
        if re.search(r"\bultim[oa] (pedido|compra)\b|\bquando (foi|fiz) (o |meu )?ultimo\b", t):
            w = f" WHERE produto_codigo = '{prod}'" if prod else ""
            tab = "itens_pedido" if prod else "pedidos"
            cols = "pedido_numero, descricao, quantidade, criado_em, status" if prod else "numero, criado_em, status, valor_total, forma_pagamento"
            return f"SELECT {cols} FROM {tab}{w} ORDER BY criado_em DESC LIMIT 1", "último pedido" + (f" de {prod}" if prod else "")
        if re.search(r"\bmais (comprei|compro|pedi|peco)\b|\bproduto (que )?mais\b", t):
            return f"SELECT descricao, SUM(quantidade) AS unidades FROM itens_pedido{filtro} GROUP BY descricao ORDER BY unidades DESC LIMIT 3", f"produtos mais comprados{sufixo_per}"
        if re.search(r"\bhistorico\b|\b(meus|ultimos) pedidos\b|\bo que (eu )?comprei\b", t):
            return f"SELECT numero, criado_em, status, valor_total FROM pedidos{filtro} ORDER BY criado_em DESC LIMIT 5", f"últimos pedidos{sufixo_per}"
        return None, ""

    def gerar_por_ia(self, pergunta: str, llm, erro_anterior: str | None = None) -> str | None:
        hoje = agora().strftime("%Y-%m-%d %H:%M:%S")
        prompt = (
            f"Dialeto SQL: {self.dialeto}. Data/hora atual: {hoje}.\n"
            f"Tabelas disponíveis (já filtradas para o cliente atual, NÃO filtre por cliente):\n{self.esquema_texto()}\n\n"
            "Regras: apenas um SELECT; use somente essas tabelas e colunas; sem WITH; sem comentários; "
            f"LIMIT no máximo {self.limite}; datas são texto 'AAAA-MM-DD HH:MM:SS'.\n"
            f"Pergunta do cliente: \"{pergunta}\"\n"
            + (f"Sua tentativa anterior foi recusada: {erro_anterior}. Corrija.\n" if erro_anterior else "")
            + 'Se não for possível responder com essas tabelas, use {"sql": null}. Formato: {"sql": "SELECT ..."}'
        )
        j = llm.gerar_json("Você converte perguntas de clientes em SQL seguro e somente leitura.", prompt, rapido=False)
        return (j or {}).get("sql")

    def responder(self, pergunta: str, cliente_id, ent: dict, llm=None) -> dict:
        tentativas = []
        sql, descricao = self.gerar_por_modelo(pergunta, ent)
        origem = "modelo"
        if sql is None and llm is not None:
            origem = "ia"
            sql = self.gerar_por_ia(pergunta, llm)
        if not sql:
            return {"ok": False, "motivo": "pergunta_nao_suportada", "tentativas": tentativas}
        for i in range(2):
            try:
                final = self.validar(sql)
                linhas = self.executar(final, cliente_id)
                return {"ok": True, "origem": origem, "sql_gerado": sql, "sql_executado": final,
                        "linhas": linhas, "descricao": descricao, "tentativas": tentativas}
            except SQLRecusado as e:
                tentativas.append({"sql": sql, "recusado": str(e)})
                log.warning("SQL recusado: %s | %s", e, sql)
                if llm is None or i == 1 or origem != "ia":
                    break
                sql = self.gerar_por_ia(pergunta, llm, erro_anterior=str(e))
                if not sql:
                    break
        return {"ok": False, "motivo": "sql_recusado", "tentativas": tentativas}
