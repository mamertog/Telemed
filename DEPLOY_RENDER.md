# Publicar na Render

1. Extraia o pacote e envie seu conteudo a um repositorio privado GitHub ou GitLab. O arquivo render.yaml deve estar na raiz do repositorio.
2. Em https://dashboard.render.com selecione New > Blueprint e conecte o repositorio.
3. Revise os planos pagos. O servico medico-lembretes e opcional: remova esse bloco do render.yaml antes de implantar se nao desejar lembretes.
4. PUBLIC_BASE_URL: informe a URL publica escolhida para o site. E-mail esta desativado; EMAIL_API_KEY e EMAIL_FROM podem ficar vazios.
5. Confirme a criacao dos recursos e aguarde o deploy. A migracao cria tabelas e a configuracao inicial ativa consultas de 30 minutos de segunda a sabado, 13h-20h. Deploys posteriores preservam a agenda existente.
6. No Shell do servico medico-web execute: flask --app wsgi create-admin --role medica
7. Acesse /acesso/entrar e configure o autenticador. Em Configuracoes > Foto, envie a fotografia autorizada.
8. Confira /health e /agendar/. Ajuste PUBLIC_BASE_URL para a URL final do servico.

O pacote nao inclui .env, ambiente virtual, banco local nem dados de pacientes. O banco da Render inicia vazio. Nao envie esses arquivos ao Git.
