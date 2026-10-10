// Render-compatible, turn-based microphone -> WAV -> Sarvam voice connection.
// This file does not contain or receive any API credentials.
(function () {
  function encodeWav(chunks, inputRate) {
    const input = new Float32Array(chunks.reduce((n, chunk) => n + chunk.length, 0));
    let offset = 0;
    for (const chunk of chunks) { input.set(chunk, offset); offset += chunk.length; }
    const rate = 16000;
    const length = Math.floor(input.length * rate / inputRate);
    const bytes = new ArrayBuffer(44 + length * 2), view = new DataView(bytes);
    const write = (at, text) => [...text].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    write(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); write(8, 'WAVE');
    write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    write(36, 'data'); view.setUint32(40, length * 2, true);
    for (let i = 0; i < length; i++) {
      const index = i * inputRate / rate, left = Math.floor(index), fraction = index - left;
      const sample = Math.max(-1, Math.min(1, input[left] * (1 - fraction) + input[Math.min(left + 1, input.length - 1)] * fraction));
      view.setInt16(44 + i * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
    }
    const data = new Uint8Array(bytes);
    let binary = '';
    for (let i = 0; i < data.length; i += 8192) binary += String.fromCharCode(...data.subarray(i, i + 8192));
    return btoa(binary);
  }

  class HostedVoice {
    constructor(options) {
      this.options = options; this.closed = false; this.waiting = true; this.playing = false;
      this.chunks = []; this.preRoll = []; this.samples = 0; this.silence = 0;
      this.audio = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 16000});
      this.microphone = null; this.socket = null; this.playSource = null;
    }

    async connect() {
      await this.audio.resume();
      if (this.closed) return;
      this.microphone = await navigator.mediaDevices.getUserMedia({audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      }});
      if (this.closed) { this.microphone.getTracks().forEach(track => track.stop()); return; }
      this.input = this.audio.createMediaStreamSource(this.microphone);
      // Simple browser VAD keeps the hosted Python process free of the local audio stack.
      this.recorder = this.audio.createScriptProcessor(1024, 1, 1);
      this.muted = this.audio.createGain(); this.muted.gain.value = 0;
      this.input.connect(this.recorder); this.recorder.connect(this.muted); this.muted.connect(this.audio.destination);
      this.recorder.onaudioprocess = event => this.capture(event.inputBuffer.getChannelData(0));
      const address = new URL('/api/voice/ws', window.location.href);
      address.protocol = address.protocol === 'https:' ? 'wss:' : 'ws:';
      this.socket = new WebSocket(address);
      this.socket.onopen = () => {
        if (!this.closed) this.socket.send(JSON.stringify({type: 'start', glid: this.options.glid, role: this.options.role}));
      };
      this.socket.onmessage = event => {
        try { this.message(JSON.parse(event.data)).catch(error => this.fail(error)); }
        catch (error) { this.fail(error); }
      };
      this.socket.onerror = () => this.fail(new Error('Voice connection failed. Check Render logs and WebSocket support.'));
      this.socket.onclose = () => {
        if (!this.closed) this.fail(new Error('Voice connection ended. Click Call to reconnect.'));
      };
    }

    capture(samples) {
      if (this.closed || this.waiting || this.playing || this.socket?.readyState !== WebSocket.OPEN) return;
      const chunk = Float32Array.from(samples);
      const rms = Math.sqrt(chunk.reduce((sum, value) => sum + value * value, 0) / chunk.length);
      if (!this.chunks.length) {
        this.preRoll.push(chunk);
        if (this.preRoll.length > 3) this.preRoll.shift();
        if (rms < 0.008) return;
        this.chunks = this.preRoll; this.preRoll = [];
        this.samples = this.chunks.reduce((sum, part) => sum + part.length, 0);
        this.silence = 0;
        this.options.onPhase('Listening…');
      } else {
        this.chunks.push(chunk); this.samples += chunk.length;
        this.silence = rms < 0.008 ? this.silence + chunk.length : 0;
      }
      if (this.silence < this.audio.sampleRate * 0.7 && this.samples < this.audio.sampleRate * 15) return;
      if (this.samples < this.audio.sampleRate * 0.35) {
        this.chunks = []; this.samples = 0; this.silence = 0; return;
      }
      const audio = encodeWav(this.chunks, this.audio.sampleRate);
      this.chunks = []; this.preRoll = []; this.samples = 0; this.silence = 0; this.waiting = true;
      this.options.onPhase('Understanding and preparing reply…');
      this.socket.send(JSON.stringify({type: 'audio', audio}));
    }

    async message(packet) {
      if (this.closed) return;
      if (packet.type === 'error') throw new Error(packet.message);
      if (packet.type === 'ready') {
        this.waiting = false; this.options.onPhase(packet.message || 'Listening…'); return;
      }
      if (packet.type !== 'reply') return;
      this.playing = true;
      this.options.onReady();
      this.options.onPhase('Agent speaking…');
      for (const encoded of packet.audios || []) {
        if (this.closed) return;
        const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
        const buffer = await this.audio.decodeAudioData(bytes.buffer);
        if (this.closed) return;
        await new Promise(resolve => {
          this.playSource = this.audio.createBufferSource(); this.playSource.buffer = buffer;
          this.playSource.connect(this.audio.destination); this.playSource.onended = resolve;
          this.playSource.start();
        });
      }
      if (this.closed) return;
      // Allow the microphone's echo suppression to settle after playback.
      await new Promise(resolve => setTimeout(resolve, 250));
      if (this.closed) return;
      this.playing = false; this.waiting = false;
      this.options.onPhase('Listening… Speak after the agent finishes.');
    }

    fail(error) {
      if (this.closed) return;
      this.close(); this.options.onError(error);
    }

    close() {
      if (this.closed) return;
      this.closed = true;
      if (this.socket) {
        this.socket.onopen = this.socket.onmessage = this.socket.onerror = this.socket.onclose = null;
        if (this.socket.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({type: 'stop'}));
        this.socket.close();
      }
      if (this.recorder) { this.recorder.onaudioprocess = null; this.recorder.disconnect(); }
      this.input?.disconnect(); this.muted?.disconnect();
      this.microphone?.getTracks().forEach(track => track.stop());
      if (this.playSource) { try { this.playSource.stop(); } catch (_) {} }
      this.audio.close().catch(() => {});
    }
  }

  globalThis.HostedVoice = HostedVoice;
  globalThis.encodeHostedWav = encodeWav;
})();
