/**
 * Emoji for the recorded moves, from Pollen Robotics' Reachy Mini desktop app
 * (src/constants/choreographies.ts, Apache-2.0), so both apps show the same
 * face for the same move. The carousel shows the desktop app's featured
 * emotions first, then the rest in the map's order (grouped by feeling), then
 * any move missing here in the daemon's order.
 */
const FEATURED = [
  "loving1", "cheerful1", "laughing1", "surprised1", "curious1", "thoughtful1", "welcoming1", "grateful1",
  "proud1", "relief1", "confused1", "sad1", "tired1", "fear1", "rage1",
];

export const EMOTION_EMOJI: Record<string, string> = {
  fear1: "😨", scared1: "😱", anxiety1: "😟", sad1: "😭", sad2: "😢", lonely1: "🥺", downcast1: "😔",
  resigned1: "😞", yes_sad1: "🥹", no_sad1: "😥", cheerful1: "😊", loving1: "🥰", laughing1: "😂", laughing2: "🤣",
  amazed1: "🤩", enthusiastic1: "🎊", enthusiastic2: "🥳", proud1: "😎", proud2: "🏆", proud3: "💪", success1: "✨",
  success2: "🌟", rage1: "😡", furious1: "🤬", irritated1: "😠", irritated2: "😤", frustrated1: "😫",
  impatient1: "⏳", impatient2: "🙄", surprised1: "😲", surprised2: "😯", confused1: "😕", lost1: "😵‍💫",
  uncertain1: "🤨", incomprehensible2: "🤷", thoughtful1: "🤔", thoughtful2: "💭", curious1: "🧐",
  inquiring1: "❓", inquiring2: "🔍", inquiring3: "🤨", attentive1: "👂", attentive2: "🦉", tired1: "😴",
  exhausted1: "😩", sleep1: "💤", boredom1: "🥱", boredom2: "😑", relief1: "😌", relief2: "😮‍💨", serenity1: "🧘",
  calming1: "☮️", disgusted1: "🤢", displeased1: "😒", displeased2: "😑", contempt1: "🙄", welcoming1: "👋",
  welcoming2: "🤗", helpful1: "🙋", helpful2: "🤝", grateful1: "🙏", understanding1: "💡", understanding2: "🤝",
  no1: "👎", no_excited1: "🙅‍♂️", go_away1: "👉", reprimand1: "😤", reprimand2: "😡", reprimand3: "🚫", yes1: "👍",
  come1: "🫴", shy1: "😳", uncomfortable1: "😬", oops1: "🫣", oops2: "😅", indifferent1: "😐", dying1: "😵",
  electric1: "⚡", dance1: "💃", dance2: "🕺", dance3: "🪩",
};

export const DANCE_EMOJI: Record<string, string> = {
  stumble_and_recover: "🫨", chin_lead: "🎭", head_tilt_roll: "🔃", jackson_square: "🕴️", pendulum_swing: "🎐",
  side_glance_flick: "👁️", grid_snap: "🤖", simple_nod: "😌", side_to_side_sway: "🌊", polyrhythm_combo: "🥁",
  interwoven_spirals: "🌀", uh_huh_tilt: "😏", chicken_peck: "🐓", yeah_nod: "🙌", headbanger_combo: "🤘",
  side_peekaboo: "🙈", dizzy_spin: "💫", neck_recoil: "⚡", groovy_sway_and_roll: "🪩", sharp_side_tilt: "📐",
  "beyonce-single-ladies": "💍", "demon-hunters-1": "👹", "eagles-hotel-california": "🌴",
  "eminem-lose-yourself": "🎤", "feel-the-magic-in-the-air": "✨", "katy-perry-fireworks": "🎆",
  "las-ketchup": "🍅", "michael-jackson-thriller": "🧟", "paint-it-black": "🖤", "pharrell-williams-happy": "😀",
  "queen-we-will-rock-you": "👑", "spice-girls": "🎀", "the-fratellis-whistle-for-the-choir": "🎻",
  "the-white-stripes-seven-nation-army": "⚔️",
};

const FALLBACK = { emotions: "🙂", dances: "🎵" } as const;
export type MoveKind = keyof typeof FALLBACK;

export interface Move {
  name: string;
  emoji: string;
  label: string;
}

/** "proud2" → "Proud 2", "yes_sad1" → "Yes sad", "mini-deep-sleep" → "Mini deep sleep". */
export function moveLabel(name: string): string {
  const match = /^(.*?)(\d+)$/.exec(name);
  const base = (match ? match[1] : name).replace(/[_-]+/g, " ").trim();
  const variant = match && match[2] !== "1" ? ` ${match[2]}` : "";
  return base.charAt(0).toUpperCase() + base.slice(1) + variant;
}

/** The carousel's items: featured, then known moves in the map's order, then the rest. */
export function carouselMoves(names: string[], kind: MoveKind): Move[] {
  const map = kind === "emotions" ? EMOTION_EMOJI : DANCE_EMOJI;
  const keys = Object.keys(map);
  const order = kind === "emotions" ? [...FEATURED, ...keys.filter((k) => !FEATURED.includes(k))] : keys;
  const known = names.filter((n) => n in map).sort((a, b) => order.indexOf(a) - order.indexOf(b));
  const rest = names.filter((n) => !(n in map));
  return [...known, ...rest].map((name) => ({ name, emoji: map[name] ?? FALLBACK[kind], label: moveLabel(name) }));
}
