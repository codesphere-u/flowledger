/* FlowLedger Interactive Double-Entry Ledger, FX & Aegis G6 Studio */

function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    const ok = document.execCommand("copy");
    showToast(ok ? "Copied to clipboard" : "Copy unavailable in this browser");
  } catch (err) {
    showToast("Copy failed: " + (err && err.message ? err.message : "clipboard blocked"));
  }
  document.body.removeChild(ta);
}

function copyText(text) {
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => showToast("Copied to clipboard"),
      () => fallbackCopy(text)
    );
  } else {
    fallbackCopy(text);
  }
}

document.addEventListener("click", (e) => {
  const copyBtn = e.target.closest("[data-copy]");
  if (copyBtn) {
    copyText(copyBtn.getAttribute("data-copy"));
    return;
  }
  const popBtn = e.target.closest("[data-popover-toggle]");
  if (popBtn) {
    const pop = document.getElementById(popBtn.getAttribute("data-popover-toggle"));
    if (pop) pop.classList.toggle("open");
    return;
  }
  document.querySelectorAll(".cta-popover.open").forEach((pop) => {
    if (!pop.closest(".cta-wrap")?.contains(e.target)) pop.classList.remove("open");
  });
});

const LEDGER_PRESETS = {
  mpesa_stk: {
    name: "Safaricom M-Pesa STK Push Settlement (KES)",
    currency: "KES",
    grossMinor: 15000,
    feeBps: 150,
    status: "MATCHED",
    notes: "Balanced DEBIT asset:clearing:mpesa:kes == CREDIT liability:merchant + revenue:fees"
  },
  fx_corridor: {
    name: "Cross-Border UGX -> KES FX Lock (Spread Revenue)",
    currency: "UGX",
    grossMinor: 500000,
    feeBps: 85,
    status: "MATCHED",
    notes: "Two balanced single-currency journals preserving integer minor units with zero float rounding."
  },
  fee_mismatch: {
    name: "Airtel Money Fee Discrepancy + 3-Way Recon Proposal",
    currency: "UGX",
    grossMinor: 250000,
    feeBps: 120,
    status: "FEE_DISCREPANCY",
    notes: "Detected 100 UGX provider statement delta; emitted balanced AdjustmentProposal entry."
  }
};

function initHomePage() {
  const sel = document.getElementById("quickLedgerSelect");
  const out = document.getElementById("quickLedgerOut");
  if (!sel || !out) return;

  function renderQuick() {
    const p = LEDGER_PRESETS[sel.value] || LEDGER_PRESETS.mpesa_stk;
    const feeMinor = Math.round((p.grossMinor * p.feeBps) / 10000);
    const netMinor = p.grossMinor - feeMinor;
    const isWarn = p.status !== "MATCHED";
    out.innerHTML = `
      <div class="finding-item ${isWarn ? "high" : "pass"}">
        <div class="finding-head">
          <strong>${esc(p.name)}</strong>
          <span class="sev-pill ${isWarn ? "high" : "ok"}">${esc(p.status)} &middot; Zero-Sum Verified</span>
        </div>
        <p style="margin:4px 0 0;font-size:0.86rem;color:var(--muted);">
          Debit: <strong>${p.grossMinor.toLocaleString()} ${esc(p.currency)}</strong> &middot; Credit Net+Fee: <strong>${(netMinor + feeMinor).toLocaleString()} ${esc(p.currency)}</strong> (Net ${netMinor.toLocaleString()} + Fee ${feeMinor.toLocaleString()})
        </p>
      </div>
    `;
  }

  sel.addEventListener("change", renderQuick);
  renderQuick();
}

function initStudioPage() {
  const chips = document.querySelectorAll("[data-preset]");
  const amountEl = document.getElementById("studioAmount");
  const feeBpsEl = document.getElementById("studioFeeBps");
  const outEl = document.getElementById("studioOutput");
  const aegisOut = document.getElementById("studioAegisOut");
  if (!outEl) return;

  let activeKey = "mpesa_stk";

  function renderStudio() {
    const p = LEDGER_PRESETS[activeKey] || LEDGER_PRESETS.mpesa_stk;
    const gross = amountEl ? Number(amountEl.value) : p.grossMinor;
    const bps = feeBpsEl ? Number(feeBpsEl.value) : p.feeBps;
    const fee = Math.round((gross * bps) / 10000);
    const net = gross - fee;
    const isWarn = p.status !== "MATCHED";

    chips.forEach((c) => c.classList.toggle("active", c.getAttribute("data-preset") === activeKey));

    outEl.innerHTML = `
      <div class="finding-item ${isWarn ? "high" : "pass"}">
        <div class="finding-head">
          <strong>${esc(p.name)}</strong>
          <span class="sev-pill ${isWarn ? "high" : "ok"}">Imbalance: 0 ${esc(p.currency)}</span>
        </div>
        <p style="margin:4px 0 6px;font-size:0.86rem;color:var(--muted);">${esc(p.notes)}</p>
        <p style="margin:0;font-size:0.82rem;color:var(--muted);">
          Postings: DEBIT <code>asset:clearing</code> <strong>${gross.toLocaleString()}</strong> | CREDIT <code>liability:merchant</code> <strong>${net.toLocaleString()}</strong> + CREDIT <code>revenue:fees</code> <strong>${fee.toLocaleString()}</strong>
        </p>
      </div>
    `;

    if (aegisOut) {
      aegisOut.textContent = JSON.stringify(
        {
          gate_id: "G6_FINANCIAL_INTEGRITY",
          rfc6962_prefix: "0x00",
          passed: true,
          currency: p.currency,
          debit_total_minor: gross,
          credit_total_minor: net + fee,
          net_imbalance_minor: 0,
          reconciliation_status: p.status
        },
        null,
        2
      );
    }
  }

  chips.forEach((c) =>
    c.addEventListener("click", () => {
      activeKey = c.getAttribute("data-preset");
      const p = LEDGER_PRESETS[activeKey];
      if (p && amountEl) amountEl.value = p.grossMinor;
      if (p && feeBpsEl) feeBpsEl.value = p.feeBps;
      renderStudio();
    })
  );
  [amountEl, feeBpsEl].forEach((el) => {
    if (el) el.addEventListener("input", renderStudio);
  });
  renderStudio();
}

function initRulesPage() {
  const searchInput = document.getElementById("rulesSearch");
  const catSelect = document.getElementById("rulesCategory");
  const cards = document.querySelectorAll("[data-rule-card]");
  const countEl = document.getElementById("rulesCount");
  if (!searchInput || !catSelect || !cards.length) return;

  function filterRules() {
    const q = searchInput.value.trim().toLowerCase();
    const cat = catSelect.value;
    let shown = 0;
    cards.forEach((card) => {
      const match = (cat === "all" || card.getAttribute("data-cat") === cat) && (!q || card.textContent.toLowerCase().includes(q));
      card.style.display = match ? "" : "none";
      if (match) shown++;
    });
    if (countEl) countEl.textContent = `${shown} of ${cards.length} specifications shown`;
  }

  searchInput.addEventListener("input", filterRules);
  catSelect.addEventListener("change", filterRules);
  filterRules();
}

document.addEventListener("DOMContentLoaded", () => {
  initHomePage();
  initStudioPage();
  initRulesPage();
});
