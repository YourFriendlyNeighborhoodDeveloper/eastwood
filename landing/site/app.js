const form = document.getElementById("riskForm");
const statusEl = document.getElementById("status");
const calcBtn = document.getElementById("calcBtn");
const submitBtn = document.getElementById("submitBtn");
const disclosureEl = document.getElementById("disclosure");

const step2 = document.getElementById("step2");
const step3 = document.getElementById("step3");
const resultBox = document.getElementById("resultBox");
const goBookBtn = document.getElementById("goBookBtn");

const slotsEl = document.getElementById("slots");
const bookStatusEl = document.getElementById("bookStatus");

let bookingToken = null;
let bookingUrl = null;
let leadId = null;
let bookingTz = null;

function normalizePhone(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (s.startsWith("+")) return s;
  const digits = s.replace(/[^\d]/g, "");
  if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && !digits.startsWith("1")) return `+1${digits.slice(0, 10)}`;
  return s;
}

function nowIso() {
  return new Date().toISOString();
}

function setStatus(msg, ok = true) {
  statusEl.textContent = msg;
  statusEl.className = `status ${ok ? "ok" : "bad"}`;
}

function setBookStatus(msg, ok = true) {
  bookStatusEl.textContent = msg;
  bookStatusEl.className = `status ${ok ? "ok" : "bad"}`;
}

function show(el) {
  el.classList.remove("hidden");
}

function calcScore(fd) {
  const age = parseInt(fd.get("age") || "0", 10) || 0;
  const smoker = String(fd.get("smoker") || "no") === "yes";
  const target = parseInt(fd.get("coverage_target") || "0", 10) || 0;
  const timeline = String(fd.get("timeline") || "");
  let score = 60;

  if (timeline === "today") score += 15;
  if (timeline === "7d") score += 10;
  if (timeline === "30d") score += 5;

  if (target >= 250000) score += 15;
  if (target >= 500000) score += 5;

  if (age >= 25 && age <= 55) score += 5;
  if (smoker) score -= 5;

  if (score < 0) score = 0;
  if (score > 100) score = 100;

  const gap = Math.max(0, target - Math.floor(target * (score / 100)));
  return { score, gap };
}

async function loadSlots() {
  if (!bookingToken) return;
  slotsEl.innerHTML = "";
  setBookStatus("Loading slots...");

  const url = `/api/booking/slots?token=${encodeURIComponent(bookingToken)}&days=7`;
  const res = await fetch(url);
  const txt = await res.text();
  let data = null;
  try { data = JSON.parse(txt); } catch {}
  if (!res.ok) {
    setBookStatus(`Slots error ${res.status}: ${data?.detail || txt || "failed"}`, false);
    return;
  }

  bookingTz = data.tz;
  const slots = Array.isArray(data.slots) ? data.slots : [];
  if (!slots.length) {
    setBookStatus("No slots available. Try later.", false);
    return;
  }

  setBookStatus(`Showing ${slots.length} slots (${bookingTz}).`, true);

  for (const s of slots.slice(0, 40)) {
    const b = document.createElement("button");
    b.className = "slot";
    b.type = "button";
    b.textContent = new Date(s).toLocaleString();
    b.addEventListener("click", () => bookSlot(s));
    slotsEl.appendChild(b);
  }
}

async function bookSlot(startAtIso) {
  if (!bookingToken) return;
  setBookStatus("Booking...");
  const res = await fetch("/api/booking/book", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: bookingToken, start_at: startAtIso })
  });

  const txt = await res.text();
  let data = null;
  try { data = JSON.parse(txt); } catch {}
  if (!res.ok) {
    setBookStatus(`Booking error ${res.status}: ${data?.detail || txt || "failed"}`, false);
    return;
  }

  setBookStatus(`Booked ✅ ${new Date(data.start_at).toLocaleString()} (${data.tz}). Check your SMS.`, true);
}

calcBtn.addEventListener("click", () => {
  const fd = new FormData(form);
  const { score, gap } = calcScore(fd);
  resultBox.textContent = `Score: ${score}/100. Coverage gap estimate: $${gap.toLocaleString()}.`;
  show(step2);
});

goBookBtn.addEventListener("click", async () => {
  show(step3);
  await loadSlots();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  submitBtn.disabled = true;
  setStatus("Submitting...");

  const fd = new FormData(form);
  const product = String(fd.get("product") || "").trim();
  const phone = normalizePhone(fd.get("phone"));
  const email = String(fd.get("email") || "").trim();
  const state = String(fd.get("state") || "").trim().toUpperCase();
  const zip = String(fd.get("zip") || "").trim();

  const payload = {
    product,
    phone,
    email: email || null,
    state,
    zip,
    age: parseInt(fd.get("age") || "0", 10) || null,
    smoker: String(fd.get("smoker") || "no") === "yes",
    coverage_target: parseInt(fd.get("coverage_target") || "0", 10) || null,
    timeline: String(fd.get("timeline") || ""),
    created_at: nowIso(),
    consent_ts: nowIso(),
    form_url: window.location.href,
    disclosure: disclosureEl ? disclosureEl.textContent.trim() : "",
    ip: null,
    user_agent: navigator.userAgent || null
  };

  try {
    const res = await fetch("/api/leads/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "risk_calc", payload })
    });

    const txt = await res.text();
    let data = null;
    try { data = JSON.parse(txt); } catch {}

    if (!res.ok) {
      setStatus(`Error ${res.status}: ${data?.detail || txt || "request failed"}`, false);
      return;
    }

    bookingToken = data.booking_token;
    bookingUrl = data.booking_url;
    leadId = data.lead_id;

    const { score, gap } = calcScore(fd);
    resultBox.textContent = `Score: ${score}/100. Coverage gap estimate: $${gap.toLocaleString()}. Booking link ready.`;
    show(step2);
    show(step3);

    setStatus(`Accepted ✅ lead_id=${leadId}`, true);
    await loadSlots();
  } catch (err) {
    setStatus(`Network error: ${String(err)}`, false);
  } finally {
    submitBtn.disabled = false;
  }
});

(function bootFromToken() {
  const u = new URL(window.location.href);
  const token = u.searchParams.get("token");
  if (token) {
    bookingToken = token;
    show(step3);
    loadSlots();
  }
})();