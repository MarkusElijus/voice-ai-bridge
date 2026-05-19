// AudioWorkletProcessor: μ-law (8 kHz) -> PCM Float32 at AudioContext rate.
//
// Receives {type:"ulaw", bytes: ArrayBuffer} messages — each chunk is one or
// more 8 kHz μ-law samples. We μ-law-decode into a normalized PCM queue, then
// linearly upsample on-the-fly so process() emits one sample per output frame
// at sampleRate (typically 48 kHz).
//
// {type:"clear"} drains the queue (barge-in) so Aria stops mid-utterance when
// the caller speaks.
//
// Linear interpolation is fine for telephony-band speech; the source is
// already band-limited to 4 kHz, so resampling artifacts above that are
// inaudible.

class UlawDecoderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.srcRate = opts.sourceRateHz || 8000;
    this.dstRate = sampleRate;
    this.step = this.srcRate / this.dstRate;       // e.g. 8000/48000 = 0.16667

    // Pre-allocated ring buffer at SOURCE rate.
    //
    // CRITICAL: must be large enough to absorb a full xAI burst. xAI realtime
    // sends a turn's audio bytes much faster than 1× wall-clock — a 12-second
    // Aria response can arrive in under a second of WS frames. The browser is
    // the wall-clock player, so we need to buffer the entire burst until
    // playback catches up. Sizing for a 90-second response covers every
    // realistic Aria turn (long availability lists, structured info read-back,
    // etc.). Memory: 90 × 8000 × 4 B = 2.88 MB — trivial.
    //
    // Sized too small (e.g. 4 s) → drops the OLDEST samples of long bursts,
    // which sounds like Aria skipping ahead through her sentences. That was
    // the symptom Mark hit on the first local test.
    //
    // Single big Float32Array + (head, length) avoids per-quantum allocations
    // on the audio render thread (no GC pauses).
    this.capacity = this.srcRate * 90;
    this.queue = new Float32Array(this.capacity);
    this.head = 0;          // index of the next sample to consume
    this.length = 0;        // valid samples in queue starting at head

    // Phase accumulator into the source queue (fractional offset from head).
    this.phase = 0;

    this.port.onmessage = (e) => {
      const msg = e.data;
      if (!msg) return;
      if (msg.type === 'ulaw' && msg.bytes) {
        this.enqueueUlaw(new Uint8Array(msg.bytes));
      } else if (msg.type === 'clear') {
        this.head = 0;
        this.length = 0;
        this.phase = 0;
      }
    };
  }

  enqueueUlaw(bytes) {
    // Decode + write into the ring buffer. If the new data won't fit, drop
    // the oldest samples (compact in place) before writing.
    const need = bytes.length;
    if (need >= this.capacity) {
      // Single chunk larger than ring — keep just the newest tail.
      this.head = 0;
      this.length = 0;
      this.phase = 0;
      const start = bytes.length - this.capacity;
      for (let i = 0; i < this.capacity; i++) {
        this.queue[i] = muLawToLinear(bytes[start + i]) / 0x8000;
      }
      this.length = this.capacity;
      return;
    }
    if (this.length + need > this.capacity) {
      // Drop the oldest samples to make room. Compact: move tail back to index 0.
      const drop = (this.length + need) - this.capacity;
      const keepStart = this.head + drop;
      const keepLen = this.length - drop;
      this.queue.copyWithin(0, keepStart, keepStart + keepLen);
      this.head = 0;
      this.length = keepLen;
      // phase referenced an offset from old head; recompute relative to new head.
      this.phase = Math.max(0, this.phase - drop);
    }
    // Compact for tail-of-buffer write if needed.
    if (this.head + this.length + need > this.capacity) {
      this.queue.copyWithin(0, this.head, this.head + this.length);
      this.head = 0;
    }
    const writeAt = this.head + this.length;
    for (let i = 0; i < need; i++) {
      this.queue[writeAt + i] = muLawToLinear(bytes[i]) / 0x8000;
    }
    this.length += need;
  }

  process(_inputs, outputs) {
    const out = outputs[0];
    if (!out || out.length === 0) return true;
    const ch = out[0];
    if (!ch) return true;

    if (this.length < 2) {
      ch.fill(0);
      return true;
    }

    const head = this.head;
    let phase = this.phase;
    const step = this.step;
    const lengthMinus1 = this.length - 1;

    for (let i = 0; i < ch.length; i++) {
      const idx = Math.floor(phase);
      if (idx >= lengthMinus1) {
        // Underrun — emit silence and pause phase advance until more arrive.
        ch[i] = 0;
        continue;
      }
      const frac = phase - idx;
      const a = this.queue[head + idx];
      const b = this.queue[head + idx + 1];
      ch[i] = a + (b - a) * frac;
      phase += step;
    }

    // Compact: advance head over consumed samples (no allocation).
    const consumed = Math.min(Math.floor(phase), this.length);
    if (consumed > 0) {
      this.head += consumed;
      this.length -= consumed;
      phase -= consumed;
      // Periodically reset head to 0 to keep the trailing capacity available.
      // This is a memmove inside the buffer, not an allocation.
      if (this.head > this.capacity / 2 && this.length > 0) {
        this.queue.copyWithin(0, this.head, this.head + this.length);
        this.head = 0;
      } else if (this.length === 0) {
        this.head = 0;
      }
    }
    this.phase = phase;
    return true;
  }
}

// G.711 μ-law decode — bit-exact reference implementation.
//   ulaw: unsigned 8-bit μ-law byte
//   returns: signed 16-bit linear PCM (range ~ -32635 .. 32635)
function muLawToLinear(ulaw) {
  ulaw = ~ulaw & 0xff;
  const sign = ulaw & 0x80;
  const exponent = (ulaw >> 4) & 0x07;
  const mantissa = ulaw & 0x0f;
  let sample = ((mantissa << 3) + 0x84) << exponent;
  sample -= 0x84;
  return sign ? -sample : sample;
}

registerProcessor('ulaw-decoder', UlawDecoderProcessor);
