"""Janelinha sempre visível com as sugestões do assistente durante a reunião.

Fica por cima das outras janelas e é escondida de compartilhamento de tela
(quem assiste à sua tela no Meet não a vê). Fecha sozinha quando a gravação termina.
"""

import ctypes
import json
import threading
import tkinter as tk
import urllib.request

import config

URL = f"http://127.0.0.1:{config.PORTA}"
WDA_EXCLUDEFROMCAPTURE = 0x11

CORES = {"fundo": "#1d2128", "texto": "#e8eaed", "mut": "#9aa0a6", "azul": "#8ab4f8", "verm": "#f28b82", "card": "#262b33"}


def api(caminho: str, metodo: str = "GET") -> dict:
    req = urllib.request.Request(URL + caminho, method=metodo, data=b"" if metodo == "POST" else None)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


class Janela:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Assistente da reunião")
        self.root.configure(bg=CORES["fundo"])
        self.root.attributes("-topmost", True)
        largura, altura = 400, 800
        x = self.root.winfo_screenwidth() - largura - 16
        y = self.root.winfo_screenheight() - altura - 72
        self.root.geometry(f"{largura}x{altura}+{x}+{y}")
        self.root.minsize(300, 300)

        f = ("Segoe UI", 10)
        self.status = tk.Label(self.root, text="Conectando…", bg=CORES["fundo"], fg=CORES["verm"], font=("Segoe UI", 10, "bold"), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(10, 4))

        # Pergunta do cliente (aparece até ser respondida)
        self.quadro_cliente = tk.Frame(self.root, bg=CORES["card"], padx=10, pady=8)
        tk.Label(self.quadro_cliente, text="Qual cliente é esta reunião?", bg=CORES["card"], fg=CORES["texto"],
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        # Lista sempre visível (um "combobox" abriria a lista atrás desta janela, que fica sempre por cima)
        self.busca = tk.Entry(self.quadro_cliente, font=("Segoe UI", 10), bg=CORES["fundo"], fg=CORES["texto"],
                              insertbackground=CORES["texto"], relief="flat")
        self.busca.pack(fill="x", pady=(6, 4), ipady=4)
        self.busca.bind("<KeyRelease>", lambda _: self._filtrar())
        self.busca.bind("<Return>", lambda _: self.confirmar_cliente())
        self.lista = tk.Listbox(self.quadro_cliente, height=5, font=("Segoe UI", 10), activestyle="none",
                                bg=CORES["fundo"], fg=CORES["texto"], selectbackground=CORES["azul"],
                                selectforeground="#14171c", relief="flat", highlightthickness=0, exportselection=False)
        self.lista.pack(fill="x")
        self.lista.bind("<Double-Button-1>", lambda _: self.confirmar_cliente())
        self.lista.bind("<<ListboxSelect>>", lambda _: self._mostrar_pessoas())
        self.dica = tk.Label(self.quadro_cliente, text="Clique no cliente e em Confirmar, ou digite para buscar ou criar um novo.",
                             bg=CORES["card"], fg=CORES["mut"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=340)
        self.dica.pack(fill="x", pady=(4, 0))

        # Quem está na chamada: pessoas já conhecidas do cliente (clique para marcar) ou um nome novo
        tk.Label(self.quadro_cliente, text="Quem está na chamada com você?", bg=CORES["card"], fg=CORES["texto"],
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(10, 0))
        self.pessoas_lista = tk.Listbox(self.quadro_cliente, height=4, selectmode="multiple", font=("Segoe UI", 10),
                                        activestyle="none", bg=CORES["fundo"], fg=CORES["texto"],
                                        selectbackground=CORES["azul"], selectforeground="#14171c", relief="flat",
                                        highlightthickness=0, exportselection=False)
        self.pessoas_lista.pack(fill="x", pady=(4, 4))
        self.pessoas_lista.bind("<<ListboxSelect>>", lambda _: self._guardar_marcadas())
        self.nova_pessoa = tk.Entry(self.quadro_cliente, font=("Segoe UI", 10), bg=CORES["fundo"], fg=CORES["texto"],
                                    insertbackground=CORES["texto"], relief="flat")
        self.nova_pessoa.pack(fill="x", ipady=4)
        self.nova_pessoa.bind("<Return>", lambda _: self.adicionar_pessoa())
        tk.Label(self.quadro_cliente, text="Clique nos nomes para marcar, ou digite um nome novo e aperte Enter.",
                 bg=CORES["card"], fg=CORES["mut"], font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(2, 0))
        self._pessoas_por_cliente = {}
        self._marcadas = []  # nomes marcados (em ordem)
        self._digitadas = []  # nomes novos digitados nesta reunião
        self._nomes = []
        self._sugerido = None
        linha = tk.Frame(self.quadro_cliente, bg=CORES["card"])
        linha.pack(fill="x", pady=(6, 0))
        tk.Button(linha, text="Confirmar", command=self.confirmar_cliente, bg=CORES["azul"], fg="#14171c",
                  relief="flat", cursor="hand2").pack(side="left")
        tk.Button(linha, text="Sem cliente", command=lambda: self.enviar_cliente({"cliente_id": None}),
                  bg=CORES["card"], fg=CORES["mut"], relief="flat", cursor="hand2").pack(side="left", padx=8)
        self.rotulo_cliente = tk.Label(self.root, bg=CORES["fundo"], fg=CORES["mut"], font=("Segoe UI", 9),
                                       anchor="w", cursor="hand2")
        self.rotulo_cliente.bind("<Button-1>", lambda _: self.mostrar_pergunta(True))
        self._clientes = {}
        self._perguntando = None
        self._trocando = False

        self.botao = tk.Button(self.root, text="O que eu respondo?", command=self.pedir, font=("Segoe UI", 11, "bold"),
                               bg=CORES["azul"], fg="#14171c", activebackground=CORES["azul"], relief="flat", cursor="hand2")
        self.botao.pack(fill="x", padx=12, pady=4, ipady=4)

        self.sugestao = tk.Text(self.root, wrap="word", bg=CORES["card"], fg=CORES["texto"], font=("Segoe UI", 11),
                                relief="flat", padx=10, pady=8, height=12)
        self.sugestao.pack(fill="both", expand=True, padx=12, pady=6)
        self.sugestao.tag_configure("titulo", foreground=CORES["azul"], font=("Segoe UI", 10, "bold"))
        self.sugestao.tag_configure("mut", foreground=CORES["mut"], font=f)

        tk.Label(self.root, text="Conversa", bg=CORES["fundo"], fg=CORES["mut"], font=f, anchor="w").pack(fill="x", padx=12)
        self.conversa = tk.Text(self.root, wrap="word", bg=CORES["fundo"], fg=CORES["mut"], font=("Segoe UI", 9),
                                relief="flat", padx=4, height=7)
        self.conversa.pack(fill="x", padx=12, pady=(0, 10))

        self._ultimo = None
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.root.after(300, self._esconder_de_captura)
        self.root.after(500, self.atualizar)

    def fechar(self):
        """Fechada por você: avisa o painel para não reabrir sozinha nesta reunião."""
        self._tentar(lambda: api("/api/janela/fechada", "POST"))
        self.root.destroy()

    def _esconder_de_captura(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass

    # ---------------------------------------------------------- cliente

    def mostrar_pergunta(self, sim: bool):
        if sim == self._perguntando:
            return
        self._perguntando = sim
        self._trocando = sim
        if sim:
            self.rotulo_cliente.pack_forget()
            self.quadro_cliente.pack(fill="x", padx=12, pady=6, after=self.status)
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.busca.delete(0, "end")
            self._filtrar(selecionar=self._sugerido)
            self.busca.focus_set()
        else:
            self.quadro_cliente.pack_forget()
            self.rotulo_cliente.pack(fill="x", padx=12, after=self.status)

    def _filtrar(self, selecionar: str | None = None):
        """Mostra os clientes que contêm o texto digitado."""
        texto = self.busca.get().strip().lower()
        visiveis = [n for n in self._nomes if texto in n.lower()]
        self.lista.delete(0, "end")
        for nome in visiveis:
            self.lista.insert("end", nome)
        alvo = selecionar if selecionar in visiveis else (visiveis[0] if texto and visiveis else None)
        if alvo:
            i = visiveis.index(alvo)
            self.lista.selection_set(i)
            self.lista.see(i)
        if texto and texto not in [n.lower() for n in visiveis]:
            self.dica.configure(text=f"Enter para criar o cliente novo \"{self.busca.get().strip()}\"" if not visiveis
                                else "Clique no cliente, ou aperte Enter para usar o primeiro da lista.")
        else:
            self.dica.configure(text="Clique no cliente e em Confirmar, ou digite para buscar ou criar um novo.")
        self._mostrar_pessoas()

    # ---------------------------------------------------------- pessoas na chamada

    def _cliente_escolhido(self) -> str | None:
        selecionado = self.lista.curselection()
        return self.lista.get(selecionado[0]) if selecionado else None

    def _mostrar_pessoas(self):
        """Pessoas do cliente marcado na lista + nomes digitados; mantém as marcações."""
        nome = self._cliente_escolhido()
        cid = self._clientes.get(nome.lower()) if nome else None
        conhecidas = self._pessoas_por_cliente.get(str(cid), []) if cid else []
        nomes = list(dict.fromkeys(conhecidas + self._digitadas + self._marcadas))
        if list(self.pessoas_lista.get(0, "end")) != nomes:
            self.pessoas_lista.delete(0, "end")
            for n in nomes:
                self.pessoas_lista.insert("end", n)
        self.pessoas_lista.selection_clear(0, "end")
        for i, n in enumerate(nomes):
            if n in self._marcadas:
                self.pessoas_lista.selection_set(i)

    def _guardar_marcadas(self):
        self._marcadas = [self.pessoas_lista.get(i) for i in self.pessoas_lista.curselection()]

    def adicionar_pessoa(self):
        nome = " ".join(self.nova_pessoa.get().split())
        if nome:
            existente = next((n for n in self.pessoas_lista.get(0, "end") if n.lower() == nome.lower()), None)
            nome = existente or nome
            if not existente:
                self._digitadas.append(nome)
            if nome not in self._marcadas:
                self._marcadas.append(nome)
        self.nova_pessoa.delete(0, "end")
        self._mostrar_pessoas()

    def confirmar_cliente(self):
        selecionado = self.lista.curselection()
        if selecionado:
            nome = self.lista.get(selecionado[0])
        else:
            nome = self.busca.get().strip()
        if not nome:
            self.dica.configure(text="Escolha um cliente na lista (ou clique em Sem cliente).")
            return
        cid = self._clientes.get(nome.lower())
        self.enviar_cliente({"cliente_id": cid} if cid else {"novo": nome})

    def enviar_cliente(self, dados: dict):
        self.adicionar_pessoa()  # nome digitado sem apertar Enter também vale
        dados = {**dados, "pessoas": list(self._marcadas)}
        def enviar():
            req = urllib.request.Request(URL + "/api/cliente", method="POST", data=json.dumps(dados).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        self._tentar(enviar)
        self._trocando = False
        self.mostrar_pergunta(False)

    def _atualizar_cliente(self, e):
        nomes = [c["nome"] for c in e.get("clientes", [])]
        self._clientes = {c["nome"].lower(): c["id"] for c in e.get("clientes", [])}
        self._sugerido = next((c["nome"] for c in e.get("clientes", []) if c["id"] == e.get("cliente_sugerido")), None)
        pessoas = e.get("pessoas_por_cliente", {})
        if pessoas != self._pessoas_por_cliente:
            self._pessoas_por_cliente = pessoas
            self._mostrar_pessoas()
        if nomes != self._nomes:
            self._nomes = nomes
            atual = self.lista.get(self.lista.curselection()[0]) if self.lista.curselection() else self._sugerido
            self._filtrar(selecionar=atual)
        if not e.get("cliente_definido") or self._trocando:
            self.mostrar_pergunta(True)
        else:
            self.mostrar_pergunta(False)
            self._marcadas = list(e.get("na_chamada", []))  # ao trocar, a lista volta com as marcações
            com = f" · com {', '.join(self._marcadas)}" if self._marcadas else ""
            self.rotulo_cliente.configure(text=f"Cliente: {e.get('cliente') or 'sem cliente'}{com}  (clique para trocar)",
                                          wraplength=360, justify="left")

    def pedir(self):
        self.botao.configure(text="Pensando…", state="disabled")
        threading.Thread(target=lambda: self._tentar(lambda: api("/api/sugerir", "POST")), daemon=True).start()

    @staticmethod
    def _tentar(func):
        try:
            return func()
        except Exception:
            return None

    def atualizar(self):
        e = self._tentar(lambda: api("/api/aovivo"))
        if e is None:
            self.status.configure(text="Sem conexão com o painel…")
        elif not e["gravando"]:
            self.status.configure(text="Reunião encerrada", fg=CORES["mut"])
            self.root.after(4000, self.root.destroy)
            return
        else:
            self.status.configure(text=f"● Gravando · {e['duracao']} · {e['titulo'][:40]}")
            self._atualizar_cliente(e)
            if not e.get("assistente"):
                self.botao.pack_forget()
            pensando = e.get("pensando")
            self.botao.configure(text="Pensando…" if pensando else "O que eu respondo?",
                                 state="disabled" if pensando else "normal")
            self._mostrar_sugestao(e)
            self._mostrar_conversa(e.get("falas", []))
        self.root.after(1500, self.atualizar)

    def _mostrar_sugestao(self, e):
        chave = json.dumps([e.get("sugestao"), e.get("erro")], ensure_ascii=False)
        if chave == self._ultimo:
            return
        self._ultimo = chave
        t = self.sugestao
        t.configure(state="normal")
        t.delete("1.0", "end")
        s = e.get("sugestao")
        if e.get("erro"):
            t.insert("end", e["erro"] + "\n\n", "mut")
        if not s:
            t.insert("end", "Ouvindo a reunião… As sugestões aparecem aqui quando alguém se dirigir a você, "
                            "ou clique no botão acima.", "mut")
        else:
            t.insert("end", f"{s.get('hora', '')}  {s.get('pergunta', '')}\n\n", "titulo")
            for i, r in enumerate(s.get("respostas", []), 1):
                t.insert("end", f"{i}. {r}\n\n")
            if s.get("lembrar"):
                t.insert("end", "Lembrar:\n", "titulo")
                for item in s["lembrar"]:
                    t.insert("end", f"• {item}\n", "mut")
        t.configure(state="disabled")

    def _mostrar_conversa(self, falas):
        c = self.conversa
        c.configure(state="normal")
        c.delete("1.0", "end")
        c.insert("end", "\n".join(f"{f['quem'].split()[0]}: {f['texto']}" for f in falas[-5:]))
        c.see("end")
        c.configure(state="disabled")


if __name__ == "__main__":
    Janela().root.mainloop()
