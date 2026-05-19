// Voice playground — browser <-> /admin/playground/voice <-> xAI bridge.
//
// Mic capture: AudioWorklet downsamples the AudioContext rate (typically 48 kHz)
// to 8 kHz mono and μ-law-encodes per the G.711 standard. Encoded bytes are
// base64'd on the main thread and sent as {type:"media", payload} JSON frames.
//
// Aria audio: server sends {type:"media", payload} (μ-law/8 kHz). The decoder
// worklet upsamples to AudioContext rate and pipes Float32 samples to the
// destination. {type:"clear"} drains the playback queue (barge-in).
//
// Auth: this page is gated behind HTTP Basic; Chrome and Edge auto-send the
// cached Authorization header on the WS upgrade, so we don't need to attach
// credentials manually.

(function () {
  const $btn        = document.getElementById('pg-voice-call');
  const $status     = document.getElementById('pg-voice-status');
  const $meter      = document.getElementById('pg-voice-meter');
  const $transcript = document.getElementById('pg-voice-transcript');
  const $listenback = document.getElementById('pg-voice-listenback');
  const $lbAudio    = document.getElementById('pg-voice-listenback-audio');
  const $lbStatus   = document.getElementById('pg-voice-listenback-status');
  const $micWarn    = document.getElementById('pg-voice-mic-warning');
  const agentId     = window.PLAYGROUND_AGENT_ID || 'aria';
  const agentLabel  = window.PLAYGROUND_AGENT_LABEL || 'Aria';

  // Mic-silence watchdog. Combines two signals to avoid false-firing during
  // the assistant's turns: (a) caller mic level below threshold for MIC_SILENCE_MS,
  // (b) no Aria audio in the last ASSISTANT_RECENCY_MS (so we're not warning
  // mid-monologue). The threshold of 0.01 (linear RMS, ~−40 dBFS) sits
  // between the operator's verified-speech samples (~0.02–0.04) and his recording
  // noise floor (~0.0006), so real speech registers and ambient doesn't.
  // See diagnostic on call 4uAzR8VLhzI (2026-05-05) for the calibration.
  const MIC_LEVEL_THRESHOLD = 0.01;
  const MIC_SILENCE_MS      = 12000;
  const ASSISTANT_RECENCY_MS     = 2500;
  const WATCHDOG_GRACE_MS   = 5000;  // don't fire in the first 5s of a call

  let state = 'idle';        // idle | connecting | live | hanging-up
  let audioCtx = null;       // AudioContext (mic side; also used for playback)
  let micStream = null;      // MediaStream from getUserMedia
  let micNode = null;        // AudioWorkletNode (encoder)
  let playNode = null;       // AudioWorkletNode (decoder)
  let micSourceNode = null;  // MediaStreamAudioSourceNode (mic -> gain -> compressor -> encoder)
  let micGainNode = null;    // GainNode applying manual mic boost for xAI VAD
  let micCompressor = null;  // DynamicsCompressorNode catching emphatic-speech peaks
  let ws = null;             // WebSocket
  let levelTimer = null;     // RAF/interval for VU meter
  let callId = null;         // server-assigned call id, set by {type:"call_started"}
  let recordingPoll = null;  // setTimeout handle for the listen-back poller
  let watchdogTimer = null;  // setInterval handle for the mic-silence watchdog
  let lastMicLoudAt = 0;     // ms timestamp of last over-threshold caller-mic level
  let lastAssistantAudioAt = 0;   // ms timestamp of last received Aria media frame
  let callStartedAt = 0;     // ms timestamp of WS-open (anchor for grace period)

  function setStatus(text, isError) {
    $status.textContent = text;
    $status.classList.toggle('error', !!isError);
  }

  function setButton(label, calling) {
    $btn.textContent = label;
    $btn.classList.toggle('calling', !!calling);
  }

  function setMeter(pct) {
    $meter.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  // ---- Lifecycle ----------------------------------------------------------

  $btn.addEventListener('click', () => {
    if (state === 'idle')      startCall().catch(showFatal);
    else if (state === 'live') hangUp();
    // 'connecting' / 'hanging-up' are transient — ignore extra clicks.
  });

  async function startCall() {
    state = 'connecting';
    setButton('Connecting…', true);
    setStatus('Requesting microphone…');
    // Wipe any leftover UI from a previous call.
    callId = null;
    clearTranscript();
    clearListenback();

    try {
      // AGC off — we apply manual gain via a GainNode below. AGC + manual gain
      // fight each other (AGC sees boosted signal, ramps down → net quiet).
      // EC + NS still on, those help xAI's STT regardless of level.
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
      });
    } catch (err) {
      state = 'idle';
      setButton('Call ' + agentLabel, false);
      showFatal(err);
      return;
    }

    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    // Some browsers start the context suspended until a user gesture; the
    // click that triggered startCall() satisfies that, but resume() is a no-op
    // when already running so it's safe to await unconditionally.
    if (audioCtx.state === 'suspended') {
      try { await audioCtx.resume(); } catch (_) { /* ignore */ }
    }

    setStatus('Loading audio worklets…');
    await audioCtx.audioWorklet.addModule('/admin/static/ulaw-encoder-worklet.js');
    await audioCtx.audioWorklet.addModule('/admin/static/ulaw-decoder-worklet.js');

    setStatus('Connecting to ' + agentLabel + '...');
    // Same-origin WS — protocol matches the page (https→wss, http→ws).
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = proto + '//' + location.host + '/admin/playground/voice?agent_id=' + encodeURIComponent(agentId);
    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    const opened = new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
      ws.addEventListener('close', (e) => {
        if (state === 'connecting') reject(new Error('WS closed before open: code=' + e.code));
      }, { once: true });
    });

    try {
      await opened;
    } catch (err) {
      cleanup();
      state = 'idle';
      setButton('Call ' + agentLabel, false);
      showFatal(err);
      return;
    }

    // Wire encoder: mic stream -> encoder worklet -> main thread (base64 + WS send)
    micNode = new AudioWorkletNode(audioCtx, 'ulaw-encoder', {
      numberOfInputs: 1, numberOfOutputs: 0,
      processorOptions: { targetRateHz: 8000 },
    });
    micNode.port.onmessage = (e) => {
      const data = e.data;
      if (!data) return;
      if (data.type === 'ulaw' && data.bytes && ws && ws.readyState === WebSocket.OPEN) {
        const b64 = bytesToBase64(new Uint8Array(data.bytes));
        ws.send(JSON.stringify({ type: 'media', payload: b64 }));
      } else if (data.type === 'level') {
        // Simple linear meter (already 0..1 from worklet). Visual only.
        setMeter(data.level * 140); // scale a bit so quiet talk still moves the bar
        if (data.level >= MIC_LEVEL_THRESHOLD) {
          lastMicLoudAt = Date.now();
        }
      } else if (data.type === 'rate') {
        // One-shot diagnostic: confirm the AudioContext rate matches what
        // the encoder is decimating from. If srcRate isn't 48000/44100/etc.
        // you'll spot it in the console immediately.
        console.log('[voice-playground] AudioContext rate:', data.srcRate, 'Hz; encoding to', data.targetRate, 'Hz');
      }
    };
    micSourceNode = audioCtx.createMediaStreamSource(micStream);

    // Manual gain stage between mic and encoder. Browser-captured audio runs
    // about 8-13 dB quieter than what xAI's server VAD expects (Twilio path
    // gets aggressive telco-side AGC; browser path doesn't). Boost ~2× to
    // bring caller audio into the −15 to −10 dBFS zone where xAI VAD reliably
    // triggers on normal speech. Verified from per-second RMS on the test
    // recording vPxwOw3On1s.wav: RMS jumped from ~3000 (−21 dBFS) → idle
    // prompt fired despite caller speaking.
    micGainNode = audioCtx.createGain();
    micGainNode.gain.value = 2.5;
    console.log('[voice-playground] Applying mic gain:', micGainNode.gain.value, 'x (AGC disabled)');

    // Soft-knee compressor between gain and encoder. Catches emphatic-speech
    // peaks (e.g. spelling letters loudly, "M-A-R-K plus B-A-R-R-E-T-T-I")
    // that would otherwise saturate at 0 dBFS and present xAI's VAD with a
    // square-wave-like signal it doesn't recognize as speech.
    //
    // Diagnosed from call My6yeWDstoM (2026-05-05): caller channel hit
    // -13.8 dBFS RMS / -0.2 dBFS peak during email+phone spell-out; xAI
    // VAD detected nothing for 20s straight, idle watcher fired. Verified
    // transcribed turns earlier in the same call ran -20 to -34 dBFS RMS
    // with peaks ~-2 dBFS — comfortably below clipping.
    //
    // Threshold -14 dB / ratio 12:1 / 3 ms attack — tightened 2026-05-05
    // after retest call 8m7O3VInLp8 still showed -0.2 dBFS peaks during
    // emphatic speech. With these params, peaks cap around -12 to -13 dBFS
    // regardless of how hot the input is, comfortably below clipping. Soft
    // 6 dB knee + 100 ms release prevents audible pumping or breath
    // artifacts. NOTE: the original -10/8:1 settings (commit c59dbcd) may
    // not have been on the user's browser at the time of 8m7O3VInLp8 — the
    // script tag now has a ?v= cache-buster so this version is guaranteed
    // to load on next page open.
    micCompressor = audioCtx.createDynamicsCompressor();
    micCompressor.threshold.value = -14;
    micCompressor.knee.value = 6;
    micCompressor.ratio.value = 12;
    micCompressor.attack.value = 0.003;
    micCompressor.release.value = 0.1;

    micSourceNode.connect(micGainNode);
    micGainNode.connect(micCompressor);
    micCompressor.connect(micNode);

    // Wire decoder: WS frames -> decoder worklet -> destination
    playNode = new AudioWorkletNode(audioCtx, 'ulaw-decoder', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: { sourceRateHz: 8000 },
    });
    playNode.connect(audioCtx.destination);

    ws.addEventListener('message', onWsMessage);
    ws.addEventListener('close', onWsClose);
    ws.addEventListener('error', () => setStatus('WebSocket error.', true));

    state = 'live';
    setButton('⏹ Hang up', true);
    setStatus('Live - ' + agentLabel + ' should greet you in ~1 s.');
    startMicWatchdog();
  }

  function onWsMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); }
    catch (_) { return; }
    if (msg.type === 'media' && typeof msg.payload === 'string') {
      const bytes = base64ToBytes(msg.payload);
      // Transfer ownership to the worklet to avoid a copy.
      const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      playNode.port.postMessage({ type: 'ulaw', bytes: buf }, [buf]);
      lastAssistantAudioAt = Date.now();
    } else if (msg.type === 'clear') {
      playNode.port.postMessage({ type: 'clear' });
    } else if (msg.type === 'call_started' && typeof msg.call_id === 'string') {
      // Stash for the post-hangup recording poll.
      callId = msg.call_id;
    } else if (msg.type === 'turn' && (msg.role === 'caller' || msg.role === 'assistant')) {
      appendTurn(msg.role, msg.text || '');
    }
  }

  // ---- Transcript bubbles -------------------------------------------------

  function appendTurn(role, text) {
    if (!$transcript) return;
    if ($transcript.style.display === 'none') {
      $transcript.style.display = 'block';
    }
    const row = document.createElement('div');
    // Reuse the chat playground's bubble classes — same look as the Chat tab.
    // role "caller" is the human (mirrors the chat tab's "user"); role
    // "assistant" is Aria.
    row.className = 'pg-row ' + (role === 'caller' ? 'user' : 'assistant');
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = role === 'caller' ? 'You' : agentLabel;
    row.appendChild(who);
    row.appendChild(document.createTextNode(text));
    $transcript.appendChild(row);
    // Pin to bottom so the latest turn is always in view.
    $transcript.scrollTop = $transcript.scrollHeight;
  }

  function clearTranscript() {
    if (!$transcript) return;
    $transcript.innerHTML = '';
    $transcript.style.display = 'none';
  }

  // ---- Mic-silence watchdog ----------------------------------------------

  function startMicWatchdog() {
    stopMicWatchdog();  // belt-and-suspenders
    callStartedAt = Date.now();
    lastMicLoudAt = 0;        // forces grace-period gate below to control timing
    lastAssistantAudioAt = 0;
    hideMicWarning();
    watchdogTimer = setInterval(() => {
      if (state !== 'live') return;
      const now = Date.now();
      // Don't warn during the call's first few seconds — mic stream is still
      // ramping up, and Aria is often greeting which counts as recent audio.
      if (now - callStartedAt < WATCHDOG_GRACE_MS) return;
      // Don't warn while Aria is currently speaking — silence then is normal.
      if (now - lastAssistantAudioAt < ASSISTANT_RECENCY_MS) {
        hideMicWarning();
        return;
      }
      // Suppress until we've seen at least one above-threshold mic event in
      // this call. Otherwise a user who hasn't said anything yet (just
      // listening to the assistant's greeting) would trigger a false warning the
      // moment Aria stops talking.
      if (lastMicLoudAt === 0) return;
      const silentFor = now - lastMicLoudAt;
      if (silentFor >= MIC_SILENCE_MS) {
        showMicWarning();
      } else {
        hideMicWarning();
      }
    }, 1000);
  }

  function stopMicWatchdog() {
    if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
    hideMicWarning();
  }

  function showMicWarning() {
    if ($micWarn && $micWarn.style.display === 'none') {
      $micWarn.style.display = 'block';
    }
  }

  function hideMicWarning() {
    if ($micWarn && $micWarn.style.display !== 'none') {
      $micWarn.style.display = 'none';
    }
  }

  // ---- Recording listen-back ---------------------------------------------

  function clearListenback() {
    if (recordingPoll) { clearTimeout(recordingPoll); recordingPoll = null; }
    if ($listenback) { $listenback.style.display = 'none'; }
    if ($lbAudio) { $lbAudio.innerHTML = ''; }
    if ($lbStatus) { $lbStatus.textContent = ''; }
  }

  function startRecordingPoll() {
    if (!callId || !$listenback) return;
    $listenback.style.display = 'block';
    if ($lbStatus) $lbStatus.textContent = 'Uploading recording…';
    let attempts = 0;
    const maxAttempts = 8;     // ~12 s total at 1.5 s intervals
    const intervalMs = 1500;

    const tick = async () => {
      attempts += 1;
      try {
        const r = await fetch('/admin/playground/voice/recording/' + encodeURIComponent(callId), {
          credentials: 'same-origin',
        });
        if (r.ok) {
          const data = await r.json();
          if (data && data.ready && typeof data.url === 'string') {
            renderListenback(data.url);
            return;
          }
        }
      } catch (_) {
        // network blip — retry below
      }
      if (attempts >= maxAttempts) {
        if ($lbStatus) {
          $lbStatus.textContent = 'Recording still processing — check the call detail page in a moment.';
        }
        return;
      }
      recordingPoll = setTimeout(tick, intervalMs);
    };

    recordingPoll = setTimeout(tick, intervalMs);
  }

  function renderListenback(url) {
    if (!$lbAudio) return;
    $lbAudio.innerHTML = '';
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.src = url;
    audio.preload = 'metadata';
    $lbAudio.appendChild(audio);
    if ($lbStatus) $lbStatus.textContent = '';
  }

  function onWsClose(ev) {
    if (state === 'live' || state === 'hanging-up') {
      setStatus('Call ended (code ' + ev.code + ').');
      finalCleanup();
    }
  }

  function hangUp() {
    if (state !== 'live') return;
    state = 'hanging-up';
    setButton('Ending…', true);
    setStatus('Hanging up…');
    try {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stop' }));
      }
    } catch (_) { /* ignore */ }
    // Server closes the WS after post_call.run; onWsClose finishes cleanup.
    // Belt-and-suspenders: if the server takes too long, force-close after 5s.
    setTimeout(() => {
      if (state === 'hanging-up') {
        try { ws && ws.close(); } catch (_) { /* ignore */ }
        finalCleanup();
      }
    }, 5000);
  }

  function cleanup() {
    if (levelTimer) { clearInterval(levelTimer); levelTimer = null; }
    stopMicWatchdog();
    try { micSourceNode && micSourceNode.disconnect(); } catch (_) {}
    try { micGainNode && micGainNode.disconnect(); } catch (_) {}
    try { micCompressor && micCompressor.disconnect(); } catch (_) {}
    try { micNode && micNode.disconnect(); } catch (_) {}
    try { playNode && playNode.disconnect(); } catch (_) {}
    if (micStream) {
      for (const t of micStream.getTracks()) { try { t.stop(); } catch (_) {} }
      micStream = null;
    }
    if (audioCtx) { try { audioCtx.close(); } catch (_) {} audioCtx = null; }
    micNode = playNode = micSourceNode = micGainNode = micCompressor = null;
    setMeter(0);
  }

  function finalCleanup() {
    cleanup();
    state = 'idle';
    setButton('Call ' + agentLabel, false);
    // Recording upload happens server-side inside post_call.run after the WS
    // is torn down. Kick off the listen-back poll here (rather than in
    // onWsClose) so we still trigger when the 5-second hangUp force-close
    // timer fires before the server's WS close frame arrives — that path
    // calls finalCleanup() directly, and onWsClose can land afterward with
    // state=='idle' which the previous guard would've skipped.
    if (callId) startRecordingPoll();
  }

  function showFatal(err) {
    console.error(err);
    setStatus('Error: ' + (err && err.message ? err.message : String(err)), true);
  }

  // ---- Base64 helpers (avoid btoa/atob char-code edge cases) ---------------

  function bytesToBase64(bytes) {
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }
  function base64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  setStatus('Idle. Click call to begin.');
})();
