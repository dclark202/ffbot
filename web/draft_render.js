// Rendering functions shared between the live draft room (draft.html) and
// the offline training-pack reviewer (train.html). Both pages render the
// SAME frozen shape -- `webapi.draft_state_json()` -- so the DOM these
// functions build (recommendation table, roster, draft log, opponents,
// confidence pill) must not fork between "watching a real draft" and
// "reviewing a synthetic one". Extracted out of draft.html rather than
// duplicated, the same reuse discipline the rest of this repo follows.
//
// Depends on `el`/`fmt` from common.js, loaded before this file on both
// pages. Plain globals (no module system) -- both pages just add a second
// <script src> tag.

const SORT_LABELS = { value: "Val", vor: "VOR", adp: "ADP", urgency: "Surv", upside: "Up", edge: "Edge" };

// The P column: the Δ column's raw gap re-expressed as "how likely is this
// actually the right pick" (see ffbot/edge.py). The bar is scaled against a
// FIXED full width of 1.0, never against max(p) -- scaling to the max would
// draw a uniform, genuine-toss-up field as twenty full bars and destroy the
// exact distinction this column exists to make. The faint tick sits at 1/n,
// so "every bar at the tick" reads instantly as a toss-up and "one bar
// towering over it" as a standout.
function pBestCell(p, uniformP, blind) {
  const cellClass = blind ? "pbest-col blind-col" : "pbest-col";
  if (p === null || p === undefined) return el("td", { class: cellClass, text: "-" });
  const wrap = el("div", { class: "pbar-wrap" });
  wrap.appendChild(el("div", { class: "pbar", style: `width:${Math.max(1, p * 100)}%` }));
  if (uniformP) {
    wrap.appendChild(el("div", { class: "pbar-tick", style: `left:${uniformP * 100}%` }));
  }
  const cell = el("td", { class: cellClass });
  cell.appendChild(wrap);
  cell.appendChild(el("span", { class: "pbest-num", text: `${fmt(p * 100, 0)}%` }));
  return cell;
}

// "3.4 live options of 20" rather than "H/ln n = 0.61": perplexity is a
// sentence someone on the clock can act on, normalized entropy is not. The
// latter rides along in the tooltip for anyone who wants it.
function renderConfidencePill(conf) {
  const pill = document.getElementById("confidence-pill");
  if (!pill) return;
  if (!conf || !conf.effective_options || !conf.n) {
    pill.style.display = "none";
    return;
  }
  const eff = conf.effective_options;
  const scope = conf.filtered ? " (this position only)" : "";
  let verdict;
  if (eff <= 1.6) verdict = "clear standout";
  else if (eff <= 4) verdict = "a few real options";
  else verdict = "toss-up";
  pill.textContent = `${verdict}: ${fmt(eff, 1)} live options of ${conf.n}${scope}`;
  pill.title =
    `P(best pick) is a softmax of the Δ column at T = ${fmt(conf.scale, 1)} season points. ` +
    `Top row ${fmt((conf.top_p || 0) * 100, 0)}% (${conf.top_name}); ` +
    `normalized entropy ${fmt(conf.normalized_entropy, 2)} (1.00 = perfect toss-up). ` +
    `Display only — it never affects the ranking.`;
  pill.style.display = "";
}

function whyChildren(r) {
  const children = [];
  if (r.intel_note) children.push(el("div", { class: "intel-note", text: r.intel_note }));
  const flagText = r.flags && r.flags.length ? ` [${r.flags.join(", ")}]` : "";
  const rest = (r.why_parts || []).join("; ") + flagText;
  if (rest) children.push(el("div", { class: "why-rest", text: rest }));
  return children;
}

// `td(cls, text)` / `tdBlind(cls, text)` -- small local helpers so a falsy
// class never becomes a literal `class="undefined"` attribute (`el()`
// writes whatever key is present in the attrs object, so the key itself,
// not just its value, has to be conditional).
function _td(cls, text) {
  return cls ? el("td", { class: cls, text }) : el("td", { text });
}

// `opts.onRowClick`, when given, replaces the default "draft this player"
// click handler -- train.html uses it to mark rank-1/2/3 instead of
// recording a pick. `opts.rowClass(r)`, when given, sets the row's class.
//
// `opts.blind`, when true, adds a "blind-col" class to the Val/Δ/P/Why
// cells -- train.html pairs this with a CSS rule
// (`body.blind-pending .blind-col { visibility: hidden }`) so a reviewer
// can rank purely off name/position/team/bye/proj/VOR/Need/ADP/Surv/Up/Scor
// until they choose to reveal the engine's own numbers.
function recRow(r, opts) {
  opts = opts || {};
  const blindCls = opts.blind ? "blind-col" : "";
  const cells = [
    // `rank` is assigned server-side AFTER the sort is applied (see
    // webapi.draft_state_json), so it always reads 1..N down the page
    // rather than carrying a stale by-value position.
    el("td", { class: "rank-col", text: String(r.rank) }),
    el("td", { text: r.name }),
    el("td", { text: r.position }),
    el("td", { text: r.team }),
    el("td", { text: r.bye_week !== null ? String(r.bye_week) : "-" }),
    el("td", { text: fmt(r.proj) }),
    el("td", { text: fmt(r.vor) }),
    el("td", { text: fmt(r.need) }),
    _td(blindCls, fmt(r.value)),
  ];
  // Against the highest-Val row in the list, not against row 1: under a
  // non-value sort those are different players, and the opportunity cost of
  // a pick is what the best available option was worth, wherever it sorted.
  const delta = opts.bestValue !== undefined ? opts.bestValue - r.value : 0;
  cells.push(_td(blindCls, delta > 0.05 ? `-${fmt(delta)}` : "-"));
  cells.push(pBestCell(r.p_best, opts.uniformP, opts.blind));
  cells.push(
    el("td", { text: r.adp !== null ? fmt(r.adp, 0) : "-" }),
    el("td", { text: r.survival !== null && r.survival !== undefined ? `${fmt(r.survival * 100, 0)}%` : "-" }),
    el("td", { text: r.upside ? fmt(r.upside * 100, 0) : "-" }),
    el("td", { text: r.arbitrage ? fmt(r.arbitrage, 0) : "-" }),
    el("td", { text: r.scoring_edge ? fmt(r.scoring_edge, 0) : "-" }),
    el("td", { class: blindCls ? `why ${blindCls}` : "why" }, whyChildren(r)),
  );
  const attrs = {};
  if (opts.rowClass) attrs.class = opts.rowClass(r);
  attrs.onclick = opts.onRowClick ? () => opts.onRowClick(r) : () => sendPick(r.key);
  return el("tr", attrs, cells);
}

// The caption is the only place the survival target pick is named, so the
// Surv column is never an unlabelled percentage. `survival_to_pick` is my
// next turn STRICTLY after the current pick (see webapi's header) — the
// same number whether or not I'm on the clock, which is what lets the
// table keep one meaning in every draft state.
function turnCaption(h) {
  if (h.survival_to_pick === null || h.survival_to_pick === undefined) {
    return h.on_the_clock
      ? "This is your last pick — nothing left for Surv to measure against."
      : "You have no picks left.";
  }
  const away = h.survival_to_pick - h.pick;
  const gap = `pick ${h.survival_to_pick}, ${away} pick${away === 1 ? "" : "s"} away`;
  return h.on_the_clock
    ? `Surv = chance he lasts to your NEXT turn (${gap}) if you pass on him now.`
    : `Your next turn is ${gap}. Surv = chance he's still there when it arrives.`;
}

function renderRoster(roster) {
  const body = document.getElementById("roster-body");
  body.innerHTML = "";
  // Bye weeks are shown here so a collision is something you scan for
  // while deciding, not something you have to notice on your own.
  for (const p of roster) {
    body.appendChild(el("tr", {}, [
      el("td", { text: p.position }),
      el("td", { text: p.name }),
      el("td", { text: p.team || "-" }),
      el("td", { text: p.bye_week !== null && p.bye_week !== undefined ? String(p.bye_week) : "-" }),
      el("td", { text: fmt(p.proj, 0) }),
    ]));
  }
}

function renderDraftLog(log) {
  const body = document.getElementById("last-picks-body");
  body.innerHTML = "";
  for (const p of log || []) {
    const team = p.mine ? "YOU" : (p.slot !== null && p.slot !== undefined ? `Team ${p.slot}` : "Team ?");
    body.appendChild(el("tr", {}, [
      el("td", { text: String(p.round) }),
      el("td", { text: String(p.number) }),
      el("td", { text: team }),
      el("td", { text: p.position || "" }),
      el("td", { text: p.name }),
      el("td", { text: p.team || "" }),
    ]));
  }
}

let selectedOpponentSlot = null;

// "Opponents" excludes my own team on purpose -- I'm not an opponent of
// myself, and my roster already has its own panel above this one.
function renderOpponents(opponents, mySlot) {
  const others = (opponents || []).filter((o) => !o.is_me);
  const tabs = document.getElementById("opponents-tabs");
  tabs.innerHTML = "";
  if (selectedOpponentSlot === null || !others.some((o) => o.slot === selectedOpponentSlot)) {
    selectedOpponentSlot = others.length ? others[0].slot : null;
  }
  for (const o of others) {
    tabs.appendChild(el("button", {
      class: "tab-btn" + (o.slot === selectedOpponentSlot ? " active" : ""),
      onclick: () => { selectedOpponentSlot = o.slot; renderOpponents(opponents, mySlot); },
      text: `Team ${o.slot}`,
    }));
  }
  const body = document.getElementById("opponent-roster-body");
  body.innerHTML = "";
  const selected = others.find((o) => o.slot === selectedOpponentSlot);
  const unfilledLabel = document.getElementById("opponent-unfilled");
  if (!selected) { unfilledLabel.textContent = ""; return; }
  for (const p of selected.roster) {
    body.appendChild(el("tr", {}, [
      el("td", { text: p.position }),
      el("td", { text: p.name }),
      el("td", { text: p.team || "-" }),
      el("td", { text: p.bye_week !== null && p.bye_week !== undefined ? String(p.bye_week) : "-" }),
      el("td", { text: fmt(p.proj, 0) }),
    ]));
  }
  unfilledLabel.textContent = selected.unfilled.length
    ? `Still needs: ${selected.unfilled.join(", ")}`
    : "Starting lineup fully covered.";
}

function renderDemandLine(demand) {
  const target = document.getElementById("demand-line");
  const entries = Object.entries(demand || {}).filter(([, v]) => v > 0);
  if (!entries.length) { target.textContent = "No standout demand from the teams ahead of your next pick."; return; }
  entries.sort((a, b) => b[1] - a[1]);
  target.textContent = "Teams picking before your next turn still need: " +
    entries.map(([pos, v]) => `${pos} (${fmt(v * 100, 0)}%)`).join(", ");
}

function renderNeedsBetween(nb) {
  const entries = Object.entries(nb || {});
  const target = document.getElementById("needs-between");
  if (entries.length === 0) { target.textContent = ""; return; }
  entries.sort((a, b) => b[1] - a[1]);
  target.textContent = "Since your last pick: " + entries.map(([pos, n]) => `${n} ${pos}`).join(", ");
}
