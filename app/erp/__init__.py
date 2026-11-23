"""Fábrica do cliente de ERP (mock ou RevGás real), conforme ERP_MODO no .env."""
from __future__ import annotations

from app.config import config
from app.erp.base import ErpCliente, ErroERP  # noqa: F401

_instancia: ErpCliente | None = None


def obter_erp() -> ErpCliente:
    global _instancia
    if _instancia is None:
        if config.erp_modo == "revgas":
            from app.erp.revgas_http import ErpRevGasHttp

            _instancia = ErpRevGasHttp()
        else:
            from app.erp.mock import ErpMock

            _instancia = ErpMock()
    return _instancia


def redefinir_erp(novo: ErpCliente | None = None) -> None:
    """Usado em testes e no botão 'resetar demonstração'."""
    global _instancia
    _instancia = novo
