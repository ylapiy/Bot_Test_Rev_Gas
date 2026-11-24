"""ERP de demonstração (SQLite) que imita o RevGás.

Serve para testar o bot inteiro sem acesso ao ERP real. Todos os dados
(clientes, preços, estoques) são FICTÍCIOS.

O pedido PED-1003 da cliente Maria "anda sozinho" (recebido -> em separação
-> saiu para entrega -> entregue) conforme o tempo passa, para você testar
"meu pedido chegou?". Ele é reiniciado sempre que o servidor sobe.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta

from app.config import config
from app.db import agora
from app.erp.base import STATUS_TEXTO, ErpCliente, ErroERP
from app.util.documentos import so_digitos, variantes_telefone_br

_lock = threading.RLock()
FMT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE filiais (id INTEGER PRIMARY KEY, nome TEXT, bairros TEXT, prazo_min INTEGER,
    telefone TEXT, endereco TEXT, ativa INTEGER DEFAULT 1);
CREATE TABLE produtos (codigo TEXT PRIMARY KEY, nome TEXT, descricao TEXT, categoria TEXT,
    unidade TEXT, ativo INTEGER DEFAULT 1);
CREATE TABLE precos (produto_codigo TEXT, filial_id INTEGER, preco REAL, custo REAL,
    PRIMARY KEY (produto_codigo, filial_id));
CREATE TABLE estoque (produto_codigo TEXT, filial_id INTEGER, quantidade INTEGER,
    PRIMARY KEY (produto_codigo, filial_id));
CREATE TABLE clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, tipo TEXT, documento TEXT UNIQUE,
    telefone TEXT, email TEXT, limite_credito REAL DEFAULT 0, observacao_interna TEXT, criado_em TEXT);
CREATE TABLE enderecos (id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER, logradouro TEXT,
    numero TEXT, complemento TEXT, bairro TEXT, cidade TEXT, cep TEXT, referencia TEXT, principal INTEGER DEFAULT 0);
CREATE TABLE pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT UNIQUE, cliente_id INTEGER,
    filial_id INTEGER, endereco_id INTEGER, status TEXT, forma_pagamento TEXT, troco_para REAL,
    valor_produtos REAL, taxa_entrega REAL, valor_total REAL, observacao TEXT, entregador TEXT,
    auto_avanco INTEGER DEFAULT 0, canal TEXT, criado_em TEXT, atualizado_em TEXT, entregue_em TEXT);
CREATE TABLE pedido_itens (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER, produto_codigo TEXT,
    descricao TEXT, quantidade INTEGER, preco_unit REAL, subtotal REAL);
CREATE TABLE titulos (id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT UNIQUE, cliente_id INTEGER,
    pedido_numero TEXT, valor REAL, vencimento TEXT, status TEXT, linha_digitavel TEXT, link TEXT, pago_em TEXT);
CREATE TABLE trocas (id INTEGER PRIMARY KEY AUTOINCREMENT, protocolo TEXT UNIQUE, cliente_id INTEGER,
    pedido_numero TEXT, produto_codigo TEXT, motivo TEXT, status TEXT, criado_em TEXT);
"""

FILIAIS = [
    (1, "Centro", "Centro,Vila Nova,Jardim América,Boa Vista,Consolação", 20, "(11) 3000-0001", "Rua Principal, 100 - Centro"),
    (2, "Zona Norte", "Santana,Tucuruvi,Vila Maria,Parque Novo Mundo,Casa Verde", 25, "(11) 3000-0002", "Av. Norte, 2000 - Santana"),
]

# codigo, nome, descricao, categoria, unidade
PRODUTOS = [
    ("P13", "Gás P13 (13 kg) - recarga", "Botijão de gás de cozinha 13 kg. Troca do vasilhame vazio pelo cheio.", "gas", "un"),
    ("P13C", "Gás P13 completo (com vasilhame)", "Botijão 13 kg cheio + vasilhame novo, para quem não tem botijão vazio.", "gas", "un"),
    ("P45", "Gás P45 (45 kg) - recarga", "Cilindro 45 kg para comércios, restaurantes e centrais de gás.", "gas", "un"),
    ("P20", "Gás P20 (20 kg) empilhadeira", "Cilindro 20 kg para empilhadeiras.", "gas", "un"),
    ("P5", "Gás P5 (5 kg) - recarga", "Botijão pequeno 5 kg, para fogareiros e uso leve.", "gas", "un"),
    ("AGUA20", "Água mineral 20 L - recarga", "Galão de água mineral 20 litros (troca do galão vazio).", "agua", "un"),
    ("KITREG", "Kit registro + mangueira", "Regulador de pressão com mangueira certificada (INMETRO).", "acessorio", "un"),
]

# produto: (preço Centro, preço Zona Norte, custo interno)
PRECOS = {
    "P13": (115.00, 112.00, 78.00), "P13C": (290.00, 285.00, 210.00), "P45": (430.00, 425.00, 310.00),
    "P20": (205.00, 200.00, 140.00), "P5": (65.00, 63.00, 40.00), "AGUA20": (16.00, 15.00, 8.50),
    "KITREG": (59.90, 59.90, 31.00),
}
ESTOQUE = {
    "P13": (80, 45), "P13C": (10, 4), "P45": (6, 0), "P20": (4, 2), "P5": (12, 5), "AGUA20": (40, 25), "KITREG": (15, 6),
}

CLIENTES = [
    # id, nome, tipo, documento, telefone, email, limite, obs interna
    (1, "Maria Silva", "PF", "52998224725", "5511999990001", "maria@example.com", 0, "Cliente desde 2019"),
    (2, "Padaria Pão Quente LTDA", "PJ", "11222333000181", "5511999990002", "compras@example.com", 3000, "Paga no boleto 30 dias"),
    (3, "João Souza", "PF", "12345678909", "5511999990003", "", 0, ""),
]
ENDERECOS = [
    (1, 1, "Rua das Flores", "120", "Casa 2", "Vila Nova", "São Paulo", "01234-000", "Portão verde", 1),
    (2, 2, "Av. Santana", "900", "", "Santana", "São Paulo", "02000-000", "Entrada pelos fundos", 1),
    (3, 3, "Rua do Sol", "45", "Apto 12", "Centro", "São Paulo", "01010-000", "", 1),
]


def _fmt(dt: datetime) -> str:
    return dt.strftime(FMT)


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, FMT).replace(tzinfo=agora().tzinfo)


class ErpMock(ErpCliente):
    nome = "mock"

    def __init__(self, caminho=None):
        self.caminho = caminho or config.erp_mock_db
        if not self.caminho.exists():
            self.resetar_demo()
        else:
            self.preparar_pedido_em_andamento()

    # ------------------------------------------------------------------ infra
    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.caminho, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _q(self, sql: str, params=(), um=False):
        with _lock:
            con = self._con()
            try:
                cur = con.execute(sql, params)
                rows = cur.fetchall()
                con.commit()
                if um:
                    return dict(rows[0]) if rows else None
                return [dict(r) for r in rows]
            finally:
                con.close()

    def _exec(self, sql: str, params=()) -> int:
        with _lock:
            con = self._con()
            try:
                cur = con.execute(sql, params)
                con.commit()
                return cur.lastrowid
            finally:
                con.close()

    # ------------------------------------------------------------------ seed
    def resetar_demo(self) -> None:
        with _lock:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            if self.caminho.exists():
                self.caminho.unlink()
            con = sqlite3.connect(self.caminho)
            con.executescript(SCHEMA)
            con.executemany("INSERT INTO filiais (id,nome,bairros,prazo_min,telefone,endereco) VALUES (?,?,?,?,?,?)", FILIAIS)
            con.executemany("INSERT INTO produtos (codigo,nome,descricao,categoria,unidade) VALUES (?,?,?,?,?)", PRODUTOS)
            for cod, (p1, p2, custo) in PRECOS.items():
                con.execute("INSERT INTO precos VALUES (?,?,?,?)", (cod, 1, p1, custo))
                con.execute("INSERT INTO precos VALUES (?,?,?,?)", (cod, 2, p2, custo))
            for cod, (e1, e2) in ESTOQUE.items():
                con.execute("INSERT INTO estoque VALUES (?,?,?)", (cod, 1, e1))
                con.execute("INSERT INTO estoque VALUES (?,?,?)", (cod, 2, e2))
            h = agora()
            for c in CLIENTES:
                con.execute("INSERT INTO clientes (id,nome,tipo,documento,telefone,email,limite_credito,observacao_interna,criado_em) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", (*c, _fmt(h - timedelta(days=900))))
            con.executemany("INSERT INTO enderecos (id,cliente_id,logradouro,numero,complemento,bairro,cidade,cep,referencia,principal) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)", ENDERECOS)

            def ped(numero, cli, fil, end, status, pag, itens, dias_atras, minutos_atras=0, auto=0, entregador=None):
                criado = h - timedelta(days=dias_atras, minutes=minutos_atras)
                vp = sum(q * PRECOS[c][0 if fil == 1 else 1] for c, q in itens)
                entregue = _fmt(criado + timedelta(minutes=17)) if status == "entregue" else None
                cur = con.execute(
                    "INSERT INTO pedidos (numero,cliente_id,filial_id,endereco_id,status,forma_pagamento,troco_para,"
                    "valor_produtos,taxa_entrega,valor_total,observacao,entregador,auto_avanco,canal,criado_em,atualizado_em,entregue_em) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (numero, cli, fil, end, status, pag, None, vp, 0, vp, "", entregador, auto, "whatsapp",
                     _fmt(criado), _fmt(criado), entregue))
                pid = cur.lastrowid
                for c, q in itens:
                    pu = PRECOS[c][0 if fil == 1 else 1]
                    nome = next(p[1] for p in PRODUTOS if p[0] == c)
                    con.execute("INSERT INTO pedido_itens (pedido_id,produto_codigo,descricao,quantidade,preco_unit,subtotal) "
                                "VALUES (?,?,?,?,?,?)", (pid, c, nome, q, pu, pu * q))

            ped("PED-1001", 1, 1, 1, "entregue", "dinheiro", [("P13", 1)], 10, entregador="Carlos (moto 12)")
            ped("PED-1002", 1, 1, 1, "entregue", "pix", [("AGUA20", 2)], 40, entregador="Ana (moto 07)")
            ped("PED-1003", 1, 1, 1, "recebido", "pix", [("P13", 1)], 0, minutos_atras=8, auto=1, entregador="Carlos (moto 12)")
            ped("PED-1004", 2, 2, 2, "entregue", "boleto", [("P45", 2)], 5, entregador="Rafael (caminhão 03)")
            ped("PED-1005", 2, 2, 2, "entregue", "boleto", [("P45", 2)], 35, entregador="Rafael (caminhão 03)")

            con.execute("INSERT INTO titulos (numero,cliente_id,pedido_numero,valor,vencimento,status,linha_digitavel,link) VALUES (?,?,?,?,?,?,?,?)",
                        ("T-501", 2, "PED-1005", 850.00, (h - timedelta(days=5)).strftime("%Y-%m-%d"), "vencido",
                         "00000.00000 00000.000000 00000.000000 0 00000000085000", "https://exemplo.invalid/boleto/T-501"))
            con.execute("INSERT INTO titulos (numero,cliente_id,pedido_numero,valor,vencimento,status,linha_digitavel,link) VALUES (?,?,?,?,?,?,?,?)",
                        ("T-502", 2, "PED-1004", 850.00, (h + timedelta(days=25)).strftime("%Y-%m-%d"), "aberto",
                         "00000.00000 00000.000000 00000.000000 0 00000000085000", "https://exemplo.invalid/boleto/T-502"))
            con.commit()
            con.close()

    def preparar_pedido_em_andamento(self) -> None:
        """Reinicia o PED-1003 para 8 minutos atrás (demonstração de rastreio)."""
        h = agora()
        self._exec("UPDATE pedidos SET criado_em=?, atualizado_em=?, status='recebido', entregue_em=NULL, auto_avanco=1 "
                   "WHERE numero='PED-1003'", (_fmt(h - timedelta(minutes=8)), _fmt(h)))

    # ------------------------------------------------------------------ clientes
    def _cliente_publico(self, c: dict | None) -> dict | None:
        if not c:
            return None
        # NUNCA devolve campos internos (limite de crédito, observação interna)
        return {k: c[k] for k in ("id", "nome", "tipo", "documento", "telefone", "email")}

    def buscar_cliente_por_telefone(self, telefone: str) -> dict | None:
        for v in variantes_telefone_br(telefone):
            c = self._q("SELECT * FROM clientes WHERE telefone=?", (v,), um=True)
            if c:
                return self._cliente_publico(c)
        return None

    def buscar_cliente_por_documento(self, documento: str) -> dict | None:
        return self._cliente_publico(self._q("SELECT * FROM clientes WHERE documento=?", (so_digitos(documento),), um=True))

    def cadastrar_cliente(self, nome: str, documento: str, telefone: str) -> dict:
        doc = so_digitos(documento)
        if self.buscar_cliente_por_documento(doc):
            raise ErroERP("Já existe cadastro com esse documento.")
        tipo = "PJ" if len(doc) == 14 else "PF"
        cid = self._exec("INSERT INTO clientes (nome,tipo,documento,telefone,email,criado_em) VALUES (?,?,?,?,?,?)",
                         (nome, tipo, doc, so_digitos(telefone), "", _fmt(agora())))
        return self._cliente_publico(self._q("SELECT * FROM clientes WHERE id=?", (cid,), um=True))

    @staticmethod
    def _end_publico(e: dict) -> dict:
        e = dict(e)
        comp = f", {e['complemento']}" if e.get("complemento") else ""
        e["texto"] = f"{e['logradouro']}, {e['numero']}{comp} - {e['bairro']}"
        e["principal"] = bool(e.get("principal"))
        return e

    def listar_enderecos(self, cliente_id) -> list[dict]:
        rows = self._q("SELECT * FROM enderecos WHERE cliente_id=? ORDER BY principal DESC, id", (cliente_id,))
        return [self._end_publico(r) for r in rows]

    def cadastrar_endereco(self, cliente_id, dados: dict) -> dict:
        eid = self._exec(
            "INSERT INTO enderecos (cliente_id,logradouro,numero,complemento,bairro,cidade,cep,referencia,principal) VALUES (?,?,?,?,?,?,?,?,?)",
            (cliente_id, dados.get("logradouro", ""), dados.get("numero", "s/n"), dados.get("complemento", ""),
             dados.get("bairro", ""), dados.get("cidade", ""), dados.get("cep", ""), dados.get("referencia", ""),
             0 if self.listar_enderecos(cliente_id) else 1))
        return self._end_publico(self._q("SELECT * FROM enderecos WHERE id=?", (eid,), um=True))

    # ------------------------------------------------------------------ catálogo
    def listar_produtos(self) -> list[dict]:
        return self._q("SELECT codigo,nome,descricao,categoria,unidade FROM produtos WHERE ativo=1 ORDER BY rowid")

    def listar_filiais(self) -> list[dict]:
        rows = self._q("SELECT id,nome,bairros,prazo_min,telefone,endereco FROM filiais WHERE ativa=1")
        for r in rows:
            r["bairros"] = [b.strip() for b in r["bairros"].split(",")]
        return rows

    def consultar_preco(self, produto_codigo: str, filial_id=None) -> dict | None:
        filial_id = filial_id or 1
        r = self._q("SELECT p.codigo produto_codigo, p.nome produto_nome, f.id filial_id, f.nome filial_nome, pr.preco "
                    "FROM precos pr JOIN produtos p ON p.codigo=pr.produto_codigo JOIN filiais f ON f.id=pr.filial_id "
                    "WHERE pr.produto_codigo=? AND pr.filial_id=?", (produto_codigo.upper(), filial_id), um=True)
        return r  # custo NÃO é retornado

    def consultar_estoque(self, produto_codigo: str, filial_id=None) -> list[dict]:
        sql = ("SELECT e.filial_id, f.nome filial_nome, e.quantidade FROM estoque e JOIN filiais f ON f.id=e.filial_id "
               "WHERE e.produto_codigo=?")
        params: list = [produto_codigo.upper()]
        if filial_id:
            sql += " AND e.filial_id=?"
            params.append(filial_id)
        rows = self._q(sql, params)
        for r in rows:
            r["disponivel"] = r["quantidade"] > 0
        return rows

    # ------------------------------------------------------------------ pedidos
    def _status_dinamico(self, p: dict) -> dict:
        if p.get("auto_avanco") and p["status"] not in ("cancelado", "entregue"):
            fil = self._q("SELECT prazo_min FROM filiais WHERE id=?", (p["filial_id"],), um=True) or {"prazo_min": 20}
            prazo = fil["prazo_min"]
            minutos = (agora() - _parse(p["criado_em"])).total_seconds() / 60
            if minutos < 2:
                novo = "recebido"
            elif minutos < 5:
                novo = "em_separacao"
            elif minutos < prazo - 2:
                novo = "em_rota"
            else:
                novo = "entregue"
            if novo != p["status"]:
                entregue_em = _fmt(_parse(p["criado_em"]) + timedelta(minutes=prazo - 2)) if novo == "entregue" else None
                self._exec("UPDATE pedidos SET status=?, entregue_em=?, atualizado_em=? WHERE id=?",
                           (novo, entregue_em, _fmt(agora()), p["id"]))
                p["status"], p["entregue_em"] = novo, entregue_em
        return p

    def _pedido_completo(self, p: dict | None) -> dict | None:
        if not p:
            return None
        p = self._status_dinamico(p)
        itens = self._q("SELECT produto_codigo, descricao, quantidade, preco_unit, subtotal FROM pedido_itens WHERE pedido_id=?", (p["id"],))
        fil = self._q("SELECT nome, prazo_min FROM filiais WHERE id=?", (p["filial_id"],), um=True) or {}
        end = self._q("SELECT * FROM enderecos WHERE id=?", (p["endereco_id"],), um=True)
        criado = _parse(p["criado_em"])
        return {
            "numero": p["numero"], "cliente_id": p["cliente_id"], "status": p["status"],
            "status_texto": STATUS_TEXTO.get(p["status"], p["status"]), "filial_id": p["filial_id"],
            "filial_nome": fil.get("nome"), "endereco_texto": self._end_publico(end)["texto"] if end else "",
            "forma_pagamento": p["forma_pagamento"], "troco_para": p["troco_para"], "itens": itens,
            "valor_produtos": p["valor_produtos"], "taxa_entrega": p["taxa_entrega"], "valor_total": p["valor_total"],
            "criado_em": p["criado_em"],
            "previsao_entrega": _fmt(criado + timedelta(minutes=fil.get("prazo_min", config.prazo_entrega_min))),
            "entregue_em": p["entregue_em"], "entregador": p["entregador"] if p["status"] in ("em_rota", "entregue") else None,
        }

    def criar_pedido(self, cliente_id, endereco_id, itens, forma_pagamento, filial_id, troco_para=None,
                     observacao="", canal="whatsapp", *, endereco_avulso=None) -> dict:
        if not itens:
            raise ErroERP("Pedido sem itens.")
        with _lock:
            total = 0.0
            linhas = []
            for it in itens:
                cod = it["produto_codigo"].upper()
                qtd = int(it.get("quantidade", 1))
                preco = self.consultar_preco(cod, filial_id)
                est = self.consultar_estoque(cod, filial_id)
                if not preco:
                    raise ErroERP(f"Produto {cod} sem preço nesta filial.")
                if not est or est[0]["quantidade"] < qtd:
                    raise ErroERP(f"Estoque insuficiente de {preco['produto_nome']} na filial {preco['filial_nome']}.")
                linhas.append((cod, preco["produto_nome"], qtd, preco["preco"]))
                total += qtd * preco["preco"]
            ultimo = self._q("SELECT numero FROM pedidos ORDER BY id DESC LIMIT 1", um=True)
            prox = int(ultimo["numero"].split("-")[1]) + 1 if ultimo else 1001
            numero = f"PED-{prox}"
            regras = config.json_config("regras_comerciais.json")
            taxa = float(regras.get("taxa_entrega", 0))
            h = _fmt(agora())
            pid = self._exec(
                "INSERT INTO pedidos (numero,cliente_id,filial_id,endereco_id,status,forma_pagamento,troco_para,valor_produtos,"
                "taxa_entrega,valor_total,observacao,entregador,auto_avanco,canal,criado_em,atualizado_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (numero, cliente_id, filial_id, endereco_id, "recebido", forma_pagamento, troco_para, total, taxa,
                 total + taxa, observacao, "Carlos (moto 12)", 1, canal, h, h))
            for cod, nome, qtd, pu in linhas:
                self._exec("INSERT INTO pedido_itens (pedido_id,produto_codigo,descricao,quantidade,preco_unit,subtotal) VALUES (?,?,?,?,?,?)",
                           (pid, cod, nome, qtd, pu, pu * qtd))
                self._exec("UPDATE estoque SET quantidade=quantidade-? WHERE produto_codigo=? AND filial_id=?", (qtd, cod, filial_id))
        return self.consultar_pedido(numero)

    def consultar_pedido(self, numero: str) -> dict | None:
        return self._pedido_completo(self._q("SELECT * FROM pedidos WHERE numero=?", (numero.upper(),), um=True))

    def listar_pedidos(self, cliente_id, inicio=None, fim=None, status=None, limite=10) -> list[dict]:
        sql, params = "SELECT * FROM pedidos WHERE cliente_id=?", [cliente_id]
        if inicio:
            sql += " AND criado_em>=?"
            params.append(inicio)
        if fim:
            sql += " AND criado_em<=?"
            params.append(fim)
        sql += " ORDER BY criado_em DESC LIMIT ?"
        params.append(int(limite))
        pedidos = [self._pedido_completo(p) for p in self._q(sql, params)]
        if status:
            pedidos = [p for p in pedidos if p["status"] == status]
        return pedidos

    def cancelar_pedido(self, numero: str, motivo: str) -> dict:
        p = self.consultar_pedido(numero)
        if not p:
            raise ErroERP("Pedido não encontrado.")
        if p["status"] not in ("recebido", "em_separacao"):
            raise ErroERP(f"O pedido já está '{p['status_texto']}' e não pode mais ser cancelado pelo WhatsApp.")
        self._exec("UPDATE pedidos SET status='cancelado', auto_avanco=0, observacao=?, atualizado_em=? WHERE numero=?",
                   (f"Cancelado pelo cliente: {motivo}", _fmt(agora()), numero.upper()))
        return self.consultar_pedido(numero)

    def atualizar_status_pedido(self, numero: str, status: str) -> dict | None:
        if status not in STATUS_TEXTO:
            raise ErroERP("Status inválido.")
        entregue = _fmt(agora()) if status == "entregue" else None
        self._exec("UPDATE pedidos SET status=?, auto_avanco=0, entregue_em=?, atualizado_em=? WHERE numero=?",
                   (status, entregue, _fmt(agora()), numero.upper()))
        return self.consultar_pedido(numero)

    # ------------------------------------------------------------------ financeiro
    def listar_titulos(self, cliente_id, status=None) -> list[dict]:
        sql, params = "SELECT numero, pedido_numero, valor, vencimento, status FROM titulos WHERE cliente_id=?", [cliente_id]
        if status == "em_aberto":
            sql += " AND status IN ('aberto','vencido')"
        elif status:
            sql += " AND status=?"
            params.append(status)
        return self._q(sql + " ORDER BY vencimento", params)

    def segunda_via(self, titulo_numero: str, *, cliente_id=None) -> dict | None:
        titulo = self._q("SELECT numero, cliente_id, pedido_numero, valor, vencimento, status, linha_digitavel, link "
                       "FROM titulos WHERE numero=?", (titulo_numero.upper(),), um=True)
        return titulo if titulo and (cliente_id is None or str(titulo["cliente_id"]) == str(cliente_id)) else None

    # ------------------------------------------------------------------ trocas
    def abrir_troca(self, cliente_id, pedido_numero, produto_codigo, motivo) -> dict:
        n = (self._q("SELECT COUNT(*) n FROM trocas", um=True) or {"n": 0})["n"]
        protocolo = f"TR-{agora():%Y%m%d}-{n + 1:03d}"
        self._exec("INSERT INTO trocas (protocolo,cliente_id,pedido_numero,produto_codigo,motivo,status,criado_em) VALUES (?,?,?,?,?,?,?)",
                   (protocolo, cliente_id, pedido_numero, produto_codigo, motivo, "aberta", _fmt(agora())))
        return {"protocolo": protocolo, "status": "aberta", "pedido_numero": pedido_numero, "produto_codigo": produto_codigo}
