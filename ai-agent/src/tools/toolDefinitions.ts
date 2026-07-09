export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export const ALL_TOOLS: ToolDefinition[] = [
  {
    name: "get_quote",
    description:
      "Generate an insurance quote for the prospect based on their details.",
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
        gender: { type: "string", enum: ["male", "female", "not_specified"] },
      },
    },
  },
  {
    name: "book_appointment",
    description: "Book a follow-up appointment for the prospect.",
    parameters: {
      type: "object",
      required: ["preferredDate", "preferredTime"],
      properties: {
        preferredDate: { type: "string", format: "date" },
        preferredTime: { type: "string" },
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
    description: "End the call gracefully.",
    parameters: {
      type: "object",
      properties: {
        outcome: {
          type: "string",
          enum: ["sold", "appointment_set", "not_interested", "callback", "dnc", "other"],
        },
        notes: { type: "string" },
      },
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
