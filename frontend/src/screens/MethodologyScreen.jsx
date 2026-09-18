/* ============================================================
   Screen: How it works — a plain-English methodology + a click-by-click
   walk-through. Editorial "field guide" style; RED = you / your answers
   throughout (the human always wins).
   ============================================================ */

import { Link } from "react-router-dom";

// ---- small building blocks -------------------------------------------------

function Stage({ n, tone, title, kicker, tech, labelsHere, children }) {
  return (
    <div className="mth-stage">
      <div className="mth-rail">
        <div className="mth-node" style={{ "--tone": tone }}>{n}</div>
      </div>
      <div className="mth-stage-body">
        <div className="mth-kicker" style={{ color: tone }}>{kicker}</div>
        <h3 className="mth-stage-title">{title}</h3>
        {(tech || labelsHere) && (
          <div className="mth-techrow">
            {tech && <span className="mth-tech">{tech}</span>}
            {labelsHere && <span className="mth-here">⟵ {labelsHere}</span>}
          </div>
        )}
        <div className="mth-stage-text">{children}</div>
      </div>
    </div>
  );
}

function Callout({ accent = "var(--ti-red)", label, children }) {
  return (
    <div className="mth-callout" style={{ "--accent": accent }}>
      {label && <div className="mth-callout-label">{label}</div>}
      <div>{children}</div>
    </div>
  );
}

function Click({ children }) {
  return <span className="mth-click">{children}</span>;
}

function JobCard({ tone, n, title, children }) {
  return (
    <div className="mth-job">
      <div className="mth-job-n" style={{ color: tone, borderColor: tone }}>{n}</div>
      <h4 className="mth-job-title">{title}</h4>
      <p className="mth-job-text">{children}</p>
    </div>
  );
}

// ---- the page --------------------------------------------------------------

export default function MethodologyScreen() {
  return (
    <div className="content mth">
      <MthStyles />

      {/* Hero */}
      <header className="mth-hero">
        <div className="mth-eyebrow">Field guide · no maths required</div>
        <h1 className="mth-title">
          How the matching works
        </h1>
        <p className="mth-lede">
          This tool attaches the right company to each overseas-owned UK property by
          <strong> matching names</strong>. Some of that is automatic; some needs you. Here is the
          whole method in plain English — what the computer does, where <span className="mth-you">your
          answers</span> come in, and exactly what you click, from your first upload to a fully
          trained model on next month's data.
        </p>
      </header>

      {/* 1 · The job */}
      <section className="mth-sec">
        <div className="mth-sec-head"><span className="mth-sec-n">01</span><h2>The job, in one picture</h2></div>
        <div className="mth-two">
          <div className="mth-card-list mth-ocod">
            <div className="mth-list-tag">OCOD</div>
            <div className="mth-list-h">~91,000 UK properties</div>
            <p>Each owned by an overseas company. We have the <strong>owner's name as written</strong>
              {" "}(messy, no ID number).</p>
          </div>
          <div className="mth-arrow" aria-hidden>
            <span>match the names</span>
            <svg width="60" height="20" viewBox="0 0 60 20"><path d="M2 10 H52 M44 3 L54 10 L44 17" fill="none" stroke="var(--ti-red)" strokeWidth="2"/></svg>
          </div>
          <div className="mth-card-list mth-roe">
            <div className="mth-list-tag">ROE</div>
            <div className="mth-list-h">~30,000 companies</div>
            <p>The official register of those overseas companies — a <strong>clean name and an ID
              number</strong> (the “OE number”) we want to attach.</p>
          </div>
        </div>
        <p className="mth-aside">
          Names rarely match exactly (typos, “Limited” vs “Ltd”, accents, word order), so matching is a
          mix of a confident computer guess and a human decision on the close calls.
        </p>
      </section>

      {/* 2 · The big idea */}
      <section className="mth-sec">
        <div className="mth-sec-head"><span className="mth-sec-n">02</span><h2>The one idea that runs through everything</h2></div>
        <p className="mth-p">There are only two kinds of thing in this tool, and keeping them apart is the
          whole game:</p>
        <div className="mth-planes">
          <div className="mth-plane">
            <div className="mth-plane-h">The computer's guess</div>
            <p>A score from <span className="mono">0</span> to <span className="mono">1</span>. Fast,
              consistent, and <strong>always provisional</strong> — it can be re-done at any time.</p>
          </div>
          <div className="mth-plane mth-plane-you">
            <div className="mth-plane-h">Your answer</div>
            <p>A plain <strong>yes / no</strong> on one specific pair. It is a <strong>fact</strong>:
              it always beats the computer, it is saved for good, and it carries over to future runs.</p>
          </div>
        </div>
        <Callout label="Why it matters">
          Everywhere in the tool you should be able to tell which one you're looking at — a provisional
          guess, or a saved human fact. <span className="mth-you">Your answers are coloured red</span>
          {" "}throughout this guide for exactly that reason: the human wins.
        </Callout>
      </section>

      {/* 3 · The pipeline */}
      <section className="mth-sec">
        <div className="mth-sec-head"><span className="mth-sec-n">03</span><h2>The method, step by step</h2></div>
        <p className="mth-p">A run moves through these steps in order, each handing its work to the next.
          The <span className="mono" style={{ fontSize: 13 }}>grey chips</span> are the real name of each
          piece in the code; the <span className="mth-you">red markers</span> show where your answers plug in.</p>

        <div className="mth-pipe">
          <Stage n="0" tone="var(--muted)" kicker="Tidy up" title="Clean the names"
            tech="config rules — name_rules.json + jurisdiction_map.csv">
            Turn messy names into a standard form so harmless spelling differences don't block a match:
            “LIMITED” → “LTD”, drop accents and punctuation, fix spacing, line up country names.
            “Badby Properties (Middlesbrough) S.à r.l.” becomes “BADBY PROPERTY MIDDLESBROUGH SARL”.
          </Stage>

          <Stage n="1" tone="var(--blue)" kicker="Free wins" title="Match the obvious ones"
            tech="deterministic exact join — clean name + country">
            Any property whose tidied name <em>and</em> country are identical to a company is matched
            right there — no judgement needed. On real data that's about <strong>three-quarters</strong>
            {" "}of matches, set aside immediately so the hard work only happens on the genuinely tricky
            leftovers.
          </Stage>

          <Stage n="2" tone="var(--amber)" kicker="The simple matcher" title="Score the tricky pairs"
            tech="Splink — Fellegi–Sunter probabilistic linkage, self-taught (EM), DuckDB">
            For the leftovers, the <strong>simple matcher</strong> compares each possible pair several
            ways — how close the names are letter-by-letter, words they share, numbers they share, the
            name with “Ltd” stripped off — and blends those clues into one score from 0 to 1.
            <span className="mth-self"> It teaches itself from the data — it does <strong>not</strong> need
            your answers.</span> Its weakness: the scores come out in a few lumps, so a precise cutoff is
            hard to place.
          </Stage>

          <Stage n="2.5" tone="var(--violet)" kicker="The trained model · optional" title="Re-score using your answers"
            tech="GBT — gradient-boosted trees (LightGBM) + Platt (logistic) calibration"
            labelsHere="your yes / no answers train this">
            Once you've confirmed enough matches, a <strong>second model</strong> learns from your
            <span className="mth-you"> yes / no answers</span>. It takes the simple matcher's score
            <em> plus</em> the name clues as inputs and produces a <strong>smoother, more trustworthy
            score</strong> you can read as a real probability (0.8 really means ~80% likely). It weighs
            <strong> distinctive words</strong> more than boilerplate, and flags look-alikes that differ
            only by a <strong>unit</strong> (27A vs 27B). Its biggest win is the bottom end — it
            <strong> confidently clears the obvious non-matches</strong>, so the pile left for you is mostly
            the genuinely hard calls. <strong>This is where your answers really do the work.</strong> It's
            switched off until you build it, and a run quietly falls back to the simple matcher unless you turn it on.
          </Stage>

          <Stage n="3" tone="var(--green)" kicker="Sort" title="Split into piles by cutoff"
            tech="threshold bucketing on the chosen score (Splink or GBT)">
            Two cutoffs split the scored pairs into <strong>auto-accept</strong> (above the high line),
            <strong> needs your review</strong> (in the middle), and <strong>ignore</strong> (below the low
            line). A separate <strong>“too close to call”</strong> pile flags a property where two companies
            tie. The cutoffs are a choice you can move, not the truth.
          </Stage>

          <Stage n="★" tone="var(--ti-red)" kicker="You decide" title="Your answers override everything — last"
            tech="label applier — runs on every run, last"
            labelsHere="your answers override the result here">
            Right at the end, <span className="mth-you">your saved yes/no answers are laid on top</span>:
            a <strong>yes</strong> forces a match into the result, a <strong>no</strong> takes one out —
            whatever the computer thought. This runs <strong>every time</strong>, so your decisions are
            never lost to a re-run or a moved cutoff.
          </Stage>
        </div>

        <Callout accent="var(--ink)" label="The actual stack, in one line">
          Python · <strong>Splink</strong> (DuckDB backend) for the simple matcher ·
          {" "}<strong>LightGBM</strong> gradient-boosted trees + Platt (logistic) calibration for the trained
          model · SQLite label store · FastAPI API · React UI. Your answers live in the label store and
          feed the <strong>LightGBM</strong> model (step 2.5) and the final override (step ★).
        </Callout>
      </section>

      {/* 4 · where labels come in */}
      <section className="mth-sec">
        <div className="mth-sec-head"><span className="mth-sec-n">04</span><h2>Where your answers come in</h2></div>
        <p className="mth-p">One saved answer (a “label”) is just your <span className="mth-you">yes/no on one
          pair</span> — “this property owner <em>is</em> / <em>isn't</em> this company”. The same answer
          quietly does <strong>three different jobs</strong>:</p>
        <div className="mth-jobs">
          <JobCard tone="var(--ti-red)" n="1" title="Fixes the output">
            Your yes forces the match into the downloaded file; your no removes it — no matter what the
            computer decided.
          </JobCard>
          <JobCard tone="var(--violet)" n="2" title="Teaches the trained model">
            Your yeses <em>and</em> nos are what the trained model learns from. The <strong>nos matter
            most</strong> — they teach it what a wrong match looks like.
          </JobCard>
          <JobCard tone="var(--blue)" n="3" title="Tests it honestly">
            A chunk of your answers is held back as a “test set”, so the model can be graded on answers it
            never learned from. Without it, a perfect-looking score can't be trusted.
          </JobCard>
        </div>
        <Callout accent="var(--amber)" label="Common confusion, cleared up">
          The <strong>simple matcher</strong> (step 2) mostly <strong>doesn't</strong> use your answers —
          it learns from the data's own patterns. It's the <strong>trained model</strong> (step 2.5) that
          learns from your yes/no. So your answers' real home is the trained model, plus overriding the
          final output.
        </Callout>
      </section>

      {/* 5 · the walkthrough */}
      <section className="mth-sec">
        <div className="mth-sec-head"><span className="mth-sec-n">05</span><h2>The full walk-through — what you click</h2></div>

        <h3 className="mth-phase">A · Your first run</h3>
        <ol className="mth-steps">
          <li><b>Upload &amp; start.</b> <Click>New run</Click> → drop in the two files (the OCOD release
            and the Companies House snapshot) → drag the <b>auto-accept cutoff</b> to how confident you
            want to be → <Click>Start run</Click>.</li>
          <li><b>Watch it land.</b> You arrive on the run page. The badge at the top reads
            <Click>Decided by: simple matcher</Click> — that's expected for a first run.</li>
          <li><b>Just want the matches?</b> <Click>Download matches only</Click> gives you the matched rows
            with no blanks. If you trust the auto-accepts, you're done.</li>
          <li><b>Do better — review the middle pile.</b> <Click>Open review queue</Click>. Go through the
            pairs and hit <Click>Mark TRUE</Click> / <Click>Mark FALSE</Click>. To go fast: drag a band on
            the chart, <Click>Mark all TRUE</Click>, then flip the few wrong ones before you save.</li>
        </ol>

        <h3 className="mth-phase">B · Build the trained model</h3>
        <ol className="mth-steps" start="5">
          <li><b>Set aside a test set.</b> On the run page open the <Click>Diagnostics</Click> tab →
            model panel → <Click>Set aside a test set</Click> (so the accuracy is honest).</li>
          <li><b>Train, then apply.</b> <Click>Train from this run</Click> → glance at the accuracy (ignore
            a perfect <span className="mono">1.000</span> with a warning) → <Click>Apply GBT &amp;
            re-bucket</Click>. If it's <b>blocked</b> (“would empty your review”), the model isn't ready —
            go back and confirm more pairs, especially <b>noes</b>.</li>
          <li><b>Now it's running the trained model.</b> The badge flips to <Click>Decided by: trained
            model</Click>, the chart says “GBT score”, and your review pile is sharper.</li>
        </ol>

        <h3 className="mth-phase">C · Next month — new data, old answers</h3>
        <ol className="mth-steps" start="8">
          <li><b>Upload the new month.</b> <Click>New run</Click> → drop in the <b>new</b> files →
            <Click>Start run</Click>.</li>
          <li><b>Your old answers re-apply by themselves.</b> They're saved against the
            <em> company's identity</em>, not the run — so when the same property-owner shows up again,
            your past yes/no is applied automatically. The run page tells you how many applied and how many
            <Click>didn't match this run's data</Click>.</li>
          <li><b>Confirm only what's genuinely new,</b> then re-train. You now have old + new answers, and
            the model gets a little better every month.</li>
        </ol>

        <Callout label="Why your answers survive new data">
          An answer is filed under <span className="mono">owner name + country + company number</span>,
          not under a run. That's why last month's decisions come back automatically this month — you only
          ever review what's actually new.
        </Callout>
      </section>

      {/* 6 · sister tool */}
      <section className="mth-sec">
        <div className="mth-sec-head"><span className="mth-sec-n">06</span><h2>The same recipe, elsewhere</h2></div>
        <p className="mth-p">A sister tool, <strong>PSC reconcile</strong>, matches <em>people</em> (company
          controllers) instead of companies. It runs the <strong>same stack</strong> — the same
          {" "}<span className="mono" style={{ fontSize: 13 }}>Splink</span> simple matcher, the same
          {" "}<span className="mono" style={{ fontSize: 13 }}>LightGBM</span> trained model and calibration,
          the same label store and review screens. Two things differ, both by design: the <strong>clues</strong>
          {" "}(PSC adds dates of birth, nationality and shared-company links; this tool matches on the
          {" "}<strong>company name</strong> alone), and PSC keeps an extra “Splink-trained-from-labels” step
          that this tool drops. The shared engine is meant to improve in lock-step; the clue sets are
          per-project plug-ins.</p>
      </section>

      <footer className="mth-foot">
        <span>Ready to try it?</span>
        <Link className="btn primary" to="/runs/new">Start a new run →</Link>
        <Link className="btn" to="/labels">See your saved answers</Link>
      </footer>
    </div>
  );
}

// ---- scoped styles ---------------------------------------------------------

function MthStyles() {
  return (
    <style>{`
      .mth { max-width: 860px; padding-bottom: 80px; }
      .mth-you { color: var(--ti-red); font-weight: 600; }
      .mth-self { color: var(--amber); }

      .mth-hero { padding: 18px 0 30px; border-bottom: 2px solid var(--ink); margin-bottom: 8px; }
      .mth-eyebrow { font: 600 11px/1 var(--font-mono); letter-spacing: .14em; text-transform: uppercase;
        color: var(--ti-red); margin-bottom: 16px; }
      .mth-title { font-size: clamp(34px, 6vw, 56px); line-height: .98; letter-spacing: -.02em;
        font-weight: 680; margin: 0 0 18px; color: var(--ink); }
      .mth-lede { font-size: 17px; line-height: 1.6; color: var(--ink-2); max-width: 64ch; margin: 0; }

      .mth-sec { padding: 38px 0; border-bottom: 1px solid var(--line); }
      .mth-sec-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 18px; }
      .mth-sec-n { font: 600 13px/1 var(--font-mono); color: var(--ti-red); letter-spacing: .05em;
        padding-top: 3px; }
      .mth-sec-head h2 { font-size: 25px; letter-spacing: -.01em; margin: 0; font-weight: 640; }
      .mth-p { font-size: 15.5px; line-height: 1.65; color: var(--ink-2); max-width: 64ch; }
      .mth-aside { font-size: 13.5px; line-height: 1.6; color: var(--muted); max-width: 64ch;
        border-left: 2px solid var(--line-strong); padding-left: 14px; margin-top: 18px; }

      /* the job */
      .mth-two { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 14px;
        margin: 8px 0; }
      .mth-card-list { border: 1px solid var(--line); border-radius: var(--r-md); padding: 16px 18px;
        background: var(--surface); box-shadow: var(--shadow-sm); }
      .mth-ocod { border-top: 3px solid var(--blue); }
      .mth-roe  { border-top: 3px solid var(--green); }
      .mth-list-tag { font: 600 11px/1 var(--font-mono); letter-spacing: .1em; color: var(--muted);
        margin-bottom: 8px; }
      .mth-list-h { font-size: 18px; font-weight: 640; margin-bottom: 6px; }
      .mth-card-list p { font-size: 13px; line-height: 1.55; color: var(--ink-2); margin: 0; }
      .mth-arrow { display: flex; flex-direction: column; align-items: center; gap: 4px; color: var(--ti-red); }
      .mth-arrow span { font: 600 10px/1 var(--font-mono); letter-spacing: .06em; text-transform: uppercase; white-space: nowrap; }

      /* planes */
      .mth-planes { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 18px 0; }
      .mth-plane { border: 1px solid var(--line); border-radius: var(--r-md); padding: 16px 18px;
        background: var(--surface-sub); }
      .mth-plane-you { border-color: var(--ti-red); background: var(--ti-red-50); }
      .mth-plane-h { font-size: 15px; font-weight: 660; margin-bottom: 6px; }
      .mth-plane-you .mth-plane-h { color: var(--ti-red-700); }
      .mth-plane p { font-size: 13.5px; line-height: 1.55; color: var(--ink-2); margin: 0; }

      /* callout */
      .mth-callout { border-left: 3px solid var(--accent); background: var(--surface-sub);
        border-radius: 0 var(--r-md) var(--r-md) 0; padding: 14px 18px; margin: 20px 0 0;
        font-size: 14px; line-height: 1.6; color: var(--ink-2); }
      .mth-callout-label { font: 600 10.5px/1 var(--font-mono); letter-spacing: .1em; text-transform: uppercase;
        color: var(--accent); margin-bottom: 7px; }

      /* pipeline */
      .mth-pipe { margin-top: 10px; }
      .mth-stage { display: grid; grid-template-columns: 52px 1fr; gap: 18px; }
      .mth-rail { display: flex; flex-direction: column; align-items: center; }
      .mth-node { width: 40px; height: 40px; flex: none; border-radius: 50%; display: grid; place-items: center;
        font: 600 15px/1 var(--font-mono); color: #fff; background: var(--tone);
        box-shadow: 0 0 0 4px var(--bg), 0 0 0 5px color-mix(in srgb, var(--tone) 35%, transparent); }
      .mth-stage:not(:last-child) .mth-rail::after { content: ""; flex: 1; width: 2px; min-height: 22px;
        background: linear-gradient(var(--line-strong), var(--line)); margin: 6px 0; }
      .mth-stage-body { padding-bottom: 26px; }
      .mth-kicker { font: 600 11px/1 var(--font-mono); letter-spacing: .08em; text-transform: uppercase; margin-bottom: 6px; }
      .mth-stage-title { font-size: 19px; font-weight: 640; margin: 0 0 7px; letter-spacing: -.01em; }
      .mth-stage-text { font-size: 14.5px; line-height: 1.62; color: var(--ink-2); max-width: 60ch; }
      .mth-techrow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 9px; }
      .mth-tech { font: 500 11.5px/1.5 var(--font-mono); color: var(--ink-2); background: var(--surface-sub);
        border: 1px solid var(--line); border-radius: 4px; padding: 2px 9px; }
      .mth-here { font: 600 11px/1.5 var(--font-mono); color: #fff; background: var(--ti-red);
        border-radius: 4px; padding: 2px 9px; }

      /* jobs */
      .mth-jobs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 6px 0; }
      .mth-job { border: 1px solid var(--line); border-radius: var(--r-md); padding: 16px; background: var(--surface); }
      .mth-job-n { width: 28px; height: 28px; border: 2px solid; border-radius: 50%; display: grid; place-items: center;
        font: 700 13px/1 var(--font-mono); margin-bottom: 10px; }
      .mth-job-title { font-size: 15px; font-weight: 640; margin: 0 0 6px; }
      .mth-job-text { font-size: 13px; line-height: 1.55; color: var(--ink-2); margin: 0; }

      /* walkthrough */
      .mth-phase { font-size: 16px; font-weight: 660; margin: 26px 0 10px; padding-bottom: 6px;
        border-bottom: 1px solid var(--line); color: var(--ti-red-700); }
      .mth-steps { margin: 0; padding-left: 0; list-style: none; counter-reset: s; }
      .mth-steps > li { position: relative; counter-increment: s; padding: 10px 0 10px 40px;
        font-size: 14.5px; line-height: 1.6; color: var(--ink-2); border-bottom: 1px dashed var(--line); }
      .mth-steps > li::before { content: counter(s); position: absolute; left: 0; top: 9px;
        width: 26px; height: 26px; border-radius: 50%; background: var(--ink); color: var(--bg);
        display: grid; place-items: center; font: 600 12px/1 var(--font-mono); }
      .mth-click { display: inline-block; font: 500 12.5px/1.5 var(--font-mono); background: var(--ink);
        color: var(--bg); padding: 1px 7px; border-radius: 4px; white-space: nowrap; }

      .mth-foot { display: flex; align-items: center; gap: 14px; padding-top: 30px; flex-wrap: wrap; }
      .mth-foot > span { font-size: 16px; font-weight: 600; }

      @media (max-width: 720px) {
        .mth-two, .mth-planes, .mth-jobs { grid-template-columns: 1fr; }
        .mth-arrow { transform: rotate(90deg); margin: 4px 0; }
      }
    `}</style>
  );
}
