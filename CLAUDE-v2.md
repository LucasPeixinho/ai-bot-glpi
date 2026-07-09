# Chatbot GLPI v2 — Sistema completo de abertura e acompanhamento de chamados

## Contexto

Evolução do MVP já em produção de testes em `/opt/chatbot-glpi` (ler o CLAUDE.md
original para o histórico). O MVP entregou: triagem por IA, criação de chamado com
requerente correto, relay de followups públicos, retomada de sessão e revisão visual.

A v2 transforma o MVP em produto completo: autenticação AD, múltiplos chamados por
usuário (thread por chamado), IA que também consulta chamados, confirmação de solução
pelo usuário e painel de atendimento para técnicos.

**Princípios (não negociáveis):**
1. O GLPI é a fonte da verdade. O app é uma camada de conveniência — tudo que acontece
   nele reflete no GLPI e vice-versa. Técnico que trabalhar só pelo GLPI produz o mesmo
   efeito de quem usa o app.
2. O usuário sempre sabe com quem fala: assistente (IA) ou suporte (humano). Nunca os
   dois no mesmo fluxo sem transição visual explícita.
3. Um chamado = uma conversa (thread). Estado de relay é POR THREAD, nunca por sessão.
4. Linguagem da interface e da IA: português simples, sem jargão (público leigo).

## Stack (mantida do MVP)

FastAPI + Jinja2 + HTMX + Alpine.js, claude-agent-sdk + MCP mcp-glpi, SQLite,
mesmo servidor do GLPI, Apache como reverse proxy. Modelo do agente via
`MODELO_AGENTE` no `.env`. Novidades: `ldap3` (auth AD) e SSE para tempo real.

## Autenticação — AD/LDAP (substitui o "usuário digitado" do MVP)

- Tela de login única: **usuário + senha do domínio** (mesma credencial do Windows/GLPI).
- Validação por bind LDAP direto no AD via `ldap3`:
  - Config no `.env`: `LDAP_SERVER`, `LDAP_DOMAIN` (para montar user@dominio ou
    DOMINIO\\user no bind), `LDAP_BASE_DN`.
  - Bind com a credencial informada; sucesso = autenticado. NUNCA armazenar a senha.
- Após autenticar, resolver o usuário no GLPI (`glpi_search_user` pelo login) e carregar
  os perfis via `Profile_User`:
  - Perfil na lista `PERFIS_TECNICO_IDS` (.env) → papel TÉCNICO
  - Caso contrário → papel COLABORADOR
  - Técnico pode alternar para a visão de colaborador (técnico também abre chamado).
- Sessão: cookie assinado, httponly, expiração 8h. Rate limit de login (ex: 5 tentativas
  / 5 min por usuário) com mensagem amigável.
- Falha de LDAP (servidor fora) → mensagem clara, logar o erro, nunca stack trace na tela.
- Manter a função de autenticação isolada (`autenticar(username, senha)`) para futura
  troca por SSO Kerberos sem refatorar.

## Jornada do COLABORADOR

### Tela inicial (pós-login)
- **Meus chamados**: lista de chamados ATIVOS do usuário (novo, em atendimento,
  aguardando confirmação), cada item com:
  - Título, número, status em linguagem humana ("Aguardando atendimento",
    "João do suporte está cuidando", "Resolvido — confirme se funcionou")
  - Badge de mensagem nova não lida
- Botão primário **"Preciso de ajuda"** → nova conversa com o assistente.
- **Finalizados ocultos por padrão**: chamados fechados NÃO aparecem na lista principal.
  Botão/link discreto "Ver chamados finalizados" expande a lista dos fechados
  (somente leitura: dá pra abrir e reler a conversa, mas não enviar mensagem).

### Conversa com o assistente (IA) — porta de entrada universal
O agente atende três intenções sem menu (infere da mensagem):
1. **Abrir chamado**: triagem do MVP (2-4 perguntas, linguagem leiga, confirmação
   obrigatória, formato de descrição padronizado — regras do CLAUDE.md original valem).
2. **Consultar chamados**: "cadê meu chamado da impressora?" → IA lê os chamados DO
   PRÓPRIO usuário no GLPI e responde status, técnico responsável, última movimentação,
   em linguagem simples. REGRA DURA no backend: as tools de consulta só retornam
   chamados cujo requerente é o usuário logado — o filtro é aplicado no código, nunca
   delegado ao modelo.
3. **Escalar para humano**: se o usuário pedir ("quero falar com alguém do TI"), a IA
   cria o chamado imediatamente com o que já coletou (categoria melhor possível ou
   fallback "Outros") sem forçar o resto da triagem.

### Thread do chamado (comportamento por estado)
- **Aguardando atendimento**: histórico da triagem + aviso de recebimento. Mensagem
  nova do usuário → followup público (complemento de informação). IA não responde.
- **Em atendimento** (técnico atribuído/primeiro followup humano): relay puro (já
  implementado). Divisor visual "Fulano do Suporte entrou no atendimento".
- **Resolvido — aguardando confirmação**: solução em destaque + dois botões:
  - **"Funcionou"** → fecha o chamado no GLPI (aprovação da solução). Thread vai
    para Finalizados.
  - **"Ainda não resolveu"** → reabre no GLPI com followup do usuário explicando
    (campo de texto obrigatório, mínimo ~10 caracteres). Volta a "Em atendimento".
- **Fechado**: somente leitura, listado em Finalizados.

### Confirmação com prazo de 48h
- Chamado resolvido sem confirmação do usuário em **48 horas** → considerado
  resolvido: fecha e vai para Finalizados automaticamente.
- Implementação preferida: usar a configuração NATIVA do GLPI de fechamento
  automático de chamados solucionados (Entidade → Assistência → "Fechamento
  automático") ajustada para 2 dias — o app apenas reflete o status. Confirmar com o
  Lucas se pode alterar essa config da entidade; se não puder, implementar um job
  interno (verificação periódica a cada hora + na carga da sessão) que fecha via API
  chamados resolvidos há mais de 48h que foram abertos pelo app.
- Enquanto aguarda confirmação, o thread mostra aviso do prazo ("Se não houver
  resposta até <data>, o chamado será fechado automaticamente").

## Jornada do TÉCNICO (painel de suporte)

### Painel (pós-login com papel técnico)
Duas listas:
- **Fila**: chamados SEM atribuição — TODOS os chamados do GLPI da entidade, abertos
  pelo bot ou não (o app é um mini-frontend de atendimento). Colunas: número, título,
  categoria, requerente, idade do chamado, origem (ícone indicando "via assistente"
  quando criado pelo bot).
- **Meus atendimentos**: chamados atribuídos ao técnico logado, com badge de mensagem
  nova do usuário. Ordenar por última atividade.
- Alternador visível "Ver como colaborador" (e volta).

### Thread do chamado (visão técnico)
- Vê tudo: triagem da IA, followups públicos E privados (privados com estilo visual
  distinto de "nota interna").
- Ações:
  - **Assumir** → atribui a si no GLPI (chamado sai da Fila, entra em Meus atendimentos)
  - **Responder** → followup PÚBLICO (aparece no chat do colaborador)
  - **Nota interna** → followup PRIVADO (NUNCA aparece para o colaborador)
  - **Resolver** → registra solução no GLPI (campo obrigatório), dispara o fluxo de
    confirmação do colaborador
- Chamados não abertos pelo bot também abrem como thread: a "conversa" é reconstruída
  a partir da descrição + followups do GLPI.

## Estados do chamado (modelo do app)

```
Triagem (IA) → Aguardando atendimento → Em atendimento → Resolvido (aguard. confirmação) → Fechado
                                              ↑                    |
                                              └── "Não resolveu" ──┘
```
Cada transição gera evento visual (divisor de timeline) na conversa.
Mapear estados do GLPI: novo(1)/atribuído(2)=aguardando ou em atendimento conforme
atribuição, em andamento(2-3), pendente(4)=em atendimento com aviso, solucionado(5)=
aguardando confirmação, fechado(6)=finalizado.

## Tempo real

- Substituir o polling HTMX por **SSE** (Server-Sent Events) do FastAPI: um stream por
  sessão que empurra novos followups, mudanças de status e badges.
- Internamente o backend continua consultando o GLPI por mudanças (poll de 10-15s
  centralizado no servidor, uma varredura para todas as sessões ativas — não um poll
  por aba aberta), e distribui via SSE.
- Fallback: se SSE cair, HTMX polling como reserva (degradação suave).
- Apache: garantir configuração para SSE na rota (sem buffering).

## Segurança (regras duras, aplicadas no backend)

- Colaborador só lê/escreve nos chamados em que é requerente.
- Followups privados jamais serializados em resposta para papel colaborador (filtrar
  no servidor, não no template).
- Ações de técnico (assumir, resolver, nota interna, fila) exigem papel técnico
  verificado na sessão — nunca confiar em parâmetro do cliente.
- IA sem tools destrutivas; tools de consulta com filtro de requerente injetado pelo
  código.
- Senha do AD: usada só no bind, nunca logada, nunca persistida.
- Logs de auditoria: login, criação de chamado, ações de técnico (arquivo de log
  rotacionado).

## Configuração (.env — adições)

```
LDAP_SERVER=ldap://ip-ou-host-do-dc
LDAP_DOMAIN=DOMINIO            # ou dominio.local, conforme formato de bind
LDAP_BASE_DN=DC=dominio,DC=local
PERFIS_TECNICO_IDS=x,y         # IDs dos perfis GLPI que dão papel técnico
HORAS_CONFIRMACAO_SOLUCAO=48
SECRET_KEY_SESSAO=...          # cookie assinado
```

## Fases de execução (uma por vez, aprovação do Lucas entre fases)

1. **Threads múltiplos**: refatorar sessão/estado para conversa-por-chamado
   (relay por thread), tela "Meus chamados" com badges, botão "Preciso de ajuda",
   Finalizados ocultos com botão. É a base de todo o resto.
2. **Auth AD**: login LDAP + papéis (colaborador/técnico) + sessão por cookie +
   rate limit. Testar com usuário real do domínio e com senha errada.
3. **IA consulta chamados**: tools de leitura filtradas por requerente + intenção de
   consulta no system prompt + escalar para humano.
4. **Confirmação de solução**: botões Funcionou/Não resolveu, reabertura com motivo,
   fechamento automático em 48h (nativo GLPI se autorizado; senão job interno),
   avisos de prazo.
5. **SSE**: substituir polling por stream com poll centralizado no backend + fallback.
6. **Painel do técnico**: fila completa, meus atendimentos, assumir/responder/nota
   interna/resolver, thread de chamados não-bot. (Por último, conforme decisão.)
7. **Deploy**: systemd + Apache (incluindo config SSE) + smoke test completo nos dois
   papéis + troca para API key dedicada da Anthropic (pré-requisito de produção).

Regras de trabalho: mesmas do CLAUDE.md original (simplicidade, pt-BR nos
identificadores sem acento, chamados de teste com prefixo "TESTE BOT", confirmar
antes de mudanças fora de /opt/chatbot-glpi, mostrar plano antes de refatorações
grandes).

## Fora de escopo da v2

- Upload de anexos no chat (candidato à v2.1 — primeiro da fila)
- SSO Kerberos sem senha (v3; a auth está isolada para isso)
- Dashboard/relatórios para gestão do TI (GLPI já tem)
- Notificações push, WhatsApp, Teams
- Base de conhecimento como fonte de auto-resolução da IA
```
