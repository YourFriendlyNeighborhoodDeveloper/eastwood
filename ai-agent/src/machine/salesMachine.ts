import { assign, createMachine, setup } from "xstate";
import type { LeadContext, Quote } from "../types.js";

export type SalesState =
  | "greeting"
  | "discovery"
  | "qualifying"
  | "quoting"
  | "objection_handling"
  | "closing"
  | "wrap_up"
  | "transferring"
  | "terminating"
  | "done";

export const ALLOWED_TOOLS: Record<SalesState, string[]> = {
  greeting: ["capture_dnc", "update_lead_field"],
  discovery: ["capture_dnc", "capture_objection", "update_lead_field", "add_followup_questions"],
  qualifying: ["capture_dnc", "capture_objection", "update_lead_field", "add_followup_questions"],
  quoting: ["get_quote", "capture_dnc", "capture_objection", "update_lead_field"],
  objection_handling: ["capture_objection", "capture_dnc", "get_quote"],
  closing: ["book_appointment", "transfer_live", "capture_consent", "capture_dnc", "capture_objection"],
  wrap_up: ["end_call", "transfer_to_human", "capture_dnc"],
  transferring: ["transfer_to_human"],
  terminating: ["end_call", "capture_dnc"],
  done: [],
};

export interface SalesContext {
  lead: LeadContext;
  quote: Quote | null;
  hasDnc: boolean;
  turnCount: number;
}

export type SalesEvent =
  | { type: "ADVANCE" }
  | { type: "QUOTE_READY"; quote: Quote }
  | { type: "OBJECTION" }
  | { type: "CLOSE" }
  | { type: "DNC" }
  | { type: "TRANSFER" }
  | { type: "WRAP_UP" }
  | { type: "END" };

export const salesMachine = setup({
  types: {
    context: {} as SalesContext,
    events: {} as SalesEvent,
    input: {} as { lead: LeadContext },
  },
  guards: {
    isDncTrigger: ({ context }) => context.hasDnc,
    hasQuote: ({ context }) => context.quote !== null,
    consentSafe: ({ context }) =>
      context.lead.consentStatus === "express_written",
  },
  actions: {
    incrementTurn: assign({
      turnCount: ({ context }) => context.turnCount + 1,
    }),
    setQuote: assign({
      quote: ({ event }) =>
        event.type === "QUOTE_READY" ? event.quote : null,
    }),
    setDnc: assign({ hasDnc: () => true }),
  },
}).createMachine({
  id: "salesMachine",
  initial: "greeting",
  context: ({ input }) => ({
    lead: input.lead,
    quote: null,
    hasDnc: false,
    turnCount: 0,
  }),
  states: {
    greeting: {
      on: {
        ADVANCE: "discovery",
        DNC: { actions: "setDnc", target: "terminating" },
      },
    },
    discovery: {
      on: {
        ADVANCE: "qualifying",
        DNC: { actions: "setDnc", target: "terminating" },
        TRANSFER: "transferring",
      },
    },
    qualifying: {
      on: {
        ADVANCE: "quoting",
        OBJECTION: "objection_handling",
        DNC: { actions: "setDnc", target: "terminating" },
        TRANSFER: "transferring",
      },
    },
    quoting: {
      on: {
        QUOTE_READY: { actions: "setQuote", target: "closing" },
        OBJECTION: "objection_handling",
        DNC: { actions: "setDnc", target: "terminating" },
        TRANSFER: "transferring",
      },
    },
    objection_handling: {
      on: {
        ADVANCE: "quoting",
        DNC: { actions: "setDnc", target: "terminating" },
        TRANSFER: "transferring",
        CLOSE: "closing",
      },
    },
    closing: {
      on: {
        WRAP_UP: "wrap_up",
        DNC: { actions: "setDnc", target: "terminating" },
        TRANSFER: "transferring",
      },
    },
    wrap_up: {
      on: {
        END: "done",
        TRANSFER: "transferring",
      },
    },
    transferring: {
      on: {
        END: "done",
      },
    },
    terminating: {
      on: {
        END: "done",
      },
    },
    done: {
      type: "final",
    },
  },
});
