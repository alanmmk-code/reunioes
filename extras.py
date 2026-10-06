"""Telas de clientes, cobrança, perguntas, nova reunião e resumo da semana.

Registradas no app do painel por `registrar(...)`, chamado no fim de reunioes.py.
"""

from datetime import date, datetime, timedelta

from flask import flash, redirect, request, url_for

import clientes
import config
import db
import google_services as g

TELAS = config.BASE_DIR / "telas"


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
        if not c:
            flash("Cliente não encontrado.")
            return redirect(url_for("lista_clientes"))
        reunioes_cli = clientes.atas_do_cliente(cliente_id)
        for r in reunioes_cli:
            r["breve"] = breve(r["ata"].get("resumo", ""))
        return pagina(
            _tela("cliente.html"),
            c=clientes.resumo_cliente(c), contatos=clientes.contatos(cliente_id), reunioes=reunioes_cli,
            proximas=clientes.proximas_reunioes(cliente_id, _eventos(0, 30), sugerir_cliente),
            notas=db.notas(cliente_id), resposta=None,
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
                enviados = clientes.enviar_cobranca(request.form.get("para", ""), request.form.get("assunto", ""),
                                                    request.form.get("corpo", ""))
                db.criar_nota(cliente_id, f"Cobrança enviada por e-mail para {', '.join(enviados)}:\n\n"
                                          + request.form.get("corpo", ""))
                flash(f"E-mail enviado para {', '.join(enviados)}. Ficou registrado nas anotações do cliente.")
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
        cliente_id = request.values.get("cliente_id", type=int)
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
                criado = g.agendar_followup(request.form["titulo"].strip() or "Reunião", inicio,
                                            int(request.form.get("duracao", 30)), convidados,
                                            request.form.get("pauta", "").strip())
                flash("Reunião criada no Google Agenda" + (f" e convites enviados para {', '.join(convidados)}"
                                                         if convidados else "") + ".")
                cid = request.form.get("cliente_id", type=int)
                return redirect(url_for("ficha_cliente", cliente_id=cid) if cid else url_for("inicio"))
            except Exception as e:
                flash(f"Não consegui criar a reunião: {e}")
        cliente_id = request.values.get("cliente", type=int)
        c = db.cliente(cliente_id)
        pauta, convidados = "", ""
        if c:
            pendentes = db.tarefas(cliente_id=cliente_id)
            if pendentes:
                pauta = "Pendências em aberto:\n" + "\n".join(
                    f"- {t['descricao']} ({'nosso' if t['minha'] else 'cliente'})" for t in pendentes)
            convidados = ", ".join(p["email"] for p in clientes.contatos(cliente_id))
        amanha = date.today() + timedelta(days=1)
        return pagina(_tela("nova_reuniao.html"), c=c, clientes=db.clientes(), pauta=pauta, convidados=convidados,
                      data=amanha.isoformat(), titulo=f"Reunião {c['nome']}" if c else "")

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
