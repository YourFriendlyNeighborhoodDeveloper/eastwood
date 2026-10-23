/**
 * The turn gate: decide whether the prospect actually answered, BEFORE the
 * assistant is allowed to speak.
 *
 * ── Why this exists ───────────────────────────────────────────────────────
 * Server-side VAD opens and closes a turn on SOUND, not on WORDS. A cough, a
 * door, a half-second of line noise, or the prospect drawing breath is enough
 * to end a turn. With `turn_detection.create_response: true` the Realtime API
 * then generates a reply immediately — before input transcription has produced
 * anything, and with nothing in the audio that constitutes an answer. An
 * audio-native model handed that will produce the most probable continuation
 * of a sales call, which is "Perfect, thanks." followed by the next question.
 *
 * Every check downstream of that moment is too late. update_lead_field can
 * refuse to STORE the invented answer, but the prospect has already been
 * thanked for information nobody has, and has already been asked something
 * else. The record stays clean while the conversation goes wrong.
 *
 * So response creation is taken away from VAD and done here instead. When the
 * prospect's turn ends we wait (briefly, bounded) for the transcript, rule on
 * what was actually heard, and only then tell the model to speak — and what
 * kind of turn it is allowed to take.
 *
 * ── What counts as an answer ──────────────────────────────────────────────
 * All three of these, or it is not an answer:
 *   1. the transcript is FINALIZED (a partial is not evidence of anything),
 *   2. recognition confidence is above threshold (see transcriptGrounding),
 *   3. it semantically answers the question that was asked.
 *
 * The functions here are deliberately deterministic and conservative. They
 * decide "is this definitely not an answer?" with high precision rather than
 * "is this an answer?" with high recall — a wrong NO costs one "sorry, could
 * you say that again?", and a wrong YES costs a fabricated fact that nobody
 * ever knows to go back and check.
 */

import { normalize } from "./transcriptGrounding.js";

/** What the assistant's last question was looking for. */
export type QuestionExpectation =
  | "yes_no"
  /** An either/or question — "burial or cremation?", "term or whole life?".
   *  Structurally identical to a yes/no ("would you...", "do you want...") but
   *  the answer is one of the alternatives, and it contains no yes and no no.
   *  Classifying these as yes_no is what made "burial" fail to answer the
   *  burial-or-cremation question, so the slot never left HEARD and the call
   *  looped on it — see the choice branch in classifyQuestion. */
  | "choice"
  | "name"
  | "number"
  | "amount"
  | "free_text"
  /** No question was asked, or we could not read one — shape is unknown. */
  | "none";

export type TurnOutcome =
  /** Real, confident words that answer what was asked. Proceed normally. */
  | "heard_answer"
  /** Real words with real content, but recognition was too poor to trust them.
   *  Distinct from not_heard on purpose: there IS something to read back, so
   *  the recovery is "sixty-eight, is that right?" — one yes — rather than
   *  making the prospect repeat the whole answer. Advancing is still
   *  forbidden; the slot is HEARD, not CONFIRMED. */
  | "heard_unclear"
  /** Real words, but not an answer to the question. Deal with what they said;
   *  the question is still outstanding. */
  | "heard_other"
  /** Nothing usable: silence, noise, filler, or a clipped fragment. There is
   *  nothing to acknowledge, nothing to read back, and nothing to advance to. */
  | "not_heard";

/** Why a turn was ruled not_heard — drives which clarification is spoken. */
export type NotHeardReason =
  | "no_transcript"
  | "transcription_failed"
  | "empty"
  | "noise_only"
  | "filler_only"
  | "backchannel_only"
  | "clipped"
  | "non_answer"
  | "low_confidence";

export interface TurnVerdict {
  outcome: TurnOutcome;
  /** Set only when outcome is "not_heard". */
  notHeardReason: NotHeardReason | null;
  /** Human-readable, logged and fed to the model. */
  reason: string;
  confidence: number | null;
  /** The transcript the ruling was made on, when there was one. */
  transcript: string | null;
}

export interface TurnEvidence {
  /** Finalized transcript of THIS turn, or null when none arrived in time.
   *  A partial transcript must never be passed here — pass null. */
  transcript: string | null;
  /** Mean per-token probability, or null when the API sent no logprobs.
   *  Null means UNSCORED, which is not the same as confident: an unscored
   *  transcript passes this check and is judged on its content alone. */
  confidence: number | null;
  /** The transcriber explicitly reported it could not make out the audio. */
  transcriptionFailed: boolean;
  /** What the assistant's last question was looking for. */
  expectation: QuestionExpectation;
  /** Below this mean per-token probability the words are not trusted. */
  confidenceMin: number;
}

// ── Lexicons ────────────────────────────────────────────────────────────────

/** Words that carry no content and cannot, alone, constitute an answer. */
const FILLER = new Set([
  "a", "an", "the", "like", "just", "you", "know", "i", "im", "is", "was",
  "it", "its", "and", "or", "so", "well", "to", "of", "for", "that", "this",
  "my", "me", "am", "be", "there", "here", "s", "t",
]);

/**
 * Hesitation and backchannel sounds. Deliberately includes "mhm" and "uh huh":
 * on a clean line a person means yes by those, but on a phone line they are
 * also what a transcriber produces from a breath or a passing car. The cost of
 * treating one as a yes is a wrong fact on an insurance record; the cost of
 * not doing so is one short "was that a yes or a no?" — which is exactly what
 * an experienced agent asks when they are not sure.
 */
const HESITATION = new Set([
  "uh", "uhh", "uhhh", "um", "umm", "ummm", "er", "err", "erm", "ah", "ahh",
  "eh", "ehh", "oh", "ohh", "hm", "hmm", "hmmm", "mm", "mmm", "mhm", "mmhm",
  "mmhmm", "uhhuh", "huh", "ugh", "tsk", "aah", "yknow",
]);

/**
 * Whole utterances that are, in themselves, not answers to anything — they are
 * the prospect asking US something, stalling, or reporting a line problem.
 * Matched against the WHOLE normalized utterance rather than by token
 * membership, so "sorry, no" is still a no and only a bare "sorry" is not.
 */
const NON_ANSWER_PATTERNS: RegExp[] = [
  /^(what|sorry|pardon|excuse me|come again|again)$/,
  /^(what was that|what did you say|say that again|say again|can you say that again|could you say that again|can you repeat that|could you repeat that|repeat that|one more time|once more)$/,
  /^(hang on|hold on|hold on a (sec|second|minute|moment)|one (sec|second|moment|minute)|just a (sec|second|moment|minute)|give me a (sec|second|minute)|wait|wait a (sec|second|minute))$/,
  /^(hello|hi|hey|yello)$/,
  // Courtesies and farewells. These are here because of what transcription
  // does on SILENCE, not because prospects say them at odd moments: whisper's
  // most common hallucination on a near-silent segment is "Thank you." or
  // "Bye-bye.", and a real traced call is full of them. Left as answers they
  // are worse than noise — they are confident, well-formed text that reaches
  // userTranscripts, and from there they can ground a capture, so a slot the
  // prospect never spoke to gets stored as CONFIRMED with a verbatim quote
  // nobody said. Neither is ever an answer to a discovery question.
  /^(thanks|thank you|thank you very much|thanks a lot|cheers|you re welcome|youre welcome|no problem)$/,
  /^(bye|bye bye|byebye|goodbye|good bye|see you|take care|ok bye|okay bye|alright bye)$/,
  /^(who is this|whos this|who s this|who are you|what is this|whats this|what s this|whats this about|who am i speaking to)$/,
  /^(are you (still )?there|you (still )?there|can you hear me|i cant hear you|i can t hear you|youre breaking up|you re breaking up|bad connection|the lines bad|the line s bad)$/,
];

/** Explicit yes. */
const AFFIRM = new Set([
  "yes", "yeah", "yep", "yup", "yea", "ya", "aye", "correct", "right",
  "sure", "absolutely", "definitely", "certainly", "affirmative", "true",
  "ok", "okay", "do", "does", "did", "have", "has", "had", "will", "would",
  "can", "could", "would", "indeed", "exactly", "precisely",
]);

/** Explicit no. */
const NEGATE = new Set([
  "no", "nope", "nah", "naw", "never", "negative", "not", "dont", "don",
  "didnt", "doesnt", "havent", "hasnt", "wont", "cant", "none", "nothing",
  "nobody", "neither", "nor", "nonsmoker", "nonsmoking", "false", "incorrect",
  "wrong", "nowhere",
]);

/**
 * Qualified answers. The prospect answered — just not with a bare yes or no.
 * "I quit", "I smoke occasionally", "used to", "only socially" are all real,
 * usable answers and must not be sent back for a repeat. Kept generic (tense,
 * frequency, cessation, hedging) rather than topic-specific, so it works for
 * every yes/no question on the call, not only the tobacco one.
 */
const QUALIFIER = new Set([
  "quit", "quitting", "quits", "stopped", "stop", "gave", "given", "up",
  "used", "formerly", "previously", "before", "since", "ago", "years", "year",
  "months", "month", "weeks", "week", "days", "day",
  "occasionally", "sometimes", "socially", "rarely", "seldom", "hardly",
  "barely", "only", "once", "twice", "daily", "weekly", "monthly", "yearly",
  "few", "couple", "several", "bit", "little", "lot",
  "maybe", "probably", "possibly", "think", "believe", "guess", "suppose",
  "kind", "sort", "mostly", "mainly", "partly", "depends", "sometimes",
  "still", "always", "usually", "often",
]);

/** Spoken-form numbers, so "sixty eight" counts as a numeric answer. */
const NUMBER_WORDS = new Set([
  "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
  "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
  "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
  "forty", "fourty", "fifty", "sixty", "seventy", "eighty", "ninety",
  "hundred", "thousand", "million", "grand", "k", "half", "quarter",
]);

// ── Token helpers ───────────────────────────────────────────────────────────

function tokens(s: string): string[] {
  return normalize(s).split(" ").filter(Boolean);
}

/** Tokens that carry meaning — filler and hesitation removed. */
export function contentTokens(s: string): string[] {
  return tokens(s).filter((t) => !FILLER.has(t) && !HESITATION.has(t));
}

// ── "Definitely not an answer" detectors ────────────────────────────────────

/**
 * The transcriber emitted only an annotation of a SOUND, not of speech:
 * "[background noise]", "(coughing)", "*static*", "[BLANK_AUDIO]", "...".
 * Run against the RAW text — normalize() strips the brackets that identify it.
 */
export function isNoiseOnly(raw: string): boolean {
  const t = (raw ?? "").trim();
  if (!t) return false;
  // Remove every bracketed/starred annotation, then see if anything is left.
  const stripped = t.replace(/[[(<*][^\])>*]*[\])>*]/g, "").trim();
  if (stripped === "") return true;
  // Nothing but punctuation or symbols — "...", "—", "?!".
  if (/^[\p{P}\p{S}\s]+$/u.test(stripped)) return true;
  // Bare markers some transcribers emit without brackets.
  return /^(inaudible|unintelligible|indistinct|blank[_ ]audio|silence|no speech|background noise|noise|static)$/i.test(
    normalize(stripped)
  );
}

/** Nothing but hesitation sounds — "uh...", "um", "hmm". */
export function isFillerOnly(raw: string): boolean {
  const t = tokens(raw);
  if (t.length === 0) return false;
  return t.every((tok) => HESITATION.has(tok) || FILLER.has(tok));
}

/** Nothing but a backchannel grunt — "mhm", "uh huh". Ambiguous by nature. */
export function isBackchannelOnly(raw: string): boolean {
  const t = tokens(raw);
  if (t.length === 0) return false;
  const hasBackchannel = t.some((tok) => HESITATION.has(tok));
  return hasBackchannel && t.every((tok) => HESITATION.has(tok) || FILLER.has(tok));
}

/**
 * The utterance was cut off, or is the transcriber's rendering of overlapping
 * speech. A leading or trailing dash is the standard marker for both.
 */
export function isClipped(raw: string): boolean {
  const t = (raw ?? "").trim();
  if (!t) return false;
  if (/^[-–—]/.test(t)) return true;
  if (/[-–—]$/.test(t)) return true;
  // Trailing off mid-thought, with too little said to be an answer. A longer
  // sentence ending in "..." is a complete answer that merely trails off.
  if (/(\.\.\.|…)$/.test(t) && contentTokens(t).length < 2) return true;
  return false;
}

/** The whole utterance is a stall, a question back at us, or a line report. */
export function isNonAnswerUtterance(raw: string): boolean {
  const n = normalize(raw);
  if (!n) return false;
  return NON_ANSWER_PATTERNS.some((re) => re.test(n));
}

// ── Question classification ─────────────────────────────────────────────────

/**
 * What shape of answer the assistant's last question was looking for, read
 * from what it actually said out loud. Only the last question counts — if
 * Imogen said "That helps. And are you a smoker?", the pending question is the
 * yes/no one.
 */
export function classifyQuestion(assistantText: string | null | undefined): QuestionExpectation {
  const text = (assistantText ?? "").trim();
  if (!text || !text.includes("?")) return "none";

  // Everything after the second-to-last "?" is the last question asked.
  const parts = text.split("?").map((p) => p.trim()).filter(Boolean);
  const segment = parts[parts.length - 1] ?? "";

  // Then the last CLAUSE of it. She leads into questions — "That helps. And
  // are you a smoker?", "Quick one first — any tobacco in the past year?" —
  // and the lead-in is what obscures the question's shape. Split before
  // normalizing, which strips the punctuation that marks the boundary.
  const clauses = segment
    .split(/[.!;—–]|(?:\s-\s)/)
    .map((c) => c.trim())
    .filter(Boolean);
  const rawLast = clauses[clauses.length - 1] ?? segment;

  // A short adverbial opener sits in front of the auxiliary too, and unlike
  // the discourse markers below it is punctuated — "For rating purposes, are
  // you male or female?". Stripped before normalize, which removes the comma
  // that marks where it ends. Kept short and comma-anchored so it cannot eat
  // into a screening list ("...in the last 5 years: cancer, heart disease...").
  // Only a genuine adverbial, never the question's own stem: "Which would you
  // rather, term or whole life?" opens with the words classification depends
  // on, so stripping to the comma there would throw the question away.
  const last = rawLast.replace(/^([^,?]{0,28}),\s*/, (whole, opener: string) =>
    /\b(are|is|was|were|am|do|does|did|have|has|had|will|would|can|could|should|shall|may|might|which|what|who|whose|how|any)\b/i.test(
      opener
    )
      ? whole
      : ""
  );

  // Leading conjunctions and discourse markers sit in front of the auxiliary
  // that identifies a yes/no question.
  const q = normalize(last).replace(
    /^(and|so|but|now|then|ok|okay|right|well|also|plus|anyway|alright)\s+/,
    ""
  );
  if (!q) return "none";

  // Name questions read as free text otherwise ("what's your first name" would
  // fall through to free_text), so they are matched before the generic cases.
  if (/\b(first|last|full|middle|maiden)?\s*name\b/.test(q)) return "name";
  if (/\b(who am i speaking (with|to)|what should i call you|how do you spell)\b/.test(q)) {
    return "name";
  }

  // Money and quantity, before the yes/no auxiliaries — "did you have an
  // amount in mind" is asking for a number, not a yes.
  if (/\b(how much|how many|what.{0,12}(budget|premium|amount)|per month|monthly|coverage amount|face amount|dollars?)\b/.test(q)) {
    return "amount";
  }
  if (/\b(how old|what.{0,10}age|how tall|what.{0,10}(height|weight)|what year|which year|how long)\b/.test(q)) {
    return "number";
  }

  // Choice, BEFORE yes/no — every either/or question she asks opens with an
  // auxiliary, a modal, or a copula ("would you prefer...", "are you male or
  // female?") and would otherwise be read as a yes/no. It is not: the answer
  // is one of the alternatives, so it contains neither a yes nor a no, and
  // judging it as a yes/no rejects the correct answer and asks for it again
  // forever. This is what stuck the call on burial-or-cremation and on the
  // rating-sex question, which is a prerequisite for get_quote.
  //
  // The "or" alone proves nothing — "any tobacco or nicotine in the past
  // twelve months?" is a genuine yes/no. Three things disqualify it:
  //   - a literal yes/no ("was that a yes or a no?", "...or not?")
  //   - "any", which marks the or-phrase as the object of a yes/no question
  //     ("do you have any children or grandchildren?")
  //   - a comma LIST, which is a screening question rather than a choice
  //     ("cancer, heart disease or heart attack, diabetes, stroke, COPD?").
  //     Two or more commas, not one: a single comma is how she separates the
  //     stem from the alternatives ("Which would you rather, term or whole
  //     life?"), and treating that as a list threw the choice away.
  //
  // Requiring a selection verb ("prefer", "rather", "want") was too narrow —
  // the phrasing the prompt actually uses, "Would this mostly be for burial or
  // cremation costs?", has none of them, and a live trace caught it still
  // being read as a yes/no. The exclusions carry the weight instead, and the
  // error costs are wildly asymmetric: a choice wrongly read as yes/no loops
  // the call forever and blocks the quote, while a yes/no wrongly read as a
  // choice only relaxes the grunt check — and grunts are already rejected
  // upstream as backchannel, filler or noise, whatever the expectation.
  const listed = (last.match(/,/g) ?? []).length >= 2;
  const literalYesNo = /\b(yes\s+or|or\s+(a\s+)?(no|not))\b/.test(q);
  if (/\bor\b/.test(q) && !literalYesNo && !/\bany\b/.test(q) && !listed) {
    return "choice";
  }

  // Yes/no: an auxiliary or modal opening, or an explicit either/or tail.
  if (/^(are|is|was|were|am|do|does|did|have|has|had|will|would|can|could|should|shall|may|might|any|anyone|anything)\b/.test(q)) {
    return "yes_no";
  }
  if (/\b(yes or no|or no|is that (right|correct)|have i got that right|correct|right)\s*$/.test(q)) {
    return "yes_no";
  }

  return "free_text";
}

// ── Does the utterance answer the question? ─────────────────────────────────

/**
 * Whether the words given actually answer the shape of question that was
 * asked. Used to separate "they answered" from "they said something else" —
 * NOT to police wording. A qualified answer ("I quit", "only socially") is a
 * complete answer and must pass.
 */
export function answersExpectation(raw: string, expectation: QuestionExpectation): boolean {
  const content = contentTokens(raw);
  if (content.length === 0) return false;

  switch (expectation) {
    case "yes_no": {
      return content.some(
        (t) => AFFIRM.has(t) || NEGATE.has(t) || QUALIFIER.has(t)
      );
    }
    case "number":
    case "amount": {
      return content.some((t) => /\d/.test(t) || NUMBER_WORDS.has(t));
    }
    case "name": {
      // A name is a word, not a digit and not a stall.
      return content.some((t) => /^\p{L}{2,}$/u.test(t));
    }
    case "choice": {
      // Any real word answers a choice question. Which alternative it names is
      // the model's to read and the grounding check's to verify against the
      // transcript — not something to decide from a token list here, which
      // would need to know the alternatives of every question she can ask.
      // The grunts and noise this gate exists to catch are already gone: they
      // never survive contentTokens.
      return content.some((t) => /^\p{L}{2,}$/u.test(t));
    }
    case "free_text":
    case "none":
    default:
      return true;
  }
}

/**
 * Whether the words quoted actually support the VALUE being recorded, for a
 * choice question.
 *
 * checkGrounding proves the quote was said. It does not prove the quote has
 * anything to do with the value — `heardVerbatim: "Thank you."` with
 * `value: "burial"` passes it, because the prospect did say "Thank you" at
 * some point. For a yes/no question answersExpectation caught that by
 * accident; for a choice question nothing did, so any transcribed phrase
 * could carry any value into the record as CONFIRMED.
 *
 * A choice question is the one case where this is decidable: the answer IS one
 * of the alternatives, so the value has to appear in what they said. Not
 * applied to free text, where it would be wrong — "final expenses" is a
 * correct reading of "I don't want my daughter stuck with the bill", and no
 * token is shared.
 */
export function quoteSupportsChoice(value: unknown, heardVerbatim: string | null): boolean {
  const claim = contentTokens(heardVerbatim ?? "");
  if (claim.length === 0) return false;
  const wanted = contentTokens(String(value ?? ""));
  if (wanted.length === 0) return false;
  // Every word of the value must be present. Values here are one or two words
  // ("burial", "female", "type 2"), so this is a containment test, not fuzzy
  // matching — a near-miss on a categorical value is a different category.
  return wanted.every((w) => claim.some((c) => c === w || c.startsWith(w) || w.startsWith(c)));
}

// ── The ruling ──────────────────────────────────────────────────────────────

const REASON_TEXT: Record<NotHeardReason, string> = {
  no_transcript:
    "no finalized transcript arrived for this turn — nothing the prospect said was recognized",
  transcription_failed:
    "the transcriber reported it could not make out the audio",
  empty: "the transcript for this turn was empty",
  noise_only:
    "the turn contained sound but no speech (background noise, a cough, or line noise)",
  filler_only:
    "the prospect made a hesitation sound but did not say anything",
  backchannel_only:
    "the prospect made an ambiguous sound that could be a yes, a no, or nothing at all",
  clipped:
    "the audio was cut off or overlapped, so only a fragment was recognized",
  non_answer:
    "the prospect asked for a repeat or stalled — they did not give an answer",
  low_confidence:
    "the words were recognized too poorly to be trusted",
};

function notHeard(
  reason: NotHeardReason,
  confidence: number | null,
  transcript: string | null
): TurnVerdict {
  return {
    outcome: "not_heard",
    notHeardReason: reason,
    reason: REASON_TEXT[reason],
    confidence,
    transcript,
  };
}

/**
 * Rule on one turn of the prospect's speech. This is the single decision that
 * governs whether the assistant is allowed to acknowledge anything or move on.
 *
 * The order matters: cheap structural disqualifications first, then
 * confidence, then meaning. A transcript that fails any of them is not an
 * answer, and UNKNOWN is the correct outcome — a repeat costs one sentence.
 */
export function adjudicateTurn(ev: TurnEvidence): TurnVerdict {
  if (ev.transcriptionFailed) {
    return notHeard("transcription_failed", ev.confidence, ev.transcript);
  }
  // A partial is never passed here; null means transcription did not finalize
  // anything for this turn within the window. That is not an answer.
  if (ev.transcript === null || ev.transcript === undefined) {
    return notHeard("no_transcript", ev.confidence, null);
  }

  const raw = ev.transcript;
  if (raw.trim() === "") return notHeard("empty", ev.confidence, raw);
  if (isNoiseOnly(raw)) return notHeard("noise_only", ev.confidence, raw);
  if (isBackchannelOnly(raw)) return notHeard("backchannel_only", ev.confidence, raw);
  if (isFillerOnly(raw)) return notHeard("filler_only", ev.confidence, raw);
  if (isClipped(raw)) return notHeard("clipped", ev.confidence, raw);
  if (isNonAnswerUtterance(raw)) return notHeard("non_answer", ev.confidence, raw);

  if (contentTokens(raw).length === 0) {
    return notHeard("filler_only", ev.confidence, raw);
  }

  // A real score below threshold means the transcriber was guessing at words
  // it did hear. That is NOT the same as hearing nothing: there is a candidate
  // to put back to the prospect, and one "is that right?" is a far better
  // recovery than making them say the whole thing again.
  //
  // An ABSENT score (null) is unscored, not confident — it passes here and is
  // judged on content, because whisper-family models return no logprobs at all
  // and gating on a missing field would gate every call made with one.
  if (typeof ev.confidence === "number" && ev.confidence < ev.confidenceMin) {
    return {
      outcome: "heard_unclear",
      notHeardReason: "low_confidence",
      reason: REASON_TEXT.low_confidence,
      confidence: ev.confidence,
      transcript: raw,
    };
  }

  if (answersExpectation(raw, ev.expectation)) {
    return {
      outcome: "heard_answer",
      notHeardReason: null,
      reason: "the prospect answered the question that was asked",
      confidence: ev.confidence,
      transcript: raw,
    };
  }

  return {
    outcome: "heard_other",
    notHeardReason: null,
    reason:
      "the prospect said something, but it does not answer the question that was asked",
    confidence: ev.confidence,
    transcript: raw,
  };
}

// ── What the assistant is told to say next ──────────────────────────────────

/**
 * The clarification the assistant must speak when a turn produced no answer.
 * Phrased for the specific failure so it sounds like a person who was
 * listening — "was that a yes or a no?" after an ambiguous grunt on a yes/no
 * question, not a generic "could you repeat that".
 */
export function clarificationLine(
  reason: NotHeardReason,
  expectation: QuestionExpectation
): string {
  if (expectation === "yes_no" && reason === "backchannel_only") {
    return "Sorry — was that a yes or a no?";
  }
  // Never "was that a yes or a no?" here: the question had no yes and no no in
  // it, and asking for one is how the call gets stuck re-asking a choice the
  // prospect has already made.
  if (expectation === "choice") {
    return "Sorry — which of those would you prefer?";
  }
  switch (reason) {
    case "clipped":
      return "I think we talked over each other there — what was that?";
    case "non_answer":
      return "Of course — let me say that again.";
    case "noise_only":
    case "no_transcript":
    case "transcription_failed":
    case "empty":
      return "Sorry, I didn't catch that.";
    case "filler_only":
    case "backchannel_only":
    case "low_confidence":
    default:
      return "Could you say that one more time?";
  }
}

/**
 * The per-response instruction for a turn where nothing was heard.
 *
 * This REPLACES the session instructions for this one response, which is the
 * point: on a turn with no answer there is exactly one valid utterance, and
 * the sales script is what pulls the model toward producing a different one.
 * The persona line is carried over so the clarification still sounds like her.
 */
export function notHeardInstructions(opts: {
  agentDisplayName: string;
  reason: NotHeardReason;
  reasonText: string;
  expectation: QuestionExpectation;
  pendingQuestion: string | null;
  consecutiveFailures: number;
  escalateAfter: number;
}): string {
  const suggested = clarificationLine(opts.reason, opts.expectation);
  const escalate = opts.consecutiveFailures >= opts.escalateAfter;

  const lines = [
    `You are ${opts.agentDisplayName}: a warm, dry, economical English woman. Stay in character.`,
    "",
    "YOU DID NOT GET AN ANSWER TO YOUR LAST QUESTION.",
    `Specifically: ${opts.reasonText}.`,
    "",
    opts.pendingQuestion
      ? `The question still outstanding is: "${opts.pendingQuestion}"`
      : "Your last question is still outstanding.",
    "",
    "You do not have their answer. You must not behave as though you do:",
    '  - Do NOT say "Perfect", "Got it", "Great", "Thanks", "That helps", or any',
    "    other acknowledgement. There is nothing to acknowledge.",
    "  - Do NOT move on to the next question.",
    "  - Do NOT guess, infer, or fill in what they probably meant.",
    "  - Do NOT call update_lead_field. Nothing was heard, so nothing is recorded.",
    "",
    escalate
      ? "This has now happened several turns in a row, so name the problem plainly " +
        "and warmly — something like: \"I'm sorry, the line is being difficult on my " +
        "end. Could I get that one more time?\" Then stop and listen."
      : `Say ONE short, natural line asking them to repeat, then STOP and listen. ` +
        `Something like: "${suggested}"`,
    "",
    "Say exactly one thing. Do not add a second question. Do not continue into",
    "anything you were planning to say next. Then stop talking and wait.",
  ];
  return lines.filter((l) => l !== undefined).join("\n");
}

/**
 * The per-response instruction when a question has used its whole budget.
 *
 * Asked once, clarified once, still nothing usable. The question is over. This
 * is the instruction that makes the loop terminate: without it every ruling
 * says "ask again", and "ask again" is always locally correct — the answer
 * genuinely was not heard — so the call never leaves the question. A traced
 * call produced five clarifications in a row on this path.
 *
 * Skipping is the cheap option and it is deliberately framed as normal rather
 * than as a failure, because the model will otherwise try to rescue the slot.
 * An UNKNOWN slot costs a licensed agent one question at the appointment. A
 * third, fourth and fifth "sorry, could you say that again?" costs the call.
 */
export function skipQuestionInstructions(opts: {
  agentDisplayName: string;
  pendingQuestion: string | null;
  attempts: number;
}): string {
  return [
    `You are ${opts.agentDisplayName}: a warm, dry, economical English woman. Stay in character.`,
    "",
    `You have now asked this ${opts.attempts} times and still have no usable answer:`,
    opts.pendingQuestion ? `  "${opts.pendingQuestion}"` : "  (your last question)",
    "",
    "STOP ASKING IT. Asking a third time is worse than not knowing — it is the",
    "point where the prospect concludes you cannot hear them and gives up on",
    "the call. This question is now closed and the slot stays unknown, which is",
    "a perfectly acceptable outcome: the licensed agent confirms it later.",
    "",
    "  - Do NOT ask it again, in any wording.",
    "  - Do NOT record a value for it. You did not hear one.",
    "  - Do NOT apologise at length or explain what went wrong.",
    "",
    "Move on. Let it go lightly — something like \"No matter, we can sort that",
    "one out later\" — and then ask the NEXT item on your agenda. One short",
    "sentence to move past it, then the next question, then stop and listen.",
  ].join("\n");
}

/**
 * The per-response instruction when several questions in a row have been
 * abandoned. At that point the problem is not the question, it is the line, and
 * continuing to work the agenda is a waste of the prospect's time.
 */
export function escalateInstructions(opts: {
  agentDisplayName: string;
  skipped: number;
}): string {
  return [
    `You are ${opts.agentDisplayName}: a warm, dry, economical English woman. Stay in character.`,
    "",
    `${opts.skipped} questions in a row have produced nothing usable. This is not`,
    "the prospect being difficult and it is not one bad question — the audio",
    "between you is not working well enough to run this call.",
    "",
    "Stop the discovery entirely. Do NOT ask another agenda question.",
    "",
    "Name it plainly and warmly, once, and offer the way out: a licensed agent",
    "calling them back on a better line. Something like: \"I'm sorry — the line",
    "between us really isn't playing ball today. Rather than make you repeat",
    "yourself, let me have one of our licensed agents call you back properly.\"",
    "",
    "Then act on what they say: book the callback, transfer them, or end the",
    "call politely. Do not return to the agenda.",
  ].join("\n");
}

/**
 * The per-response instruction for a turn where words arrived but recognition
 * was too poor to trust them.
 *
 * The difference from notHeardInstructions is the recovery, and it matters on
 * a real call: there IS a candidate answer here, so putting it back to the
 * prospect costs them one word ("yes") instead of the whole sentence again.
 * What is identical is the prohibition — the slot is HEARD, not CONFIRMED, so
 * nothing may be acknowledged, recorded, or advanced past.
 */
export function unclearInstructions(opts: {
  agentDisplayName: string;
  expectation: QuestionExpectation;
  pendingQuestion: string | null;
}): string {
  const suggested =
    opts.expectation === "yes_no"
      ? '"Sorry — was that a yes or a no?"'
      : '"Just to make sure I\'ve got this right — [what you think they said], is that correct?"';

  return [
    `You are ${opts.agentDisplayName}: a warm, dry, economical English woman. Stay in character.`,
    "",
    "YOU HEARD SOMETHING, BUT NOT CLEARLY ENOUGH TO TRUST IT.",
    "The line was poor and the words came through badly recognized.",
    opts.pendingQuestion ? `You had asked: "${opts.pendingQuestion}"` : "",
    "",
    "You do not yet have a usable answer, so you must not act as though you do:",
    '  - Do NOT say "Perfect", "Got it", "Great", "Thanks", or any other',
    "    acknowledgement. Nothing is settled yet.",
    "  - Do NOT move on to the next question.",
    "  - Do NOT record it with update_lead_field yet.",
    "",
    "Read back what you think they said and get a plain yes. Something like:",
    `  ${suggested}`,
    "",
    "Then STOP and listen. Say exactly one thing — no second question, no",
    "continuing into whatever you were going to say next. If they confirm it,",
    "record it with update_lead_field and confirmed=true. If they correct you,",
    "record the corrected value.",
  ]
    .filter((l) => l !== "")
    .join("\n");
}
