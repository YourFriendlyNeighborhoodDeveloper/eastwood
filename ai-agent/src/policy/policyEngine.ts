import type { PolicyDecision, Quote } from "../types.js";

const MISLEADING_GUARANTEE_RE =
  /\b(guaranteed|guarantee|no\s+questions?\s+asked|cannot\s+be\s+denied|always\s+approved)\b/i;

const MEDICARE_GOVT_RE =
  /\b(government\s+(program|benefit)|medicare\s+is\s+free|free\s+medicare|federally\s+funded\s+benefit)\b/i;

const PII_READBACK_RE =
  /\b(\d{3}-?\d{2}-?\d{4}|SSN|social\s+security\s+number|bank\s+account|routing\s+number)\b/i;

const PREMIUM_PER_1K_FACE_BAND: Record<string, [number, number]> = {
  final_expense: [3, 35],
  life: [0.5, 10],
  annuity: [0, 0],
};

export class PolicyEngine {
  evaluateUtterance(text: string): PolicyDecision {
    if (MISLEADING_GUARANTEE_RE.test(text)) {
      return {
        allowed: false,
        reason: "misleading_guarantee",
        severity: "block",
      };
    }

    if (MEDICARE_GOVT_RE.test(text)) {
      return {
        allowed: false,
        reason: "cms_soa_violation",
        severity: "block",
      };
    }

    if (PII_READBACK_RE.test(text)) {
      return {
        allowed: false,
        reason: "pii_readback",
        severity: "block",
      };
    }

    return { allowed: true, reason: "clean", severity: "info" };
  }

  evaluateAction(action: string, state: string): PolicyDecision {
    if (action === "get_quote" && state === "greeting") {
      return {
        allowed: false,
        reason: "quote_before_discovery",
        severity: "warn",
      };
    }

    return { allowed: true, reason: "clean", severity: "info" };
  }

  evaluateQuote(quote: Quote): PolicyDecision {
    const band = PREMIUM_PER_1K_FACE_BAND[quote.productLine];
    if (band && quote.faceValue && quote.faceValue > 0) {
      const [minPer1k, maxPer1k] = band;
      const per1k = (quote.monthlyPremium / quote.faceValue) * 1000;
      if (per1k < minPer1k || per1k > maxPer1k) {
        return {
          allowed: false,
          reason: "quote_outside_sanity_band",
          severity: "block",
        };
      }
    }

    return { allowed: true, reason: "clean", severity: "info" };
  }
}
