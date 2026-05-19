// AudioWorkletProcessor: PCM (Float32, AudioContext rate) -> μ-law (8 kHz).
//
// `process()` is called per 128-sample render quantum. We accumulate samples
// in a ring buffer, run a low-pass average + decimation step to drop to 8 kHz
// (matches Twilio/xAI on-the-wire format), then μ-law-encode each 8 kHz
// sample via the standard G.711 algorithm.
//
// Why decimation w/ averaging instead of strict FIR low-pass: telephony band
// (8 kHz sample → 4 kHz Nyquist) tolerates the modest aliasing this introduces,
// and computing a proper polyphase filter inside the audio thread for every
// quantum is unnecessary overhead. xAI's input is already speech-band content.

class UlawEncoderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRateHz || 8000;
    this.srcRate = sampleRate;                      // global, set by AudioWorkletGlobalScope

    // Rate-accurate fractional decimation. Integer averaging-window logic
    // (round(srcRate/targetRate) input samples per output) only matches the
    // target rate exactly when srcRate/targetRate is an integer — true for
    // 48 kHz → 8 kHz (6:1), but NOT for 44.1 kHz → 8 kHz (5.5125:1, which
    // would round to 6 and produce 7350 Hz instead of 8000 Hz). Wrong-rate
    // audio reaching xAI sounds slowed down and confuses STT (mis-recognized
    // names, words). Track expected output count via cumulative input count
    // — this gives bit-exact target rate regardless of ratio.
    this.inputCount = 0;
    this.outputCount = 0;
    this.acc = 0;
    this.accN = 0;

    // Buffer one frame's worth of μ-law before posting. ~20 ms at 8 kHz = 160 bytes.
    this.frameBytes = 160;
    this.outBuf = new Uint8Array(this.frameBytes);
    this.outIdx = 0;
    // Level meter: track max abs sample over a meter window (~50 ms).
    this.levelMax = 0;
    this.levelCount = 0;
    this.levelWindow = Math.max(1, Math.round(this.srcRate * 0.05));

    // Tell the host (one-shot) so we can confirm hardware rate from the page.
    this.port.postMessage({ type: 'rate', srcRate: this.srcRate, targetRate: this.targetRate });
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (!ch) return true;

    for (let i = 0; i < ch.length; i++) {
      const s = ch[i];

      // VU meter
      const a = s < 0 ? -s : s;
      if (a > this.levelMax) this.levelMax = a;
      if (++this.levelCount >= this.levelWindow) {
        this.port.postMessage({ type: 'level', level: this.levelMax });
        this.levelMax = 0;
        this.levelCount = 0;
      }

      // Fractional decimation: produce one output sample whenever
      // floor(inputCount * targetRate / srcRate) advances.
      this.acc += s;
      this.accN += 1;
      this.inputCount += 1;
      const expected = Math.floor(this.inputCount * this.targetRate / this.srcRate);
      while (this.outputCount < expected) {
        const avg = this.acc / this.accN;
        this.acc = 0;
        this.accN = 0;
        this.outputCount += 1;

        // PCM16 from Float32, clamp first
        const clamped = avg < -1 ? -1 : (avg > 1 ? 1 : avg);
        const pcm16 = clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff);
        this.outBuf[this.outIdx++] = linearToMuLaw(pcm16);

        if (this.outIdx >= this.frameBytes) {
          // Transfer the frame; allocate a fresh buffer for the next batch.
          const out = this.outBuf;
          this.outBuf = new Uint8Array(this.frameBytes);
          this.outIdx = 0;
          this.port.postMessage({ type: 'ulaw', bytes: out.buffer }, [out.buffer]);
        }
      }
    }
    return true;
  }
}

// G.711 μ-law encode — bit-exact to the standard reference.
//   sample: signed 16-bit linear PCM
//   returns: unsigned 8-bit μ-law byte
function linearToMuLaw(sample) {
  const MU_BIAS = 0x84;
  const MU_CLIP = 32635;

  let sign = 0;
  if (sample < 0) {
    sign = 0x80;
    sample = -sample;
  }
  if (sample > MU_CLIP) sample = MU_CLIP;
  sample = sample + MU_BIAS;

  // Find segment (exponent): position of highest set bit in [7..14].
  let seg = 7;
  for (let mask = 0x4000; (sample & mask) === 0 && seg > 0; mask >>= 1) seg--;

  const mantissa = (sample >> (seg + 3)) & 0x0f;
  const ulaw = ~(sign | (seg << 4) | mantissa) & 0xff;
  return ulaw;
}

registerProcessor('ulaw-encoder', UlawEncoderProcessor);
