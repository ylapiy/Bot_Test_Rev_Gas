"""Integração HTTP com o ERP RevGás (configurável, sem mexer em código).

⚠️ Ainda NÃO temos a documentação da API do RevGás. Este adaptador lê os
caminhos, parâmetros e nomes de campos de `config/erp_revgas.json`. Quando
o fornecedor mandar a documentação, basta ajustar o JSON (ver
GUIA_DE_CONFIGURACAO.md, Etapa 5). Se a API tiver algo muito diferente
(ex.: SOAP, paginação exótica), ajustamos este arquivo.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import config
from app.erp.base import STATUS_TEXTO, ErpCliente, ErroERP
from app.util.documentos import so_digitos, variantes_telefone_br

log = logging.getLogger("erp.revgas")
_VAR = re.compile(r"\{(\w+)\}")


def _pegar(obj: Any, caminho: str | None) -> Any:
    """Lê 'a.b.0.c' de dicionários/listas."""
    if caminho in (None, ""):
        return obj
    for parte in caminho.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(parte)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(parte)
        else:
            return None
    return obj


def _render(modelo: Any, vars_: dict) -> Any:
    if isinstance(modelo, str):
        m = _VAR.fullmatch(modelo)
        if m:
            return vars_.get(m.group(1))
        return _VAR.sub(lambda x: "" if vars_.get(x.group(1)) is None else str(vars_.get(x.group(1))), modelo)
    if isinstance(modelo, dict):
        return {k: _render(v, vars_) for k, v in modelo.items()}
    if isinstance(modelo, list):
        return [_render(v, vars_) for v in modelo]
    return modelo


class ErpRevGasHttp(ErpCliente):
    nome = "revgas"

    def __init__(self):
        if not config.erp_base_url:
            raise ErroERP("ERP_BASE_URL não configurado no .env")
        self.cfg = config.json_config("erp_revgas.json")
        self.http = httpx.Client(base_url=config.erp_base_url, timeout=config.erp_timeout, headers=self._headers())

    # ------------------------------------------------------------ infra
    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if config.erp_auth_tipo == "bearer" and config.erp_token:
            h["Authorization"] = f"Bearer {config.erp_token}"
        elif config.erp_auth_tipo == "apikey" and config.erp_token:
            h[config.erp_apikey_header] = config.erp_token
        elif config.erp_auth_tipo == "basic":
            import base64

            cred = base64.b64encode(f"{config.erp_usuario}:{config.erp_senha}".encode()).decode()
            h["Authorization"] = f"Basic {cred}"
        return h

    def _mapear(self, item: Any, nome_mapa: str) -> dict:
        mapa = self.cfg.get("mapas", {}).get(nome_mapa, {})
        if not mapa:
            return item if isinstance(item, dict) else {"valor": item}
        out = {}
        for chave, origem in mapa.items():
            if isinstance(origem, dict):  # lista aninhada: {"caminho": "itens", "mapa": "item"}
                lst = _pegar(item, origem.get("caminho")) or []
                out[chave] = [self._mapear(x, origem["mapa"]) for x in lst]
            else:
                out[chave] = _pegar(item, origem)
        return out

    def _chamar(self, operacao: str, **vars_) -> Any:
        ep = self.cfg.get("endpoints", {}).get(operacao)
        if not ep:
            raise ErroERP(f"Operação '{operacao}' não configurada em config/erp_revgas.json")
        caminho = _render(ep["caminho"], vars_)
        query = {k: v for k, v in (_render(ep.get("query", {}), vars_) or {}).items() if v not in (None, "")}
        corpo = _render(ep.get("corpo"), vars_) if ep.get("corpo") is not None else None
        try:
            r = self.http.request(ep.get("metodo", "GET"), caminho, params=query or None, json=corpo)
        except httpx.HTTPError as e:
            log.error("ERP indisponível (%s): %s", operacao, e)
            raise ErroERP("O sistema da loja está fora do ar no momento.") from e
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            log.error("ERP erro %s em %s: %s", r.status_code, operacao, r.text[:500])
            raise ErroERP("Não consegui consultar o sistema da loja agora.")
        dados = r.json() if r.content else None
        if ep.get("lista") is not None:
            lst = _pegar(dados, ep["lista"]) or []
            return [self._mapear(x, ep.get("mapa", "")) for x in lst]
        if ep.get("item") is not None:
            dados = _pegar(dados, ep["item"])
        if isinstance(dados, list):
            dados = dados[0] if dados else None
        return self._mapear(dados, ep.get("mapa", "")) if dados is not None else None

    def _status(self, p: dict | None) -> dict | None:
        if not p:
            return None
        tabela = self.cfg.get("status_pedido", {})
        bruto = str(p.get("status") or "")
        p["status"] = tabela.get(bruto, tabela.get(bruto.upper(), bruto.lower()))
        p["status_texto"] = STATUS_TEXTO.get(p["status"], bruto)
        return p

    # ------------------------------------------------------------ clientes
    def buscar_cliente_por_telefone(self, telefone):
        for v in variantes_telefone_br(telefone):
            c = self._chamar("buscar_cliente_por_telefone", telefone=v)
            if isinstance(c, list):
                c = c[0] if c else None
            if c:
                return c
        return None

    def buscar_cliente_por_documento(self, documento):
        c = self._chamar("buscar_cliente_por_documento", documento=so_digitos(documento))
        return (c[0] if c else None) if isinstance(c, list) else c

    def cadastrar_cliente(self, nome, documento, telefone):
        return self._chamar("cadastrar_cliente", nome=nome, documento=so_digitos(documento), telefone=so_digitos(telefone))

    def listar_enderecos(self, cliente_id):
        ends = self._chamar("listar_enderecos", cliente_id=cliente_id) or []
        for e in ends:
            comp = f", {e['complemento']}" if e.get("complemento") else ""
            e["texto"] = e.get("texto") or f"{e.get('logradouro', '')}, {e.get('numero', '')}{comp} - {e.get('bairro', '')}"
        return ends

    def cadastrar_endereco(self, cliente_id, dados):
        return self._chamar("cadastrar_endereco", cliente_id=cliente_id, **dados)

    # ------------------------------------------------------------ catálogo
    def listar_produtos(self):
        return self._chamar("listar_produtos") or []

    def listar_filiais(self):
        fs = self._chamar("listar_filiais") or []
        extra = self.cfg.get("bairros_por_filial", {})
        for f in fs:
            if isinstance(f.get("bairros"), str):
                f["bairros"] = [b.strip() for b in f["bairros"].split(",")]
            f["bairros"] = f.get("bairros") or extra.get(str(f.get("id")), [])
            f["prazo_min"] = f.get("prazo_min") or config.prazo_entrega_min
        return fs

    def consultar_preco(self, produto_codigo, filial_id=None):
        return self._chamar("consultar_preco", produto_codigo=produto_codigo, filial_id=filial_id)

    def consultar_estoque(self, produto_codigo, filial_id=None):
        r = self._chamar("consultar_estoque", produto_codigo=produto_codigo, filial_id=filial_id) or []
        r = r if isinstance(r, list) else [r]
        for e in r:
            e["disponivel"] = (e.get("quantidade") or 0) > 0
        return r

    # ------------------------------------------------------------ pedidos
    def criar_pedido(self, cliente_id, endereco_id, itens, forma_pagamento, filial_id, troco_para=None,
                     observacao="", canal="whatsapp"):
        pedido_json = _render(self.cfg.get("modelo_corpo_pedido", {}), {
            "cliente_id": cliente_id, "endereco_id": endereco_id, "filial_id": filial_id,
            "forma_pagamento": self.cfg.get("formas_pagamento", {}).get(forma_pagamento, forma_pagamento),
            "troco_para": troco_para, "observacao": observacao, "canal": canal,
        })
        modelo_item = self.cfg.get("modelo_item_pedido", {"produto": "{produto_codigo}", "quantidade": "{quantidade}"})
        pedido_json["itens"] = [_render(modelo_item, it) for it in itens]
        log.info("Criando pedido no ERP: %s", json.dumps(pedido_json, ensure_ascii=False)[:500])
        return self._status(self._chamar("criar_pedido", pedido_json=pedido_json))

    def consultar_pedido(self, numero):
        return self._status(self._chamar("consultar_pedido", numero=numero))

    def listar_pedidos(self, cliente_id, inicio=None, fim=None, status=None, limite=10):
        ps = [self._status(p) for p in (self._chamar("listar_pedidos", cliente_id=cliente_id, inicio=inicio, fim=fim, limite=limite) or [])]
        return [p for p in ps if not status or p["status"] == status]

    def cancelar_pedido(self, numero, motivo):
        return self._status(self._chamar("cancelar_pedido", numero=numero, motivo=motivo))

    # ------------------------------------------------------------ financeiro / trocas
    def listar_titulos(self, cliente_id, status=None):
        ts = self._chamar("listar_titulos", cliente_id=cliente_id) or []
        if status == "em_aberto":
            return [t for t in ts if t.get("status") in ("aberto", "vencido")]
        return [t for t in ts if not status or t.get("status") == status]

    def segunda_via(self, titulo_numero):
        return self._chamar("segunda_via", titulo_numero=titulo_numero)

    def abrir_troca(self, cliente_id, pedido_numero, produto_codigo, motivo):
        return self._chamar("abrir_troca", cliente_id=cliente_id, pedido_numero=pedido_numero,
                            produto_codigo=produto_codigo, motivo=motivo)
