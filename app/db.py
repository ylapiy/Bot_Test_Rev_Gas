"""Banco local do bot (SQLite): conversas, memória, handoffs, auditoria.

Não é o banco do ERP. Guarda apenas o estado do atendimento.
Para várias instâncias do bot em produção, troque por PostgreSQL (ver README).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canal TEXT NOT NULL,
    usuario_id TEXT NOT NULL,
    nome TEXT,
    estado TEXT NOT NULL DEFAULT 'bot',          -- bot | humano
    slots TEXT NOT NULL DEFAULT '{}',
    resumo TEXT NOT NULL DEFAULT '',
    ultima_msg_cliente TEXT,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL,
    UNIQUE(canal, usuario_id)
);
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id INTEGER NOT NULL,
    papel TEXT NOT NULL,                          -- cliente | bot | atendente | sistema
    texto TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON mensagens(conversa_id, id);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id INTEGER NOT NULL,
    motivo TEXT NOT NULL,
    prioridade TEXT NOT NULL DEFAULT 'normal',    -- normal | alta | urgente
    resumo TEXT NOT NULL,
    dados TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'aberto',        -- aberto | em_atendimento | encerrado
    atendente TEXT,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auditoria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id INTEGER,
    evento TEXT NOT NULL,
    detalhes TEXT NOT NULL DEFAULT '{}',
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ids_processados (
    id_externo TEXT PRIMARY KEY,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caixa_saida (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canal TEXT NOT NULL,
    usuario_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
"""


def agora() -> datetime:
    return datetime.now(ZoneInfo(config.fuso)).replace(microsecond=0)


def agora_iso() -> str:
    return agora().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def conexao():
    with _lock:
        con = sqlite3.connect(config.bot_db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def inicializar() -> None:
    config.bot_db.parent.mkdir(parents=True, exist_ok=True)
    with conexao() as con:
        con.executescript(SCHEMA)


def auditar(evento: str, detalhes: dict | None = None, conversa_id: int | None = None) -> None:
    from app.util.documentos import mascarar_pii_texto

    txt = mascarar_pii_texto(json.dumps(detalhes or {}, ensure_ascii=False, default=str))
    with conexao() as con:
        con.execute(
            "INSERT INTO auditoria (conversa_id, evento, detalhes, criado_em) VALUES (?,?,?,?)",
            (conversa_id, evento, txt, agora_iso()),
        )


def ja_processado(id_externo: str) -> bool:
    """Deduplicação: a Meta pode reenviar o mesmo webhook."""
    if not id_externo:
        return False
    with conexao() as con:
        try:
            con.execute("INSERT INTO ids_processados (id_externo, criado_em) VALUES (?,?)", (id_externo, agora_iso()))
            return False
        except sqlite3.IntegrityError:
            return True


def enfileirar_saida(canal: str, usuario_id: str, payload: dict) -> int:
    with conexao() as con:
        cur = con.execute(
            "INSERT INTO caixa_saida (canal, usuario_id, payload, criado_em) VALUES (?,?,?,?)",
            (canal, usuario_id, json.dumps(payload, ensure_ascii=False), agora_iso()),
        )
        return cur.lastrowid


def ler_saida(canal: str, usuario_id: str | None = None, apos_id: int = 0) -> list[dict]:
    with conexao() as con:
        if usuario_id:
            rows = con.execute(
                "SELECT * FROM caixa_saida WHERE canal=? AND usuario_id=? AND id>? ORDER BY id",
                (canal, usuario_id, apos_id),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM caixa_saida WHERE canal=? AND id>? ORDER BY id DESC LIMIT 100", (canal, apos_id)
            ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
