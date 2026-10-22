"""Catalogo v9 = catalogo v8 (acoes8.py, CONGELADO) + cliente fixado e consultor por reuniao (commit f25e25c).

O cliente e o consultor de cada reuniao ficam no evento do Google (extendedProperties.private: cliente_uuid, consultor), gravados
sem avisar os convidados (sendUpdates="none"). A agenda falsa de ambiente.py guarda os dois campos e conta as chamadas
(DEFINIDOS, DEFINIDOS_CONSULTOR); as funcoes de verdade de google_services rodam contra um _svc falso (_SvcFalso)."""
import re
from datetime import datetime, timedelta, timezone

from markupsafe import escape

import acoes8 as A8
import ambiente as amb
import db
import oraculo as o
from acoes import Acao, chamar
from oraculo import cobrar


class _SvcFalso:
    """Calendar falso que so registra as chamadas de patch/insert/list (o Google de verdade nunca e tocado)."""
    def __init__(self, itens=None):
        self.chamadas, self.itens = [], itens or []

    def events(self):
        return self

    def _exec(self, nome, kw):
        self.chamadas.append((nome, kw))
        return type("E", (), {"execute": lambda s: {"id": "evNovo", "items": self.itens}})()

    def patch(self, **kw):
        return self._exec("patch", kw)

    def insert(self, **kw):
        return self._exec("insert", kw)

    def list(self, **kw):
        return self._exec("list", kw)


def _com_svc(svc, fn, *a, **k):
    original = amb.g._svc
    amb.g._svc = lambda *x, **y: svc
    try:
        return fn(*a, **k)
    finally:
        amb.g._svc = original


def _instantaneo():
    return (len(amb.ENVIOS), len(amb.EMAILS), len(amb.INCLUIDOS), len(amb.REMARCADOS), len(amb.CANCELADOS), len(amb.AGENDADOS))


def _sem_aviso(antes):
    """Nada foi enviado/remarcado/cancelado/convidado: fixar cliente ou consultor nunca avisa ninguem."""
    return _instantaneo() == antes


def _uuid_final(uuid):
    """Segue os apelidos (clientes unidos) ate o uuid que ficou."""
    ap = {a["uuid"]: a["para"] for a in o.linhas("clientes_alias")}
    vistos = set()
    while uuid in ap and uuid not in vistos:
        vistos.add(uuid)
        uuid = ap[uuid]
    return uuid


def _id_esperado(uuid):
    """Cliente que o uuid fixado deve render: o canonico, se existe e nao foi excluido; senao None (regra automatica)."""
    if not uuid:
        return None
    final = _uuid_final(uuid)
    for c in o.linhas("clientes"):
        if c["uuid"] == final:
            return None if c["excluido"] else c["id"]
    return None


def _na_semana(ev):
    if ev.get("dia_inteiro") or not ev.get("link"):
        return False
    hoje = datetime.now().astimezone().date()
    seg = hoje - timedelta(days=hoje.weekday())
    return seg <= datetime.fromisoformat(ev["inicio"]).astimezone().date() <= seg + timedelta(days=6)


def _painel_pequeno(m, r, ev):
    """(texto do <small> do cartao deste evento na agenda, pagina inteira), ou None se o evento nao esta na semana mostrada."""
    if not _na_semana(ev):
        return None
    pc, resp = chamar(m, r, "get", "/")
    if resp.status_code != 200:
        return None
    corpo = resp.get_data(as_text=True)
    achou = re.search(re.escape(str(escape(ev["titulo"]))) + r"\s*<small>(.*?)</small>", corpo, re.S)
    return (achou.group(1) if achou else ""), corpo


HOSTIL_CLIENTE = ["", "abc", "-1", "0", "999999", str(2 ** 70), "²", "٣", " 1", "1.5", "1;2", "1 OR 1=1", "' OR 1=1 --", "1\n", "\x00", "<b>"]


def f_fixar_cliente(m, r):
    """Fixar o cliente da reuniao (POST /resumo/<id>/cliente): vale sobre a sugestao automatica em sugerir_cliente,
    clientes_das_reunioes e na agenda do painel; segue a uniao de clientes; 'nenhum' volta ao automatico; excluido nao vale;
    id hostil equivale a nenhum; evento inexistente nao grava; nunca avisa ninguem."""
    import clientes as C
    pc = m.pc(r)
    todos = o.linhas("clientes")
    vivos = [c for c in todos if not c["excluido"]]
    cenario = r.choice(["fixar", "fixar", "fixar", "nenhum", "hostil", "inexistente", "excluido", "excluido_direto", "mesclar"])
    if cenario in ("fixar", "excluido", "mesclar") and not vivos:
        cenario = "nenhum"
    if cenario == "excluido_direto" and len(todos) == len(vivos):
        cenario = "hostil"
    ev = r.choice([e for e in amb.EVENTOS if e["link"]])
    if cenario in ("nenhum", "hostil") and vivos and r.random() < 0.8:  # um cliente ja fixado, para ter o que apagar
        ev["cliente_uuid"] = r.choice(vivos)["uuid"]
    antes = _instantaneo()
    fixos_antes = {e["id"]: e["cliente_uuid"] for e in amb.EVENTOS}
    ndef = len(amb.DEFINIDOS)
    esperado_uuid = None
    c = None
    if cenario == "inexistente":
        alvo = r.choice(["nao-existe", "ev3", "x" * 40])
        valor = str(r.choice(vivos)["id"]) if vivos else "1"
        pc, resp = chamar(m, r, "post", f"/resumo/{alvo}/cliente", data={"cliente": valor})
        cobrar({e["id"]: e["cliente_uuid"] for e in amb.EVENTOS} == fixos_antes and len(amb.DEFINIDOS) == ndef, "I54-inexistente-gravou",
               f"{pc}: evento {alvo!r} (inexistente/sem Meet) gravou cliente: {amb.DEFINIDOS[ndef:]}")
        cobrar(resp.status_code in (302, 303) and not resp.headers.get("Location", "").rstrip("/").endswith(f"/resumo/{alvo}"),
               "I54-inexistente-nao-volta-ao-inicio", f"{pc}: evento {alvo!r}: {resp.status_code} {resp.headers.get('Location')}")
        cobrar(_sem_aviso(antes), "I54-avisou", f"{pc}: fixar cliente em evento inexistente enviou algo")
        return f"{pc} fixar em {alvo!r} inexistente"
    if cenario == "hostil":
        valor = r.choice(HOSTIL_CLIENTE + [str(x["id"]) for x in todos[:1]])
        try:
            n = int(valor)  # o que o Flask (type=int) entende
        except ValueError:
            n = None
        linha = next((x for x in todos if x["id"] == n), None) if n is not None and 0 < n < 2 ** 63 else None
        esperado_uuid = linha["uuid"] if linha else None
    elif cenario == "nenhum":
        valor = r.choice(["", "x", "0"])
    elif cenario == "excluido_direto":
        c = r.choice([x for x in todos if x["excluido"]])
        valor, esperado_uuid = str(c["id"]), c["uuid"]
    else:
        c = r.choice(vivos)
        valor, esperado_uuid = str(c["id"]), c["uuid"]
    pc, resp = chamar(m, r, "post", f"/resumo/{ev['id']}/cliente", data={"cliente": valor})
    cobrar(resp.status_code in (302, 303) and resp.headers.get("Location", "").endswith(f"/resumo/{ev['id']}"), "I54-fixar-redireciona",
           f"{pc}: cliente={valor!r}: {resp.status_code} {resp.headers.get('Location')}")
    cobrar(ev["cliente_uuid"] == esperado_uuid, "I54-fixar-gravou-outro",
           f"{pc}: cliente={valor!r} cenario={cenario}: evento ficou com {ev['cliente_uuid']!r}, esperado {esperado_uuid!r}",
           f"I54-fixar-gravou-outro:{cenario}")
    cobrar(len(amb.DEFINIDOS) == ndef + 1, "I54-fixar-chamadas", f"{pc}: {len(amb.DEFINIDOS) - ndef} chamadas ao Google")
    cobrar(_sem_aviso(antes), "I54-avisou", f"{pc}: fixar cliente enviou convite/e-mail/remarcou/cancelou")
    if cenario == "mesclar":  # o outro PC tinha o mesmo cliente com um uuid menor: o uuid fixado vira apelido
        ex = db.exportar()
        linha = dict(next(x for x in ex["clientes"] if x["uuid"] == esperado_uuid))
        linha["uuid"] = "0" * 31 + "1"
        db.mesclar({"clientes": [linha], "tarefas": [], "notas": [], "pessoas": [], "apelidos": []})
    if cenario == "excluido":
        db.excluir_cliente(c["id"])
        if r.random() < 0.4:  # cadastrar de novo traz o mesmo uuid de volta
            db.criar_cliente(c["nome"], c["emails"])
    import clientes as C
    reun = amb.g.obter_reuniao(ev["id"])
    sem = {**reun, "cliente_uuid": None}
    auto = amb.reunioes.sugerir_cliente(sem)
    auto_c = C.clientes_das_reunioes([sem]).get(ev["id"])
    fixo = _id_esperado(ev["cliente_uuid"])
    esp = fixo if fixo is not None else auto
    esp_c = fixo if fixo is not None else auto_c
    got = amb.reunioes.sugerir_cliente(reun)
    cobrar(got == esp, "I54-sugerir-cliente", f"{pc}: cenario={cenario} uuid={ev['cliente_uuid']!r}: sugerir_cliente={got}, esperado {esp} (automatico {auto})",
           f"I54-sugerir-cliente:{cenario}")
    got_c = C.clientes_das_reunioes([reun]).get(ev["id"])
    cobrar(got_c == esp_c, "I54-clientes-das-reunioes", f"{pc}: cenario={cenario} uuid={ev['cliente_uuid']!r}: clientes_das_reunioes={got_c}, esperado {esp_c}",
           f"I54-clientes-das-reunioes:{cenario}")
    nomes = {x["id"]: x["nome"] for x in db.clientes()}
    p = _painel_pequeno(m, r, ev)
    if p is not None and nomes.get(esp):
        pequeno, corpo = p
        cobrar(pequeno.startswith(str(escape(nomes[esp]))), "I54-painel-cliente",
               f"{pc}: cenario={cenario}: agenda do painel mostra {pequeno!r} para {ev['titulo']!r}, esperado cliente {nomes[esp]!r}",
               f"I54-painel-cliente:{cenario}")
    if r.random() < 0.3:  # a funcao de verdade: sendUpdates "none", uuid ou "" no corpo; e a leitura de volta
        svc = _SvcFalso()
        for v in (esperado_uuid, None, ""):
            _com_svc(svc, amb.DEFINIR_CLIENTE_REAL, "evX", v)
        for _, kw in svc.chamadas:
            cobrar(kw.get("sendUpdates") == "none", "I54-real-avisa", f"definir_cliente_do_evento chamou patch com sendUpdates={kw.get('sendUpdates')!r}")
        corpos = [kw["body"]["extendedProperties"]["private"]["cliente_uuid"] for _, kw in svc.chamadas]
        cobrar(corpos == [esperado_uuid or "", "", ""], "I54-real-corpo", f"corpos gravados {corpos}")
        for ext, esperado_leitura in ((None, None), ({}, None), ({"private": None}, None), ({"private": {}}, None),
                                      ({"private": {"cliente_uuid": ""}}, None), ({"private": {"cliente_uuid": None}}, None),
                                      ({"private": {"cliente_uuid": "abc"}}, "abc")):
            item = {"id": "evL", "start": {"dateTime": "2030-01-01T10:00:00+00:00"}, "end": {"dateTime": "2030-01-01T11:00:00+00:00"}}
            if ext is not None:
                item["extendedProperties"] = ext
            lido = _com_svc(_SvcFalso([item]), amb.EVENTOS_REAL, datetime.now(timezone.utc), datetime.now(timezone.utc))
            cobrar(bool(lido) and lido[0]["cliente_uuid"] == esperado_leitura, "I54-real-leitura",
                   f"extendedProperties={ext!r} -> {lido and lido[0]['cliente_uuid']!r}")
    return f"{pc} fixar cliente {cenario} {ev['id']} valor={valor!r}"


CONSULTORES = ["Ana", "  Ana   Souza  ", "ana souza", "ANA SOUZA", "<script>alert(1)</script>", "linha1\nlinha2", "a\r\nb\tc", "x" * 10000,
               "   ", "", "\t", "Ação", "AÇÃO", "ação", "Ação", "a&b \"q\" 'x'", "  ", "João", "y" * 79 + " z",
               "w" * 80 + "  ", "{{ 7*7 }}", "{% if 1 %}x{% endif %}"]


def f_consultor(m, r):
    """Consultor por reuniao (POST /resumo/<id>/consultor): vai ao evento normalizado (espacos), <= 80, vazio apaga, escapado
    no painel; consultores_conhecidos sem duplicata por caixa/NFC; nunca avisa; nova reuniao leva o consultor ao agendar_followup."""
    import clientes as C
    import unicodedata
    pc = m.pc(r)
    cenario = r.choice(["salvar", "salvar", "salvar", "ausente", "inexistente", "nova", "real"])
    ev = r.choice([e for e in amb.EVENTOS if e["link"]])
    valor = r.choice(CONSULTORES)
    esperado_rota = " ".join(valor.split())[:80]  # o que a rota entrega ao Google (o corte pode deixar um espaco no fim)
    esperado = esperado_rota.strip()  # o que fica no evento: google_services faz strip()[:80]
    antes = _instantaneo()
    ndef = len(amb.DEFINIDOS_CONSULTOR)
    if cenario == "inexistente":
        alvo = r.choice(["nao-existe", "ev3"])
        fixos = {e["id"]: e["consultor"] for e in amb.EVENTOS}
        pc, resp = chamar(m, r, "post", f"/resumo/{alvo}/consultor", data={"consultor": valor})
        cobrar({e["id"]: e["consultor"] for e in amb.EVENTOS} == fixos and len(amb.DEFINIDOS_CONSULTOR) == ndef, "I55-inexistente-gravou",
               f"{pc}: evento {alvo!r} gravou consultor")
        cobrar(_sem_aviso(antes), "I55-avisou", f"{pc}: consultor em evento inexistente enviou algo")
        return f"{pc} consultor em {alvo!r}"
    if cenario == "nova":
        amb.AGENDADOS.clear()
        antes = _instantaneo()
        pc, resp = chamar(m, r, "post", "/reuniao/nova", data={"data": "2030-01-02", "hora": "10:00", "titulo": "T", "duracao": "30", "consultor": valor})
        recebido = [a[1].get("consultor", "AUSENTE") for a in amb.AGENDADOS]
        cobrar(recebido == [esperado_rota], "I55-nova-consultor",
               f"{pc}: consultor {valor[:40]!r} -> agendar_followup recebeu {[x[:90] for x in recebido]}, esperado {esperado_rota[:90]!r}", "I55-nova-consultor")
        pc, resp = chamar(m, r, "get", "/reuniao/nova")
        if resp.status_code == 200:
            corpo = resp.get_data(as_text=True)
            cobrar('name="consultor"' in corpo and "<script>alert(1)</script>" not in corpo, "I55-nova-campo",
                   f"{pc}: /reuniao/nova sem o campo consultor ou com HTML solto")
        svc = _SvcFalso()
        _com_svc(svc, amb.AGENDAR_REAL, "T", datetime(2030, 1, 2, 10), 30, [], "", enviar_convites=False, consultor=valor)
        ins = [kw for n, kw in svc.chamadas if n == "insert"]
        cobrar(len(ins) == 1 and ins[0]["sendUpdates"] == "none", "I55-real-agendar-avisa", f"agendar_followup(enviar_convites=False): {ins and ins[0]['sendUpdates']}")
        gravado = ins[0]["body"]["extendedProperties"]["private"]["consultor"]
        cobrar(gravado == valor.strip()[:80] and len(gravado) <= 80, "I55-real-agendar-corpo", f"agendar_followup gravou {gravado[:90]!r} para {valor[:40]!r}")
        return f"{pc} nova reuniao consultor={valor[:20]!r}"
    if cenario == "real":
        svc = _SvcFalso()
        _com_svc(svc, amb.DEFINIR_CONSULTOR_REAL, "evX", valor)
        kw = svc.chamadas[0][1]
        gravado = kw["body"]["extendedProperties"]["private"]["consultor"]
        cobrar(kw.get("sendUpdates") == "none", "I55-real-avisa", f"definir_consultor_do_evento: sendUpdates={kw.get('sendUpdates')!r}")
        cobrar(gravado == valor.strip()[:80] and len(gravado) <= 80, "I55-real-corpo", f"gravou {gravado[:90]!r} para {valor[:40]!r}")
        for ext, esp in ((None, ""), ({"private": None}, ""), ({"private": {"consultor": None}}, ""), ({"private": {"consultor": "  Ana "}}, "Ana")):
            item = {"id": "evL", "start": {"dateTime": "2030-01-01T10:00:00+00:00"}, "end": {"dateTime": "2030-01-01T11:00:00+00:00"}}
            if ext is not None:
                item["extendedProperties"] = ext
            lido = _com_svc(_SvcFalso([item]), amb.EVENTOS_REAL, datetime.now(timezone.utc), datetime.now(timezone.utc))
            cobrar(bool(lido) and lido[0]["consultor"] == esp, "I55-real-leitura", f"extendedProperties={ext!r} -> {lido and lido[0]['consultor']!r}")
        return f"{pc} consultor real"
    dados = {} if cenario == "ausente" else {"consultor": valor}
    if cenario == "ausente":
        esperado = ""
    pc, resp = chamar(m, r, "post", f"/resumo/{ev['id']}/consultor", data=dados)
    cobrar(resp.status_code in (302, 303) and resp.headers.get("Location", "").endswith(f"/resumo/{ev['id']}"), "I55-redireciona",
           f"{pc}: {resp.status_code} {resp.headers.get('Location')}")
    cobrar(ev["consultor"] == esperado and len(ev["consultor"]) <= 80, "I55-gravou-outro",
           f"{pc}: consultor {valor[:40]!r} (len {len(valor)}): evento ficou com {ev['consultor'][:90]!r}, esperado {esperado[:90]!r}", "I55-gravou-outro")
    cobrar(len(amb.DEFINIDOS_CONSULTOR) == ndef + 1, "I55-chamadas", f"{pc}: {len(amb.DEFINIDOS_CONSULTOR) - ndef} chamadas ao Google")
    cobrar(_sem_aviso(antes), "I55-avisou", f"{pc}: salvar consultor enviou convite/e-mail/remarcou/cancelou")
    p = _painel_pequeno(m, r, ev)
    if p is not None:
        pequeno, corpo = p
        cobrar("<script>alert(1)</script>" not in corpo, "I55-painel-injeta", f"{pc}: HTML do consultor entrou cru no painel")
        if esperado:
            cobrar(f"Consultor: {escape(esperado)}" in pequeno, "I55-painel-consultor",
                   f"{pc}: painel mostra {pequeno[:120]!r}, esperado 'Consultor: {esperado[:40]}' escapado", "I55-painel-consultor")
        else:
            cobrar("Consultor" not in pequeno, "I55-painel-consultor-vazio", f"{pc}: consultor apagado mas o painel mostra {pequeno[:120]!r}")
    pc, resp = chamar(m, r, "get", f"/resumo/{ev['id']}")
    if resp.status_code == 200:
        cobrar("<script>alert(1)</script>" not in resp.get_data(as_text=True), "I55-resumo-injeta", f"{pc}: HTML do consultor entrou cru no /resumo")
    C._historico["quando"] = 0
    lista = C.consultores_conhecidos()
    chaves = [unicodedata.normalize("NFC", n).casefold() for n in lista]
    cobrar(len(set(chaves)) == len(chaves), "I55-conhecidos-repetidos", f"{pc}: consultores_conhecidos repete por caixa/acento: {[n[:30] for n in lista]}",
           "I55-conhecidos-repetidos")
    cont = {}
    for e in amb.EVENTOS:
        if e["consultor"]:
            k = unicodedata.normalize("NFC", e["consultor"]).casefold()
            cont[k] = cont.get(k, 0) + 1
    cobrar(set(chaves) == set(cont), "I55-conhecidos-conjunto", f"{pc}: consultores_conhecidos={[n[:30] for n in lista]}, nos eventos {sorted(k[:30] for k in cont)}",
           "I55-conhecidos-conjunto")
    quantidades = [cont[k] for k in chaves if k in cont]
    cobrar(quantidades == sorted(quantidades, reverse=True), "I55-conhecidos-ordem", f"{pc}: nao esta do mais usado ao menos: {quantidades}")
    return f"{pc} consultor {cenario} {ev['id']} {valor[:20]!r}"


ACOES9 = A8.ACOES8 + [
    Acao("fixar o cliente da reuniao", f_fixar_cliente, 3, True, ["/resumo/<event_id>/cliente", "clientes.clientes_das_reunioes", "reunioes.sugerir_cliente"]),
    Acao("consultor da reuniao", f_consultor, 3, True, ["/resumo/<event_id>/consultor", "clientes.consultores_conhecidos"]),
]
SEM_ACAO = A8.SEM_ACAO
SEM_FUZZ = A8.SEM_FUZZ
