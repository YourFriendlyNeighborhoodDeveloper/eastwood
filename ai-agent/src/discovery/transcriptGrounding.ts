/**
 * Transcript grounding for captured slots.
 *
 * A voice model is a predictive system: handed a clipped or noisy utterance it
 * will happily produce a plausible completion and carry on as though it heard
 * a real answer. That is how a qualification record ends up containing values
 * the prospect never said.
 *
 * The rule enforced here: a slot may only be stored if the value can be traced
 * to words the prospect was actually transcribed as saying. The model must
 * quote what it heard; that quote is then checked against the finalized
 * transcripts of the prospect's own speech. Nothing else counts as evidence —
 * not the model's confidence, not its summary, not its inference.
 *
 * ── On confidence scores ──────────────────────────────────────────────────
 * The Realtime API does not report a confidence number directly, but it will
 * attach per-token logprobs to input transcription when the session asks for
 * `item.input_audio_transcription.logprobs`. That is a real measurement of how
 * sure the transcriber was, and it is what `confidenceFromLogprobs` converts
 * into a probability. Only the gpt-4o-transcribe family emits them; whisper-1
 * does not, and the field has been intermittently missing in the API, so
 * confidence is always optional here. Absent logprobs mean "unscored", never
 * "confident" — a missing score must not read as a passing one.
 */

/** One token of transcription as the API reports it. */
export interface TranscriptLogprob {
  token?: string;
  logprob?: number;
}

/**
 * Mean per-token probability of a transcription, or null when the API sent no
 * logprobs. Uses the mean logprob rather than the product: the product falls
 * toward zero as an utterance gets longer, which would score a long clear
 * answer worse than a short mumbled one.
 */
export function confidenceFromLogprobs(
  logprobs: TranscriptLogprob[] | undefined | null
): number | null {
  if (!Array.isArray(logprobs)) return null;
  const values = logprobs
    .map((l) => l?.logprob)
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  if (values.length === 0) return null;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  return Math.exp(mean);
}

/** A finalized transcript of the prospect's own speech. */
export interface HeardTranscript {
  text: string;
  at?: number;
  /** Mean per-token probability, or null/undefined when unscored. */
  confidence?: number | null;
}

export type GroundingResult =
  | {
      status: "grounded";
      matched: string;
      /** Confidence of the transcript that supported the claim, if scored. */
      confidence: number | null;
    }
  /** Transcripts exist for this call, but none supports the claimed quote. */
  | { status: "rejected"; reason: string }
  /** No finalized transcript yet — transcription runs async and can lag a
   *  tool call. Not proof of invention, but not proof of anything else. */
  | { status: "unverifiable"; reason: string };

/** Lowercase, strip punctuation, collapse whitespace. */
export function normalize(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Spoken-form digits, so "sixty eight" matches a captured 68. */
const NUMBER_WORDS: Record<string, string> = {
  zero: "0", one: "1", two: "2", three: "3", four: "4", five: "5",
  six: "6", seven: "7", eight: "8", nine: "9", ten: "10",
  eleven: "11", twelve: "12", thirteen: "13", fourteen: "14", fifteen: "15",
  sixteen: "16", seventeen: "17", eighteen: "18", nineteen: "19",
  twenty: "20", thirty: "30", forty: "40", fifty: "50",
  sixty: "60", seventy: "70", eighty: "80", ninety: "90",
  hundred: "100", thousand: "1000",
};

function tokens(s: string): string[] {
  return normalize(s).split(" ").filter(Boolean);
}

/**
 * Token overlap of the claimed quote against a transcript, ignoring filler.
 * Exact substring is the strong signal; overlap catches the ordinary case
 * where the model's rendering differs slightly from Whisper's.
 */
const FILLER = new Set([
  "a", "an", "the", "uh", "um", "er", "like", "just", "you", "know",
  "i", "im", "is", "was", "it", "its", "and", "or", "so", "well", "yeah",
  "my", "me", "to", "of", "for", "that", "this", "have", "has", "do", "does",
]);

function contentTokens(s: string): string[] {
  return tokens(s).filter((t) => !FILLER.has(t));
}

/** Does `claim` appear in `transcript`, allowing spoken-number forms? */
function supports(transcript: string, claim: string): boolean {
  const t = normalize(transcript);
  const c = normalize(claim);
  if (!c) return false;
  if (t.includes(c)) return true;

  // Spoken numbers: "sixty eight" in the transcript supports a claim of "68".
  const spokenDigits = tokens(transcript)
    .map((w) => NUMBER_WORDS[w])
    .filter(Boolean)
    .join("");
  if (spokenDigits && c.replace(/\s/g, "").includes(spokenDigits)) return true;

  const claimContent = contentTokens(claim);
  if (claimContent.length === 0) return false;
  const transcriptSet = new Set(contentTokens(transcript));
  const hits = claimContent.filter((tok) => transcriptSet.has(tok)).length;
  // Most of the substantive words in the quote must actually be present.
  return hits / claimContent.length >= 0.6;
}

/**
 * Check a claimed verbatim quote against what the prospect was transcribed as
 * saying. `heardVerbatim` is the model's report of the prospect's own words —
 * not a paraphrase, not the parsed value.
 */
export function checkGrounding(
  heardVerbatim: string | undefined | null,
  transcripts: HeardTranscript[] | undefined | null,
  opts: { since?: number | null } = {}
): GroundingResult {
  const claim = (heardVerbatim ?? "").trim();
  // Absent (not merely empty) transcripts mean this session predates
  // transcript capture or was built without it — no evidence is available
  // either way, so report unverifiable rather than crashing or, worse,
  // silently treating it as verified.
  const heard = transcripts ?? [];

  if (heard.length === 0) {
    return {
      status: "unverifiable",
      reason: "no finalized transcript available yet for this call",
    };
  }
  if (!claim) {
    return {
      status: "rejected",
      reason:
        "no verbatim quote supplied — you must report what the prospect actually said",
    };
  }
  // Evidence must come from the turn being answered, not from anywhere on the
  // call. Without this bound a capture can quote something the prospect said
  // ten questions ago — or something transcription hallucinated onto a silent
  // segment, which is how "Thank you." came to justify a burial preference in
  // a traced call. `since` is when the prospect's current turn ended, so this
  // turn's transcript (finalized after it) passes and every earlier one does
  // not. Callers pass null for a read-back, where the quote is legitimately
  // one turn old and the HEARD→CONFIRMED path has its own guard.
  // An untimed transcript cannot be shown to belong to this turn, so under a
  // bound it does not count. Same posture as everything else here: unproven is
  // not the same as fine.
  const since = opts.since ?? null;
  const eligible =
    since === null ? heard : heard.filter((t) => typeof t.at === "number" && t.at >= since);
  if (eligible.length === 0) {
    return {
      status: "rejected",
      reason:
        "nothing was transcribed from the prospect's most recent turn, so there " +
        "is no evidence for this capture — ask again rather than recording it",
    };
  }

  // Newest first: when several turns could support the claim, the one being
  // answered right now is the relevant evidence, and its confidence is the
  // score that should follow the capture.
  for (let i = eligible.length - 1; i >= 0; i--) {
    const t = eligible[i]!;
    if (supports(t.text, claim)) {
      return { status: "grounded", matched: t.text, confidence: t.confidence ?? null };
    }
  }
  return {
    status: "rejected",
    reason:
      since === null
        ? "the quoted words do not appear in anything the prospect was heard to say on this call"
        : "the quoted words do not appear in what the prospect just said — you may " +
          "only record an answer from the turn you are answering",
  };
}
