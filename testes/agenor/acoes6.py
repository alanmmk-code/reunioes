"""Catalogo v6 = catalogo v5 (acoes5.py, CONGELADO) + copias de seguranca (voltar e desfazer), "Reconectar Google"
e o aviso de PC que parou de sincronizar."""
import time
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
    voltar = r.choice(["/", "/clientes", "https://evil.example/", "//evil.example", "/\\evil.example", ""])
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


ACOES6 = A5.ACOES5 + [
    Acao("tirar copia de seguranca", f_tirar_copia, 2, cobre=["db.copia_de_seguranca"]),
    Acao("voltar para uma copia (e desfazer)", f_voltar_copia, 3, False, ["/copias/voltar", "db.restaurar_copia", "db.copias"]),
    Acao("voltar para copia hostil", f_voltar_copia_hostil, 1, True, ["/copias/voltar", "/copias", "db.copias"]),
    Acao("reconectar Google", f_reconectar_google, 2, False, ["/google/conectar", "/api/google"]),
    Acao("PC parado e esquecer PC", f_pc_parado, 2, True, ["/sincronia/esquecer-pc"]),
    Acao("sinal de vida hostil no Drive", f_sinal_de_vida_hostil, 1, True, ["sincronia._sinal_de_vida"]),
    Acao("cancelar reuniao", f_cancelar_reuniao, 2, True, ["/reuniao/<event_id>/cancelar"]),
    Acao("editar ata", f_editar_ata, 3, False, ["/ata/<event_id>/editar", "db.atualizar_acoes_da_ata"]),
]
SEM_ACAO = A5.SEM_ACAO
SEM_FUZZ = A5.SEM_FUZZ
