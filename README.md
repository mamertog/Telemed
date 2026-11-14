# Site médico — Dra. Juliana Gonçalves Rosa

Aplicação Flask com site institucional, agendamento online, cadastro administrativo de pacientes e painel restrito, usando **PostgreSQL** e pronta para a **Render**.

> Este sistema **não** é prontuário eletrônico: não armazena diagnósticos, evoluções, prescrições ou exames. Não declare conformidade plena com a LGPD ou certificação para uso médico sem validação técnica e jurídica.

## Estrutura

```
app/
  __init__.py          fábrica, cabeçalhos de segurança, timeout de sessão, /health
  config.py            configurações por ambiente (lidas de variáveis de ambiente)
  models.py            modelos SQLAlchemy (patients, users, services, appointments, ...)
  security.py          Argon2id, cifragem do segredo TOTP, permissões, auditoria
  forms.py             formulários Flask-WTF (CSRF + validação no servidor)
  cli.py               comandos: create-admin, seed-defaults, send-reminders, ...
  services/
    scheduling.py      disponibilidade e reserva transacional (anti-conflito)
    notifications.py   e-mail via API (Resend/Brevo), lembretes
    settings.py        configurações editáveis persistidas no banco
  blueprints/          public, booking (/agendar), auth (/acesso), admin (/painel)
  templates/  static/
migrations/            Alembic (Flask-Migrate)
tests/                 Pytest com PostgreSQL real
wsgi.py  render.yaml  requirements.txt  .env.example
```

## Rodando localmente (VS Code)

Pré-requisitos: Python 3.12+ e PostgreSQL 14+ (ou Docker).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt

# Banco local (opção Docker): cria medico_dev e medico_test
docker compose up -d
# Sem Docker: crie o usuário "medico" e os bancos "medico_dev" e "medico_test" no seu PostgreSQL.

cp .env.example .env            # e ajuste SECRET_KEY e DATABASE_URL
flask --app wsgi db upgrade     # cria as tabelas
flask --app wsgi seed-defaults  # cadastra os tipos de atendimento sugeridos (inativos)
flask --app wsgi create-admin --role medica   # pede e-mail, nome e senha (oculta)
flask --app wsgi run --debug
```

No VS Code, use **Executar e Depurar → "Flask (desenvolvimento)"** (arquivo `.vscode/launch.json`). Acesse `http://127.0.0.1:5000` e o painel em `/acesso/entrar`. No primeiro login, o sistema exige configurar o MFA (QR code para Google Authenticator, Microsoft Authenticator etc.).

### Primeiros passos no painel
1. **Atendimentos**: revise descrições, durações e valores e ative os que serão oferecidos.
2. **Disponibilidade**: cadastre dias e horários (com intervalo de almoço, se houver).
3. **Configurações → Contato**: endereço, telefone, WhatsApp, e-mail, horários, link do mapa.
4. **Configurações → Foto**: fotografia profissional autorizada (armazenada no banco).
5. **Usuários**: crie a conta da secretaria.

Enquanto não houver atendimento ativo e disponibilidade, a página de agendamento informa que a agenda está em preparação. Campos de contato vazios não são exibidos.

## Testes

```bash
pytest
```
Usa `TEST_DATABASE_URL` (o esquema desse banco é **apagado** a cada execução). Cobertura: páginas e navegação, calendário e horários livres, persistência no PostgreSQL, exibição no painel, confirmação/cancelamento, reagendamento, bloqueio de conflitos, **reserva simultânea (8 threads → 1 reserva)**, restrição de exclusão no banco, cadastro e busca de pacientes, login/logout/MFA, bloqueio por tentativas, proteção de todas as rotas do painel, isolamento de papéis, CSRF, migrações em dia e configuração de produção. Os dados de teste são fictícios.

## Implantação na Render

### Opção A — Blueprint (recomendada)
1. Envie o projeto para um repositório GitHub/GitLab (o `.gitignore` já exclui `.env`).
2. Render → **New → Blueprint** → selecione o repositório. O `render.yaml` cria:
   - `medico-db` (PostgreSQL 16 gerenciado, plano pago com backups);
   - `medico-web` (Web Service: `pip install`, migrações no *pre-deploy*, Gunicorn, health check em `/health`);
   - `medico-lembretes` (Cron Job opcional de lembretes — remova do arquivo se não quiser).
3. Informe as variáveis marcadas como `sync: false` (`PUBLIC_BASE_URL`, e-mail). `SECRET_KEY` é gerada automaticamente.
4. Após o primeiro deploy, abra o **Shell** do serviço `medico-web` e crie a primeira conta:
   ```bash
   flask --app wsgi create-admin --role medica
   ```
5. Acesse `https://<seu-servico>.onrender.com/acesso/entrar`, configure o MFA e siga os “Primeiros passos”.
6. Domínio próprio: Settings → Custom Domains (o HTTPS é emitido pela Render). Atualize `PUBLIC_BASE_URL`.

### Opção B — manual
1. Crie um **PostgreSQL** e copie a *Internal Database URL*.
2. Crie um **Web Service** (Python):
   - Build: `pip install -r requirements.txt`
   - Pre-deploy: `flask --app wsgi db upgrade && flask --app wsgi seed-defaults`
   - Start: `gunicorn wsgi:app --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT`
   - Health check: `/health`
3. Variáveis: `APP_ENV=production`, `SECRET_KEY` (≥ 32 caracteres aleatórios), `DATABASE_URL`, `PYTHON_VERSION=3.12.8`, `PUBLIC_BASE_URL` e, se usar e-mail, `EMAIL_PROVIDER`, `EMAIL_API_KEY`, `EMAIL_FROM`.
4. Crie a primeira conta pelo Shell (passo 4 acima).

### Verificação pós-deploy
- `GET /health` → `{"status": "ok"}`.
- Faça um agendamento de teste pelo site, confira-o em **Painel → Agendamentos**, reinicie o serviço e confirme que ele continua lá (persistência no PostgreSQL). Depois cancele-o.

## E-mail transacional (opcional)
Sem configuração (`EMAIL_PROVIDER=none`), tudo funciona; apenas não há envio. Para ativar:
- **Resend**: `EMAIL_PROVIDER=resend`, `EMAIL_API_KEY=re_...`, `EMAIL_FROM=agenda@seudominio.com.br` (domínio verificado).
- **Brevo**: `EMAIL_PROVIDER=brevo`, `EMAIL_API_KEY=...`, `EMAIL_FROM=` remetente validado.

Mensagens: solicitação recebida, confirmação, cancelamento, reagendamento e lembrete (modelos editáveis no painel, sem dados clínicos). Cada e-mail traz um link de acompanhamento novo (o anterior expira). Também habilita a redefinição de senha por e-mail; sem e-mail, um gestor gera o link em **Usuários → Gerenciar**.

## Segurança — decisões adotadas
- **Conflitos de agenda**: validação no servidor + `pg_advisory_xact_lock` + restrição `EXCLUDE USING gist (tstzrange(start_at, end_at) WITH &&)` para agendamentos ativos.
- **Fuso horário**: instantes em `TIMESTAMPTZ`; exibição e regras em `America/Sao_Paulo`.
- **Autenticação**: Argon2id, MFA TOTP obrigatório para todas as contas internas (segredo cifrado, sem reuso de código), bloqueio após 5 falhas, mensagens genéricas (anti-enumeração), sessão com expiração por inatividade (30 min) e invalidação ao trocar senha/desativar conta.
- **Autorização**: permissões verificadas no servidor em cada rota; todo `/painel` exige login completo. O administrador técnico não acessa pacientes/agenda e só gerencia contas técnicas.
- **Web**: CSRF em todos os formulários, CSP restritiva (sem scripts/estilos inline, sem CDN), HSTS, `X-Frame-Options`, cookies `Secure/HttpOnly/SameSite`, `no-store` e `noindex` no painel, rate limiting em login e formulários públicos, honeypot anti-robô.
- **Dados**: formulário público sem CPF e sem campos clínicos; associação de pacientes por telefone + nome normalizados, sem sobrescrever cadastros; auditoria sem conteúdo clínico e IP apenas como hash; foto da médica no banco (disco do Render é efêmero), com metadados removidos.
- **Rate limit**: armazenado em memória por processo (sem Redis, conforme o escopo). É uma camada adicional ao bloqueio de conta, que fica no banco.

## Comandos administrativos
| Comando | Função |
|---|---|
| `flask --app wsgi create-admin --role medica\|secretaria\|admin_tecnico` | cria conta interna (senha oculta) |
| `flask --app wsgi reset-password --email ...` | redefine senha (recuperação) |
| `flask --app wsgi reset-mfa --email ...` | remove o MFA para nova configuração |
| `flask --app wsgi seed-defaults` | atendimentos sugeridos, inativos (idempotente) |
| `flask --app wsgi send-reminders` | envia lembretes (Cron Job) |
| `flask --app wsgi anonymize-inactive [--execute]` | política de retenção (simula por padrão) |
| `flask --app wsgi check-db` | testa a conexão com o PostgreSQL |

## Backup e restauração
- Planos pagos do PostgreSQL da Render incluem backups e recuperação a um ponto no tempo (PITR): Dashboard → banco → **Recovery**.
- Backup manual adicional (guarde em local seguro e criptografado):
  ```bash
  pg_dump --format=custom --no-owner "$DATABASE_URL" > backup-$(date +%F).dump
  pg_restore --clean --no-owner --dbname "$DATABASE_URL_DESTINO" backup-AAAA-MM-DD.dump
  ```
- Teste a restauração periodicamente em um banco separado.

## Pendências que dependem da médica / de terceiros
- Endereço, telefone, WhatsApp, e-mail, horários, link do mapa e modalidades (painel → Configurações).
- Fotografia profissional autorizada.
- Ativação, descrição, duração e valores dos atendimentos.
- RQE: só preencher e confirmar após o registro no CRM (a partir daí o site passa a exibi-lo).
- Revisão jurídica da Política de Privacidade e dos Termos de Uso (textos-modelo revisáveis).
- Conta no provedor de e-mail e domínio verificado (opcional).
