# Agenor Tester - Reunioes

Tester por propriedade, adaptado do metodo Agenor para Python/SQLite/Flask (o original e JS + Postgres).
Preservado: semente por passo, catalogo como dado, oraculo medido depois de cada passo, reducao (ddmin) ao caso
minimo, corpus de regressao, cobertura do que ainda nao se sabe testar.

```
.venv\Scripts\python.exe testes\agenor\frente.py --sementes 20 --passos 800 --reduzir
.venv\Scripts\python.exe testes\agenor\frente.py --semente 1003 --falar
.venv\Scripts\python.exe testes\agenor\frente.py --cobertura
.venv\Scripts\python.exe testes\agenor\frente.py --corpus
```

## Isolamento (nao negociavel)
`ambiente.py` troca `config.DADOS_DIR` por uma pasta temporaria ANTES de importar `db`; dois "PCs" (A e B) sao dois
bancos/pastas de atas/gravacoes dentro dela. Nunca toca dados.db, atas/, gravacoes/, token.json, credentials.json, .env.
Rede e servicos: tudo de `google_services` vira bloqueio (levanta `RedeBloqueada`), exceto falsos de agenda/Doc/e-mail;
`analisador.gerar_ata` e falso; `monitor.notificar`, `sincronia.agendar/sincronizar` sao falsos; gravacao, janela
tkinter e threads de processamento ficam de fora. O repositorio e PUBLICO: nenhum dado real entra aqui.

## Duas decisoes do ambiente
- Relogio virtual (`db.agora` estritamente crescente) e uuid deterministico: sem isso a semente nao reproduz e dois
  "PCs" empatariam no mesmo milissegundo (empate legitimo, nao defeito).
- Convergencia e cobrada na troca nos dois sentidos (B<-A, A<-B). Se so igualar na 2a rodada, e classe separada
  (`S3`), mais leve que `S2` (nunca iguala).

## Propriedades
Estado (depois de cada passo, nos dois PCs): E1..E16 (status valido, prazo ISO ou NULL, concluida_em coerente, sem tarefa
orfa, `tarefas()` nunca devolve excluida, atrasada/hoje/dias coerentes, unicidade, atas legiveis).
Insercao (dentro da acao): I1..I10, S1..S9 (mesclar nao estoura, converge, nao perde, mais recente vence, exclusao nao
desfaz, idempotente), H1..H4 (hostis), R1/R2 (rota estoura / 5xx).

## Catalogos
`v1` = acoes.py, `v2` = v1 + acoes2.py (clientes/sincronia), `v3` = v2 + acoes3.py (pessoas dos clientes, remarcar,
envios presos no Outlook, extensao sem gravacao), `v4` = v3 + acoes4.py (excluir cliente e recadastrar), `v5` = v4 + acoes5.py (POST de outro site, DNS rebinding), `v6` = v5 + acoes6.py (copias de seguranca: voltar e desfazer;
"Reconectar Google"; aviso de PC parado; sinal de vida hostil no Drive), `v7` = v6 + acoes7.py (som mudo da extensao, vinculos de
convite do Outlook entre PCs, editar ata com o Doc alterado fora do painel, arquivar videos antigos), `v8` = v7 + acoes8.py (som do PC no lugar da extensao), `v9` = v8 + acoes9.py (cliente fixado e consultor por reuniao), `v10` = v9 + acoes10.py (juntar a gravacao que voltou, copia local da agenda, alucinacao do Whisper). Cada um CONGELADO quando o
seguinte nasce: acao nova vai num catalogo novo, senao as sementes guardadas deixam de ser o mesmo passeio. Padrao: v10.
Conserto de ORACULO ou de gerador que estourava (sem mudar o sorteio de passos nem o consumo do `random`) pode entrar num catalogo
congelado: o passeio continua o mesmo, e os carimbos do corpus sao refeitos (2026-10-22: `_quando` do I49 lendo enviado_em como hora
local, em acoes7.py; tarefa hostil que nao e dict em acoes2.py).
Propriedades do v6: I30/I31 (voltar para copia reproduz tarefas/notas/clientes da copia e nao perde o criado depois), I32 (a copia
"antes de voltar" desfaz), I33 (nome de copia hostil nao muda nada), I34/I35 (aviso e login do Google, sem login duplo), I36/I37
(aviso de PC parado, escapado; esquecer PC), H6 (sinal de vida com appProperties estranhos), I38 (cancelar reuniao: so o organizador;
Google ou Outlook; sem redirecionar para fora), I39 (editar ata: feita nunca some, acao nova vira tarefa, acao apagada tira a
tarefa aberta), I40 (cancelar a criacao da ata, rodando o _processar_gravacao/processar_texto de verdade com Whisper e Claude falsos que apertam o botao: nao cria ata/Doc/tarefas, nao apaga audio, a gravacao volta a listar_pendentes() com os mesmos arquivos, cancelar tarde ou sem tarefa nao muda nada, o cancelamento nao vaza), I41 (voltar hostil do Cancelar nunca sai do site nem estoura; inclui quebra de linha), I42 (transcricao reaproveitada: com .txt o Whisper nao roda; falha do Claude sem ata devolve a gravacao a pendente), I43 (fila de atas: tirar da fila nunca gera nada nem mexe na ata ativa; a fila esvazia), I44 (tarefa_para_tela nunca estoura nem mostra tempo negativo), I45 (Convidar: so os novos, sem repetir nem por caixa, so o organizador; convite do Outlook => Google sem aviso), I46 (duracao da nova reuniao limitada a 5..1440), I47 (sugestoes com cliente vem so de reunioes desse cliente). A agenda falsa nao perde eventos ao cancelar (testes antigos contam com ev1..ev5).
Propriedades do v7 (commit f24fd3e): I48 (aviso de som mudo: gravando pela extensao, aba certa, em chamada, pico <= SOM_MINIMO por
>= SOM_MUDO_SEG => `monitor.notificar` exatamente uma vez e /gravacao mostra "nao esta chegando"; som limpa; outra aba, gravacao manual/nao
automatica, sem gravacao ou fora de chamada nunca avisa; pico/diag/trilhas/corpo lixo nunca da 5xx; o relogio de `reunioes.time` e a thread
do aviso sao trocados so durante a acao), I49 (vinculos de convite, com as funcoes ORIGINAIS de uma segunda copia de `envio.py`
(`ambiente.envio_real()`; o Outlook continua bloqueado): mesclar idempotente e comutativo por evento, vale o maior atualizado_em/enviado_em,
ignora entrada lixo, cancelado nao volta por versao mais velha, `convite_do_evento` nunca devolve cancelado, registrar/completar/anotar),
I50 (remarcar/cancelar/convidar com convite do Outlook: completar -> Outlook -> Google sem aviso, nessa ordem; Outlook falhou => Google
intocado e sem 5xx; sem convite o Outlook nunca e chamado), I51 (editar ata com o Doc alterado fora do painel, com `g.doc_modificado_em`
sorteado: mexido = mais de 3 min depois de doc_salvo_em/gerada_em; mexido sem substituir=1 => 200 com o digitado, nada salvo, Doc e tarefas
intocados; com substituir=1 ou nao mexido => salva e grava doc_salvo_em; excecao/None nunca da 5xx nem bloqueia), I52 (`arquivar_videos`:
VIDEOS_DIAS<=0 nada some; nunca apaga *-tela*, mais novo que o limite, outro tipo, nem o que nao confere no Drive; ocupado() para; upload
que falha/trunca nao perde o video). Fora do alcance: o passo 1b de `_sincronizar` (Drive); so a mesclagem pura e testada.
Propriedades do v8 (commit 02e0eb6 + `_TrilhaExterna.start()`): I53 (som do PC; `f_som_do_pc` em acoes8.py). Roda o Gravador REAL com uma
`_TrilhaExterna` real (WAV em pasta temporaria) e troca `gravador._SomDoPC` por um duble (`_SomDoPCFalso`: nada de soundcard; modos ok /
falha_na_hora / falha_depois; alimenta com do_pc=True o valor -0.25, a extensao manda 0.5 por `/api/extensao/audio`); `gravador.time`
e `reunioes.time` sao o mesmo relogio virtual e `gravador.wave` abre devagar (30 ms) para o primeiro audio logo depois de `start()` so
sobreviver se o WAV for aberto dentro de `start()`. Cenarios: certo, sem_trilha (como o f_som_mudo da v7, que continua valendo), manual,
nao_automatica, sem_gravacao, arquivo_ruim. Cobra: troca so depois de SOM_PC_SEG de silencio em chamada (I53-troca-cedo); volta quando
chega som; o estado do modelo = `usando_som_do_pc` = `trilha.do_pc`; o audio da fonte fora de uso e ignorado (conferido por `_escritas`
a cada alimentar) e o WAV final tem so as amostras da fonte em uso, na ordem (I53-wav-misturado); trilha alinhada (`_escritas` perto do
tempo corrido); duble que falha nunca e recriado (1 por gravacao) e o aviso de som mudo (so se o som do PC NAO esta em uso) segue a regra;
fora de gravacao / manual / nao automatica nada troca nem avisa; `parar()` limpa tudo e `usar_som_do_pc` sem gravacao devolve False;
/gravacao mostra "gravada pelo som do PC" so quando em uso. O modelo do aviso mudo da regra nova esta em f_som_do_pc; f_som_mudo (v7) fica
como esta porque seu Gravador nao tem trilhas.
Mutantes (copias em AGENOR_ALVO, nunca no codigo real) que o I53 pega: troca com 5 s em vez de 45; `alimentar` sem checar do_pc; ignorar
`_SomDoPC.erro`; aviso mudo sem a condicao do som do PC; `start()` antigo (WAV aberto na thread); nao voltar para a extensao; tentar de
novo apos falha; sem preencher buraco; tela sempre dizendo som do PC; trocar fora de chamada. Para refazer: copie os *.py para uma pasta,
mude uma linha e rode com `AGENOR_ALVO=<pasta>` (o passeio so da acao: filtre os indices por `N.escolher(...)[0].nome`).
Propriedades do v9 (commit f25e25c): I54 (cliente fixado: POST /resumo/<id>/cliente grava `cliente_uuid` no evento; vale sobre a
sugestao automatica em `reunioes.sugerir_cliente`, `clientes.clientes_das_reunioes` e no cartao da agenda; segue a uniao de clientes;
'nenhum'/id hostil volta ao automatico; cliente excluido nao vale; evento inexistente nao grava; nunca avisa ninguem; a funcao real de
google_services roda contra um `_SvcFalso` e cobra sendUpdates="none" e a leitura de extendedProperties lixo), I55 (consultor: espacos
normalizados, <= 80, vazio apaga, escapado no painel e no /resumo; `consultores_conhecidos` sem repetir por caixa/acento e do mais usado
ao menos; /reuniao/nova leva o consultor ao `agendar_followup`). Mutantes pegos: `sugerir_cliente` ignorando o fixado; rota sem normalizar.
I56 (e-mail do consultor: `clientes.emails_dos_consultores` nunca estoura com evento/participante lixo, nunca devolve a minha conta
nem algo sem @, o guardado mais recente vence e o palpite so vale com candidato unico que comeca com o primeiro nome; /reuniao/nova com
consultor_email invalido nao cria evento nem convite, valido entra nos convidados uma vez so e vai ao evento, sem consultor e ignorado;
o mapa vai escapado no <script>). Achou: nome so com acento solto (IndexError) e participantes/e-mail que nao sao texto.
Propriedades do v10 (ainda nao commitadas quando escritas: `juntar_com_a_anterior`, `_guardar_agenda`, `alucinacao_conhecida`):
I57 (`f_juntar_gravacoes`; WAVs de verdade de 1000/8000 Hz em pasta temporaria, `-agj-` no nome, apagados no fim; o modelo diz QUEM e a anterior
olhando o disco antes da chamada; roda `juntar_com_a_anterior` direto ou o `ao_encerrar_meet` inteiro com `encerrar_gravacao`, `_vincular_telas`,
`webbrowser` e `monitor.notificar` falsos). Cobra: audio final = anterior + silencio + nova (silencio = inicio da nova - inicio da anterior -
duracao do AUDIO da anterior, nunca negativo), duracao total, minutos = duracao total, encerrada_em da nova, transcricao ao vivo concatenada,
nova resolvida=`juntada:<anterior>` sem audios, uma pendente a menos, iscas intactas, chamar de novo nao faz nada, trilha que a nova nao tem fica
como estava; NAO junta (e nenhum arquivo muda) com outra reuniao, avulsa, anterior ja decidida, .webm/.txt em qualquer das duas, fora de
-60..600 s, SINCRONIZAR_AUDIO, nova com trilha que a anterior nao tem, anterior sem a trilha voce ou com encerrada_em lixo; no ao_encerrar_meet
juntou => devolve a anterior, abre a pergunta dela e nao avisa; nao juntou => devolve a nova, avisa e abre a nova; juntar_wavs falhando: os
audios da nova ficam, a nova continua pendente e o ao_encerrar_meet segue (ver ABERTAS abaixo).
I58 (`f_agenda_local`; `g.AGENDA_LOCAL` aponta para uma pasta dentro de TMP durante a acao; o ambiente agora deixa `reunioes_guardadas` de
verdade e guarda `amb.LISTAR_REUNIOES_REAL`, que roda com `g._eventos` trocado): `_guardar_agenda` troca so o periodo consultado (o que sumiu do
Google sai, o de fora do periodo fica), guarda so de -2 a +7 dias, sem id repetido, sem `.tmp`, nunca levanta; `listar_reunioes` guarda e filtra
so os com link; Google falhando nao grava; `reunioes_guardadas` com arquivo corrompido/pasta/lixo devolve lista vazia; `_reuniao_da_janela` com
o Google falhando associa pela copia (codigo do Meet, ou o unico em andamento), igual ao caminho normal com os mesmos candidatos, e com o Google
respondendo (mesmo vazio) a copia nunca vale.
I59 (`f_alucinacao`): "Legendas pela comunidade de Amara.org", "se inscreva no canal", "obrigado por assistir" etc. (qualquer caixa, com texto em volta)
sao descartadas por `segmento_confiavel` mesmo com numeros bons; "a legenda de preco", "inscreva-se no site", "Tchau, tchau" nao; a regra antiga
(compressao > 2.4, silencio provavel, logprob) segue valendo; texto None/vazio nunca estoura; `transcrever_audio` (modelo falso) so devolve as legitimas.
Mutantes (copias em AGENOR_ALVO, nunca no codigo real) que o v10 pega, 30 de 30: janela de 10 min alargada; sem a margem de -60 s; sem o id da
reuniao; sem o SINCRONIZAR_AUDIO; sem o "avulsa-"; sem o subconjunto de trilhas; sem o .webm/.txt da anterior; silencio zero; audios da nova nao
apagados; nova nao resolvida; marca `juntada:` errada; minutos somados em vez de recalculados; encerrada_em nao atualizado; transcricoes
invertidas; `ao_encerrar_meet` devolvendo a nova / ignorando a juntada / avisando mesmo juntando / deixando a excecao subir; poda de 20 dias;
sem substituir o periodo; frente de 30 dias; `listar_reunioes` sem guardar; `os.replace` no arquivo errado; `except` sem ValueError; fallback
com lista vazia; copia usada com o Google respondendo vazio; "obrigado por assistir" fora do regex; "legenda" solta; "inscreva(-se)?" solto;
sem IGNORECASE; `segmento_confiavel` sem chamar `alucinacao_conhecida`. Os scripts dos mutantes sao descartaveis (copiar *.py do projeto,
trocar um trecho, `AGENOR_ALVO=<pasta>`, rodar so as tres acoes novas filtrando os indices por `N.escolher(...)[0].nome`).

Propriedades do v3: I21/I22 (pessoa ligada aparece, sem duplicar), I23 (remarcar so quando pode, horario/duracao/aviso
certos, convite do Outlook atualizado), I24 (dispensar envio so mexe nele), I25 (extensao sem gravacao nao cria arquivo),
S10 (pessoas convergem entre os PCs), E26/E27 (pessoa uma vez por cliente, inclusive acentuada; listas coerentes).

## Antes de cada push
`.githooks/pre-push` roda `--cobertura` (reprova rota/funcao de db sem acao e sem explicacao em SEM_ACAO) e `--corpus`.
Ligado por `git config core.hooksPath .githooks` (o instalar.ps1 faz). Emergencia: `git push --no-verify`.

## Reducao rapida
O ddmin de 800 passos pode passar de 10 min. Antes: bissecar o passo do achado e testar `[passo]` e pares `[k, passo]`
(quase sempre basta). Para recapturar um defeito ja consertado, `AGENOR_ALVO` numa copia do commit anterior
(`git archive HEAD`), sem mexer na arvore de trabalho.

## Antes de relatar um achado
Pergunte: e do sistema ou do oraculo? A propriedade e de estado ou de insercao? O gerador e legitimo? A semente NAO
atravessa mudanca de `acoes.py` (o corpus carimba o catalogo e avisa).

## Corpus
`sementes/` = regressao (so o que ja foi consertado). `sementes/abertos/` = achado verdadeiro ainda sem decisao.

## O que o Agenor nao ve: o Outlook de verdade

O Agenor simula o Outlook (nada sai de verdade), entao confere so a ordem que o painel da, nao o que o Outlook
manda. Mudou algo em `envio.py` que mexe em convite? Rode a mao o teste real, so entre contas suas:
`.venv\Scripts\python.exe testes\outlook_real\incluir_convidado.py ORGANIZADOR CONVIDADO NOVO`.

## ABERTAS (achados verdadeiros sem decisao; casos minimos em `sementes/abertos/`)
Nenhuma. Consertados em 23/10 e guardados como regressao em `sementes/`:
- `I58-guardadas-levanta`: `reunioes_guardadas` com agenda_local.json valido mas nao lista (`null`, `5`) levantava TypeError; agora devolve [].
- `I57-erro-juntou-pela-metade`: `juntar_com_a_anterior` junta todas as trilhas em temporarios e so troca no fim; falha numa trilha nao muda nada.
- (observacao) o regex de alucinacao agora so pega "legendas por Fulano" quando e o segmento inteiro; "revisar as legendas por produto" passa.
