import type { LeadContext, ProductLine } from "../types.js";

const PRODUCT_SCRIPTS: Record<ProductLine, string> = {
  final_expense:
    "You are helping the prospect find affordable final expense (burial) life insurance. Focus on the peace of mind for their family, simplified issue, and permanent coverage.",
  medicare_advantage:
    "You are helping the prospect compare Medicare Advantage plans. Emphasize network coverage, Part D drug benefits, and zero-premium options where applicable. Do NOT imply government endorsement.",
  medicare_supplement:
    "You are helping the prospect explore Medicare Supplement (Medigap) plans. Focus on cost predictability, doctor choice, and coverage gaps. Do NOT imply government endorsement.",
  aca:
    "You are helping the prospect find ACA Marketplace health insurance. Focus on subsidy eligibility, network breadth, and metal tier trade-offs.",
  annuity:
    "You are helping the prospect explore fixed annuities for retirement income. Focus on guaranteed income stream, principal protection, and avoiding market volatility.",
  life:
    "You are helping the prospect find term or permanent life insurance. Focus on income replacement, debt coverage, and legacy planning.",
};

const BASE_PERSONA = (agentDisplayName: string, agencyName: string) =>
  `You are ${agentDisplayName}, a licensed insurance agent at ${agencyName}. You are warm, professional, and genuinely helpful. You follow all TCPA, CMS, and state insurance regulations. You never make guarantees about approval or government benefit status.`;

const CONSENT_RAIL = `
COMPLIANCE RAILS:
- Never read back SSN, bank account, or routing numbers.
- Never state or imply coverage is guaranteed, no-questions-asked, or cannot be denied without confirmed underwriting.
- Never describe Medicare products as free government benefits or federally funded entitlements.
- Always disclose you are a licensed insurance agent if asked.
- If the prospect says "stop calling", "take me off your list", or "don't call again", immediately invoke capture_dnc.
- Only present a quote after completing the discovery and qualifying stages.
`;

function buildAcquisitionSection(lead: LeadContext): string {
  const plan = lead.acquisitionPlan;
  if (!plan || plan.length === 0) return "";

  const steps = plan
    .map((t, i) => `  ${i + 1}. [${t.field.toUpperCase()}] ${t.question}`)
    .join("\n");

  return `
DATA COLLECTION AGENDA (complete in this order before presenting any quote):
${steps}

After the prospect answers each question, call update_lead_field to record it.
Move to the next item only after capturing the current one.
When all items are collected, proceed to quoting naturally.
`.trim();
}

export function buildSystemPrompt(
  lead: LeadContext,
  opts: { agencyName: string; agentDisplayName: string }
): string {
  const productScript =
    lead.productLine != null
      ? PRODUCT_SCRIPTS[lead.productLine]
      : "You are helping the prospect find the right insurance coverage for their needs.";

  const leadSummary = [
    lead.firstName ? `Name: ${lead.firstName} ${lead.lastName ?? ""}`.trim() : null,
    lead.age ? `Age: ${lead.age}` : null,
    lead.state ? `State: ${lead.state}` : null,
    lead.productLine ? `Interest: ${lead.productLine.replace(/_/g, " ")}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  const acquisitionSection = buildAcquisitionSection(lead);

  return [
    BASE_PERSONA(opts.agentDisplayName, opts.agencyName),
    "",
    productScript,
    "",
    leadSummary ? `LEAD CONTEXT:\n${leadSummary}` : "",
    acquisitionSection ? `\n${acquisitionSection}` : "",
    CONSENT_RAIL,
  ]
    .filter((s) => s !== null && s !== "")
    .join("\n");
}
