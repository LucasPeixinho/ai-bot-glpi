#!/usr/bin/env bash
# Runbook de instalacao da Fase 7 (deploy) do chatbot-glpi.
# Idempotente: pode rodar mais de uma vez sem quebrar nada.
#
# Precisa de sudo. Rode manualmente, revisando cada bloco antes:
#   sudo bash /opt/chatbot-glpi/deploy/instalar.sh
#
# ESTADO ATUAL: o servico roda como usuario "administrator" (reaproveita o
# login pessoal do Claude Code) porque ainda nao ha ANTHROPIC_API_KEY
# dedicada no .env. Isso e temporario - ver bloco "TROCA PRO USUARIO
# DEDICADO" no final deste arquivo pra quando a chave chegar.

set -euo pipefail

PROJETO=/opt/chatbot-glpi

if [ "$(id -u)" -ne 0 ]; then
    echo "Rode com sudo: sudo bash $0" >&2
    exit 1
fi

if grep -qE '^# *ANTHROPIC_API_KEY=\s*$|^ANTHROPIC_API_KEY=\s*$' "$PROJETO/.env"; then
    echo "AVISO: ANTHROPIC_API_KEY ainda nao preenchida no .env." >&2
    echo "       Rodando mesmo assim com login pessoal (User=administrator" >&2
    echo "       no .service). Isso e temporario - ver o topo deste script." >&2
fi

echo "==> mcp-glpi global (evita depender do cache npx de um usuario especifico)"
if ! npm list -g --depth=0 2>/dev/null | grep -q mcp-glpi; then
    npm install -g mcp-glpi
else
    echo "    ja instalado, ok."
fi

echo "==> Servico systemd"
cp "$PROJETO/deploy/chatbot-glpi.service" /etc/systemd/system/chatbot-glpi.service
systemctl daemon-reload
systemctl enable --now chatbot-glpi
sleep 2
systemctl --no-pager status chatbot-glpi || true

echo "==> nginx (porta de entrada unica - separa o chat do pool prefork do Apache/GLPI)"
if ! command -v nginx >/dev/null; then
    apt-get install -y nginx
fi
ln -sf "$PROJETO/deploy/nginx-chatbot.conf" /etc/nginx/sites-available/chatbot-glpi.conf
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/chatbot-glpi.conf /etc/nginx/sites-enabled/chatbot-glpi.conf
nginx -t

echo "==> Apache: move pra 127.0.0.1:8090 (so a porta muda - mod_php/GLPI intocados)"
if grep -q "^Listen 80$" /etc/apache2/ports.conf; then
    sed -i 's/^Listen 80$/Listen 127.0.0.1:8090/' /etc/apache2/ports.conf
fi
if grep -q "<VirtualHost \*:80>" /etc/apache2/sites-available/glpi.conf; then
    sed -i 's/<VirtualHost \*:80>/<VirtualHost 127.0.0.1:8090>/' /etc/apache2/sites-available/glpi.conf
fi
if [ -L /etc/apache2/sites-enabled/chatbot-glpi.conf ]; then
    a2dissite chatbot-glpi >/dev/null
fi
apache2ctl configtest

echo "==> Cutover: Apache solta a porta 80, nginx assume"
systemctl restart apache2
systemctl enable --now nginx
systemctl reload nginx

echo
echo "==> Pronto. Verificacoes:"
echo "    journalctl -u chatbot-glpi -f                                (logs do servico)"
echo "    curl -H 'Host: suporte.cdp.lub' http://127.0.0.1/            (GLPI via nginx->Apache:8090)"
echo "    curl -H 'Host: chat.cdp.lub' http://127.0.0.1/               (chatbot via nginx->uvicorn:8100)"
echo "    curl -H 'Host: qualquercoisa.invalido' http://127.0.0.1/     (deve fechar/444 - conexao vazia, sem resposta)"
echo "    ss -tlnp | grep :80                                         (deve mostrar nginx, nao apache2)"

# ---------------------------------------------------------------------------
# TROCA PRO USUARIO DEDICADO (rodar quando a ANTHROPIC_API_KEY chegar)
# ---------------------------------------------------------------------------
# 1. Editar /opt/chatbot-glpi/.env: descomentar e preencher ANTHROPIC_API_KEY
# 2. Editar /opt/chatbot-glpi/deploy/chatbot-glpi.service: trocar
#    User=administrator / Group=administrator para User=chatbot-glpi /
#    Group=chatbot-glpi
# 3. Rodar (com sudo):
#      useradd --system --create-home --home-dir /var/lib/chatbot-glpi \
#          --shell /usr/sbin/nologin chatbot-glpi
#      usermod -aG administrator chatbot-glpi
#      chmod 640 /opt/chatbot-glpi/.env
#      chmod g+w /opt/chatbot-glpi /opt/chatbot-glpi/chatbot.db
#      bash /opt/chatbot-glpi/deploy/instalar.sh   # reaplica o .service novo
