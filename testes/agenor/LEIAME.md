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
envios presos no Outlook, extensao sem gravacao), `v4` = v3 + acoes4.py (excluir cliente e recadastrar), `v5` = v4 + acoes5.py (POST de outro site, DNS rebinding). Cada um CONGELADO quando o seguinte nasce: acao nova vai num catalogo
novo, senao as sementes guardadas deixam de ser o mesmo passeio. Padrao: v5.
Propriedades do v3: I21/I22 (pessoa ligada aparece, sem duplicar), I23 (remarcar so quando pode, horario/duracao/aviso
certos, convite do Outlook atualizado), I24 (dispensar envio so mexe nele), I25 (extensao sem gravacao nao cria arquivo),
S10 (pessoas convergem entre os PCs), E26/E27 (pessoa uma vez por cliente, inclusive acentuada; listas coerentes).

## Reducao rapida
O ddmin de 800 passos pode passar de 10 min. Antes: bissecar o passo do achado e testar `[passo]` e pares `[k, passo]`
(quase sempre basta). Para recapturar um defeito ja consertado, `AGENOR_ALVO` numa copia do commit anterior
(`git archive HEAD`), sem mexer na arvore de trabalho.

## Antes de relatar um achado
Pergunte: e do sistema ou do oraculo? A propriedade e de estado ou de insercao? O gerador e legitimo? A semente NAO
atravessa mudanca de `acoes.py` (o corpus carimba o catalogo e avisa).

## Corpus
`sementes/` = regressao (so o que ja foi consertado). `sementes/abertos/` = achado verdadeiro ainda sem decisao.
