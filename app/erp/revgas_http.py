"""Adapta o contrato público RevGás v1 aos dados usados nas conversas."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from app.config import config
from app.db import agora
from app.erp.base import STATUS_TEXTO, ErpCliente, ErroERP
from app.util.documentos import so_digitos, variantes_telefone_br

log = logging.getLogger("erp.revgas")

PAGAMENTOS = {
    "pix": "PIX", "dinheiro": "MONEY",
    "cartao_debito": "CARTAO_DE_DEBITO", "cartao_credito": "CARTAO_DE_CREDITO",
}
ENDERECO = {
    "logradouro": "location", "numero": "number", "bairro": "neighborhood",
    "cidade": "city", "uf": "state_federation", "complemento": "complement",
    "referencia": "landmark",
}


def _id(valor):
    try:
        identificador = int(str(valor))
        if identificador > 0:
            return identificador
    except (TypeError, ValueError):
        pass
    raise ErroERP("Identificador inválido no sistema da loja.")


def _id_pedido(numero):
    return _id(re.sub(r"(?i)^PED-", "", str(numero)))


def _dinheiro(valor):
    try:
        decimal = Decimal(str(valor))
        if decimal.is_finite() and decimal > 0 and decimal == decimal.quantize(Decimal("0.01")):
            return decimal
    except InvalidOperation:
        pass
    raise ErroERP("O produto está sem um preço válido na tabela da loja.")


def _data_hora(valor):
    return str(valor).replace("T", " ", 1)[:19] if valor else None


def _texto_endereco(dados):
    return ", ".join(str(dados[k]) for k in ("location", "number", "complement", "neighborhood", "city")
                     if dados.get(k))


class ErpRevGasHttp(ErpCliente):
    nome = "revgas"
    endereco_avulso_na_venda = True

    def __init__(self, *, transport=None):
        url = httpx.URL(config.erp_base_url)
        if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
            raise ErroERP("ERP_BASE_URL precisa ser a URL HTTPS base da API.")
        if not config.erp_token:
            raise ErroERP("Preencha ERP_TOKEN no .env com a chave da RevGás.")
        self.filiais = config.json_config("erp_revgas.json").get("filiais", {})
        self.http = httpx.Client(
            base_url=url, timeout=config.erp_timeout, transport=transport,
            headers={"Accept": "application/json", "Authorization": config.erp_token},
        )

    def _chamar(self, metodo, caminho, *, query=None, corpo=None, aceita_404=False):
        params = {k: v for k, v in (query or {}).items() if v is not None}
        try:
            resposta = self.http.request(metodo, caminho, params=params, json=corpo)
        except httpx.HTTPError as exc:
            log.warning("Falha de transporte RevGás (%s, %s)", metodo, type(exc).__name__)
            if metodo != "GET":
                raise ErroERP("A RevGás não confirmou a gravação. Confira no painel antes de reenviar.") from exc
            raise ErroERP("O sistema da loja está indisponível no momento.") from exc
        if aceita_404 and resposta.status_code == 404:
            return None
        if not resposta.is_success:
            log.warning("RevGás retornou HTTP %s (%s)", resposta.status_code, metodo)
            if resposta.status_code in (401, 403):
                raise ErroERP(f"A RevGás recusou a credencial ou a permissão (HTTP {resposta.status_code}).")
            if resposta.status_code == 429:
                raise ErroERP("A consulta à RevGás atingiu o limite. Tente novamente em instantes.")
            if metodo != "GET" and resposta.status_code >= 500:
                raise ErroERP("A RevGás não confirmou a gravação. Confira no painel antes de reenviar.")
            raise ErroERP(f"A RevGás não concluiu a operação (HTTP {resposta.status_code}).")
        try:
            dados = resposta.json()
        except ValueError as exc:
            raise ErroERP("A RevGás retornou uma resposta inválida.") from exc
        if not isinstance(dados, dict):
            raise ErroERP("A RevGás retornou uma resposta em formato inesperado.")
        return dados

    def _listar(self, caminho, *, limite=None, **query):
        pagina = 1
        tamanho = min(limite, 100) if limite else 100
        while True:
            dados = self._chamar("GET", caminho, query={**query, "page": pagina, "page_size": tamanho})
            linhas = dados.get("data")
            if not isinstance(linhas, list) or any(not isinstance(x, dict) for x in linhas):
                raise ErroERP("A RevGás retornou uma listagem em formato inesperado.")
            yield from linhas
            paginacao = dados.get("pagination") or {}
            if not paginacao.get("has_next"):
                return
            if not linhas or paginacao.get("page", pagina) != pagina:
                raise ErroERP("A RevGás retornou uma paginação inconsistente.")
            pagina += 1

    @staticmethod
    def _cliente(c):
        return {
            "id": _id(c.get("id")), "nome": c.get("name") or c.get("fantasy_name"),
            "tipo": "PJ" if c.get("type") == "JURIDICAL" or c.get("cnpj") else "PF",
            "documento": c.get("cpf") or c.get("cnpj"),
            "telefone": next(iter(c.get("phones") or []), None),
        }

    def buscar_cliente_por_telefone(self, telefone):
        digitos = so_digitos(telefone)
        if not (len(digitos) in (10, 11) or (len(digitos) in (12, 13) and digitos.startswith("55"))):
            return None
        variantes = variantes_telefone_br(digitos)
        variantes.extend(v[2:] for v in list(variantes) if v.startswith("55") and len(v) > 11)
        encontrados = {}
        for variante in dict.fromkeys(variantes):
            for cliente in self._listar("/customers", phone=variante):
                if any(so_digitos(tel) in variantes for tel in cliente.get("phones") or []):
                    encontrados[cliente["id"]] = cliente
                if len(encontrados) > 1:
                    return None
        return self._cliente(next(iter(encontrados.values()))) if encontrados else None

    def buscar_cliente_por_documento(self, documento):
        documento = so_digitos(documento)
        if len(documento) not in (11, 14):
            return None
        return next((self._cliente(c) for c in self._listar("/customers", cpf_or_cnpj=documento)
                     if so_digitos(c.get("cpf") or c.get("cnpj")) == documento), None)

    def cadastrar_cliente(self, nome, documento, telefone):
        documento = so_digitos(documento)
        if len(documento) not in (11, 14):
            raise ErroERP("Informe um CPF ou CNPJ válido para cadastrar o cliente.")
        pj = len(documento) == 14
        corpo = {"name": nome, "type": "JURIDICAL" if pj else "PHYSICAL",
                 "cnpj" if pj else "cpf": documento, "phones": [so_digitos(telefone)]}
        return self._cliente(self._chamar("POST", "/customers", corpo=corpo))

    @staticmethod
    def _endereco(c):
        return {
            **{k: c.get(v) for k, v in ENDERECO.items()},
            "id": _id(c.get("id")), "cep": c.get("postal_code"),
            "texto": _texto_endereco(c), "principal": True,
        }

    def listar_enderecos(self, cliente_id):
        cliente = self._chamar("GET", f"/customers/{_id(cliente_id)}", aceita_404=True)
        return [self._endereco(cliente)] if cliente and cliente.get("location") else []

    def cadastrar_endereco(self, cliente_id, dados):
        # O endereço principal do cadastro é único. O checkout envia endereços avulsos na própria venda.
        raise ErroERP("O endereço avulso é registrado na venda.")

    def listar_filiais(self):
        return [{
            "id": _id(f.get("id")),
            "nome": f.get("description") or f.get("display_name") or f.get("name") or str(f["id"]),
            "bairros": self.filiais.get(str(f["id"]), {}).get("bairros", []),
            "prazo_min": config.prazo_entrega_min,
            "endereco": _texto_endereco(f),
        } for f in self._listar("/establishments")]

    def _produto_configurado(self, codigo, filial_id):
        return self.filiais.get(str(filial_id), {}).get("produtos", {}).get(codigo.upper())

    def _produtos_filial(self, filial_id):
        return {str(p["id"]): p for p in self._listar("/products", establishment_id=_id(filial_id), inactive=False)
                if str(p.get("establishment_id")) == str(filial_id) and p.get("id")}

    def listar_produtos(self):
        catalogo = {}
        for filial, f in self.filiais.items():
            produtos = self._produtos_filial(filial)
            for codigo, oferta in f.get("produtos", {}).items():
                produto = produtos.get(str(oferta.get("id")))
                if produto:
                    catalogo.setdefault(codigo, {"codigo": codigo, "nome": produto.get("description") or codigo})
        return list(catalogo.values())

    def consultar_preco(self, produto_codigo, filial_id=None):
        if filial_id is None:
            if len(self.filiais) != 1:
                raise ErroERP("Informe o bairro da entrega para eu consultar o preço da filial.")
            filial_id = next(iter(self.filiais))
        oferta = self._produto_configurado(produto_codigo, filial_id)
        if not oferta:
            raise ErroERP("O produto está sem um preço cadastrado para essa filial.")
        preco = _dinheiro(oferta.get("preco"))
        produto = self._produtos_filial(filial_id).get(str(oferta.get("id")))
        if not produto:
            return None
        return {"produto_codigo": produto_codigo, "produto_nome": produto.get("description") or produto_codigo,
                "filial_id": _id(filial_id), "filial_nome": str(produto.get("establishment_description") or filial_id),
                "preco": float(preco)}

    def consultar_estoque(self, produto_codigo, filial_id=None):
        resultado = []
        for filial in ([str(filial_id)] if filial_id is not None else self.filiais):
            oferta = self._produto_configurado(produto_codigo, filial)
            if not oferta:
                continue
            produto = self._produtos_filial(filial).get(str(oferta.get("id")))
            if not produto:
                continue
            quantidade = produto.get("stock_quantity")
            if type(quantidade) is not int:
                raise ErroERP("A RevGás não retornou o saldo do produto.")
            resultado.append({"filial_id": _id(filial), "filial_nome": str(produto.get("establishment_description") or filial),
                              "quantidade": quantidade, "disponivel": quantidade > 0})
        return resultado

    def _pedido(self, venda):
        id_venda = _id(venda.get("id"))
        status_api = str(venda.get("status") or "")
        entregue = _data_hora(venda.get("delivered_at"))
        status = ("cancelado" if status_api == "CANCEL" else "entregue" if entregue else
                  "recebido" if status_api in {"OPEN", "ACTIVE", "WARN", "WARNING", "CONFIRM", "CHECK"} else "desconhecido")
        filial = venda.get("establishment_id")
        codigos = {str(p["id"]): c for c, p in self.filiais.get(str(filial), {}).get("produtos", {}).items()}
        itens = [{"produto_codigo": codigos.get(str(i.get("product_id")), str(i.get("product_id") or "")),
                  "descricao": i.get("product_description"), "quantidade": i.get("quantity")}
                 for i in venda.get("products") or []]
        pagamento = next(iter(venda.get("payments") or []), {})
        forma = next((k for k, v in PAGAMENTOS.items() if v == pagamento.get("type")), None)
        abertura = next((l.get("created_at") for l in venda.get("logs") or [] if l.get("status") == "OPEN"), None)
        return {"numero": f"PED-{id_venda}", "cliente_id": venda.get("customer_id"), "status": status,
                "status_texto": STATUS_TEXTO.get(status, "Status em atualização na loja"),
                "filial_id": filial, "filial_nome": venda.get("establishment_description"),
                "itens": itens, "valor_total": venda.get("total"), "forma_pagamento": forma,
                "endereco_texto": _texto_endereco(venda), "entregador": venda.get("deliverer_name"),
                "criado_em": _data_hora(abertura or venda.get("competence")), "entregue_em": entregue}

    def criar_pedido(self, cliente_id, endereco_id, itens, forma_pagamento, filial_id, troco_para=None,
                     observacao="", canal="whatsapp", *, endereco_avulso=None):
        tipo_pagamento = PAGAMENTOS.get(forma_pagamento)
        if not tipo_pagamento:
            raise ErroERP("Essa forma de pagamento precisa ser confirmada por um atendente.")
        if config.json_config("regras_comerciais.json").get("taxa_entrega", 0):
            raise ErroERP("A equipe precisa confirmar a taxa de entrega para registrar a venda.")
        permitidos = {p.get("payment_type") for p in self._listar(
            "/payment-types", establishment_id=_id(filial_id), status="ACTIVE")}
        if tipo_pagamento not in permitidos:
            raise ErroERP("A forma de pagamento escolhida não está ativa na filial.")
        estoque = self._produtos_filial(filial_id)
        produtos = {}
        total = Decimal(0)
        for item in itens:
            oferta = self._produto_configurado(item["produto_codigo"], filial_id)
            if not oferta:
                raise ErroERP("Produto sem configuração de venda na filial.")
            id_prod = _id(oferta.get("id"))
            preco = _dinheiro(oferta.get("preco"))
            if preco != _dinheiro(item.get("preco_unit")):
                raise ErroERP("O preço mudou desde a confirmação. Confira os valores com a equipe.")
            quantidade = _id(item.get("quantidade"))
            linha = produtos.setdefault(id_prod, {"product_id": id_prod, "quantity": 0, "value": float(preco)})
            linha["quantity"] += quantidade
            saldo = estoque.get(str(id_prod), {}).get("stock_quantity")
            if type(saldo) is not int or linha["quantity"] > saldo:
                raise ErroERP("O estoque mudou. A equipe pode confirmar a disponibilidade.")
            total += preco * quantidade
        if not produtos:
            raise ErroERP("O pedido está sem produtos.")
        if troco_para and Decimal(str(troco_para)) < total:
            raise ErroERP("O valor informado para troco é inferior ao total.")
        if endereco_avulso:
            endereco = {v: endereco_avulso[k] for k, v in ENDERECO.items() if endereco_avulso.get(k)}
        else:
            if _id(endereco_id) != _id(cliente_id):
                raise ErroERP("Endereço inválido para este cliente.")
            cliente = self._chamar("GET", f"/customers/{_id(cliente_id)}")
            endereco = {v: cliente[v] for v in ENDERECO.values() if cliente.get(v)}
        if not all(endereco.get(campo) for campo in ("location", "number", "neighborhood")):
            raise ErroERP("O endereço de entrega está incompleto.")
        bairro = self.filial_por_bairro(endereco["neighborhood"])
        if not bairro or bairro["id"] != _id(filial_id):
            raise ErroERP("O endereço de entrega precisa ser confirmado pela filial.")
        if troco_para:
            observacao = f"{observacao}. Troco para R$ {troco_para:.2f}".strip(". ")
        venda = self._chamar("POST", "/sales", corpo={
            **endereco, "customer_id": _id(cliente_id), "establishment_id": _id(filial_id),
            "products": list(produtos.values()), "payments": [{"payment_type": tipo_pagamento, "value": float(total)}],
            "observation": observacao, "channel": "WHATSAPP", "origin": "INTEGRATION",
            "option": "NORMAL", "broadcast": "NEW_SALE",
        })
        pedido = self._pedido(venda)
        if pedido["valor_total"] is None or Decimal(str(pedido["valor_total"])) != total:
            raise ErroERP(f"A venda {pedido['numero']} foi registrada com total diferente. Confira no painel antes de reenviar.")
        if pedido["status"] == "cancelado":
            raise ErroERP(f"A RevGás devolveu a venda {pedido['numero']} como cancelada.")
        pedido.update(
            itens=[{**i, "subtotal": float(_dinheiro(i["preco_unit"]) * _id(i["quantidade"]))} for i in itens],
            valor_produtos=float(total), taxa_entrega=0, forma_pagamento=forma_pagamento,
            troco_para=troco_para, endereco_texto=_texto_endereco(endereco),
        )
        return pedido

    def consultar_pedido(self, numero):
        venda = self._chamar("GET", f"/sales/{_id_pedido(numero)}", aceita_404=True)
        return self._pedido(venda) if venda else None

    def listar_pedidos(self, cliente_id, inicio=None, fim=None, status=None, limite=10):
        if limite <= 0:
            return []
        hoje = agora().date()
        fim_api = str(fim)[:10] if fim else hoje.isoformat()
        inicio_api = str(inicio)[:10] if inicio else (date.fromisoformat(fim_api) - timedelta(days=365)).isoformat()
        pedidos = []
        for venda in self._listar("/sales", limite=limite, customer_id=_id(cliente_id),
                                  **{"from": inicio_api, "to": fim_api}):
            if str(venda.get("customer_id")) != str(cliente_id):
                continue
            pedido = self._pedido(venda)
            if not status or pedido["status"] == status:
                pedidos.append(pedido)
                if len(pedidos) == limite:
                    break
        return pedidos

    def cancelar_pedido(self, numero, motivo):
        venda = self._chamar("PUT", f"/sales/{_id_pedido(numero)}/cancel", corpo={"motive_cancel_sale_id": None})
        pedido = self._pedido(venda)
        if pedido["status"] != "cancelado":
            raise ErroERP("A RevGás não confirmou o cancelamento da venda.")
        return pedido

    @staticmethod
    def _titulo(t, numero):
        vencimento = t.get("due_date") or t.get("due")
        try:
            vencido = bool(vencimento and date.fromisoformat(vencimento[:10]) < agora().date())
        except ValueError:
            vencido = False
        return {"numero": str(numero), "cliente_id": t.get("customer_id"),
                "valor": t.get("total") if t.get("total") is not None else t.get("value"),
                "vencimento": vencimento, "status": "vencido" if vencido else "aberto"}

    def listar_titulos(self, cliente_id, status=None):
        hoje = agora().date()
        contas = self._listar("/future-receipts", customer_id=_id(cliente_id), payment_type="FETLOCK",
                             **{"from": (hoje - timedelta(days=365)).isoformat(), "to": hoje.isoformat()})
        titulos = [self._titulo(t, t["id"]) for t in contas
                   if t.get("id") and str(t.get("customer_id")) == str(cliente_id)]
        return [t for t in titulos if status in (None, "em_aberto", t["status"])]

    def segunda_via(self, titulo_numero, *, cliente_id=None):
        caminho = f"/future-receipts/{_id(titulo_numero)}"
        titulo = self._chamar("GET", caminho, aceita_404=True)
        if not titulo or cliente_id is None or str(titulo.get("customer_id")) != str(cliente_id):
            return None
        resposta = self._chamar("GET", f"{caminho}/invoice", aceita_404=True)
        fatura = (resposta or {}).get("invoice") or {}
        boleto = fatura.get("bank_slip") or {}
        link = fatura.get("secure_url")
        link = link if isinstance(link, str) and link.startswith("https://") else None
        if not link and not boleto.get("digitable_line"):
            return None
        return {**self._titulo(titulo, titulo_numero), "link": link, "linha_digitavel": boleto.get("digitable_line")}

    def abrir_troca(self, cliente_id, pedido_numero, produto_codigo, motivo):
        raise ErroERP("A equipe da loja confirma as trocas pelo atendimento humano.")
