"""Funcoes diretas de API do GLPI, pra operacoes deterministicas e baratas
(identificacao de usuario) que nao precisam passar pelo Agent SDK.

Criacao/consulta de chamados continuam via MCP dentro do agente (agente.py).
"""
from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ_PROJETO / ".env")

GLPI_URL = os.environ["GLPI_URL"]
GLPI_APP_TOKEN = os.environ["GLPI_APP_TOKEN"]
GLPI_USER_TOKEN = os.environ["GLPI_USER_TOKEN"]
HORAS_CONFIRMACAO_SOLUCAO = int(os.environ.get("HORAS_CONFIRMACAO_SOLUCAO", "48"))

# search option ids de User (glpi_list_search_options User)
CAMPO_LOGIN = 1
CAMPO_ID = 2
CAMPO_PRIMEIRO_NOME = 9
CAMPO_SOBRENOME = 34
CAMPO_ATIVO = 8

# search option ids de Ticket (glpi_list_search_options Ticket)
CAMPO_TICKET_TITULO = 1
CAMPO_TICKET_ID = 2
CAMPO_TICKET_REQUERENTE = 4
CAMPO_TICKET_TECNICO = 5
CAMPO_TICKET_CATEGORIA = 7
CAMPO_TICKET_STATUS = 12
CAMPO_TICKET_DATA_ABERTURA = 15
CAMPO_TICKET_DATA_MOD = 19

# GLPI: 1=Novo 2=Em atendimento(atribuido) 3=Em atendimento(planejado)
# 4=Pendente 5=Resolvido 6=Fechado
STATUS_ABERTOS = {1, 2, 3, 4}
STATUS_RESOLVIDO = 5
STATUS_FECHADO = 6
STATUS_RESOLVIDO_OU_FECHADO = {STATUS_RESOLVIDO, STATUS_FECHADO}
STATUS_REABERTURA = 2  # pra onde o chamado volta quando o usuario diz "ainda nao resolveu"

# GLPI: CommonITILValidation - status de aprovacao de uma ITILSolution
# (checado ao vivo no codigo-fonte, /var/www/html/glpi/src/CommonITILValidation.php)
SOLUCAO_STATUS_ACEITA = 3
SOLUCAO_STATUS_RECUSADA = 4
ROTULOS_STATUS = {
    1: "Novo",
    2: "Em atendimento",
    3: "Em atendimento",
    4: "Pendente",
    5: "Resolvido",
    6: "Fechado",
}


def texto_prazo_confirmacao(data_resolucao: str | None) -> str | None:
    """Aviso de prazo pro card de 'resolvido, aguardando confirmacao' (Fase
    4). O fechamento automatico em si (nativo do GLPI ou job interno) fica
    pendente de decisao - por ora e so o aviso na tela."""
    if not data_resolucao:
        return None
    resolvido_em = datetime.strptime(data_resolucao, "%Y-%m-%d %H:%M:%S")
    prazo = resolvido_em + timedelta(hours=HORAS_CONFIRMACAO_SOLUCAO)
    return (
        f"Se não houver resposta até {prazo.strftime('%d/%m às %H:%M')}, "
        "o chamado será considerado resolvido automaticamente."
    )


async def _abrir_sessao(cliente: httpx.AsyncClient) -> str:
    resp = await cliente.get(
        f"{GLPI_URL}/apirest.php/initSession",
        headers={
            "App-Token": GLPI_APP_TOKEN,
            "Authorization": f"user_token {GLPI_USER_TOKEN}",
        },
    )
    resp.raise_for_status()
    return resp.json()["session_token"]


async def _fechar_sessao(cliente: httpx.AsyncClient, session_token: str) -> None:
    await cliente.get(
        f"{GLPI_URL}/apirest.php/killSession",
        headers={"App-Token": GLPI_APP_TOKEN, "Session-Token": session_token},
    )


async def buscar_usuario_por_login(login: str) -> dict | None:
    """Busca usuario no GLPI pelo login (usuario de rede). None se nao achar.

    Retorna dict com id, login, primeiro_nome, sobrenome, nome_completo, ativo.
    """
    resp = await _requisicao(
        "GET",
        "/search/User",
        params={
            "criteria[0][field]": CAMPO_LOGIN,
            "criteria[0][searchtype]": "contains",
            "criteria[0][value]": login,
            "forcedisplay[0]": CAMPO_ID,
            "forcedisplay[1]": CAMPO_LOGIN,
            "forcedisplay[2]": CAMPO_PRIMEIRO_NOME,
            "forcedisplay[3]": CAMPO_SOBRENOME,
            "forcedisplay[4]": CAMPO_ATIVO,
        },
    )
    resp.raise_for_status()
    corpo = resp.json()

    linhas = corpo.get("data") or []
    if not linhas:
        return None

    linha = linhas[0]
    primeiro_nome = linha.get(str(CAMPO_PRIMEIRO_NOME)) or ""
    sobrenome = linha.get(str(CAMPO_SOBRENOME)) or ""
    nome_completo = f"{primeiro_nome} {sobrenome}".strip() or linha.get(str(CAMPO_LOGIN), login)

    return {
        "id": linha[str(CAMPO_ID)],
        "login": linha.get(str(CAMPO_LOGIN), login),
        "nome_completo": nome_completo,
        "ativo": bool(linha.get(str(CAMPO_ATIVO), 0)),
    }


def _texto_simples(bruto: str) -> str:
    """GLPI guarda followups/solucoes como rich text (HTML, por vezes com
    as tags escapadas em entidades). O chat mostra texto puro, entao
    convertemos pra texto legivel aqui."""
    texto = html.unescape(bruto or "")
    texto = html.unescape(texto)  # as vezes vem escapado 2x (&#60;p&#62; -> <p> -> ja plano)
    texto = re.sub(r"<br\s*/?>", "\n", texto, flags=re.I)
    texto = re.sub(r"</p\s*>", "\n\n", texto, flags=re.I)
    texto = re.sub(r"<[^>]+>", "", texto)
    return texto.strip()


# Cliente HTTP compartilhado (keep-alive/pool de conexao TCP) - NAO
# confundir com a sessao do GLPI, que continua uma por chamada: o GLPI
# serializa chamadas concorrentes que usam o MESMO session-token (testado
# ao vivo: 20 chamadas com o mesmo token = ~4s; com tokens diferentes =
# 0.2s - bate com o lock de arquivo de sessao do PHP). Aqui e so a camada
# TCP/HTTP que e reaproveitada, o que e seguro e corta o handshake de
# conexao por requisicao.
_CLIENTE_HTTP: httpx.AsyncClient | None = None


def _cliente_http() -> httpx.AsyncClient:
    global _CLIENTE_HTTP
    if _CLIENTE_HTTP is None:
        _CLIENTE_HTTP = httpx.AsyncClient(timeout=10.0)
    return _CLIENTE_HTTP


async def _requisicao(metodo: str, caminho: str, **kwargs) -> httpx.Response:
    """Abre sessao GLPI, faz 1 requisicao, fecha sessao (sessao POR
    chamada de proposito - ver comentario de _cliente_http). O custo de
    abrir/fechar (~60ms) so aparece em cadeias sequenciais - os chamadores
    quentes paralelizam com asyncio.gather, entao vira custo unico."""
    cliente = _cliente_http()
    session_token = await _abrir_sessao(cliente)
    headers = kwargs.pop("headers", {})
    headers["App-Token"] = GLPI_APP_TOKEN
    headers["Session-Token"] = session_token
    try:
        resp = await cliente.request(
            metodo, f"{GLPI_URL}/apirest.php{caminho}", headers=headers, **kwargs
        )
    finally:
        await _fechar_sessao(cliente, session_token)
    return resp


# Cache de nomes de usuario: o mesmo tecnico/requerente e re-resolvido a
# cada tick de SSE/polling (a cada poucos segundos, pra sempre o mesmo
# resultado) - nome de usuario praticamente nao muda, entao 10min de TTL
# elimina a maior parte dessas chamadas sem risco pratico de nome velho.
_CACHE_NOMES: dict[int, tuple[float, dict | None]] = {}
_TTL_CACHE_NOMES_SEGUNDOS = 600


def _cache_nome_valido(usuario_id: int) -> tuple[bool, dict | None]:
    entrada = _CACHE_NOMES.get(usuario_id)
    if entrada and (time.monotonic() - entrada[0]) < _TTL_CACHE_NOMES_SEGUNDOS:
        return True, entrada[1]
    return False, None


async def obter_usuario_por_id(usuario_id: int) -> dict | None:
    """Resolve nome de exibicao de um usuario pelo ID (usado pra rotular
    followups do tecnico: 'Suporte - Fulano: ...'). Com cache (TTL)."""
    em_cache, valor = _cache_nome_valido(usuario_id)
    if em_cache:
        return valor
    resp = await _requisicao(
        "GET",
        "/search/User",
        params={
            "criteria[0][field]": CAMPO_ID,
            "criteria[0][searchtype]": "contains",
            "criteria[0][value]": usuario_id,
            "forcedisplay[0]": CAMPO_PRIMEIRO_NOME,
            "forcedisplay[1]": CAMPO_SOBRENOME,
        },
    )
    resp.raise_for_status()
    linhas = resp.json().get("data") or []
    if not linhas:
        _CACHE_NOMES[usuario_id] = (time.monotonic(), None)
        return None
    linha = linhas[0]
    primeiro_nome = linha.get(str(CAMPO_PRIMEIRO_NOME)) or ""
    sobrenome = linha.get(str(CAMPO_SOBRENOME)) or ""
    # fallback apresentavel (nunca "usuario 123" vazando ID interno pra
    # tela) pra conta de servico/tecnico sem nome cadastrado no GLPI.
    nome_completo = f"{primeiro_nome} {sobrenome}".strip() or "Suporte de TI"
    resultado = {"id": usuario_id, "nome_completo": nome_completo}
    _CACHE_NOMES[usuario_id] = (time.monotonic(), resultado)
    return resultado


async def listar_followups_publicos(ticket_id: int) -> list[dict]:
    """Followups publicos do chamado, ordenados por id crescente. Followups
    privados (is_private=1) NUNCA sao retornados aqui - nao devem aparecer
    no chat do colaborador."""
    resp = await _requisicao("GET", f"/Ticket/{ticket_id}/ITILFollowup")
    resp.raise_for_status()
    linhas = resp.json() or []
    publicos = [linha for linha in linhas if not linha.get("is_private")]
    publicos.sort(key=lambda linha: linha["id"])
    return [
        {
            "id": linha["id"],
            "conteudo": _texto_simples(linha["content"]),
            "users_id": linha["users_id"],
            "data": linha["date"],
        }
        for linha in publicos
    ]


async def listar_timeline_completa(ticket_id: int) -> list[dict]:
    """Followups publicos E privados do chamado, ordenados por id crescente -
    igual listar_followups_publicos mas SEM o filtro is_private. Uso
    EXCLUSIVO das rotas /tecnico/* (painel do tecnico) - nunca chamar isso
    de uma rota que serve o colaborador, ou nota interna vaza pro chat
    dele."""
    resp = await _requisicao("GET", f"/Ticket/{ticket_id}/ITILFollowup")
    resp.raise_for_status()
    linhas = resp.json() or []
    linhas.sort(key=lambda linha: linha["id"])
    return [
        {
            "id": linha["id"],
            "conteudo": _texto_simples(linha["content"]),
            "users_id": linha["users_id"],
            "data": linha["date"],
            "privado": bool(linha.get("is_private")),
        }
        for linha in linhas
    ]


async def buscar_perfis_usuario(usuario_id: int) -> list[int]:
    """IDs de todos os perfis (Profile_User) atribuidos ao usuario, em
    qualquer entidade - usado pra decidir o papel (tecnico/colaborador)
    no login.

    IMPORTANTE: filtra em Python (nao usa searchText[users_id]=... do GLPI -
    aquele filtro faz correspondencia por SUBSTRING no campo, entao buscar
    pelo usuario 9 tambem retorna os usuarios 19, 90-99, 109... um bug de
    seguranca real (papel tecnico vazando por coincidencia de digito)."""
    resp = await _requisicao("GET", "/Profile_User", params={"range": "0-999"})
    resp.raise_for_status()
    linhas = resp.json() or []
    return [linha["profiles_id"] for linha in linhas if linha.get("users_id") == usuario_id]


async def obter_chamado_resumo(ticket_id: int) -> dict:
    """Status e titulo atuais do chamado (uso no polling)."""
    resp = await _requisicao("GET", f"/Ticket/{ticket_id}")
    resp.raise_for_status()
    corpo = resp.json()
    return {"id": ticket_id, "status": corpo["status"], "titulo": corpo["name"]}


async def obter_chamado_completo(ticket_id: int) -> dict:
    """Status, titulo, descricao (texto simples) e data de abertura do
    chamado - usado pra reconstruir o topo de um thread ao abrir
    /chamados/{ticket_id} sem depender de estado em memoria (ex: apos
    reinicio do processo), e pelas tools de consulta da IA (Fase 3)."""
    resp = await _requisicao("GET", f"/Ticket/{ticket_id}")
    resp.raise_for_status()
    corpo = resp.json()
    return {
        "id": ticket_id,
        "status": corpo["status"],
        "titulo": corpo["name"],
        "descricao": _texto_simples(corpo.get("content", "")),
        "data_criacao": (corpo.get("date_creation") or "")[:10],
        "data_criacao_completa": corpo.get("date_creation"),
        "data_resolucao": corpo.get("solvedate"),
    }


# GLPI: tipo de ator num chamado (Ticket_User.type) - 1=requerente,
# 2=atribuido (tecnico), 3=observador.
TIPO_ATOR_ATRIBUIDO = 2


async def obter_tecnico_atribuido(ticket_id: int) -> str | None:
    """Nome do tecnico atribuido ao chamado, ou None se ainda ninguem
    assumiu - usado pela tool de consulta da IA (Fase 3)."""
    resp = await _requisicao("GET", f"/Ticket/{ticket_id}/Ticket_User")
    resp.raise_for_status()
    linhas = resp.json() or []
    atribuidos = [linha for linha in linhas if linha.get("type") == TIPO_ATOR_ATRIBUIDO]
    if not atribuidos:
        return None
    autor = await obter_usuario_por_id(atribuidos[0]["users_id"])
    return autor["nome_completo"] if autor else None


async def atribuir_tecnico(ticket_id: int, tecnico_id: int) -> None:
    """'Assumir': cria o Ticket_User (type=atribuido) pro tecnico. O
    chamador PRECISA checar obter_tecnico_atribuido antes de chamar isso -
    essa funcao nao protege contra sobrescrever um tecnico ja atribuido
    (evitar corrida entre dois tecnicos clicando 'Assumir' ao mesmo
    tempo e responsabilidade da rota, nao desta funcao)."""
    resp = await _requisicao(
        "POST",
        "/Ticket_User",
        json={
            "input": {
                "tickets_id": ticket_id,
                "users_id": tecnico_id,
                "type": TIPO_ATOR_ATRIBUIDO,
            }
        },
    )
    resp.raise_for_status()


async def obter_solucao(ticket_id: int) -> str | None:
    resp = await _requisicao("GET", f"/Ticket/{ticket_id}/ITILSolution")
    resp.raise_for_status()
    linhas = resp.json() or []
    if not linhas:
        return None
    conteudo = linhas[-1].get("content")
    return _texto_simples(conteudo) if conteudo else None


async def criar_solucao(ticket_id: int, conteudo: str) -> None:
    """Tecnico resolvendo o chamado: registra a ITILSolution e garante que
    o status vira Resolvido (nao confiar so no hook automatico do GLPI -
    forcar o PUT explicito, igual aprovar_solucao/recusar_solucao ja fazem
    com status)."""
    resp = await _requisicao(
        "POST",
        "/ITILSolution",
        json={"input": {"itemtype": "Ticket", "items_id": ticket_id, "content": conteudo}},
    )
    resp.raise_for_status()
    resp = await _requisicao(
        "PUT", f"/Ticket/{ticket_id}", json={"input": {"status": STATUS_RESOLVIDO}}
    )
    resp.raise_for_status()


async def criar_followup_publico(ticket_id: int, conteudo: str) -> int:
    """Registra a mensagem do colaborador como followup publico do chamado
    (modo acompanhamento / relay puro, sem passar pela IA)."""
    resp = await _requisicao(
        "POST",
        "/ITILFollowup",
        json={
            "input": {
                "itemtype": "Ticket",
                "items_id": ticket_id,
                "content": conteudo,
                "is_private": 0,
            }
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def criar_followup_privado(ticket_id: int, conteudo: str) -> int:
    """Nota interna do tecnico - NUNCA deve aparecer no chat do colaborador
    (is_private=1). Uso exclusivo das rotas /tecnico/*."""
    resp = await _requisicao(
        "POST",
        "/ITILFollowup",
        json={
            "input": {
                "itemtype": "Ticket",
                "items_id": ticket_id,
                "content": conteudo,
                "is_private": 1,
            }
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def aprovar_solucao(ticket_id: int) -> None:
    """'Funcionou': fecha o chamado aprovando a solucao atual. Confirmado ao
    vivo no codigo-fonte do GLPI (CommonITILObject::pre_updateInDB): mandar
    '_accepted': 1 junto com status=fechado marca a ULTIMA ITILSolution como
    aceita (status=3, users_id_approval/date_approval preenchidos)."""
    resp = await _requisicao(
        "PUT", f"/Ticket/{ticket_id}", json={"input": {"status": STATUS_FECHADO, "_accepted": 1}}
    )
    resp.raise_for_status()


async def recusar_solucao(ticket_id: int, motivo: str) -> int:
    """'Ainda nao resolveu': reabre o chamado (volta pra 'em atendimento')
    e registra o motivo como followup publico. Confirmado ao vivo: so
    mudar o status pra fora de resolvido/fechado ja faz o GLPI marcar
    sozinho a ultima ITILSolution como recusada (status=4) - nao precisa
    tocar na solucao manualmente.

    Retorna o id do followup criado - o chamador PRECISA gravar esse id
    como watermark local (ultimo_followup_id), senao o proximo poll do
    relay confunde a propria explicacao do usuario com uma resposta nova
    do tecnico (ambas sao so 'followup publico' pro GLPI)."""
    resp = await _requisicao(
        "PUT", f"/Ticket/{ticket_id}", json={"input": {"status": STATUS_REABERTURA}}
    )
    resp.raise_for_status()
    return await criar_followup_publico(ticket_id, f"Ainda não resolveu: {motivo}")


async def buscar_todos_chamados_usuario(usuario_id: int) -> list[dict]:
    """Todos os chamados (qualquer status) onde o usuario e requerente,
    mais recente primeiro. Fonte da verdade pra 'Meus chamados' - a tela
    nunca decide sozinha quais chamados existem, so o GLPI decide."""
    resp = await _requisicao(
        "GET",
        "/search/Ticket",
        params={
            "criteria[0][field]": CAMPO_TICKET_REQUERENTE,
            "criteria[0][searchtype]": "equals",
            "criteria[0][value]": usuario_id,
            "forcedisplay[0]": CAMPO_TICKET_ID,
            "forcedisplay[1]": CAMPO_TICKET_TITULO,
            "forcedisplay[2]": CAMPO_TICKET_STATUS,
            "sort": CAMPO_TICKET_ID,
            "order": "DESC",
            "range": "0-99",
        },
    )
    resp.raise_for_status()
    linhas = resp.json().get("data") or []
    return [
        {
            "id": linha[str(CAMPO_TICKET_ID)],
            "titulo": linha.get(str(CAMPO_TICKET_TITULO), ""),
            "status": linha.get(str(CAMPO_TICKET_STATUS)),
        }
        for linha in linhas
    ]


async def buscar_chamados_abertos_usuario(usuario_id: int) -> list[dict]:
    """Chamados nao resolvidos/fechados onde o usuario e requerente."""
    todos = await buscar_todos_chamados_usuario(usuario_id)
    return [c for c in todos if c["status"] in STATUS_ABERTOS]


async def _resolver_nomes_usuarios(usuario_ids: set[int]) -> dict[int, str]:
    """Resolve o nome de varios usuarios de uma vez (1 busca agrupada em vez
    de 1 chamada por requerente) - usado pra montar a coluna 'requerente' da
    Fila/Meus atendimentos sem rajada de chamadas em /Ticket_User. Usa e
    alimenta o mesmo _CACHE_NOMES de obter_usuario_por_id, entao nos ticks
    seguintes do SSE a busca costuma nem sair pro GLPI."""
    nomes: dict[int, str] = {}
    faltantes: set[int] = set()
    for usuario_id in usuario_ids:
        em_cache, valor = _cache_nome_valido(usuario_id)
        if em_cache:
            if valor:
                nomes[usuario_id] = valor["nome_completo"]
            continue
        faltantes.add(usuario_id)
    if not faltantes:
        return nomes

    params = {
        "forcedisplay[0]": CAMPO_ID,
        "forcedisplay[1]": CAMPO_PRIMEIRO_NOME,
        "forcedisplay[2]": CAMPO_SOBRENOME,
    }
    for indice, usuario_id in enumerate(faltantes):
        prefixo = f"criteria[{indice}]"
        if indice:
            params[f"{prefixo}[link]"] = "OR"
        params[f"{prefixo}[field]"] = CAMPO_ID
        params[f"{prefixo}[searchtype]"] = "equals"
        params[f"{prefixo}[value]"] = usuario_id
    resp = await _requisicao("GET", "/search/User", params=params)
    resp.raise_for_status()
    linhas = resp.json().get("data") or []
    agora = time.monotonic()
    for linha in linhas:
        usuario_id = int(linha[str(CAMPO_ID)])
        primeiro_nome = linha.get(str(CAMPO_PRIMEIRO_NOME)) or ""
        sobrenome = linha.get(str(CAMPO_SOBRENOME)) or ""
        nome = f"{primeiro_nome} {sobrenome}".strip() or "Suporte de TI"
        nomes[usuario_id] = nome
        _CACHE_NOMES[usuario_id] = (agora, {"id": usuario_id, "nome_completo": nome})
    return nomes


def _montar_criterio_status_aberto(indice_pai: int) -> dict:
    """Grupo de criterios (link=OR) pros 4 status 'abertos' (1-4) - o campo
    Status (12) do GLPI so aceita searchtype=equals (confirmado ao vivo via
    glpi_list_search_options: lessthan/morethan sao ignorados nesse campo),
    entao precisa agrupar um criterio por valor."""
    params = {f"criteria[{indice_pai}][link]": "AND"}
    for i, status in enumerate((1, 2, 3, 4)):
        prefixo = f"criteria[{indice_pai}][criteria][{i}]"
        params[f"{prefixo}[link]"] = "OR"
        params[f"{prefixo}[field]"] = CAMPO_TICKET_STATUS
        params[f"{prefixo}[searchtype]"] = "equals"
        params[f"{prefixo}[value]"] = status
    return params


async def _buscar_e_montar_lista(params: dict) -> list[dict]:
    """Base comum de listar_fila/listar_atribuidos_*: dispara a busca, monta
    os dicts em portugues e resolve os nomes dos requerentes em lote."""
    resp = await _requisicao("GET", "/search/Ticket", params=params)
    resp.raise_for_status()
    linhas = resp.json().get("data") or []
    requerente_ids = {
        int(linha[str(CAMPO_TICKET_REQUERENTE)])
        for linha in linhas
        if linha.get(str(CAMPO_TICKET_REQUERENTE))
    }
    nomes = await _resolver_nomes_usuarios(requerente_ids)
    resultado = []
    for linha in linhas:
        requerente_id = linha.get(str(CAMPO_TICKET_REQUERENTE))
        requerente_id = int(requerente_id) if requerente_id else None
        resultado.append(
            {
                "id": linha[str(CAMPO_TICKET_ID)],
                "titulo": linha.get(str(CAMPO_TICKET_TITULO), ""),
                "status": linha.get(str(CAMPO_TICKET_STATUS)),
                "categoria": linha.get(str(CAMPO_TICKET_CATEGORIA)) or None,
                "requerente_id": requerente_id,
                "requerente_nome": nomes.get(requerente_id, "Suporte de TI"),
                "data_abertura": linha.get(str(CAMPO_TICKET_DATA_ABERTURA)),
            }
        )
    return resultado


async def listar_fila() -> list[dict]:
    """TODOS os chamados abertos (status 1-4) sem tecnico atribuido, da
    entidade inteira - a 'Fila' do painel do tecnico (Fase 6). Mais antigo
    primeiro (data de abertura ASC)."""
    params = {
        "criteria[0][field]": CAMPO_TICKET_TECNICO,
        "criteria[0][searchtype]": "equals",
        "criteria[0][value]": 0,
        "forcedisplay[0]": CAMPO_TICKET_ID,
        "forcedisplay[1]": CAMPO_TICKET_TITULO,
        "forcedisplay[2]": CAMPO_TICKET_STATUS,
        "forcedisplay[3]": CAMPO_TICKET_CATEGORIA,
        "forcedisplay[4]": CAMPO_TICKET_REQUERENTE,
        "forcedisplay[5]": CAMPO_TICKET_DATA_ABERTURA,
        "sort": CAMPO_TICKET_DATA_ABERTURA,
        "order": "ASC",
        # 0-499: testado ao vivo, o GLPI aceita esse range numa unica
        # busca sem paginar (o limite de configuracao default costuma
        # ser 500) - 0-99 truncava silenciosamente tecnicos com mais de
        # 100 chamados no historico (ver listar_atribuidos_ativos).
        "range": "0-499",
    }
    params.update(_montar_criterio_status_aberto(1))
    return await _buscar_e_montar_lista(params)


def _params_atribuidos(tecnico_id: int) -> dict:
    return {
        "criteria[0][field]": CAMPO_TICKET_TECNICO,
        "criteria[0][searchtype]": "equals",
        "criteria[0][value]": tecnico_id,
        "forcedisplay[0]": CAMPO_TICKET_ID,
        "forcedisplay[1]": CAMPO_TICKET_TITULO,
        "forcedisplay[2]": CAMPO_TICKET_STATUS,
        "forcedisplay[3]": CAMPO_TICKET_CATEGORIA,
        "forcedisplay[4]": CAMPO_TICKET_REQUERENTE,
        "forcedisplay[5]": CAMPO_TICKET_DATA_ABERTURA,
        "sort": CAMPO_TICKET_DATA_MOD,
        "order": "DESC",
        # 0-99 truncava tecnicos com historico grande (confirmado ao
        # vivo: um tecnico de teste tinha 168 atribuidos, 68 ficavam fora).
        "range": "0-499",
    }


async def listar_atribuidos_ativos(tecnico_id: int) -> list[dict]:
    """So os chamados ATIVOS (status 1-4) atribuidos a esse tecnico - o
    carregamento padrao de 'Meus atendimentos'. Solucionados/fechados sao
    a maior parte do historico e o GLPI cobra ~7ms por linha hidratada
    (medido ao vivo: 169 linhas = 1.3s POR TICK de SSE), entao eles ficam
    de fora daqui e so carregam sob demanda (listar_atribuidos_por_status)
    quando o tecnico clica no filtro."""
    params = _params_atribuidos(tecnico_id)
    params.update(_montar_criterio_status_aberto(1))
    return await _buscar_e_montar_lista(params)


async def listar_atribuidos_por_status(tecnico_id: int, status: int) -> list[dict]:
    """Chamados atribuidos a esse tecnico num status especifico (5=
    solucionado, 6=fechado) - carregamento sob demanda do filtro do
    painel (ver listar_atribuidos_ativos)."""
    params = _params_atribuidos(tecnico_id)
    params["criteria[1][link]"] = "AND"
    params["criteria[1][field]"] = CAMPO_TICKET_STATUS
    params["criteria[1][searchtype]"] = "equals"
    params["criteria[1][value]"] = status
    return await _buscar_e_montar_lista(params)


async def contar_atribuidos_por_status(tecnico_id: int, status: int) -> int:
    """So a CONTAGEM (range 0-0, GLPI devolve totalcount sem hidratar
    linha nenhuma - 0.16s contra 1.3s da lista completa) - pros numeros
    nos botoes de filtro do painel sem pagar a lista inteira."""
    resp = await _requisicao(
        "GET",
        "/search/Ticket",
        params={
            "criteria[0][field]": CAMPO_TICKET_TECNICO,
            "criteria[0][searchtype]": "equals",
            "criteria[0][value]": tecnico_id,
            "criteria[1][link]": "AND",
            "criteria[1][field]": CAMPO_TICKET_STATUS,
            "criteria[1][searchtype]": "equals",
            "criteria[1][value]": status,
            "forcedisplay[0]": CAMPO_TICKET_ID,
            "range": "0-0",
        },
    )
    resp.raise_for_status()
    return int(resp.json().get("totalcount") or 0)
