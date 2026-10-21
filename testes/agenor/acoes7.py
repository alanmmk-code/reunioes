"""Catalogo v7 = catalogo v6 (acoes6.py, CONGELADO) + as novidades do commit f24fd3e:
aviso de som mudo da extensao, vinculos de convite do Outlook entre PCs, editar ata com o Google Doc alterado fora
do painel e arquivamento de videos antigos."""
import json
import os
import threading as _threading
import time as _time
from datetime import datetime, timedelta, timezone

import acoes6 as A6
import ambiente as amb
import db
import envio
import oraculo as o
from acoes import Acao, chamar
from oraculo import cobrar

g = amb.g
sincronia = amb.sincronia
reunioes = amb.reunioes
monitor = amb.monitor
config = amb.config


# ----------------------------------------------------------------------------- 1. som mudo da chamada

ABA = 7
# (valor no JSON, "som"?) : o que o painel tem de entender como som (pico > SOM_MINIMO) ou silencio
PICOS = [(0, False), (0.0, False), (None, False), (0.01, False), (0.0100001, True), (0.5, True), (1e9, True), (-1, False),
         ("0.5", True), ("abc", False), ("", False), ([], False), ({}, False), ([1], False), (True, True), ("nan", False)]
PASSOS_T = [1, 5, 60, 179, 180, 181, 400]


class _Relogio:
    """Substitui `reunioes.time`: o tempo e nosso, o resto (sleep, monotonic...) e do modulo de verdade."""
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def __getattr__(self, nome):
        return getattr(_time, nome)


class _ThreadFalsa:
    """So executa (na hora) a thread do aviso; qualquer outra (encerrar a gravacao...) e descartada."""
    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target is NOTIFICACOES["fn"]:
            self.target(*self.args, **self.kwargs)

    def join(self, *a, **k):
        pass


NOTIFICACOES = {"fn": None, "lista": []}


class _Threading:
    Thread = _ThreadFalsa

    def __getattr__(self, nome):
        return getattr(_threading, nome)


def _corpo_extensao(r, aba, em_chamada, pico, lixo):
    """(json, aba_certa?, em_chamada?, som?) ; lixo: corpo que nao e dict."""
    if lixo:
        return r.choice([[], [1, 2], "texto", 5, None, "{"]), None
    d = {"aba": aba, "em_chamada": em_chamada, "titulo": r.choice(["Reuniao", "", None, 5]), "pico": pico}
    if r.random() < 0.3:
        d["trilhas"] = r.choice([[], "x", 3, [{"a": 1}], None])
    if r.random() < 0.3:
        d["diag"] = r.choice(["texto", 5, [], {"a": 1}, None, {"trilhas": [1, 2]}])
    if r.random() < 0.15:
        d["audio_ctx"] = r.choice(["running", 5, None])
    if r.random() < 0.1:
        d.pop("pico")
    if r.random() < 0.05:
        d["fim"] = True
    return d, None


def f_som_mudo(m, r):
    """Aviso de som mudo: gravando pela extensao, na aba certa, em chamada, pico <= SOM_MINIMO por >= SOM_MUDO_SEG:
    monitor.notificar uma vez so e /gravacao mostra o aviso; som limpa; outra aba, sem gravacao ou fora de chamada
    nunca avisa; corpo/pico/diag lixo nunca da 500."""
    pc = m.pc(r)
    G = reunioes.GRAVADOR
    cenario = r.choices(["certo", "manual", "sem_gravacao", "nao_automatica"], [6, 1, 1, 1])[0]
    salvo_g = {k: getattr(G, k) for k in ("ativo", "automatica", "pela_extensao", "reuniao", "inicio")}
    salvo_e = dict(reunioes.EXTENSAO)
    salvo = (reunioes.time, reunioes.threading, monitor.notificar, reunioes.ao_detectar_meet)
    rel = _Relogio()
    chamadas_detectar = []
    NOTIFICACOES["lista"] = lista = []
    NOTIFICACOES["fn"] = lambda *a, **k: lista.append(a)
    SEG, MIN = reunioes.SOM_MUDO_SEG, reunioes.SOM_MINIMO
    try:
        reunioes.time, reunioes.threading = rel, _Threading()
        monitor.notificar = NOTIFICACOES["fn"]
        reunioes.ao_detectar_meet = lambda *a, **k: chamadas_detectar.append(a)
        G.ativo, G.automatica, G.pela_extensao = {"certo": (True, True, True), "manual": (True, False, False),
                                                  "nao_automatica": (True, False, True), "sem_gravacao": (False, False, False)}[cenario]
        G.reuniao = {"titulo": "Reuniao teste", "id": "ev1"}
        G.inicio = datetime.now()
        pelo_ext = bool(G.ativo and G.automatica and G.pela_extensao)
        reunioes.EXTENSAO.update(aba=ABA, som_em=rel.t, mudo_avisado=False, fora_desde=None, log_em=0.0, aba_sinal_em=rel.t, pico=0.0)
        # modelo
        som_em, avisado, aba_ok, fora, esperadas = rel.t, False, True, None, 0
        passos = r.randint(2, 7)
        ultimo_resp = None
        for k in range(passos):
            rel.t += r.choice(PASSOS_T)
            lixo = r.random() < 0.12
            outra = r.random() < 0.15
            em_chamada = r.random() < 0.85
            valor, som = r.choice(PICOS) if r.random() < 0.55 else (0, False)
            aba = r.choice([8, "7", None, 0]) if outra else ABA
            corpo, _ = _corpo_extensao(r, aba, em_chamada, valor, lixo)
            if isinstance(corpo, dict) and "pico" not in corpo:
                som = False
            if r.random() < 0.08:
                resp = m.cli.post("/api/extensao/estado", data="nao e json{", content_type="application/json")
                lixo, em_chamada = True, False
            else:
                try:
                    resp = m.cli.post("/api/extensao/estado", json=corpo)
                except amb.RedeBloqueada:
                    continue
            cobrar(resp.status_code == 200, "I48-extensao-estado-5xx", f"{pc}: corpo {corpo!r} -> {resp.status_code}", "I48-estado-5xx")
            # o modelo (so vale gravando pela extensao; senao nada avisa)
            if pelo_ext and not lixo and not outra and aba_ok:
                if em_chamada:
                    fora = None
                if som:
                    som_em, avisado = rel.t, False
                elif em_chamada and rel.t - som_em >= SEG and not avisado:
                    avisado = True
                    esperadas += 1
                if not em_chamada:
                    fora = fora or rel.t
                    if corpo.get("fim") or rel.t - fora >= monitor.FIM_APOS:
                        aba_ok = False  # o painel esquece a aba: dai em diante um corpo sem "aba" a casaria; para por aqui
            e = reunioes.EXTENSAO
            cobrar(len(lista) == esperadas, "I48-aviso-mudo-contagem",
                   f"{pc}: cenario={cenario} passo {k}: notificou {len(lista)}x, esperado {esperadas}x (t={rel.t - 1_000_000.0:.0f}s desde o inicio, "
                   f"corpo {corpo!r})", f"I48-aviso-mudo-contagem:{cenario}:{len(lista) > esperadas}")
            if pelo_ext and aba_ok:
                cobrar(bool(e["mudo_avisado"]) == avisado, "I48-aviso-mudo-estado", f"{pc}: mudo_avisado={e['mudo_avisado']} esperado {avisado} (corpo {corpo!r})",
                       "I48-aviso-mudo-estado")
            ultimo_resp = resp
            if not aba_ok:
                break
        # nunca avisa fora do caso certo
        if cenario != "certo":
            cobrar(not lista, "I48-aviso-mudo-indevido", f"{pc}: cenario={cenario} notificou {len(lista)}x", f"I48-aviso-mudo-indevido:{cenario}")
        if cenario == "sem_gravacao":
            cobrar(not G.ativo, "I48-sem-gravacao", "gravacao nao deveria estar ativa")
        # a tela da gravacao
        if G.ativo:
            tela = m.cli.get("/gravacao")
            cobrar(tela.status_code == 200, "I48-gravacao-tela-5xx", f"{pc}: GET /gravacao -> {tela.status_code}", "I48-gravacao-tela")
            texto = tela.get_data(as_text=True)
            mostra = "não está chegando" in texto
            cobrar(mostra == bool(pelo_ext and reunioes.EXTENSAO["mudo_avisado"]), "I48-gravacao-aviso-na-tela",
                   f"{pc}: cenario={cenario} mudo_avisado={reunioes.EXTENSAO['mudo_avisado']} e a tela {'mostra' if mostra else 'nao mostra'} o aviso")
        if pelo_ext:  # gravacao que deixa de ser pela extensao esconde o aviso, mesmo com a marca velha ligada
            reunioes.EXTENSAO["mudo_avisado"] = True
            G.pela_extensao = False
            texto = m.cli.get("/gravacao").get_data(as_text=True)
            cobrar("não está chegando" not in texto, "I48-gravacao-aviso-velho", f"{pc}: gravacao fora da extensao mostrou o aviso do som mudo")
        if cenario == "sem_gravacao":  # comecar a gravar zera o aviso e o relogio do silencio
            reunioes.EXTENSAO["mudo_avisado"] = True
            rel.t += 1000
            m.cli.post("/api/extensao/estado", json={"aba": ABA, "em_chamada": True, "pico": 0})
            cobrar(reunioes.EXTENSAO["mudo_avisado"] is False and reunioes.EXTENSAO["som_em"] == rel.t, "I48-comecar-zera-aviso",
                   f"{pc}: ao comecar a gravar mudo_avisado={reunioes.EXTENSAO['mudo_avisado']} som_em={reunioes.EXTENSAO['som_em']}")
            cobrar(len(chamadas_detectar) >= 1, "I48-comecar-nao-detectou", f"{pc}: em chamada sem gravacao nao tentou gravar")
            cobrar(not lista, "I48-sem-gravacao-notificou", f"{pc}: sem gravacao notificou {len(lista)}x")
    finally:
        reunioes.time, reunioes.threading, monitor.notificar, reunioes.ao_detectar_meet = salvo
        for k, v in salvo_g.items():
            setattr(G, k, v)
        reunioes.EXTENSAO.clear()
        reunioes.EXTENSAO.update(salvo_e)
    return f"{pc} som mudo cenario={cenario}"


# ----------------------------------------------------------------------------- 2. vinculos de convite entre PCs

T = ["2030-01-01T10:00:00.000+00:00", "2030-01-01T10:00:00.001+00:00", "2030-01-02T09:00:00.000+00:00",
     "2030-02-01T00:00:00.000+00:00", "2030-03-01T00:00:00.000+00:00"]
ENVIADOS = ["2030-01-01T10:00:00", "2030-01-05T10:00:00", "2030-02-10T10:00:00"]


def _convite(r, entry=None):
    c = {"remetente": r.choice(["a@x.com", "b@x.com"]), "entry_id": entry or r.choice(["E1", "E2", "E3", "E4"]),
         "titulo": r.choice([None, "Reuniao 1", "Reuniao 2"]), "inicio": r.choice([None, "2030-01-01T10:00", "2030-02-03T14:30"])}
    if r.random() < 0.8:
        c["atualizado_em"] = r.choice(T)
    if r.random() < 0.8:
        c["enviado_em"] = r.choice(ENVIADOS)
    if r.random() < 0.25:
        c["cancelado"] = True
    return c


LIXO_CONVITE = [None, 5, "texto", [], ["E1"], {}, {"entry_id": 5}, {"entry_id": None}, {"entry_id": ["E1"]}, {"remetente": "a@x.com"},
                {"entry_id": {"a": 1}, "atualizado_em": T[4]}]


def _quando(c):
    return str(c.get("atualizado_em") or c.get("enviado_em") or "")


def _dicionario(r, n, lixo):
    d = {}
    for k in range(n):
        d[f"ev{r.randint(1, 5)}"] = r.choice(LIXO_CONVITE) if lixo and r.random() < 0.3 else _convite(r)
    return d


def _valido(c):
    return isinstance(c, dict) and isinstance(c.get("entry_id"), str)


def f_vinculos_de_convite(m, r):
    """Vinculos evento->convite (envio.py): mesclar idempotente e comutativo por evento (vale o mais recente), ignora lixo,
    cancelado nao volta por versao mais velha e convite_do_evento nunca devolve cancelado. Usa as funcoes ORIGINAIS
    (copia de envio.py; o modulo envio do ambiente tem tudo trocado por falso). Nada toca o Outlook."""
    e = amb.envio_real()
    cobrar(not hasattr(e.mesclar_convites, "__wrapped__") and e.mesclar_convites.__module__ == "envio_real_agenor", "I49-ambiente", "copia real de envio")
    base = amb.RUN["dir"]
    e._CONVITES = base / "convites_a.json"
    local = _dicionario(r, r.randint(0, 5), lixo=r.random() < 0.3)
    remoto_dic = _dicionario(r, r.randint(0, 5), lixo=r.random() < 0.4)
    remoto = remoto_dic if r.random() < 0.9 else r.choice([None, [], "x", 5, [remoto_dic]])
    e._escrever(local)
    antes = e._ler()
    try:
        res = e.mesclar_convites(remoto)
    except Exception as ex:
        raise o.Achado("I49-mesclar-estoura", f"mesclar_convites({remoto!r}) com local {local!r}: {type(ex).__name__}: {ex}", f"I49-mesclar-estoura:{type(ex).__name__}")
    cobrar(e._ler() == res, "I49-mesclar-nao-gravou", "mesclar_convites devolve algo diferente do que gravou")
    rem = remoto if isinstance(remoto, dict) else {}
    for ev in set(antes) | set(rem):
        cand = [c for c in ([antes.get(ev)] if isinstance(antes.get(ev), dict) else []) + ([rem[ev]] if ev in rem and _valido(rem[ev]) else [])]
        if not cand:
            cobrar(res.get(ev) == antes.get(ev), "I49-mesclar-mexeu-no-lixo", f"{ev}: {antes.get(ev)!r} -> {res.get(ev)!r}")
            continue
        melhor = max(_quando(c) for c in cand)
        cobrar(isinstance(res.get(ev), dict) and _quando(res[ev]) == melhor and res[ev] in cand, "I49-mesclar-nao-vale-o-mais-recente",
               f"{ev}: local={antes.get(ev)!r} remoto={rem.get(ev)!r} -> {res.get(ev)!r}", "I49-mesclar-mais-recente")
        if isinstance(rem.get(ev), dict) and not _valido(rem[ev]):
            cobrar(res.get(ev) == antes.get(ev), "I49-mesclar-aceitou-lixo", f"{ev}: remoto sem entry_id {rem[ev]!r} entrou: {res.get(ev)!r}")
        if ev in rem and not _valido(rem[ev]) and ev not in antes:
            cobrar(ev not in res, "I49-mesclar-aceitou-lixo", f"{ev}: remoto sem entry_id entrou: {res.get(ev)!r}")
    # idempotente
    de_novo = e.mesclar_convites(remoto)
    cobrar(de_novo == res, "I49-mesclar-nao-idempotente", f"mesclar duas vezes mudou: {res!r} -> {de_novo!r}")
    # comutativo no que importa: o momento vencedor de cada evento (so com os dois lados validos)
    if isinstance(remoto, dict):
        A = {k: v for k, v in local.items() if _valido(v)}
        B = {k: v for k, v in remoto.items() if _valido(v)}
        e._escrever(A)
        ab = e.mesclar_convites(B)
        e._escrever(B)
        ba = e.mesclar_convites(A)
        cobrar(set(ab) == set(ba) and all(_quando(ab[k]) == _quando(ba[k]) for k in ab), "I49-mesclar-nao-comutativo",
               f"A={A!r} B={B!r}: A<-B {ab!r}  B<-A {ba!r}", "I49-mesclar-nao-comutativo")
        # cancelado nao volta por versao mais velha / cancelamento mais novo vence
        for ev in set(A) & set(B):
            a, b = A[ev], B[ev]
            if a.get("entry_id") == b.get("entry_id") and _quando(a) != _quando(b):
                novo, velho = (a, b) if _quando(a) > _quando(b) else (b, a)
                e._escrever({ev: dict(velho)})
                e.mesclar_convites({ev: dict(novo)})
                final = e.convite_do_evento(ev)
                cobrar((final is None) == bool(novo.get("cancelado")), "I49-cancelado-voltou",
                       f"{ev}: novo={novo!r} velho={velho!r} -> convite_do_evento {final!r}", "I49-cancelado-voltou")
                e._escrever({ev: dict(novo)})
                e.mesclar_convites({ev: dict(velho)})
                cobrar((e.convite_do_evento(ev) is None) == bool(novo.get("cancelado")), "I49-cancelado-voltou-por-versao-velha",
                       f"{ev}: novo={novo!r} velho={velho!r} depois de mesclar o velho -> {e.convite_do_evento(ev)!r}", "I49-cancelado-voltou")
    # convite_do_evento nunca devolve cancelado nem lixo
    e._escrever(local)
    for ev in list(local) + ["nao-existe"]:
        c = e.convite_do_evento(ev)
        cobrar(c is None or (isinstance(c, dict) and not c.get("cancelado")), "I49-convite-do-evento-cancelado", f"{ev}: {c!r}")
    # registrar / completar / anotar
    ev = f"evR{r.randint(1, 3)}"
    ini = r.choice([datetime(2030, 2, 3, 14, 30), datetime(2030, 2, 3, 14, 30, tzinfo=timezone.utc), None, "texto", datetime(9999, 12, 31, 23, 59)])
    e._escrever({})
    antigo = r.random() < 0.2  # vinculo antigo: registrado sem assunto nem horario
    e.registrar_convite(ev, "a@x.com", "ER1", *(() if antigo else ("Titulo", ini)))
    ini = None if antigo else ini
    c = e.convite_do_evento(ev)
    cobrar(c and c["entry_id"] == "ER1" and c["remetente"] == "a@x.com" and c.get("atualizado_em") and c.get("enviado_em"), "I49-registrar", f"{c!r}")
    if isinstance(ini, datetime):
        cobrar(c.get("inicio") is not None and len(c["inicio"]) == 16 and c["inicio"][10] == "T", "I49-registrar-inicio", f"inicio {ini!r} -> {c.get('inicio')!r}")
    else:
        cobrar(c.get("inicio") is None, "I49-registrar-inicio", f"inicio {ini!r} -> {c.get('inicio')!r}")
    outro = e.convite_do_evento("evOutro")
    cobrar(outro is None, "I49-registrar-outro", f"evento nao registrado devolveu {outro!r}")
    antes_c = dict(c)
    novo_ini = datetime(2031, 1, 1, 8, 0)
    e.completar_convite(ev, "Titulo Novo", novo_ini)
    depois = e.convite_do_evento(ev)
    if antes_c.get("titulo") and antes_c.get("inicio"):
        cobrar(depois == antes_c, "I49-completar-sobrescreveu", f"{antes_c!r} -> {depois!r}")
    else:
        cobrar(depois["titulo"] == "Titulo Novo" and depois["inicio"] == "2031-01-01T08:00", "I49-completar-nao-completou", f"{antes_c!r} -> {depois!r}")
    e.completar_convite("evNaoExiste", "T", novo_ini)
    e.completar_convite(ev, "", novo_ini)
    e.completar_convite(ev, "T", None)
    cobrar(e.convite_do_evento("evNaoExiste") is None, "I49-completar-criou", "completar_convite criou vinculo de evento que nao existia")
    outro_c = _convite(r, entry="ER2")
    e._escrever({ev: e._ler()[ev], "evLixo": r.choice(LIXO_CONVITE), "evOutroConvite": outro_c})
    e._anotar_convite("ER1", cancelado=True)
    cobrar(e.convite_do_evento(ev) is None, "I49-cancelar-anotado", "convite cancelado ainda aparece")
    cobrar(e._ler()["evOutroConvite"] == outro_c, "I49-anotar-mexeu-em-outro", f"anotar o convite ER1 mexeu no ER2: {outro_c!r} -> {e._ler()['evOutroConvite']!r}")
    return f"vinculos: local={len(local)} remoto={'dict' if isinstance(remoto, dict) else type(remoto).__name__}"


# ----------------------------------------------------------------------------- 3. remarcar/cancelar/convidar: o Outlook vem antes do Google

def f_outlook_antes_do_google(m, r):
    """Com convite do Outlook, o Outlook e chamado ANTES do Google (e o vinculo e completado antes de mexer); se o Outlook
    falhar, o Google nao e alterado e a rota nao estoura; sem convite do Outlook, o Outlook nunca e chamado."""
    pc = m.pc(r)
    ev = r.choice(amb.EVENTOS)
    acao = r.choice(["remarcar", "cancelar", "convidar"])
    com_convite = r.random() < 0.6
    falha = com_convite and r.random() < 0.4
    ordem = []
    salvos = {n: getattr(envio, n) for n in ("convite_do_evento", "completar_convite", "atualizar_convite", "cancelar_convite", "incluir_no_convite")}
    salvos_g = {n: getattr(g, n) for n in ("remarcar_evento", "cancelar_evento", "incluir_convidados")}

    def outlook(nome):
        def f(*a, **k):
            ordem.append(("outlook", nome))
            if falha:
                raise RuntimeError("Outlook falhou no teste")
        return f

    def google(nome):
        def f(*a, **k):
            ordem.append(("google", nome, k.get("avisar", True) if nome != "remarcar_evento" else (a[4] if len(a) > 4 else k.get("avisar", True))))
            return salvos_g[nome](*a, **k)
        return f

    try:
        envio.convite_do_evento = lambda eid: {"entry_id": "entrada-falsa"} if com_convite else None
        envio.completar_convite = lambda *a, **k: ordem.append(("completar",))
        envio.atualizar_convite = outlook("atualizar")
        envio.cancelar_convite = outlook("cancelar")
        envio.incluir_no_convite = outlook("incluir")
        g.remarcar_evento, g.cancelar_evento, g.incluir_convidados = google("remarcar_evento"), google("cancelar_evento"), google("incluir_convidados")
        if acao == "remarcar":
            pc, resp = chamar(m, r, "post", f"/reuniao/{ev['id']}/remarcar", data={"data": "2030-02-03", "hora": "14:30", "duracao": "45"})
        elif acao == "cancelar":
            pc, resp = chamar(m, r, "post", f"/reuniao/{ev['id']}/cancelar", data={})
        else:
            pc, resp = chamar(m, r, "post", f"/reuniao/{ev['id']}/convidar", data={"convidados": "novo.convite@y.com"})
    finally:
        for n, f in salvos.items():
            setattr(envio, n, f)
        for n, f in salvos_g.items():
            setattr(g, n, f)
    pode = bool(ev.get("organizador")) and not (acao == "remarcar" and ev.get("dia_inteiro"))
    if resp.status_code == 0:
        return f"{pc} {acao} {ev['id']}: rede bloqueada"
    if not pode:
        cobrar(not ordem, "I50-sem-permissao-mexeu", f"{pc}: {acao} {ev['id']} (nao organizador) -> {ordem}")
        return f"{pc} {acao} {ev['id']} recusado"
    nome_outlook = {"remarcar": "atualizar", "cancelar": "cancelar", "convidar": "incluir"}[acao]
    nome_google = {"remarcar": "remarcar_evento", "cancelar": "cancelar_evento", "convidar": "incluir_convidados"}[acao]
    if not com_convite:
        cobrar(ordem == [("google", nome_google, True)], "I50-sem-convite-caminho", f"{pc}: {acao} sem convite do Outlook -> {ordem}")
        return f"{pc} {acao} {ev['id']} so Google"
    if falha:
        cobrar(ordem == [("completar",), ("outlook", nome_outlook)], "I50-outlook-falhou-google-mexeu", f"{pc}: {acao} com Outlook falhando -> {ordem}", f"I50-outlook-falhou-google-mexeu:{acao}")
    else:
        cobrar(ordem == [("completar",), ("outlook", nome_outlook), ("google", nome_google, False)], "I50-ordem-outlook-google",
               f"{pc}: {acao} com convite do Outlook -> {ordem}", f"I50-ordem:{acao}")
    return f"{pc} {acao} {ev['id']} outlook falha={falha}"


# ----------------------------------------------------------------------------- 4. editar ata com o Google Doc mexido fora do painel

DELTAS_DOC = [-3600, -1, 0, 179, 180, 180, 181, 181, 3600, 86400]
_doc_n = {"n": 0}


def f_editar_ata_doc_mexido(m, r):
    """Editar ata com o Doc alterado fora do painel (mais de 3 min depois do que o painel gravou): sem substituir=1 nao
    salva, nao atualiza o Doc, nao mexe nas tarefas e devolve 200 com o que foi digitado; com substituir=1 salva e grava
    doc_salvo_em; doc_modificado_em que levanta excecao nunca da 500 nem bloqueia."""
    pc = m.pc(r)
    ids = sorted(amb.config.ATAS_DIR.glob("*.json"))
    if not ids:
        chamar(m, r, "post", "/manual/ev1", data={"texto": "texto da reuniao", "titulo": ""})
        ids = sorted(amb.config.ATAS_DIR.glob("*.json"))
    if not ids:
        return f"{pc} sem ata"
    ev = r.choice([p.stem for p in ids])
    reg = amb.atas.carregar(ev)
    if not reg or not isinstance(reg.get("ata"), dict):
        return f"{pc} ata {ev} ilegivel"
    _doc_n["n"] += 1
    n = _doc_n["n"]
    base = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=r.randint(0, 5000))
    forma = r.choice(["salvo"] * 4 + ["gerada"] * 3 + ["lixo", "nada"])
    reg["doc_link"] = reg.get("doc_link") or "https://docs.google.com/document/d/" + "A" * 25 + "/edit"
    reg.pop("doc_salvo_em", None)
    if forma == "salvo":
        reg["doc_salvo_em"] = base.isoformat()
        base_ok = base
    elif forma == "gerada":
        reg["gerada_em"] = base.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        base_ok = base
    elif forma == "lixo":
        reg["doc_salvo_em"] = "lixo"
        base_ok = None
    else:
        reg["gerada_em"] = ""
        base_ok = None
    amb.atas.salvar(ev, reg)
    reg_antes = amb.atas.carregar(ev)
    modo = r.choice(["horario"] * 8 + ["none", "erro"])
    delta = r.choice(DELTAS_DOC)
    alterado = base + timedelta(seconds=delta)
    mexido = modo == "horario" and base_ok is not None and delta > 180

    def falso(link):
        if modo == "erro":
            raise RuntimeError("Drive indisponivel no teste")
        return alterado if modo == "horario" else None

    salvo_fn = g.doc_modificado_em
    g.doc_modificado_em = falso
    tarefas_antes = o.linhas("tarefas")
    docs_antes = len(amb.DOCS_ATUALIZADOS)
    try:
        # GET: o aviso aparece so quando o Doc foi mexido
        pc, resp = chamar(m, r, "get", f"/ata/{ev}/editar")
        cobrar(resp.status_code == 200, "I51-editar-get", f"{pc}: GET editar {ev} ({modo}, forma={forma}, delta={delta}) -> {resp.status_code}", "I51-editar-get")
        corpo = resp.get_data(as_text=True)
        cobrar(("name=\"substituir\"" in corpo) == mexido, "I51-editar-aviso-na-tela",
               f"{pc}: forma={forma} modo={modo} delta={delta}s: Doc mexido={mexido} e a tela {'tem' if 'name=\"substituir\"' in corpo else 'nao tem'} a confirmacao",
               f"I51-aviso-na-tela:{forma}:{modo}")
        ata_velha = reg_antes["ata"]
        form = {"titulo": f"TituloDoc{n}x", "resumo": f"ResumoDoc{n}x", "participantes": "A\nB", "decisoes": "d1", "pontos": "",
                "prox_pauta": "", "prox_data": "", "topico_titulo": ["t1"], "topico_texto": ["x"],
                "acao_tarefa": [a["tarefa"] for a in ata_velha.get("acoes", [])] + [f"acao doc {n}"],
                "acao_resp": ["Joao"] * (len(ata_velha.get("acoes", [])) + 1), "acao_prazo": [""] * (len(ata_velha.get("acoes", [])) + 1),
                "acao_minha": ["1"] * (len(ata_velha.get("acoes", [])) + 1)}
        substituir = r.choice(["1", "1", "", "0", "true", None])
        if substituir is not None:
            form["substituir"] = substituir
        pc, resp = chamar(m, r, "post", f"/ata/{ev}/editar", data=form)
    finally:
        g.doc_modificado_em = salvo_fn
    depois = amb.atas.carregar(ev)
    if mexido and substituir != "1":
        cobrar(resp.status_code == 200, "I51-mexido-nao-devolveu-200", f"{pc}: Doc mexido sem substituir=1 -> {resp.status_code}")
        cobrar(depois == reg_antes, "I51-mexido-salvou-assim-mesmo", f"{pc}: Doc mexido (+{delta}s) sem substituir=1 e a ata mudou: {depois.get('ata', {}).get('titulo')!r}", "I51-mexido-salvou")
        cobrar(len(amb.DOCS_ATUALIZADOS) == docs_antes, "I51-mexido-atualizou-o-doc", f"{pc}: Doc mexido sem substituir=1 e atualizar_google_doc foi chamado")
        cobrar(o.linhas("tarefas") == tarefas_antes, "I51-mexido-mexeu-nas-tarefas", f"{pc}: Doc mexido sem substituir=1 e as tarefas mudaram")
        txt = resp.get_data(as_text=True)
        cobrar(f"TituloDoc{n}x" in txt and f"ResumoDoc{n}x" in txt and f"acao doc {n}" in txt, "I51-mexido-perdeu-o-digitado",
               f"{pc}: a tela devolvida nao traz o que foi digitado")
        cobrar("name=\"substituir\"" in txt, "I51-mexido-sem-confirmacao", f"{pc}: a tela devolvida nao pede confirmacao")
        return f"{pc} editar ata {ev}: Doc mexido (+{delta}s), bloqueado"
    cobrar(resp.status_code in (302, 303), "I51-editar-rota", f"{pc}: forma={forma} modo={modo} delta={delta}s substituir={substituir!r} -> {resp.status_code}", f"I51-editar-rota:{modo}")
    cobrar(depois["ata"]["titulo"] == f"TituloDoc{n}x", "I51-editar-nao-salvou", f"{pc}: modo={modo} delta={delta}s substituir={substituir!r}: titulo {depois['ata']['titulo']!r}", f"I51-editar-nao-salvou:{modo}")
    cobrar(len(amb.DOCS_ATUALIZADOS) == docs_antes + 1, "I51-editar-nao-atualizou-o-doc", f"{pc}: salvou e atualizar_google_doc rodou {len(amb.DOCS_ATUALIZADOS) - docs_antes}x")
    cobrar(any(t["descricao"] == db._descricao(f"acao doc {n}") for t in o.linhas("tarefas") if not t["excluido"]), "I51-editar-acao-sem-tarefa", f"{pc}: acao nova sem tarefa")
    if modo == "horario":
        cobrar(depois.get("doc_salvo_em") == alterado.isoformat(), "I51-doc-salvo-em", f"{pc}: doc_salvo_em {depois.get('doc_salvo_em')!r}, esperado {alterado.isoformat()!r}")
    elif modo == "none":
        cobrar(depois.get("doc_salvo_em") is None, "I51-doc-salvo-em-none", f"{pc}: doc_salvo_em {depois.get('doc_salvo_em')!r} com Doc sem horario")
    return f"{pc} editar ata {ev}: modo={modo} mexido={mexido} substituir={substituir!r}"


# ----------------------------------------------------------------------------- 5. arquivar videos antigos

def f_arquivar_videos(m, r):
    """sincronia.arquivar_videos: VIDEOS_DIAS=0 nada some; nunca apaga *-tela*.webm, video mais novo que o limite nem o que
    nao confere no Drive; ocupado() para tudo; upload que falha ou vem truncado nao perde o video."""
    pc = m.pc(r)
    pasta = amb.config.GRAVACOES_DIR
    dias = r.choice([0, -3, 1, 1, 7, 30])
    no_drive = r.random() < 0.7
    agora = _time.time()
    limite = agora - dias * 86400
    n = m.prox()
    nomes = []
    for k in range(r.randint(1, 6)):
        stem = r.choice([f"2030010{k}-100000-ev{n}v{k}", f"2030010{k}-100000-ev{n}v{k}-tela", f"2030010{k}-100000-ev{n}v{k}-tela-2",
                         f"2030010{k}-100000-ev{n}v{k}", f"2030010{k}-100000-ev{n}v{k}"])
        ext = r.choice([".webm", ".webm", ".webm", ".wav", ".mp4", ".webm.baixando"])
        p = pasta / (stem + ext)
        p.write_bytes(b"x" * r.randint(1, 50))
        idade = r.choice([-86400 * 3, -3600, 3600, 86400 * 2, 86400 * 400]) if dias > 0 else r.choice([3600, 86400 * 400])
        t = agora - dias * 86400 - idade if dias > 0 else agora - idade
        os.utime(p, (t, t))
        nomes.append(p.name)
    todos = {p.name: (p.stat().st_size, p.stat().st_mtime) for p in pasta.glob("*.webm")}

    def elegivel(nome, st):
        stem = nome[:-5]
        return not stem.endswith("-tela") and "-tela-" not in stem and st[1] < limite

    drive = {}  # nome -> tamanhos
    for nome, (tam, _) in todos.items():
        if r.random() < 0.25:
            drive[nome] = {r.choice([tam, tam, tam + 1, 0])}
    comportamento = r.choice(["ok", "ok", "trunca", "levanta", "nao_grava"])
    ja_no_drive = {nome for nome, tams in drive.items() if todos[nome][0] in tams}  # ja conferidos antes da rodada
    enviados, consultas, duplicados = [], [], []

    def pasta_fake(nome):
        consultas.append(("pasta", nome))
        return "pasta-videos"

    def tamanhos(pid):
        consultas.append(("tamanhos", pid))
        return {k: set(v) for k, v in drive.items()}

    def enviar(pid, nome, conteudo=None, caminho=None, mimetype="", file_id=None, props=None):
        enviados.append(nome)
        if nome in drive and not file_id:
            duplicados.append(nome)  # ja havia um arquivo com esse nome: criar outro acumula copias no Drive
        if comportamento == "levanta":
            raise RuntimeError("Drive caiu no teste")
        if comportamento == "nao_grava":
            return "id"
        tam = os.path.getsize(caminho)
        novo = tam if comportamento == "ok" else max(0, tam - 1)
        if file_id:  # substitui o arquivo existente (envio anterior incompleto)
            drive[file_id] = {novo}
        else:
            drive.setdefault(nome, set()).add(novo)
        return "id"

    def listar(pid):
        consultas.append(("listar", pid))
        return {k: {"id": k} for k in drive}

    limite_ocupado = r.choice([None] * 6 + [0, 1, 2])
    chamadas_ocupado = []

    def ocupado():
        chamadas_ocupado.append(1)
        return limite_ocupado is not None and len(chamadas_ocupado) > limite_ocupado

    salvo_cfg = (config.VIDEOS_DIAS, config.VIDEOS_NO_DRIVE)
    salvo_g = (g.drive_pasta, g.drive_tamanhos, g.drive_enviar, g.drive_listar)
    config.VIDEOS_DIAS, config.VIDEOS_NO_DRIVE = dias, no_drive
    g.drive_pasta, g.drive_tamanhos, g.drive_enviar, g.drive_listar = pasta_fake, tamanhos, enviar, listar
    erro = None
    try:
        try:
            tirados = sincronia.arquivar_videos(ocupado=ocupado)
        except Exception as e:
            tirados, erro = None, e
    finally:
        config.VIDEOS_DIAS, config.VIDEOS_NO_DRIVE = salvo_cfg
        g.drive_pasta, g.drive_tamanhos, g.drive_enviar, g.drive_listar = salvo_g
    depois = {p.name: (p.stat().st_size, p.stat().st_mtime) for p in pasta.glob("*.webm")}
    sumiram = sorted(set(todos) - set(depois))
    cfg = f"dias={dias} no_drive={no_drive} drive={comportamento} ocupado_apos={limite_ocupado}"
    cobrar(not (set(depois) - set(todos)), "I52-arquivar-criou-arquivo", f"{pc}: {cfg}: apareceu {set(depois) - set(todos)}")
    for nome in set(depois):
        cobrar(depois[nome] == todos[nome], "I52-arquivar-alterou", f"{pc}: {nome} foi alterado")
    for nome in nomes:
        if not nome.endswith(".webm"):
            cobrar((pasta / nome).exists(), "I52-arquivar-apagou-outro-tipo", f"{pc}: {cfg}: {nome} (nao e .webm) sumiu")
    if dias <= 0:
        cobrar(not sumiram and not tirados and not enviados and not consultas and erro is None, "I52-dias-zero-mexeu",
               f"{pc}: {cfg}: sumiram {sumiram}, devolveu {tirados!r}, erro {erro!r}, drive {consultas}")
        return f"{pc} arquivar videos {cfg}: nada"
    for nome in sumiram:
        cobrar(elegivel(nome, todos[nome]), "I52-arquivar-apagou-quem-nao-devia",
               f"{pc}: {cfg}: apagou {nome} (tela={('-tela' in nome)}, mais novo que o limite={todos[nome][1] >= limite})", "I52-arquivar-apagou-quem-nao-devia")
        if no_drive:
            cobrar(todos[nome][0] in drive.get(nome, ()), "I52-arquivar-apagou-sem-conferir",
                   f"{pc}: {cfg}: apagou {nome} ({todos[nome][0]} bytes) e no Drive so ha {sorted(drive.get(nome, ()))}", "I52-arquivar-apagou-sem-conferir")
    if erro is None:
        cobrar(sorted(tirados) == sumiram, "I52-arquivar-lista-diferente", f"{pc}: {cfg}: devolveu {tirados} e sumiram {sumiram}")
    cobrar(len(sumiram) <= (limite_ocupado if limite_ocupado is not None else 10 ** 9), "I52-arquivar-ocupado",
           f"{pc}: {cfg}: ocupado() ficou verdadeiro e mesmo assim sumiram {len(sumiram)} (consultou {len(chamadas_ocupado)}x)", "I52-arquivar-ocupado")
    if limite_ocupado == 0:
        cobrar(not enviados and not sumiram, "I52-arquivar-ocupado-mexeu", f"{pc}: {cfg}: ocupado desde o inicio e enviou {enviados}")
    if not no_drive:
        cobrar(not consultas and not enviados, "I52-arquivar-sem-drive-usou-drive", f"{pc}: {cfg}: VIDEOS_NO_DRIVE=nao e usou o Drive {consultas} {enviados}")
    if erro is None and limite_ocupado is None and (not no_drive or comportamento == "ok"):
        quer = sorted(n_ for n_, st in todos.items() if elegivel(n_, st))
        cobrar(sumiram == quer, "I52-arquivar-deixou-velho", f"{pc}: {cfg}: deviam sair {quer} e sairam {sumiram}", "I52-arquivar-deixou-velho")
    cobrar(not duplicados, "I52-arquivar-duplicou-no-drive", f"{pc}: {cfg}: criou outra copia de {duplicados} no Drive")
    if comportamento == "levanta" and no_drive and limite_ocupado is None:
        # todo upload falha: so pode sair o que ja estava conferido no Drive antes
        cobrar(set(sumiram) <= ja_no_drive, "I52-arquivar-upload-falhou-apagou",
               f"{pc}: upload falhou e sumiram {sorted(set(sumiram) - ja_no_drive)} (nao estavam conferidos no Drive)")
    # limpeza: o que sobrou fica (a proxima acao pode ver), mas tira os de teste desta rodada para nao acumular
    for nome in nomes:
        (pasta / nome).unlink(missing_ok=True)
    return f"{pc} arquivar videos {cfg}: tirou {len(sumiram)}"


ACOES7 = A6.ACOES6 + [
    Acao("aviso de som mudo da extensao", f_som_mudo, 3, True, ["/api/extensao/estado", "/gravacao", "reunioes.api_extensao_estado"]),
    Acao("vinculos de convite do Outlook (mesclar, completar)", f_vinculos_de_convite, 3, True, ["envio.registrar_convite", "envio.mesclar_convites", "envio.completar_convite", "envio.convite_do_evento", "sincronia._sincronizar"]),
    Acao("Outlook antes do Google (remarcar, cancelar, convidar)", f_outlook_antes_do_google, 3, True, ["/reuniao/<event_id>/remarcar", "/reuniao/<event_id>/cancelar", "/reuniao/<event_id>/convidar"]),
    Acao("editar ata com Doc alterado fora do painel", f_editar_ata_doc_mexido, 4, True, ["/ata/<event_id>/editar", "reunioes._doc_mexido"]),
    Acao("arquivar videos antigos", f_arquivar_videos, 3, True, ["sincronia.arquivar_videos"]),
]
SEM_ACAO = {k: v for k, v in A6.SEM_ACAO.items() if not k.startswith("/api/extensao/estado")}
SEM_ACAO["/api/extensao/estado e gravacao pela extensao"] = "a gravacao de audio real nao roda: o teste liga GRAVADOR.ativo/automatica/pela_extensao a mao (aviso de som mudo, I48); o caminho que comeca a gravar (ao_detectar_meet) e falso"
SEM_ACAO["sincronia._sincronizar passo 1b (convites.json)"] = "depende do Drive; o lado testavel (envio.mesclar_convites) e coberto por I49"
SEM_FUZZ = A6.SEM_FUZZ
