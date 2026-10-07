"""Usa o Claude para entender a reunião e escrever a ata estruturada."""

from datetime import datetime

from pydantic import BaseModel, Field

import config
import ia


class Topico(BaseModel):
    titulo: str
    discussao: str = Field(description="O que foi discutido sobre o tema, com os principais argumentos")


class Acao(BaseModel):
    tarefa: str
    responsavel: str = Field(description="Nome de quem ficou responsável; 'A definir' se ninguém assumiu")
    prazo: str = Field(description="Prazo no formato AAAA-MM-DD quando dito ou dedutível; senão 'A definir'")
    do_usuario: bool = Field(
        default=True,
        description="True se a tarefa cabe ao usuário (dono desta ferramenta) ou à empresa/equipe dele; "
        "False se cabe ao cliente ou a terceiros"
    )


class ProximaReuniao(BaseModel):
    combinada: bool = Field(description="True se os participantes combinaram uma nova reunião")
    data_hora: str = Field(description="AAAA-MM-DDTHH:MM se foi combinada data/hora; senão vazio")
    duracao_min: int
    pauta: list[str]


class Ata(BaseModel):
    titulo: str
    resumo: str = Field(description="Resumo executivo em 3 a 6 frases")
    participantes: list[str]
    topicos: list[Topico]
    decisoes: list[str]
    acoes: list[Acao]
    pontos_de_atencao: list[str] = Field(description="Riscos, dúvidas em aberto e pendências")
    proxima_reuniao: ProximaReuniao


SYSTEM = """Você é um secretário executivo experiente que redige atas de reunião em português do Brasil.

Você recebe a transcrição automática de uma reunião do Google Meet. Transcrições automáticas têm erros \
de reconhecimento, frases cortadas e conversa paralela: interprete o sentido, corrija nomes próprios \
usando a lista de convidados quando for evidente, e ignore cumprimentos e assuntos sem relação com a pauta.

Regras:
- Registre apenas o que foi efetivamente dito. Não invente decisões, responsáveis ou prazos.
- Uma decisão é algo que o grupo fechou; uma ação é uma tarefa que alguém precisa executar depois.
- Em cada ação, marque do_usuario: o usuário quer controlar o que ele e a equipe dele precisam entregar, \
separado do que ficou com o cliente.
- Converta prazos relativos ("sexta que vem", "fim do mês") em datas, usando a data da reunião como referência.
- Escreva de forma objetiva e profissional, na terceira pessoa."""


def gerar_ata(transcricao: str, reuniao: dict) -> Ata:
    convidados = ", ".join(
        f"{p['nome'] or p['email']} <{p['email']}>" for p in reuniao.get("participantes", [])
    ) or "não informado"
    cliente = reuniao.get("cliente_nome")
    contexto = (
        f"Usuário (dono desta ferramenta): {config.SEU_NOME}\n"
        f"Cliente desta reunião: {cliente or 'não informado'}\n"
        f"Título do evento: {reuniao.get('titulo', '')}\n"
        f"Data/hora de início: {reuniao.get('inicio', '')}\n"
        f"Convidados na agenda: {convidados}\n"
        f"Pessoas na chamada (informadas pelo usuário): {', '.join(reuniao.get('na_chamada', [])) or 'não informado'}\n"
        f"Descrição do evento: {reuniao.get('descricao', '') or '(vazia)'}\n"
        f"Data de hoje: {datetime.now().strftime('%Y-%m-%d')}"
    )

    return ia.gerar(
        SYSTEM,
        [f"<contexto>\n{contexto}\n</contexto>", f"<transcricao>\n{transcricao}\n</transcricao>"],
        "Escreva a ata desta reunião.",
        Ata,
        effort="high",
    )
