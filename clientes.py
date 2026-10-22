"""Visão por cliente e recursos de acompanhamento:

- clientes que precisam de atenção (tarefas atrasadas, pendências do cliente, tempo sem reunião)
- ficha do cliente (contatos, atas, tarefas, próximas reuniões, anotações)
- texto de cobrança das pendências do cliente
- perguntas às atas (Claude)
- resumo da semana e números do mês
- status do sistema
"""

import json
import os
import shutil
import subprocess
import time
import unicodedata
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field

import atas
import config
import db
import google_services as g
import ia

DIAS_SEM_REUNIAO = 30  # a partir daqui, o cliente aparece como "sem contato"


# ---------------------------------------------------------------- dados por cliente

_conta = {"email": None}


def minha_conta() -> str:
    """E-mail da conta Google conectada (para não aparecer como contato do cliente)."""
    if _conta["email"] is None:
        try:
            _conta["email"] = (g.autenticar(interativo=False) or "").lower()
        except Exception:
            _conta["email"] = ""
    return _conta["email"]


def atas_do_cliente(cliente_id) -> list[dict]:
    lista = []
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        if reg.get("ata") and reg.get("cliente_id") == cliente_id:
            lista.append({"event_id": event_id, **reg})
    return sorted(lista, key=lambda r: r["reuniao"].get("inicio", ""), reverse=True)


def contatos(cliente_id) -> list[dict]:
    """Pessoas que participaram das reuniões com o cliente (e-mail, nome, quantas reuniões)."""
    eu = minha_conta()
    pessoas = {}
    c = db.cliente(cliente_id) or {}
    for email in [e.strip() for e in (c.get("emails") or "").split(",") if e.strip() and not e.strip().startswith("@")]:
        pessoas[email] = {"email": email, "nome": "", "reunioes": 0}
    for reg in atas_do_cliente(cliente_id):
        for p in reg["reuniao"].get("participantes", []):
            email = (p.get("email") or "").lower()
            if not email or email == eu:
                continue
            item = pessoas.setdefault(email, {"email": email, "nome": p.get("nome") or "", "reunioes": 0})
            item["reunioes"] += 1
            item["nome"] = item["nome"] or p.get("nome") or ""
    return sorted(pessoas.values(), key=lambda x: -x["reunioes"])


DOMINIOS_PESSOAIS = {"gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "yahoo.com.br", "icloud.com",
                     "uol.com.br", "bol.com.br", "terra.com.br", "msn.com"}
_historico = {"quando": 0.0, "eventos": []}


def _reunioes_passadas(dias=180) -> list[dict]:
    """Reuniões da agenda (últimos `dias` e próximos 30) mais as atas salvas. Cache de 10 min: a tela de nova reunião
    consulta a cada e-mail digitado."""
    if time.time() - _historico["quando"] > 600:
        agora = datetime.now().astimezone()
        try:
            eventos = g.listar_eventos(agora - timedelta(days=dias), agora + timedelta(days=30))
        except Exception:
            eventos = []
        vistos = {e["id"] for e in eventos}
        for event_id in atas.existentes():
            r = (atas.carregar(event_id) or {}).get("reuniao") or {}
            if event_id not in vistos and r.get("participantes"):
                eventos.append({"id": event_id, "titulo": r.get("titulo", ""), "inicio": r.get("inicio", ""),
                                "participantes": r["participantes"]})
        _historico.update(quando=time.time(), eventos=eventos)
    return _historico["eventos"]


def clientes_das_reunioes(eventos) -> dict:
    """{event_id: cliente_id} de cada reunião: o cliente da ata, quando há ata; senão o cliente com mais
    participantes cadastrados (e-mail exato vale mais que domínio); no empate ou sem ninguém, o nome no título."""
    cadastro = [(c["id"], c["nome"], {e.strip().lower() for e in (c.get("emails") or "").split(",") if e.strip()})
                for c in db.clientes()]
    resultado = {}
    for ev in eventos:
        if not ev.get("id"):  # sem id não dá para guardar a atribuição (id-less colidiriam na mesma chave)
            continue
        fixado = db.cliente_por_uuid(ev.get("cliente_uuid")) if ev.get("cliente_uuid") else None
        if fixado and not fixado.get("excluido"):  # escolhido na tela de resumo
            resultado[ev["id"]] = fixado["id"]
            continue
        reg = atas.carregar(ev["id"])
        if reg and reg.get("cliente_id"):
            resultado[ev["id"]] = reg["cliente_id"]
            continue
        votos = {}
        for p in ev.get("participantes") or []:
            m = (p.get("email") or "").lower()
            if "@" not in m:
                continue
            for cid, _, cadastrados in cadastro:
                if m in cadastrados:
                    votos[cid] = votos.get(cid, 0) + 2
                elif "@" + m.split("@", 1)[1] in cadastrados:
                    votos[cid] = votos.get(cid, 0) + 1
        maior = max(votos.values(), default=0)
        candidatos = [cid for cid, v in votos.items() if v == maior] if maior else [c[0] for c in cadastro]
        titulo = unicodedata.normalize("NFC", ev.get("titulo") or "").casefold()
        # "Reunião Camisetaria - Linx": vale o cliente citado primeiro
        no_titulo = sorted((titulo.find(unicodedata.normalize("NFC", nome).casefold()), cid) for cid, nome, _ in cadastro
                           if cid in candidatos and unicodedata.normalize("NFC", nome).casefold() in titulo)
        if no_titulo and (len(no_titulo) == 1 or no_titulo[0][0] < no_titulo[1][0]):
            resultado[ev.get("id")] = no_titulo[0][1]
        elif maior and len(candidatos) == 1:
            resultado[ev.get("id")] = candidatos[0]
    return resultado


def consultores_conhecidos() -> list[str]:
    """Consultores já anotados nas reuniões, do mais usado para o menos (para sugerir no campo)."""
    contagem = {}
    for ev in _reunioes_passadas():
        nome = (ev.get("consultor") or "").strip()
        if nome:
            chave = unicodedata.normalize("NFC", nome).casefold()
            vezes, escrito = contagem.get(chave, (0, nome))
            contagem[chave] = (vezes + 1, escrito)
    return [escrito for _, escrito in sorted(contagem.values(), key=lambda x: (-x[0], x[1].casefold()))]


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "")) if not unicodedata.combining(c)).casefold()


def emails_dos_consultores(eventos=None, eu=None) -> dict[str, str]:
    """E-mail de cada consultor conhecido (chave: nome em NFC+casefold), para a tela de nova reunião já convidá-lo.

    Vale o e-mail guardado na reunião mais recente dele. Sem nenhum guardado, um palpite: o convidado das reuniões
    dele cujo e-mail ou nome começa com o primeiro nome do consultor (só se houver um único candidato)."""
    eu = (minha_conta() if eu is None else eu).lower()
    eventos = eventos if eventos is not None else _reunioes_passadas()
    guardado, palpites = {}, {}
    for ev in sorted(eventos, key=lambda e: e.get("inicio") or ""):
        nome = str(ev.get("consultor") or "").strip()
        if not nome:
            continue
        chave = unicodedata.normalize("NFC", nome).casefold()
        email = str(ev.get("consultor_email") or "").strip()
        if "@" in email:
            guardado[chave] = email  # o mais recente fica
        primeiro = (_sem_acento(nome).split() or [""])[0]
        participantes = ev.get("participantes")
        for p in (participantes if primeiro and isinstance(participantes, list) else []):
            e = str(p.get("email") or "").strip().lower() if isinstance(p, dict) else ""
            if "@" not in e or e == eu:
                continue
            local = _sem_acento(e.split("@")[0])
            nome_p = _sem_acento(p.get("nome") or "").split()
            if local.startswith(primeiro) or (nome_p and nome_p[0] == primeiro):
                palpites.setdefault(chave, set()).add(e)
    for chave, candidatos in palpites.items():
        if chave not in guardado and len(candidatos) == 1:
            guardado[chave] = next(iter(candidatos))
    return guardado


def sugerir_convidados(emails, cliente_id=None, eventos=None, eu=None) -> dict:
    """Quem costuma estar nas reuniões com estas pessoas (ou com o cliente), para marcar em vez de digitar.

    Com cliente: só as reuniões desse cliente (quem atende vários clientes não traz a turma dos outros).
    Sem cliente: reunião relacionada = teve alguém de `emails` ou alguém do mesmo domínio de empresa.
    Devolve {"pessoas": [{email, nome, reunioes, ultima}], "ultima": {titulo, data, emails} | None}; "ultima" é a
    turma da reunião relacionada mais recente (atalho "mesmas pessoas da última")."""
    eu = (minha_conta() if eu is None else eu).lower()
    meu_dominio = eu.split("@")[-1] if "@" in eu else None
    digitados = {e.strip().lower() for e in emails if "@" in (e or "")}
    dominios = {e.split("@")[1] for e in digitados} - DOMINIOS_PESSOAIS - {meu_dominio}
    do_cliente = set()
    if cliente_id:
        do_cliente = {p["email"] for p in contatos(cliente_id)}
    pessoas, ultima = {}, None
    eventos = eventos if eventos is not None else _reunioes_passadas()
    do_evento = clientes_das_reunioes(eventos) if cliente_id else {}
    for ev in sorted(eventos, key=lambda e: e.get("inicio") or ""):
        part = [p for p in ev.get("participantes") or [] if "@" in (p.get("email") or "")]
        mails = {p["email"].lower() for p in part if not p["email"].lower().endswith("calendar.google.com")} - {eu}
        if not mails:
            continue
        if cliente_id:
            if do_evento.get(ev.get("id")) != cliente_id:
                continue
        elif not (mails & digitados or any(m.split("@")[1] in dominios for m in mails)):
            continue
        data = (ev.get("inicio") or "")[:10]
        ultima = {"titulo": ev.get("titulo", ""), "data": data, "emails": sorted(mails)}
        for p in part:
            m = p["email"].lower()
            if m in mails:
                item = pessoas.setdefault(m, {"email": m, "nome": "", "reunioes": 0, "ultima": ""})
                item["reunioes"] += 1
                item["ultima"] = max(item["ultima"], data)
                item["nome"] = item["nome"] or p.get("nome") or ""
    for m in do_cliente - set(pessoas):   # contato cadastrado que ainda não esteve em reunião
        pessoas[m] = {"email": m, "nome": "", "reunioes": 0, "ultima": ""}
    lista = sorted(pessoas.values(), key=lambda x: (x["reunioes"], x["ultima"]), reverse=True)
    return {"pessoas": lista[:20], "ultima": ultima}


def proximas_reunioes(cliente_id, eventos: list[dict], sugerir_cliente) -> list[dict]:
    return [e for e in eventos if e.get("link") and not e["ja_terminou"] and sugerir_cliente(e) == cliente_id]


def resumo_cliente(c: dict) -> dict:
    """Tudo o que importa para saber como está um cliente."""
    tarefas = db.tarefas(cliente_id=c["id"])
    minhas = [t for t in tarefas if t["minha"]]
    dele = [t for t in tarefas if not t["minha"]]
    reunioes_cli = atas_do_cliente(c["id"])
    ultima = reunioes_cli[0]["reuniao"].get("inicio", "")[:10] if reunioes_cli else None
    dias = (date.today() - date.fromisoformat(ultima)).days if ultima else None
    motivos = []
    atrasadas = [t for t in minhas if t["atrasada"]]
    if atrasadas:
        motivos.append(("alta", f"{len(atrasadas)} tarefa{'s' if len(atrasadas) > 1 else ''} sua{'s' if len(atrasadas) > 1 else ''} atrasada{'s' if len(atrasadas) > 1 else ''}"))
    dele_atrasadas = [t for t in dele if t["atrasada"]]
    if dele_atrasadas:
        motivos.append(("media", f"{len(dele_atrasadas)} pendência{'s' if len(dele_atrasadas) > 1 else ''} do cliente vencida{'s' if len(dele_atrasadas) > 1 else ''} — cobrar"))
    elif dele:
        motivos.append(("baixa", f"{len(dele)} pendência{'s' if len(dele) > 1 else ''} do lado do cliente"))
    if dias is not None and dias >= DIAS_SEM_REUNIAO:
        motivos.append(("media", f"sem reunião há {dias} dias"))
    peso = {"alta": 3, "media": 2, "baixa": 1}
    return {**c, "minhas": minhas, "dele": dele, "atrasadas": atrasadas, "dele_atrasadas": dele_atrasadas,
            "reunioes": len(reunioes_cli), "ultima": ultima, "dias_sem_reuniao": dias, "motivos": motivos,
            "prioridade": sum(peso[n] for n, _ in motivos),
            "nivel": max((n for n, _ in motivos), key=lambda n: peso[n]) if motivos else None}


def todos_os_clientes() -> list[dict]:
    return sorted((resumo_cliente(c) for c in db.clientes()), key=lambda c: (-c["prioridade"], c["nome"].lower()))


def precisam_de_atencao(limite: int = 6) -> list[dict]:
    return [c for c in todos_os_clientes() if c["motivos"]][:limite]


def agrupar_pendencias(tarefas: list[dict], reunioes_cli: list[dict], nome_cliente: str = "") -> dict:
    """Pendências do cliente agrupadas por reunião (mais recente primeiro) e por pessoa."""
    inicio = {r["event_id"]: r["reuniao"].get("inicio", "") for r in reunioes_cli}
    por_reuniao: dict[str, dict] = {}
    for t in tarefas:
        chave = t["origem_ata"] or ""
        if chave not in por_reuniao:
            data = inicio.get(chave, "")
            rotulo = t["origem_titulo"] or "Sem reunião de origem"
            if nome_cliente and rotulo.lower().startswith(nome_cliente.lower()):  # a ficha já é do cliente
                rotulo = rotulo[len(nome_cliente):].lstrip(" –—-:") or rotulo
            por_reuniao[chave] = {"rotulo": (f"{data[8:10]}/{data[5:7]} · " if data else "") + rotulo,
                                  "data": data, "itens": []}
        t["data_reuniao"] = f"{inicio[chave][8:10]}/{inicio[chave][5:7]}" if inicio.get(chave) else ""
        por_reuniao[chave]["itens"].append(t)
    por_pessoa: dict[str, dict] = {}
    for t in tarefas:
        nome = (t["responsavel"] or "").strip() or "Sem responsável"
        por_pessoa.setdefault(nome.lower(), {"rotulo": nome, "itens": []})["itens"].append(t)
    return {"reuniao": sorted(por_reuniao.values(), key=lambda g: g["data"], reverse=True),
            "pessoa": sorted(por_pessoa.values(), key=lambda g: (-len(g["itens"]), g["rotulo"].lower()))}


# ---------------------------------------------------------------- cobrança

def texto_cobranca(cliente_id) -> dict:
    """Rascunho de e-mail cordial com o que ficou pendente do lado do cliente."""
    c = db.cliente(cliente_id)
    dele = [t for t in db.tarefas(cliente_id=cliente_id) if not t["minha"]]
    pessoas = contatos(cliente_id)
    primeiro = (pessoas[0]["nome"].split()[0] if pessoas and pessoas[0]["nome"] else "").strip()
    linhas = []
    for t in dele:
        quando = ""
        if t["origem_titulo"]:
            quando = f" (combinado em \"{t['origem_titulo']}\")"
        prazo = f" — prazo {t['prazo'][8:10]}/{t['prazo'][5:7]}" if t["prazo"] else ""
        linhas.append(f"• {t['descricao']}{prazo}{quando}")
    corpo = (
        f"Olá{' ' + primeiro if primeiro else ''}, tudo bem?\n\n"
        "Passando para acompanhar alguns pontos que ficaram combinados nas nossas últimas conversas:\n\n"
        + ("\n".join(linhas) if linhas else "• (nenhuma pendência registrada)")
        + "\n\nConsegue me dar um retorno sobre esses itens? Se precisar de algo da nossa parte, é só avisar.\n\n"
        f"Obrigado!\n{config.SEU_NOME if config.SEU_NOME != 'Eu' else ''}".rstrip()
    )
    return {"para": ", ".join(p["email"] for p in pessoas[:3]),
            "assunto": f"Acompanhamento — {c['nome'] if c else 'pendências'}", "corpo": corpo, "itens": dele}


def enviar_cobranca(para: str, assunto: str, corpo: str, remetente: str | None = None) -> list[str]:
    from html import escape

    import envio

    html = "<div style='font-family:Arial,sans-serif;font-size:11pt'>" + escape(corpo).replace("\n", "<br>") + "</div>"
    return envio.enviar_email(remetente or envio.remetente_padrao(), para, assunto, html)


# ---------------------------------------------------------------- perguntas às atas

class Resposta(BaseModel):
    resposta: str = Field(description="Resposta direta, em português, citando datas e números quando houver")
    fontes: list[str] = Field(description="ids (campo id) das reuniões usadas na resposta")
    encontrou: bool = Field(description="False se as atas não trazem a informação")


SYSTEM_PERGUNTA = """Você responde perguntas sobre as reuniões de trabalho do usuário, usando SOMENTE as atas \
fornecidas. Se a informação não estiver nas atas, diga isso claramente (encontrou=false) em vez de supor. \
Seja direto: 1 a 4 frases, com datas e valores exatos. Em fontes, liste os ids das reuniões usadas."""


def perguntar(pergunta: str, cliente_id=None) -> dict:
    nomes = {c["id"]: c["nome"] for c in db.clientes()}
    blocos = []
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        a, r = reg.get("ata"), reg.get("reuniao", {})
        if not a or (cliente_id and reg.get("cliente_id") != cliente_id):
            continue
        blocos.append(json.dumps({
            "id": event_id, "cliente": nomes.get(reg.get("cliente_id"), "sem cliente"),
            "data": r.get("inicio", "")[:16], "titulo": a.get("titulo"), "resumo": a.get("resumo"),
            "assuntos": a.get("topicos", []), "decisoes": a.get("decisoes", []), "acoes": a.get("acoes", []),
            "pontos_de_atencao": a.get("pontos_de_atencao", []),
        }, ensure_ascii=False))
    if not blocos:
        return {"resposta": "Ainda não há atas" + (" deste cliente" if cliente_id else "") + " para consultar.",
                "fontes": [], "encontrou": False}
    r = ia.gerar(SYSTEM_PERGUNTA, ["<atas>\n" + "\n".join(blocos) + "\n</atas>"],
                 f"Pergunta: {pergunta}", Resposta, effort="medium")
    fontes = []
    for event_id in r.fontes:
        reg = atas.carregar(event_id)
        if reg:
            fontes.append({"event_id": event_id, "titulo": reg["ata"].get("titulo", ""),
                           "data": reg["reuniao"].get("inicio", "")[:10]})
    return {"resposta": r.resposta, "fontes": fontes, "encontrou": r.encontrou}


# ---------------------------------------------------------------- semana e mês

def resumo_da_semana(eventos_proxima: list[dict]) -> dict:
    hoje = date.today()
    inicio = hoje - timedelta(days=7)
    nomes = {c["id"]: c["nome"] for c in db.clientes()}
    reunioes_semana = []
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        dia = reg.get("reuniao", {}).get("inicio", "")[:10]
        if reg.get("ata") and dia and inicio.isoformat() <= dia <= hoje.isoformat():
            reunioes_semana.append({"event_id": event_id, "data": dia, "titulo": reg["ata"].get("titulo"),
                                    "cliente": nomes.get(reg.get("cliente_id"), "Sem cliente"),
                                    "decisoes": reg["ata"].get("decisoes", [])})
    todas = db.tarefas(incluir_feitas=True)
    concluidas = [t for t in todas if t["status"] == "feito" and (t["concluida_em"] or "")[:10] >= inicio.isoformat()]
    fim_proxima = (hoje + timedelta(days=7)).isoformat()
    vencem = [t for t in todas if t["minha"] and t["status"] != "feito" and t["prazo"]
              and hoje.isoformat() <= t["prazo"] <= fim_proxima]
    atrasadas = [t for t in todas if t["minha"] and t["atrasada"]]
    return {"inicio": inicio, "fim": hoje, "reunioes": sorted(reunioes_semana, key=lambda x: x["data"], reverse=True),
            "concluidas": concluidas, "vencem": vencem, "atrasadas": atrasadas,
            "proximas": [e for e in eventos_proxima if not e["ja_terminou"]]}


def numeros_do_mes(eventos_mes: list[dict]) -> dict:
    hoje = date.today()
    mes = hoje.strftime("%Y-%m")
    nomes = {c["id"]: c["nome"] for c in db.clientes()}
    por_cliente = {}
    for event_id in atas.existentes():
        reg = atas.carregar(event_id) or {}
        if reg.get("ata") and reg.get("reuniao", {}).get("inicio", "")[:7] == mes:
            nome = nomes.get(reg.get("cliente_id"), "Sem cliente")
            por_cliente[nome] = por_cliente.get(nome, 0) + 1
    minutos = 0
    for e in eventos_mes:
        if e.get("link") and not e["dia_inteiro"]:
            minutos += (datetime.fromisoformat(e["fim"]) - datetime.fromisoformat(e["inicio"])).total_seconds() / 60
    todas = db.tarefas(incluir_feitas=True)
    concluidas = len([t for t in todas if t["status"] == "feito" and (t["concluida_em"] or "")[:7] == mes])
    criadas = len([t for t in todas if (t["criada_em"] or "")[:7] == mes])
    maior = max(por_cliente.values()) if por_cliente else 1
    return {"reunioes_por_cliente": sorted(por_cliente.items(), key=lambda x: -x[1]), "maior": maior,
            "atas": sum(por_cliente.values()), "horas_meet": round(minutos / 60, 1),
            "concluidas": concluidas, "criadas": criadas}


# ---------------------------------------------------------------- status do sistema

_status = {"quando": 0.0, "dados": None}


def status_sistema(estado_sinc: dict, monitor_ligado: bool) -> list[dict]:
    """Verificações rápidas (Google e Claude ficam em cache por 5 minutos)."""
    if not _status["dados"] or time.time() - _status["quando"] > 300:
        itens = []
        try:
            conta = minha_conta() or g.autenticar(interativo=False)
            itens.append({"nome": "Google", "ok": bool(conta), "detalhe": conta or "sem conta"})
        except Exception as e:
            itens.append({"nome": "Google", "ok": False, "detalhe": f"refaça o login ({str(e)[:60]})"})
        if config.IA_MODO == "api":
            itens.append({"nome": "Claude", "ok": True, "detalhe": "API"})
        else:
            caminho = shutil.which("claude")
            ok, detalhe = False, "Claude Code não encontrado"
            if caminho:
                try:
                    # sem a chave da API no ambiente, mostra o login da assinatura (o mesmo que ia.py usa)
                    ambiente = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
                    saida = subprocess.run([caminho, "auth", "status"], capture_output=True, text=True, timeout=20,
                                           env=ambiente, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    info = json.loads(saida.stdout or "{}")
                    ok = bool(info.get("loggedIn"))
                    detalhe = f"assinatura ({info.get('email', '')})" if ok else "faça login: claude → /login"
                except Exception as e:
                    detalhe = f"não consegui verificar ({str(e)[:40]})"
            itens.append({"nome": "Claude", "ok": ok, "detalhe": detalhe})
        _status.update(quando=time.time(), dados=itens)
    itens = list(_status["dados"])
    if config.SINCRONIZAR:
        ultima = estado_sinc.get("ultima")
        itens.append({"nome": "Drive", "ok": not estado_sinc.get("erro"),
                      "detalhe": estado_sinc.get("erro") or (f"sincronizado às {ultima:%H:%M}" if ultima else "aguardando")})
        agora = datetime.now().astimezone()
        for pc, quando in sorted((estado_sinc.get("outros_pcs") or {}).items()):
            horas = (agora - quando).total_seconds() / 3600
            itens.append({"nome": f"PC {pc}", "ok": horas <= 24 * 3,
                          "detalhe": "sincronizou há menos de 1 hora" if horas < 1 else
                          (f"sincronizou há {int(horas)} h" if horas < 48 else f"sem sincronizar há {int(horas // 24)} dias")})
    itens.append({"nome": "Detector do Meet", "ok": monitor_ligado, "detalhe": "ligado" if monitor_ligado else "desligado"})
    import envio

    aberto = envio.outlook_aberto()
    contas = envio.contas()
    itens.append({"nome": "Outlook", "ok": aberto,
                  "detalhe": (f"aberto ({len(contas)} contas)" if aberto
                              else "fechado — abra o Outlook para os e-mails e convites saírem")})
    return itens
