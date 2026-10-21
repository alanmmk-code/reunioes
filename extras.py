"""Telas de clientes, cobrança, perguntas, nova reunião e resumo da semana.

Registradas no app do painel por `registrar(...)`, chamado no fim de reunioes.py.
"""

import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from flask import flash, redirect, request, url_for

import clientes
import config
import db
import envio
import google_services as g
import service_desk

TELAS = config.BASE_DIR / "telas"


_EMAIL = re.compile(r"[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+")
DURACOES = (15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480)  # opções de duração ao marcar/remarcar


def _tela(nome: str) -> str:
    return (TELAS / nome).read_text(encoding="utf-8")


def _eventos(dias_atras: int, dias_frente: int) -> list[dict]:
    agora = datetime.now().astimezone()
    try:
        return g.listar_eventos(agora - timedelta(days=dias_atras), agora + timedelta(days=dias_frente))
    except Exception:
        return []


def registrar(app, pagina, seletor_cliente, sugerir_cliente, cliente_do_formulario, breve):
    # ------------------------------------------------------------ clientes

    @app.route("/clientes")
    def lista_clientes():
        return pagina(_tela("clientes.html"), clientes=clientes.todos_os_clientes())

    @app.route("/cliente/<int:cliente_id>")
    def ficha_cliente(cliente_id):
        c = db.cliente(cliente_id)
        if not c or c["excluido"]:
            flash("Cliente não encontrado.")
            return redirect(url_for("lista_clientes"))
        reunioes_cli = clientes.atas_do_cliente(cliente_id)
        for r in reunioes_cli:
            r["breve"] = breve(r["ata"].get("resumo", ""))
        resumo = clientes.resumo_cliente(c)
        return pagina(
            _tela("cliente.html"),
            c=resumo, grupos=clientes.agrupar_pendencias(resumo["dele"], reunioes_cli, c["nome"]), contatos=clientes.contatos(cliente_id), reunioes=reunioes_cli,
            proximas=clientes.proximas_reunioes(cliente_id, _eventos(0, 30), sugerir_cliente),
            notas=db.notas(cliente_id), resposta=None,
            sd=service_desk.casos_do_cliente(c["nome"]),
        )

    @app.post("/cliente/<int:cliente_id>/nota")
    def nota_nova(cliente_id):
        if not db.criar_nota(cliente_id, request.form.get("texto", "")):
            flash("Escreva a anotação antes de salvar.")
        return redirect(url_for("ficha_cliente", cliente_id=cliente_id) + "#notas")

    @app.post("/nota/<int:nota_id>/excluir")
    def nota_excluir(nota_id):
        db.excluir_nota(nota_id)
        return redirect((request.referrer or url_for("lista_clientes")).split("#")[0] + "#notas")

    # ------------------------------------------------------------ cobrança

    @app.route("/cliente/<int:cliente_id>/cobrar", methods=["GET", "POST"])
    def cobrar(cliente_id):
        c = db.cliente(cliente_id)
        if not c:
            return redirect(url_for("lista_clientes"))
        if request.method == "POST":
            try:
                remetente = request.form.get("remetente") or envio.remetente_padrao()
                enviados = clientes.enviar_cobranca(request.form.get("para", ""), request.form.get("assunto", ""),
                                                    request.form.get("corpo", ""), remetente)
                db.criar_nota(cliente_id, f"Cobrança enviada de {remetente} para {', '.join(enviados)}:\n\n"
                                          + request.form.get("corpo", ""))
                flash(f"E-mail enviado de {remetente} para {', '.join(enviados)}. Ficou registrado nas anotações do cliente.")
                return redirect(url_for("ficha_cliente", cliente_id=cliente_id))
            except Exception as e:
                flash(f"Não consegui enviar: {e}")
                rascunho = {"para": request.form.get("para", ""), "assunto": request.form.get("assunto", ""),
                            "corpo": request.form.get("corpo", ""), "itens": []}
        else:
            rascunho = clientes.texto_cobranca(cliente_id)
        return pagina(_tela("cobrar.html"), c=c, r=rascunho)

    # ------------------------------------------------------------ perguntas às atas

    @app.route("/perguntar", methods=["GET", "POST"])
    def perguntar():
        pergunta = (request.values.get("pergunta") or "").strip()
        c_valido = db.cliente(request.values.get("cliente_id"))
        cliente_id = c_valido["id"] if c_valido else None
        resposta = None
        if request.method == "POST" and pergunta:
            try:
                resposta = clientes.perguntar(pergunta, cliente_id)
            except Exception as e:
                resposta = {"resposta": f"Não consegui consultar o Claude: {e}", "fontes": [], "encontrou": False}
        return pagina(_tela("perguntar.html"), pergunta=pergunta, resposta=resposta,
                      cliente=db.cliente(cliente_id), clientes=db.clientes())

    # ------------------------------------------------------------ nova reunião

    @app.route("/reuniao/nova", methods=["GET", "POST"])
    def nova_reuniao():
        if request.method == "POST":
            try:
                inicio = datetime.fromisoformat(f"{request.form['data']}T{request.form['hora']}")
                convidados = [e.strip() for e in request.form.get("convidados", "").replace(";", ",").split(",") if "@" in e]
                titulo, duracao = request.form["titulo"].strip() or "Reunião", max(5, min(int(request.form.get("duracao", 30)), 24 * 60))
                pauta = request.form.get("pauta", "").strip()
                # evento (com Meet) no Google Agenda sem e-mail do Google; o convite sai pelo Outlook
                consultor = " ".join(request.form.get("consultor", "").split())[:80]
                criado = g.agendar_followup(titulo, inicio, duracao, convidados, pauta, enviar_convites=False,
                                            consultor=consultor)
                remetente = request.form.get("remetente") or envio.remetente_padrao()
                if convidados:
                    try:
                        entry = envio.enviar_convite(remetente, convidados, titulo, inicio, duracao,
                                                     criado.get("link_meet"), pauta)
                        envio.registrar_convite(criado["id"], remetente, entry, titulo, inicio)
                    except Exception as erro:
                        flash(f"A reunião foi criada no Google Agenda, mas o convite pelo Outlook falhou: {erro}")
                        raise
                flash("Reunião criada no Google Agenda" + (f" e convite enviado de {remetente} para {', '.join(convidados)}"
                                                         if convidados else "")
                      + ". Para mudar data ou horário, use Remarcar na agenda do Painel.")
                cid = request.form.get("cliente_id", type=int)
                return redirect(url_for("ficha_cliente", cliente_id=cid) if cid else url_for("inicio"))
            except Exception as e:
                flash(f"Não consegui criar a reunião: {e}")
        c = db.cliente(request.values.get("cliente"))
        cliente_id = c["id"] if c else None
        pauta, convidados = "", ""
        if c:
            pendentes = db.tarefas(cliente_id=cliente_id)
            if pendentes:
                pauta = "Pendências em aberto:\n" + "\n".join(
                    f"- {t['descricao']} ({'nosso' if t['minha'] else 'cliente'})" for t in pendentes)
            # a turma da última reunião com o cliente; as outras pessoas do histórico ficam para marcar na tela
            ultima = clientes.sugerir_convidados([], cliente_id)["ultima"]
            convidados = ", ".join(ultima["emails"] if ultima else (p["email"] for p in clientes.contatos(cliente_id)))
        amanha = date.today() + timedelta(days=1)
        return pagina(_tela("nova_reuniao.html"), c=c, clientes=db.clientes(), pauta=pauta, convidados=convidados,
                      data=amanha.isoformat(), titulo=f"Reunião {c['nome']}" if c else "", duracoes=DURACOES,
                      consultores=clientes.consultores_conhecidos())

    @app.get("/reuniao/sugestoes")
    def sugestoes_convidados():
        """Pessoas do histórico de reuniões ligadas aos e-mails digitados (ou ao cliente), para a tela marcar."""
        emails = [e.strip() for e in request.args.get("emails", "").replace(";", ",").split(",") if "@" in e]
        cliente_id = request.args.get("cliente", type=int)
        if not cliente_id and emails:   # sem cliente escolhido: o do primeiro e-mail conhecido
            cliente_id = next((cid for cid in map(db.cliente_por_email, emails) if cid), None)
        return clientes.sugerir_convidados(emails, cliente_id)

    # ------------------------------------------------------------ remarcar

    @app.route("/reuniao/<event_id>/remarcar", methods=["GET", "POST"])
    def remarcar_reuniao(event_id):
        e = g.obter_evento(event_id)
        voltar = request.values.get("voltar") or request.referrer or url_for("inicio")
        partes = urlsplit(voltar)
        if partes.hostname in ("localhost", "127.0.0.1"):  # o referrer vem com endereço completo
            voltar = partes.path + (f"?{partes.query}" if partes.query else "")
        if not voltar.startswith("/") or voltar.startswith("//") or "\\" in voltar or not voltar.isprintable():
            voltar = url_for("inicio")
        if not e:
            flash("Reunião não encontrada no Google Agenda.")
            return redirect(voltar)
        if not e["organizador"]:
            flash("Só quem organizou a reunião pode mudar a data e o horário. Peça ao organizador.")
            return redirect(voltar)
        if e["dia_inteiro"]:
            flash("Este é um evento de dia inteiro; altere pelo Google Agenda.")
            return redirect(voltar)
        ini = datetime.fromisoformat(e["inicio"]).astimezone()
        duracao_atual = int((datetime.fromisoformat(e["fim"]) - datetime.fromisoformat(e["inicio"])).total_seconds() // 60)
        if request.method == "POST":
            try:
                novo_inicio = datetime.fromisoformat(f"{request.form['data']}T{request.form['hora']}")
                duracao = max(5, min(int(request.form.get("duracao", duracao_atual)), 24 * 60))
                titulo = request.form.get("titulo", "").strip() or None
                novo_titulo = titulo if titulo != e["titulo"] else None
                convite = envio.convite_do_evento(event_id)
                if convite:  # convite saiu pelo Outlook: atualização pelo Outlook (antes: se falhar, nada muda), Google sem e-mail
                    envio.completar_convite(event_id, e["titulo"], datetime.fromisoformat(e["inicio"]))
                    envio.atualizar_convite(convite["entry_id"], novo_inicio, duracao, novo_titulo)
                    g.remarcar_evento(event_id, novo_inicio, duracao, novo_titulo, avisar=False)
                else:
                    g.remarcar_evento(event_id, novo_inicio, duracao, novo_titulo)
                flash(f"Reunião remarcada para {novo_inicio:%d/%m às %H:%M}"
                      + (" e convidados avisados." if e["participantes"] else "."))
                return redirect(voltar)
            except (ValueError, KeyError):
                flash("Data ou hora inválida.")
            except Exception as erro:
                flash(f"Não consegui remarcar: {erro}")
        duracoes = sorted({*DURACOES, duracao_atual})
        return pagina(_tela("remarcar.html"), e=e, voltar=voltar, data=ini.date().isoformat(), hora=ini.strftime("%H:%M"),
                      duracao=duracao_atual, duracoes=duracoes, atual=f"{ini:%d/%m/%Y às %H:%M} ({duracao_atual} min)")

    @app.post("/reuniao/<event_id>/cancelar")
    def cancelar_reuniao(event_id):
        """Cancela a reunião para todos: some da agenda e os convidados recebem o cancelamento (pelo Outlook, se o
        convite saiu por lá; senão, pelo Google)."""
        voltar = request.values.get("voltar") or url_for("inicio")
        if not voltar.startswith("/") or voltar.startswith("//") or "\\" in voltar or not voltar.isprintable():
            voltar = url_for("inicio")
        e = g.obter_evento(event_id)
        if not e:
            flash("Reunião não encontrada no Google Agenda.")
            return redirect(voltar)
        if not e["organizador"]:
            flash("Só quem organizou a reunião pode cancelá-la. Recuse o convite pelo Google Agenda.")
            return redirect(voltar)
        try:
            convite = envio.convite_do_evento(event_id)
            if convite:  # convite saiu pelo Outlook: o cancelamento vai por ele; o Google não manda outro e-mail
                envio.completar_convite(event_id, e["titulo"], datetime.fromisoformat(e["inicio"]))
                envio.cancelar_convite(convite["entry_id"])
                g.cancelar_evento(event_id, avisar=False)
            else:
                g.cancelar_evento(event_id)
        except Exception as erro:
            flash(f"Não consegui cancelar: {erro}")
            return redirect(voltar)
        flash(f"Reunião “{e['titulo']}” cancelada" + (" e convidados avisados." if e["participantes"] else "."))
        return redirect(voltar)

    @app.route("/reuniao/<event_id>/convidar", methods=["GET", "POST"])
    def convidar_reuniao(event_id):
        """Inclui pessoas numa reunião já marcada: as novas recebem o convite (pelo Outlook, quem já estava recebe
        uma atualização)."""
        voltar = request.values.get("voltar") or url_for("inicio")
        if not voltar.startswith("/") or voltar.startswith("//") or "\\" in voltar or not voltar.isprintable():
            voltar = url_for("inicio")
        e = g.obter_evento(event_id)
        if not e:
            flash("Reunião não encontrada no Google Agenda.")
            return redirect(voltar)
        if not e["organizador"]:
            flash("Só quem organizou a reunião pode convidar mais pessoas. Peça ao organizador.")
            return redirect(voltar)
        texto = request.form.get("convidados", "")
        if request.method == "POST":
            bruto = [x.strip() for x in texto.replace(";", ",").replace("\n", ",").split(",") if x.strip()]
            emails = list({x.lower(): x for x in reversed(bruto)}.values())[::-1]  # sem repetir, nem com outra caixa
            invalidos = [x for x in emails if not _EMAIL.fullmatch(x)]
            ja = {p.lower() for p in e["participantes"]}
            novos = [x for x in emails if x not in invalidos and x.lower() not in ja]
            if invalidos:
                flash(f"E-mail inválido: {', '.join(invalidos)}")
            elif not novos:
                flash("Essas pessoas já estão convidadas." if emails else "Informe pelo menos um e-mail.")
            else:
                try:
                    convite = envio.convite_do_evento(event_id)
                    if convite:  # convite saiu pelo Outlook: os novos recebem por ele; o Google não manda outro e-mail
                        envio.completar_convite(event_id, e["titulo"], datetime.fromisoformat(e["inicio"]))
                        envio.incluir_no_convite(convite["entry_id"], novos)
                        g.incluir_convidados(event_id, novos, avisar=False)
                    else:
                        g.incluir_convidados(event_id, novos)
                except Exception as erro:
                    flash(f"Não consegui convidar: {erro}")
                else:
                    flash(f"Convite enviado para {', '.join(novos)}."
                          + (" Quem já estava convidado recebeu uma atualização do convite (não um cancelamento)."
                             if convite else ""))
                    return redirect(voltar)
        return pagina(_tela("convidar.html"), e=e, voltar=voltar, texto=texto)

    # ------------------------------------------------------------ semana e mês

    @app.route("/semana")
    def semana():
        hoje = date.today()
        inicio_mes = datetime.combine(hoje.replace(day=1), datetime.min.time()).astimezone()
        try:
            eventos_mes = g.listar_eventos(inicio_mes, datetime.now().astimezone())
        except Exception:
            eventos_mes = []
        return pagina(_tela("semana.html"), s=clientes.resumo_da_semana(_eventos(0, 7)),
                      n=clientes.numeros_do_mes(eventos_mes), mes=hoje.strftime("%m/%Y"))
