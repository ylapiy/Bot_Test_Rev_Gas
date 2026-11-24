"""Orquestrador: o "cérebro" que aplica todas as técnicas em cada mensagem.

Ordem de um turno:
  1. Conversation Memory  – carrega a conversa (slots + histórico)
  2. Guardrails (entrada) – corta abuso, detecta manipulação e EMERGÊNCIA (vazamento)
  3. Identificação        – telefone do WhatsApp -> cliente no ERP
  4. Query Rewriting      – resolve gírias/elipses/referências ("chegou?" -> pedido atual)
  5. Intent Classification
  6. Entity Extraction
  7. Query Rewriting      – consulta estruturada para as ferramentas
  8. Fluxo ativo? (pedido, identificação, confirmação) senão roteia pela intenção
  9. Tool Calling / Text-to-SQL / RAG híbrido + Reranking / Agentic RAG
 10. Human Handoff        – se algum gatilho disparou
 11. Guardrails (saída)   – preço confirmado, sem desconto, sem dado interno, PII mascarada
 12. Conversation Memory  – salva tudo
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

from app.canais import CANAIS_SEM_BOTOES
from app.config import config
from app.core import agente, ferramentas, fluxos, guardrails, handoff, memoria
from app.core.ferramentas import Contexto
from app.core.modelos import MensagemEntrada, Rastreio, ResultadoTurno
from app.core.turno import NOMES_PRODUTO, Turno
from app.db import agora
from app.erp import obter_erp
from app.erp.base import STATUS_TEXTO
from app.llm import obter_llm
from app.nlu import entidades, intencoes, reescrita
from app.util.documentos import mascarar_documento
from app.util.texto import formatar_moeda as moeda
from app.util.texto import normalizar

log = logging.getLogger("orquestrador")

BOTAO_TEXTO = {
    "novo_pedido": "quero fazer um pedido", "pedir_agora": "quero fazer um pedido", "acompanhar": "meu pedido chegou?",
    "falar_atendente": "quero falar com um atendente", "falar_comercial": "quero falar com o comercial",
    "ver_boletos": "quero ver meus boletos", "trocar": "quero solicitar uma troca", "menu": "menu",
    "trocar_agora": "quero trocar o produto que veio com defeito",
    "prod_P13": "quero um gás P13", "prod_AGUA20": "quero uma água 20 litros", "prod_outros": "quais produtos vocês têm?",
    "preco_P13": "quanto custa o P13", "preco_P45": "quanto custa o P45", "preco_AGUA20": "quanto custa a água 20 litros",
}
MENU = [("novo_pedido", "Fazer pedido"), ("acompanhar", "Acompanhar pedido"), ("falar_atendente", "Falar com atendente")]
INTENCOES_MENU = {"fazer_pedido": "novo_pedido", "status_pedido": "acompanhar",
                  "falar_atendente": "falar_atendente"}
NAO_CONTAM_COMO_ASSUNTO = {"confirmar", "negar", "saudacao", "agradecimento", "despedida", "fora_escopo"}


def para_whatsapp(texto: str) -> str:
    """Markdown -> formatação do WhatsApp."""
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", texto, flags=re.M)
    texto = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", texto)
    return texto.strip()


class Orquestrador:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._filiais: tuple[float, list] = (0.0, [])
        self.handlers = {
            "saudacao": self.h_saudacao, "fazer_pedido": self.h_fazer_pedido, "consultar_preco": self.h_preco,
            "consultar_estoque": self.h_estoque, "status_pedido": self.h_status, "cancelar_pedido": self.h_cancelar,
            "boleto": self.h_boleto, "troca": self.h_troca, "consulta_historico": self.h_historico,
            "duvida_informacao": self.h_duvida, "pedir_desconto": self.h_desconto, "reclamacao": self.h_reclamacao,
            "falar_atendente": self.h_atendente, "agradecimento": self.h_agradecimento, "despedida": self.h_agradecimento,
            "confirmar": self.h_confirmar_solto, "negar": self.h_confirmar_solto, "fora_escopo": self.h_fora_escopo,
            "emergencia_vazamento": self.h_emergencia,
        }

    # ======================================================================= entrada
    def processar(self, msg: MensagemEntrada) -> ResultadoTurno:
        with self._locks[f"{msg.canal}:{msg.usuario_id}"]:  # mensagens do mesmo cliente em ordem
            return self._processar(msg)

    def _filiais_cache(self) -> list:
        ts, fs = self._filiais
        if time.time() - ts > 300 or not fs:
            try:
                fs = obter_erp().listar_filiais()
                self._filiais = (time.time(), fs)
            except Exception:  # noqa: BLE001
                fs = fs or []
        return fs

    def _processar(self, msg: MensagemEntrada) -> ResultadoTurno:
        rastreio = Rastreio()
        conv = memoria.carregar(msg.canal, msg.usuario_id, msg.nome)
        automatico = config.atendimento_modo != "legado"
        llm = obter_llm()
        ctx = Contexto(conv=conv, rastreio=rastreio, llm=None if automatico else llm)
        texto = (msg.texto or "").strip()
        # Cada número seleciona uma opção do último turno do bot.
        opcoes = conv.slots.pop("opcoes_numeradas", None) or {}
        if msg.tipo == "texto" and texto.strip(" .)") in opcoes:
            msg.tipo, msg.botao_id = "botao", opcoes[texto.strip(" .)")]
            texto = ""
        if msg.tipo == "botao":
            # botões "de atalho" viram frases; botões de fluxo usam o título exibido
            texto = BOTAO_TEXTO.get(msg.botao_id or "") or texto or (msg.botao_id or "")
        t = Turno(msg=msg, conv=conv, ctx=ctx, texto=texto)
        rastreio.add("Conversation Memory", "conversa carregada", {
            "conversa_id": conv.id, "estado": conv.estado, "expirou": conv.expirou,
            "cliente": (conv.cliente or {}).get("nome"), "nivel_identificacao": conv.nivel,
            "fluxo": (conv.fluxo or {}).get("nome"), "pedido_atual": conv.slots.get("pedido_atual"),
            "produto": conv.slots.get("produto"), "ultima_intencao": conv.slots.get("ultima_intencao")})
        memoria.registrar_mensagem(conv, "cliente", texto or f"[{msg.tipo}]", {"tipo": msg.tipo, "botao": msg.botao_id})

        if conv.estado == "humano":
            h = handoff.ativo(conv.id)
            if h and h["status"] == "aberto" and handoff.minutos_desde(h["criado_em"]) > config.handoff_espera_max_min:
                # ninguém assumiu a tempo: o bot volta a ajudar (o chamado continua no painel)
                conv.estado = "bot"
                rastreio.add("Human Handoff", "ninguém assumiu a tempo — bot reassume (chamado segue aberto)", {"handoff_id": h["id"]})
                t.responder("Nossa equipe ainda não conseguiu assumir sua conversa — enquanto isso, sigo te ajudando por aqui. 🙂")
            elif h:
                rastreio.add("Human Handoff", "conversa com atendente humano — bot em silêncio", {"handoff_id": h["id"], "status": h["status"]})
                memoria.salvar(conv)
                return ResultadoTurno([], rastreio, None, "humano")
            else:
                conv.estado = "bot"

        g = guardrails.verificar_entrada(texto)
        t.texto = g["texto"]
        rastreio.add("Guardrails", "verificação de entrada", g["flags"] or "ok")

        # mídias que o bot não interpreta
        if msg.tipo in ("audio", "imagem", "video", "documento", "outro", "figurinha") and not texto:
            if msg.tipo == "imagem" and conv.slots.get("ultima_intencao") == "troca":
                t.responder("Recebi a foto, obrigado! 📷 Ela fica registrada no seu atendimento.")
            else:
                t.responder("Ainda não consigo ouvir áudios nem ver imagens por aqui. 🙏 Pode me escrever o que precisa?")
            return self._finalizar(t)
        if msg.tipo == "localizacao" and not ((conv.fluxo or {}).get("etapa") == "endereco_novo"):
            conv.slots["localizacao"] = msg.localizacao
            t.responder("Recebi sua localização 📍. Quer fazer um pedido para esse endereço?", [("novo_pedido", "Fazer pedido")])
            return self._finalizar(t)

        # EMERGÊNCIA vem antes de tudo (inclusive de fluxos em andamento)
        pre = intencoes.classificar_regras(t.texto)
        if pre["intencao"] == "emergencia_vazamento":
            t.cls = pre
            rastreio.add("Intent Classification", "EMERGÊNCIA detectada por regra", {"gatilhos": pre["gatilhos"]})
            self.h_emergencia(t)
            return self._finalizar(t)
        if g["injecao"]:
            rastreio.add("Guardrails", "tentativa de manipulação bloqueada", None)
            pergunta = conv.slots.get("pergunta_fluxo") if conv.fluxo else None
            if pergunta:
                t.responder(pergunta["texto"], pergunta["botoes"])
            else:
                t.responder("Posso ajudar com pedidos de gás e água. Escolha uma opção.", MENU)
            return self._finalizar(t)

        comando = normalizar(t.texto) if msg.tipo == "texto" else ""
        if automatico and (comando in ("sair", "menu") or t.botao == "menu"):
            conv.set_fluxo(None)
            conv.slots.pop("cadastro", None)
            if comando == "sair":
                t.responder("Atendimento encerrado. Envie *menu* para começar de novo.")
                return self._finalizar(t)
            comando = "menu"

        sem_erp_real = conv.canal == "evolution" and config.whatsapp_envio_real and config.erp_modo != "revgas"
        if sem_erp_real:
            conv.set_fluxo(None)

        if automatico and comando == "continuar" and conv.fluxo:
            pergunta = conv.slots.get("pergunta_fluxo")
            if pergunta:
                t.responder(pergunta["texto"], pergunta["botoes"])
            else:
                t.responder("Para retomar, responda à última pergunta do atendimento. Digite *sair* para encerrar.")
            return self._finalizar(t)

        if automatico:
            # A IA interpreta a entrada. Os fluxos e as consultas seguem regras locais.
            if not conv.fluxo and not t.botao:
                cls = intencoes.classificar(t.texto, llm=llm, permitidas=(*INTENCOES_MENU, "fora_escopo")) if (
                    llm is not None and comando not in ("menu", "continuar")
                ) else {}
                if cls.get("origem") != "ia" or cls["confianca"] < 0.60 or cls["intencao"] == "fora_escopo":
                    rastreio.add("Atendimento", "menu guiado", "IA ausente, indisponível ou interpretação incerta")
                    t.responder("Como posso ajudar? Escolha uma opção.", MENU)
                    return self._finalizar(t)
                t.cls = {**cls, "secundarias": []}
            elif conv.fluxo and not t.botao and (conv.fluxo.get("nome") == "confirmar_acao"
                    or conv.fluxo.get("etapa") in ("confirmacao", "alterar", "endereco_confirmar", "pagamento")):
                pergunta = conv.slots.get("pergunta_fluxo")
                if pergunta:
                    t.responder(pergunta["texto"], pergunta["botoes"])
                    return self._finalizar(t)
                # Conversas anteriores à atualização voltam a exibir as opções da etapa.
                t.cls = {"intencao": "fora_escopo", "confianca": 0, "secundarias": [], "origem": "menu"}
        if sem_erp_real:
            t.ctx.llm = None
            motivo = dict(MENU).get(t.botao or INTENCOES_MENU.get(t.intencao))
            if motivo:
                t.ctx.pedidos_handoff.append({"motivo": motivo, "prioridade": "normal"})
            else:
                t.responder("Nossa equipe pode ajudar com pedidos e entregas.", MENU)
            return self._finalizar(t)
        fluxos.identificar_por_telefone(t)
        self._nlu(t)

        if conv.fluxo:
            fluxo = conv.fluxo
            rastreio.add("Conversation Memory", f"fluxo em andamento: {fluxo.get('nome')} / {fluxo.get('etapa')}", None)
            r = fluxos.continuar(t)
            if isinstance(r, dict) and r.get("retomar"):
                self._retomar(t, r["retomar"])
            elif r is False:
                pergunta = conv.slots.get("pergunta_fluxo")
                if automatico and not t.botao and pergunta:
                    conv.slots["fluxo"] = fluxo
                    t.cls = {}
                    t.responder(pergunta["texto"], pergunta["botoes"])
                else:
                    self._rotear(t, interrompeu=True)
        else:
            self._rotear(t)
        return self._finalizar(t)

    # ======================================================================= NLU
    def _nlu(self, t: Turno) -> None:
        conv, llm = t.conv, t.ctx.llm
        hist = memoria.historico_texto(conv, 6)
        exp = reescrita.expandir(t.texto, conv.slots, llm, hist)
        t.texto_exp = exp["texto"]
        t.rastreio.add("Query Rewriting", "normalização e contexto", {"original": t.texto, "reescrita": t.texto_exp,
                                                                       "regras": exp["regras"] or ["só normalização"]})
        cls = t.cls or intencoes.classificar(t.texto_exp, hist, None if conv.fluxo else llm)
        t.cls = cls
        t.ctx.intencao = cls["intencao"]
        t.rastreio.add("Intent Classification", f"{cls['intencao']} ({cls['confianca']:.0%}, {cls.get('origem')})",
                       {"secundarias": cls.get("secundarias"), "gatilhos": cls.get("gatilhos")})
        filiais = self._filiais_cache()
        ent = entidades.extrair(t.texto_exp, filiais)
        orig = entidades.extrair(t.texto, filiais)
        for k in ("documento", "cpf", "cnpj", "cep", "documento_invalido", "endereco", "troco_para"):
            if k in orig:
                ent[k] = orig[k]
            elif k in ent and k not in orig and k in ("documento", "cpf", "cnpj"):
                ent.pop(k)
        sugeridas = cls.get("entidades_ia") or {}
        if config.atendimento_modo != "legado":
            produto = sugeridas.get("produto")
            quantidade = sugeridas.get("quantidade")
            sugeridas = {}
            if isinstance(produto, str) and produto in NOMES_PRODUTO:
                sugeridas["produto"] = produto
            if type(quantidade) is int and 1 <= quantidade <= 999:
                sugeridas["quantidade"] = quantidade
        ent = entidades.mesclar_llm(ent, sugeridas)
        t.ent = ent
        t.ctx.entidades = ent
        visiveis = {k: (mascarar_documento(v) if k in ("documento", "cpf", "cnpj", "documento_invalido") else v) for k, v in ent.items()}
        t.rastreio.add("Entity Extraction", f"{len(ent)} entidade(s)", visiveis or None)
        if ent.get("produto"):
            conv.slots["produto"] = ent["produto"]
        consulta = reescrita.estruturar(cls["intencao"], ent, conv.slots, conv.cliente, t.texto_exp)
        t.rastreio.add("Query Rewriting", "consulta estruturada", consulta)

    # ======================================================================= roteamento
    def _pendente(self, t: Turno) -> dict:
        return {"intencao": t.intencao, "texto": t.texto_exp, "ent": t.ent}

    def _rotear(self, t: Turno, interrompeu: bool = False) -> None:
        conv = t.conv
        if t.botao and t.botao.startswith("2via_"):
            self.h_segunda_via(t, t.botao[5:])
            return
        if t.botao in ("falar_atendente", "falar_comercial"):
            t.cls = {**t.cls, "intencao": "falar_atendente"}
        intencao = t.intencao

        # documento informado espontaneamente ("meu cpf é ..., cadê meu pedido?")
        if t.ent.get("documento") and conv.nivel < 2 and not conv.fluxo:
            pend = self._pendente(t) if intencao not in NAO_CONTAM_COMO_ASSUNTO else None
            conv.set_fluxo("identificacao", "documento", pendente=pend, tentativas=0)
            r = fluxos.continuar_identificacao(t)
            if isinstance(r, dict) and r.get("retomar"):
                self._retomar(t, r["retomar"])
            return

        secundarias = [s for s in t.cls.get("secundarias", []) if s in self.handlers and s not in NAO_CONTAM_COMO_ASSUNTO
                       and s != "emergencia_vazamento"]
        if secundarias and intencao not in NAO_CONTAM_COMO_ASSUNTO | {"falar_atendente", "fazer_pedido"}:
            self._multi_intencao(t, [intencao] + secundarias[:1])
        else:
            self.handlers.get(intencao, self.h_fora_escopo)(t)

        if t.respostas and intencao not in ("saudacao", "emergencia_vazamento") and not conv.slots.get("cumprimentou") \
                and re.match(r"^(oi+|ola|bom dia|boa tarde|boa noite|opa|e ai)\b", t.texto_exp):
            nome = t.primeiro_nome()
            t.respostas[0].texto = f"Olá{', ' + nome if nome else ''}! 👋 " + t.respostas[0].texto
        conv.slots["cumprimentou"] = True
        if intencao != "fora_escopo":
            conv.slots["falhas_seguidas"] = 0
        if interrompeu and conv.fluxo and conv.fluxo.get("nome") == "pedido" and conv.estado == "bot":
            t.responder("📝 Seu pedido continua em aberto.", [("continuar_pedido", "Continuar pedido"), ("cancelar_fluxo", "Descartar")])

    def _multi_intencao(self, t: Turno, lista: list[str]) -> None:
        """Agentic RAG: uma mensagem com mais de um assunto vira um plano com subtarefas."""
        t.rastreio.add("Agentic RAG", "mensagem com vários assuntos — plano de subtarefas", lista)
        if t.ctx.llm is not None and agente.executar_com_ia(t, "multi-assunto"):
            return
        for i, intencao in enumerate(lista):
            if i > 0 and (t.conv.fluxo and t.conv.fluxo.get("nome") != "confirmar_acao" or t.conv.estado == "humano"):
                break
            t.cls = {**t.cls, "intencao": intencao}
            self.handlers.get(intencao, self.h_fora_escopo)(t)
        t.cls = {**t.cls, "intencao": lista[0]}

    def _retomar(self, t: Turno, pend: dict) -> None:
        t.rastreio.add("Conversation Memory", "retomando o assunto pendente após identificação", pend.get("intencao"))
        if pend.get("intencao") == "retomar_pedido":
            t.conv.slots["fluxo"] = pend["pedido"]
            fluxos.avancar_pedido(t)
            return
        t.cls = {**t.cls, "intencao": pend["intencao"], "secundarias": []}
        t.ent = {**(pend.get("ent") or {}), **{k: v for k, v in t.ent.items() if k not in ("documento", "cpf", "cnpj", "cep")}}
        t.ctx.entidades = t.ent
        t.texto_exp = pend.get("texto") or t.texto_exp
        self.handlers.get(pend["intencao"], self.h_fora_escopo)(t)

    def _exige_verificado(self, t: Turno, motivo: str) -> bool:
        if t.conv.nivel >= 2 and t.conv.cliente and t.conv.cliente.get("id"):
            return True
        t.rastreio.add("Guardrails", "dados da conta exigem cliente verificado", {"nivel": t.conv.nivel})
        fluxos.iniciar_identificacao(t, self._pendente(t), motivo)
        return False

    # ======================================================================= saída
    def _finalizar(self, t: Turno) -> ResultadoTurno:
        conv = t.conv
        if t.intencao not in NAO_CONTAM_COMO_ASSUNTO and t.cls:
            conv.slots["ultima_intencao"] = t.intencao
            hist = conv.slots.setdefault("historico_intencoes", [])
            hist.append(t.intencao)
            conv.slots["historico_intencoes"] = hist[-15:]
        if t.ctx.pedidos_handoff and conv.estado != "humano":
            h = sorted(t.ctx.pedidos_handoff, key=lambda x: {"urgente": 0, "alta": 1}.get(x["prioridade"], 2))[0]
            r = handoff.transferir(conv, h["motivo"], h["prioridade"], t.ctx.llm)
            t.rastreio.add("Human Handoff", f"transferido ({h['prioridade']})", {"motivo": h["motivo"], "handoff_id": r["handoff_id"],
                                                                               "resumo": r["resumo"]})
            if h.get("avisar", True):
                t.responder(handoff.mensagem_ao_cliente(h["prioridade"]))
        saida, todas_flags = [], []
        for r in t.respostas:
            txt, flags = guardrails.verificar_saida(para_whatsapp(r.texto), t.ctx.evidencias, r.ia)
            r.texto = txt
            todas_flags += flags
            saida.append(r)
        t.rastreio.add("Guardrails", "verificação de saída", todas_flags or "ok")
        if config.atendimento_modo != "legado":
            if conv.fluxo and conv.estado == "bot" and saida:
                pergunta = saida[-1]
                conv.slots["pergunta_fluxo"] = {"texto": pergunta.texto, "botoes": list(pergunta.botoes)}
                if 0 < len(pergunta.botoes) < 3 and not any(bid == "menu" for bid, _ in pergunta.botoes):
                    pergunta.botoes.append(("menu", "Voltar ao menu"))
                pergunta.texto += "\n\n_Digite *menu* para voltar ao início e descartar o fluxo atual._"
            elif not conv.fluxo or conv.estado != "bot":
                conv.slots.pop("pergunta_fluxo", None)
        numerar_texto = conv.canal in CANAIS_SEM_BOTOES
        if numerar_texto or config.atendimento_modo != "legado":
            opcoes = {}
            for r in saida:
                if r.botoes:
                    itens = []
                    for bid, titulo in r.botoes:
                        numero = str(len(opcoes) + 1)
                        opcoes[numero] = bid
                        itens.append(f"*{numero}.* {titulo}")
                    r.texto += "\n\n_Responda com o número:_\n" + "\n".join(itens)
                if numerar_texto:
                    r.botoes = []
            if opcoes:
                conv.slots["opcoes_numeradas"] = opcoes
        for r in saida:
            memoria.registrar_mensagem(conv, "bot", r.texto, {"botoes": r.botoes})
        memoria.salvar(conv)
        t.rastreio.add("Conversation Memory", "conversa salva", {"slots": sorted(k for k in conv.slots if k != "historico_intencoes")})
        return ResultadoTurno(saida, t.rastreio, t.intencao if t.cls else None, conv.estado)

    # ======================================================================= handlers
    def h_emergencia(self, t: Turno) -> None:
        t.conv.set_fluxo(None)
        t.responder(guardrails.resposta_emergencia(handoff.dentro_do_horario()))
        t.ctx.pedidos_handoff.append({"motivo": "EMERGÊNCIA: possível vazamento/cheiro de gás", "prioridade": "urgente", "avisar": False})

    def h_saudacao(self, t: Turno) -> None:
        nome = t.primeiro_nome()
        txt = (f"Olá{', ' + nome if nome else ''}! 👋 Sou a {config.bot_nome}, assistente virtual da *{config.empresa_nome}*.\n"
               "Posso anotar seu pedido de gás ou água, informar preços, acompanhar a entrega, enviar 2ª via de boleto e ajudar com trocas.")
        extra = fluxos._regras().get("mensagem_boas_vindas_extra")
        if extra:
            txt += f"\n\n{extra}"
        ped = t.conv.slots.get("pedido_atual")
        if ped and t.conv.nivel >= 2:
            p = ferramentas.executar("consultar_pedido", {"numero_pedido": ped}, t.ctx)
            if not p.get("erro") and p["status"] not in ("entregue", "cancelado"):
                txt += f"\n\nVi que seu pedido *{p['numero']}* está: *{p['status_texto']}*."
        t.responder(txt + "\n\nComo posso ajudar?", MENU)

    def h_fazer_pedido(self, t: Turno) -> None:
        # "Pedir agora" logo depois de consultar preço/estoque: usa o produto em contexto
        if not t.ent.get("produtos") and t.botao == "pedir_agora" and t.conv.slots.get("produto"):
            t.ent["produtos"] = [{"codigo": t.conv.slots["produto"], "quantidade": None}]
            t.rastreio.add("Conversation Memory", "produto em contexto usado no pedido", t.conv.slots["produto"])
        fluxos.iniciar_pedido(t)

    def _filial_para_preco(self, t: Turno) -> tuple[int | None, str | None]:
        fid = t.ent.get("filial_id") or t.conv.slots.get("filial_id")
        if fid:
            return fid, None
        if config.erp_modo == "revgas":
            return None, None
        return 1, "Preço da filial Centro — pode variar um pouco conforme o bairro."

    def h_preco(self, t: Turno) -> None:
        prods = [p["codigo"] for p in t.ent.get("produtos", [])] or ([t.conv.slots["produto"]] if t.conv.slots.get("produto") else [])
        if not prods:
            t.responder("De qual produto você quer saber o preço?", [("preco_P13", "Gás P13"), ("preco_P45", "Gás P45"),
                                                                      ("preco_AGUA20", "Água 20 L")])
            return
        fid, nota = self._filial_para_preco(t)
        linhas, filial_nome = [], None
        for cod in prods[:4]:
            r = ferramentas.executar("consultar_preco", {"produto_codigo": cod, "filial_id": fid}, t.ctx)
            if r.get("erro"):
                linhas.append(f"• {NOMES_PRODUTO.get(cod, cod)}: {r['erro']}")
            else:
                filial_nome = r.get("filial_nome")
                linhas.append(f"• *{r['produto_nome']}*: {moeda(r['preco'])}")
        cab = f"💰 Preço atual{' (filial ' + filial_nome + ')' if filial_nome else ''}:"
        txt = cab + "\n" + "\n".join(linhas) + (f"\n_{nota}_" if nota else "") + "\n\nQuer fazer o pedido?"
        t.responder(txt, [("pedir_agora", "Pedir agora"), ("acompanhar", "Acompanhar pedido")])

    def h_estoque(self, t: Turno) -> None:
        prods = [p["codigo"] for p in t.ent.get("produtos", [])] or ([t.conv.slots["produto"]] if t.conv.slots.get("produto") else [])
        if not prods:
            t.responder("Qual produto você procura? Ex.: P13, P45, água 20 L, kit registro.")
            return
        fid = t.ent.get("filial_id") or t.conv.slots.get("filial_id")
        linhas = []
        for cod in prods[:3]:
            r = ferramentas.executar("consultar_estoque", {"produto_codigo": cod}, t.ctx)
            est = r.get("estoque") or []
            # quantidade exata é informação interna: o cliente só vê disponível / indisponível
            if fid:
                minha = next((e for e in est if e["filial_id"] == fid), None)
                outras = [e["filial_nome"] for e in est if e["filial_id"] != fid and e["disponivel"]]
                if minha and minha["disponivel"]:
                    linhas.append(f"✅ *{NOMES_PRODUTO.get(cod, cod)}*: disponível para entrega no seu endereço.")
                else:
                    extra = f" (a filial {outras[0]} tem — posso verificar com a equipe)" if outras else ""
                    linhas.append(f"❌ *{NOMES_PRODUTO.get(cod, cod)}*: sem estoque na sua região no momento{extra}.")
            else:
                disp = [e["filial_nome"] for e in est if e["disponivel"]]
                linhas.append(f"✅ *{NOMES_PRODUTO.get(cod, cod)}*: disponível ({', '.join(disp)})." if disp
                              else f"❌ *{NOMES_PRODUTO.get(cod, cod)}*: sem estoque no momento.")
        if any(ln.startswith("✅") for ln in linhas):
            t.responder("\n".join(linhas) + "\n\nQuer pedir?", [("pedir_agora", "Pedir agora")])
        else:
            t.responder("\n".join(linhas), [("falar_atendente", "Falar com atendente"), ("prod_outros", "Ver outros produtos")])

    def h_status(self, t: Turno) -> None:
        if not self._exige_verificado(t, "Para consultar seu pedido, preciso confirmar quem é você."):
            return
        num = t.ent.get("numero_pedido") or t.conv.slots.get("pedido_atual")
        p = ferramentas.executar("consultar_pedido", {"numero_pedido": num} if num else {}, t.ctx)
        if p.get("erro"):
            t.responder(f"Não encontrei {'o pedido ' + num if num else 'pedido em andamento'} no seu cadastro. Quer fazer um pedido agora?",
                        [("novo_pedido", "Fazer pedido"), ("falar_atendente", "Falar com atendente"), ("menu", "Voltar ao menu")])
            return
        st = p["status"]
        prev = _hora(p.get("previsao_entrega"))
        txt = f"📦 *Pedido {p['numero']}*\nStatus: *{p['status_texto']}*"
        detalhes = fluxos.detalhes_pedido(p)
        if detalhes:
            txt += f"\n\n{detalhes}"
        menu = ("menu", "Voltar ao menu")
        if st in ("recebido", "em_separacao", "em_rota"):
            t.conv.slots["pedido_atual"] = p["numero"]
            if prev:
                txt += f"\nPrevisão de entrega: até *{prev}*."
            if st == "em_rota" and p.get("entregador"):
                txt += f"\nEntregador: {p['entregador']}"
            if _atrasado(p.get("previsao_entrega")):
                txt += "\n\nA entrega passou do horário previsto. A central pode verificar o andamento."
            t.responder(txt, [("acompanhar", "Atualizar status"), ("falar_atendente", "Chamar a central"), menu])
            return
        t.conv.slots.pop("pedido_atual", None)
        if st == "entregue":
            entregue = _hora(p.get("entregue_em"))
            if entregue:
                txt += f"\nEntregue às *{entregue}*."
            t.responder(txt, [("trocar", "Solicitar troca"), ("novo_pedido", "Novo pedido"), menu])
        else:
            t.responder(txt, [("novo_pedido", "Novo pedido"), ("falar_atendente", "Chamar a central"), menu])

    def h_cancelar(self, t: Turno) -> None:
        if not self._exige_verificado(t, "Para cancelar um pedido, preciso confirmar quem é você."):
            return
        num = t.ent.get("numero_pedido") or t.conv.slots.get("pedido_atual")
        p = ferramentas.executar("consultar_pedido", {"numero_pedido": num} if num else {}, t.ctx)
        if p.get("erro") or p.get("status") in ("entregue", "cancelado"):
            t.responder("Não encontrei pedido em andamento para cancelar. 🤔")
            return
        if p["status"] in ("recebido", "em_separacao"):
            fluxos.preparar_confirmacao(t, {"acao": "cancelar_pedido", "numero_pedido": p["numero"], "motivo": t.texto[:200]},
                                        f"Confirma o *cancelamento* do pedido *{p['numero']}* (total {moeda(p['valor_total'])})?")
        else:
            t.responder(f"O pedido *{p['numero']}* já *saiu para entrega*, então não consigo cancelar por aqui. "
                        "Quer que eu chame a central?", [("falar_atendente", "Chamar a central")])

    def h_boleto(self, t: Turno) -> None:
        if not self._exige_verificado(t, "Para ver seus boletos, preciso confirmar quem é você."):
            return
        r = ferramentas.executar("consultar_financeiro", {"status": t.ent.get("status") if t.ent.get("status") in
                                                          ("vencido", "pago", "em_aberto") else "em_aberto"}, t.ctx)
        tits = r.get("titulos") or []
        if not tits:
            t.responder("Você não tem boletos em aberto. 🎉")
            return
        if len(tits) == 1:
            self.h_segunda_via(t, tits[0]["numero"])
            return
        linhas = [f"• *{x['numero']}* — {moeda(x['valor'])} — venc. {_data(x['vencimento'])}" + (" ⚠️ vencido" if x["status"] == "vencido" else "")
                  for x in tits]
        t.responder("📄 Seus boletos em aberto:\n" + "\n".join(linhas) + "\n\nDe qual você quer a 2ª via?",
                    [(f"2via_{x['numero']}", f"2ª via {x['numero']}") for x in tits[:3]])

    def h_segunda_via(self, t: Turno, numero: str) -> None:
        if not self._exige_verificado(t, "Para enviar a 2ª via, preciso confirmar quem é você."):
            return
        b = ferramentas.executar("segunda_via_boleto", {"titulo_numero": numero}, t.ctx)
        if b.get("erro"):
            t.responder("Não encontrei esse boleto no seu cadastro.")
            return
        aviso = "\n⚠️ Este boleto está vencido." if b.get("status") == "vencido" else ""
        linha = f"\n\nLinha digitável:\n{b['linha_digitavel']}" if b.get("linha_digitavel") else ""
        link = f"\n\nLink: {b['link']}" if b.get("link") else ""
        t.responder(f"🧾 2ª via do boleto *{b['numero']}*\nValor: {moeda(b['valor'])} — vencimento {_data(b['vencimento'])}{aviso}{linha}{link}")
        t.cls = {**t.cls, "intencao": "boleto"}

    def h_troca(self, t: Turno) -> None:
        if not self._exige_verificado(t, "Para verificar a troca, preciso confirmar seus dados."):
            return
        if t.ctx.llm is not None and agente.executar_com_ia(t, "troca"):
            return
        agente.plano_troca(t)

    def h_historico(self, t: Turno) -> None:
        if not self._exige_verificado(t, "Para consultar seu histórico, preciso confirmar quem é você."):
            return
        r = ferramentas.executar("consultar_historico_sql", {"pergunta": t.texto_exp}, t.ctx)
        if r.get("erro"):
            if t.ctx.llm is not None and agente.executar_com_ia(t, "histórico"):
                return
            t.responder("Não consegui responder isso pelo seu histórico. Posso mostrar seus *últimos pedidos* ou chamar um atendente.",
                        [("falar_atendente", "Falar com atendente")])
            return
        if t.ctx.llm is not None:
            try:
                txt = t.ctx.llm.gerar(
                    "Responda ao cliente em português do Brasil, em até 4 linhas, usando SOMENTE os dados fornecidos. Valores em R$ no formato R$ 1.234,56.",
                    [{"role": "user", "content": f"Pergunta: {t.texto}\nResultado da consulta ({r.get('descricao')}): {r['linhas']}"}],
                    max_tokens=300, rapido=True)
                if txt:
                    t.responder(txt, ia=True)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("IA falhou ao redigir histórico: %s", e)
        t.responder(_formatar_linhas(r.get("descricao") or "Seu histórico", r["linhas"]))

    def h_duvida(self, t: Turno) -> None:
        if t.ctx.llm is not None and agente.executar_com_ia(t, "dúvida"):
            return
        if agente.responder_com_base(t):
            return
        self.h_fora_escopo(t, ja_tentou_base=True)

    def h_desconto(self, t: Turno) -> None:
        t.rastreio.add("Guardrails", "política de desconto aplicada (bot não negocia preço)", None)
        t.responder(guardrails.resposta_desconto(), [("falar_comercial", "Falar com comercial"), ("novo_pedido", "Fazer pedido")])

    def h_reclamacao(self, t: Turno) -> None:
        nome = t.primeiro_nome()
        t.responder(f"Sinto muito por isso{', ' + nome if nome else ''}. 😔 Sua reclamação é importante e vou passar agora para um "
                    "responsável, com o resumo do que aconteceu.")
        t.ctx.pedidos_handoff.append({"motivo": "Reclamação do cliente", "prioridade": "alta"})

    def h_atendente(self, t: Turno) -> None:
        motivo = "Cliente pediu negociação comercial" if t.botao == "falar_comercial" else "Cliente pediu para falar com atendente"
        t.ctx.pedidos_handoff.append({"motivo": motivo, "prioridade": "normal"})

    def h_agradecimento(self, t: Turno) -> None:
        nome = t.primeiro_nome()
        t.responder(f"Por nada{', ' + nome if nome else ''}! 😊 Quando precisar de gás ou água, é só chamar.")

    def h_confirmar_solto(self, t: Turno) -> None:
        t.responder("Certo! Em que posso ajudar?", MENU)

    def h_fora_escopo(self, t: Turno, ja_tentou_base: bool = False) -> None:
        if t.ctx.llm is not None and agente.executar_com_ia(t, "geral"):
            return
        if not ja_tentou_base and len(t.texto_exp.split()) >= 3 and agente.responder_com_base(t, limiar=0.5):
            return
        falhas = int(t.conv.slots.get("falhas_seguidas", 0)) + 1
        t.conv.slots["falhas_seguidas"] = falhas
        if falhas >= config.falhas_para_handoff:
            t.conv.slots["falhas_seguidas"] = 0
            t.responder("Acho que não estou conseguindo te ajudar direito. 😅")
            t.ctx.pedidos_handoff.append({"motivo": f"Bot não entendeu {falhas} mensagens seguidas", "prioridade": "normal"})
            return
        t.responder("Desculpe, não entendi. 😅 Posso ajudar com:\n• pedido de gás ou água\n• preços e disponibilidade\n"
                    "• acompanhar a entrega\n• 2ª via de boleto\n• trocas e dúvidas", MENU)


# ======================================================================= utilidades
def _parse(dt: str | None) -> datetime | None:
    try:
        return datetime.strptime((dt or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _hora(dt: str | None) -> str | None:
    d = _parse(dt)
    return d.strftime("%H:%M") if d else None


def _data(d: str | None) -> str:
    try:
        return datetime.strptime((d or "")[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return d or "-"


def _atrasado(previsao: str | None) -> bool:
    d = _parse(previsao)
    return bool(d and agora().replace(tzinfo=None) > d)


ROTULOS = {"total_pedidos": "Pedidos", "total_gasto": "Total gasto", "unidades": "Unidades", "media_por_pedido": "Média por pedido",
           "valor_total": "Valor", "criado_em": "Data", "status": "Situação", "numero": "Pedido", "pedido_numero": "Pedido",
           "descricao": "Produto", "quantidade": "Qtd.", "produto_codigo": "Produto", "forma_pagamento": "Pagamento",
           "subtotal": "Subtotal", "valor": "Valor", "vencimento": "Vencimento"}
MONETARIOS = {"total_gasto", "media_por_pedido", "valor_total", "subtotal", "valor", "preco_unit"}


def _formatar_linhas(descricao: str, linhas: list[dict]) -> str:
    if not linhas:
        return f"📊 *{descricao[:1].upper() + descricao[1:]}*: não encontrei registros."
    out = []
    for ln in linhas:
        partes = []
        for k, v in ln.items():
            if v is None:
                v = 0 if k in MONETARIOS | {"total_pedidos", "unidades"} else "-"
            if k in MONETARIOS:
                v = moeda(v)
            elif k == "criado_em":
                v = (_parse(str(v)) or datetime.now()).strftime("%d/%m/%Y")
            elif k == "status":
                v = STATUS_TEXTO.get(v, v)
            elif k == "produto_codigo":
                v = NOMES_PRODUTO.get(v, v)
            partes.append(f"{ROTULOS.get(k, k)}: *{v}*")
        out.append("• " + " | ".join(partes))
    return f"📊 *{descricao[:1].upper() + descricao[1:]}*\n" + "\n".join(out)


_orquestrador: Orquestrador | None = None


def obter_orquestrador() -> Orquestrador:
    global _orquestrador
    if _orquestrador is None:
        _orquestrador = Orquestrador()
    return _orquestrador
