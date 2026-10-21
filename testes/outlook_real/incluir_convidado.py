"""Teste MANUAL com o Outlook de verdade (o Agenor simula o Outlook e não vê o que ele manda).

Envia uma reunião de TESTE só entre contas suas, inclui mais uma pessoa com a função do painel, lista o que o
Outlook mandou e cancela a reunião de teste no fim. Passa se ninguém recebeu "Cancelado" antes do fim.

    .venv\\Scripts\\python.exe testes\\outlook_real\\incluir_convidado.py ORGANIZADOR CONVIDADO NOVO

Exemplo: ... alanmmk@hotmail.com alanmk026@gmail.com mota@msxdesign.com.br
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import envio  # noqa: E402

if len(sys.argv) != 4:
    sys.exit(__doc__)
organizador, convidado, novo = sys.argv[1:]
titulo = f"TESTE convite Assistente - incluir pessoa {datetime.now():%H%M%S} (pode ignorar)"
quando = (datetime.now() + timedelta(days=7)).replace(hour=7, minute=0, second=0, microsecond=0)


def enviados(ol):
    itens = envio._conta(ol, organizador).DeliveryStore.GetDefaultFolder(5).Items  # Itens Enviados
    itens.Sort("[SentOn]", True)
    achados = []
    for i, it in enumerate(itens):
        if i >= 30:
            break
        try:
            if titulo in (it.Subject or ""):
                para = [it.Recipients.Item(k).Address for k in range(1, it.Recipients.Count + 1)]
                achados.append((it.MessageClass, it.Subject, para))
        except Exception:
            continue
    return achados[::-1]


entry = envio.enviar_convite(organizador, [convidado], titulo, quando, 30, "", "Reunião de teste do Assistente.")
print(f"1. convite enviado para {convidado}")
time.sleep(15)
envio.incluir_no_convite(entry, [novo])
print(f"2. {novo} incluído")
time.sleep(45)  # tempo para o Outlook mandar e arquivar em Itens Enviados
antes_do_fim = envio._no_outlook(enviados)
envio.cancelar_convite(entry)
print("3. reunião de teste cancelada (limpeza)\n")

for classe, assunto, para in antes_do_fim:
    print(f"   {classe:32} {assunto} -> {', '.join(para)}")
cancelados = [p for classe, _, p in antes_do_fim if "Canceled" in classe]
pedidos = [p for classe, _, p in antes_do_fim if classe == "IPM.Schedule.Meeting.Request"]
ok = not cancelados and any(novo.lower() in (x.lower() for x in p) for p in pedidos)
print("\nPASSOU: ninguém recebeu 'Cancelado' e o novo recebeu o convite" if ok else
      f"\nFALHOU: cancelados={cancelados} pedidos={pedidos}")
sys.exit(0 if ok else 1)
