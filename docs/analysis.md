# Análise e adaptação

## Evidências do HAR

A captura local contém 1.591 requisições, duas respostas bem-sucedidas da API `gaia/rooms` e três conexões WebSocket. Os documentos Firestore de membros ocupam quatro partições (`_0` a `_3`): 49 membros no total, 35 com `isOnline=true`. Os dois snapshots de salas também contêm 35 usuários. A comparação dos conjuntos de IDs não encontrou diferenças.

Os campos `lastOnline` e `status` não substituem `isOnline`: a captura contém 44 membros com status `online` e cinco com `busy`, embora apenas 35 estejam conectados. O Firestore também fornece nomes; nenhuma lista de nomes ou IDs reais foi incluída neste documento ou nos testes.

O WebSocket de chat não fornece uma lista autoritativa de presença. A conexão Gaia `/gaia/g1/v2/` contém 3.004 frames, predominantemente binários. A integração evita depender de uma interpretação especulativa desses frames.

A consulta de salas observada usa POST com `groupId` no corpo. Não houve cabeçalho Authorization nem cookies nessa requisição do HAR. Isso não estabelece que a API seja pública ou dispensará autenticação em todos os ambientes; a implementação permanece no contexto do navegador com login próprio e acompanha os cabeçalhos `source` e `version` da aplicação.

## Origem do código

Foram lidos, sem modificar, os módulos Python em `workspace/spotvirutal_inspector/spotvirtual_inspector` e a interface de comandos original. Os seguintes módulos foram copiados individualmente e adaptados:

| Módulo | Adaptação |
| --- | --- |
| `models.py` | Campos de escritório/sala/membro em lugar de world/spot/org membership |
| `tracker.py` | Identidade composta escritório/usuário; remoção do ID offline específico do Spot; timestamp explícito em snapshot vazio |
| `utils.py` | Nomes neutros para timestamps e mensagens |
| `storage.py` | Histórico independente e timestamp explícito para snapshots vazios |
| `reporting.py` | Replay com campos SoWork e relatórios locais |
| `lockfile.py` | Bloqueio independente e fechamento do descritor quando o lock falha |
| `event_delivery.py` | Emissor SoWork, `userId`, UTC, `eventId` estável e reinício da confirmação após indisponibilidade |

Configuração, cliente, observador de rede, leitor HAR, orquestração e CLI foram implementados neste projeto. Não há imports, caminhos de execução, endpoints ou dependências de configuração que apontem para o projeto original. O HAR do SoWork é apenas entrada de análise/replay e não é necessário em produção.

## Verificação

A reprodução da captura em `/tmp` gerou dois snapshots, 35 usuários por snapshot e um relatório diário reconstruído. Os testes usam identidades fictícias e verificam transições que não ocorreram nos dois snapshots da captura. O arquivo original não basta para comprovar todas as transições em produção.

A operação contínua autenticada depende do primeiro `login` em um perfil novo deste bot. Sem esse login, não é correto afirmar que a sessão real foi validada de ponta a ponta. O replay não reutiliza os tokens ou a sessão presentes no HAR.

A injeção do observador também foi verificada em Chrome headless real com perfil temporário e página local. Uma consulta HTTP direta à API, sem aproveitar autenticação ou cabeçalhos do HAR, retornou `403`; por isso não foi usada como integração de produção. A coleta autenticada permanece no contexto do aplicativo no navegador, como documentado pelo [Selenium para execução de scripts e CDP](https://www.selenium.dev/selenium/docs/api/py/selenium_webdriver_common/selenium.webdriver.common.webdriver.html).

## Correção após teste com sessão autenticada (19/10/2026)

A inspeção de nomes de cabeçalhos e códigos HTTP confirmou que o pedido de salas do próprio aplicativo inclui `Authorization` e retorna 200; o pedido anterior do bot omitia esse cabeçalho e retornava 403. O HAR inicial não incluía esse cabeçalho, por isso não comprovava a ausência de autenticação. O observador agora preserva a autenticação observada na sessão própria somente em memória JavaScript e a reaplica ao endpoint de salas. Ela não faz parte dos valores retornados pelo Selenium nem das exportações. Os erros de presença agora mostram a causa específica sem expor exceções brutas de terceiros.

Após a correção, `collect-once` com o perfil próprio já autenticado completou uma coleta real com 1 usuário online, gerando eventos, estado e relatório diário em `/tmp`, com notificações desativadas. Foram aprovados 17 testes Python e os testes JavaScript, incluindo a reaplicação do cabeçalho sem retorná-lo ao Python.

## Entrada automática no escritório

A tela de pré-entrada também consulta salas. O bot agora clica no botão Enter Office visível e habilitado e exige uma conexão Gaia que já recebeu mensagens antes de coletar. O encerramento dessa conexão invalida a coleta; frames binários não são decodificados. Foram aprovados 20 testes Python e os testes JavaScript, incluindo lobby, botão desabilitado, entrada e desconexão. A verificação desta alteração com o perfil real ficou impedida por outra instância ativa do inspector; ela não foi interrompida.

## Coleta passiva de presença (22/10/2026)

A primeira adaptação passiva dependia de novas respostas de salas e falhou em uso real por falta de resposta recente. A coleta passou a usar os documentos Firestore de membros, com `isOnline`, recebidos naturalmente pelo aplicativo. Não envia consultas extras nem retém cabeçalhos de autenticação. As referências anteriores à reaplicação de Authorization descrevem a implementação antiga.

O observador exige sincronização CURRENT e readTime recente para cada partição e compara a quantidade de membros únicos com groups.memberCount. Documentos pendentes, resets, remoção de documentos relevantes e falhas de transporte invalidam a leitura. A inicialização espera a sincronização até o timeout configurado. O replay do tráfego Firestore do HAR no observador produziu quatro partições, 49 membros e 35 online no primeiro snapshot sincronizado. Esse replay e os testes locais não substituem validação de duração e reconexão na sessão real.

## Partição vazia na sessão real (22/10/2026)

O diagnóstico com o perfil real identificou oito membros distribuídos em três partições e uma quarta partição vazia. O Firestore omite `fields` em `users.mapValue` vazio; o observador confundia isso com documento inválido e bloqueava a inicialização. Agora aceita um mapValue presente sem fields como uma partição vazia, mantendo a validação de contagem e sincronização. O timeout de inicialização preserva a causa específica da coleta passiva em vez de sempre sugerir novo login.

## Divergência do contador de membros (22/10/2026)

O diagnóstico da sessão em execução encontrou conexão Gaia ativa, quatro partições válidas e sincronizadas, oito membros nos documentos e groups.memberCount=9. A comparação estrita com esse contador bloqueava a coleta apesar das confirmações recentes. A implementação agora observa os addTarget nativos do aplicativo para group_membermaps e exige todos os documentos assinados e seus targets sincronizados. Não usa memberCount como prova de completude. O HAR contém quatro assinaturas de documentos de membros no formato observado pelo novo leitor. Atualizações posteriores sem targetIds preservam os targets previamente conhecidos. Foram aprovados os testes de contador divergente, documento assinado ausente e preservação de targets. A instância já em execução mantém o observador antigo até reiniciar.
