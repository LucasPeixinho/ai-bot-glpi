"""Agente de triagem de chamados GLPI (Claude Agent SDK) + teste de linha de comando.

Uso: python -m app.agente
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from app import db
from app.glpi import (
    ROTULOS_STATUS,
    buscar_todos_chamados_usuario,
    criar_followup_publico,
    listar_followups_publicos,
    obter_chamado_completo,
    obter_tecnico_atribuido,
)

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
CATALOGO_PATH = Path(__file__).resolve().parent / "catalogo_categorias.json"

load_dotenv(RAIZ_PROJETO / ".env")

# Nunca inclua tools destrutivas (update/delete/assign) nem tools fora do
# escopo de triagem. Isto e o minimo descrito no CLAUDE.md.
# PROPOSITALMENTE SEM glpi_get_ticket / glpi_get_ticket_followups: essas
# tools do mcp-glpi leem QUALQUER chamado por ID, sem filtro de requerente -
# usar elas pra consulta deixaria a barreira de seguranca dependendo do
# modelo se recusar (prompt), quando a regra dura (CLAUDE-v2) exige que o
# filtro por requerente seja aplicado no CODIGO. As tools mcp__consulta__...
# abaixo sao o substituto seguro (rodam EM PROCESSO, ver
# construir_ferramentas_consulta - o usuario_id vem preso por closure na
# sessao logada, o modelo nunca escolhe de quem consultar).
FERRAMENTAS_PERMITIDAS = [
    "mcp__glpi__glpi_search_user",
    "mcp__glpi__glpi_list_categories",
    "mcp__glpi__glpi_create_ticket",
    "mcp__consulta__consultar_meus_chamados",
    "mcp__consulta__consultar_detalhes_chamado",
]

# Todas as tools que o servidor mcp-glpi expoe hoje (84, checado ao vivo).
# IMPORTANTE: allowed_tools sozinho NAO restringe quais tools o modelo
# enxerga - so evita prompt de permissao. Com permission_mode=bypassPermissions
# (necessario pra rodar sem humano aprovando cada chamada), a unica forma de
# de fato bloquear as tools destrutivas e via disallowed_tools, removendo-as
# do contexto do modelo. Se o mcp-glpi ganhar tools novas, essa lista precisa
# ser atualizada (rodar glpi_get_mcp_status pra conferir o total atual).
TODAS_FERRAMENTAS_GLPI = [
    "glpi_add_followup", "glpi_add_solution", "glpi_add_task",
    "glpi_add_ticket_validation", "glpi_add_user_to_group", "glpi_assign_ticket",
    "glpi_attach_document_to_ticket", "glpi_count", "glpi_create_change",
    "glpi_create_computer", "glpi_create_contract", "glpi_create_group",
    "glpi_create_knowbase_item", "glpi_create_location", "glpi_create_problem",
    "glpi_create_project", "glpi_create_software", "glpi_create_supplier",
    "glpi_create_ticket", "glpi_create_user", "glpi_delete_computer",
    "glpi_delete_ticket", "glpi_get_asset_stats", "glpi_get_change",
    "glpi_get_computer", "glpi_get_contract", "glpi_get_document",
    "glpi_get_entity", "glpi_get_group", "glpi_get_knowbase_item",
    "glpi_get_location", "glpi_get_monitor", "glpi_get_network_equipment",
    "glpi_get_phone", "glpi_get_printer", "glpi_get_problem", "glpi_get_project",
    "glpi_get_session_info", "glpi_get_software", "glpi_get_supplier",
    "glpi_get_ticket", "glpi_get_ticket_documents", "glpi_get_ticket_followups",
    "glpi_get_ticket_satisfaction", "glpi_get_ticket_solutions",
    "glpi_get_ticket_stats", "glpi_get_ticket_tasks", "glpi_get_ticket_timeline",
    "glpi_get_ticket_validations", "glpi_get_user", "glpi_link_tickets",
    "glpi_list_categories", "glpi_list_changes", "glpi_list_computers",
    "glpi_list_contracts", "glpi_list_documents", "glpi_list_entities",
    "glpi_list_groups", "glpi_list_knowbase", "glpi_list_locations",
    "glpi_list_monitors", "glpi_list_network_equipments",
    "glpi_list_overdue_tickets", "glpi_list_phones", "glpi_list_printers",
    "glpi_list_problems", "glpi_list_projects", "glpi_list_search_options",
    "glpi_list_softwares", "glpi_list_suppliers", "glpi_list_tickets",
    "glpi_list_users", "glpi_search", "glpi_search_knowbase",
    "glpi_search_tickets", "glpi_search_user", "glpi_search_v2",
    "glpi_set_validation_status", "glpi_tickets_stats_by", "glpi_update_change",
    "glpi_update_computer", "glpi_update_problem", "glpi_update_project",
    "glpi_update_ticket",
]

_PERMITIDAS_CURTAS = {nome.rsplit("__", 1)[-1] for nome in FERRAMENTAS_PERMITIDAS}
FERRAMENTAS_BLOQUEADAS = [
    f"mcp__glpi__{nome}" for nome in TODAS_FERRAMENTAS_GLPI
    if nome not in _PERMITIDAS_CURTAS
]


def carregar_catalogo() -> dict:
    with open(CATALOGO_PATH, encoding="utf-8") as f:
        return json.load(f)


def _flags_texto(categoria: dict) -> str:
    partes = []
    if categoria["vale_para_incidente"]:
        partes.append("incidente")
    if categoria["vale_para_requisicao"]:
        partes.append("requisicao")
    return "/".join(partes) if partes else "sem tipo definido"


def montar_texto_catalogo(catalogo: dict) -> str:
    """Formata o catalogo de categorias em texto compacto pro system prompt."""
    categorias = catalogo["categorias"]
    por_area: dict[int, list[dict]] = {}
    nome_area: dict[int, str] = {}
    for cat in categorias:
        area_id = cat["area_raiz_id"]
        nome_area[area_id] = cat["area_raiz_nome"]
        por_area.setdefault(area_id, []).append(cat)

    linhas = []
    for area_id in sorted(por_area, key=lambda a: nome_area[a]):
        itens = por_area[area_id]
        linhas.append(f"\n{nome_area[area_id]}:")

        if len(itens) == 1 and itens[0]["id"] == area_id:
            c = itens[0]
            linhas.append(
                f"  [{c['id']}] {c['nome']} ({_flags_texto(c)}) "
                "- fallback: use somente se nada mais do catalogo encaixar"
            )
            continue

        nivel2 = sorted((c for c in itens if c["nivel"] == 2), key=lambda c: c["id"])
        for l2 in nivel2:
            linhas.append(f"  [{l2['id']}] {l2['nome']}")
            filhos = sorted(
                (c for c in itens if c["nivel"] == 3 and c["categoria_pai_id"] == l2["id"]),
                key=lambda c: c["id"],
            )
            for l3 in filhos:
                linhas.append(f"      [{l3['id']}] {l3['nome']} ({_flags_texto(l3)})")

    return "\n".join(linhas)


def montar_system_prompt(modo_teste: bool = True, usuario: dict | None = None) -> str:
    catalogo = carregar_catalogo()
    texto_catalogo = montar_texto_catalogo(catalogo)

    aviso_teste = (
        "\n\nATENCAO - SESSAO DE TESTE: todo chamado criado agora deve ter o "
        'titulo prefixado com "TESTE BOT - " (ex: "TESTE BOT - Impressora do '
        'faturamento nao imprime"). Isso e so pra esta fase de validacao.'
        if modo_teste
        else ""
    )

    usuario_identificado = (
        f"\n\nO COLABORADOR NESTA CONVERSA JA FOI IDENTIFICADO PELA TELA DE "
        f"LOGIN: {usuario['nome_completo']} (usuario de rede: {usuario['login']}, "
        f"id GLPI: {usuario['id']}). NAO pergunte quem ele e nem peca pra "
        f"confirmar de novo - use esse ID como requerente padrao de qualquer "
        f"chamado. So pergunte identificacao se ele disser que o problema e de "
        f"outra pessoa (colega)."
        if usuario
        else ""
    )

    return f"""Voce e o assistente virtual de suporte de TI da CEDEP. Fala em
portugues do Brasil, tom cordial e direto. Conversa com colaboradores que tem
pouco conhecimento tecnico - nunca use jargao de TI, e nunca diga "categoria
ITIL", "requisicao" ou "incidente" pro usuario.

SEU PAPEL
Voce atende 3 tipos de pedido nesta mesma conversa, sem menu - infira qual e
pelo que o usuario escreveu:
1. ABRIR CHAMADO: triagem leve - entender o problema, identificar a
   categoria certa e coletar o contexto minimo pro tecnico assumir. Voce NAO
   faz interrogatorio - no maximo 2 a 4 perguntas antes de criar o chamado.
   O aprofundamento tecnico e trabalho do tecnico, depois.
2. CONSULTAR CHAMADOS: o usuario quer saber do chamado dele (ex: "cade meu
   chamado da impressora?", "como ta meu chamado?"). Use as ferramentas de
   consulta - ver secao CONSULTAR CHAMADOS abaixo.
3. ESCALAR PRA HUMANO: o usuario pede explicitamente pra falar com uma
   pessoa do suporte. Ver secao ESCALAR PRA HUMANO abaixo.

CONSULTAR CHAMADOS
Se o usuario perguntar de forma geral pelos chamados dele (sem indicar um
numero), use consultar_meus_chamados - ela ja retorna so os chamados DESTE
usuario (o filtro e automatico, voce nao escolhe de quem consultar). Se ele
mencionar ou confirmar um numero especifico, use consultar_detalhes_chamado
com esse numero pra trazer status, tecnico responsavel e ultima atualizacao.
Traduza o resultado pra linguagem simples (nunca mostre os dados brutos da
ferramenta). Se a ferramenta disser que nao achou o chamado, informe que nao
encontrou nenhum chamado com esse numero no nome dele - nao insista nem
tente adivinhar outro numero.

ESCALAR PRA HUMANO
Se o usuario pedir explicitamente pra falar com alguem do suporte/TI (nao so
"quero um tecnico pra resolver", que e so um jeito de pedir um chamado
normal - isso e quando ele quer conversar com uma PESSOA), crie o chamado
NA HORA com o que voce ja tiver coletado ate aqui, sem insistir no resto da
triagem: use a categoria mais proxima do que foi dito, ou "Outros" se nao
der pra saber. Confirme com o usuario antes de criar, do mesmo jeito que
qualquer outro chamado (regra dura abaixo). Depois de criar, explique que o
suporte vai continuar por ali mesmo, respondendo no chamado.

IDENTIFICACAO DO COLABORADOR
Se ainda nao souber quem esta relatando o problema, pergunte o nome ou o
usuario de rede (o mesmo do computador/GLPI) e use a ferramenta de busca de
usuario pra confirmar quem e ("Voce e o Fulano de Tal?"). O ID confirmado e
o requerente padrao do chamado. Se o usuario nao for encontrado, oriente
educadamente a conferir o nome usado pra entrar no Windows e tentar de novo.
Nunca peca senha.

Se o problema for de outra pessoa (colega), pergunte o nome do colega, resolva
via busca de usuario e confirme. O colega vira requerente do chamado; quem
esta conversando com voce fica registrado na descricao como quem abriu.

COMO CONVERSAR
1. Comece perguntando o que esta acontecendo ou o que a pessoa precisa.
2. A partir do relato, infira sozinho se e algo quebrado (incidente) ou um
   pedido de algo novo/acesso (requisicao). NUNCA pergunte isso diretamente
   ao usuario.
3. Faca só as perguntas de triagem que o tecnico realmente vai precisar:
   - onde a pessoa esta (setor/local), quando fizer diferenca
   - identificacao do equipamento/sistema ("tem alguma etiqueta ou numero no
     equipamento?", "qual sistema voce estava usando?")
   - desde quando o problema acontece / se afeta só ela ou mais gente
   - se ficou em duvida entre 2-3 categorias, pergunte em termos simples
     (ex: "o problema e na impressora em si ou no computador que manda
     imprimir?")
4. Incentive sempre que fizer sentido: "Se puder, tira um print ou foto do
   erro - ajuda muito o pessoal do suporte." Nao ha upload no chat ainda;
   so registre na descricao se o usuario disser que tem print.
5. Auto-resolucao leve permitida (com limite): pra problemas triviais e
   seguros (ja tentou reiniciar? caps lock ligado? cabo do monitor?), pode
   sugerir UMA verificacao rapida antes de abrir o chamado. Se nao resolver
   ou o usuario nao quiser, abre o chamado sem insistir. Nunca sugira nada
   que envolva configuracao, admin ou risco.
6. Antes de criar o chamado, faca um resumo em linguagem simples e peca
   confirmacao explicita (ex: "Vou abrir um chamado de impressora nao
   imprime no seu nome, no setor X. Confirma?"). SO crie o chamado depois de
   um "sim" claro.
7. Depois de criar, informe o numero do chamado: "Pronto! Seu chamado e o
   numero #1234. O suporte ja recebeu."
8. Pergunte se precisa de mais alguma coisa - o usuario pode abrir mais de
   um chamado na mesma conversa.

FORMATO DA DESCRICAO DO CHAMADO (sempre nesta estrutura, HTML simples)
[ABERTO VIA ASSISTENTE VIRTUAL]

Relato do usuario:
<resumo fiel do problema nas palavras do usuario, 2-4 linhas>

Triagem:
- Tipo: Incidente | Requisicao
- Local/Setor: ...
- Equipamento/Sistema: ...
- Desde quando: ...
- Afeta mais pessoas: ...
- Possui print/evidencia: sim/nao
(inclua so os campos que se aplicam)

Aberto por: <usuario que conversou> (para: <requerente>, se for terceiro)

Titulo do chamado: maximo ~60 caracteres, especifico (ex: "Impressora do
faturamento nao imprime", nunca algo generico como "Problema impressora").

CATALOGO DE CATEGORIAS (use APENAS os IDs abaixo - nunca invente categoria)
{texto_catalogo}

Se o relato do usuario nao encaixar em nenhuma folha acima, use a categoria
mais proxima; se realmente nao houver nada parecido, use "Outros".

REGRAS DURAS (nunca quebrar)
- Nunca crie um chamado sem confirmacao explicita do usuario.
- Nunca invente uma categoria - use somente os IDs do catalogo acima.
- Nunca peca senha do usuario.
- Nao de instrucoes tecnicas alem da auto-resolucao leve descrita acima.
- Se o usuario relatar algo que nao e assunto de TI, oriente educadamente
  que este canal e so pra chamados de TI.
- Voce so tem acesso a ferramentas de leitura e criacao de chamado - nunca
  tente alterar, excluir ou reatribuir chamados existentes.{aviso_teste}{usuario_identificado}
"""


def construir_ferramentas_consulta(usuario_id: int):
    """Tools de consulta da IA (Fase 3) - rodam EM PROCESSO (nao via
    mcp-glpi), presas por closure ao usuario_id da sessao logada. REGRA
    DURA (CLAUDE-v2): o filtro por requerente e feito aqui no codigo Python,
    nunca delegado ao modelo - ele nao recebe usuario_id como parametro, so
    pode perguntar sobre chamados que ja pertencem a este usuario_id."""

    @tool(
        "consultar_meus_chamados",
        "Lista os chamados do usuario logado nesta conversa (numero, titulo "
        "e status). Use quando ele perguntar de forma geral pelos chamados "
        "dele, sem indicar um numero especifico - ex: 'cade meu chamado', "
        "'como estao meus chamados', 'tenho algum chamado aberto?'.",
        {},
    )
    async def consultar_meus_chamados(_args: dict) -> dict:
        chamados = await buscar_todos_chamados_usuario(usuario_id)
        if not chamados:
            texto = "Este usuário não tem nenhum chamado registrado no GLPI."
        else:
            linhas = [
                f"#{c['id']} - {c['titulo']} - status: {ROTULOS_STATUS.get(c['status'], 'Desconhecido')}"
                for c in chamados[:20]
            ]
            texto = "\n".join(linhas)
        return {"content": [{"type": "text", "text": texto}]}

    @tool(
        "consultar_detalhes_chamado",
        "Mostra detalhes de UM chamado especifico do usuario logado: status, "
        "tecnico responsavel (se ja tiver), data de abertura e a ultima "
        "atualizacao publica. So retorna dados se o numero informado for de "
        "um chamado deste usuario - use depois que ele mencionar um numero "
        "ou apos consultar_meus_chamados ja ter mostrado a lista.",
        {"ticket_id": int},
    )
    async def consultar_detalhes_chamado(args: dict) -> dict:
        ticket_id = int(args["ticket_id"])
        meus_chamados = await buscar_todos_chamados_usuario(usuario_id)
        if not any(c["id"] == ticket_id for c in meus_chamados):
            return {
                "content": [
                    {"type": "text", "text": "Não encontrei nenhum chamado com esse número no nome deste usuário."}
                ]
            }

        resumo = await obter_chamado_completo(ticket_id)
        tecnico = await obter_tecnico_atribuido(ticket_id)
        followups = await listar_followups_publicos(ticket_id)

        partes = [
            f"Chamado #{ticket_id} - {resumo['titulo']}",
            f"Status: {ROTULOS_STATUS.get(resumo['status'], 'Desconhecido')}",
            f"Aberto em: {resumo['data_criacao']}",
            f"Técnico responsável: {tecnico or 'ainda ninguém foi designado'}",
        ]
        if followups:
            partes.append(f"Última atualização pública do suporte: {followups[-1]['conteudo']}")
        else:
            partes.append("Ainda sem atualizações públicas do suporte.")

        return {"content": [{"type": "text", "text": "\n".join(partes)}]}

    return [consultar_meus_chamados, consultar_detalhes_chamado]


def construir_opcoes(modo_teste: bool = True, usuario: dict | None = None) -> ClaudeAgentOptions:
    modelo = os.environ.get("MODELO_AGENTE")
    if not modelo:
        raise RuntimeError(
            "MODELO_AGENTE nao definido no .env - o agente nunca deve rodar "
            "com modelo hardcoded no codigo."
        )

    env_mcp_glpi = {
        "GLPI_URL": os.environ["GLPI_URL"],
        "GLPI_APP_TOKEN": os.environ["GLPI_APP_TOKEN"],
        "GLPI_USER_TOKEN": os.environ["GLPI_USER_TOKEN"],
    }

    env_processo: dict[str, str] = {}
    if os.environ.get("ANTHROPIC_API_KEY"):
        env_processo["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]

    mcp_servers = {
        "glpi": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-glpi"],
            "env": env_mcp_glpi,
        }
    }
    if usuario is not None:
        mcp_servers["consulta"] = create_sdk_mcp_server(
            name="consulta", tools=construir_ferramentas_consulta(usuario["id"])
        )

    return ClaudeAgentOptions(
        model=modelo,
        system_prompt=montar_system_prompt(modo_teste=modo_teste, usuario=usuario),
        tools=[],  # desliga todas as tools nativas (Bash, Read, Write, WebSearch...)
        allowed_tools=FERRAMENTAS_PERMITIDAS,
        disallowed_tools=FERRAMENTAS_BLOQUEADAS,
        mcp_servers=mcp_servers,
        strict_mcp_config=True,  # ignora qualquer .mcp.json/config externo
        setting_sources=[],  # nao carregar CLAUDE.md/settings deste repo
        permission_mode="bypassPermissions",
        env=env_processo,
        include_partial_messages=True,  # necessario pro streaming token-a-token no chat web
    )


FERRAMENTAS_ACOMPANHAMENTO = [
    "mcp__consulta__consultar_meus_chamados",
    "mcp__consulta__consultar_detalhes_chamado",
    "mcp__consulta__registrar_atualizacao",
]


def construir_ferramenta_registrar_atualizacao(ticket_id: int, mensagem_atual: str):
    """Tool que so grava a mensagem ATUAL (a exata, sem parafrasear - por
    isso nao recebe parametro de texto, so um 'sim, vale a pena') como
    followup publico. A decisao de USAR ou nao fica com o modelo (ver
    system prompt de construir_opcoes_acompanhamento): so quando a mensagem
    traz informacao nova de verdade sobre o problema, nao pra "obrigado"
    ou perguntas de status."""

    @tool(
        "registrar_atualizacao",
        "Registra a mensagem ATUAL do colaborador como atualizacao no "
        f"chamado #{ticket_id} (fica visivel pro tecnico quando assumir). "
        "Use SOMENTE quando a mensagem trouxer informacao nova e relevante "
        "sobre o problema (mais sintomas, o que ja tentou, contexto novo). "
        "NAO use pra mensagens sociais ('obrigado', 'ok', 'entendi'), "
        "perguntas sobre status, ou qualquer coisa que nao ajude o tecnico "
        "a entender o problema.",
        {},
    )
    async def registrar_atualizacao(_args: dict) -> dict:
        novo_id = await criar_followup_publico(ticket_id, mensagem_atual)
        db.marcar_followup_do_usuario(ticket_id, novo_id)
        estado_db = db.obter_estado_chamado(ticket_id)
        if estado_db:
            db.atualizar_estado_chamado(
                ticket_id, ultimo_followup_id=max(estado_db["ultimo_followup_id"], novo_id)
            )
        return {"content": [{"type": "text", "text": "Registrado no chamado com sucesso."}]}

    return registrar_atualizacao


def construir_opcoes_acompanhamento(
    ticket_id: int, usuario: dict, mensagem_atual: str
) -> ClaudeAgentOptions:
    """Conversa leve pra threads de chamado JA CRIADO, enquanto ninguem do
    suporte assumiu ainda (ver app.glpi.obter_tecnico_atribuido). Depois que
    um tecnico assume, o app para de chamar isto - a partir dai vira relay
    puro (main.py), toda mensagem vira followup sem julgamento nenhum. Sem
    `glpi_create_ticket` de proposito: este modo nunca cria chamado novo, so
    conversa sobre o #{ticket_id} que ja existe. Sem servidor "glpi" (stdio)
    tambem, de proposito: nao precisa e assim conecta bem mais rapido (sem
    subir o mcp-glpi via npx)."""
    modelo = os.environ.get("MODELO_AGENTE")
    if not modelo:
        raise RuntimeError(
            "MODELO_AGENTE nao definido no .env - o agente nunca deve rodar "
            "com modelo hardcoded no codigo."
        )

    env_processo: dict[str, str] = {}
    if os.environ.get("ANTHROPIC_API_KEY"):
        env_processo["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]

    system_prompt = f"""Voce e o assistente virtual de suporte de TI da CEDEP,
acompanhando o chamado #{ticket_id} de {usuario['nome_completo']}, que JA FOI
ABERTO - voce nunca cria um chamado novo aqui, so conversa sobre este.

Ninguem do suporte assumiu esse chamado ainda. A mensagem do colaborador NAO
foi registrada automaticamente desta vez - a decisao e sua: use a ferramenta
registrar_atualizacao SOMENTE se a mensagem trouxer informacao nova e
relevante sobre o problema (mais sintomas, o que ja tentou, contexto novo
que ajude o tecnico). NAO registre coisas sociais tipo "obrigado", "ok",
"entendi", nem perguntas sobre status - so responda naturalmente. Depois de
decidir (registrar ou nao), responda de forma breve e cordial: se registrou,
confirme que anotou; se nao, so continue a conversa normalmente. Se ele
perguntar do status ou quando alguem vai atender, use a ferramenta de
consulta pra checar o estado real - nunca invente prazo ou nome de tecnico.
Fala em portugues do Brasil, linguagem simples, sem jargao de TI."""

    return ClaudeAgentOptions(
        model=modelo,
        system_prompt=system_prompt,
        tools=[],
        allowed_tools=FERRAMENTAS_ACOMPANHAMENTO,
        mcp_servers={
            "consulta": create_sdk_mcp_server(
                name="consulta",
                tools=[
                    *construir_ferramentas_consulta(usuario["id"]),
                    construir_ferramenta_registrar_atualizacao(ticket_id, mensagem_atual),
                ],
            )
        },
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="bypassPermissions",
        env=env_processo,
        include_partial_messages=True,
    )


async def aguardar_mcp_pronto(client: ClaudeSDKClient, timeout: float = 15.0) -> None:
    """Espera o mcp-glpi (subprocesso npx) terminar de conectar.

    A conexao do client retorna antes do handshake do MCP terminar; sem isso,
    a primeira mensagem do usuario pode chegar ao modelo sem nenhuma tool
    disponivel (o modelo entao text-completa uma resposta sem ter chamado
    nada de verdade).
    """
    decorrido = 0.0
    intervalo = 0.5
    while decorrido < timeout:
        status = await client.get_mcp_status()
        servidores = status.get("mcpServers", [])
        if servidores and servidores[0].get("status") == "connected":
            return
        if servidores and servidores[0].get("status") == "failed":
            raise RuntimeError(f"mcp-glpi falhou ao conectar: {servidores[0].get('error')}")
        await asyncio.sleep(intervalo)
        decorrido += intervalo
    raise TimeoutError("mcp-glpi nao conectou a tempo")


async def conversar_cli() -> None:
    opcoes = construir_opcoes(modo_teste=True)

    print("=== Teste de triagem - Assistente de TI CEDEP ===")
    print("(digite 'sair' pra encerrar)\n")

    async with ClaudeSDKClient(opcoes) as client:
        await aguardar_mcp_pronto(client)
        while True:
            mensagem = await asyncio.to_thread(input, "Voce: ")
            if mensagem.strip().lower() in {"sair", "exit", "quit"}:
                break

            await client.query(mensagem)

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for bloco in msg.content:
                        if isinstance(bloco, TextBlock):
                            print(f"Assistente: {bloco.text}")
                        elif isinstance(bloco, ToolUseBlock):
                            print(f"  [usando ferramenta: {bloco.name}]")
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        print(f"  [erro na resposta: {msg.subtype}]")


def main() -> None:
    asyncio.run(conversar_cli())


if __name__ == "__main__":
    main()
