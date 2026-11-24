"""Configuração central do bot.

Tudo vem do arquivo `.env` (copie de `.env.example`). Nenhuma senha/token
fica no código. Veja GUIA_DE_CONFIGURACAO.md para o passo a passo.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")


def _txt(nome: str, padrao: str = "") -> str:
    v = os.getenv(nome)
    return v.strip() if v and v.strip() else padrao


def _bool(nome: str, padrao: bool = False) -> bool:
    v = os.getenv(nome)
    if v is None or v.strip() == "":
        return padrao
    return v.strip().lower() in ("1", "true", "sim", "s", "yes", "y", "on")


def _int(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)))
    except ValueError:
        return padrao


def _caminho(nome: str, padrao: Path) -> Path:
    v = _txt(nome)
    p = Path(v) if v else padrao
    return p if p.is_absolute() else (RAIZ / p)


class Config:
    """Lê as variáveis de ambiente. Use `config.recarregar()` em testes."""

    def __init__(self) -> None:
        self.recarregar()

    def recarregar(self) -> None:
        # ---------------- Empresa ----------------
        self.empresa_nome = _txt("EMPRESA_NOME", "Gás Exemplo")
        self.bot_nome = _txt("BOT_NOME", "Gabi")
        self.prazo_entrega_min = _int("PRAZO_ENTREGA_MINUTOS", 20)
        self.horario_humano_inicio = _txt("HORARIO_ATENDIMENTO_INICIO", "08:00")
        self.horario_humano_fim = _txt("HORARIO_ATENDIMENTO_FIM", "20:00")
        self.dias_atendimento_humano = _txt("DIAS_ATENDIMENTO", "seg,ter,qua,qui,sex,sab")
        self.fuso = _txt("FUSO_HORARIO", "America/Sao_Paulo")
        self.telefone_plantao = _txt("TELEFONE_PLANTAO_EMERGENCIA")

        # ---------------- Pastas / bancos locais ----------------
        self.erp_modo = _txt("ERP_MODO", "mock").lower()
        self.pasta_dados = _caminho("PASTA_DADOS", RAIZ / "data")
        self.pasta_dados.mkdir(parents=True, exist_ok=True)
        banco_bot = "bot-revgas.db" if self.erp_modo == "revgas" else "bot.db"
        self.bot_db = _caminho("BOT_DB_PATH", self.pasta_dados / banco_bot)
        self.pasta_conhecimento = _caminho("PASTA_CONHECIMENTO", RAIZ / "knowledge")
        self.pasta_config = _caminho("PASTA_CONFIG", RAIZ / "config")

        # ---------------- WhatsApp ----------------
        # nenhum | cloud (API oficial Meta) | evolution (não oficial, opcional)
        self.whatsapp_provedor = _txt("WHATSAPP_PROVEDOR", "nenhum").lower()
        # false = modo sandbox: recebe webhooks mas NÃO envia nada à Meta;
        # as respostas ficam visíveis no simulador (aba "Saída WhatsApp").
        self.whatsapp_envio_real = _bool("WHATSAPP_ENVIO_REAL", False)
        self.whatsapp_token = _txt("WHATSAPP_TOKEN")
        self.whatsapp_phone_number_id = _txt("WHATSAPP_PHONE_NUMBER_ID")
        self.whatsapp_waba_id = _txt("WHATSAPP_BUSINESS_ACCOUNT_ID")
        self.whatsapp_verify_token = _txt("WHATSAPP_VERIFY_TOKEN")
        self.whatsapp_app_secret = _txt("WHATSAPP_APP_SECRET")
        self.whatsapp_api_versao = _txt("WHATSAPP_API_VERSION", "v26.0")
        self.whatsapp_template_status = _txt("WHATSAPP_TEMPLATE_STATUS_PEDIDO")
        self.whatsapp_template_idioma = _txt("WHATSAPP_TEMPLATE_IDIOMA", "pt_BR")
        self.whatsapp_marcar_lido = _bool("WHATSAPP_MARCAR_COMO_LIDO", True)

        # Evolution API (alternativa NÃO oficial — leia os riscos no README)
        self.evolution_url = _txt("EVOLUTION_URL").rstrip("/")
        self.evolution_api_key = _txt("EVOLUTION_API_KEY")
        self.evolution_instancia = _txt("EVOLUTION_INSTANCIA")
        self.evolution_webhook_token = _txt("EVOLUTION_WEBHOOK_TOKEN")

        # ---------------- IA (LLM) ----------------
        self.atendimento_modo = _txt("ATENDIMENTO_MODO", "auto").lower()
        # nenhum (regras locais) | anthropic | openai (ou compatível)
        self.llm_provedor = _txt("LLM_PROVEDOR", "nenhum").lower()
        self.anthropic_api_key = _txt("ANTHROPIC_API_KEY")
        self.openai_api_key = _txt("OPENAI_API_KEY")
        self.openai_base_url = _txt("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        # Anthropic: padrões abaixo. OpenAI/compatível: informe o modelo no .env.
        padrao_modelo = "claude-sonnet-5-5" if self.llm_provedor == "anthropic" else ""
        padrao_rapido = "claude-haiku-5-5" if self.llm_provedor == "anthropic" else ""
        self.llm_modelo = _txt("LLM_MODELO", padrao_modelo)
        self.llm_modelo_rapido = _txt("LLM_MODELO_RAPIDO", padrao_rapido) or self.llm_modelo
        self.llm_timeout = _int("LLM_TIMEOUT_SEGUNDOS", 30)
        self.llm_max_iteracoes_agente = _int("AGENTE_MAX_PASSOS", 5)
        self.rerank_com_llm = _bool("RERANK_COM_LLM", True)

        # ---------------- Embeddings (busca vetorial) ----------------
        # local (sem custo, já funciona) | openai (ou compatível)
        self.embeddings_provedor = _txt("EMBEDDINGS_PROVEDOR", "local").lower()
        self.embeddings_modelo = _txt("EMBEDDINGS_MODELO", "text-embedding-3-small")
        self.embeddings_base_url = _txt("EMBEDDINGS_BASE_URL", self.openai_base_url).rstrip("/")
        self.embeddings_api_key = _txt("EMBEDDINGS_API_KEY", self.openai_api_key)

        # ---------------- ERP ----------------
        # mock (dados de demonstração) | revgas (API HTTP real)
        self.erp_mock_db = _caminho("ERP_MOCK_DB_PATH", self.pasta_dados / "erp_mock.db")
        self.erp_base_url = _txt("ERP_BASE_URL", "https://api.revgas.com/v1").rstrip("/")
        self.erp_token = _txt("ERP_TOKEN")
        self.erp_timeout = _int("ERP_TIMEOUT_SEGUNDOS", 15)
        self.erp_webhook_token = _txt("ERP_WEBHOOK_TOKEN")

        # ---------------- Text-to-SQL ----------------
        # vazio = usa o banco do ERP mock. Produção: usuário SOMENTE LEITURA.
        self.sql_url = _txt("SQL_URL")
        self.sql_dialeto = _txt("SQL_DIALETO", "sqlite")
        self.sql_timeout = _int("SQL_TIMEOUT_SEGUNDOS", 5)

        # ---------------- Handoff / atendimento humano ----------------
        self.handoff_webhook_url = _txt("HANDOFF_WEBHOOK_URL")
        self.atendente_senha = _txt("PAINEL_ATENDENTE_SENHA", "trocar-esta-senha")
        self.falhas_para_handoff = _int("FALHAS_PARA_TRANSFERIR", 2)
        # se ninguém assumir a conversa neste tempo, o bot volta a responder (o chamado continua aberto)
        self.handoff_espera_max_min = _int("HANDOFF_ESPERA_MAXIMA_MINUTOS", 10)

        # ---------------- Memória ----------------
        self.memoria_expira_min = _int("MEMORIA_EXPIRA_MINUTOS", 60)
        self.memoria_max_mensagens = _int("MEMORIA_MAX_MENSAGENS", 20)

        # ---------------- Segurança ----------------
        self.simulador_ativo = _bool("SIMULADOR_ATIVO", True)
        self.log_nivel = _txt("LOG_NIVEL", "INFO")

    # ---------------- utilidades ----------------
    def json_config(self, nome: str) -> dict:
        caminho = self.pasta_config / nome
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)

    @property
    def llm_ativo(self) -> bool:
        if not self.llm_modelo:
            return False
        if self.llm_provedor == "anthropic":
            return bool(self.anthropic_api_key)
        if self.llm_provedor == "openai":
            return bool(self.openai_api_key) or "localhost" in self.openai_base_url
        return False

    def resumo_status(self) -> dict:
        """Status de cada componente (sem expor segredos)."""
        return {
            "empresa": self.empresa_nome,
            "atendimento_modo": self.atendimento_modo,
            "whatsapp": {
                "provedor": self.whatsapp_provedor,
                "envio_real": self.whatsapp_envio_real,
                "token": bool(self.whatsapp_token),
                "phone_number_id": bool(self.whatsapp_phone_number_id),
                "verify_token": bool(self.whatsapp_verify_token),
                "app_secret": bool(self.whatsapp_app_secret),
                "api_versao": self.whatsapp_api_versao,
            },
            "llm": {"provedor": self.llm_provedor, "ativo": self.llm_ativo, "modelo": self.llm_modelo},
            "embeddings": self.embeddings_provedor,
            "erp": {"modo": self.erp_modo, "base_url": bool(self.erp_base_url)},
            "text_to_sql": "banco mock" if not self.sql_url else "banco configurado",
            "handoff_webhook": bool(self.handoff_webhook_url),
            "simulador": self.simulador_ativo,
        }


config = Config()
