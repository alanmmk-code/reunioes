// Service worker: repassa ao painel (localhost) o estado da chamada e o áudio de cada aba do Meet,
// e controla a gravação da tela (clique no ícone da extensão).
const PAINEL = "http://127.0.0.1:5055";

function postar(caminho, corpo, tipo) {
  return fetch(PAINEL + caminho, { method: "POST", headers: { "Content-Type": tipo }, body: corpo })
    .catch(() => {}); // painel fechado: tenta de novo no próximo segundo
}

function estado(aba, em_chamada, titulo, fim, extra) {
  return postar("/api/extensao/estado", JSON.stringify({ ...(extra || {}), aba, em_chamada, titulo, fim: !!fim }),
                "application/json");
}

function erro(onde, e) {
  postar("/api/extensao/erro", JSON.stringify({ onde, erro: String((e && e.message) || e) }), "application/json");
}

function deBase64(texto) {
  const bin = atob(texto);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

// ---------------------------------------------------------------- gravação da tela

const emChamada = new Map(); // aba -> está numa chamada agora

// Guardado na sessão: o service worker pode reiniciar no meio da reunião
async function abasGravando() {
  const { gravandoTela = [] } = await chrome.storage.session.get("gravandoTela");
  return new Set(gravandoTela);
}
async function marcarGravando(aba, sim) {
  const abas = await abasGravando();
  sim ? abas.add(aba) : abas.delete(aba);
  await chrome.storage.session.set({ gravandoTela: [...abas] });
}

async function atualizarIcone(aba) {
  const gravando = (await abasGravando()).has(aba);
  let texto = "", titulo = "Assistente de Reuniões";
  if (gravando) {
    texto = "REC"; titulo = "Gravando a tela desta reunião — clique para parar";
    chrome.action.setBadgeBackgroundColor({ tabId: aba, color: "#dc2626" });
  } else if (emChamada.get(aba)) {
    texto = "TELA"; titulo = "Clique para gravar a tela desta reunião";
    chrome.action.setBadgeBackgroundColor({ tabId: aba, color: "#d97706" });
  }
  chrome.action.setBadgeText({ tabId: aba, text: texto }).catch(() => {});
  chrome.action.setTitle({ tabId: aba, title: titulo }).catch(() => {});
}

async function iniciarTela(aba) {
  // Só funciona a partir do clique no ícone (exigência do Chrome para capturar uma aba)
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: aba });
  if (!(await chrome.offscreen.hasDocument())) {
    await chrome.offscreen.createDocument({ url: "tela.html", reasons: ["USER_MEDIA"],
                                           justification: "Gravar o vídeo da aba do Meet" });
  }
  await marcarGravando(aba, true);
  chrome.runtime.sendMessage({ alvo: "tela", acao: "iniciar", aba, streamId }).catch(() => {});
  atualizarIcone(aba);
}

async function pararTela(aba) {
  if (!(await abasGravando()).has(aba)) return;
  await marcarGravando(aba, false);
  chrome.runtime.sendMessage({ alvo: "tela", acao: "parar", aba }).catch(() => {});
  atualizarIcone(aba);
}

chrome.action.onClicked.addListener(async (tab) => {
  // tab.url só vem com a permissão activeTab; a aba também conta como Meet se já mandou estado
  const ehMeet = (tab.url || "").startsWith("https://meet.google.com/") || emChamada.has(tab.id);
  if (!ehMeet) return;
  try {
    if ((await abasGravando()).has(tab.id)) await pararTela(tab.id);
    else await iniciarTela(tab.id);
  } catch (e) {
    await marcarGravando(tab.id, false);
    atualizarIcone(tab.id);
    erro("clique no ícone", e);
  }
});

chrome.runtime.onMessage.addListener((m) => {
  if (m.alvo !== "fundo") return;
  if (m.acao === "tela_falhou") erro("captura da tela", m.erro);
  if (m.acao === "tela_parou" || m.acao === "tela_falhou") marcarGravando(m.aba, false).then(() => atualizarIcone(m.aba));
});

// ---------------------------------------------------------------- abas do Meet

chrome.runtime.onConnect.addListener((porta) => {
  if (porta.name !== "meet" || !porta.sender || !porta.sender.tab) return;
  const aba = porta.sender.tab.id;
  let titulo = "Meet";
  porta.onMessage.addListener((m) => {
    if (m.tipo === "estado") {
      titulo = m.titulo || titulo;
      estado(aba, m.em_chamada, titulo, m.fim, { trilhas: m.trilhas, pico: m.pico, audio_ctx: m.audio_ctx });
      const antes = emChamada.get(aba) || false;
      if (m.em_chamada !== antes) {
        emChamada.set(aba, m.em_chamada);
        if (antes && !m.em_chamada) pararTela(aba); // saiu da chamada: para o vídeo também
        atualizarIcone(aba);
      }
    } else if (m.tipo === "audio" && m.pcm) {
      postar(`/api/extensao/audio?aba=${aba}`, deBase64(m.pcm), "application/octet-stream");
    }
  });
  // A aba fechou ou recarregou
  porta.onDisconnect.addListener(() => {
    estado(aba, false, titulo, true);
    emChamada.delete(aba);
    pararTela(aba);
  });
});
