"""Conversation Memory.

Guarda, por conversa (canal + telefone):
  - slots: cliente identificado, pedido atual, produto, filial, carrinho, etapa do
    fluxo, confirmação pendente, falhas seguidas, última intenção...
  - histórico de mensagens (últimas N vão de contexto para a IA)
  - resumo (para o handoff)
Depois de MEMORIA_EXPIRA_MINUTOS sem mensagens, o fluxo em andamento é descartado,
mas a identificação do cliente é mantida.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from app.config import config
from app.db import agora, agora_iso, conexao

# slots que sobrevivem à expiração da conversa
SLOTS_PERSISTENTES = {"cliente", "nivel_identificacao", "pedido_atual", "ultimo_pedido_itens", "canal_origem"}


@dataclass
class Conversa:
    id: int
    canal: str
    usuario_id: str
    nome: str = ""
    estado: str = "bot"
    slots: dict = field(default_factory=dict)
    resumo: str = ""
    ultima_msg_cliente: str | None = None
    atualizado_em: str = ""
    expirou: bool = False

    # ---------- atalhos ----------
    @property
    def cliente(self) -> dict | None:
        return self.slots.get("cliente")

    @property
    def nivel(self) -> int:
        """0 = desconhecido | 1 = informou CPF/CNPJ | 2 = verificado (telefone do cadastro ou CEP confirmado)"""
        return int(self.slots.get("nivel_identificacao", 0))

    @property
    def fluxo(self) -> dict | None:
        return self.slots.get("fluxo")

    def set_fluxo(self, nome: str | None, etapa: str | None = None, **dados) -> None:
        if nome is None:
            self.slots.pop("fluxo", None)
        else:
            atual = self.slots.get("fluxo") or {}
            base = atual if atual.get("nome") == nome else {"nome": nome}
            base.update({"etapa": etapa, **dados})
            self.slots["fluxo"] = base


def carregar(canal: str, usuario_id: str, nome: str = "") -> Conversa:
    h = agora_iso()
    with conexao() as con:
        r = con.execute("SELECT * FROM conversas WHERE canal=? AND usuario_id=?", (canal, usuario_id)).fetchone()
        if not r:
            cur = con.execute(
                "INSERT INTO conversas (canal, usuario_id, nome, criado_em, atualizado_em) VALUES (?,?,?,?,?)",
                (canal, usuario_id, nome, h, h))
            return Conversa(id=cur.lastrowid, canal=canal, usuario_id=usuario_id, nome=nome, atualizado_em=h)
    conv = Conversa(id=r["id"], canal=r["canal"], usuario_id=r["usuario_id"], nome=r["nome"] or nome,
                    estado=r["estado"], slots=json.loads(r["slots"] or "{}"), resumo=r["resumo"] or "",
                    ultima_msg_cliente=r["ultima_msg_cliente"], atualizado_em=r["atualizado_em"])
    if nome and not conv.nome:
        conv.nome = nome
    # expiração do contexto
    try:
        ultima = datetime.strptime(conv.atualizado_em, "%Y-%m-%d %H:%M:%S").replace(tzinfo=agora().tzinfo)
        if (agora() - ultima).total_seconds() > config.memoria_expira_min * 60 and conv.estado == "bot":
            conv.slots = {k: v for k, v in conv.slots.items() if k in SLOTS_PERSISTENTES}
            conv.expirou = True
    except ValueError:
        pass
    return conv


def salvar(conv: Conversa) -> None:
    with conexao() as con:
        con.execute(
            "UPDATE conversas SET nome=?, estado=?, slots=?, resumo=?, ultima_msg_cliente=?, atualizado_em=? WHERE id=?",
            (conv.nome, conv.estado, json.dumps(conv.slots, ensure_ascii=False, default=str), conv.resumo,
             conv.ultima_msg_cliente, agora_iso(), conv.id))


def obter_por_id(conversa_id: int) -> Conversa | None:
    with conexao() as con:
        r = con.execute("SELECT canal, usuario_id FROM conversas WHERE id=?", (conversa_id,)).fetchone()
    return carregar(r["canal"], r["usuario_id"]) if r else None


def registrar_mensagem(conv: Conversa, papel: str, texto: str, meta: dict | None = None) -> None:
    with conexao() as con:
        con.execute("INSERT INTO mensagens (conversa_id, papel, texto, meta, criado_em) VALUES (?,?,?,?,?)",
                    (conv.id, papel, texto, json.dumps(meta or {}, ensure_ascii=False, default=str), agora_iso()))
    if papel == "cliente":
        conv.ultima_msg_cliente = agora_iso()


def historico(conv: Conversa, limite: int | None = None) -> list[dict]:
    limite = limite or config.memoria_max_mensagens
    with conexao() as con:
        rows = con.execute("SELECT papel, texto, criado_em FROM mensagens WHERE conversa_id=? ORDER BY id DESC LIMIT ?",
                           (conv.id, limite)).fetchall()
    return [dict(r) for r in reversed(rows)]


def historico_texto(conv: Conversa, limite: int = 8) -> str:
    nomes = {"cliente": "Cliente", "bot": "Assistente", "atendente": "Atendente", "sistema": "Sistema"}
    return "\n".join(f"{nomes.get(m['papel'], m['papel'])}: {m['texto']}" for m in historico(conv, limite))


def historico_para_llm(conv: Conversa, limite: int = 10) -> list[dict]:
    """Formato user/assistant alternado, exigido pelas APIs de IA."""
    msgs: list[dict] = []
    for m in historico(conv, limite):
        role = "user" if m["papel"] == "cliente" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + m["texto"]
        else:
            msgs.append({"role": role, "content": m["texto"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def resetar(canal: str, usuario_id: str) -> None:
    with conexao() as con:
        r = con.execute("SELECT id FROM conversas WHERE canal=? AND usuario_id=?", (canal, usuario_id)).fetchone()
        if r:
            con.execute("DELETE FROM mensagens WHERE conversa_id=?", (r["id"],))
            con.execute("DELETE FROM handoffs WHERE conversa_id=?", (r["id"],))
            con.execute("DELETE FROM conversas WHERE id=?", (r["id"],))
