# SoWork Inspector

Bot independente em Python para observar presença no SoWork, registrar entradas/saídas, calcular horas por dia e emitir notificações HTTP opcionais. O código de rastreamento, armazenamento, relatórios, bloqueio de processo e fila de entrega foi adaptado do bot do Spot; este projeto não importa nem executa aquele repositório.

## Instalação e configuração

Requer Linux/macOS, Python 3.10+ e Google Chrome. O bloqueio de processos usa `fcntl`. O Selenium Manager resolve o ChromeDriver; em ambientes sem download, configure `SOWORK_CHROMEDRIVER` com um driver compatível com seu Chrome.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edite `.env`:

- `SOWORK_APP_URL`: URL completa do seu escritório, por exemplo `https://app.sowork.com/s/SEU_GROUP_ID/SEU_ANDAR`. Usar a raiz também funciona se você entrar no escritório na janela aberta.
- `SOWORK_GROUP_ID`: identificador depois de `/s/` na URL. Se vazio, é identificado a partir da consulta de salas feita pelo próprio aplicativo. Defina-o para impedir coleta acidental de outro escritório.
- `SOWORK_TIME_ZONE`: fuso IANA dos relatórios, padrão `America/Sao_Paulo`.
- `SOWORK_POLL_SECONDS`: intervalo de leitura dos dados observados no navegador, padrão 60, mínimo 10 segundos. Não gera consultas HTTP.
- `SOWORK_HEADLESS`: `true` para coleta sem janela; o comando `login` sempre abre uma janela.
- `SOWORK_PROFILE_DIR`: perfil exclusivo deste bot, padrão `profiles/sowork`.
- `SOWORK_OUTPUT_DIR`: diretório dos dados locais, padrão `runtime`.
- `SOWORK_LOGIN_TIMEOUT`: espera pelo escritório, padrão 300 segundos.
- `SOWORK_WEBHOOK_URL`: endpoint opcional para notificações. Vazio desativa o envio.
- `SOWORK_CHROME_BINARY` e `SOWORK_CHROMEDRIVER`: caminhos opcionais para instalações locais.

Variáveis do processo têm precedência sobre `.env`. Não use o perfil do Spot. O bot cria um perfil novo e você faz login diretamente no SoWork; ele não extrai autenticação do HAR. Em servidor sem ambiente gráfico, faça o primeiro login com acesso a uma sessão gráfica nesse mesmo ambiente.

## Uso

```bash
python3 main.py login
python3 main.py collect-once
python3 main.py run
python3 main.py report
python3 main.py report --day 2026-10-13
```

No primeiro `login`, autentique-se. O bot clica automaticamente no botão **Enter Office** quando ele estiver visível e habilitado, inclusive nas próximas inicializações headless. A coleta só começa após receber dados em uma conexão Gaia ativa do escritório e receber documentos de membros completos e sincronizados pelo Firestore. Uma consulta de salas na tela de entrada, sozinha, não confirma presença. Se a sessão expirar, execute `login` novamente. Não é necessário configurar senha ou token em `.env`.

`collect-once` registra uma observação. `run` mantém a coleta até Ctrl+C. Dois processos não podem usar simultaneamente o mesmo perfil nem o mesmo diretório de saída. Se mudar de escritório, use outro diretório de saída.

## Como a presença é lida

O HAR fornecido contém documentos Firestore `group_membermaps/<groupId>_<partição>` com `isOnline`, `userName` e `roomDefinitionId`, além de `POST https://api.sowork.com/api/v1/gaia/rooms` com corpo `{"groupId":"..."}`. A resposta desta API lista todos os andares/salas em `roomDefinitionMap`, com `userIds` e `userCount`.

Na captura, os 35 usuários com `isOnline=true` coincidem exatamente com os 35 IDs presentes nas salas. A coleta é passiva: lê as respostas Firestore que o próprio aplicativo recebe por XHR/fetch, sem enviar consultas extras nem capturar autenticação. Cada documento `group_membermaps` atualiza sua partição de membros, usando `isOnline` para presença e `userName` para o nome.

A coleta exige conexão Gaia ativa, sincronização `CURRENT` das partições e confirmações `readTime` recentes. A lista precisa conter todos os documentos de membros assinados pelo próprio aplicativo, com seus respectivos targets sincronizados; respostas incompletas, membros duplicados, mudanças ainda não confirmadas e dados antigos suspendem a coleta. `groups.memberCount` é apenas metadado: foi observada divergência entre esse contador e os documentos sincronizados, por isso ele não bloqueia a coleta. A tolerância de atualização é duas vezes `SOWORK_POLL_SECONDS` (mínimo 30 segundos). O horário registrado corresponde à confirmação recebida do fluxo, não à leitura posterior do cache. Os frames binários Gaia não são interpretados como presença. A frequência real das confirmações e eventuais mudanças de esquema precisam ser validadas na sessão do usuário.

A presença indica conexão com o escritório; não mede atividade, produtividade nem ausência do teclado. `status=busy` não representa offline. A API é interna e seu formato pode mudar. A equivalência foi confirmada nesta captura; usuários em outros modos/clientes, como mobile, exigem nova validação. O WebSocket Gaia tem frames binários que não são interpretados para presença; seus eventos de conexão, mensagem e desconexão confirmam se o navegador entrou no escritório.

Respostas HTTP com erro, de outro escritório ou com estrutura/contagens inválidas são rejeitadas. Uma lista vazia de definições também é rejeitada por não permitir confirmar a completude; salas válidas sem usuários representam um escritório vazio. Falhas de coleta não geram saídas. Eventos curtos entre duas consultas podem não ser observados. Na retomada e em falhas, sessões de horas são encerradas na última observação válida, excluindo o intervalo sem observação.

## Histórico e relatórios

O diretório `runtime/` contém:

- `events/YYYY-MM-DD.jsonl`: observações de presença e eventos de sessão/sala.
- `snapshots/YYYY-MM-DD/`: snapshots com horário UTC.
- `state.json`: estado persistido do rastreador.
- `daily_hours.json`: totais por usuário/dia, incluindo a projeção até a última observação.
- `reports/YYYY-MM-DD.json`: nome, ID, email (vazio quando não fornecido), horas e primeiro horário observado.
- `notifications.sqlite`: estado e fila de notificações, quando habilitadas.

Horas são divididas à meia-noite no fuso configurado. `report` reconstrói os relatórios a partir dos eventos; também aceita `--events-file CAMINHO`. Os horários representam observações do coletor. Não são registros de presença anteriores ao início do bot.

## Notificações

Ao configurar `SOWORK_WEBHOOK_URL`, cada nova presença gera um POST `entry`; a saída é confirmada após dois snapshots válidos consecutivos sem o usuário. Uma falha de coleta reinicia essa confirmação. Na primeira coleta, usuários já online geram `entry`.

```json
{
  "collectorId": "sowork-inspector",
  "type": "entry",
  "user": "Nome",
  "userId": "identificador-do-usuario",
  "email": null,
  "happenedAt": "2026-10-13T14:00:00+00:00",
  "eventId": "identificador-unico",
  "source": "sowork-inspector"
}
```

O estado e a fila são transacionais em SQLite; a entrega mantém ordem FIFO e tenta novamente com espera crescente, até 300 segundos entre tentativas. O receptor deve deduplicar por `eventId`: a entrega é pelo menos uma vez. O formato foi adaptado para SoWork (`userId`, sem `spotId`); um receptor antigo do Spot precisa aceitar esse formato. O bot não depende da API de relatórios do Spot nem envia emails diretamente.

## Verificação local

```bash
python3 -m unittest discover -s tests -v
node tests/test_observer.cjs
```

O Node é necessário apenas para o teste JavaScript do observador. Os testes Python cobrem validação de presença, escritórios diferentes, salas vazias, deduplicação, histórico e replay, virada de dia, intervalos de indisponibilidade, locks e persistência/reenvio de notificações.

Para reproduzir um HAR, use um diretório de saída novo, separado dos dados reais. Esse comando não abre navegador nem envia notificações:

```bash
SOWORK_OUTPUT_DIR=/tmp/sowork-replay \
  python3 main.py replay-har app.sowork.com.har
SOWORK_OUTPUT_DIR=/tmp/sowork-replay python3 main.py report
```

A leitura ignora cabeçalhos, cookies e autenticação do HAR, preservando apenas dados necessários à presença. Um diretório que já contém `state.json` é recusado para evitar misturar observações históricas com o rastreamento real.

`.env`, HARs, perfis, bancos e saídas estão excluídos no `.gitignore`. Mantenha esses dados locais: o perfil criado pelo login contém a sessão do SoWork e o HAR original pode conter dados sensíveis. Nenhuma credencial, sessão, saída histórica ou pasta `.git` do Spot foi copiada.

Veja [a análise da captura e a origem dos módulos](docs/analysis.md).

## Diagnóstico de coleta

Erros de presença mostram o motivo específico, como HTTP 403 recebido pelo próprio aplicativo, ausência de confirmação Firestore recente ou escritório ainda não carregado. Não é necessário copiar tokens para `.env`. Um 403 do próprio site ainda exige verificar a sessão e as permissões; a coleta passiva não contorna essa recusa.

Após atualizar o código, interrompa uma execução antiga com Ctrl+C e inicie `python3 main.py run` novamente. Procure as mensagens `Enter Office clicked` e `Office connection confirmed` antes dos snapshots. Se a entrada não for confirmada dentro de `SOWORK_LOGIN_TIMEOUT`, a coleta falha explicitamente em vez de aceitar o lobby como um escritório vazio.
