export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export const ALL_TOOLS: ToolDefinition[] = [
  {
    name: "get_quote",
    description:
      "Generate the real quote options for the prospect. Returns up to three " +
      "engine-priced options (C, B, A) sized from what the prospect stated. " +
      "MANDATORY once discovery and health screening are complete — never skip " +
      "to booking while quoting is possible. Rating sex is read from the " +
      "captured rating_sex field; it is never passed in or inferred.",
    parameters: {
      type: "object",
      required: ["productLine", "age", "state"],
      properties: {
        productLine: {
          type: "string",
          enum: [
            "final_expense",
            "medicare_advantage",
            "medicare_supplement",
            "aca",
            "annuity",
            "life",
          ],
        },
        age: { type: "integer", minimum: 18, maximum: 99 },
        state: { type: "string", minLength: 2, maxLength: 2 },
        faceValue: { type: "integer", minimum: 1000 },
        tobaccoUser: { type: "boolean" },
      },
    },
  },
  {
    name: "select_quote_option",
    description:
      "Record which of the presented options (C, B, or A) the prospect chose, " +
      "after asking which one makes the most sense for them. Set " +
      "affordabilityConfirmed true only after they verbally confirm the monthly " +
      "amount is comfortable to maintain long term.",
    parameters: {
      type: "object",
      required: ["option"],
      properties: {
        option: { type: "string", enum: ["C", "B", "A"] },
        affordabilityConfirmed: {
          type: "boolean",
          description: "True only after the prospect confirms the premium is comfortable long term",
        },
      },
    },
  },
  {
    name: "get_booking_slots",
    description:
      "Retrieve real upcoming appointment times from the calendar BEFORE " +
      "offering any time to the prospect. Offer two or three of the returned " +
      "times and let them pick. Never invent an available time.",
    parameters: {
      type: "object",
      properties: {
        timezone: { type: "string", description: "Prospect's IANA timezone, e.g. 'America/Detroit'" },
      },
    },
  },
  {
    name: "book_appointment",
    description:
      "Book the licensed-agent appointment. Only after a quote was presented " +
      "(or quoting genuinely failed). When the prospect picked one of the real " +
      "times from get_booking_slots, pass it as slotStartTime along with their " +
      "email (collect it first — the calendar invite needs it). notes carries " +
      "anything the prospect asked the agent to know before the call.",
    parameters: {
      type: "object",
      properties: {
        slotStartTime: {
          type: "string",
          description: "ISO start time chosen from get_booking_slots results",
        },
        email: { type: "string", description: "Prospect's email address, as they stated it" },
        preferredDate: { type: "string", format: "date" },
        preferredTime: { type: "string" },
        timezone: { type: "string", description: "Prospect's timezone, e.g. 'ET', 'America/Detroit'" },
        notes: { type: "string" },
      },
    },
  },
  {
    name: "capture_objection",
    description: "Record a prospect objection for follow-up handling.",
    parameters: {
      type: "object",
      required: ["objectionType", "text"],
      properties: {
        objectionType: {
          type: "string",
          enum: [
            "price",
            "timing",
            "trust",
            "need",
            "competitor",
            "spouse",
            "health",
            "other",
          ],
        },
        text: { type: "string" },
      },
    },
  },
  {
    name: "capture_consent",
    description:
      "Record prospect consent. Use the correct consentType: contact_consent (ok to call/contact), " +
      "recording_consent (ok to record the call), sms_consent (ok to send SMS), " +
      "application_consent (agrees to submit application), health_disclosure_ack (authorises sharing health info). " +
      "Only call after the prospect verbally agrees to that specific consent.",
    parameters: {
      type: "object",
      required: ["consentType", "consentText"],
      properties: {
        consentType: {
          type: "string",
          enum: [
            "contact_consent",
            "recording_consent",
            "sms_consent",
            "application_consent",
            "health_disclosure_ack",
          ],
        },
        consentText: { type: "string", description: "Verbatim words the prospect used to agree" },
        timestamp: { type: "string", format: "date-time" },
      },
    },
  },
  {
    name: "capture_dnc",
    description:
      "Add prospect to the Do Not Call list immediately when they request it.",
    parameters: {
      type: "object",
      required: ["phoneE164"],
      properties: {
        phoneE164: { type: "string" },
        reason: { type: "string" },
      },
    },
  },
  {
    name: "transfer_to_human",
    description: "Transfer the call to a human agent.",
    parameters: {
      type: "object",
      properties: {
        reason: { type: "string" },
        priority: { type: "string", enum: ["normal", "urgent"] },
      },
    },
  },
  {
    name: "transfer_live",
    description:
      "Request an immediate live bridge to a human closer when the prospect is " +
      "qualified, has budget, has given health info, and is ready to move forward now. " +
      "Do NOT use for scheduled appointments — use book_appointment for those. " +
      "This is for hot intent that will decay if not handled immediately.",
    parameters: {
      type: "object",
      properties: {
        reason: {
          type: "string",
          description: "Why this prospect is hot right now",
        },
        topCarrier: {
          type: "string",
          description: "Best placement carrier identified",
        },
        topTier: {
          type: "string",
          enum: ["level", "graded", "modified", "gi"],
        },
        quoteAmount: {
          type: "number",
          description: "Monthly premium estimate discussed on the call",
        },
      },
    },
  },
  {
    name: "end_call",
    description:
      "End the call gracefully. ALWAYS provide notes: a 2-4 sentence factual " +
      "recap of everything the prospect actually said — especially any monthly " +
      "price they want to stay under (even approximate, e.g. 'wants to stay " +
      "under $50/mo'), coverage goals, health facts, objections, and the next " +
      "step they agreed to. State only what was said; never invent.",
    parameters: {
      type: "object",
      properties: {
        outcome: {
          type: "string",
          enum: ["sold", "appointment_set", "not_interested", "callback", "dnc", "other"],
        },
        notes: {
          type: "string",
          description:
            "Factual recap of the conversation: stated budget ceiling, coverage " +
            "goals, health facts, objections, agreed next step.",
        },
      },
      required: ["notes"],
    },
  },
  {
    name: "add_followup_questions",
    description:
      "Call this immediately after the prospect confirms a health condition during the screening question. " +
      "It loads the condition-specific follow-up questions into your agenda so you can ask them in order. " +
      "Only call after the prospect has explicitly named the condition — never assume or pre-load.",
    parameters: {
      type: "object",
      required: ["condition"],
      properties: {
        condition: {
          type: "string",
          enum: ["cancer", "heart_attack", "stroke", "diabetes", "copd", "kidney_disease"],
          description: "The condition the prospect just explicitly confirmed.",
        },
      },
    },
  },
  {
    name: "update_lead_field",
    description:
      "Record a field captured from the prospect during the data collection agenda. " +
      "Call this immediately after the prospect answers each question.",
    parameters: {
      type: "object",
      required: ["field", "captureKey", "value"],
      properties: {
        field: {
          type: "string",
          description: "The agenda field name (e.g. 'age', 'tobacco', 'health', 'beneficiary', 'budget')",
        },
        captureKey: {
          type: "string",
          description: "The storage key from the agenda task",
        },
        value: {
          description: "The captured value. Use the valueHint from the agenda for format guidance.",
        },
        rawText: {
          type: "string",
          description: "The prospect's verbatim response, for audit purposes",
        },
      },
    },
  },
];

export function filterToolsByAllowed(allowedNames: string[]): ToolDefinition[] {
  const set = new Set(allowedNames);
  return ALL_TOOLS.filter((t) => set.has(t.name));
}
