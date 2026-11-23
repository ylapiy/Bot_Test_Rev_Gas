"""Conversa com o bot pelo terminal (sem servidor e sem WhatsApp).

Uso:
    python scripts/chat_cli.py                       # cliente Maria (cadastrada)
    python scripts/chat_cli.py --telefone 5511999990099   # número sem cadastro
    python scripts/chat_cli.py --debug               # mostra cada técnica aplicada

Para clicar num botão, digite  #id_do_botao  (ex.: #confirmar_pedido).
Comandos: /reset  /sair
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.core import memoria  # noqa: E402
from app.core.modelos import MensagemEntrada  # noqa: E402
from app.core.orquestrador import obter_orquestrador  # noqa: E402
from app.erp import obter_erp  # noqa: E402

PERFIS = {
    "5511999990001": "Maria Silva (PF, cadastrada, pedido em andamento)",
    "5511999990002": "Padaria Pão Quente (PJ, boletos)",
    "5511999990003": "João Souza (PF, sem pedidos)",
    "5511999990099": "Número sem cadastro",
}


ULTIMOS_BOTOES: dict[str, str] = {}


def imprimir(res, debug: bool) -> None:
    if debug:
        print("\033[90m" + "─" * 70)
        for e in res.rastreio.etapas:
            det = json.dumps(e["detalhe"], ensure_ascii=False, default=str)
            print(f"  [{e['tecnica']}] {e['etapa']}" + (f" → {det[:220]}" if e["detalhe"] not in (None, "ok") else ""))
        print(f"  total: {res.rastreio.total_ms()} ms" + "\033[0m")
    for r in res.respostas:
        print(f"\n\033[92m🤖 {r.texto}\033[0m")
        if r.botoes:
            ULTIMOS_BOTOES.clear()
            ULTIMOS_BOTOES.update(dict(r.botoes))
            print("   " + "  ".join(f"[#{i} {t}]" for i, t in r.botoes))
    if res.estado == "humano":
        print("\n\033[93m(conversa com atendente humano — responda pelo painel /atendente)\033[0m")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--telefone", default="5511999990001")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    db.inicializar()
    obter_erp()
    orq = obter_orquestrador()
    print(f"Simulando: {a.telefone} — {PERFIS.get(a.telefone, 'perfil livre')}\n(/reset para recomeçar, /sair para sair)\n")
    ultimo_evento = 0
    while True:
        # respostas do atendente humano (painel /atendente) chegam por aqui
        for ev in db.ler_saida("cli", a.telefone, ultimo_evento):
            ultimo_evento = ev["id"]
            print(f"\n\033[93m👤 {ev['payload']['texto']}\033[0m")
        try:
            txt = input("\033[96m👤 você: \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not txt:
            continue
        if txt == "/sair":
            break
        if txt == "/reset":
            memoria.resetar("cli", a.telefone)
            print("(conversa reiniciada)")
            continue
        if txt.startswith("#"):
            bid = txt[1:]
            msg = MensagemEntrada(canal="cli", usuario_id=a.telefone, tipo="botao", botao_id=bid, texto=ULTIMOS_BOTOES.get(bid, ""))
        else:
            msg = MensagemEntrada(canal="cli", usuario_id=a.telefone, texto=txt)
        imprimir(orq.processar(msg), a.debug)


if __name__ == "__main__":
    main()
