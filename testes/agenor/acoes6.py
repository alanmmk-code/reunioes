"""Catalogo v6 = catalogo v5 (acoes5.py, CONGELADO) + copias de seguranca (voltar e desfazer), "Reconectar Google"
e o aviso de PC que parou de sincronizar."""
import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from markupsafe import escape

import acoes5 as A5
import ambiente as amb
import db
import envio
import oraculo as o
from acoes import Acao, chamar
from oraculo import cobrar

g = amb.g
sincronia = amb.sincronia

CAMPOS_TAREFA = ("descricao", "responsavel", "minha", "prazo", "status", "excluido")


def _tarefas_notas():
    """Conteudo das tarefas e anotacoes (sem datas), por uuid: o que voltar para uma copia tem de reproduzir."""
    exp = db.exportar()
    return ({t["uuid"]: tuple(t[c] for c in CAMPOS_TAREFA) for t in exp["tarefas"]},
            {n["uuid"]: (n["texto"], n["excluido"]) for n in exp["notas"]})


def _fotos(m):
    if not hasattr(m, "fotos_copias"):
        m.fotos_copias = {}  # (pc, nome da copia) -> (tarefas, notas, clientes vivos) no momento da copia
    return m.fotos_copias


# ----------------------------------------------------------------------------- copias de seguranca

def f_tirar_copia(m, r):
    """Uma copia "de outro dia" (o nome por data e o que a tela lista)."""
    pc = m.pc(r)
    nome = f"dados-2030-01-{r.randint(1, 28):02d}.db"
    db._copiar_banco(db._pasta_copias() / nome)
    vivos = {c["uuid"] for c in db.exportar()["clientes"] if not c["excluido"]}
    _fotos(m)[(pc, nome)] = (*_tarefas_notas(), vivos)
    return f"{pc} copia {nome}"


def f_voltar_copia(m, r):
    """Voltar para uma copia traz tarefas/anotacoes como eram nela, nao perde o que foi criado depois,
    e a copia "antes de voltar" desfaz."""
    pc = m.pc(r)
    minhas = [nome for (p, nome) in _fotos(m) if p == pc and (db._pasta_copias() / nome).exists()]
    if not minhas:
        return f"{pc} sem copia para voltar"
    nome = r.choice(sorted(minhas))
    lista = {c["nome"]: c for c in db.copias()}
    cobrar(lista.get(nome, {}).get("legivel"), "I30-copia-boa-ilegivel",
           f"{pc}: copia {nome} tirada pelo sistema aparece ilegivel: {lista.get(nome, {}).get('erro')}")
    tar_copia, not_copia, vivos_copia = _fotos(m)[(pc, nome)]
    tar_antes, not_antes = _tarefas_notas()
    pc, resp = chamar(m, r, "post", "/copias/voltar", data={"nome": nome})
    cobrar(resp.status_code in (302, 303), "I30-voltar-copia-rota", f"{pc}: voltar para {nome} -> {resp.status_code}")
    tar, nts = _tarefas_notas()
    for u, v in tar_copia.items():
        cobrar(tar.get(u) == v, "I30-voltar-copia-tarefa", f"{pc}: tarefa {u} era {v} na copia {nome} e ficou {tar.get(u)}")
    for u, v in not_copia.items():
        cobrar(nts.get(u) == v, "I30-voltar-copia-nota", f"{pc}: nota {u} era {v} na copia {nome} e ficou {nts.get(u)}")
    for u, v in tar_antes.items():
        if u not in tar_copia:
            cobrar(tar.get(u) == v, "I31-voltar-copia-perdeu-o-novo", f"{pc}: tarefa {u} criada depois da copia: {v} -> {tar.get(u)}")
    with db.conexao() as con:
        for u in vivos_copia:
            final = db._canonico(con, u)
            vivo = con.execute("SELECT excluido FROM clientes WHERE uuid = ?", (final,)).fetchone()
            cobrar(vivo is not None and not vivo["excluido"], "I30-voltar-copia-cliente",
                   f"{pc}: cliente {u} (hoje {final}) existia na copia {nome} e nao voltou")
    if r.random() < 0.5:  # desfazer: a copia "antes de voltar" mais nova
        antes = [c for c in db.copias() if c["antes_de_voltar"]]
        cobrar(bool(antes), "I32-sem-copia-antes-de-voltar", f"{pc}: voltou para {nome} sem guardar o estado anterior")
        chamar(m, r, "post", "/copias/voltar", data={"nome": antes[0]["nome"]})
        tar, nts = _tarefas_notas()
        cobrar((tar, nts) == (tar_antes, not_antes), "I32-desfazer-nao-volta",
               f"{pc}: desfazer a volta para {nome}: tarefas/notas diferentes de antes")
    if r.random() < 0.3:
        o.trocar(m)
    return f"{pc} voltou para {nome}"


def f_voltar_copia_hostil(m, r):
    """Nome de copia que nao e uma copia (caminho, lixo, arquivo estragado): recusa e nao muda nada."""
    pc = m.pc(r)
    pasta = db._pasta_copias()
    if r.random() < 0.4:
        pasta.mkdir(exist_ok=True)
        (pasta / "dados-2030-02-30.db").write_bytes(r.choice([b"", b"lixo" * 50, b"SQLite format 3\x00" + b"\x00" * 50]))
    nome = r.choice(["../dados.db", "..\\dados.db", str(db.ARQUIVO), "dados.db", "dados-2030-02-30.db", "", " ",
                     "dados-2030-01-01.db/../../dados.db", "x" * 300, "dados-2030-01-01.db\x00"])
    antes = {t: o.linhas(t) for t in ("clientes", "tarefas", "notas", "pessoas")}
    copias_antes = sorted(p.name for p in pasta.glob("*")) if pasta.exists() else []
    pc, resp = chamar(m, r, "post", "/copias/voltar", data={"nome": nome})
    cobrar({t: o.linhas(t) for t in antes} == antes, "I33-voltar-copia-hostil-mudou", f"{pc}: nome {nome!r} alterou dados")
    depois = sorted(p.name for p in pasta.glob("*")) if pasta.exists() else []
    cobrar(depois == copias_antes, "I33-voltar-copia-hostil-copia", f"{pc}: nome {nome!r} criou/apagou copia: {set(depois) ^ set(copias_antes)}")
    pc, resp = chamar(m, r, "get", "/copias")
    cobrar(resp.status_code == 200, "I33-tela-copias", f"{pc}: /copias com arquivo estragado -> {resp.status_code}")
    return f"{pc} voltar hostil {nome[:30]!r}"


# ----------------------------------------------------------------------------- reconectar o Google

def _esperar_login(limite=5.0):
    fim = time.time() + limite
    while g.ESTADO["login"]["rodando"] and time.time() < fim:
        time.sleep(0.01)
    return not g.ESTADO["login"]["rodando"]


def f_reconectar_google(m, r):
    m.pc(r)
    caiu = r.random() < 0.6
    g.ESTADO["desconectado"] = "invalid_grant: Token has been expired or revoked." if caiu else None
    pc, resp = chamar(m, r, "get", "/clientes")
    tem_botao = b"Reconectar Google" in (resp.data if hasattr(resp, "data") else b"")
    cobrar(tem_botao == caiu, "I34-aviso-google", f"{pc}: desconectado={caiu} e botao na pagina={tem_botao}")
    falha = r.random() < 0.3
    original = g.reconectar
    if falha:
        def _recusa(espera_s=300):
            amb.LOGINS.append(espera_s)
            raise RuntimeError("o login não foi concluído (teste)")
        g.reconectar = _recusa
    try:
        n = len(amb.LOGINS)
        cliques = r.choice([1, 1, 2])
        if cliques == 2:  # segundo clique com o primeiro login ainda aberto: nao abre outro
            g.ESTADO["login"].update(rodando=True)
            chamar(m, r, "post", "/google/conectar")
            cobrar(len(amb.LOGINS) == n, "I35-login-duplo", f"{pc}: clique com login aberto abriu outro")
            g.ESTADO["login"].update(rodando=False)
        pc, resp = chamar(m, r, "post", "/google/conectar")
        cobrar(_esperar_login(), "I35-login-preso", f"{pc}: o login falso nao terminou")
    finally:
        g.reconectar = original
    cobrar(len(amb.LOGINS) == n + 1, "I35-login-nao-abriu", f"{pc}: {len(amb.LOGINS) - n} login(s) abertos")
    pc, resp = chamar(m, r, "get", "/api/google")
    j = resp.get_json() if hasattr(resp, "get_json") else {}
    if falha:
        cobrar(bool(j.get("erro")) and j.get("desconectado") == caiu, "I35-login-falhou-errado", f"{pc}: {j}")
    else:
        cobrar(not j.get("erro") and j.get("conta") and not j.get("desconectado"), "I35-login-certo-errado", f"{pc}: {j}")
    return f"{pc} reconectar google (caiu={caiu} falha={falha})"


# ----------------------------------------------------------------------------- PC que parou de sincronizar

PCS_NOMES = ["ESCRITORIO", "casa-pc", "<b>x</b>", "PC \"aspas\" 'e' & <", "çãõ"]


def f_pc_parado(m, r):
    m.pc(r)
    agora = datetime.now(timezone.utc)
    outros = {}
    for nome in r.sample(PCS_NOMES, r.randint(0, 3)):
        outros[nome] = agora - timedelta(hours=r.choice([0.1, 5, 47, 71, 73, 24 * 10]))
    sincronia.ESTADO["outros_pcs"] = dict(outros)
    parados = {p for p, q in outros.items() if agora - q > timedelta(days=sincronia.PARADO_DIAS)}
    ligada = r.random() < 0.7  # com a sincronizacao desligada (SINCRONIZAR=nao) nao ha aviso
    amb.config.SINCRONIZAR = ligada
    try:
        pc, resp = chamar(m, r, "get", "/tarefas")
    finally:
        amb.config.SINCRONIZAR = False
    html = resp.data.decode("utf-8") if hasattr(resp, "data") else ""
    for nome in outros:
        visivel = f"O PC <b>{escape(nome)}</b>" in html
        cobrar(visivel == (ligada and nome in parados), "I36-aviso-pc-parado",
               f"{pc}: {nome!r} parado={nome in parados} sincronizacao={ligada} e aviso={visivel}")
    cobrar("<b>x</b></b>" not in html, "I36-aviso-pc-sem-escape", f"{pc}: nome do PC com HTML entrou cru na pagina")
    # "Nao uso mais esse PC": tira o sinal de vida do Drive (falso) e o aviso some
    apagados = []
    falsos = {"drive_pasta": lambda nome: "pasta", "drive_apagar": apagados.append,
              "drive_listar": lambda pasta: {f"pc__{p}": {"id": f"id-{p}"} for p in outros}}
    originais = {k: getattr(g, k) for k in falsos}
    for k, f in falsos.items():
        setattr(g, k, f)
    try:
        alvo = r.choice(sorted(outros) + ["nao-existe", ""]) if outros else "nao-existe"
        pc, resp = chamar(m, r, "post", "/sincronia/esquecer-pc", data={"pc": alvo})
    finally:
        for k, f in originais.items():
            setattr(g, k, f)
    if alvo in outros:
        cobrar(apagados == [f"id-{alvo}"] and alvo not in sincronia.ESTADO["outros_pcs"], "I37-esquecer-pc",
               f"{pc}: esquecer {alvo!r} apagou {apagados}, ainda acompanhados {sorted(sincronia.ESTADO['outros_pcs'])}")
    else:
        cobrar(not apagados and sincronia.ESTADO["outros_pcs"] == outros, "I37-esquecer-pc-inexistente",
               f"{pc}: esquecer {alvo!r} apagou {apagados}")
    sincronia.ESTADO["outros_pcs"] = {}
    return f"{pc} pcs {sorted(outros)} esquecer {alvo!r}"


def f_sinal_de_vida_hostil(m, r):
    """Arquivos pc__ do Drive com appProperties estranhos: nao estoura, so guarda datas validas, nunca o proprio PC,
    e so reescreve o proprio sinal quando falta ou tem mais de 1 hora. (O Drive sempre devolve cada arquivo como
    dicionario e appProperties com valores texto; o que varia e o texto, escrito por outro PC ou a mao.)"""
    m.pc(r)
    agora = datetime.now(timezone.utc)
    lixos = [{}, {"atualizado_em": ""}, {"atualizado_em": "ontem"}, {"atualizado_em": "2030-01-01T10:00:00"},
             {"atualizado_em": (agora - timedelta(days=4)).isoformat()}, {"atualizado_em": (agora - timedelta(minutes=5)).isoformat()},
             {"atualizado_em": "1700000000"}, {"outro": "x"}]
    remotos = {}
    for nome in r.sample(["pc__A1", "pc__", "pc__B2", f"pc__{sincronia.PC}", "dados.json", "pc__C3"], r.randint(0, 5)):
        remotos[nome] = r.choice([{"id": "x", "appProperties": r.choice(lixos)}, {"id": "y"}])
    enviados = []
    original = g.drive_enviar
    g.drive_enviar = lambda pasta, nome, conteudo=None, **k: enviados.append((nome, k.get("file_id"))) or "id"
    try:
        sincronia._sinal_de_vida("pasta", remotos)
    except Exception as e:  # noqa: BLE001
        cobrar(False, "H6-sinal-de-vida-estoura", f"remotos {remotos!r}: {type(e).__name__}: {e}")
    finally:
        g.drive_enviar = original
    outros = sincronia.ESTADO["outros_pcs"]
    cobrar(sincronia.PC not in outros and "" not in outros, "H6-sinal-de-vida-proprio", f"outros={sorted(outros)}")
    cobrar(all(isinstance(q, datetime) and q.tzinfo for q in outros.values()), "H6-sinal-de-vida-data", f"outros={outros}")
    meu = remotos.get(f"pc__{sincronia.PC}")
    props = meu.get("appProperties") if isinstance(meu, dict) else None
    recente = isinstance(props, dict) and props.get("atualizado_em") == lixos[5]["atualizado_em"]
    cobrar(len(enviados) == (0 if recente else 1), "H6-sinal-de-vida-escrita", f"proprio={meu!r} enviou {enviados}")
    sincronia.ESTADO["outros_pcs"] = {}
    return f"sinal de vida com {len(remotos)} arquivo(s)"


# ----------------------------------------------------------------------------- cancelar reuniao

def f_cancelar_reuniao(m, r):
    """So o organizador cancela; o evento some da agenda; convite do Outlook cancela pelo Outlook (Google sem e-mail)."""
    m.pc(r)
    ev = r.choice(amb.EVENTOS + [{"id": "nao-existe"}]) if amb.EVENTOS else {"id": "nao-existe"}
    com_convite = r.random() < 0.3
    original = envio.convite_do_evento
    if com_convite:
        envio.convite_do_evento = lambda eid: {"entry_id": "entrada-falsa"}
    n_envios = len(amb.ENVIOS)
    amb.CANCELADOS.clear()
    voltar = r.choice(["/", "/clientes", "https://evil.example/", "//evil.example", "/\\evil.example", "/\n/evil.example", ""])
    try:
        pc, resp = chamar(m, r, "post", f"/reuniao/{ev['id']}/cancelar", data={"voltar": voltar})
    finally:
        envio.convite_do_evento = original
    destino = resp.headers.get("Location", "") if hasattr(resp, "headers") else ""
    cobrar("evil.example" not in destino, "I38-cancelar-redireciona-para-fora", f"{pc}: voltar={voltar!r} -> {destino}")
    pode = bool(ev.get("organizador")) and ev["id"] != "nao-existe"
    outlook = [a for a in amb.ENVIOS[n_envios:] if a[0] == "cancelar_convite"]
    if not pode:
        cobrar(not amb.CANCELADOS and not outlook, "I38-cancelar-indevido", f"{pc}: {ev['id']} organizador={ev.get('organizador')} cancelou")
        return f"{pc} cancelar {ev['id']} recusado"
    cobrar(amb.CANCELADOS == [(ev["id"], not com_convite)], "I38-cancelar-google", f"{pc}: {ev['id']} convite Outlook={com_convite} -> {amb.CANCELADOS}")
    cobrar(len(outlook) == (1 if com_convite else 0), "I38-cancelar-outlook", f"{pc}: {ev['id']} convite Outlook={com_convite} -> {outlook}")
    return f"{pc} cancelou {ev['id']}"


# ----------------------------------------------------------------------------- editar a ata pelo painel

def _tarefas_da_ata(ev):
    return [t for t in o.linhas("tarefas") if t["origem_ata"] == ev and not t["excluido"]]


def f_editar_ata(m, r):
    """Editar a ata: o arquivo guarda o novo conteudo; tarefa ja feita nunca some; acao nova vira tarefa; acao
    apagada tira a tarefa aberta dela. Formulario torto (listas de tamanhos diferentes, lixo) nao estoura."""
    pc = m.pc(r)
    ids = sorted(amb.config.ATAS_DIR.glob("*.json"))
    if not ids or r.random() < 0.2:
        chamar(m, r, "post", "/manual/ev1", data={"texto": "texto da reuniao", "titulo": ""})
        ids = sorted(amb.config.ATAS_DIR.glob("*.json"))
    ev = r.choice([p.stem for p in ids] + ["nao-existe"]) if ids else "nao-existe"
    reg = amb.atas.carregar(ev) or {}
    velhas = (reg.get("ata") or {}).get("acoes") or [] if isinstance(reg.get("ata"), dict) else []
    tarefas_antes = _tarefas_da_ata(ev)
    feitas = {t["uuid"]: t for t in tarefas_antes if t["status"] == "feito"}
    if tarefas_antes and r.random() < 0.4:  # marca uma como feita antes de editar
        alvo = r.choice(tarefas_antes)
        db.atualizar_tarefa(alvo["id"], status="feito")
        tarefas_antes = _tarefas_da_ata(ev)
        feitas = {t["uuid"]: t for t in tarefas_antes if t["status"] == "feito"}
    # novas acoes: mantem umas, apaga outras, muda prazo de alguma, acrescenta
    novas = []
    for a in velhas:
        if not isinstance(a, dict):
            continue
        sorte = r.random()
        if sorte < 0.25:
            continue  # apagada
        a = dict(a)
        if sorte < 0.35:
            a["prazo"] = r.choice(["2030-03-03", "A definir", ""])
        novas.append(a)
    for k in range(r.choice([0, 0, 1, 2])):
        novas.append({"tarefa": r.choice([f"nova acao {k}", "acao 0", "  ", "ç" * 50]), "responsavel": r.choice(["", "Joao"]),
                      "prazo": r.choice(["", "2030-01-15", "amanha"]), "do_usuario": r.random() < 0.5})
    dados = {"titulo": r.choice(["Ata editada", "", "<b>x</b>"]), "resumo": r.choice(["novo resumo", ""]),
             "participantes": "A\nB", "decisoes": r.choice(["d1\nd2", ""]), "pontos": "", "prox_pauta": "",
             "prox_data": r.choice(["", "2030-01-01T10:00", "lixo"]), "prox_combinada": r.choice(["", "1"]),
             "topico_titulo": ["t1", ""], "topico_texto": ["x", ""],
             "acao_tarefa": [str(a.get("tarefa", "")) for a in novas],
             "acao_resp": [str(a.get("responsavel", "")) for a in novas],
             "acao_prazo": [str(a.get("prazo", "")) for a in novas],
             "acao_minha": ["1" if a.get("do_usuario") else "0" for a in novas]}
    torto = r.random() < 0.15
    if torto:  # listas de tamanhos diferentes / campos faltando (formulario mexido a mao)
        dados["acao_resp"] = dados["acao_resp"][:1]
        dados.pop(r.choice(["resumo", "titulo", "acao_minha"]), None)
    pc, resp = chamar(m, r, "post", f"/ata/{ev}/editar", data=dados)
    if not reg or not isinstance(reg.get("ata"), dict):
        return f"{pc} editar ata {ev} sem ata"
    cobrar(resp.status_code in (302, 303), "I39-editar-ata-rota", f"{pc}: editar {ev} -> {resp.status_code}")
    depois = _tarefas_da_ata(ev)
    vivas = {t["uuid"] for t in depois}
    for u, t in feitas.items():
        cobrar(u in vivas, "I39-editar-ata-apagou-feita", f"{pc}: {ev}: tarefa feita {t['descricao']!r} sumiu ao editar a ata")
    salva = (amb.atas.carregar(ev) or {}).get("ata") or {}
    acoes_salvas = [a["tarefa"] for a in salva.get("acoes", [])]
    if not torto:
        esperadas = [a["tarefa"].strip() for a in novas if str(a.get("tarefa", "")).strip()]
        cobrar(acoes_salvas == esperadas, "I39-editar-ata-nao-salvou", f"{pc}: {ev}: pediu {esperadas}, salvou {acoes_salvas}")
    # so o que a edicao promete: acao que nao existia vira tarefa; acao que saiu de vez tira as tarefas abertas dela,
    # quando todas eram dela (tarefa apagada a mao, ou de uma geracao anterior da ata, nao entram na conta)
    def desc(lista):
        return [db._descricao(str(a.get("tarefa", ""))) for a in lista if isinstance(a, dict) and str(a.get("tarefa", "")).strip()]
    antes_d, depois_d = desc(velhas), desc(salva.get("acoes", []))
    abertas_antes = [t["descricao"] for t in tarefas_antes if t["status"] != "feito"]
    for d in set(depois_d) - set(antes_d):
        cobrar(any(t["descricao"] == d for t in depois), "I39-acao-nova-sem-tarefa", f"{pc}: {ev}: acao nova {d!r} sem tarefa")
    for d in set(antes_d) - set(depois_d):
        if abertas_antes.count(d) <= antes_d.count(d):
            sobra = [t for t in depois if t["descricao"] == d and t["status"] != "feito"]
            cobrar(not sobra, "I39-acao-apagada-ficou-tarefa", f"{pc}: {ev}: acao {d!r} saiu da ata e a tarefa aberta ficou")
    return f"{pc} editou ata {ev} ({len(velhas)} -> {len(acoes_salvas)} acoes)"


# ----------------------------------------------------------------------------- cancelar a criacao da ata

def _wav(caminho, segundos=1):
    import wave
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * segundos)


def _bytes_dos_arquivos():
    return {p.name: p.read_bytes() for p in amb.config.GRAVACOES_DIR.iterdir() if p.suffix in (".wav", ".webm")}


def _pend(pid):
    return next((p for p in amb.reunioes.listar_pendentes() if p["pid"] == pid), None)


VOLTAR_ATA = ["/", "/clientes", "", "https://evil.example/", "//evil.example", "/\\evil.example", "/\t/evil.example",
              "/\n/evil.example", "javascript:alert(1)", "http://localhost@evil.example", "\\\\evil.example", "/%0d%0aX:y"]


def _para_fora(destino):
    """O Location, lido como o navegador le (ele tira tab e quebra de linha), vai para outro site?"""
    d = re.sub(r"[\t\r\n]", "", destino or "").replace("\\", "/")
    return not d.startswith("/") or d.startswith("//") or "evil.example" in d


def f_cancelar_ata(m, r):
    """Cancelar a criacao da ata (transcricao / Claude / depois do ponto sem volta / sem tarefa), pelo codigo de
    verdade (reunioes._processar_gravacao e processar_texto), com Whisper e Claude falsos que apertam o botao."""
    pc = m.pc(r)
    modo = r.choice(["gravacao", "texto"])
    fase = r.choice(["transcricao", "claude", "depois", "nenhuma", "sem-tarefa"] if modo == "gravacao"
                    else ["claude", "depois", "nenhuma", "sem-tarefa"])
    pid = f"reuniao-2030{m.prox():04d}"
    pasta = amb.config.GRAVACOES_DIR
    arquivos = {"voce": pasta / f"{pid}-voce.wav", "outros": pasta / f"{pid}-outros.wav"}
    for c in arquivos.values():
        _wav(c, r.randint(1, 3))
    ev = r.choice(["ev1", "ev2"])
    reuniao = {"id": ev, "titulo": "Reuniao", "inicio": "2030-01-01T10:00", "cliente_id": None}
    nomes = {n: c.name for n, c in arquivos.items()}
    ja_tinha_json = modo == "texto" or r.random() < 0.6
    if ja_tinha_json:  # como o pendente_gerar deixa: decidida "ata" enquanto gera
        (pasta / f"{pid}.json").write_text(json.dumps(
            {"reuniao": reuniao, "arquivos": nomes, "transcricao_ao_vivo": "texto ao vivo", "minutos": 1,
             "encerrada_em": "2030-01-01T10:05", "resolvida": "ata", "atualizado_em": db.agora()}), encoding="utf-8")

    docs = []
    original_doc = g.criar_google_doc
    g.criar_google_doc = lambda titulo, html: (docs.append(titulo), original_doc(titulo, html))[1]
    original_transcrever = amb.reunioes.gravador.transcrever
    cliques = []
    cliques_em = r.choice([10, 50, 90])
    clicou = []

    def apertar():
        pc2, resp = chamar(m, r, "post", "/gravacao/cancelar-ata", data={"voltar": "/"})
        cliques.append(resp.status_code)

    def transcrever_falso(arqs, progresso=lambda p: None, reuniao=None):
        for pct in (10, 50, 90):
            progresso(pct)
            if fase == "transcricao" and pct == cliques_em and not clicou:
                clicou.append(1)
                apertar()
                progresso(pct + 1)  # o Whisper segue avisando; o proximo aviso e onde o Cancelar entra
        return "texto transcrito"

    if fase == "claude":
        amb.GANCHO_ATA["fn"] = apertar
    elif fase == "depois":
        g.criar_google_doc = lambda titulo, html: (apertar(), docs.append(titulo), original_doc(titulo, html))[2]
    ata_arq = amb.config.ATAS_DIR / f"{ev}.json"
    antes = {"tarefas": o.linhas("tarefas"), "audios": _bytes_dos_arquivos(),
             "ata": ata_arq.read_bytes() if ata_arq.exists() else None}
    T = amb.reunioes.TAREFA

    def gerar():
        amb.reunioes.gravador.transcrever = transcrever_falso
        try:
            if modo == "gravacao":
                amb.PROCESSAR_GRAVACAO_REAL(reuniao, arquivos, False)
            else:
                amb.PROCESSAR_TEXTO_REAL(reuniao, "texto da reuniao", nomes)
        finally:
            amb.reunioes.gravador.transcrever = original_transcrever

    try:
        if fase == "sem-tarefa":
            tarefa0 = dict(T)
            ligado0 = amb.reunioes.CANCELAR_ATA.is_set()  # sobra de um cancelamento anterior e legitima: o evento so e limpo ao comecar
            apertar()
            cobrar(cliques == [302], "I40-cancelar-sem-tarefa-rota", f"{pc}: cancelar sem tarefa -> {cliques}")
            cobrar(amb.reunioes.CANCELAR_ATA.is_set() == ligado0, "I40-cancelar-sem-tarefa-vaza",
                   f"{pc}: cancelar sem tarefa ativa deixou o evento ligado (vaza para a proxima ata)")
            cobrar(dict(T) == tarefa0, "I40-cancelar-sem-tarefa-mexeu", f"{pc}: TAREFA {tarefa0} -> {T}")
            cobrar(o.linhas("tarefas") == antes["tarefas"] and _bytes_dos_arquivos() == antes["audios"], "I40-cancelar-sem-tarefa-mexeu",
                   f"{pc}: cancelar sem tarefa mudou tarefas/audios")
            # e a ata seguinte sai normal
        gerar()
    finally:
        g.criar_google_doc = original_doc
        amb.reunioes.gravador.transcrever = original_transcrever
        amb.GANCHO_ATA["fn"] = None
    cobrar(not T["ativa"] and not T["cancelavel"], "I40-tarefa-ficou-ativa", f"{pc}: {fase}: TAREFA ficou {T}")
    if fase in ("transcricao", "claude"):
        cobrar(cliques == [302], "I40-cancelar-rota", f"{pc}: {fase}: clique -> {cliques}")
        cobrar(T["cancelada"] and not T["erro"], "I40-cancelada-nao-marcou", f"{pc}: {fase}: cancelada={T['cancelada']} erro={T['erro']!r}")
        cobrar((ata_arq.read_bytes() if ata_arq.exists() else None) == antes["ata"], "I40-cancelada-criou-ata", f"{pc}: {fase}: ata de {ev} mudou apos cancelar")
        cobrar(not docs, "I40-cancelada-criou-doc", f"{pc}: {fase}: criou Google Doc {docs}")
        cobrar(o.linhas("tarefas") == antes["tarefas"], "I40-cancelada-importou-tarefas", f"{pc}: {fase}: tarefas mudaram apos cancelar")
        cobrar(_bytes_dos_arquivos() == antes["audios"], "I40-cancelada-apagou-audio", f"{pc}: {fase}: audios mudaram: {set(antes['audios']) ^ set(_bytes_dos_arquivos())}")
        p = _pend(pid)
        cobrar(p is not None, "I40-cancelada-sumiu-da-lista", f"{pc}: {fase}/{modo}/json={ja_tinha_json}: {pid} nao esta em listar_pendentes()")
        cobrar(p["arquivos"] == nomes, "I40-cancelada-arquivos-trocados", f"{pc}: {fase}: arquivos {p['arquivos']} != {nomes}")
        cobrar(p["reuniao"].get("id") == ev, "I40-cancelada-reuniao-trocada", f"{pc}: {fase}: reuniao {p['reuniao'].get('id')} != {ev}")
        if r.random() < 0.5:  # o cancelamento nao vaza: a geracao seguinte, sem cancelar, termina
            nomes.update(p["arquivos"])
            amb.GANCHO_ATA["fn"] = None
            gerar()
            cobrar(not T["cancelada"] and not T["erro"] and T["etapa"] == "Concluído", "I40-cancelamento-vazou",
                   f"{pc}: depois de cancelar ({fase}), a geracao seguinte saiu cancelada={T['cancelada']} erro={T['erro']!r} etapa={T['etapa']!r}")
            cobrar(amb.atas.carregar(ev) is not None, "I40-cancelamento-vazou-sem-ata", f"{pc}: geracao seguinte a cancelada nao criou ata")
    elif fase == "depois":
        cobrar(cliques == [302], "I40-cancelar-tarde-rota", f"{pc}: cancelar tarde -> {cliques}")
        cobrar(not T["cancelada"] and not T["erro"], "I40-cancelar-tarde-cancelou", f"{pc}: cancelar depois do ponto sem volta: cancelada={T['cancelada']} erro={T['erro']!r}")
        cobrar(len(docs) == 1 and amb.atas.carregar(ev) is not None, "I40-cancelar-tarde-sem-ata", f"{pc}: cancelar tarde: docs={docs} ata={amb.atas.carregar(ev) is not None}")
        cobrar(not amb.reunioes.CANCELAR_ATA.is_set(), "I40-cancelar-tarde-vaza", f"{pc}: cancelar tarde ligou o evento")
        cobrar(T["etapa"] == "Concluído", "I40-cancelar-tarde-etapa", f"{pc}: etapa {T['etapa']!r}")
    else:
        cobrar(not T["cancelada"] and not T["erro"] and T["etapa"] == "Concluído", "I40-sem-cancelar-nao-concluiu",
               f"{pc}: {fase}: cancelada={T['cancelada']} erro={T['erro']!r} etapa={T['etapa']!r}")
        cobrar(len(docs) == 1 and amb.atas.carregar(ev) is not None, "I40-sem-cancelar-sem-ata", f"{pc}: {fase}: docs={docs}")
    return f"{pc} {modo}/{fase} json={ja_tinha_json}"


def f_cancelar_ata_voltar_hostil(m, r):
    """O campo `voltar` do Cancelar nunca leva para fora, com tarefa ativa ou sem ela; e nunca estoura."""
    pc = m.pc(r)
    T = amb.reunioes.TAREFA
    salvo = dict(T)
    if r.random() < 0.5:
        T.update(ativa=True, cancelavel=r.random() < 0.7)
    voltar = r.choice(VOLTAR_ATA)
    try:
        pc, resp = chamar(m, r, "post", "/gravacao/cancelar-ata", data={"voltar": voltar})
    finally:
        T.update(salvo)
        amb.reunioes.CANCELAR_ATA.clear()
    destino = resp.headers.get("Location", "") if hasattr(resp, "headers") else ""
    cobrar(resp.status_code in (302, 303), "I41-cancelar-ata-voltar-rota", f"{pc}: voltar={voltar!r} -> {resp.status_code}")
    cobrar(not _para_fora(destino), "I41-cancelar-ata-redireciona-para-fora", f"{pc}: voltar={voltar!r} -> Location {destino!r}")
    return f"{pc} cancelar-ata voltar={voltar[:20]!r}"


# ----------------------------------------------------------------------------- transcricao reaproveitada e fila de atas

def _ata_bytes(ev):
    a = amb.config.ATAS_DIR / f"{ev}.json"
    return a.read_bytes() if a.exists() else None


class _Fakes:
    """Whisper, Docs e Claude falsos, com contagem; restaura tudo no fim."""

    def __init__(self, texto_whisper="texto transcrito"):
        self.whisper, self.docs, self.recebido = 0, [], []
        self.texto = texto_whisper

    def __enter__(self):
        self.o_trans, self.o_doc, self.o_ata = amb.reunioes.gravador.transcrever, g.criar_google_doc, amb.analisador.gerar_ata
        original_doc, original_ata = self.o_doc, self.o_ata

        def trans(arqs, progresso=lambda p: None, reuniao=None):
            self.whisper += 1
            return self.texto

        def doc(titulo, html):
            self.docs.append(titulo)
            return original_doc(titulo, html)

        def ata(transcricao, reuniao):
            self.recebido.append(transcricao)
            return original_ata(transcricao, reuniao)

        amb.reunioes.gravador.transcrever, g.criar_google_doc, amb.analisador.gerar_ata = trans, doc, ata
        return self

    def __exit__(self, *e):
        amb.reunioes.gravador.transcrever, g.criar_google_doc, amb.analisador.gerar_ata = self.o_trans, self.o_doc, self.o_ata
        amb.GANCHO_ATA["fn"] = None


def f_transcricao_reaproveitada(m, r):
    """Com a transcricao do Whisper ja guardada (.txt) o Whisper nao roda de novo; se o Claude falha e nao ha ata, a
    gravacao volta a listar_pendentes() com o .txt, e a proxima geracao nao transcreve outra vez."""
    pc = m.pc(r)
    modo = r.choice(["gravacao", "gravacao", "texto"])
    txt_modo = r.choice(["sem-txt", "txt-ok", "txt-ok", "txt-vazio"]) if modo == "gravacao" else "sem-txt"
    falha = r.random() < 0.6
    pid = f"reuniao-2030{m.prox():04d}"
    pasta = amb.config.GRAVACOES_DIR
    arquivos = {"voce": pasta / f"{pid}-voce.wav", "outros": pasta / f"{pid}-outros.wav"}
    for c in arquivos.values():
        _wav(c, 1)
    ev = r.choice(["ev1", "ev2", "ev3"])
    reuniao = {"id": ev, "titulo": "Reuniao", "inicio": "2030-01-01T10:00", "cliente_id": None}
    nomes = {n: c.name for n, c in arquivos.items()}
    if modo == "texto" or r.random() < 0.6:
        (pasta / f"{pid}.json").write_text(json.dumps(
            {"reuniao": reuniao, "arquivos": nomes, "transcricao_ao_vivo": "texto ao vivo", "minutos": 1,
             "encerrada_em": "2030-01-01T10:05", "resolvida": "ata", "atualizado_em": db.agora()}), encoding="utf-8")
    txt = pasta / f"{pid}-voce.txt"
    conteudo = f"fala guardada {r.randint(0, 99999)}"
    if txt_modo == "txt-ok":
        txt.write_text(conteudo, encoding="utf-8")
    elif txt_modo == "txt-vazio":
        txt.write_text("", encoding="utf-8")
    ata_antes = bool(amb.atas.carregar(ev))
    audios = _bytes_dos_arquivos()
    T = amb.reunioes.TAREFA
    tarefas0 = o.linhas("tarefas")

    def gerar():
        if modo == "gravacao":
            amb.PROCESSAR_GRAVACAO_REAL(reuniao, arquivos, False)
        else:
            amb.PROCESSAR_TEXTO_REAL(reuniao, "texto da reuniao", nomes)

    def quebrar():
        raise RuntimeError(r.choice(["claude caiu", "timeout <b>x</b>", "falha\ncom quebra"]))

    with _Fakes() as f:
        if falha:
            amb.GANCHO_ATA["fn"] = quebrar
        gerar()
        cobrar(not T["ativa"] and not T["cancelavel"], "I42-tarefa-ficou-ativa", f"{pc}: TAREFA ficou {T}")
        if modo == "gravacao":
            if txt_modo == "txt-ok":
                cobrar(f.whisper == 0, "I42-whisper-rodou-com-txt", f"{pc}: ja havia .txt e o Whisper rodou {f.whisper}x")
                cobrar(f.recebido == [conteudo], "I42-ata-nao-usou-txt", f"{pc}: Claude recebeu {f.recebido!r}, o .txt tinha {conteudo!r}")
            else:
                cobrar(f.whisper == 1, "I42-whisper-nao-rodou", f"{pc}: {txt_modo}: Whisper rodou {f.whisper}x")
                cobrar(txt.exists() and txt.read_text(encoding="utf-8") == "texto transcrito", "I42-txt-nao-guardado",
                       f"{pc}: {txt_modo}: .txt depois da geracao: {txt.read_text(encoding='utf-8') if txt.exists() else None!r}")
        if not falha:
            cobrar(T["etapa"] == "Concluído" and not T["erro"], "I42-nao-concluiu", f"{pc}: etapa {T['etapa']!r} erro {T['erro']!r}")
            cobrar(len(f.docs) == 1 and amb.atas.carregar(ev), "I42-sem-ata", f"{pc}: docs={f.docs}")
            return f"{pc} {modo}/{txt_modo} ok"
        cobrar(T["erro"] and not T["cancelada"], "I42-falha-sem-erro", f"{pc}: erro={T['erro']!r} cancelada={T['cancelada']}")
        cobrar(not f.docs, "I42-falha-criou-doc", f"{pc}: Claude falhou e criou Doc {f.docs}")
        cobrar(o.linhas("tarefas") == tarefas0, "I42-falha-importou-tarefas", f"{pc}: tarefas mudaram apos a falha")
        cobrar(_bytes_dos_arquivos() == audios, "I42-falha-apagou-audio", f"{pc}: audios mudaram")
        if ata_antes:
            return f"{pc} {modo}/{txt_modo} falha (ja havia ata)"
        p = _pend(pid)
        cobrar(p is not None, "I42-falha-sumiu-da-lista", f"{pc}: {modo}/{txt_modo}: Claude falhou, nao ha ata e {pid} nao esta em listar_pendentes()",
               f"I42-falha-sumiu-da-lista:{modo}")
        cobrar(p["arquivos"] == nomes, "I42-falha-arquivos-trocados", f"{pc}: arquivos {p['arquivos']} != {nomes}")
        if modo == "gravacao":
            cobrar(txt.exists() and txt.stat().st_size > 0, "I42-falha-perdeu-txt", f"{pc}: {txt_modo}: o .txt nao ficou guardado")
            whisper1 = f.whisper
        else:
            whisper1 = 0
        amb.GANCHO_ATA["fn"] = None  # nova geracao, agora o Claude responde
        f.recebido.clear()
        gerar()
        cobrar(f.whisper == whisper1, "I42-transcreveu-de-novo", f"{pc}: segunda geracao: Whisper {whisper1} -> {f.whisper}")
        cobrar(T["etapa"] == "Concluído" and not T["erro"], "I42-segunda-nao-concluiu", f"{pc}: etapa {T['etapa']!r} erro {T['erro']!r}")
        cobrar(len(f.docs) == 1 and amb.atas.carregar(ev), "I42-segunda-sem-ata", f"{pc}: docs={f.docs}")
        if modo == "gravacao":
            esperado = conteudo if txt_modo == "txt-ok" else "texto transcrito"
            cobrar(f.recebido == [esperado], "I42-segunda-texto-errado", f"{pc}: Claude recebeu {f.recebido!r}, esperado {esperado!r}")
    return f"{pc} {modo}/{txt_modo} falha e nova geracao"


CHAVES_HOSTIS = ["x", "0" * 600, "../..", "%00", "None", "ç\u0000", "a b", "<b>", "true", "-1"]


def _esperar_workers(n, limite=3.0):
    t0 = time.time()
    while len(amb.WORKERS) < n and time.time() - t0 < limite:
        time.sleep(0.005)


def f_fila_de_atas(m, r):
    """Fila: gerar varias atas pelas rotas (entrar_na_fila), cancelar algumas pela chave, rodar os workers de verdade
    em sequencia. Item cancelado nao gera nada e volta a pendente; cancelar da fila nunca toca a ata ativa; a fila esvazia."""
    pc = m.pc(r)
    T, FILA = amb.reunioes.TAREFA, amb.reunioes.FILA
    # o que sobrou de rotas sorteadas por outras acoes roda agora, como se as threads tivessem terminado
    with _Fakes():
        for tipo, a in list(amb.WORKERS):
            (amb.PROCESSAR_GRAVACAO_FILA_REAL if tipo == "gravacao" else amb.PROCESSAR_TEXTO_REAL)(*a)
    amb.WORKERS.clear()
    FILA.clear()
    T.update(ativa=False, cancelavel=False, etapa="", erro=None, cancelada=False, titulo="", event_id=None)
    ativa_antes = r.random() < 0.4
    if ativa_antes:
        T.update(ativa=True, etapa="Escrevendo a ata com o Claude", cancelavel=True, event_id="evX", titulo="Em andamento",
                 etapa_desde=time.time() - 120)
    n = r.randint(2, 3)
    evs = r.sample(["ev1", "ev2", "ev3", "ev4", "ev5"], n)
    pasta = amb.config.GRAVACOES_DIR
    itens = []  # (titulo, modo, ev, pid)
    for k in range(n):
        pid = f"reuniao-2030{m.prox():04d}"
        titulo = f"Fila {pid}"
        modo = r.choice(["gravacao", "gravacao", "texto"])
        arquivos = {"voce": pasta / f"{pid}-voce.wav", "outros": pasta / f"{pid}-outros.wav"}
        for c in arquivos.values():
            _wav(c, 1)
        reuniao = {"id": evs[k], "titulo": titulo, "inicio": "2030-01-01T10:00", "cliente_id": None}
        amb.reunioes.salvar_pendente(reuniao, arquivos, "texto ao vivo", 1.0)
        if modo == "texto":
            for c in arquivos.values():
                c.unlink()
        itens.append((titulo, modo, evs[k], pid))
    snap_t = dict(T)
    for k, (titulo, modo, ev, pid) in enumerate(itens):
        pc, resp = chamar(m, r, "post", f"/pendente/{pid}/gerar", data={})
        cobrar(resp.status_code == 302, "I43-gerar-rota", f"{pc}: gerar {pid} -> {resp.status_code}")
        _esperar_workers(k + 1)
        cobrar(len(FILA) == k + 1 and FILA[-1]["titulo"] == titulo and FILA[-1]["event_id"] == ev and not FILA[-1]["cancelada"],
               "I43-entrar-na-fila", f"{pc}: depois de gerar o item {k}: FILA={FILA}")
        if ativa_antes:
            cobrar(dict(T) == snap_t, "I43-fila-mexeu-na-ativa", f"{pc}: entrar na fila com ata ativa mudou TAREFA {snap_t} -> {dict(T)}")
        elif k == 0:
            cobrar(T["ativa"] and T["etapa"] == "Preparando" and T["event_id"] == ev and T["titulo"] == titulo,
                   "I43-preparando", f"{pc}: nada ativo e o primeiro item nao pos 'Preparando': {T}")
            snap_t = dict(T)
        else:
            cobrar(dict(T) == snap_t, "I43-preparando-trocado", f"{pc}: item {k} mudou TAREFA {snap_t} -> {dict(T)}")
    cobrar(len(amb.WORKERS) == n, "I43-workers", f"{pc}: {len(amb.WORKERS)} workers para {n} itens")
    itens_fila = list(FILA)
    for caminho in ("/", "/gravacao"):  # as telas mostram a fila
        pc, resp = chamar(m, r, "get", caminho)
        if resp.status_code == 200:
            corpo = resp.get_data(as_text=True)
            for i in itens_fila:
                if caminho == "/":
                    cobrar(i["chave"] in corpo, "I43-tela-sem-item", f"{pc}: {caminho} nao mostra o item da fila {i['titulo']}")
                cobrar(i["titulo"] in corpo, "I43-tela-sem-titulo", f"{pc}: {caminho} nao mostra {i['titulo']} na fila")
    # cancelar: chave hostil nao faz nada; chave de verdade tira da fila sem tocar a ata ativa
    cancelar = {i["chave"] for i in itens_fila if r.random() < 0.5}
    if r.random() < 0.3 and itens_fila:
        cancelar.add(itens_fila[0]["chave"])
    ligado0 = amb.reunioes.CANCELAR_ATA.is_set()
    ordem = list(itens_fila)
    acoes = [("hostil", c) for c in r.sample(CHAVES_HOSTIS, 3)] + [("real", c) for c in sorted(cancelar)]
    r.shuffle(acoes)
    for tipo, chave in acoes:
        antes_fila, antes_t = [i["chave"] for i in FILA], dict(T)
        pc, resp = chamar(m, r, "post", "/gravacao/cancelar-ata", data={"chave": chave, "voltar": "/"})
        cobrar(resp.status_code == 302, "I43-cancelar-rota", f"{pc}: chave {chave[:20]!r} -> {resp.status_code}")
        cobrar(amb.reunioes.CANCELAR_ATA.is_set() == ligado0, "I43-cancelar-fila-ligou-evento",
               f"{pc}: cancelar com chave {tipo} {chave[:20]!r} mexeu no CANCELAR_ATA (ata ativa: {ativa_antes})")
        cobrar(dict(T) == antes_t, "I43-cancelar-fila-mexeu-na-tarefa", f"{pc}: chave {tipo} {chave[:20]!r}: TAREFA {antes_t} -> {dict(T)}")
        esperada = [c for c in antes_fila if not (tipo == "real" and c == chave)]
        cobrar([i["chave"] for i in FILA] == esperada, "I43-cancelar-fila", f"{pc}: chave {tipo} {chave[:20]!r}: FILA {antes_fila} -> {[i['chave'] for i in FILA]}")
    for i in ordem:
        cobrar(i["cancelada"] == (i["chave"] in cancelar), "I43-cancelada-errada", f"{pc}: {i['titulo']} cancelada={i['cancelada']}")
    # a ata ativa (se havia) termina; os workers rodam um depois do outro, como a trava faria
    if ativa_antes:
        T.update(ativa=False, cancelavel=False)
    registros = list(amb.WORKERS)
    amb.WORKERS.clear()
    for tipo, a in registros:
        item = a[-1]
        reuniao = a[0]
        ev = reuniao["id"]
        nomes_item = ({n: Path(c).name for n, c in a[1].items()} if tipo == "gravacao" else dict(a[2]))
        ata0, tarefas0, audios0 = _ata_bytes(ev), o.linhas("tarefas"), _bytes_dos_arquivos()
        with _Fakes() as f:
            (amb.PROCESSAR_GRAVACAO_FILA_REAL if tipo == "gravacao" else amb.PROCESSAR_TEXTO_REAL)(*a)
        cobrar(item not in FILA, "I43-ficou-na-fila", f"{pc}: {reuniao['titulo']} continua na FILA depois de rodar")
        if item["cancelada"]:
            cobrar(f.whisper == 0 and not f.docs and not f.recebido, "I43-cancelada-gerou",
                   f"{pc}: item cancelado {reuniao['titulo']}: whisper={f.whisper} docs={f.docs} claude={len(f.recebido)}")
            cobrar(_ata_bytes(ev) == ata0, "I43-cancelada-criou-ata", f"{pc}: ata de {ev} mudou com o item cancelado")
            cobrar(o.linhas("tarefas") == tarefas0, "I43-cancelada-importou-tarefas", f"{pc}: tarefas mudaram com o item cancelado")
            cobrar(_bytes_dos_arquivos() == audios0, "I43-cancelada-mexeu-no-audio", f"{pc}: audios mudaram com o item cancelado")
            p = next((x for x in amb.reunioes.listar_pendentes() if x["reuniao"].get("titulo") == reuniao["titulo"]), None)
            cobrar(p is not None, "I43-cancelada-sumiu-da-lista", f"{pc}: {reuniao['titulo']} cancelado na fila nao voltou a listar_pendentes()")
            cobrar(p["arquivos"] == nomes_item, "I43-cancelada-arquivos", f"{pc}: arquivos {p['arquivos']} != {nomes_item}")
        else:
            cobrar(T["etapa"] == "Concluído" and not T["erro"], "I43-nao-cancelada-nao-concluiu", f"{pc}: {reuniao['titulo']}: etapa {T['etapa']!r} erro {T['erro']!r}")
            cobrar(len(f.docs) == 1 and amb.atas.carregar(ev), "I43-nao-cancelada-sem-ata", f"{pc}: {reuniao['titulo']}: docs={f.docs}")
            cobrar(f.whisper == (1 if tipo == "gravacao" else 0), "I43-whisper-contagem", f"{pc}: {tipo}: Whisper {f.whisper}x")
    cobrar(not FILA, "I43-fila-nao-esvaziou", f"{pc}: sobrou na FILA {FILA}")
    cobrar(not T["ativa"] and not T["cancelavel"], "I43-tarefa-ficou-ativa",
           f"{pc}: tudo terminou e TAREFA ativa={T['ativa']} etapa={T['etapa']!r} (cancelados: {len(cancelar)} de {n}, ata ativa antes: {ativa_antes})",
           f"I43-tarefa-ficou-ativa:cancelados={len(cancelar) == n}:{ativa_antes}")
    ligado0 = amb.reunioes.CANCELAR_ATA.is_set()  # os workers limpam o evento ao comecar (legitimo): a partir daqui e a nova referencia
    for chave in [i["chave"] for i in ordem][:2]:  # Cancelar de um item que ja saiu da fila: so aviso
        antes_t = dict(T)
        pc, resp = chamar(m, r, "post", "/gravacao/cancelar-ata", data={"chave": chave})
        cobrar(resp.status_code == 302 and dict(T) == antes_t and amb.reunioes.CANCELAR_ATA.is_set() == ligado0,
               "I43-cancelar-item-que-saiu", f"{pc}: item ja processado: {resp.status_code} TAREFA {antes_t} -> {dict(T)}")
    return f"{pc} fila de {n} (cancelados {len(cancelar)}, ata ativa antes {ativa_antes})"


def f_tarefa_para_tela(m, r):
    """tarefa_para_tela nunca estoura, com qualquer etapa_desde e relogio; a tela escapa titulos hostis da fila."""
    pc = m.pc(r)
    T, FILA = amb.reunioes.TAREFA, amb.reunioes.FILA
    salvo_t, salva_fila = dict(T), list(FILA)
    agora = time.time()
    desde = r.choice([0, 0.0, None, agora, agora - 59, agora - 60, agora - 90, agora - 86400 * 3, agora + 3600, agora + 5, 1e12])
    marca = "<i id=zz>x</i>"
    try:
        T.update(etapa_desde=desde, ativa=r.random() < 0.7, cancelavel=r.random() < 0.5, titulo=r.choice(["", "Reuniao", marca]),
                 etapa=r.choice(["Preparando", "Escrevendo a ata com o Claude", "Transcrevendo o áudio", "Cancelando"]))
        FILA[:] = [{"chave": f"k{k}", "titulo": r.choice(["", "Reuniao", marca]), "event_id": "ev1", "cancelada": False}
                   for k in range(r.randint(0, 3))]
        try:
            t = amb.reunioes.tarefa_para_tela()
        except Exception as e:
            raise o.Achado("I44-tarefa-para-tela-estoura", f"{pc}: etapa_desde={desde!r}: {type(e).__name__}: {e}",
                           f"I44:{type(e).__name__}:{desde is None}")
        cobrar(isinstance(t.get("ha"), str) and isinstance(t.get("fila"), list) and len(t["fila"]) == len(FILA),
               "I44-forma", f"{pc}: etapa_desde={desde!r}: {t.get('ha')!r} {t.get('fila')!r}")
        cobrar("-" not in t["ha"], "I44-ha-negativo", f"{pc}: etapa_desde={desde!r}: {t['ha']!r}", "I44-ha-negativo")
        for caminho in ("/", "/gravacao"):
            pc, resp = chamar(m, r, "get", caminho)
            if resp.status_code == 200:
                cobrar(marca not in resp.get_data(as_text=True), "I44-tela-nao-escapa", f"{pc}: {caminho} deixou o titulo hostil cru")
    finally:
        T.clear()
        T.update(salvo_t)
        FILA[:] = salva_fila
    return f"{pc} tarefa_para_tela etapa_desde={desde!r}"


# ----------------------------------------------------------------------------- convidar mais gente e duracao ao marcar

CONVIDAR_NOVOS = ["n1@y.com", "N1@Y.com", "n2@y.com.br", "N3@Y.COM"]
CONVIDAR_JA = ["a@x.com", "A@X.com", " a@x.com "]
CONVIDAR_INVALIDOS = ["sem-arroba", "a@b", "x@@y.com", "<b>@x.com", "a b@x.com", "@x.com"]


def f_convidar(m, r):
    """Convidar mais pessoas: so os novos (sem caixa, sem repetir, sem invalido) vao; so o organizador; convite do
    Outlook inclui pelo Outlook e o Google nao avisa; voltar hostil nao sai do site."""
    m.pc(r)
    ev = r.choice(amb.EVENTOS + [{"id": "nao-existe"}])
    partes = [r.choice(CONVIDAR_NOVOS) for _ in range(r.randint(0, 3))] + [r.choice(CONVIDAR_JA) for _ in range(r.randint(0, 2))]
    invalidos = [r.choice(CONVIDAR_INVALIDOS) for _ in range(r.choice([0, 0, 0, 1]))]
    partes += invalidos
    r.shuffle(partes)
    SEPS = [",", ";", "\n", ", ", "\r\n"]
    texto = "".join(p + r.choice(SEPS) for p in partes)[:-1] if partes else r.choice(["", " , ,"])
    com_convite = r.random() < 0.4
    voltar = r.choice(["/", "/clientes", "https://evil.example/", "//evil.example", "/\\evil.example", "/\n/evil.example", ""])
    metodo = "get" if r.random() < 0.15 else "post"
    original = envio.convite_do_evento
    if com_convite:
        envio.convite_do_evento = lambda eid: {"entry_id": "entrada-falsa"}
    amb.INCLUIDOS.clear()
    n_envios = len(amb.ENVIOS)
    try:
        if metodo == "post":
            pc, resp = chamar(m, r, "post", f"/reuniao/{ev['id']}/convidar", data={"convidados": texto, "voltar": voltar})
        else:
            pc, resp = chamar(m, r, "get", f"/reuniao/{ev['id']}/convidar", query_string={"voltar": voltar})
    finally:
        envio.convite_do_evento = original
    destino = resp.headers.get("Location", "") if hasattr(resp, "headers") else ""
    cobrar(not _para_fora(destino) if destino else True, "I45-convidar-redireciona-para-fora", f"{pc}: voltar={voltar!r} -> {destino!r}")
    outlook = [a for a in amb.ENVIOS[n_envios:] if a[0] == "incluir_no_convite"]
    google = list(amb.INCLUIDOS)
    pode = bool(ev.get("organizador")) and ev["id"] != "nao-existe"
    novos = {}
    for x in partes:
        x = x.strip()
        if x in CONVIDAR_NOVOS:
            novos.setdefault(x.lower(), x)
    if metodo == "get" or not pode or invalidos or not novos:
        cobrar(not outlook and not google, "I45-convidar-indevido",
               f"{pc}: {ev['id']} {metodo} organizador={pode} invalidos={invalidos} novos={list(novos)} texto={texto!r} enviou outlook={outlook} google={google}",
               f"I45-convidar-indevido:{metodo}:{pode}:{bool(invalidos)}")
        return f"{pc} convidar {ev['id']} recusado"
    cobrar(len(google) == 1, "I45-convidar-google", f"{pc}: {ev['id']} texto={texto!r}: chamadas ao Google {google}")
    enviados = google[0][1]
    cobrar(sorted(x.lower() for x in enviados) == sorted(novos), "I45-convidar-lista",
           f"{pc}: texto={texto!r}: enviou {enviados}, esperado so os novos sem repetir {sorted(novos)}",
           "I45-convidar-lista:" + ("repetido" if len({x.lower() for x in enviados}) != len(enviados) else "outro"))
    cobrar(google[0][2] == (not com_convite), "I45-convidar-aviso", f"{pc}: convite Outlook={com_convite} e Google avisar={google[0][2]}")
    cobrar(len(outlook) == (1 if com_convite else 0), "I45-convidar-outlook", f"{pc}: convite Outlook={com_convite}: {outlook}")
    if com_convite:
        cobrar(list(outlook[0][1][1]) == enviados, "I45-convidar-outlook-lista", f"{pc}: Outlook {outlook[0][1][1]} != Google {enviados}")
    return f"{pc} convidou {enviados} em {ev['id']} (Outlook={com_convite})"


def f_nova_reuniao_duracao(m, r):
    """/reuniao/nova limita a duracao a 5..1440; duracao que nao e numero nao cria nada; o formulario oferece as DURACOES."""
    pc = m.pc(r)
    dur = r.choice(["30", "0", "-5", "4", "5", "480", "1440", "1441", "100000", amb.HUGE if hasattr(amb, "HUGE") else str(2 ** 70), "x", "", "1.5"])
    amb.AGENDADOS.clear()
    pc, resp = chamar(m, r, "post", "/reuniao/nova", data={"data": "2030-01-02", "hora": "10:00", "titulo": "T", "duracao": dur})
    try:
        esperado = max(5, min(int(dur), 24 * 60))
    except ValueError:
        esperado = None
    if esperado is None:
        cobrar(not amb.AGENDADOS, "I46-nova-duracao-invalida-criou", f"{pc}: duracao {dur!r} criou evento {amb.AGENDADOS}")
    else:
        cobrar(len(amb.AGENDADOS) == 1 and amb.AGENDADOS[0][0][2] == esperado, "I46-nova-duracao",
               f"{pc}: duracao {dur!r} -> {[a[0][2] for a in amb.AGENDADOS]}, esperado {esperado}")
    pc, resp = chamar(m, r, "get", "/reuniao/nova")
    if resp.status_code == 200:
        corpo = resp.get_data(as_text=True)
        cobrar('value="480"' in corpo and 'value="30"' in corpo, "I46-nova-opcoes", f"{pc}: o formulario nao oferece as duracoes ate 480")
    return f"{pc} nova reuniao duracao={dur!r}"


# ----------------------------------------------------------------------------- sugerir convidados por cliente

def _norm_titulo(s):
    import unicodedata
    return unicodedata.normalize("NFC", s or "").casefold()


def f_sugerir_convidados_cliente(m, r):
    """sugerir_convidados com cliente: so reunioes atribuidas a esse cliente (ata, e-mail/dominio cadastrado ou nome no
    titulo); nunca estoura com titulos/e-mails/eventos estranhos; determinista."""
    import clientes as C
    pc = m.pc(r)
    nomes = ["Camisetaria", "Linx", "Orca", "Ação", "Café", "Rede Sul"]
    r.shuffle(nomes)
    cads = [{"id": 901 + k, "nome": nomes[k], "emails": r.choice(["", "ana@cam.com", "@linx.com", "ana@cam.com, @linx.com", "BIA@Orca.com,  ", "@gmail.com"])}
            for k in range(r.randint(1, 3))]
    pool = ["ana@cam.com", "Ana@Cam.com", "x@linx.com", "y@LINX.com", "bia@orca.com", "z@gmail.com", "q@outro.com", "eu@meu.com",
            "sala@group.calendar.google.com", "sem-arroba", "", None, "a@b"]
    titulos = ["Reunião Camisetaria - Linx", "reunião linx", "ACAO e cafe", "Ação", "Ação".replace("̧", "̧"), "CAFÉ da manhã",
               None, "", "Orca <b>x</b>", "Rede Sul", "nada a ver", "Linx Camisetaria"]
    eventos = []
    for k in range(r.randint(0, 8)):
        ev = {"titulo": r.choice(titulos), "inicio": r.choice(["2030-01-0%dT10:00" % r.randint(1, 9), "", None, "lixo"])}
        if r.random() < 0.9:
            ev["id"] = r.choice([f"zz{k}", f"zz{k}", "ev1" if k == 0 else f"zy{k}", "ev2" if k == 1 else f"zx{k}", None])  # ids unicos: o Google nunca repete
        if r.random() < 0.9:
            ev["participantes"] = [{"email": r.choice(pool), "nome": r.choice(["", "N", None])} for _ in range(r.randint(0, 4))]
        elif r.random() < 0.5:
            ev["participantes"] = None
        eventos.append(ev)
    cid = r.choice([c["id"] for c in cads])
    c = next(x for x in cads if x["id"] == cid)
    o_clientes, o_cliente = db.clientes, db.cliente
    db.clientes = lambda: [dict(x) for x in cads]
    db.cliente = lambda i: next((dict(x) for x in cads if str(x["id"]) == str(i)), None)
    try:
        try:
            a = C.sugerir_convidados(r.choice([[], ["ana@cam.com"], [""]]), cid, eventos=eventos, eu="eu@meu.com")
            b = C.sugerir_convidados([], cid, eventos=eventos, eu="eu@meu.com")
            atrib = C.clientes_das_reunioes(eventos)
            atrib2 = C.clientes_das_reunioes(eventos)
        except Exception as e:
            raise o.Achado("I47-sugerir-estoura", f"{pc}: {type(e).__name__}: {e} [{o.lugar(e)}] eventos={eventos}",
                           f"I47-estoura:{type(e).__name__}:{o.lugar(e)}")
        cobrar(atrib == atrib2, "I47-nao-determinista", f"{pc}: clientes_das_reunioes mudou entre duas chamadas")
        cadastrados = {e.strip().lower() for e in c["emails"].split(",") if e.strip()}

        def justificada(ev):
            reg = amb.atas.carregar(ev["id"]) if ev.get("id") else None
            if reg and reg.get("cliente_id") == cid:
                return True
            for p in ev.get("participantes") or []:
                em = (p.get("email") or "").lower()
                if "@" in em and (em in cadastrados or "@" + em.split("@", 1)[1] in cadastrados):
                    return True
            return bool(c["nome"]) and _norm_titulo(c["nome"]) in _norm_titulo(ev.get("titulo"))

        legit = [ev for ev in eventos if justificada(ev)]
        legit_mails = {(p.get("email") or "").lower() for ev in legit for p in ev.get("participantes") or []}
        for p in a["pessoas"]:
            if p["reunioes"] > 0:
                cobrar(p["email"] in legit_mails, "I47-pessoa-de-outro-cliente",
                       f"{pc}: cliente {c['nome']!r} ({c['emails']!r}): sugeriu {p['email']} (reunioes={p['reunioes']}) que so esteve em reunioes sem ligacao com ele; eventos={eventos}",
                       "I47-pessoa-de-outro-cliente")
        if a["ultima"]:
            ok = any(ev.get("titulo", "") == a["ultima"]["titulo"] and (ev.get("inicio") or "")[:10] == a["ultima"]["data"] for ev in legit)
            cobrar(ok, "I47-ultima-de-outro-cliente", f"{pc}: cliente {c['nome']!r}: 'ultima' {a['ultima']} nao e de reuniao dele; eventos={eventos}",
                   "I47-ultima-de-outro-cliente")
        cobrar(b["pessoas"] == [x for x in b["pessoas"]] and set(map(lambda p: p["email"], a["pessoas"])) == set(map(lambda p: p["email"], b["pessoas"])),
               "I47-digitados-mudam-com-cliente", f"{pc}: com cliente, os e-mails digitados mudaram a lista")
    finally:
        db.clientes, db.cliente = o_clientes, o_cliente
    return f"{pc} sugerir cliente {c['nome']!r} {len(eventos)} eventos -> {len(a['pessoas'])} pessoas"


ACOES6 = A5.ACOES5 + [
    Acao("tirar copia de seguranca", f_tirar_copia, 2, cobre=["db.copia_de_seguranca"]),
    Acao("voltar para uma copia (e desfazer)", f_voltar_copia, 3, False, ["/copias/voltar", "db.restaurar_copia", "db.copias"]),
    Acao("voltar para copia hostil", f_voltar_copia_hostil, 1, True, ["/copias/voltar", "/copias", "db.copias"]),
    Acao("reconectar Google", f_reconectar_google, 2, False, ["/google/conectar", "/api/google"]),
    Acao("PC parado e esquecer PC", f_pc_parado, 2, True, ["/sincronia/esquecer-pc"]),
    Acao("sinal de vida hostil no Drive", f_sinal_de_vida_hostil, 1, True, ["sincronia._sinal_de_vida"]),
    Acao("cancelar reuniao", f_cancelar_reuniao, 2, True, ["/reuniao/<event_id>/cancelar"]),
    Acao("editar ata", f_editar_ata, 3, False, ["/ata/<event_id>/editar", "db.atualizar_acoes_da_ata"]),
    Acao("cancelar a criacao da ata", f_cancelar_ata, 3, False, ["/gravacao/cancelar-ata", "reunioes.listar_pendentes"]),
    Acao("cancelar a ata com voltar hostil", f_cancelar_ata_voltar_hostil, 1, True, ["/gravacao/cancelar-ata"]),
    Acao("transcricao reaproveitada e falha do Claude", f_transcricao_reaproveitada, 3, False, ["reunioes.transcricao_ja_feita"]),
    Acao("fila de atas (gerar, cancelar na fila, rodar)", f_fila_de_atas, 3, True, ["/pendente/<pid>/gerar", "/gravacao/cancelar-ata", "reunioes.entrar_na_fila"]),
    Acao("tarefa_para_tela e fila na tela", f_tarefa_para_tela, 2, True, ["/gravacao", "reunioes.tarefa_para_tela"]),
    Acao("convidar mais pessoas", f_convidar, 3, True, ["/reuniao/<event_id>/convidar"]),
    Acao("nova reuniao: duracao", f_nova_reuniao_duracao, 1, True, ["/reuniao/nova"]),
    Acao("sugerir convidados por cliente", f_sugerir_convidados_cliente, 2, True, ["clientes.sugerir_convidados", "clientes.clientes_das_reunioes"]),
]
SEM_ACAO = A5.SEM_ACAO
SEM_FUZZ = A5.SEM_FUZZ
