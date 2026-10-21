// Roda dentro da página do Meet (mundo "MAIN"), antes do código do Meet.
// Observa as conexões WebRTC da chamada: sabe quando ela está conectada e pega as trilhas de áudio
// que chegam dos outros participantes. Mistura tudo em 16 kHz mono e repassa para ponte.js.
//
// Desde o Chrome 155 (16/10/2026) as trilhas pegas só pelo evento "track" chegavam mudas. Por isso a trilha
// também é pega quando o próprio Meet a põe num <audio> para tocar (srcObject), e o elemento auxiliar toca com
// volume 0 em vez de "muted" (elemento mudo pode não puxar o áudio). O diagnóstico vai no estado para o log.
(() => {
  const Original = window.RTCPeerConnection;
  if (!Original) return;

  const conexoes = new Set();
  const ligadas = new Map(); // id da trilha -> {trilha, pico}
  let trilhasRecebidas = 0, elementosDoMeet = 0;
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

  function medidor(info, fonte) {
    // pico de cada trilha, para o diagnóstico mostrar se alguma traz som
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    fonte.connect(an);
    const buf = new Float32Array(an.fftSize);
    info.medir = () => {
      an.getFloatTimeDomainData(buf);
      for (const v of buf) if (Math.abs(v) > info.pico) info.pico = Math.abs(v);
    };
  }

  function adicionarTrilha(trilha) {
    if (!trilha || trilha.kind !== "audio" || ligadas.has(trilha.id)) return;
    const info = { trilha, pico: 0, medir: () => {} };
    ligadas.set(trilha.id, info); // antes do tocador: o srcObject dele passa pelo gancho abaixo
    prepararAudio();
    const stream = new MediaStream([trilha]);
    // O Chrome só entrega áudio WebRTC remoto ao Web Audio se o stream também estiver tocando num elemento de mídia.
    // Volume 0 (e não muted): o som continua saindo só pelo Meet.
    const tocador = new Audio();
    tocador.volume = 0;
    tocador.__reunioes = true;
    tocador.srcObject = stream;
    tocador.play().catch(() => {});
    const fonte = ctx.createMediaStreamSource(stream);
    fonte.connect(misturador);
    medidor(info, fonte);
    trilhasRecebidas++;
    trilha.addEventListener("ended", () => {
      fonte.disconnect(); tocador.srcObject = null; ligadas.delete(trilha.id);
    });
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

  // Só trilhas que chegam da chamada (nunca o seu microfone, que também pode ir para um elemento de mídia)
  function ehRemota(t) {
    for (const c of conexoes) {
      try { if (c.getReceivers().some((r) => r.track === t)) return true; } catch (_) { /* conexão fechada */ }
    }
    return false;
  }

  // As trilhas que o Meet de fato toca: quando ele põe um stream num <audio>/<video>, liga as de áudio também
  const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "srcObject");
  if (desc && desc.set) {
    Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
      configurable: true, enumerable: desc.enumerable,
      get() { return desc.get.call(this); },
      set(valor) {
        desc.set.call(this, valor);
        try {
          if (!this.__reunioes && valor instanceof MediaStream && valor.getAudioTracks().length) {
            elementosDoMeet++;
            for (const t of valor.getAudioTracks()) if (ehRemota(t)) adicionarTrilha(t);
            valor.addEventListener("addtrack", (e) => { if (ehRemota(e.track)) adicionarTrilha(e.track); });
          }
        } catch (_) { /* nunca atrapalha o Meet */ }
      },
    });
  }

  function emChamada() {
    if (!trilhasRecebidas) return false; // tela de espera: ainda não chega áudio de ninguém
    for (const c of conexoes) {
      if (c.connectionState === "closed" || c.connectionState === "failed") conexoes.delete(c);
    }
    return conexoes.size > 0;
  }

  function diagnostico() {
    const lista = [...ligadas.values()];
    lista.forEach((i) => i.medir());
    const d = {
      ativas: lista.filter((i) => i.trilha.readyState === "live").length,
      mudas: lista.filter((i) => i.trilha.muted).length,
      com_som: lista.filter((i) => i.pico > 0.01).length,
      elementos: elementosDoMeet,
    };
    lista.forEach((i) => { i.pico = 0; });
    return d;
  }

  function enviar(msg, transferir) {
    window.postMessage({ __reunioes: 1, ...msg }, location.origin, transferir || []);
  }

  setInterval(() => {
    const chamada = emChamada();
    if (chamada && ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
    enviar({ tipo: "estado", em_chamada: chamada, titulo: document.title, trilhas: trilhasRecebidas,
             pico: Math.round(pico * 1000) / 1000, audio_ctx: ctx ? ctx.state : "nenhum",
             diag: ctx ? diagnostico() : null });
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
