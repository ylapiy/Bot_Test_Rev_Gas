"""Contrato que qualquer integração de ERP precisa cumprir.

O bot só conversa com o ERP através destes métodos. Hoje existem duas
implementações:
  - ErpMock (dados de demonstração em SQLite) -> funciona sem nada configurado
  - ErpRevGasHttp (API HTTP do RevGás)       -> precisa da documentação da API

Todos os métodos devolvem dicionários "normalizados" (mesmas chaves), assim
o restante do bot não depende do formato do ERP.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

STATUS_TEXTO = {
    "recebido": "Recebido pela central de pedidos",
    "em_separacao": "Em separação na filial",
    "em_rota": "Saiu para entrega",
    "entregue": "Entregue",
    "cancelado": "Cancelado",
}


class ErroERP(Exception):
    """Falha de comunicação ou regra do ERP (mensagem amigável em .args[0])."""


class ErpCliente(ABC):
    nome = "base"

    # ---------- clientes ----------
    @abstractmethod
    def buscar_cliente_por_telefone(self, telefone: str) -> dict | None: ...

    @abstractmethod
    def buscar_cliente_por_documento(self, documento: str) -> dict | None: ...

    @abstractmethod
    def cadastrar_cliente(self, nome: str, documento: str, telefone: str) -> dict: ...

    @abstractmethod
    def listar_enderecos(self, cliente_id: int | str) -> list[dict]: ...

    @abstractmethod
    def cadastrar_endereco(self, cliente_id: int | str, dados: dict) -> dict: ...

    # ---------- catálogo / filiais ----------
    @abstractmethod
    def listar_produtos(self) -> list[dict]: ...

    @abstractmethod
    def listar_filiais(self) -> list[dict]: ...

    @abstractmethod
    def consultar_preco(self, produto_codigo: str, filial_id: int | str | None = None) -> dict | None: ...

    @abstractmethod
    def consultar_estoque(self, produto_codigo: str, filial_id: int | str | None = None) -> list[dict]: ...

    def filial_por_bairro(self, bairro: str) -> dict | None:
        from app.util.texto import normalizar

        b = normalizar(bairro)
        for f in self.listar_filiais():
            if any(normalizar(x) == b for x in f.get("bairros", [])):
                return f
        return None

    # ---------- pedidos ----------
    @abstractmethod
    def criar_pedido(self, cliente_id, endereco_id, itens: list[dict], forma_pagamento: str,
                     filial_id, troco_para: float | None = None, observacao: str = "",
                     canal: str = "whatsapp") -> dict: ...

    @abstractmethod
    def consultar_pedido(self, numero: str) -> dict | None: ...

    @abstractmethod
    def listar_pedidos(self, cliente_id, inicio: str | None = None, fim: str | None = None,
                       status: str | None = None, limite: int = 10) -> list[dict]: ...

    @abstractmethod
    def cancelar_pedido(self, numero: str, motivo: str) -> dict: ...

    # ---------- financeiro ----------
    @abstractmethod
    def listar_titulos(self, cliente_id, status: str | None = None) -> list[dict]: ...

    @abstractmethod
    def segunda_via(self, titulo_numero: str) -> dict | None: ...

    # ---------- pós-venda ----------
    @abstractmethod
    def abrir_troca(self, cliente_id, pedido_numero: str | None, produto_codigo: str | None,
                    motivo: str) -> dict: ...

    # ---------- utilitário (simulação) ----------
    def atualizar_status_pedido(self, numero: str, status: str) -> dict | None:
        raise ErroERP("Atualização manual de status não suportada por este ERP.")
