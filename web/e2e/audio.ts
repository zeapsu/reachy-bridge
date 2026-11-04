import { createHash, createHmac, randomBytes } from "node:crypto";
import { closeSync, existsSync, openSync, readFileSync, readSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Page } from "@playwright/test";

// What the voice tests listen for: tools/sim/run.py --audio plays one tone
// per robot source into the audio gateway and writes its outputs to raw
// files; the browser's fake mic plays the third tone.

const env = (name: string, fallback: string) => process.env[name] ?? fallback;
export const TONES = {
  mic: Number(env("SIM_TONE_MIC", "500")),
  robot: Number(env("SIM_TONE_ROBOT", "700")),
  browser: Number(env("SIM_TONE_BROWSER", "1100")),
};
export type Tone = keyof typeof TONES;
const FILE_RATE = Number(env("SIM_AUDIO_RATE", "16000"));
/** A tone's amplitude (0 to 1) at or above this is there, below SILENT it's not. */
export const HEARD = 0.02;
export const SILENT = 0.005;

/**
 * The browser mic's WAV: the browser tone in 300 ms bursts, so the browser's
 * noise suppression doesn't take it for steady noise. Written once, in the
 * run's audio dir.
 */
export function browserMicFile(): string {
  return micFile(`browser-mic-${TONES.browser}.wav`, 0.3);
}

/** A browser mic that sends only silence: a dead or wrong input. */
export function silentMicFile(): string {
  return micFile("browser-mic-silent.wav", 0);
}

function micFile(name: string, volume: number): string {
  const path = join(env("SIM_AUDIO", "."), name);
  if (existsSync(path)) return path;
  const rate = 48000;
  const seconds = 2;
  const samples = rate * seconds;
  const data = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) {
    const on = (i / rate) % 0.5 < 0.3;
    data.writeInt16LE(on ? Math.round(volume * 32767 * Math.sin((2 * Math.PI * TONES.browser * i) / rate)) : 0, i * 2);
  }
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data.length, 40);
  writeFileSync(path, Buffer.concat([header, data]));
  return path;
}

/**
 * A tone's amplitude in one stretch of samples, as a sine of that amplitude
 * would give: Goertzel, Hann-windowed so a loud tone doesn't leak into its
 * neighbours' readings.
 */
function amplitude(samples: Float32Array, rate: number, freq: number): number {
  const n = samples.length;
  if (n < 2) return 0;
  const c = 2 * Math.cos((2 * Math.PI * freq) / rate);
  let s1 = 0;
  let s2 = 0;
  let gain = 0;
  for (let i = 0; i < n; i++) {
    const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
    gain += w;
    const s = samples[i] * w + c * s1 - s2;
    s2 = s1;
    s1 = s;
  }
  return (2 * Math.sqrt(Math.max(s1 * s1 + s2 * s2 - c * s1 * s2, 0))) / gain;
}

/**
 * Each tone's amplitude in `samples`, averaged over blocks of `block`
 * samples, so a join between recorded blocks doesn't smear it.
 */
export function levels(samples: Float32Array, rate: number, block: number): Record<Tone, number> {
  const out = {} as Record<Tone, number>;
  const blocks = Math.floor(samples.length / block);
  for (const [name, freq] of Object.entries(TONES) as [Tone, number][]) {
    let sum = 0;
    for (let i = 0; i < blocks; i++) sum += amplitude(samples.subarray(i * block, (i + 1) * block), rate, freq);
    out[name] = blocks ? sum / blocks : 0;
  }
  return out;
}

/** One of the gateway's output files: what reached the robot's speaker or its apps' mic. */
export type Output = "speaker" | "apps";

/** Where an output file ends now, to measure what comes after. */
export function mark(output: Output): number {
  try {
    return statSync(join(env("SIM_AUDIO", ""), `${output}.raw`)).size;
  } catch {
    return 0;
  }
}

/** The tones in an output file since `from`, and how many seconds of audio that was. */
export function heardIn(output: Output, from: number): { seconds: number; tones: Record<Tone, number> } {
  const path = join(env("SIM_AUDIO", ""), `${output}.raw`);
  const end = mark(output);
  const bytes = Math.max(0, end - from) & ~1;
  const buffer = Buffer.alloc(bytes);
  if (bytes) {
    const fd = openSync(path, "r");
    try {
      readSync(fd, buffer, 0, bytes, from);
    } finally {
      closeSync(fd);
    }
  }
  const samples = new Float32Array(bytes / 2);
  for (let i = 0; i < samples.length; i++) samples[i] = buffer.readInt16LE(i * 2) / 32768;
  return { seconds: samples.length / FILE_RATE, tones: levels(samples, FILE_RATE, FILE_RATE / 10) };
}

type Recording = { rate: number; samples: number[]; state: string; packets: number };
type Tap = { __record: (ms: number) => Promise<Recording> };

/**
 * Before the page loads: keep every peer connection and the audio tracks it
 * receives, and let the test record the latest one (what this browser hears).
 */
export async function tapIncomingAudio(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const tracks: MediaStreamTrack[] = [];
    const peers: RTCPeerConnection[] = [];
    const Original = window.RTCPeerConnection;
    class Tapped extends Original {
      constructor(config?: RTCConfiguration) {
        super(config);
        peers.push(this);
        this.addEventListener("track", (event) => tracks.push(event.track));
      }
    }
    window.RTCPeerConnection = Tapped as typeof RTCPeerConnection;
    const packets = async () => {
      let total = 0;
      for (const peer of peers) {
        if (peer.connectionState === "closed") continue;
        (await peer.getStats()).forEach((s: { type: string; kind?: string; packetsReceived?: number }) => {
          if (s.type === "inbound-rtp" && s.kind === "audio") total += s.packetsReceived ?? 0;
        });
      }
      return total;
    };
    // Samples of the latest live track for `ms`, at the context's rate, and
    // the RTP packets received meanwhile.
    (window as unknown as Tap).__record = async (ms) => {
      const track = [...tracks].reverse().find((t) => t.readyState === "live");
      if (!track) return { rate: 48000, samples: [], state: "no track", packets: 0 };
      const before = await packets();
      const context = new AudioContext();
      await context.resume();
      const source = context.createMediaStreamSource(new MediaStream([track]));
      const analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser);
      const samples: number[] = [];
      const block = new Float32Array(analyser.fftSize);
      const end = performance.now() + ms;
      while (performance.now() < end) {
        analyser.getFloatTimeDomainData(block);
        samples.push(...block);
        await new Promise((r) => setTimeout(r, (analyser.fftSize / context.sampleRate) * 1000));
      }
      const { sampleRate: rate, state } = context;
      await context.close();
      return { rate, samples, state, packets: (await packets()) - before };
    };
  });
}

/**
 * What this browser receives over `ms`: each tone's level, how many seconds
 * were recorded, whether WebAudio was running, and the RTP packets that came.
 */
export async function heardInBrowser(page: Page, ms = 1000) {
  const { rate, samples, state, packets } = await page.evaluate((ms) => (window as unknown as Tap).__record(ms), ms);
  return { tones: levels(Float32Array.from(samples), rate, 2048), seconds: samples.length / rate, state, packets };
}

/** The audio gateway's session, asked as the controller does (signed with its key). */
export async function gatewaySession(): Promise<null | Record<string, unknown>> {
  const key = readFileSync(env("SIM_AUDIO_KEY", ""), "utf8").trim();
  const seconds = Math.floor(Date.now() / 1000);
  const nonce = randomBytes(16).toString("hex");
  const body = createHash("sha256").update("").digest("hex");
  const mac = createHmac("sha256", key).update(`request:${seconds}:${nonce}:GET:/status:${body}`).digest("hex");
  const reply = await fetch(`${env("SIM_AUDIO_GATEWAY", "")}/status`, { headers: { "X-Audio-Auth": `${seconds}.${nonce}.${mac}` } });
  if (!reply.ok) throw new Error(`audio gateway status: ${reply.status}`);
  return ((await reply.json()) as { session: null | Record<string, unknown> }).session;
}
