/**
 * Kitchen-friendly "new order" ding.
 *
 * We synthesise the sound with the Web Audio API instead of shipping an mp3 —
 * a two-tone chime is unmistakably a notification, and every kilobyte we do
 * not ship is one less thing to cache-bust when the design changes.
 *
 * Browsers block audio until the tab has received a user gesture (click, key,
 * touch). The first order that arrives before the barista has clicked anywhere
 * on the dashboard will be silent; every subsequent order rings. That's the
 * platform's rule, not ours to fight.
 */

let ctx: AudioContext | null = null;

function getContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (ctx) return ctx;
  const Ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!Ctor) return null;
  ctx = new Ctor();
  return ctx;
}

/**
 * Two-tone (E5 → G5) sine chime, ~500ms total, at conservative volume so it
 * carries in a busy kitchen without startling the barista in a quiet cafe.
 */
export function playNewOrderSound(): void {
  const context = getContext();
  if (!context) return;

  // If the tab has never had a gesture, the context is "suspended". resume()
  // best-effort — a failing resume just means we stay silent this round.
  if (context.state === "suspended") {
    void context.resume().catch(() => {});
  }

  const now = context.currentTime;
  const gain = context.createGain();
  gain.gain.setValueAtTime(0.001, now);
  gain.gain.exponentialRampToValueAtTime(0.25, now + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.001, now + 0.55);
  gain.connect(context.destination);

  const tones: Array<[number, number]> = [
    [659.25, 0.0], // E5
    [783.99, 0.14], // G5
  ];
  for (const [freq, offset] of tones) {
    const osc = context.createOscillator();
    osc.type = "sine";
    osc.frequency.value = freq;
    osc.connect(gain);
    osc.start(now + offset);
    osc.stop(now + 0.55);
  }
}
