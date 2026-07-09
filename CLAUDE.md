# Chatbot GLPI — Abertura de chamados com IA

## O que é este projeto

Web app de chat onde colaboradores da CEDEP abrem chamados de TI conversando com uma IA.
A IA faz a triagem em linguagem simples, identifica a categoria ITIL correta e cria o
chamado no GLPI com o colaborador como requerente. O suporte atende pelo próprio GLPI;
as respostas do técnico (followups públicos) aparecem no chat do colaborador.

**Público:** usuários com pouco conhecimento técnico. Toda interação da IA deve ser em
português simples, sem jargão de TI, sem termos como "categoria ITIL", "requisição" ou
"incidente" nas perguntas ao usuário.

**Papel da IA:** triagem inicial leve — rotear o chamado pra categoria certa e coletar
contexto mínimo pro técnico assumir. NÃO é interrogatório: 2 a 4 perguntas no máximo
antes de criar o chamado. O técnico aprofunda depois.

## Stack e ambiente (JÁ INSTALADO E VALIDADO — não reinstalar)

- Servidor: Ubuntu 22.04, mesmo host do GLPI (`cedep-pbi01`), Apache 2.4
- Diretório do projeto: `/opt/chatbot-glpi`
- Python 3.10 + venv em `/opt/chatbot-glpi/.venv` com: fastapi, uvicorn[standard],
  claude-agent-sdk, pandas, openpyxl, python-dotenv, jinja2
- Node 22 (para o MCP)
- MCP `mcp-glpi` já registrado no Claude Code e VALIDADO contra o GLPI real:
  - `GLPI_URL` = URL base SEM `/apirest.php` (o MCP concatena sozinho)
  - Tools testadas com sucesso: listar categorias ITIL, `glpi_search_user`,
    criação de chamado COM requerente customizado (`_users_id_requester`)
- Backend fala com GLPI via localhost. IA via API Anthropic (Agent SDK).

## Arquitetura

```
[Colaborador] ⇄ navegador ⇄ Apache (reverse proxy /chat) ⇄ FastAPI (127.0.0.1:8100)
                                                              ├─ Agent SDK (Claude) ⇄ MCP mcp-glpi ⇄ GLPI API (localhost)
                                                              └─ Polling de followups ⇄ GLPI API
[Técnico] ⇄ GLPI normal (nada muda pra ele)
```

- Frontend: Jinja2 + HTMX + Alpine.js (mesmo padrão do app-sac-gestao). Sem framework JS pesado.
- Sessões de chat em memória + SQLite para persistência (sessão ⇄ ticket_id ⇄ usuário),
  suficiente pro MVP. Nada de Postgres/Redis agora.
- O agente roda server-side via `claude-agent-sdk` com o MCP `mcp-glpi` como stdio server.
  Config do MCP (env vars) vem do `.env` do projeto.

## Identificação do colaborador (MVP)

1. Tela inicial pede o **usuário de rede** (o mesmo do computador/GLPI).
2. Backend valida via `glpi_search_user`: existe + ativo. Mostra "Você é **Fulano de Tal**?"
   para confirmação.
3. O ID retornado vira o `_users_id_requester` de qualquer chamado criado na sessão.
4. Se o usuário não for encontrado: mensagem amigável orientando a verificar o nome de
   usuário usado pra entrar no Windows, com opção de tentar de novo.
5. NÃO há senha no MVP. Isolar a resolução em `resolver_usuario(username)` — vai ser
   trocada por SSO Kerberos (header REMOTE_USER do Apache) na v1.1 sem tocar no resto.

**Chamado para terceiro:** os formulários atuais permitem abrir chamado "para outro
usuário". Manter: a IA pergunta naturalmente se o problema é da própria pessoa ou de um
colega; se for colega, pede o nome, resolve via `glpi_search_user` e confirma. O colega
vira requerente; quem abriu fica registrado na descrição.

## Triagem — como a IA conversa

Fluxo conversacional (não é formulário com passos rígidos; a IA adapta):

1. **Abertura:** "Oi, [nome]! Me conta o que está acontecendo ou o que você precisa."
2. **Classificação silenciosa:** a partir do relato, a IA infere:
   - Tipo: incidente (algo quebrou/parou) vs requisição (pedido de algo novo/acesso).
     NUNCA perguntar "é incidente ou requisição?" — inferir do relato.
   - Área e categoria ITIL (ver catálogo abaixo).
3. **Perguntas de triagem (2-4, linguagem leiga):** só o que o técnico precisa pra começar:
   - Onde a pessoa está (setor/local) quando relevante
   - Identificação do equipamento/sistema (ex: "tem alguma etiqueta ou número no
     equipamento?", "qual sistema você estava usando — Winthor, RM...?")
   - Desde quando / afeta só ela ou mais gente (ajuda o técnico a priorizar)
   - Se a categoria ficou ambígua entre 2-3 opções, perguntar em termos simples
     ("o problema é na impressora em si ou no computador que manda imprimir?")
4. **Prints/evidências:** SEMPRE incentivar quando fizer sentido: "Se puder, tira um
   print ou foto do erro — ajuda muito o pessoal do suporte." No MVP não há upload no
   chat; registrar na descrição se o usuário informou que tem print e orientar que o
   técnico vai solicitar (ou que pode responder o e-mail de notificação do GLPI
   anexando). Upload no chat é a primeira melhoria pós-MVP.
5. **Confirmação obrigatória antes de criar:** resumo em linguagem simples
   ("Vou abrir um chamado de *impressora não imprime* no seu nome, no setor X. Confirma?").
   Só criar após o "sim".
6. **Criação e retorno:** criar via MCP com categoria, tipo, título curto e objetivo,
   descrição estruturada (formato abaixo) e requerente correto. Informar o número:
   "Pronto! Seu chamado é o **#1234**. O suporte já recebeu."

**Auto-resolução leve (permitida, com limite):** para problemas triviais e seguros
(ex: "já tentou reiniciar?", caps lock na senha, cabo do monitor), a IA pode sugerir UMA
verificação rápida antes de abrir o chamado. Se não resolver ou o usuário não quiser,
abre o chamado sem insistir. Nunca sugerir nada que envolva configuração, admin ou risco.

## Formato da descrição do chamado (padrão pro TI)

O corpo do chamado criado segue sempre esta estrutura (HTML simples aceito pelo GLPI):

```
[ABERTO VIA ASSISTENTE VIRTUAL]

Relato do usuário:
<resumo fiel do problema nas palavras da IA, 2-4 linhas>

Triagem:
- Tipo: Incidente | Requisição
- Local/Setor: ...
- Equipamento/Sistema: ...
- Desde quando: ...
- Afeta mais pessoas: ...
- Possui print/evidência: sim/não
<incluir só os campos que se aplicam>

Aberto por: <usuário logado no chat> (para: <requerente>, se terceiro)
```

Título do chamado: máx ~60 caracteres, específico ("Impressora do faturamento não
imprime" e não "Problema impressora").

## Catálogo de áreas e categorias

Os formulários Formcreator atuais (exports em `docs/formcreator/`) seguem todos o mesmo
padrão e servem de REFERÊNCIA, não de limite. As áreas hoje:

- **Comunicação e E-mail** (árvore ITIL raiz 172): Email/Outlook (173), Telefonia (178)
- **Equipamentos** (raiz 184): Impressoras (185), Computador (191), Periféricos (196),
  Relógio de Ponto (199), Celulares/Tablet (203), Coletores (208)
- **Monitoramento** (raiz 213): CCTV (214)
- **Sistemas e Aplicações** (raiz 216): Winthor (217), Portal Vendas (226), SPED (233),
  GLPI (237), Sascar (242), Exactus (248), RM (253), Rede (260), Sitef (264),
  Fusion (271), Íres (278), Mais Dados (285)
- **Outras solicitações**: fallback sem categoria específica

**IMPORTANTE — na Fase 1, gerar o catálogo real a partir do GLPI vivo**, não dos exports:
usar o MCP para listar a árvore completa de categorias ITIL (com IDs e flags de
incidente/requisição) e salvar em `app/catalogo_categorias.json`. Esse JSON entra no
system prompt do agente. Os IDs acima são as raízes conhecidas; as folhas devem vir do
GLPI. Se o usuário relatar algo que não encaixa em nenhuma folha, usar a categoria mais
próxima ou o fallback "Outras" — nunca travar a conversa por falta de categoria.

## Handoff pro técnico (relay)

- Após criar o chamado, a sessão entra em modo "acompanhamento":
  - Polling a cada 15s (só com sessão aberta): novos followups PÚBLICOS
    (`is_private = 0`) e mudanças de status/atribuição do chamado.
  - Followup público do técnico → aparece no chat como "Suporte — <nome>: ...".
  - A partir do primeiro followup de técnico, a IA SILENCIA: mensagens do usuário viram
    followups no chamado (relay puro), sem resposta da IA.
  - Followups privados NUNCA aparecem no chat.
- Status resolvido/fechado → chat mostra a solução e agradece.
- Reabertura de sessão: usuário se identifica de novo → backend busca chamados
  abertos/recentes dele → oferece retomar o acompanhamento ou abrir chamado novo.
- O usuário pode abrir mais de um chamado na mesma sessão (a IA pergunta "precisa de
  mais alguma coisa?" após criar).

## System prompt do agente (diretrizes de conteúdo)

Ao construir o system prompt do agente de triagem, incluir:
- Persona: assistente do suporte de TI da CEDEP, tom cordial e direto, pt-BR.
- Catálogo de categorias (JSON gerado na Fase 1).
- Regras de triagem e formato de descrição desta spec.
- Regras duras: nunca criar chamado sem confirmação explícita; nunca inventar categoria
  (usar só IDs do catálogo); nunca pedir senha do usuário; não dar instruções técnicas
  além da auto-resolução leve; se o usuário relatar algo fora de TI, orientar educadamente
  que este canal é só para chamados de TI.
- O agente NÃO deve ter acesso a tools destrutivas do MCP: restringir allowed tools ao
  mínimo (criar chamado, listar categorias, buscar usuário, followups, ler chamado).
- Modelo: ler de MODELO_AGENTE no .env (padrão claude-sonnet-4-6). O agente.py NUNCA
  deve ter modelo hardcoded — a troca (ex: teste com Haiku) é só editar .env e reiniciar.

## Configuração

`.env` em `/opt/chatbot-glpi/.env` (chmod 600, NUNCA commitar):
```
ANTHROPIC_API_KEY=...
GLPI_URL=http://localhost/glpi        # base SEM /apirest.php
GLPI_APP_TOKEN=...
GLPI_USER_TOKEN=...
PERFIS_TECNICO_IDS=...                # para detectar followup de técnico, se necessário
MODELO_AGENTE=claude-sonnet-4-6      # modelo do agente de triagem (trocável sem deploy)
```

## Estrutura de projeto sugerida

```
/opt/chatbot-glpi/
├── CLAUDE.md
├── .env
├── docs/formcreator/          # exports JSON (referência)
├── app/
│   ├── main.py                # FastAPI: rotas, sessões
│   ├── agente.py              # Agent SDK + system prompt + allowed tools
│   ├── glpi.py                # funções diretas de API quando o MCP não cobrir
│   ├── usuarios.py            # resolver_usuario(username)
│   ├── relay.py               # polling de followups / modo acompanhamento
│   ├── catalogo_categorias.json
│   ├── templates/             # Jinja2
│   └── static/
├── deploy/
│   ├── chatbot-glpi.service   # systemd (uvicorn 127.0.0.1:8100, user dedicado)
│   └── apache-chat.conf       # ProxyPass /chat + flushpackets=on p/ streaming
└── tests/
```

## Fases de execução (uma por vez, com aprovação do Lucas entre fases)

1. **Catálogo:** extrair árvore ITIL completa do GLPI via MCP → `catalogo_categorias.json`.
   Mostrar o resultado pro Lucas validar antes de seguir.
2. **Core do agente:** `agente.py` com system prompt + teste em linha de comando
   (conversa de triagem → criação de chamado real de teste, título prefixado "TESTE BOT").
3. **Web app:** FastAPI + templates + identificação de usuário + chat com streaming.
4. **Relay:** polling de followups, modo acompanhamento, retomada de sessão.
5. **Deploy:** systemd + Apache + smoke test de ponta a ponta.

Regras de trabalho: código simples e direto, sem over-engineering; nomes em pt-BR sem
acentos em identificadores; confirmar com o Lucas antes de qualquer mudança fora de
`/opt/chatbot-glpi`; chamados de teste sempre com prefixo "TESTE BOT" no título.

## Fora do escopo do MVP (não implementar agora)

- Upload de anexos no chat (primeira melhoria pós-MVP)
- SSO Kerberos / senha (v1.1 — só manter `resolver_usuario` isolada)
- Painel próprio para técnicos (técnico usa o GLPI)
- Submissão via Formcreator/FormAnswer (chamados são nativos — Rota A)
- Base de conhecimento como fonte de auto-resolução
- Notificações push / WhatsApp / Teams
