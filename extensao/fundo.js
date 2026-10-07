// Service worker: repassa ao painel (localhost) o estado da chamada e o áudio de cada aba do Meet.
const PAINEL = "http://127.0.0.1:5055";

function postar(caminho, corpo, tipo) {
  return fetch(PAINEL + caminho, { method: "POST", headers: { "Content-Type": tipo }, body: corpo })
    .catch(() => {}); // painel fechado: tenta de novo no próximo segundo
}

function estado(aba, em_chamada, titulo, fim, extra) {
  return postar("/api/extensao/estado", JSON.stringify({ ...(extra || {}), aba, em_chamada, titulo, fim: !!fim }),
                "application/json");
}

function deBase64(texto) {
  const bin = atob(texto);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

chrome.runtime.onConnect.addListener((porta) => {
  if (porta.name !== "meet" || !porta.sender || !porta.sender.tab) return;
  const aba = porta.sender.tab.id;
  let titulo = "Meet";
  porta.onMessage.addListener((m) => {
    if (m.tipo === "estado") {
      titulo = m.titulo || titulo;
      estado(aba, m.em_chamada, titulo, m.fim, { trilhas: m.trilhas, pico: m.pico, audio_ctx: m.audio_ctx });
    } else if (m.tipo === "audio" && m.pcm) {
      postar(`/api/extensao/audio?aba=${aba}`, deBase64(m.pcm), "application/octet-stream");
    }
  });
  // A aba fechou ou recarregou
  porta.onDisconnect.addListener(() => estado(aba, false, titulo, true));
});
