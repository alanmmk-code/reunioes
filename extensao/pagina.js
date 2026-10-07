// Roda dentro da página do Meet (mundo "MAIN"), antes do código do Meet.
// Observa as conexões WebRTC da chamada: sabe quando ela está conectada e pega as trilhas de áudio
// que chegam dos outros participantes. Mistura tudo em 16 kHz mono e repassa para ponte.js.
(() => {
  const Original = window.RTCPeerConnection;
  if (!Original) return;

  const conexoes = new Set();
  let trilhasRecebidas = 0;
  let ctx = null, misturador = null;
  let pedacos = [], amostras = 0, pico = 0;

  function prepararAudio() {
    if (ctx) return;
    ctx = new AudioContext({ sampleRate: 16000 });
    misturador = ctx.createGain();
    // ScriptProcessor (e não AudioWorklet): o worklet precisaria carregar um arquivo, e a CSP do Meet bloqueia.
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const mudo = ctx.createGain();
    mudo.gain.value = 0; // só para o processador rodar; o Meet já toca o som normalmente
    misturador.connect(proc);
    proc.connect(mudo);
    mudo.connect(ctx.destination);
    proc.onaudioprocess = (e) => {
      if (!emChamada()) return;
      const dados = e.inputBuffer.getChannelData(0);
      const int16 = new Int16Array(dados.length);
      for (let i = 0; i < dados.length; i++) {
        const v = Math.max(-1, Math.min(1, dados[i]));
        int16[i] = v * 32767;
        if (Math.abs(v) > pico) pico = Math.abs(v);
      }
      pedacos.push(int16);
      amostras += int16.length;
    };
  }

  function adicionarTrilha(trilha) {
    prepararAudio();
    const stream = new MediaStream([trilha]);
    // O Chrome só entrega áudio WebRTC remoto ao Web Audio se o stream também estiver num elemento de mídia
    // (sem isso chega silêncio). Elemento mudo: o som continua saindo só pelo Meet.
    const tocador = new Audio();
    tocador.muted = true;
    tocador.srcObject = stream;
    tocador.play().catch(() => {});
    const fonte = ctx.createMediaStreamSource(stream);
    fonte.connect(misturador);
    trilhasRecebidas++;
    trilha.addEventListener("ended", () => { fonte.disconnect(); tocador.srcObject = null; });
  }

  class ConexaoObservada extends Original {
    constructor(...args) {
      super(...args);
      conexoes.add(this);
      this.addEventListener("track", (e) => {
        if (e.track.kind === "audio") adicionarTrilha(e.track);
      });
    }
  }
  window.RTCPeerConnection = ConexaoObservada;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = ConexaoObservada;

  function emChamada() {
    if (!trilhasRecebidas) return false; // tela de espera: ainda não chega áudio de ninguém
    for (const c of conexoes) {
      if (c.connectionState === "closed" || c.connectionState === "failed") conexoes.delete(c);
    }
    return conexoes.size > 0;
  }

  function enviar(msg, transferir) {
    window.postMessage({ __reunioes: 1, ...msg }, location.origin, transferir || []);
  }

  setInterval(() => {
    const chamada = emChamada();
    if (chamada && ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
    enviar({ tipo: "estado", em_chamada: chamada, titulo: document.title, trilhas: trilhasRecebidas,
             pico: Math.round(pico * 1000) / 1000, audio_ctx: ctx ? ctx.state : "nenhum" });
    pico = 0;
    if (amostras) {
      const tudo = new Int16Array(amostras);
      let pos = 0;
      for (const p of pedacos) { tudo.set(p, pos); pos += p.length; }
      pedacos = []; amostras = 0;
      enviar({ tipo: "audio", pcm: tudo.buffer }, [tudo.buffer]);
    }
  }, 1000);

  // Saiu da página (fechou a aba, navegou para fora): avisa na hora
  window.addEventListener("pagehide", () => enviar({ tipo: "estado", em_chamada: false, fim: true, titulo: document.title }));
})();
