"""Sincroniza os dados entre PCs por uma pasta do Google Drive.

O que vai para o Drive (pasta "Reuniões - dados do app"):
- dados.json            clientes e tarefas (mesclados registro a registro: vale a alteração mais recente)
- convites.json         qual convite do Outlook é de qual evento do Google (por evento, vale o mais recente)
- ata__<id>.json        uma ata por arquivo (vale a versão mais recente)
- grav__<nome>.json     gravações aguardando a decisão "Gerar ata?"
- grav__<nome>.txt      transcrições das gravações
- grav__<nome>.wav      áudios, só com SINCRONIZAR_AUDIO=sim (são grandes)
- config__credentials.json  a credencial do Google, para configurar outro PC
- pc__<nome do PC>      sinal de vida de cada PC (quando sincronizou), para avisar se um deles parou
- copia__<PC>__dados-<data>.db  cópias diárias do banco de cada PC (as 7 mais recentes), contra perda do disco

O código NÃO vai para o Drive: ele fica no GitHub (git pull).
"""

import json
import os
import socket
from pathlib import Path
import threading
import time
import traceback
from datetime import datetime, timezone

import atas
import config
import db
import google_services as g

ESTADO = {"rodando": False, "ultima": None, "erro": None, "resumo": "", "outros_pcs": {}}
PC = (socket.gethostname() or "pc").strip() or "pc"
PARADO_DIAS = 3  # outro PC sem sincronizar há mais que isso: o painel avisa (fim de semana desligado não conta)
_lock = threading.Lock()
_agendada = threading.Event()


EXCLUIDOS_ARQ = config.DADOS_DIR / "excluidos.json"  # o que foi excluído (vale para todos os PCs)


def excluidos() -> dict:
    try:
        return json.loads(EXCLUIDOS_ARQ.read_text(encoding="utf-8"))
    except Exception:
        return {}


def marcar_excluido(*nomes: str) -> None:
    """Registra exclusões (nomes como ficam no Drive: ata__<id>.json, grav__<arquivo>, evento__<id>),
    para os outros PCs apagarem as cópias deles e ninguém reenviar o que foi excluído."""
    lista = excluidos()
    for nome in nomes:
        lista[nome] = db.agora()
    EXCLUIDOS_ARQ.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")


def _caminho_local(nome: str):
    """Arquivo local correspondente a um nome do Drive — só dentro das pastas do app."""
    if nome.startswith("ata__"):
        pasta, arquivo = config.ATAS_DIR, nome[len("ata__"):]
    elif nome.startswith("grav__"):
        pasta, arquivo = config.GRAVACOES_DIR, nome[len("grav__"):]
    else:
        return None
    # nome com barra, "..", ou que não seja um arquivo simples: ignora (não deixa sair da pasta)
    if not arquivo or "/" in arquivo or "\\" in arquivo or ".." in arquivo or ":" in arquivo or Path(arquivo).name != arquivo:
        return None
    return pasta / arquivo


def _apagar_local(nome: str) -> bool:
    """Apaga a cópia local correspondente a um nome do Drive."""
    caminho = _caminho_local(nome)
    if caminho is None:
        return False
    if caminho.exists():
        caminho.unlink()
        return True
    return False


def _mtime(caminho) -> str:
    return datetime.fromtimestamp(os.path.getmtime(caminho), timezone.utc).isoformat(timespec="milliseconds")


def _json_local(caminho) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        return {}


def sincronizar() -> str:
    """Faz uma rodada completa de sincronização. Devolve um resumo do que mudou."""
    if not config.SINCRONIZAR:
        return "Sincronização desligada (SINCRONIZAR=nao)."
    with _lock:
        ESTADO["rodando"] = True
        try:
            resumo = _sincronizar()
            ESTADO.update(ultima=datetime.now(), erro=None, resumo=resumo)
            g.anotar_conexao()
            return resumo
        except Exception as e:
            traceback.print_exc()
            ESTADO["erro"] = str(e)
            g.anotar_conexao(e)
            raise
        finally:
            ESTADO["rodando"] = False


def _sincronizar() -> str:
    pasta = g.drive_pasta(config.PASTA_DRIVE)
    remotos = g.drive_listar(pasta)
    enviados = baixados = apagados = 0

    # 0) Exclusões: junta a lista dos dois lados e apaga o que foi excluído, aqui e no Drive
    lista = excluidos()
    remoto_exc = remotos.get("excluidos.json")
    texto_exc_remoto = g.drive_baixar(remoto_exc["id"]).decode("utf-8") if remoto_exc else None
    if texto_exc_remoto:
        for nome, quando in json.loads(texto_exc_remoto).items():
            lista[nome] = max(quando, lista.get(nome, ""))
    texto_exc = json.dumps(lista, ensure_ascii=False, sort_keys=True)
    EXCLUIDOS_ARQ.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")
    for nome in lista:
        if nome in remotos:
            g.drive_apagar(remotos.pop(nome)["id"])
            apagados += 1
        if _apagar_local(nome):
            apagados += 1
    if texto_exc_remoto != texto_exc:
        g.drive_enviar(pasta, "excluidos.json", texto_exc.encode("utf-8"), file_id=remoto_exc["id"] if remoto_exc else None)

    # 1) Clientes e tarefas: mescla e publica o resultado
    remoto_dados = remotos.get("dados.json")
    mudancas = 0
    texto_remoto = None
    if remoto_dados:
        texto_remoto = g.drive_baixar(remoto_dados["id"]).decode("utf-8")
        mudancas = db.mesclar(json.loads(texto_remoto))
    texto_local = json.dumps(db.exportar(), ensure_ascii=False, sort_keys=True)
    if texto_local != texto_remoto:
        g.drive_enviar(pasta, "dados.json", texto_local.encode("utf-8"),
                       file_id=remoto_dados["id"] if remoto_dados else None)
        enviados += 1

    # 1b) Vínculos evento do Google -> convite do Outlook: sem eles, remarcar/cancelar em outro PC iria pelo Google
    #     e os convidados ficariam com dois convites
    import envio
    remoto_conv = remotos.get("convites.json")
    texto_conv_remoto = None
    if remoto_conv:
        texto_conv_remoto = g.drive_baixar(remoto_conv["id"]).decode("utf-8")
        try:
            envio.mesclar_convites(json.loads(texto_conv_remoto))
        except ValueError:
            pass  # arquivo remoto estragado: o deste PC o substitui
    texto_conv = json.dumps(envio._ler(), ensure_ascii=False, sort_keys=True)
    if texto_conv != texto_conv_remoto and texto_conv != "{}":
        g.drive_enviar(pasta, "convites.json", texto_conv.encode("utf-8"),
                       file_id=remoto_conv["id"] if remoto_conv else None)
        enviados += 1

    # 2) Atas: a versão mais recente vence
    locais = {f"ata__{p.stem}.json": p for p in config.ATAS_DIR.glob("*.json")}
    for nome in (set(locais) | {n for n in remotos if n.startswith("ata__")}) - set(lista):
        local, remoto = locais.get(nome), remotos.get(nome)
        quando_local = (_json_local(local).get("atualizado_em") or _mtime(local)) if local else ""
        quando_remoto = (remoto or {}).get("appProperties", {}).get("atualizado_em", "")
        if local and quando_local > quando_remoto:
            g.drive_enviar(pasta, nome, local.read_bytes(), file_id=remoto["id"] if remoto else None,
                           props={"atualizado_em": quando_local})
            enviados += 1
        elif remoto and quando_remoto > quando_local:
            if _caminho_local(nome) is None:
                continue
            try:
                reg = json.loads(g.drive_baixar(remoto["id"]).decode("utf-8"))
            except ValueError:
                continue  # arquivo remoto corrompido: mantém o local
            if not isinstance(reg, dict):
                continue
            cli = db.cliente_por_uuid(reg.get("cliente_uuid"))  # o id do cliente muda de um PC para outro
            reg["cliente_id"] = cli["id"] if cli else None
            atas.salvar_bruto(nome[len("ata__"):-len(".json")], reg)
            baixados += 1

    # 3) Gravações: pendências (.json), transcrições (.txt) e, se ligado, áudios (.wav)
    extensoes = (".json", ".txt") + ((".wav",) if config.SINCRONIZAR_AUDIO else ())
    locais = {f"grav__{p.name}": p for p in config.GRAVACOES_DIR.iterdir() if p.suffix in extensoes}
    for nome in (set(locais) | {n for n in remotos if n.startswith("grav__") and n.endswith(extensoes)}) - set(lista):
        local, remoto = locais.get(nome), remotos.get(nome)
        if nome.endswith(".json"):
            quando_local = (_json_local(local).get("atualizado_em") or _mtime(local)) if local else ""
        else:
            quando_local = "1" if local else ""  # texto/áudio não mudam depois de criados
        quando_remoto = (remoto or {}).get("appProperties", {}).get("atualizado_em", "")
        if not nome.endswith(".json") and remoto:
            quando_remoto = "1"
        if local and quando_local > quando_remoto:
            tipo = {"json": "application/json", "txt": "text/plain", "wav": "audio/wav"}[nome.rsplit(".", 1)[1]]
            g.drive_enviar(pasta, nome, caminho=local, mimetype=tipo, file_id=remoto["id"] if remoto else None,
                           props={"atualizado_em": quando_local})
            enviados += 1
        elif remoto and quando_remoto > quando_local:
            destino = _caminho_local(nome)
            if destino is not None:
                g.drive_baixar(remoto["id"], destino=destino)
                baixados += 1

    # 4) Credencial do Google, para facilitar a instalação em outro PC
    if config.CREDENTIALS_FILE.exists() and "config__credentials.json" not in remotos:
        g.drive_enviar(pasta, "config__credentials.json", config.CREDENTIALS_FILE.read_bytes())
    elif not config.CREDENTIALS_FILE.exists() and "config__credentials.json" in remotos:
        config.CREDENTIALS_FILE.write_bytes(g.drive_baixar(remotos["config__credentials.json"]["id"]))

    # 5) Sinal de vida deste PC, e quando cada um dos outros sincronizou pela última vez
    _sinal_de_vida(pasta, remotos)

    partes = []
    if apagados:
        partes.append(f"{apagados} item(ns) excluído(s)")
    if mudancas:
        partes.append(f"{mudancas} tarefa(s)/cliente(s) atualizados")
    if baixados:
        partes.append(f"{baixados} arquivo(s) recebidos")
    if enviados:
        partes.append(f"{enviados} arquivo(s) enviados")
    return ", ".join(partes) or "Tudo em dia"


def _data(texto) -> datetime | None:
    """Data/hora com fuso de um appProperties (ou None, se vier qualquer outra coisa)."""
    try:
        quando = datetime.fromisoformat(texto) if isinstance(texto, str) else None
    except ValueError:
        return None
    return quando if quando and quando.tzinfo else None


def _quando_remoto(arquivo) -> datetime | None:
    props = arquivo.get("appProperties") if isinstance(arquivo, dict) else None
    return _data(props.get("atualizado_em")) if isinstance(props, dict) else None


def _sinal_de_vida(pasta, remotos: dict) -> None:
    meu_nome = f"pc__{PC}"
    meu = remotos.get(meu_nome)
    antes = _quando_remoto(meu)
    agora = datetime.now(timezone.utc)
    if antes is None or (agora - antes).total_seconds() >= 3600:  # no máximo uma escrita por hora
        g.drive_enviar(pasta, meu_nome, b"{}", file_id=meu["id"] if meu else None,
                       props={"atualizado_em": agora.isoformat(timespec="seconds")})
    outros = {}
    for nome, arquivo in remotos.items():
        quando = _quando_remoto(arquivo)
        if nome.startswith("pc__") and nome != meu_nome and len(nome) > 4 and quando:
            outros[nome[4:]] = quando
    ESTADO["outros_pcs"] = outros


def pcs_parados(dias: float = PARADO_DIAS) -> list[tuple[str, datetime]]:
    """Outros PCs que não sincronizam há mais de `dias` (do mais antigo ao mais recente)."""
    agora = datetime.now(timezone.utc)
    return sorted(((pc, q) for pc, q in ESTADO["outros_pcs"].items() if (agora - q).total_seconds() > dias * 86400),
                  key=lambda x: x[1])


def esquecer_pc(pc: str) -> bool:
    """Tira do Drive o sinal de vida de um PC que não é mais usado (o aviso dele some)."""
    if not isinstance(pc, str) or pc not in ESTADO["outros_pcs"]:
        return False
    pasta = g.drive_pasta(config.PASTA_DRIVE)
    arquivo = g.drive_listar(pasta).get(f"pc__{pc}")
    if arquivo:
        g.drive_apagar(arquivo["id"])
    ESTADO["outros_pcs"].pop(pc, None)
    return True


def enviar_copias(manter: int = 7) -> int:
    """Sobe ao Drive as cópias diárias do banco deste PC (copia__<PC>__dados-<data>.db) e deixa lá só as
    `manter` mais recentes. As cópias locais somem junto com o disco; estas, não."""
    if not config.SINCRONIZAR:
        return 0
    locais = sorted((db.ARQUIVO.parent / "copias").glob("dados-*.db"))[-manter:]
    if not locais:
        return 0
    pasta = g.drive_pasta(config.PASTA_DRIVE)
    remotos = g.drive_listar(pasta)
    prefixo = f"copia__{PC}__"
    enviadas = 0
    for copia in locais:
        if prefixo + copia.name not in remotos:
            g.drive_enviar(pasta, prefixo + copia.name, caminho=copia, mimetype="application/octet-stream")
            enviadas += 1
    todas = sorted({n for n in remotos if n.startswith(prefixo)} | {prefixo + c.name for c in locais})
    for velha in todas[:-manter]:
        if velha in remotos:
            g.drive_apagar(remotos[velha]["id"])
    return enviadas


def arquivar_videos(ocupado=lambda: False) -> list[str]:
    """Tira do PC os vídeos de reunião com mais de VIDEOS_DIAS dias (são grandes). Com VIDEOS_NO_DRIVE, cada um vai
    antes para a pasta de vídeos do Drive e só é apagado daqui se o tamanho lá conferir. Devolve os nomes tirados."""
    if config.VIDEOS_DIAS <= 0:
        return []
    limite = time.time() - config.VIDEOS_DIAS * 86400
    velhos = sorted(p for p in config.GRAVACOES_DIR.glob("*.webm")
                    if not p.stem.endswith("-tela") and "-tela-" not in p.stem and p.stat().st_mtime < limite)
    if not velhos:
        return []
    tirados = []
    pasta = g.drive_pasta(config.PASTA_VIDEOS) if config.VIDEOS_NO_DRIVE else None
    for video in velhos:
        if ocupado():  # não disputa disco/rede com gravação ou ata em andamento; continua na próxima rodada
            break
        if pasta:
            no_drive = g.drive_tamanhos(pasta)
            if video.stat().st_size not in no_drive.get(video.name, ()):
                g.drive_enviar(pasta, video.name, caminho=video, mimetype="video/webm")
                no_drive = g.drive_tamanhos(pasta)
            if video.stat().st_size not in no_drive.get(video.name, ()):
                print(f"[videos] {video.name}: tamanho no Drive não confere; fica no PC")
                continue
        video.unlink()
        tirados.append(video.name)
    return tirados


def agendar(atraso: float = 8) -> None:
    """Pede uma sincronização em breve (várias alterações seguidas viram uma rodada só)."""
    _agendada.set()


def iniciar_automatico(intervalo_min: int = 5) -> None:
    """Sincroniza ao abrir, a cada `intervalo_min` minutos e logo depois de alterações."""
    if not config.SINCRONIZAR:
        return

    def loop():
        time.sleep(5)
        proxima = 0.0
        while True:
            if time.time() >= proxima or _agendada.is_set():
                if _agendada.is_set():
                    time.sleep(8)  # junta alterações em sequência
                _agendada.clear()
                try:
                    print(f"[sincronia] {sincronizar()}")
                except Exception as e:
                    print(f"[sincronia] erro: {e}")
                proxima = time.time() + intervalo_min * 60
            _agendada.wait(timeout=5)

    threading.Thread(target=loop, daemon=True, name="sincronia").start()
