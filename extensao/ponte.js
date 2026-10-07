// Mundo isolado da extensão: recebe as mensagens de pagina.js e repassa para fundo.js,
// que é quem pode falar com o painel em localhost.
let porta = null;

function conectar() {
  porta = chrome.runtime.connect({ name: "meet" });
  porta.onDisconnect.addListener(() => { porta = null; });
}

function paraBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let texto = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    texto += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(texto);
}

window.addEventListener("message", (e) => {
  if (e.source !== window || !e.data || e.data.__reunioes !== 1) return;
  const { __reunioes, pcm, ...msg } = e.data;
  if (pcm) msg.pcm = paraBase64(pcm);
  try {
    if (!porta) conectar();
    porta.postMessage(msg);
  } catch (_) {
    porta = null; // o service worker reiniciou; reconecta na próxima mensagem
  }
});
