"""Catalogo v10 = catalogo v9 (acoes9.py, CONGELADO) + tres novidades:

1. `reunioes.juntar_com_a_anterior` e o uso em `ao_encerrar_meet`: a chamada cai e volta, a gravacao nova (pendente) e juntada
   no fim da anterior da MESMA reuniao (I57).
2. `google_services._guardar_agenda` / `reunioes_guardadas` / `AGENDA_LOCAL` e o plano B de `reunioes._reuniao_da_janela`
   quando `g.listar_reunioes` falha (I58). O ambiente troca `g.listar_reunioes` por um falso, entao aqui a funcao de verdade
   (`amb.LISTAR_REUNIOES_REAL`) roda com `g._eventos` trocado, e `g.AGENDA_LOCAL` aponta para uma pasta temporaria dentro de TMP.
3. `gravador.alucinacao_conhecida` / `segmento_confiavel` (I59).

Os audios sao WAVs de verdade (1000 ou 8000 Hz, mono, int16, valor constante por arquivo) em pasta temporaria do PC; nada de
Whisper nem de microfone. Os nomes de tudo o que a acao cria levam "-agj-" e sao apagados no fim."""
import contextlib
import io
import json
import shutil
import tempfile
import types
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import acoes9 as A9
import ambiente as amb
import gravador
from acoes import Acao
from oraculo import cobrar

reunioes = amb.reunioes
g = amb.g
config = amb.config
MARCA = "-agj-"


# ---------------------------------------------------------------- I57: juntar a gravacao que voltou com a anterior

def _pid(inicio, tag):
    return f"{inicio:%Y%m%d-%H%M%S}{MARCA}{tag}"


def _wav(caminho, valor, seg, taxa):
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(np.full(int(seg * taxa), valor, dtype="<i2").tobytes())


def _ler_wav(caminho):
    with wave.open(str(caminho)) as w:
        return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy(), w.getparams()


def _montar(pasta, pid, rid, gravada, encerrada, trilhas, valores, dur, taxa, texto=None, resolvida=None, extras=()):
    """Cria uma gravacao pendente de verdade (json + WAVs) com o _gravar_pendente do sistema."""
    arquivos = {}
    for t in trilhas:
        arquivos[t] = f"{pid}-{t}.wav"
        _wav(pasta / arquivos[t], valores[t], dur, taxa)
    for fim in extras:
        (pasta / f"{pid}{fim}").write_bytes(b"x")
    dados = {"reuniao": {"id": rid, "titulo": "Reuniao J", "inicio": gravada.isoformat(timespec="minutes"), "participantes": []},
             "gravada_em": gravada.isoformat(timespec="seconds"), "arquivos": arquivos,
             "minutos": round(dur / 60), "encerrada_em": encerrada}
    if texto is not None:
        dados["transcricao_ao_vivo"] = texto
    if resolvida:
        dados["resolvida"] = resolvida
    reunioes._gravar_pendente(pid, dados)
    return dados


def _snap(pasta):
    return {p.name: p.read_bytes() for p in pasta.iterdir() if MARCA in p.name}


def _json(pasta, pid):
    return json.loads((pasta / f"{pid}.json").read_text(encoding="utf-8"))


def _tem_vt(pasta, pid):
    return any(p.name.startswith(pid) and p.suffix in (".webm", ".txt") for p in pasta.iterdir())


def _modelo(pasta, plano):
    """Quem e a anterior que a regra manda juntar (ou None), olhando o disco ANTES da chamada. A regra (do commit): mesma reuniao e nao
    avulsa; a anterior ainda aguarda decisao; nenhuma das duas tem .webm/.txt; a anterior comecou-lhe (nova) ate 10 min depois do
    encerrada_em dela (aceita 60 s antes); a anterior tem todas as trilhas da nova (e a voce); SINCRONIZAR_AUDIO falso."""
    if plano["sinc"] or str(plano["rid"]).startswith("avulsa-") or "voce" not in plano["trilhas"] or plano["video"]:
        return None
    for nome in sorted((p.name for p in pasta.iterdir() if p.suffix == ".json" and MARCA in p.name), reverse=True):
        apid = nome[:-5]
        try:
            ant = json.loads((pasta / nome).read_text(encoding="utf-8"))
        except ValueError:
            continue
        if apid == plano["pid"] or ant.get("resolvida") or ant["reuniao"].get("id") != plano["rid"] or _tem_vt(pasta, apid):
            continue
        try:
            iv = (plano["gravada"] - datetime.fromisoformat(ant["encerrada_em"])).total_seconds()
        except (TypeError, ValueError):
            continue
        aud = {t for t, c in ant["arquivos"].items() if (pasta / Path(c).name).exists()}
        if "voce" in aud and set(plano["trilhas"]) <= aud and -60 <= iv <= 600:
            return apid
    return None


CEN = {"junta": 14, "erro": 3, "tarde": 2, "ant_resolvida": 1, "ant_webm": 1, "ant_txt": 1, "nova_webm": 1, "nova_txt": 1,
       "sincronizar": 1, "outra_reuniao": 1, "avulsa": 1, "cedo": 1, "trilha_a_mais": 1, "ant_sem_wav": 1, "encerrada_lixo": 1,
       "nova_sem_voce": 1}


class _Navegador:
    def __init__(self):
        self.urls = []

    def open(self, url, *a, **k):
        self.urls.append(url)
        return True


def f_juntar_gravacoes(m, r):
    """I57: a chamada caiu e voltou (ver acima). Gera anterior + nova + iscas, calcula com o modelo QUEM deveria ser a anterior olhando
    o disco, roda juntar_com_a_anterior (ou o ao_encerrar_meet inteiro, com a gravacao e o navegador falsos) e cobra:
    - juntou: o WAV de cada trilha da nova = anterior + silencio (do inicio da anterior ao inicio da nova menos a duracao do audio da
      anterior; nenhum se for negativo) + nova; a duracao total = (inicio da nova - inicio da anterior) + duracao da nova quando ha
      intervalo; minutos = duracao total; encerrada_em da nova; transcricao ao vivo concatenada; a nova fica resolvida=juntada:<anterior>
      sem audios; exatamente uma pendente sobra da reuniao; as iscas ficam como estavam; chamar de novo nao faz nada.
    - nao juntou: nenhum arquivo de nenhuma gravacao muda, a nova continua pendente (e, no ao_encerrar_meet, avisa e abre a nova).
    - juntar_wavs falhou: os audios da nova continuam la e a nova continua pendente (e a anterior nao fica pela metade)."""
    pc = m.pc(r)
    pasta = config.GRAVACOES_DIR
    cen = r.choices(list(CEN), list(CEN.values()))[0]
    via = "direto" if cen == "nova_sem_voce" else r.choice(["direto", "encerrar"])
    taxa = 8000 if (via == "direto" and r.random() < 0.15) else 1000
    dur_a = r.randint(5, 10) if taxa == 8000 else r.randint(5, 150)
    dur_n = r.randint(61, 150) if via == "encerrar" else (r.randint(5, 10) if taxa == 8000 else r.randint(5, 150))
    if cen == "tarde":
        iv = r.choice([601, r.randint(601, 900)])
    elif cen == "cedo":
        iv = r.choice([-61, r.randint(-300, -61)])
    else:
        iv = r.choice([-60, 0, 600]) if r.random() < 0.3 else r.randint(-60, 600)
        if taxa == 8000:
            iv = min(iv, 40)  # silencio de 600 s a 8000 Hz seria arquivo grande demais para um passo
    if via == "encerrar":
        iv = {-60: -55, 600: 595, 601: 610, -61: -70}.get(iv, iv)
    agora = datetime.now().replace(microsecond=0)
    N = agora - timedelta(seconds=dur_n) if via == "encerrar" else agora - timedelta(seconds=r.randint(3600, 86400))
    enc_ant = N - timedelta(seconds=iv)
    enc_txt = enc_ant.replace(second=0).isoformat(timespec="minutes") if (via == "direto" and r.random() < 0.3) else enc_ant.isoformat(timespec="seconds")
    A0 = enc_ant - timedelta(seconds=dur_a + r.randint(-5, 30))
    rid = "evJ1"
    rid_ant = "evJ2" if cen == "outra_reuniao" else rid
    if cen == "avulsa":
        rid = rid_ant = f"avulsa-{N:%Y%m%d%H%M%S}"
    ta = r.choice([["voce"], ["voce", "outros"]])
    tn = r.choice([t for t in (["voce"], ["voce", "outros"]) if set(t) <= set(ta)])
    if cen == "trilha_a_mais":
        ta, tn = ["voce"], ["voce", "outros"]
    if cen == "nova_sem_voce":
        ta, tn = ["voce", "outros"], ["outros"]
    if cen == "encerrada_lixo":
        enc_txt = r.choice(["", "ontem", None])
    va = {"voce": 3000, "outros": 4000}
    vn = {"voce": 5000, "outros": 6000}
    textos = [None, "", "[voce] oi", "linha 1\nlinha 2"]
    t_ant, t_nov = r.choice(textos), r.choice(textos)
    pid_a, pid_n = _pid(A0, "a"), _pid(N, "n")
    sinc = cen == "sincronizar"
    video = cen in ("nova_webm", "nova_txt")
    erro_k = 0
    if cen == "erro":
        erro_k = r.randint(1, len(tn))

    snap_antes, antigo = {}, {}
    salvo = (config.SINCRONIZAR_AUDIO, reunioes.encerrar_gravacao, reunioes._vincular_telas, reunioes._juntar_audio_nos_videos, reunioes.webbrowser,
             reunioes.monitor.notificar, gravador.juntar_wavs)
    G = reunioes.GRAVADOR
    salvo_g = (G.ativo, G.automatica, G.inicio)
    nav = _Navegador()
    avisos = []
    saida = io.StringIO()
    exc = None
    ret = "nao-rodou"
    try:
        dados_a = _montar(pasta, pid_a, rid_ant, A0, enc_txt, ta, va, dur_a, taxa, t_ant,
                          resolvida="descartada" if cen == "ant_resolvida" else None,
                          extras=("-tela.webm",) if cen == "ant_webm" else ("-voce.txt",) if cen == "ant_txt" else ())
        if cen == "ant_sem_wav":
            (pasta / dados_a["arquivos"]["voce"]).unlink()
        # iscas: parecem a anterior certa, mas cada uma tem um defeito
        for k, tipo in enumerate(r.sample(["outra", "resolvida", "webm", "longe"], r.randint(0, 2))):
            did = _pid(N - timedelta(seconds=400 + 60 * k), f"d{k}")
            fim = N - timedelta(seconds=30 + 60 * k)
            _montar(pasta, did, "evJ2" if tipo == "outra" else rid, fim - timedelta(seconds=40), (fim - timedelta(seconds=1500)).isoformat(timespec="seconds")
                    if tipo == "longe" else fim.isoformat(timespec="seconds"), ["voce"], {"voce": 7000 + k}, 5, 1000,
                    resolvida="ata" if tipo == "resolvida" else None, extras=("-tela.webm",) if tipo == "webm" else ())
        nova_dados = _montar(pasta, pid_n, rid, N, "2030-01-01T00:00", tn, vn, dur_n, taxa, t_nov,
                             extras=("-voce.txt",) if cen == "nova_txt" else ("-tela.webm",) if (cen == "nova_webm" and via == "direto") else ())
        if via == "encerrar":
            (pasta / f"{pid_n}.json").unlink()  # quem grava o json da nova e o ao_encerrar_meet (salvar_pendente)
        plano = {"rid": rid, "pid": pid_n, "gravada": N, "trilhas": tn, "sinc": sinc, "video": video}
        alvo = _modelo(pasta, plano)
        snap_antes = _snap(pasta)
        pend_antes = {p["pid"] for p in reunioes.listar_pendentes() if p["reuniao"].get("id") == rid and MARCA in p["pid"]}
        config.SINCRONIZAR_AUDIO = sinc
        if alvo is None:
            erro_k = 0  # o cenario de erro so vale se a regra manda juntar (encerrada_em com so minutos pode tirar da janela)
        if erro_k:
            original, chamadas = gravador.juntar_wavs, []

            def juntar_falho(destino, extra, silencio, **kw):
                chamadas.append(1)
                if len(chamadas) == erro_k:
                    raise OSError("disco cheio (falso)")
                return original(destino, extra, silencio, **kw)
            gravador.juntar_wavs = juntar_falho
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
            try:
                if via == "direto":
                    ret = reunioes.juntar_com_a_anterior(pid_n)
                else:
                    reuniao = {"id": rid, "titulo": "Reuniao J", "inicio": N.isoformat(timespec="minutes"), "participantes": []}
                    arquivos = {t: pasta / f"{pid_n}-{t}.wav" for t in tn}
                    G.ativo, G.automatica, G.inicio = True, True, N
                    reunioes.encerrar_gravacao = lambda: (reuniao, arquivos, t_nov or "")

                    def vincular(pid):
                        if cen == "nova_webm":
                            (pasta / f"{pid}-tela.webm").write_bytes(b"x")
                            return [("1", pasta / f"{pid}-tela.webm", 0.0)]
                        return []
                    reunioes._vincular_telas = vincular
                    reunioes._juntar_audio_nos_videos = lambda *a, **k: None
                    reunioes.webbrowser = nav
                    reunioes.monitor.notificar = lambda *a, **k: avisos.append(a)
                    ret = reunioes.ao_encerrar_meet()
            except Exception as e:
                exc = e
        gravador.juntar_wavs = salvo[6]
        config.SINCRONIZAR_AUDIO = salvo[0]
        if exc is not None and not erro_k:
            cobrar(False, "I57-estoura", f"{pc}: {cen}/{via}: levantou {type(exc).__name__}: {exc}", f"I57-estoura:{type(exc).__name__}:{cen}")
        snap_depois = _snap(pasta)
        base = f"{pc}: {cen}/{via} (intervalo {iv}s, trilhas anterior {ta} nova {tn}, {taxa} Hz)"
        nova_pend = reunioes.carregar_pendente(pid_n)

        if erro_k:  # juntar_wavs falhou: nada se perde, nada fica pela metade
            cobrar(exc is not None or via == "encerrar", "I57-erro-engolido", f"{base}: juntar_wavs falhou e juntar_com_a_anterior nao levantou")
            if via == "encerrar":
                cobrar(ret == pid_n, "I57-erro-retorno", f"{base}: ao_encerrar_meet devolveu {ret!r} depois do erro (devia seguir com {pid_n!r})")
            nova_wavs = {n: b for n, b in snap_antes.items() if n.startswith(pid_n) and n.endswith(".wav")}
            cobrar(all(snap_depois.get(n) == b for n, b in nova_wavs.items()), "I57-erro-apagou-a-nova",
                   f"{base}: juntar_wavs falhou na chamada {erro_k} e os audios da nova sumiram/mudaram", "I57-erro-apagou-a-nova")
            cobrar(nova_pend is not None, "I57-erro-resolveu-a-nova", f"{base}: juntar_wavs falhou e a nova deixou de aguardar decisao", "I57-erro-resolveu-a-nova")
            wavs_a = {n: b for n, b in snap_antes.items() if n.startswith(pid_a) and n.endswith(".wav")}
            cobrar(all(snap_depois.get(n) == b for n, b in wavs_a.items()), "I57-erro-juntou-pela-metade",
                   f"{base}: juntar_wavs falhou na chamada {erro_k} de {len(tn)} e a anterior ficou com {[n for n, b in wavs_a.items() if snap_depois.get(n) != b]} "
                   f"alterada(s) enquanto a nova continua pendente com os mesmos audios (uma nova tentativa juntaria de novo e duplicaria)",
                   "I57-erro-juntou-pela-metade")
            return f"{base}: erro na chamada {erro_k}"

        if alvo is None:  # nao junta: nada muda
            cobrar(ret == (pid_n if via == "encerrar" else None), "I57-juntou-sem-poder", f"{base}: devolveu {ret!r}, nao devia juntar com ninguem", f"I57-juntou-sem-poder:{cen}")
            mudou = [n for n, b in snap_antes.items() if snap_depois.get(n) != b]
            cobrar(not mudou, "I57-perdeu-audio-sem-juntar", f"{base}: nao juntou e mesmo assim mudou/apagou {mudou}", f"I57-perdeu-audio-sem-juntar:{cen}")
            novos = sorted(set(snap_depois) - set(snap_antes))
            cobrar(novos == ([f"{pid_n}.json"] if via == "encerrar" else []) or (via == "encerrar" and cen == "nova_webm" and novos == sorted([f"{pid_n}.json", f"{pid_n}-tela.webm"])),
                   "I57-arquivo-novo", f"{base}: arquivos novos {novos}", f"I57-arquivo-novo:{cen}")
            cobrar(nova_pend is not None, "I57-nova-sumiu", f"{base}: a nova nao aguarda mais decisao e nao foi juntada", f"I57-nova-sumiu:{cen}")
            if via == "encerrar":
                cobrar(len(nav.urls) == 1 and nav.urls[0].endswith("/pendente/" + reunioes.quote(pid_n)), "I57-navegador",
                       f"{base}: abriu {nav.urls}, esperado a pergunta da nova")
                cobrar(len(avisos) == 1, "I57-aviso", f"{base}: {len(avisos)} avisos 'Reuniao encerrada', esperado 1")
            return f"{base}: nao junta"

        # ---- juntou
        cobrar(ret == alvo, "I57-nao-juntou", f"{base}: devia juntar com {alvo!r} e devolveu {ret!r}", f"I57-nao-juntou:{cen}")
        nj = _json(pasta, pid_n)
        cobrar(nj.get("resolvida") == f"juntada:{alvo}", "I57-resolvida", f"{base}: nova ficou com resolvida={nj.get('resolvida')!r}", "I57-resolvida")
        restos = [n for n in snap_depois if n.startswith(pid_n) and not n.endswith(".json")]
        cobrar(not restos, "I57-audios-da-nova-ficaram", f"{base}: depois de juntar sobraram {restos}", "I57-audios-da-nova-ficaram")
        pend_depois = {p["pid"] for p in reunioes.listar_pendentes() if p["reuniao"].get("id") == rid and MARCA in p["pid"]}
        cobrar(pend_depois == pend_antes - {pid_n}, "I57-pendentes", f"{base}: pendentes da reuniao {sorted(pend_antes)} -> {sorted(pend_depois)}", "I57-pendentes")
        aj = _json(pasta, alvo)
        cobrar("resolvida" not in aj and reunioes.carregar_pendente(alvo) is not None, "I57-anterior-resolvida", f"{base}: a anterior deixou de aguardar decisao")
        cobrar(all(aj[k] == dados_a[k] for k in ("reuniao", "gravada_em", "arquivos")), "I57-anterior-mudou-dados", f"{base}: a anterior perdeu reuniao/gravada_em/arquivos")
        iniA, iniN = datetime.fromisoformat(aj["gravada_em"]), datetime.fromisoformat(nj["gravada_em"])
        for t in tn:
            na, nn = int(dur_a * taxa), int(dur_n * taxa)
            tx, arr, par = _ler_wav(pasta / aj["arquivos"][t])
            cobrar(tx == taxa and par.nchannels == 1 and par.sampwidth == 2, "I57-formato", f"{base}: {t}: formato {par}")
            s0 = max(0.0, (iniN - iniA).total_seconds() - na / taxa) * taxa
            s = len(arr) - na - nn
            cobrar(s >= 0 and abs(s - s0) <= 2, "I57-silencio", f"{base}: {t}: silencio de {s / taxa:.2f}s entre as duas partes, esperado {s0 / taxa:.2f}s "
                   f"(inicio da nova - inicio da anterior - duracao do audio da anterior)", "I57-silencio")
            ok = (np.array_equal(arr[:na], np.full(na, va[t])) and not arr[na:na + s].any() and np.array_equal(arr[na + s:], np.full(nn, vn[t])))
            cobrar(ok, "I57-audio-final", f"{base}: {t}: o audio final nao e anterior + silencio + nova", "I57-audio-final")
            if t == "voce":
                total = len(arr) / taxa
                if (iniN - iniA).total_seconds() - na / taxa > 0:
                    cobrar(abs(total - ((iniN - iniA).total_seconds() + nn / taxa)) <= 3 / taxa, "I57-duracao",
                           f"{base}: duracao {total:.2f}s, esperado {(iniN - iniA).total_seconds() + nn / taxa:.2f}s", "I57-duracao")
                cobrar(aj["minutos"] == round(total / 60), "I57-minutos", f"{base}: minutos={aj['minutos']} para {total:.1f}s de audio (esperado {round(total / 60)})", "I57-minutos")
        for t in set(ta) - set(tn):
            cobrar(snap_depois[dados_a["arquivos"][t]] == snap_antes[dados_a["arquivos"][t]], "I57-trilha-alheia", f"{base}: a trilha {t}, que a nova nao tem, mudou")
        cobrar(aj["encerrada_em"] == nj["encerrada_em"], "I57-encerrada-em", f"{base}: encerrada_em {aj['encerrada_em']!r}, a da nova e {nj['encerrada_em']!r}", "I57-encerrada-em")
        esperado_txt = "\n".join(t for t in (t_ant, t_nov) if t)
        cobrar(aj["transcricao_ao_vivo"] == esperado_txt, "I57-transcricao", f"{base}: transcricao ao vivo {aj['transcricao_ao_vivo']!r}, esperado {esperado_txt!r}", "I57-transcricao")
        outros = [n for n in snap_antes if not n.startswith((pid_a, pid_n))]
        cobrar(all(snap_depois.get(n) == snap_antes[n] for n in outros), "I57-isca-mudou", f"{base}: mexeu em outra gravacao: {[n for n in outros if snap_depois.get(n) != snap_antes[n]]}", "I57-isca-mudou")
        if via == "encerrar":
            cobrar(len(nav.urls) == 1 and nav.urls[0].endswith("/pendente/" + reunioes.quote(alvo)), "I57-navegador", f"{base}: abriu {nav.urls}, esperado a pergunta da anterior")
            cobrar(not avisos, "I57-aviso-indevido", f"{base}: avisou 'Reuniao encerrada' ({len(avisos)}x) mesmo juntando")
        # idempotencia: de novo nao faz nada
        depois = _snap(pasta)
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
            de_novo = reunioes.juntar_com_a_anterior(pid_n)
            ninguem = reunioes.juntar_com_a_anterior("nao-existe-agj")
            if len(pend_depois) == 1:  # a anterior sozinha: tambem nao junta consigo mesma
                sozinha = reunioes.juntar_com_a_anterior(alvo)
            else:
                sozinha = None
        cobrar(de_novo is None and ninguem is None and sozinha is None, "I57-idempotencia", f"{base}: chamar de novo devolveu {de_novo!r}/{ninguem!r}/{sozinha!r}", "I57-idempotencia")
        cobrar(_snap(pasta) == depois, "I57-idempotencia-arquivos", f"{base}: chamar de novo mudou arquivos", "I57-idempotencia-arquivos")
        return f"{base}: junta com a anterior"
    finally:
        gravador.juntar_wavs = salvo[6]
        (config.SINCRONIZAR_AUDIO, reunioes.encerrar_gravacao, reunioes._vincular_telas, reunioes._juntar_audio_nos_videos, reunioes.webbrowser,
         reunioes.monitor.notificar) = salvo[:6]
        G.ativo, G.automatica, G.inicio = salvo_g
        for p in list(pasta.iterdir()):
            if MARCA in p.name:
                p.unlink(missing_ok=True)


# ---------------------------------------------------------------- I58: copia local da agenda

H = timedelta(hours=1)


def _iso(dt, estilo):
    if estilo == "z":
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.astimezone().isoformat()


def _reuniao_falsa(r, k, agora, off_h, codigo=None, duracao_h=1.0, estilo="local", link=True):
    ini = agora + off_h * H
    return {"id": f"ag{k}", "titulo": f"Agenda {k}", "descricao": "", "local": "", "inicio": _iso(ini, estilo), "fim": _iso(ini + duracao_h * H, estilo),
            "dia_inteiro": False, "link": "https://meet.example/x" if link else None, "link_agenda": None, "codigo_meet": codigo,
            "participantes": [], "anexos": [], "ja_terminou": False, "cliente_uuid": None, "consultor": ""}


OFFSETS = [-80, -49.5, -47.5, -30, -1, 0.5, 3, 25, 100, 166.5, 169.5, 200, -23, 23, 160]
CORRUPCOES = [b"", b"{", b"null", b"5", b"true", b'"texto"', b"[1, 2, 3]", b'{"a": 1}', b"\xff\xfe\x00bad", b"[{\"id\": \"x\"}]", b"[null, 5, {}]"]
CODIGOS = ["abc-defg-hij", "klm-nopq-rst", "uvw-xyza-bcd"]


def _ids(lista):
    return {x["id"]: x for x in lista}


def f_agenda_local(m, r):
    """I58: a copia local da agenda (google_services._guardar_agenda / reunioes_guardadas / AGENDA_LOCAL) e o plano B de _reuniao_da_janela.
    guardar: troca so o periodo consultado (o que sumiu do Google, cancelado, sai da copia; o que esta fora do periodo fica), guarda so de
    2 dias atras a 7 a frente, sem duplicar id, sem sobrar .tmp, nunca levanta (nem com arquivo corrompido ou item lixo), e
    listar_reunioes de verdade chama _guardar_agenda e devolve so os eventos com link (Google falhou: nada e gravado).
    janela: com g.listar_reunioes levantando, a chamada e associada pela copia (codigo do Meet, ou o unico em andamento); com o Google
    respondendo (mesmo vazio) a copia NUNCA vale; o resultado e igual ao do caminho normal com os mesmos candidatos."""
    pc = m.pc(r)
    cen = r.choice(["guardar", "guardar", "listar", "listar_falha", "corrompido", "envenenado", "janela", "janela", "janela_google_ok"])
    pasta = Path(tempfile.mkdtemp(prefix="agenor-agenda-", dir=amb.TMP))
    arq = pasta / "agenda_local.json"
    salvo = (g.AGENDA_LOCAL, g._eventos, g.listar_reunioes)
    saida = io.StringIO()
    g.AGENDA_LOCAL = arq
    agora = datetime.now(timezone.utc)
    try:
        assert amb.TMP in g.AGENDA_LOCAL.parents
        if cen in ("guardar", "listar", "listar_falha"):
            antigas = [_reuniao_falsa(r, f"a{k}", agora, r.choice(OFFSETS), estilo=r.choice(["local", "z"])) for k in range(r.randint(0, 6))]
            arq.write_text(json.dumps(antigas), encoding="utf-8")
            da, df = r.choice([1, 7]), r.choice([1, 7])
            ini, fim = agora - timedelta(days=da), agora + timedelta(days=df)
            novas = [_reuniao_falsa(r, f"{'a' if r.random() < 0.3 else 'n'}{k}", agora, r.choice(OFFSETS), estilo=r.choice(["local", "z"]),
                                    link=r.random() < 0.85) for k in range(r.randint(0, 6))]
            novas = list(_ids(novas).values())
            if r.random() < 0.15:
                novas.append({**_reuniao_falsa(r, "dia", agora, 5), "inicio": "2030-01-05", "fim": "2030-01-06", "dia_inteiro": True})
            lixo_sem_id = cen == "guardar" and r.random() < 0.1
            if lixo_sem_id:
                novas.append({"inicio": _iso(agora + 3 * H, "local")})
            em = lambda x, a, b: a <= x <= b  # noqa: E731
            off = lambda reun: (datetime.fromisoformat(reun["inicio"].replace("Z", "+00:00")) - agora) / H  # noqa: E731
            if cen == "listar_falha":
                g._eventos = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sem internet (falso)"))
                antes = arq.read_bytes()
                try:
                    with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
                        amb.LISTAR_REUNIOES_REAL(da, df)
                    cobrar(False, "I58-listar-nao-propagou", f"{pc}: o Google falhou e listar_reunioes devolveu em vez de levantar")
                except RuntimeError:
                    pass
                cobrar(arq.read_bytes() == antes, "I58-guardou-na-falha", f"{pc}: o Google falhou e a copia local mudou")
                return f"{pc} agenda: Google falhou, copia intacta"
            with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
                if cen == "listar":
                    g._eventos = lambda a, b: [dict(e) for e in novas if e.get("id")]
                    devolvidas = amb.LISTAR_REUNIOES_REAL(da, df)
                    cobrar([e["id"] for e in devolvidas] == [e["id"] for e in novas if e.get("id") and e["link"]], "I58-listar-filtro",
                           f"{pc}: listar_reunioes devolveu {[e['id'] for e in devolvidas]}, so os com link eram {[e['id'] for e in novas if e.get('id') and e['link']]}")
                    novas_g = [e for e in novas if e.get("id") and e["link"]]
                else:
                    g._guardar_agenda(ini, fim, novas)
                    novas_g = novas
            esperado = {}
            if lixo_sem_id:  # item sem id: o sistema desiste de guardar (sem levantar) e a copia fica como estava
                esperado = None
            else:
                for x in antigas:
                    if not em(off(x), -24 * da, 24 * df) and x.get("id"):
                        esperado[x["id"]] = x
                for x in novas_g:
                    esperado[x["id"]] = x
                esperado = {i: x for i, x in esperado.items() if x["inicio"].count("T") and em(off(x), -48, 168)}
            guardado = g.reunioes_guardadas()
            ids = [x["id"] for x in guardado]
            cobrar(len(ids) == len(set(ids)), "I58-repetido", f"{pc}: a copia local repete ids: {ids}")
            if esperado is None:
                cobrar(_ids(guardado) == {x["id"]: x for x in antigas}, "I58-lixo-mudou-copia", f"{pc}: item sem id e a copia mudou")
            else:
                cobrar(_ids(guardado) == esperado, "I58-conteudo",
                       f"{pc}: periodo -{da}d..+{df}d: a copia ficou com {sorted((i, round(off(x))) for i, x in _ids(guardado).items())}, esperado "
                       f"{sorted((i, round(off(x))) for i, x in esperado.items())}", f"I58-conteudo:{cen}")
            cobrar(not arq.with_suffix(".tmp").exists(), "I58-tmp", f"{pc}: sobrou {arq.with_suffix('.tmp').name}")
            return f"{pc} agenda {cen}: {len(guardado)} guardadas"

        if cen in ("corrompido", "envenenado"):
            bom = [_reuniao_falsa(r, f"n{k}", agora, r.choice([-30, -1, 3, 24, 100]), estilo=r.choice(["local", "z"])) for k in range(r.randint(1, 3))]
            bom = list(_ids(bom).values())
            if cen == "envenenado":  # item guardado com 'inicio' que nao e data: nao pode impedir a copia de se refazer
                arq.write_text(json.dumps([{"id": "ruim", "inicio": r.choice(["lixo", "2030-99-99T99:99", "T", "2030-01-01T"])}]), encoding="utf-8")
            else:
                modo = r.choice(["bytes", "bytes", "pasta"])
                if modo == "pasta":
                    arq.mkdir()
                else:
                    arq.write_bytes(r.choice(CORRUPCOES))
            with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
                try:
                    lido = g.reunioes_guardadas()
                except Exception as e:
                    cobrar(False, "I58-guardadas-levanta", f"{pc}: arquivo {arq.read_bytes()[:30] if arq.is_file() else 'pasta'!r}: reunioes_guardadas levantou {type(e).__name__}: {e}",
                           f"I58-guardadas-levanta:{type(e).__name__}")
                cobrar(isinstance(lido, list) and all(isinstance(x, dict) and x.get("id") and x.get("inicio") for x in lido), "I58-guardadas-lixo", f"{pc}: devolveu {lido!r}")
                if cen == "corrompido" and arq.is_file() and arq.read_bytes() not in (b"[{\"id\": \"x\"}]",):
                    cobrar(lido == [], "I58-guardadas-corrompido", f"{pc}: arquivo {arq.read_bytes()[:30]!r} -> {lido!r}, esperado lista vazia")
                if arq.is_dir():
                    arq.rmdir()
                    arq.write_text("[]", encoding="utf-8")
                try:
                    g._guardar_agenda(agora - timedelta(days=1), agora + timedelta(days=7), bom)
                except Exception as e:
                    cobrar(False, "I58-guardar-levanta", f"{pc}: _guardar_agenda levantou {type(e).__name__}: {e}", f"I58-guardar-levanta:{type(e).__name__}")
            depois = _ids(g.reunioes_guardadas())
            cobrar(set(depois) >= {x["id"] for x in bom}, "I58-nao-se-refaz",
                   f"{pc}: {cen}: depois de _guardar_agenda com {[x['id'] for x in bom]} a copia tem {sorted(depois)} (uma entrada ruim guardada trava a copia para sempre)",
                   f"I58-nao-se-refaz:{cen}")
            return f"{pc} agenda {cen}"

        # ---- janela: a chamada detectada e associada a reuniao
        reuniao_ate = lambda off_h, dur=1.0, **k: _reuniao_falsa(r, k.pop("n"), agora, off_h, duracao_h=dur, estilo=r.choice(["local", "z"]), **k)  # noqa: E731
        itens = []
        for k in range(r.randint(0, 5)):
            itens.append(reuniao_ate(r.choice([-0.7, -0.1, 0.1, 0.2, 0.4, 1.0, -73, 120, 240]), r.choice([0.5, 1.0, 2.0]),
                                     codigo=r.choice(CODIGOS + [None]), n=f"j{k}"))
        guardadas_ok = [x for x in itens if -48 <= (datetime.fromisoformat(x["inicio"].replace("Z", "+00:00")) - agora) / H <= 168]
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
            if r.random() < 0.8:
                g._guardar_agenda(agora - timedelta(days=1), agora + timedelta(days=1), itens)
            else:
                guardadas_ok = []  # sem copia (nunca guardou)
            if r.random() < 0.1 and cen == "janela":
                arq.write_bytes(r.choice(CORRUPCOES))
                guardadas_ok = [x for x in guardadas_ok if False]
            codigo = r.choice(CODIGOS + [None, None])
            titulo = f"Meet - {codigo} - Google Chrome" if codigo and r.random() < 0.5 else (f"Meet - Revisao {codigo} - Google Chrome" if codigo else "Meet - Revisao semanal - Google Chrome")
            if arq.exists() and arq.read_bytes() in CORRUPCOES:
                guardadas_ok = []

            def esperado_de(cands):
                if codigo:
                    return next((x for x in cands if x["codigo_meet"] == codigo), None)
                andamento = [x for x in cands if datetime.fromisoformat(x["inicio"].replace("Z", "+00:00")) - timedelta(minutes=15) <= datetime.now(timezone.utc)
                             <= datetime.fromisoformat(x["fim"].replace("Z", "+00:00"))]
                return andamento[0] if len(andamento) == 1 else None
            if cen == "janela_google_ok":
                g.listar_reunioes = lambda dias_atras=7, dias_frente=7: []
            else:
                g.listar_reunioes = lambda dias_atras=7, dias_frente=7: (_ for _ in ()).throw(RuntimeError("sem internet (falso)"))
            try:
                got = reunioes._reuniao_da_janela(titulo)
            except Exception as e:
                cobrar(False, "I58-janela-levanta", f"{pc}: {cen}: _reuniao_da_janela({titulo!r}) levantou {type(e).__name__}: {e}", f"I58-janela-levanta:{type(e).__name__}")
            esperado = None if cen == "janela_google_ok" else esperado_de(guardadas_ok)
            if esperado is None:
                cobrar(str(got.get("id", "")).startswith("avulsa-") and got.get("participantes") == [], "I58-janela-avulsa",
                       f"{pc}: {cen}: titulo {titulo!r}, copia {[(x['id'], x['codigo_meet']) for x in guardadas_ok]}: devolveu {got.get('id')!r}, esperado a reuniao avulsa",
                       f"I58-janela-avulsa:{cen}")
            else:
                cobrar(got.get("id") == esperado["id"], "I58-janela-fallback",
                       f"{pc}: Google fora, titulo {titulo!r}: devolveu {got.get('id')!r}, esperado {esperado['id']!r} (copia local {[(x['id'], x['codigo_meet']) for x in guardadas_ok]})",
                       "I58-janela-fallback")
                # o mesmo resultado pelo caminho normal com os mesmos candidatos
                g.listar_reunioes = lambda dias_atras=7, dias_frente=7: list(g.reunioes_guardadas())
                normal = reunioes._reuniao_da_janela(titulo)
                cobrar(normal == got, "I58-janela-difere-do-normal", f"{pc}: fallback {got.get('id')} difere do caminho normal {normal.get('id')}")
        return f"{pc} agenda {cen}: {titulo!r}"
    finally:
        g.AGENDA_LOCAL, g._eventos, g.listar_reunioes = salvo
        shutil.rmtree(pasta, ignore_errors=True)


# ---------------------------------------------------------------- I59: alucinacao do Whisper

ALUCINACOES = ["Legendas pela comunidade de Amara.org", "legendas por Fulano de Tal", "Legendas pela comunidade Amara.org", "Se inscreva no canal",
               "se inscreva no canal e ative o sininho", "Inscreva-se no canal", "inscreva no canal", "Obrigado por assistir!", "Obrigada por assistir.",
               "OBRIGADO POR ASSISTIR", "Deixe seu like", "deixe o like e compartilhe", "Até o próximo vídeo!", "ATÉ O PRÓXIMO VÍDEO",
               "Sous-titrage ST' 501", "Subtítulos realizados por la comunidad de Amara.org", "www.amara.org", "Subtítulos por la comunidad"]
LEGITIMAS = ["A legenda de preço está errada", "Vamos ver a legenda do gráfico", "Inscreva-se no site", "Inscreva-se no evento até sexta",
             "Tchau, tchau", "Obrigado pela atenção", "Obrigado por participar", "Deixe o relatório comigo", "Esse canal de vendas é o principal",
             "Até o próximo mês", "Posso assistir o vídeo depois?", "Bom dia a todos", "Preciso do like do gerente na proposta", "Tchau", "Obrigado"]


class _Seg:
    def __init__(self, texto, compression_ratio=1.0, no_speech_prob=0.0, avg_logprob=-0.3):
        self.text = texto
        self.compression_ratio, self.no_speech_prob, self.avg_logprob = compression_ratio, no_speech_prob, avg_logprob
        self.start, self.end = 0.0, 1.0


def f_alucinacao(m, r):
    """I59: frases que o Whisper inventa no silencio (alucinacao_conhecida) sao descartadas por segmento_confiavel mesmo com numeros
    bons; frases legitimas parecidas nao; o resto da regra (compressao > 2.4, silencio provavel, logprob) segue valendo; texto vazio/None
    nunca estoura; e transcrever_audio de verdade (modelo falso) so devolve os segmentos confiaveis."""
    pc = m.pc(r)
    prefixo = r.choice(["", "", "Entao, ", "  ", "...", "Bom dia. "])
    sufixo = r.choice(["", "", " ", " Obrigado.", "..."])
    ruim = r.choice(ALUCINACOES)
    ruim = r.choice([ruim, ruim.upper(), ruim.lower()]) if r.random() < 0.3 else ruim
    bom = r.choice(LEGITIMAS)
    cobrar(gravador.alucinacao_conhecida(prefixo + ruim + sufixo), "I59-nao-reconheceu", f"{pc}: {prefixo + ruim + sufixo!r} devia ser alucinacao conhecida", f"I59-nao-reconheceu:{ruim.lower()[:20]}")
    cobrar(not gravador.segmento_confiavel(_Seg(prefixo + ruim + sufixo)), "I59-manteve", f"{pc}: segmento {ruim!r} com numeros bons foi mantido", f"I59-manteve:{ruim.lower()[:20]}")
    cobrar(not gravador.alucinacao_conhecida(prefixo + bom + sufixo), "I59-falso-positivo", f"{pc}: {prefixo + bom + sufixo!r} e fala legitima e foi tratada como alucinacao",
           f"I59-falso-positivo:{bom[:20]}")
    cobrar(gravador.segmento_confiavel(_Seg(prefixo + bom + sufixo)), "I59-descartou-legitima", f"{pc}: {bom!r} com numeros bons foi descartada", f"I59-descartou-legitima:{bom[:20]}")
    # o resto da regra continua valendo
    cr, nsp, lp = r.choice([1.0, 2.4, 2.41, 3.0]), r.choice([0.0, 0.6, 0.61, 0.9]), r.choice([-0.3, -1.0, -1.01, -1.5, -1.51, -2.0])
    esperado = not (cr > 2.4) and not (nsp > 0.6 and lp < -1.0) and lp > -1.5
    cobrar(gravador.segmento_confiavel(_Seg(bom, cr, nsp, lp)) == esperado, "I59-regra-antiga",
           f"{pc}: {bom!r} compressao={cr} no_speech={nsp} logprob={lp}: segmento_confiavel={not esperado}, esperado {esperado}", f"I59-regra-antiga:{cr}:{nsp}:{lp}")
    for texto in (None, "", "   "):
        try:
            gravador.alucinacao_conhecida(texto or "")
            gravador.segmento_confiavel(_Seg(texto))
            gravador.segmento_confiavel(types.SimpleNamespace())
        except Exception as e:
            cobrar(False, "I59-estoura", f"{pc}: texto {texto!r}: {type(e).__name__}: {e}", "I59-estoura")
    # de ponta a ponta: o modelo falso devolve legitimas e alucinacoes misturadas
    n = r.randint(2, 6)
    textos = []
    for k in range(n):
        textos.append(r.choice(ALUCINACOES) if r.random() < 0.5 else f"{r.choice(LEGITIMAS)} {k}")
    segs = [_Seg(t) for t in textos]
    modelo = types.SimpleNamespace(transcribe=lambda audio, **kw: (iter(segs), types.SimpleNamespace(language="pt")))
    saiu = [t for _, _, t, _ in gravador.transcrever_audio(modelo, np.full(1600, 0.1, dtype=np.float32))]
    cobrar(saiu == [t for t in textos if t not in ALUCINACOES], "I59-transcrever", f"{pc}: transcrever_audio devolveu {saiu}, esperado so as legitimas de {textos}", "I59-transcrever")
    return f"{pc} alucinacao {ruim[:20]!r} / {bom[:20]!r}"


ACOES10 = A9.ACOES9 + [
    Acao("juntar a gravacao que voltou com a anterior", f_juntar_gravacoes, 4, True,
         ["reunioes.juntar_com_a_anterior", "reunioes.ao_encerrar_meet", "gravador.juntar_wavs"]),
    Acao("copia local da agenda e plano B da janela", f_agenda_local, 3, True,
         ["google_services._guardar_agenda", "google_services.reunioes_guardadas", "google_services.listar_reunioes", "reunioes._reuniao_da_janela"]),
    Acao("alucinacao do Whisper", f_alucinacao, 2, True, ["gravador.alucinacao_conhecida", "gravador.segmento_confiavel"]),
]
SEM_ACAO = A9.SEM_ACAO
SEM_FUZZ = A9.SEM_FUZZ
