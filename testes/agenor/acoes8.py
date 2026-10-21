"""Catalogo v8 = catalogo v7 (acoes7.py, CONGELADO) + a novidade do som do PC (commit 02e0eb6 + `_TrilhaExterna.start()`):
quando a extensao do Chrome nao manda som, a trilha "outros" passa a gravar o som do PC (loopback) e volta sozinha.

A acao f_som_mudo (v7) segue valendo: ela usa o Gravador SEM trilhas, entao o som do PC nunca esta em uso e o aviso de som mudo
dispara como antes. Aqui o Gravador tem uma `_TrilhaExterna` REAL (WAV em pasta temporaria) e o `_SomDoPC` e um duble (nada de
soundcard) que pode falhar na hora, falhar depois ou funcionar, e que alimenta com do_pc=True um valor identificavel (-0.25;
a extensao manda 0.5). O modelo do aviso de som mudo desta acao ja e a regra nova: so avisa se o som do PC NAO esta em uso."""
import datetime as _dt
import shutil
import tempfile
import threading as _threading
import time as _time
import wave as _wave
from pathlib import Path

import numpy as np

import acoes7 as A7
import ambiente as amb
import gravador
from acoes import Acao
from oraculo import cobrar

reunioes = amb.reunioes
monitor = amb.monitor
ABA = A7.ABA

V_EXT = 16384            # int16 que a extensao manda (0.5)
WAV_EXT = int(np.float32(V_EXT / 32768.0) * 32767)
WAV_PC = int(np.float32(-0.25) * 32767)


class _SomDoPCFalso:
    """No lugar de gravador._SomDoPC: nada de soundcard. `modo`: ok | falha_na_hora | falha_depois."""
    criados = []
    modo = "ok"

    def __init__(self, trilha, parar):
        self.trilha, self.parar, self.erro, self.rodando = trilha, parar, None, False
        _SomDoPCFalso.criados.append(self)

    def start(self):
        self.rodando = True
        if _SomDoPCFalso.modo == "falha_na_hora":
            self.falhar()

    def falhar(self):  # o mesmo que o `except` do _SomDoPC de verdade
        self.erro = RuntimeError("loopback falso falhou")
        self.trilha.do_pc = False
        self.rodando = False

    def join(self, timeout=None):
        pass

    def alimentar(self, mono):  # o que a thread de captura faria
        if self.rodando and self.erro is None:
            self.trilha.alimentar(mono, do_pc=True)


class _WaveLento:
    """gravador.wave com abertura lenta: se a trilha abrir o WAV so dentro da thread, o primeiro audio apos start() se perde."""
    def open(self, *a, **k):
        _time.sleep(0.03)
        return _wave.open(*a, **k)

    def __getattr__(self, nome):
        return getattr(_wave, nome)


def _blocos(valores):
    """[(valor, n)...] sem zeros, juntando vizinhos iguais."""
    saida = []
    for v, n in valores:
        if v == 0 or n == 0:
            continue
        if saida and saida[-1][0] == v:
            saida[-1][1] += n
        else:
            saida.append([v, n])
    return [tuple(x) for x in saida]


def _blocos_do_wav(caminho):
    with _wave.open(str(caminho), "rb") as w:
        dados = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if not len(dados):
        return [], 0
    cortes = np.flatnonzero(np.diff(dados)) + 1
    ini = np.concatenate([[0], cortes])
    fim = np.concatenate([cortes, [len(dados)]])
    return _blocos([(int(dados[a]), int(b - a)) for a, b in zip(ini, fim)]), len(dados)


def f_som_do_pc(m, r):
    """Troca da trilha "outros" entre a extensao e o som do PC, com Gravador/_TrilhaExterna reais e o loopback falso.
    I53: so troca depois de SOM_PC_SEG de silencio em chamada; volta quando chega som; nunca grava os dois (o WAV so tem
    amostras da fonte em uso); duble que falha => nao tenta de novo e o aviso de som mudo funciona como antes; sem gravacao,
    gravacao manual, nao automatica ou sem trilha externa nada troca; trilha alinhada no tempo; o primeiro audio logo depois
    de start() nao se perde; parar() desliga tudo; /gravacao mostra 'gravada pelo som do PC' so quando em uso."""
    pc = m.pc(r)
    G = reunioes.GRAVADOR
    cenario = r.choices(["certo", "sem_trilha", "manual", "nao_automatica", "sem_gravacao", "arquivo_ruim"], [10, 2, 1, 1, 1, 1])[0]
    modo = r.choices(["ok", "falha_na_hora", "falha_depois"], [5, 2, 3])[0]
    pasta = Path(tempfile.mkdtemp(prefix="agenor-som-"))
    campos = ("ativo", "automatica", "pela_extensao", "reuniao", "inicio", "arquivos", "_parar", "_trilhas", "trilhas", "_som_do_pc")
    salvo_g = {k: getattr(G, k) for k in campos}
    salvo_e = dict(reunioes.EXTENSAO)
    salvo = (reunioes.time, reunioes.threading, monitor.notificar, reunioes.ao_detectar_meet, gravador.time, gravador.wave, gravador._SomDoPC)
    rel = A7._Relogio()
    detectou = []
    A7.NOTIFICACOES["lista"] = lista = []
    A7.NOTIFICACOES["fn"] = lambda *a, **k: lista.append(a)
    SEG_PC, SEG_MUDO = reunioes.SOM_PC_SEG, reunioes.SOM_MUDO_SEG
    _SomDoPCFalso.criados, _SomDoPCFalso.modo = [], modo
    trilha = None
    meu_parar = _threading.Event()
    try:
        reunioes.time, reunioes.threading, gravador.time = rel, A7._Threading(), rel
        gravador.wave = _WaveLento()
        gravador._SomDoPC = _SomDoPCFalso
        monitor.notificar = A7.NOTIFICACOES["fn"]
        reunioes.ao_detectar_meet = lambda *a, **k: detectou.append(a)
        G._parar = meu_parar
        caminho = pasta / ("nao-existe" if cenario == "arquivo_ruim" else "") / "outros.wav"
        G.ativo, G.automatica, G.pela_extensao = {
            "manual": (True, False, False), "nao_automatica": (True, False, True), "sem_gravacao": (False, False, False),
        }.get(cenario, (True, True, True))
        G.reuniao, G.inicio, G.arquivos = {"titulo": "Reuniao teste", "id": "ev1"}, _dt.datetime.now(), {"outros": caminho}
        tem_externa = cenario in ("certo", "nao_automatica", "arquivo_ruim")
        if tem_externa:
            trilha = gravador._TrilhaExterna(caminho, meu_parar)
            trilha.start()
            G._trilhas, G.trilhas = [trilha], {"outros": trilha}
        elif cenario == "manual":  # a trilha "outros" de uma gravacao manual e uma _Trilha comum
            G._trilhas, G.trilhas = [], {"outros": gravador._Trilha(None, caminho, meu_parar)}
        else:
            G._trilhas, G.trilhas = [], {}
        G._som_do_pc = None
        pelo_ext = bool(G.ativo and G.automatica and G.pela_extensao)
        wav_ok = cenario in ("certo", "nao_automatica")
        reunioes.EXTENSAO.update(aba=ABA, som_em=rel.t, mudo_avisado=False, fora_desde=None, log_em=0.0, aba_sinal_em=rel.t, pico=0.0)
        inicio_trilha = rel.t

        # ---- modelo
        som_em, avisado, aba_ok, fora, esperadas = rel.t, False, True, None, 0
        usando, instanciados, falhou = False, 0, False
        aceitos = []  # (valor_no_wav, n) na ordem em que o WAV tem de ter
        ultimo_aceito_t = None

        def _alinhada():
            esperado = int((rel.t - inicio_trilha) * gravador.TAXA)
            cobrar(trilha._escritas >= esperado - gravador.TAXA // 2, "I53-trilha-desalinhada",
                   f"{pc}: trilha com {trilha._escritas} amostras em t={rel.t - inicio_trilha:.0f}s (devia ter ~{esperado})", "I53-trilha-desalinhada")

        def alimentar_ext(aba):
            nonlocal ultimo_aceito_t
            n = r.randint(20, 400)
            antes = trilha._escritas if trilha is not None else 0
            m.cli.post(f"/api/extensao/audio?aba={aba}", data=np.full(n, V_EXT, dtype="<i2").tobytes())
            deve = bool(pelo_ext and aba_ok and aba == ABA and wav_ok and not usando and tem_externa)
            if trilha is not None:
                aceito = trilha._escritas > antes
                cobrar(aceito == deve, "I53-audio-da-extensao", f"{pc}: {cenario}: audio da extensao (aba={aba}) {'entrou' if aceito else 'foi ignorado'}"
                       f" mas {'deveria entrar' if deve else 'deveria ser ignorado'} (som do PC em uso={usando})", f"I53-audio-extensao:{aceito}:{usando}")
                if aceito:
                    aceitos.append((WAV_EXT, n))
                    ultimo_aceito_t = rel.t
                    _alinhada()

        def alimentar_pc():
            nonlocal ultimo_aceito_t
            if not _SomDoPCFalso.criados:
                return
            n = r.randint(20, 400)
            antes = trilha._escritas if trilha is not None else 0
            _SomDoPCFalso.criados[0].alimentar(np.full(n, -0.25, dtype=np.float32))
            if trilha is not None:
                aceito = trilha._escritas > antes
                deve = bool(usando and wav_ok and _SomDoPCFalso.criados[0].erro is None)
                cobrar(aceito == deve, "I53-audio-do-pc", f"{pc}: {cenario}: audio do som do PC {'entrou' if aceito else 'foi ignorado'}"
                       f" mas {'deveria entrar' if deve else 'deveria ser ignorado'} (em uso={usando})", f"I53-audio-pc:{aceito}:{usando}")
                if aceito:
                    aceitos.append((WAV_PC, n))
                    ultimo_aceito_t = rel.t
                    _alinhada()

        # o primeiro audio, na hora depois de start(), antes de a thread rodar
        if tem_externa:
            alimentar_ext(ABA)

        passos = r.randint(3, 9)
        for k in range(passos):
            rel.t += r.choice(A7.PASSOS_T)
            if modo == "falha_depois" and _SomDoPCFalso.criados and not falhou and r.random() < 0.35:
                _SomDoPCFalso.criados[0].falhar()
                falhou, usando = True, False
            for _ in range(r.randint(0, 2)):
                alimentar_ext(ABA if r.random() < 0.85 else 8)
                alimentar_pc()
            lixo = r.random() < 0.1
            outra = r.random() < 0.12
            em_chamada = r.random() < 0.9
            valor, som = r.choice(A7.PICOS) if r.random() < 0.4 else (0, False)
            corpo, _ = A7._corpo_extensao(r, r.choice([8, "7", None, 0]) if outra else ABA, em_chamada, valor, lixo)
            if isinstance(corpo, dict) and "pico" not in corpo:
                som = False
            if isinstance(corpo, dict) and corpo.get("fim") and r.random() < 0.7:
                corpo.pop("fim")  # o fim da chamada encerra o passeio cedo demais
            resp = m.cli.post("/api/extensao/estado", json=corpo)
            cobrar(resp.status_code == 200, "I53-estado-5xx", f"{pc}: {cenario}: corpo {corpo!r} -> {resp.status_code}", "I53-estado-5xx")
            valido = pelo_ext and not lixo and not outra and aba_ok
            era_usando = usando
            if valido:
                if em_chamada:
                    fora = None
                if som:
                    som_em, avisado = rel.t, False
                    if usando:
                        usando = False
                elif em_chamada and rel.t - som_em >= SEG_PC and not usando and tem_externa:
                    if instanciados == 0:
                        instanciados = 1
                        falhou = falhou or modo == "falha_na_hora"
                    usando = not falhou
                if not som and em_chamada and not usando and rel.t - som_em >= SEG_MUDO and not avisado:
                    avisado = True
                    esperadas += 1
                if not em_chamada:
                    fora = fora or rel.t
                    if corpo.get("fim") or rel.t - fora >= monitor.FIM_APOS:
                        aba_ok = False
            # ---- oraculos
            real = G.usando_som_do_pc
            if real and not era_usando:
                cobrar(valido and em_chamada and rel.t - som_em >= SEG_PC, "I53-troca-cedo",
                       f"{pc}: {cenario}: trocou para o som do PC com {rel.t - som_em:.0f}s de silencio (min {SEG_PC}), corpo {corpo!r}", "I53-troca-cedo")
            cobrar(real == usando, "I53-som-do-pc-estado", f"{pc}: {cenario}: som do PC em uso={real}, esperado {usando} (t={rel.t - inicio_trilha:.0f}s, "
                   f"silencio={rel.t - som_em:.0f}s, falhou={falhou}, corpo {corpo!r})", f"I53-som-do-pc-estado:{real}:{cenario}:{modo}")
            if trilha is not None and tem_externa:
                cobrar(trilha.do_pc == usando, "I53-trilha-do-pc", f"{pc}: trilha.do_pc={trilha.do_pc}, esperado {usando}", "I53-trilha-do-pc")
            cobrar(len(_SomDoPCFalso.criados) <= 1, "I53-tentou-de-novo", f"{pc}: {cenario}: {len(_SomDoPCFalso.criados)} _SomDoPC criados na mesma gravacao", "I53-tentou-de-novo")
            if not tem_externa or not pelo_ext:
                cobrar(not _SomDoPCFalso.criados and not real, "I53-troca-indevida", f"{pc}: {cenario}: trocou sem poder (criados={len(_SomDoPCFalso.criados)})", "I53-troca-indevida")
            cobrar(len(lista) == esperadas, "I53-aviso-mudo-contagem",
                   f"{pc}: {cenario}/{modo}: passo {k}: notificou {len(lista)}x, esperado {esperadas}x (silencio={rel.t - som_em:.0f}s, "
                   f"som do PC em uso={real}, corpo {corpo!r})", f"I53-aviso-mudo-contagem:{cenario}:{modo}:{len(lista) > esperadas}")
            if pelo_ext and aba_ok:
                cobrar(bool(reunioes.EXTENSAO["mudo_avisado"]) == avisado, "I53-aviso-mudo-estado",
                       f"{pc}: mudo_avisado={reunioes.EXTENSAO['mudo_avisado']} esperado {avisado} (em uso={real}, corpo {corpo!r})", "I53-aviso-mudo-estado")
            if not aba_ok:
                break
        if cenario in ("manual", "nao_automatica", "sem_gravacao"):
            cobrar(not lista and not _SomDoPCFalso.criados, "I53-indevido", f"{pc}: {cenario}: notificou {len(lista)}x e criou {len(_SomDoPCFalso.criados)} _SomDoPC")

        # ---- a tela
        if G.ativo:
            texto = m.cli.get("/gravacao").get_data(as_text=True)
            mostra_pc = "gravada pelo som do PC" in texto
            cobrar(mostra_pc == bool(pelo_ext and usando), "I53-tela-som-do-pc",
                   f"{pc}: {cenario}: em uso={usando} e a tela {'mostra' if mostra_pc else 'nao mostra'} o texto", "I53-tela-som-do-pc")
            mostra_mudo = "não está chegando" in texto
            cobrar(mostra_mudo == bool(pelo_ext and reunioes.EXTENSAO["mudo_avisado"]), "I53-tela-mudo",
                   f"{pc}: {cenario}: mudo_avisado={reunioes.EXTENSAO['mudo_avisado']} e a tela {'mostra' if mostra_mudo else 'nao mostra'} o aviso", "I53-tela-mudo")

        # ---- parar de verdade
        if G.ativo and tem_externa:
            try:
                G.parar()
            except RuntimeError as e:
                cobrar(cenario == "arquivo_ruim", "I53-parar-estoura", f"{pc}: {cenario}: parar() levantou {e}")
            cobrar(not G.ativo and not G.usando_som_do_pc and G._som_do_pc is None and not G.pela_extensao, "I53-parar-nao-limpou",
                   f"{pc}: depois de parar(): ativo={G.ativo} som_do_pc={G.usando_som_do_pc} pela_extensao={G.pela_extensao}")
            cobrar(G.usar_som_do_pc(True) is False and G.usar_som_do_pc(False) is False, "I53-usar-sem-gravacao", f"{pc}: usar_som_do_pc sem gravacao devolveu True")
            cobrar(len(_SomDoPCFalso.criados) <= 1, "I53-usar-sem-gravacao-criou", f"{pc}: usar_som_do_pc sem gravacao criou _SomDoPC")
            G.alimentar_outros(np.full(50, 0.5, dtype=np.float32))  # nao estoura, nao escreve
            if cenario == "arquivo_ruim":
                cobrar(trilha.erro is not None and not caminho.exists(), "I53-arquivo-ruim", f"{pc}: WAV impossivel: erro={trilha.erro!r} existe={caminho.exists()}")
            else:
                blocos, total = _blocos_do_wav(caminho)
                esperado = _blocos(aceitos)
                cobrar(blocos == esperado, "I53-wav-misturado",
                       f"{pc}: {cenario}/{modo}: o WAV nao tem so o que devia: {blocos[:6]} (esperado {esperado[:6]}); mudar de fonte nao pode misturar nem perder",
                       "I53-wav-misturado")
                if ultimo_aceito_t is not None:
                    cobrar(total >= int((ultimo_aceito_t - inicio_trilha) * gravador.TAXA) - gravador.TAXA // 2, "I53-wav-curto",
                           f"{pc}: WAV com {total} amostras, devia ter cerca de {int((ultimo_aceito_t - inicio_trilha) * gravador.TAXA)}")
    finally:
        reunioes.time, reunioes.threading, monitor.notificar, reunioes.ao_detectar_meet, gravador.time, gravador.wave, gravador._SomDoPC = salvo
        meu_parar.set()
        if trilha is not None and trilha.is_alive():
            trilha.join(timeout=5)
        for k, v in salvo_g.items():
            setattr(G, k, v)
        reunioes.EXTENSAO.clear()
        reunioes.EXTENSAO.update(salvo_e)
        shutil.rmtree(pasta, ignore_errors=True)
    return f"{pc} som do PC cenario={cenario} modo={modo} criados={len(_SomDoPCFalso.criados)}"


ACOES8 = A7.ACOES7 + [
    Acao("som do PC no lugar da extensao (trilha real, loopback falso)", f_som_do_pc, 4, True,
         ["/api/extensao/estado", "/api/extensao/audio", "/gravacao", "reunioes.api_extensao_estado", "gravador.Gravador.usar_som_do_pc"]),
]
SEM_ACAO = dict(A7.SEM_ACAO)
SEM_ACAO["/api/extensao/estado e gravacao pela extensao"] = ("o audio real (soundcard) nao roda: o teste liga GRAVADOR.ativo/automatica/pela_extensao a mao; "
                                                               "I48 (sem trilhas) e I53 (trilha externa real + _SomDoPC falso); o caminho que comeca a gravar (ao_detectar_meet) e falso")
SEM_FUZZ = A7.SEM_FUZZ
