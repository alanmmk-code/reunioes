// Documento oculto (offscreen) da extensão: grava o vídeo da aba do Meet e manda ao painel em pedaços.
const PAINEL = "http://127.0.0.1:5055";
const gravadores = new Map(); // aba -> MediaRecorder

chrome.runtime.onMessage.addListener((m) => {
  if (m.alvo !== "tela") return;
  if (m.acao === "iniciar") iniciar(m.aba, m.streamId);
  else if (m.acao === "parar") parar(m.aba);
});

async function iniciar(aba, streamId) {
  if (gravadores.has(aba)) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false, // o áudio já vem pela página (capturar o áudio da aba deixaria o Meet mudo para você)
      video: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId,
                            maxFrameRate: 15, maxWidth: 1920, maxHeight: 1080 } },
    });
  } catch (e) {
    chrome.runtime.sendMessage({ alvo: "fundo", acao: "tela_falhou", aba, erro: String(e) });
    return;
  }
  const tipo = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"].find((t) => MediaRecorder.isTypeSupported(t));
  const rec = new MediaRecorder(stream, { mimeType: tipo, videoBitsPerSecond: 1200000 }); // ~540 MB por hora
  let parte = 0, fila = Promise.resolve();
  rec.ondataavailable = (e) => {
    if (!e.data.size) return;
    const n = parte++;
    // Em ordem: os pedaços juntos formam um único arquivo WebM
    fila = fila.then(() => fetch(`${PAINEL}/api/extensao/tela?aba=${aba}&parte=${n}`, { method: "POST", body: e.data })
      .catch(() => {}));
  };
  rec.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    gravadores.delete(aba);
    chrome.runtime.sendMessage({ alvo: "fundo", acao: "tela_parou", aba });
  };
  // A aba fechou: a captura acaba sozinha
  stream.getVideoTracks()[0].addEventListener("ended", () => rec.state !== "inactive" && rec.stop());
  rec.start(5000); // um pedaço a cada 5 s
  gravadores.set(aba, rec);
}

function parar(aba) {
  const rec = gravadores.get(aba);
  if (rec && rec.state !== "inactive") rec.stop();
}
