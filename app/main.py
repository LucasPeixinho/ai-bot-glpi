"""FastAPI: identificacao do colaborador + hub "Meus chamados" + chat por
thread (1 thread = 1 chamado, mais a conversa de ajuda antes do chamado
existir), com streaming.

Roda em 127.0.0.1:8100 (uvicorn). Apache faz o reverse proxy na Fase 5.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app import db, relay
from app.agente import (
    aguardar_mcp_pronto,
    carregar_catalogo,
    construir_opcoes,
    construir_opcoes_acompanhamento,
)
from app.autenticacao_ad import LDAPIndisponivel, autenticar
from app.glpi import (
    STATUS_FECHADO,
    STATUS_REABERTURA,
    STATUS_RESOLVIDO,
    STATUS_RESOLVIDO_OU_FECHADO,
    aprovar_solucao,
    atribuir_tecnico,
    baixar_documento,
    buscar_todos_chamados_usuario,
    criar_followup_privado,
    criar_followup_publico,
    contar_atribuidos_por_status,
    criar_solucao,
    enviar_documento,
    listar_atribuidos_ativos,
    listar_atribuidos_por_status,
    listar_fila,
    listar_followups_publicos,
    listar_timeline_completa,
    obter_chamado_completo,
    obter_solucao,
    obter_tecnico_atribuido,
    obter_usuario_por_id,
    recusar_solucao,
    texto_prazo_confirmacao,
    vincular_documento_ticket,
)
from app.usuarios import PAPEL_TECNICO, resolver_papel, resolver_usuario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("chatbot")

RAIZ_APP = Path(__file__).resolve().parent

# Sessoes de browser ativas em memoria: sessao_id -> {
#   usuario, papel, expira_em, client_ajuda, ajuda_mensagens, lock,
#   eventos_pendentes: {ticket_id: [eventos]},
# }
# `client_ajuda` e a UNICA conversa com a IA da sessao (a de "Preciso de
# ajuda" - pode gerar varios chamados em sequencia). Cada chamado, uma vez
# criado, vira thread independente (relay puro, sem IA) rastreado via
# db.chamados - nao mais um "chamado_ativo" unico por sessao.
# Sessao nao sobrevive a reinicio do processo (ja era assim no MVP) -
# `expira_em` (time.monotonic()) so cobre o caso normal de expirar apos 8h
# de uso continuo, checado a cada request via _sessao_ativa().
SESSOES_ATIVAS: dict[str, dict] = {}

NOME_COOKIE_SESSAO = "sessao_id"
DURACAO_SESSAO_SEGUNDOS = 8 * 60 * 60

# Rate limit de login: {login em minusculo: [timestamps das tentativas
# falhas]}. Em memoria de proposito - nao precisa sobreviver a reinicio,
# e evita mexer no schema do banco por causa de um contador efemero.
TENTATIVAS_LOGIN: dict[str, list[float]] = {}
JANELA_RATE_LIMIT_SEGUNDOS = 5 * 60
MAX_TENTATIVAS_LOGIN = 5

ROTULOS_FERRAMENTAS = {
    "mcp__glpi__glpi_search_user": "verificando seu cadastro...",
    "mcp__glpi__glpi_list_categories": "consultando categorias...",
    "mcp__glpi__glpi_create_ticket": "abrindo o chamado...",
    "mcp__consulta__consultar_meus_chamados": "consultando seus chamados...",
    "mcp__consulta__consultar_detalhes_chamado": "consultando o chamado...",
    "mcp__consulta__registrar_atualizacao": "registrando no chamado...",
}

_CATEGORIAS_POR_ID = {c["id"]: c for c in carregar_catalogo()["categorias"]}


def _nome_categoria_exibicao(category_id: int | None) -> str | None:
    """Versao curta do caminho da categoria pro card de 'chamado criado'
    (sem a area raiz, que costuma ser generica demais)."""
    if not category_id:
        return None
    cat = _CATEGORIAS_POR_ID.get(category_id)
    if not cat:
        return None
    partes = cat["caminho_completo"].split(" > ")
    return " · ".join(partes[1:]) if len(partes) > 1 else partes[0]


def _formatar_hora(data_glpi: str | None) -> str | None:
    """'YYYY-MM-DD HH:MM:SS' (GLPI) -> 'HH:MM' se for hoje, 'DD/MM HH:MM'
    senao - usado pra reconstruir o horario real das mensagens antigas de
    um chamado (diferente das mensagens ao vivo, que pegam a hora local do
    navegador na hora de exibir)."""
    if not data_glpi:
        return None
    momento = datetime.strptime(data_glpi, "%Y-%m-%d %H:%M:%S")
    if momento.date() == datetime.now().date():
        return momento.strftime("%H:%M")
    return momento.strftime("%d/%m %H:%M")


def _status_humano(status: int, atribuido: bool, *, para_tecnico: bool = False) -> tuple[str, str]:
    """`atribuido` precisa vir de obter_tecnico_atribuido (Ticket_User de
    verdade) - NAO de modo_acompanhamento (que so reflete se ja teve
    followup de tecnico). Atribuir alguem no GLPI nao cria followup
    nenhum sozinho, entao usar modo_acompanhamento aqui deixava o rotulo
    preso em "Aguardando atendimento" mesmo depois de atribuido.

    `para_tecnico`: o rotulo de RESOLVIDO muda de audiencia - pro
    colaborador e uma instrucao ("confirme se funcionou"), pro tecnico e
    so informativo (ele nao e quem confirma).

    Retorna (texto, classe) - a classe e uma chave estavel em ingles pro
    CSS/JS mapear cor semantica sem precisar comparar a string em
    portugues (fragil se o texto mudar de redacao um dia)."""
    if status == STATUS_FECHADO:
        return "Fechado", "fechado"
    if status == STATUS_RESOLVIDO:
        if para_tecnico:
            return "Aguardando confirmação do usuário", "resolvido"
        return "Resolvido — confirme se funcionou", "resolvido"
    if atribuido:
        return "Em atendimento", "atendimento"
    return "Aguardando atendimento", "aguardando"


TIPOS_ANEXO_PERMITIDOS = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
TAMANHO_MAX_IMAGEM_BYTES = 5 * 1024 * 1024
TAMANHO_MAX_PDF_BYTES = 15 * 1024 * 1024


class AnexoInvalido(Exception):
    def __init__(self, motivo: str):
        self.motivo = motivo


async def _ler_e_validar_anexo(arquivo: UploadFile) -> bytes:
    if arquivo.content_type not in TIPOS_ANEXO_PERMITIDOS:
        raise AnexoInvalido("Formato não suportado. Envie uma imagem (PNG/JPG/WEBP/GIF) ou PDF.")
    conteudo = await arquivo.read()
    limite = TAMANHO_MAX_PDF_BYTES if arquivo.content_type == "application/pdf" else TAMANHO_MAX_IMAGEM_BYTES
    if len(conteudo) > limite:
        raise AnexoInvalido(f"Arquivo muito grande (máximo {limite // (1024 * 1024)}MB).")
    return conteudo


async def _processar_anexo(arquivo: UploadFile, ticket_id: int | None) -> dict:
    """Valida, faz upload pro GLPI e (se ja existir chamado) vincula na hora.
    NAO grava na tabela local - quem chama decide o followup_id (ou None)."""
    conteudo = await _ler_e_validar_anexo(arquivo)
    documento_id = await enviar_documento(arquivo.filename, conteudo, arquivo.content_type)
    if ticket_id is not None:
        await vincular_documento_ticket(ticket_id, documento_id)
    return {"documento_id": documento_id, "nome_arquivo": arquivo.filename, "mime_tipo": arquivo.content_type}


def _texto_com_aviso_anexo(mensagem: str, nome_arquivo: str | None) -> str:
    """Nota textual pra IA reconhecer o anexo SEM analisar o conteudo
    (decisao: sem visao computacional) - so concatena string, nunca muda o
    formato de client.query()."""
    if not nome_arquivo:
        return mensagem
    aviso = f"[Usuário anexou um arquivo: {nome_arquivo}]"
    return f"{mensagem}\n\n{aviso}" if mensagem else aviso


def _agrupar_anexos_por_followup(ticket_id: int, prefixo_url: str) -> dict[int | None, list[dict]]:
    grupos: dict[int | None, list[dict]] = {}
    for linha in db.listar_anexos_chamado(ticket_id):
        grupos.setdefault(linha["followup_id"], []).append(
            {
                "documento_id": linha["glpi_document_id"],
                "nome_arquivo": linha["nome_arquivo"],
                "mime_tipo": linha["mime_tipo"],
                "url": f"{prefixo_url}/{linha['glpi_document_id']}",
            }
        )
    return grupos


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.inicializar_banco()
    tarefa_polling = asyncio.create_task(relay.loop_polling(SESSOES_ATIVAS))
    tarefa_fechamento = asyncio.create_task(relay.loop_fechamento_automatico())
    yield
    tarefa_polling.cancel()
    tarefa_fechamento.cancel()
    for sessao in SESSOES_ATIVAS.values():
        client = sessao.get("client_ajuda")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=RAIZ_APP / "static"), name="static")
templates = Jinja2Templates(directory=RAIZ_APP / "templates")


def _sessao_ativa(request: Request) -> dict | None:
    sessao_id = request.cookies.get(NOME_COOKIE_SESSAO)
    if not sessao_id:
        return None
    sessao = SESSOES_ATIVAS.get(sessao_id)
    if not sessao:
        return None
    if time.monotonic() > sessao["expira_em"]:
        logger.info(
            "sessao expirada (8h) sessao=%s usuario=%s", sessao_id, sessao["usuario"]["login"]
        )
        SESSOES_ATIVAS.pop(sessao_id, None)
        return None
    return sessao


def _rate_limit_excedido(login: str) -> bool:
    agora = time.monotonic()
    tentativas = TENTATIVAS_LOGIN.setdefault(login.lower(), [])
    tentativas[:] = [t for t in tentativas if agora - t < JANELA_RATE_LIMIT_SEGUNDOS]
    return len(tentativas) >= MAX_TENTATIVAS_LOGIN


def _registrar_tentativa_falha(login: str) -> None:
    TENTATIVAS_LOGIN.setdefault(login.lower(), []).append(time.monotonic())


def _limpar_tentativas_login(login: str) -> None:
    TENTATIVAS_LOGIN.pop(login.lower(), None)


@app.get("/", response_class=HTMLResponse)
async def pagina_inicial(request: Request):
    if _sessao_ativa(request):
        return RedirectResponse("/chamados", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, login: str = Form(...), senha: str = Form(...)):
    login = login.strip()
    if not login or not senha:
        return templates.TemplateResponse(
            request,
            "_form_login.html",
            {"erro": "Preenche usuário e senha pra continuar.", "login_tentado": login},
        )

    if _rate_limit_excedido(login):
        logger.warning("rate limit de login excedido usuario=%s", login)
        return templates.TemplateResponse(
            request,
            "_form_login.html",
            {
                "erro": "Muitas tentativas seguidas. Espera uns minutos e tenta de novo.",
                "login_tentado": login,
            },
        )

    try:
        autenticado = await autenticar(login, senha)
    except LDAPIndisponivel:
        logger.error("LDAP indisponivel durante tentativa de login usuario=%s", login)
        return templates.TemplateResponse(
            request,
            "_form_login.html",
            {
                "erro": "Sistema de login está indisponível no momento. Tenta de novo em instantes.",
                "login_tentado": login,
            },
        )

    if not autenticado:
        _registrar_tentativa_falha(login)
        logger.info("login falhou (credencial invalida) usuario=%s", login)
        return templates.TemplateResponse(
            request,
            "_form_login.html",
            {"erro": "Usuário ou senha incorretos.", "login_tentado": login},
        )

    usuario = await resolver_usuario(login)
    if usuario is None:
        logger.warning("autenticado no AD mas sem cadastro ativo no GLPI usuario=%s", login)
        return templates.TemplateResponse(
            request,
            "_form_login.html",
            {
                "erro": (
                    "Login confirmado, mas não encontrei seu cadastro no GLPI. "
                    "Fala com o suporte de TI."
                ),
                "login_tentado": login,
            },
        )

    _limpar_tentativas_login(login)
    papel = await resolver_papel(usuario["id"])

    sessao_id = uuid.uuid4().hex
    db.criar_sessao(sessao_id, usuario, papel)
    SESSOES_ATIVAS[sessao_id] = {
        "usuario": usuario,
        "papel": papel,
        "expira_em": time.monotonic() + DURACAO_SESSAO_SEGUNDOS,
        "client_ajuda": None,
        "ajuda_mensagens": [],
        "lock": asyncio.Lock(),
        "eventos_pendentes": {},
    }
    logger.info("login ok sessao=%s usuario=%s papel=%s", sessao_id, login, papel)

    resposta = HTMLResponse(status_code=200, headers={"HX-Redirect": "/chamados"})
    resposta.set_cookie(
        NOME_COOKIE_SESSAO, sessao_id, httponly=True, samesite="lax",
        max_age=DURACAO_SESSAO_SEGUNDOS,
    )
    return resposta


@app.post("/logout")
async def logout(request: Request):
    sessao_id = request.cookies.get(NOME_COOKIE_SESSAO)
    sessao = SESSOES_ATIVAS.pop(sessao_id, None) if sessao_id else None
    if sessao:
        client = sessao.get("client_ajuda")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        logger.info("logout sessao=%s usuario=%s", sessao_id, sessao["usuario"]["login"])

    resposta = RedirectResponse("/", status_code=303)
    resposta.delete_cookie(NOME_COOKIE_SESSAO)
    return resposta


async def _computar_ativos(usuario_id: int) -> list[dict]:
    """Lista de chamados ativos + status humano + badge, sempre ao vivo
    contra o GLPI (status e atribuicao mudam por fora do app o tempo
    todo - nunca confiar so no watermark local pra isso). Usado tanto no
    carregamento normal do hub quanto no SSE (/chamados/stream), pra manter
    o rotulo "Aguardando atendimento" -> "Em atendimento" atualizado sem
    precisar de F5."""
    todos = await buscar_todos_chamados_usuario(usuario_id)
    ativos_brutos = [c for c in todos if c["status"] != STATUS_FECHADO]

    async def _montar_item(chamado: dict) -> dict | None:
        await relay.garantir_estado_chamado(usuario_id, chamado["id"])
        estado_db = db.obter_estado_chamado(chamado["id"])

        # verificacao oportunista "na carga da sessao" (Fase 4, alem da
        # varredura horaria): se abrir o hub ja acha o chamado vencido,
        # fecha na hora em vez de esperar o proximo ciclo da hora. So faz
        # sentido quando o status AO VIVO (da busca de agora ha pouco) e
        # resolvido - antes isso chamava obter_chamado_completo pra TODO
        # chamado do bot em TODO tick do SSE, so pra descobrir que nao
        # tinha nada resolvido pra fechar.
        if (
            chamado["status"] == STATUS_RESOLVIDO
            and estado_db
            and await relay.fechar_se_vencido(chamado["id"], estado_db)
        ):
            return None

        tecnico_nome = None
        if chamado["status"] not in STATUS_RESOLVIDO_OU_FECHADO:
            tecnico_nome = await obter_tecnico_atribuido(chamado["id"])
        nao_lida = bool(
            estado_db
            and estado_db["visto_em"]
            and estado_db["atualizado_em"] > estado_db["visto_em"]
        )
        status_texto, status_classe = _status_humano(chamado["status"], bool(tecnico_nome))
        if tecnico_nome and status_classe == "atendimento":
            # o colaborador precisa saber QUEM esta cuidando do chamado,
            # nao so que "alguem" esta - _status_humano fica generico de
            # proposito (reusado pelo painel do tecnico, onde o nome nao
            # faz sentido), entao o nome entra aqui.
            status_texto = f"Em atendimento — {tecnico_nome}"
        return {
            "id": chamado["id"],
            "titulo": chamado["titulo"],
            "status_texto": status_texto,
            "status_classe": status_classe,
            "nao_lida": nao_lida,
        }

    # um item por vez era o que deixava a troca de pagina lenta: cada
    # chamado custa 1-2 idas ao GLPI (~0.2s cada) e elas nao dependem uma
    # da outra - gather preserva a ordem da lista.
    itens = await asyncio.gather(*[_montar_item(c) for c in ativos_brutos])
    return [item for item in itens if item is not None]


@app.get("/chamados", response_class=HTMLResponse)
async def hub_chamados(request: Request):
    sessao = _sessao_ativa(request)
    if not sessao:
        return RedirectResponse("/", status_code=303)

    usuario = sessao["usuario"]
    ativos = await _computar_ativos(usuario["id"])
    return templates.TemplateResponse(
        request,
        "chamados.html",
        {"usuario": usuario, "ativos": ativos, "papel": sessao["papel"]},
    )


def _resumo_badges(usuario_id: int) -> list[dict]:
    linhas = db.listar_chamados_usuario(usuario_id)
    return [
        {
            "id": linha["ticket_id"],
            "nao_lida": bool(linha["visto_em"] and linha["atualizado_em"] > linha["visto_em"]),
        }
        for linha in linhas
    ]


@app.get("/chamados/resumo")
async def resumo_chamados(request: Request):
    """Poll leve pro hub atualizar badges sem recarregar a pagina - so olha
    o watermark local (sem bater no GLPI a cada 10s por aba aberta).
    Fallback do /chamados/stream (SSE) - ver CLAUDE-v2 Fase 5: se o SSE
    cair, o front-end volta pra este polling classico."""
    sessao = _sessao_ativa(request)
    if not sessao:
        return JSONResponse([])
    return JSONResponse(_resumo_badges(sessao["usuario"]["id"]))


# Cabecalhos que toda resposta SSE precisa: sem cache, sem buffering no
# proxy (Apache precisa de ProxyPass ... flushpackets=on na Fase 7 pra isso
# valer de ponta a ponta - X-Accel-Buffering e o equivalente pro nginx, sem
# efeito no Apache, mas inofensivo deixar).
CABECALHOS_SSE = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
INTERVALO_SSE_SEGUNDOS = 2
# O SSE do hub bate no GLPI de verdade a cada ciclo (status + atribuicao de
# cada chamado ativo) - intervalo maior que o de thread pra nao pesar.
INTERVALO_SSE_HUB_SEGUNDOS = 5


@app.get("/chamados/stream")
async def hub_stream_sse(request: Request):
    """SSE do hub (Fase 5): empurra a lista de chamados ativos (titulo,
    status humano, badge) toda vez que ela muda - inclui o rotulo
    "Aguardando atendimento" -> "Em atendimento", que so muda quando
    alguem e atribuido de verdade no GLPI (nao da pra saber isso so pelo
    watermark local). Por bater no GLPI de verdade a cada ciclo (diferente
    do polling de badge antigo, que so lia SQLite), usa um intervalo maior
    que o do SSE de thread."""
    sessao = _sessao_ativa(request)
    if not sessao:
        return JSONResponse([], status_code=403)
    usuario_id = sessao["usuario"]["id"]

    async def stream():
        ultimo_enviado = None
        while True:
            if await request.is_disconnected():
                break
            try:
                atual = await _computar_ativos(usuario_id)
            except Exception:
                logger.exception("erro no SSE do hub usuario=%s", usuario_id)
                atual = ultimo_enviado
            if atual != ultimo_enviado:
                yield f"data: {json.dumps(atual, ensure_ascii=False)}\n\n"
                ultimo_enviado = atual
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(INTERVALO_SSE_HUB_SEGUNDOS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=CABECALHOS_SSE)


@app.get("/chamados/finalizados", response_class=HTMLResponse)
async def chamados_finalizados(request: Request):
    sessao = _sessao_ativa(request)
    if not sessao:
        return RedirectResponse("/", status_code=303)

    todos = await buscar_todos_chamados_usuario(sessao["usuario"]["id"])
    finalizados = [
        {"id": c["id"], "titulo": c["titulo"]} for c in todos if c["status"] == STATUS_FECHADO
    ]
    return templates.TemplateResponse(
        request, "chamados_finalizados.html", {"usuario": sessao["usuario"], "finalizados": finalizados}
    )


@app.post("/chamados/novo", response_class=HTMLResponse)
async def nova_conversa_ajuda(request: Request):
    sessao = _sessao_ativa(request)
    if not sessao:
        return RedirectResponse("/", status_code=303)

    if not sessao.get("client_ajuda"):
        opcoes = construir_opcoes(modo_teste=True, usuario=sessao["usuario"])
        client = ClaudeSDKClient(opcoes)
        await client.connect()
        await aguardar_mcp_pronto(client)
        sessao["client_ajuda"] = client
        sessao["ajuda_mensagens"] = []
        logger.info("conversa de ajuda iniciada sessao_usuario=%s", sessao["usuario"]["login"])

    return HTMLResponse(status_code=200, headers={"HX-Redirect": "/chamados/ajuda"})


@app.get("/chamados/ajuda", response_class=HTMLResponse)
async def pagina_ajuda(request: Request):
    sessao = _sessao_ativa(request)
    if not sessao:
        return RedirectResponse("/", status_code=303)
    if not sessao.get("client_ajuda"):
        # conversa perdida (ex: processo reiniciou) - volta pro hub pra
        # comecar de novo com "Preciso de ajuda".
        return RedirectResponse("/chamados", status_code=303)

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "usuario": sessao["usuario"],
            "modo": "ajuda",
            "chamado": None,
            "chamado_json": json.dumps(None),
            "somente_leitura": False,
            "mensagens_iniciais_json": json.dumps(
                sessao.get("ajuda_mensagens", []), ensure_ascii=False
            ),
        },
    )


@app.post("/chamados/ajuda/mensagem")
async def enviar_mensagem_ajuda(
    request: Request, mensagem: str = Form(""), arquivo: UploadFile | None = File(None)
):
    sessao_id = request.cookies.get(NOME_COOKIE_SESSAO)
    sessao = SESSOES_ATIVAS.get(sessao_id) if sessao_id else None
    client: ClaudeSDKClient | None = sessao.get("client_ajuda") if sessao else None

    if not sessao or not client:
        async def sem_sessao():
            yield _evento("erro", valor="Sessão expirada. Atualiza a página.")
        return StreamingResponse(sem_sessao(), media_type="application/x-ndjson")

    usuario_id = sessao["usuario"]["id"]

    async def gerar():
        ferramentas_pendentes: dict[str, str] = {}
        entradas_pendentes: dict[str, dict] = {}
        n_eventos = 0

        mensagem_final = mensagem.strip()
        if not mensagem_final and not arquivo:
            yield _evento("erro", valor="Escreve algo ou anexa um arquivo antes de enviar.")
            yield _evento("fim")
            return

        if arquivo:
            # chamado ainda nao existe nesse modo - so vincula quando (e se)
            # a IA criar um chamado nesta mesma conversa (ver bloco
            # glpi_create_ticket abaixo). Se o usuario fechar a aba antes
            # disso, o Document fica orfao no GLPI (aceitavel, sem limpeza
            # automatica - fora de escopo).
            try:
                info_anexo = await _processar_anexo(arquivo, ticket_id=None)
            except AnexoInvalido as erro:
                yield _evento("erro", valor=erro.motivo)
                yield _evento("fim")
                return
            sessao.setdefault("anexos_pendentes_ajuda", []).append(info_anexo)
            mensagem_final = _texto_com_aviso_anexo(mensagem_final, arquivo.filename)

        try:
            async with sessao["lock"]:
                await client.query(mensagem_final)

                async for msg in client.receive_response():
                    n_eventos += 1
                    if isinstance(msg, StreamEvent):
                        evento = msg.event
                        tipo_evento = evento.get("type")
                        if tipo_evento == "content_block_start":
                            bloco = evento.get("content_block", {})
                            if bloco.get("type") == "text":
                                yield _evento("novo_texto")
                        elif tipo_evento == "content_block_delta":
                            delta = evento.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield _evento("texto_delta", valor=delta.get("text", ""))

                    elif isinstance(msg, AssistantMessage):
                        for bloco in msg.content:
                            if isinstance(bloco, ToolUseBlock):
                                ferramentas_pendentes[bloco.id] = bloco.name
                                if bloco.name == "mcp__glpi__glpi_create_ticket":
                                    entradas_pendentes[bloco.id] = bloco.input
                                rotulo = ROTULOS_FERRAMENTAS.get(bloco.name, "trabalhando...")
                                yield _evento("ferramenta", valor=rotulo)

                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for bloco in msg.content:
                            if not isinstance(bloco, ToolResultBlock):
                                continue
                            nome_tool = ferramentas_pendentes.get(bloco.tool_use_id)
                            if nome_tool != "mcp__glpi__glpi_create_ticket" or bloco.is_error:
                                continue
                            try:
                                conteudo = bloco.content
                                texto = conteudo[0]["text"] if isinstance(conteudo, list) else conteudo
                                resultado = json.loads(texto)
                                if resultado.get("success"):
                                    novo_ticket_id = resultado["id"]
                                    db.registrar_chamado(usuario_id, novo_ticket_id)
                                    logger.info(
                                        "chamado registrado usuario=%s ticket_id=%s",
                                        usuario_id, novo_ticket_id,
                                    )
                                    for pendente in sessao.get("anexos_pendentes_ajuda", []):
                                        await vincular_documento_ticket(
                                            novo_ticket_id, pendente["documento_id"]
                                        )
                                        db.registrar_anexo(
                                            novo_ticket_id, usuario_id,
                                            pendente["nome_arquivo"], pendente["mime_tipo"],
                                            pendente["documento_id"], followup_id=None,
                                        )
                                    sessao["anexos_pendentes_ajuda"] = []
                                    entrada = entradas_pendentes.get(bloco.tool_use_id, {})
                                    yield _evento(
                                        "chamado_criado",
                                        id=novo_ticket_id,
                                        titulo=entrada.get("name", ""),
                                        categoria=_nome_categoria_exibicao(
                                            entrada.get("category_id")
                                        ),
                                    )
                            except (KeyError, ValueError, TypeError, IndexError):
                                logger.exception(
                                    "falha ao interpretar resultado de create_ticket usuario=%s",
                                    usuario_id,
                                )

                    elif isinstance(msg, ResultMessage):
                        if msg.is_error:
                            logger.error(
                                "ResultMessage com erro usuario=%s subtype=%s",
                                usuario_id, msg.subtype,
                            )
                            yield _evento("erro", valor=f"Erro: {msg.subtype}")
                        yield _evento("fim")

        except Exception:
            logger.exception(
                "erro inesperado no turno usuario=%s eventos_ate_aqui=%d", usuario_id, n_eventos
            )
            yield _evento("erro", valor="Deu um erro no servidor ao falar com o assistente.")
            yield _evento("fim")

    return StreamingResponse(gerar(), media_type="application/x-ndjson")


async def _autorizar_chamado(usuario_id: int, ticket_id: int) -> bool:
    todos = await buscar_todos_chamados_usuario(usuario_id)
    return any(c["id"] == ticket_id for c in todos)


def _exigir_tecnico(sessao: dict | None) -> bool:
    """Painel do tecnico (Fase 6): qualquer tecnico pode ver/agir em
    qualquer chamado (sem ACL por pessoa/chamado - decisao confirmada com
    o Lucas), entao aqui so importa o papel da sessao, nunca um id vindo
    do cliente."""
    return bool(sessao) and sessao["papel"] == PAPEL_TECNICO


def _anexo_do_chamado_ou_none(ticket_id: int, documento_id: int):
    """Confere que o documento pedido pertence de fato a ESSE chamado antes
    de baixar - impede um usuario autorizado num chamado baixar o
    documento_id de outro so trocando o numero na URL."""
    for linha in db.listar_anexos_chamado(ticket_id):
        if linha["glpi_document_id"] == documento_id:
            return linha
    return None


@app.get("/chamados/{ticket_id}/anexos/{documento_id}")
async def baixar_anexo_colaborador(request: Request, ticket_id: int, documento_id: int):
    sessao = _sessao_ativa(request)
    if not sessao or not await _autorizar_chamado(sessao["usuario"]["id"], ticket_id):
        return JSONResponse({"erro": "Sessão expirada."}, status_code=403)
    anexo = _anexo_do_chamado_ou_none(ticket_id, documento_id)
    if not anexo:
        return JSONResponse({"erro": "Anexo não encontrado."}, status_code=404)
    conteudo = await baixar_documento(documento_id)
    return Response(content=conteudo, media_type=anexo["mime_tipo"])


@app.get("/tecnico/chamados/{ticket_id}/anexos/{documento_id}")
async def baixar_anexo_tecnico(request: Request, ticket_id: int, documento_id: int):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({"erro": "Sessão expirada."}, status_code=403)
    anexo = _anexo_do_chamado_ou_none(ticket_id, documento_id)
    if not anexo:
        return JSONResponse({"erro": "Anexo não encontrado."}, status_code=404)
    conteudo = await baixar_documento(documento_id)
    return Response(content=conteudo, media_type=anexo["mime_tipo"])


@app.get("/chamados/{ticket_id}", response_class=HTMLResponse)
async def pagina_chamado(request: Request, ticket_id: int):
    sessao = _sessao_ativa(request)
    if not sessao:
        return RedirectResponse("/", status_code=303)

    usuario = sessao["usuario"]

    # autorizacao + dados em paralelo (eram 3 idas sequenciais ao GLPI,
    # ~0.2s cada): sao todas leituras, entao buscar os dados junto com o
    # check de autorizacao nao vaza nada - se nao autorizado, descarta e
    # redireciona sem usar. return_exceptions pra um 404 de ticket
    # inexistente nao virar 500 antes do check de autorizacao (que ja
    # barra ticket inexistente, pois ele nunca esta na lista do usuario).
    autorizado, resumo, followups = await asyncio.gather(
        _autorizar_chamado(usuario["id"], ticket_id),
        obter_chamado_completo(ticket_id),
        listar_followups_publicos(ticket_id),
        return_exceptions=True,
    )
    if isinstance(autorizado, BaseException) or not autorizado:
        return RedirectResponse("/chamados", status_code=303)
    if isinstance(resumo, BaseException):
        raise resumo
    if isinstance(followups, BaseException):
        raise followups

    await relay.garantir_estado_chamado(usuario["id"], ticket_id)
    db.marcar_chamado_visto(ticket_id)
    ids_usuario = db.followups_do_usuario(ticket_id)
    anexos_por_followup = _agrupar_anexos_por_followup(ticket_id, f"/chamados/{ticket_id}/anexos")

    mensagens: list[dict] = [
        {"tipo": "sistema", "texto": f"Chamado #{ticket_id} - {resumo['titulo']}"},
    ]
    # rotulo de autor (avatar + nome) so aparece quando o remetente muda -
    # mesma regra do lado cliente (chat.html/ultimoTipoConversacional),
    # espelhada aqui pra reconstrucao do historico nao ficar repetindo
    # cabecalho em toda mensagem consecutiva do mesmo remetente.
    ultimo_tipo_conversacional: str | None = None

    if resumo["descricao"]:
        mensagens.append(
            {
                "tipo": "assistente",
                "texto": resumo["descricao"],
                "mostrarRotulo": True,
                "hora": _formatar_hora(resumo["data_criacao_completa"]),
                "anexos": anexos_por_followup.get(None, []),
            }
        )
        ultimo_tipo_conversacional = "assistente"

    # resolve os nomes dos autores de uma vez (em paralelo) em vez de um
    # por um dentro do loop - obter_usuario_por_id ja tem cache, entao na
    # pratica so a primeira visita de cada autor sai pro GLPI.
    uids_autores = {
        f["users_id"] for f in followups if f["id"] not in ids_usuario
    }
    autores = await asyncio.gather(*[obter_usuario_por_id(uid) for uid in uids_autores])
    nomes_cache: dict[int, str] = {
        uid: (autor["nome_completo"] if autor else "Suporte")
        for uid, autor in zip(uids_autores, autores)
    }
    tem_followup_tecnico = False
    for followup in followups:
        hora = _formatar_hora(followup["data"])
        if followup["id"] in ids_usuario:
            # mensagem do proprio usuario relayada (ou explicacao de "ainda
            # nao resolveu") - o GLPI registra com o mesmo autor de servico
            # de qualquer followup de tecnico, entao sem essa checagem ela
            # reapareceria aqui como se fosse o suporte falando com ele.
            mensagens.append(
                {
                    "tipo": "usuario",
                    "texto": followup["conteudo"],
                    "hora": hora,
                    "mostrarRotulo": ultimo_tipo_conversacional != "usuario",
                    "anexos": anexos_por_followup.get(followup["id"], []),
                }
            )
            ultimo_tipo_conversacional = "usuario"
            continue
        tem_followup_tecnico = True
        mensagens.append(
            {
                "tipo": "suporte",
                "texto": followup["conteudo"],
                "autorNome": nomes_cache.get(followup["users_id"], "Suporte"),
                "mostrarRotulo": ultimo_tipo_conversacional != "suporte",
                "hora": hora,
                "anexos": anexos_por_followup.get(followup["id"], []),
            }
        )
        ultimo_tipo_conversacional = "suporte"

    if resumo["status"] in STATUS_RESOLVIDO_OU_FECHADO:
        solucao = await obter_solucao(ticket_id)
        if solucao:
            aguardando = resumo["status"] == STATUS_RESOLVIDO
            mensagens.append(
                {
                    "tipo": "resolvido",
                    "texto": solucao,
                    "mostrarBotoes": aguardando,
                    "prazoTexto": (
                        texto_prazo_confirmacao(resumo["data_resolucao"]) if aguardando else None
                    ),
                    # campos que os botoes Funcionou/Nao resolveu escrevem via
                    # JS (chat.html) - declarados aqui de proposito, ja com o
                    # valor neutro, pra nunca depender do Alpine criar essas
                    # chaves em cima da hora na primeira interacao.
                    "enviando": False,
                    "erro": None,
                    "mostrarFormRecusa": False,
                    "jaRespondido": None,
                    "motivoTexto": "",
                }
            )

    # Descarta eventos que ficaram parados na fila (ex: usuario nunca abriu
    # a aba pra consumi-los via SSE/polling) - a reconstrucao acima ja
    # reflete o estado atual completo, entao repetir um evento antigo por
    # cima seria redundante ou, pior, mostrar um status que ja nem e mais
    # o atual (ex: "mudou pra Em atendimento" de horas atras, depois que o
    # chamado ja foi resolvido).
    sessao.get("eventos_pendentes", {}).pop(ticket_id, None)

    # Sincroniza o watermark local com o que acabou de ser mostrado - sem
    # isso, o proximo ciclo do relay.loop_polling compara contra um
    # watermark desatualizado, "redescobre" os mesmos followups/resolucao
    # e duplica o card/evento ao vivo pra quem esta com a pagina aberta.
    # houve_novidade=False de proposito: o usuario acabou de ver isso agora
    # mesmo, nao e novidade pra ele.
    estado_db = db.obter_estado_chamado(ticket_id)
    if estado_db:
        ultimo_followup_visto = estado_db["ultimo_followup_id"]
        if followups:
            ultimo_followup_visto = max(ultimo_followup_visto, followups[-1]["id"])
        db.atualizar_estado_chamado(
            ticket_id,
            ultimo_followup_id=ultimo_followup_visto,
            ultimo_status_glpi=resumo["status"],
            modo_acompanhamento=bool(estado_db["modo_acompanhamento"]) or tem_followup_tecnico,
        )

    tecnico_nome = None
    if resumo["status"] not in STATUS_RESOLVIDO_OU_FECHADO:
        tecnico_nome = await obter_tecnico_atribuido(ticket_id)

    chamado_info = {"id": ticket_id, "status": resumo["status"], "tecnico": tecnico_nome}
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "usuario": usuario,
            "modo": "chamado",
            "chamado": chamado_info,
            "chamado_json": json.dumps(chamado_info, ensure_ascii=False),
            "somente_leitura": resumo["status"] == STATUS_FECHADO,
            "mensagens_iniciais_json": json.dumps(mensagens, ensure_ascii=False),
        },
    )


@app.get("/chamados/{ticket_id}/novidades")
async def novidades_chamado(request: Request, ticket_id: int):
    """Fallback do /chamados/{ticket_id}/eventos (SSE) - ver CLAUDE-v2 Fase
    5: se o SSE cair, o front-end volta pra este polling classico."""
    sessao = _sessao_ativa(request)
    if not sessao:
        return JSONResponse([])
    eventos = sessao.get("eventos_pendentes", {}).pop(ticket_id, [])
    return JSONResponse(eventos)


@app.get("/chamados/{ticket_id}/eventos")
async def eventos_chamado_sse(request: Request, ticket_id: int):
    """SSE da thread de 1 chamado (Fase 5): empurra followups/mudancas de
    status assim que o relay.loop_polling os detecta, em vez do front-end
    perguntar a cada 5s. So olha o dict que o loop ja preenche - o poll de
    verdade contra o GLPI continua no mesmo lugar de sempre."""
    sessao = _sessao_ativa(request)
    if not sessao or not await _autorizar_chamado(sessao["usuario"]["id"], ticket_id):
        return JSONResponse([], status_code=403)

    async def stream():
        while True:
            if await request.is_disconnected():
                break
            eventos = sessao.get("eventos_pendentes", {}).pop(ticket_id, [])
            parou = False
            for evento in eventos:
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
                if evento.get("tipo") == "resolvido" and not evento.get("mostrarBotoes"):
                    parou = True  # chamado fechado - nao ha mais nada pra acompanhar
            if parou:
                break
            if not eventos:
                yield ": keep-alive\n\n"
            await asyncio.sleep(INTERVALO_SSE_SEGUNDOS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=CABECALHOS_SSE)


def _evento(tipo: str, **campos) -> str:
    return json.dumps({"tipo": tipo, **campos}, ensure_ascii=False) + "\n"


@app.post("/chamados/{ticket_id}/mensagem")
async def enviar_mensagem_chamado(
    request: Request,
    ticket_id: int,
    mensagem: str = Form(""),
    arquivo: UploadFile | None = File(None),
):
    """Thread de um chamado ja existente. Regra: a IA so silencia depois que
    um TECNICO ASSUME o chamado de verdade (obter_tecnico_atribuido).

    Antes da atribuicao: a mensagem NAO vira followup automatico - a IA
    conversa e decide sozinha (via tool registrar_atualizacao) se aquilo e
    informacao nova relevante o suficiente pra anotar no chamado, ou so uma
    mensagem social ("obrigado", "ok") que nao precisa poluir o historico
    que o tecnico vai ler.

    Depois da atribuicao: relay puro, sem julgamento nenhum - toda mensagem
    vira followup publico direto, a IA nem entra em cena."""
    sessao = _sessao_ativa(request)
    if not sessao or not await _autorizar_chamado(sessao["usuario"]["id"], ticket_id):
        async def sem_sessao():
            yield _evento("erro", valor="Sessão expirada. Atualiza a página.")
        return StreamingResponse(sem_sessao(), media_type="application/x-ndjson")

    usuario = sessao["usuario"]

    async def gerar():
        mensagem_final = mensagem.strip()
        if not mensagem_final and not arquivo:
            yield _evento("erro", valor="Escreve algo ou anexa um arquivo antes de enviar.")
            yield _evento("fim")
            return

        info_anexo = None
        if arquivo:
            # ticket ja existe - vincula na hora, sem precisar de estado pendente
            try:
                info_anexo = await _processar_anexo(arquivo, ticket_id=ticket_id)
            except AnexoInvalido as erro:
                yield _evento("erro", valor=erro.motivo)
                yield _evento("fim")
                return
            yield _evento("anexo", **info_anexo)
            mensagem_final = _texto_com_aviso_anexo(mensagem_final, arquivo.filename)

        try:
            tecnico = await obter_tecnico_atribuido(ticket_id)
        except Exception:
            logger.exception("erro ao checar atribuicao chamado=%s", ticket_id)
            yield _evento("erro", valor="Não consegui verificar o chamado agora. Tenta de novo.")
            yield _evento("fim")
            return

        if tecnico:
            try:
                novo_id = await criar_followup_publico(ticket_id, mensagem_final)
                db.marcar_followup_do_usuario(ticket_id, novo_id)
                if info_anexo:
                    db.registrar_anexo(
                        ticket_id, usuario["id"], info_anexo["nome_arquivo"],
                        info_anexo["mime_tipo"], info_anexo["documento_id"], followup_id=novo_id,
                    )
                estado_db = db.obter_estado_chamado(ticket_id)
                if estado_db:
                    db.atualizar_estado_chamado(
                        ticket_id, ultimo_followup_id=max(estado_db["ultimo_followup_id"], novo_id)
                    )
                yield _evento("relay_ok")
            except Exception:
                logger.exception("erro ao relayar mensagem chamado=%s", ticket_id)
                yield _evento(
                    "erro", valor="Não consegui registrar sua mensagem no chamado. Tenta de novo."
                )
            yield _evento("fim")
            return

        if info_anexo:
            db.registrar_anexo(
                ticket_id, usuario["id"], info_anexo["nome_arquivo"],
                info_anexo["mime_tipo"], info_anexo["documento_id"], followup_id=None,
            )

        # ninguem assumiu ainda - a IA responde e decide se vale registrar
        # (conversa leve, sem criar chamado novo). Client novo e curto: sem
        # servidor stdio externo (so as tools em processo), conecta rapido.
        client: ClaudeSDKClient | None = None
        try:
            opcoes = construir_opcoes_acompanhamento(ticket_id, usuario, mensagem_final)
            client = ClaudeSDKClient(opcoes)
            await client.connect()
            await aguardar_mcp_pronto(client)
            await client.query(f'O colaborador mandou no chamado #{ticket_id}: "{mensagem_final}"')
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    evento = msg.event
                    tipo_evento = evento.get("type")
                    if tipo_evento == "content_block_start":
                        bloco = evento.get("content_block", {})
                        if bloco.get("type") == "text":
                            yield _evento("novo_texto")
                    elif tipo_evento == "content_block_delta":
                        delta = evento.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield _evento("texto_delta", valor=delta.get("text", ""))
                elif isinstance(msg, AssistantMessage):
                    for bloco in msg.content:
                        if isinstance(bloco, ToolUseBlock):
                            rotulo = ROTULOS_FERRAMENTAS.get(bloco.name, "trabalhando...")
                            yield _evento("ferramenta", valor=rotulo)
                elif isinstance(msg, ResultMessage) and msg.is_error:
                    logger.error(
                        "ResultMessage com erro (acompanhamento) chamado=%s subtype=%s",
                        ticket_id, msg.subtype,
                    )
                    yield _evento("erro", valor=f"Erro: {msg.subtype}")
        except Exception:
            logger.exception("erro no modo acompanhamento chamado=%s", ticket_id)
            yield _evento("erro", valor="Sua mensagem foi registrada, mas não consegui responder agora.")
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        yield _evento("fim")

    return StreamingResponse(gerar(), media_type="application/x-ndjson")


@app.post("/chamados/{ticket_id}/aceitar")
async def aceitar_solucao_chamado(request: Request, ticket_id: int):
    """'Funcionou': aprova a solucao e fecha o chamado no GLPI."""
    sessao = _sessao_ativa(request)
    if not sessao or not await _autorizar_chamado(sessao["usuario"]["id"], ticket_id):
        return JSONResponse({"ok": False, "erro": "Sessão expirada. Atualiza a página."}, status_code=403)

    resumo = await obter_chamado_completo(ticket_id)
    if resumo["status"] != STATUS_RESOLVIDO:
        return JSONResponse(
            {"ok": False, "erro": "Este chamado não está mais aguardando confirmação."},
            status_code=409,
        )

    try:
        await aprovar_solucao(ticket_id)
    except Exception:
        logger.exception("erro ao aprovar solucao chamado=%s", ticket_id)
        return JSONResponse(
            {"ok": False, "erro": "Não consegui confirmar agora. Tenta de novo."}, status_code=502
        )

    db.atualizar_estado_chamado(
        ticket_id, ultimo_status_glpi=STATUS_FECHADO, modo_acompanhamento=False
    )
    logger.info("chamado=%s aprovado (funcionou) usuario=%s", ticket_id, sessao["usuario"]["login"])
    return JSONResponse({"ok": True})


@app.post("/chamados/{ticket_id}/recusar")
async def recusar_solucao_chamado(request: Request, ticket_id: int, motivo: str = Form(...)):
    """'Ainda nao resolveu': reabre o chamado com o motivo do usuario."""
    sessao = _sessao_ativa(request)
    if not sessao or not await _autorizar_chamado(sessao["usuario"]["id"], ticket_id):
        return JSONResponse({"ok": False, "erro": "Sessão expirada. Atualiza a página."}, status_code=403)

    motivo = motivo.strip()
    if len(motivo) < 10:
        return JSONResponse(
            {"ok": False, "erro": "Explica com mais detalhes (mínimo 10 caracteres)."},
            status_code=422,
        )

    resumo = await obter_chamado_completo(ticket_id)
    if resumo["status"] != STATUS_RESOLVIDO:
        return JSONResponse(
            {"ok": False, "erro": "Este chamado não está mais aguardando confirmação."},
            status_code=409,
        )

    try:
        novo_followup_id = await recusar_solucao(ticket_id, motivo)
    except Exception:
        logger.exception("erro ao recusar solucao chamado=%s", ticket_id)
        return JSONResponse(
            {"ok": False, "erro": "Não consegui registrar agora. Tenta de novo."}, status_code=502
        )

    db.marcar_followup_do_usuario(ticket_id, novo_followup_id)
    estado_db = db.obter_estado_chamado(ticket_id)
    db.atualizar_estado_chamado(
        ticket_id,
        ultimo_followup_id=max(novo_followup_id, estado_db["ultimo_followup_id"] if estado_db else 0),
        ultimo_status_glpi=STATUS_REABERTURA,
        modo_acompanhamento=True,
    )
    logger.info("chamado=%s recusado (nao resolveu) usuario=%s", ticket_id, sessao["usuario"]["login"])
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Painel do tecnico (Fase 6) - CLAUDE-v2.md "Jornada do TECNICO". Qualquer
# tecnico pode ver/agir em qualquer chamado (sem ACL por pessoa/chamado,
# decisao confirmada com o Lucas) - so o papel da sessao importa,
# verificado em _exigir_tecnico. "Assumir" so organiza Fila/Meus
# atendimentos, nao e uma trava de permissao.
# ---------------------------------------------------------------------------

INTERVALO_SSE_TECNICO_SEGUNDOS = 6


STATUS_PENDENTE = 4  # GLPI: pendente (ver comentario de STATUS_ABERTOS em glpi.py)


def _grupo_status_tecnico(status: int) -> str:
    """Agrupamento pros botoes de filtro de 'Meus atendimentos' (painel do
    tecnico) - diferente de _status_humano (que so tem 'atendimento' pra
    tudo que nao e resolvido/fechado): aqui pendente vira grupo proprio,
    senao a lista de atendimentos fica gigante e misturada."""
    if status == STATUS_FECHADO:
        return "fechado"
    if status == STATUS_RESOLVIDO:
        return "solucionado"
    if status == STATUS_PENDENTE:
        return "pendente"
    return "atendimento"


async def _obter_preview_mensagem_usuario(ticket_id: int) -> tuple[str | None, str | None]:
    """(texto, data) da ULTIMA mensagem do colaborador nesse chamado - pro
    preview + notificacao dos cards de 'Em atendimento' do painel do
    tecnico. (None, None) se o colaborador ainda nao mandou nada."""
    followups = await listar_followups_publicos(ticket_id)
    ids_usuario = db.followups_do_usuario(ticket_id)
    mensagens_usuario = [f for f in followups if f["id"] in ids_usuario]
    if not mensagens_usuario:
        return None, None
    ultima = mensagens_usuario[-1]
    texto = ultima["conteudo"].replace("\n", " ").strip()
    if len(texto) > 120:
        texto = texto[:120].rstrip() + "…"
    return texto, ultima["data"]


def _mensagem_e_nova(data_mensagem: str | None, visto_em: str | None) -> bool:
    """`data_mensagem` vem do GLPI ('YYYY-MM-DD HH:MM:SS'), `visto_em` vem
    do nosso banco (ISO, de _agora()) - formatos diferentes, por isso
    parseia os dois em vez de comparar string com string."""
    if not data_mensagem:
        return False
    if not visto_em:
        return True
    momento_msg = datetime.strptime(data_mensagem, "%Y-%m-%d %H:%M:%S")
    momento_visto = datetime.fromisoformat(visto_em)
    return momento_msg > momento_visto


def _decorar_chamados_tecnico(chamados: list[dict], atribuido: bool) -> list[dict]:
    """Decoracao comum dos cards do painel do tecnico (status humano,
    grupo de filtro, icone 'via assistente')."""
    bot_ids = db.chamados_criados_pelo_bot([c["id"] for c in chamados])
    decorados = []
    for c in chamados:
        grupo_status = _grupo_status_tecnico(c["status"])
        status_texto, status_classe = _status_humano(c["status"], atribuido, para_tecnico=True)
        if atribuido and grupo_status == "pendente":
            # _status_humano nao distingue pendente de "em atendimento"
            # (o colaborador ve os dois como a mesma coisa, de
            # proposito) - mas o painel do tecnico precisa, senao o
            # filtro "Pendente" mostra cards escritos "Em atendimento".
            status_texto, status_classe = "Pendente", "pendente"
        decorados.append(
            {
                "id": c["id"],
                "titulo": c["titulo"],
                "categoria": c["categoria"],
                "requerente_nome": c["requerente_nome"],
                "data_abertura": c["data_abertura"],
                "status_texto": status_texto,
                "status_classe": status_classe,
                "grupo_status": grupo_status,
                "via_bot": c["id"] in bot_ids,
                # preenchidos so pros cards de "Em atendimento" - default
                # neutro nos outros, pra nunca depender do Alpine criar
                # essas chaves na hora.
                "preview_texto": None,
                "nova_mensagem": False,
            }
        )
    return decorados


async def _computar_painel_tecnico(tecnico_id: int) -> dict:
    """Fila (chamados sem tecnico, status aberto) + Meus atendimentos
    ATIVOS (em atendimento/pendente) + contagens de solucionados/fechados.
    Solucionados/fechados NAO vem hidratados aqui de proposito - sao a
    maior parte do historico e o GLPI cobra ~7ms/linha (169 linhas = 1.3s
    por tick de SSE, medido ao vivo); eles carregam sob demanda em
    /tecnico/atendimentos/{grupo} quando o filtro e clicado, e os botoes
    mostram a contagem barata (range 0-0)."""
    fila, atribuidos, total_solucionados, total_fechados = await asyncio.gather(
        listar_fila(),
        listar_atribuidos_ativos(tecnico_id),
        contar_atribuidos_por_status(tecnico_id, STATUS_RESOLVIDO),
        contar_atribuidos_por_status(tecnico_id, STATUS_FECHADO),
    )

    fila_decorada = _decorar_chamados_tecnico(fila, atribuido=False)
    atribuidos_decorados = _decorar_chamados_tecnico(atribuidos, atribuido=True)

    # Preview da ultima mensagem do colaborador + bolinha de "mensagem
    # nova" - so pros chamados "Em atendimento" (grupo pequeno e ativo).
    em_atendimento = [c for c in atribuidos_decorados if c["grupo_status"] == "atendimento"]
    if em_atendimento:
        previews = await asyncio.gather(
            *[_obter_preview_mensagem_usuario(c["id"]) for c in em_atendimento]
        )
        vistos = db.obter_vistos_tecnico(tecnico_id, [c["id"] for c in em_atendimento])
        for c, (texto, data) in zip(em_atendimento, previews):
            c["preview_texto"] = texto
            c["nova_mensagem"] = _mensagem_e_nova(data, vistos.get(c["id"]))

    return {
        "fila": fila_decorada,
        "atribuidos": atribuidos_decorados,
        "contagens": {"solucionado": total_solucionados, "fechado": total_fechados},
    }


@app.get("/tecnico", response_class=HTMLResponse)
async def painel_tecnico(request: Request):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return RedirectResponse("/chamados", status_code=303)

    usuario = sessao["usuario"]
    painel = await _computar_painel_tecnico(usuario["id"])
    return templates.TemplateResponse(
        request,
        "tecnico_painel.html",
        {
            "usuario": usuario,
            "fila_json": json.dumps(painel["fila"], ensure_ascii=False),
            "atribuidos_json": json.dumps(painel["atribuidos"], ensure_ascii=False),
            "contagens_json": json.dumps(painel["contagens"], ensure_ascii=False),
        },
    )


@app.get("/tecnico/resumo")
async def resumo_tecnico(request: Request):
    """Fallback do /tecnico/stream (SSE) - ver CLAUDE-v2 Fase 5: se o SSE
    cair, o front-end volta pra este polling classico. Diferente do
    /chamados/resumo (que so le o watermark local), aqui nao tem fonte
    local pra Fila/Meus atendimentos - e sempre ao vivo contra o GLPI,
    entao o fallback paga o mesmo custo do SSE, so que num intervalo
    maior."""
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({"fila": [], "atribuidos": []})
    return JSONResponse(await _computar_painel_tecnico(sessao["usuario"]["id"]))


GRUPOS_SOB_DEMANDA = {"solucionado": STATUS_RESOLVIDO, "fechado": STATUS_FECHADO}


@app.get("/tecnico/atendimentos/{grupo}")
async def tecnico_atendimentos_grupo(request: Request, grupo: str):
    """Carregamento sob demanda dos filtros 'Solucionados'/'Fechados' do
    painel - essas listas sao a maior parte do historico do tecnico e nao
    entram no carregamento padrao nem no SSE (ver _computar_painel_tecnico)."""
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse([], status_code=403)
    status = GRUPOS_SOB_DEMANDA.get(grupo)
    if status is None:
        return JSONResponse([], status_code=404)
    chamados = await listar_atribuidos_por_status(sessao["usuario"]["id"], status)
    return JSONResponse(_decorar_chamados_tecnico(chamados, atribuido=True))


@app.get("/tecnico/stream")
async def tecnico_stream_sse(request: Request):
    """Mesmo padrao de /chamados/stream: recalcula Fila+Meus atendimentos
    contra o GLPI a cada ciclo e so manda SSE se a lista mudou."""
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({}, status_code=403)
    tecnico_id = sessao["usuario"]["id"]

    async def stream():
        ultimo_enviado = None
        while True:
            if await request.is_disconnected():
                break
            try:
                atual = await _computar_painel_tecnico(tecnico_id)
            except Exception:
                logger.exception("erro no SSE do painel tecnico=%s", tecnico_id)
                atual = ultimo_enviado
            if atual != ultimo_enviado:
                yield f"data: {json.dumps(atual, ensure_ascii=False)}\n\n"
                ultimo_enviado = atual
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(INTERVALO_SSE_HUB_SEGUNDOS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=CABECALHOS_SSE)


async def _montar_timeline_tecnico(ticket_id: int) -> tuple[dict, list[dict]]:
    """Reconstroi a thread do jeito que o tecnico ve: descricao + timeline
    COMPLETA (publica e privada, via listar_timeline_completa - diferente
    de pagina_chamado, que so usa listar_followups_publicos). Followups
    marcados em db.followups_do_usuario sao do proprio colaborador
    (relayados) - mesma logica de autoria de pagina_chamado, so que aqui o
    rotulo e 'colaborador' em vez de 'usuario'."""
    resumo, timeline = await asyncio.gather(
        obter_chamado_completo(ticket_id), listar_timeline_completa(ticket_id)
    )
    ids_usuario = db.followups_do_usuario(ticket_id)
    autores_tecnico = db.autores_tecnico_followups(ticket_id)
    anexos_por_followup = _agrupar_anexos_por_followup(ticket_id, f"/tecnico/chamados/{ticket_id}/anexos")

    mensagens: list[dict] = [
        {"tipo": "sistema", "texto": f"Chamado #{ticket_id} - {resumo['titulo']}"},
    ]
    if resumo["descricao"]:
        mensagens.append(
            {
                "tipo": "colaborador",
                "texto": resumo["descricao"],
                "hora": _formatar_hora(resumo["data_criacao_completa"]),
                "anexos": anexos_por_followup.get(None, []),
            }
        )

    # nomes dos autores de uma vez, em paralelo (com cache) - antes era 1
    # ida ao GLPI por autor, dentro do loop.
    uids_autores = {
        item["users_id"]
        for item in timeline
        if item["id"] not in ids_usuario and item["id"] not in autores_tecnico
    }
    autores = await asyncio.gather(*[obter_usuario_por_id(uid) for uid in uids_autores])
    nomes_cache: dict[int, str] = {
        uid: (autor["nome_completo"] if autor else "Suporte")
        for uid, autor in zip(uids_autores, autores)
    }
    for item in timeline:
        hora = _formatar_hora(item["data"])
        if item["id"] in ids_usuario:
            mensagens.append({
                "tipo": "colaborador", "texto": item["conteudo"], "hora": hora,
                "anexos": anexos_por_followup.get(item["id"], []),
            })
            continue
        if item["id"] in autores_tecnico:
            # respondido/anotado por este painel - o nome real do tecnico
            # (nao o token de servico compartilhado que o GLPI ve como
            # autor de toda escrita do app).
            autor_nome = autores_tecnico[item["id"]]
        else:
            autor_nome = nomes_cache.get(item["users_id"], "Suporte")
        mensagens.append(
            {
                "tipo": "tecnico",
                "texto": item["conteudo"],
                "autorNome": autor_nome,
                "privado": item["privado"],
                "hora": hora,
                "anexos": anexos_por_followup.get(item["id"], []),
            }
        )

    if resumo["status"] in STATUS_RESOLVIDO_OU_FECHADO:
        solucao = await obter_solucao(ticket_id)
        if solucao:
            mensagens.append({"tipo": "solucao", "texto": solucao})

    return resumo, mensagens


@app.get("/tecnico/chamados/{ticket_id}", response_class=HTMLResponse)
async def tecnico_thread(request: Request, ticket_id: int):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return RedirectResponse("/chamados", status_code=303)

    (resumo, mensagens), tecnico_atribuido = await asyncio.gather(
        _montar_timeline_tecnico(ticket_id), obter_tecnico_atribuido(ticket_id)
    )
    db.marcar_chamado_visto_tecnico(ticket_id, sessao["usuario"]["id"])

    chamado_info = {"id": ticket_id, "status": resumo["status"], "titulo": resumo["titulo"]}
    return templates.TemplateResponse(
        request,
        "tecnico_thread.html",
        {
            "usuario": sessao["usuario"],
            "chamado": chamado_info,
            "chamado_json": json.dumps(chamado_info, ensure_ascii=False),
            "tecnico_atribuido_json": json.dumps(tecnico_atribuido, ensure_ascii=False),
            "mensagens_iniciais_json": json.dumps(mensagens, ensure_ascii=False),
            "total_mensagens_iniciais": len(mensagens),
            "somente_leitura": resumo["status"] == STATUS_FECHADO,
        },
    )


@app.get("/tecnico/chamados/{ticket_id}/eventos")
async def tecnico_thread_sse(request: Request, ticket_id: int, desde: int = 0):
    """SSE auto-contido (NAO reusa eventos_pendentes/loop_polling do
    colaborador, pra nao arriscar regressao no relay que ja funciona):
    recalcula a timeline completa a cada poucos segundos e manda so as
    entradas alem de `desde` (a pagina manda o total ja renderizado no
    carregamento inicial, senao a primeira mensagem do SSE duplicaria o
    historico inteiro)."""
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse([], status_code=403)

    async def stream():
        enviadas = desde
        ultimo_status: int | None = None
        while True:
            if await request.is_disconnected():
                break
            try:
                resumo, mensagens = await _montar_timeline_tecnico(ticket_id)
            except Exception:
                logger.exception("erro no SSE da thread tecnico chamado=%s", ticket_id)
                await asyncio.sleep(INTERVALO_SSE_TECNICO_SEGUNDOS)
                continue

            novas = mensagens[enviadas:]
            houve_envio = False
            if ultimo_status is not None and resumo["status"] != ultimo_status:
                evento = {"tipo": "status", "chamado": {"id": ticket_id, "status": resumo["status"]}}
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
                houve_envio = True
            for msg in novas:
                yield f"data: {json.dumps({'tipo': 'mensagem', 'mensagem': msg}, ensure_ascii=False)}\n\n"
                houve_envio = True
            if not houve_envio:
                yield ": keep-alive\n\n"

            enviadas = len(mensagens)
            ultimo_status = resumo["status"]
            if resumo["status"] == STATUS_FECHADO:
                break
            await asyncio.sleep(INTERVALO_SSE_TECNICO_SEGUNDOS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=CABECALHOS_SSE)


@app.post("/tecnico/chamados/{ticket_id}/assumir")
async def tecnico_assumir(request: Request, ticket_id: int):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({"ok": False, "erro": "Sessão expirada. Atualiza a página."}, status_code=403)

    ja_atribuido = await obter_tecnico_atribuido(ticket_id)
    if ja_atribuido:
        return JSONResponse(
            {"ok": False, "erro": f"Já foi assumido por {ja_atribuido}."}, status_code=409
        )

    try:
        await atribuir_tecnico(ticket_id, sessao["usuario"]["id"])
    except Exception:
        logger.exception("erro ao assumir chamado=%s", ticket_id)
        return JSONResponse(
            {"ok": False, "erro": "Não consegui assumir agora. Tenta de novo."}, status_code=502
        )

    logger.info("chamado=%s assumido por tecnico=%s", ticket_id, sessao["usuario"]["login"])
    return JSONResponse({"ok": True})


async def _anexar_arquivo_tecnico(arquivo: UploadFile | None, ticket_id: int) -> dict | None:
    """Valida+sobe+vincula um anexo do tecnico. Levanta AnexoInvalido - quem
    chama decide como reportar (JSONResponse 422, mesmo padrao das rotas)."""
    if not arquivo:
        return None
    return await _processar_anexo(arquivo, ticket_id=ticket_id)


@app.post("/tecnico/chamados/{ticket_id}/responder")
async def tecnico_responder(
    request: Request,
    ticket_id: int,
    mensagem: str = Form(""),
    arquivo: UploadFile | None = File(None),
):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({"ok": False, "erro": "Sessão expirada. Atualiza a página."}, status_code=403)

    mensagem = mensagem.strip()
    if not mensagem and not arquivo:
        return JSONResponse(
            {"ok": False, "erro": "Escreve algo ou anexa um arquivo antes de enviar."}, status_code=422
        )

    try:
        info_anexo = await _anexar_arquivo_tecnico(arquivo, ticket_id)
    except AnexoInvalido as erro:
        return JSONResponse({"ok": False, "erro": erro.motivo}, status_code=422)

    mensagem_final = _texto_com_aviso_anexo(mensagem, arquivo.filename if arquivo else None)
    try:
        novo_id = await criar_followup_publico(ticket_id, mensagem_final)
    except Exception:
        logger.exception("erro ao responder chamado=%s", ticket_id)
        return JSONResponse(
            {"ok": False, "erro": "Não consegui enviar agora. Tenta de novo."}, status_code=502
        )

    db.marcar_followup_do_tecnico(ticket_id, novo_id, sessao["usuario"]["nome_completo"])
    if info_anexo:
        db.registrar_anexo(
            ticket_id, sessao["usuario"]["id"], info_anexo["nome_arquivo"],
            info_anexo["mime_tipo"], info_anexo["documento_id"], followup_id=novo_id,
        )
    logger.info("chamado=%s resposta publica tecnico=%s", ticket_id, sessao["usuario"]["login"])
    return JSONResponse({"ok": True})


@app.post("/tecnico/chamados/{ticket_id}/nota")
async def tecnico_nota(
    request: Request,
    ticket_id: int,
    mensagem: str = Form(""),
    arquivo: UploadFile | None = File(None),
):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({"ok": False, "erro": "Sessão expirada. Atualiza a página."}, status_code=403)

    mensagem = mensagem.strip()
    if not mensagem and not arquivo:
        return JSONResponse(
            {"ok": False, "erro": "Escreve algo ou anexa um arquivo antes de enviar."}, status_code=422
        )

    try:
        info_anexo = await _anexar_arquivo_tecnico(arquivo, ticket_id)
    except AnexoInvalido as erro:
        return JSONResponse({"ok": False, "erro": erro.motivo}, status_code=422)

    mensagem_final = _texto_com_aviso_anexo(mensagem, arquivo.filename if arquivo else None)
    try:
        novo_id = await criar_followup_privado(ticket_id, mensagem_final)
    except Exception:
        logger.exception("erro ao registrar nota chamado=%s", ticket_id)
        return JSONResponse(
            {"ok": False, "erro": "Não consegui registrar agora. Tenta de novo."}, status_code=502
        )

    db.marcar_followup_do_tecnico(ticket_id, novo_id, sessao["usuario"]["nome_completo"])
    if info_anexo:
        db.registrar_anexo(
            ticket_id, sessao["usuario"]["id"], info_anexo["nome_arquivo"],
            info_anexo["mime_tipo"], info_anexo["documento_id"], followup_id=novo_id,
        )
    logger.info("chamado=%s nota interna tecnico=%s", ticket_id, sessao["usuario"]["login"])
    return JSONResponse({"ok": True})


@app.post("/tecnico/chamados/{ticket_id}/resolver")
async def tecnico_resolver(request: Request, ticket_id: int, solucao: str = Form(...)):
    sessao = _sessao_ativa(request)
    if not _exigir_tecnico(sessao):
        return JSONResponse({"ok": False, "erro": "Sessão expirada. Atualiza a página."}, status_code=403)

    solucao = solucao.strip()
    if len(solucao) < 10:
        return JSONResponse(
            {"ok": False, "erro": "Descreve a solução com mais detalhes (mínimo 10 caracteres)."},
            status_code=422,
        )

    try:
        await criar_solucao(ticket_id, solucao)
    except Exception:
        logger.exception("erro ao resolver chamado=%s", ticket_id)
        return JSONResponse(
            {"ok": False, "erro": "Não consegui resolver agora. Tenta de novo."}, status_code=502
        )

    logger.info("chamado=%s resolvido por tecnico=%s", ticket_id, sessao["usuario"]["login"])
    return JSONResponse({"ok": True})
