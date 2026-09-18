/* ============================================================
   Screen: How it works — the method in plain English, stage by
   stage. Editorial "field guide" style. RED marks the reader's own
   answers, because a human label always wins.
   ============================================================ */

import { Link } from "react-router-dom";
import { useProfile } from "../profile";

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
  const profile = useProfile();
  const tracks = profile.tracks || [];
  const trackA = tracks[0]?.label || "People";
  const trackB = tracks[1]?.label || "Organisations";
  const donations = profile.key === "donations";

  return (
    <div className="content mth">
      <MthStyles />

      {/* Hero */}
      <header className="mth-hero">
        <div className="mth-eyebrow">Field guide · no maths required</div>
        <h1 className="mth-title">How the matching works</h1>
        <p className="mth-lede">
          {profile.title} finds the records that are the same person or the same organisation. It
          gives every group of those records one ID. Whenever it is not sure, it asks you. The tool
          assists you. It does not decide alone.
        </p>
      </header>

      {/* 1 · guess and answer */}
      <section className="mth-sec">
        <div className="mth-sec-head">
          <span className="mth-sec-n">01</span>
          <h2>Two kinds of thing</h2>
        </div>
        <p className="mth-p">
          Every screen in this tool shows one of two kinds of thing. Telling them apart is the
          whole method. One is a score the computer worked out. The other is an answer you gave.
        </p>
        <div className="mth-planes">
          <div className="mth-plane">
            <div className="mth-plane-h">The computer's score</div>
            <p>
              A number from <span className="mono">0</span> to <span className="mono">1</span>. It
              is quick and consistent. It can be worked out again at any time, so it is always
              provisional.
            </p>
          </div>
          <div className="mth-plane mth-plane-you">
            <div className="mth-plane-h">Your answer</div>
            <p>
              A yes or a no on one pair of records. It is saved for good. It beats every score and
              every rule, and it carries over to later runs.
            </p>
          </div>
        </div>
        <Callout label="Why it matters">
          <span className="mth-you">Your answers are coloured red</span> throughout this guide. If
          a rule and your answer disagree, your answer stands.
        </Callout>
      </section>

      {/* 2 · the stages */}
      <section className="mth-sec">
        <div className="mth-sec-head">
          <span className="mth-sec-n">02</span>
          <h2>The method, step by step</h2>
        </div>
        <p className="mth-p">
          A run moves through these stages in order. Each stage hands its work to the next. The
          grey chip on each stage names the tab where you change it. The red marker shows where
          your answers come in.
        </p>

        <div className="mth-pipe">
          <Stage n="0" tone="var(--muted)" kicker="Load" title="Read the file">
            <p>
              In goes one file. The tool reads every row and gives each row a record ID. Out comes
              one record per row, with the columns this profile knows about. You change nothing
              here. To read the records, open the <Click>Records</Click> tab on the run.
            </p>
          </Stage>

          <Stage
            n="1"
            tone="var(--blue)"
            kicker="Tracks"
            title={`Sort each record into ${trackA} or ${trackB}`}
            tech="Config → Tracks"
          >
            <p>
              In go the raw records. Ordered rules decide which track a record belongs to. The
              first rule whose conditions all hold wins. A record no rule catches takes the default
              track. Out comes one track per record.
            </p>
            <p>
              {trackA} and {trackB} are matched separately. The tool never suggests that a record
              on one track is the same thing as a record on the other. To see the split, use the
              track filter on the <Click>Records</Click> tab.
            </p>
          </Stage>

          <Stage
            n="2"
            tone="var(--blue)"
            kicker="Cleaning"
            title="Tidy the values into new columns"
            tech="Config → Cleaning rules"
          >
            <p>
              In go the raw columns. Ordered steps write new columns beside them. A step can put
              text into capitals, drop punctuation, remove titles such as MR, map a nickname to a
              full name, or call a named function such as the one that tidies a postcode. A raw
              column is never overwritten.
            </p>
            {donations ? (
              <p>
                For example, &ldquo;Mr Hugh E Osmond&rdquo; becomes{" "}
                <span className="mono">HUGH E OSMOND</span> with the title{" "}
                <span className="mono">MR</span> kept in its own column. The company number{" "}
                <span className="mono">4250076</span> becomes <span className="mono">04250076</span>
                , so it lines up with the eight-digit form.
              </p>
            ) : (
              <p>
                For example, a name typed in mixed case with punctuation becomes one plain
                upper-case name, and a short code is padded to its full length so the two forms
                line up.
              </p>
            )}
            <p>
              Out come the cleaned columns. To read them, open the <Click>Records</Click> tab and
              turn on <Click>Cleaned and derived columns</Click>.
            </p>
          </Stage>

          <Stage
            n="3"
            tone="var(--blue)"
            kicker="Derived columns"
            title="Standardise a category that is often wrong"
            tech="Config → Derived columns"
          >
            <p>
              In go the raw and the cleaned columns. Ordered rules set a standard value in a new
              column. The first rule whose conditions all hold sets the value. A record no rule
              catches takes the value of the column you named as the default.
            </p>
            {donations ? (
              <p>
                Donor status is the example this profile ships with. If a company number starts
                with OC, SO or NC, the record is a limited liability partnership, whatever the
                sheet said.
              </p>
            ) : (
              <p>
                A category that the source often records wrongly is the case this is for. A rule
                reads a cleaned column and sets the value the category should have.
              </p>
            )}
            <p>
              Out comes the new column, together with a note of which rule set each value. To read
              it, open the <Click>Records</Click> tab and turn on{" "}
              <Click>Cleaned and derived columns</Click>.
            </p>
          </Stage>

          <Stage
            n="4"
            tone="var(--green)"
            kicker="Exact keys"
            title="Merge the records that plainly agree"
            tech="Config → Match keys"
          >
            <p>
              In go the cleaned and derived columns. A match key names one or more columns. Records
              that hold the same value in every one of those columns form a group. Keys run in
              order within a track, and groups that share a record are joined into one.
            </p>
            <p>
              Guards stop a group that looks wrong. A guard can ignore a value that appears on a
              blocked list, refuse a group over a size limit, refuse a group carrying more
              different names than you allow, or require the records to agree on a second column
              as well. If a guard stops a group, the group is held and a person decides it later.
              Nothing is merged.
            </p>
            <p>
              Out come the merged groups and the held groups. To read them, open the{" "}
              <Click>Exact groups</Click> tab on the run.
            </p>
          </Stage>

          <Stage
            n="5"
            tone="var(--amber)"
            kicker="Scoring"
            title="Score the pairs that are not obvious"
            tech="Config → Thresholds & Splink"
          >
            <p>
              In go the results of stage 4. Each merged group counts as one unit. Every other
              record is a unit on its own. Units are scored within a track, never across two.
            </p>
            <p>
              Comparing every unit with every other unit would take far too long. Blocking rules
              decide which pairs get compared at all. A blocking rule says something like
              &ldquo;only compare two records when they share a surname&rdquo;. A tight rule is
              quick and can miss a match. A loose rule finds more and costs more time.
            </p>
            <p>
              Each pair that gets through blocking is compared column by column. The comparisons
              are combined into one score between 0 and 1, which reads as the chance that the two
              units are the same thing. The score is worked out from the shape of the data itself.
              Your answers do not train it.
            </p>
            <p>
              The tab that holds these settings is called Thresholds &amp; Splink. Splink is the
              name of the open-source engine that does the scoring.
            </p>
            <p>
              Two lines then sort every scored pair into three buckets. If the score is 0.92 or
              more, the pair is accepted without review. If the score is between 0.50 and 0.92, the
              pair goes to you. If the score is below 0.50, the pair is rejected. Both lines are
              yours to move. Out come the scored pairs. When a trained model is in use, stage 6
              re-scores them and its score sets the buckets instead.
            </p>
            <p>
              A veto is a plain rule about a pair that stops the tool accepting something a person
              would never accept. Two people born thirty years apart are not one person, even when
              the name and the postcode agree. A veto either sends the pair to you or rejects it,
              and the reason it gives is shown beside the pair on the review screen. An earlier
              grouping can still overrule a veto, and your own answer overrules everything.
            </p>
          </Stage>

          <Stage
            n="6"
            tone="var(--violet)"
            kicker="The model"
            title="A trained model re-scores the pairs"
            tech="Diagnostics → Trained model"
          >
            <p>
              In go the scored pairs. A model learns from the answers people have saved: your own
              answers, the decisions taken on whole groups, and at a lower weight the earlier manual
              grouping. Each track has its own model. Out comes a second score for every pair.
            </p>
            <p>
              A model that has seen fewer than fifty of your answers is a cold start. It re-orders
              the review queue so the pairs worth your time come first, and it decides nothing.
            </p>
            <p>
              A model becomes graded once it has enough of your answers and a set of answers held
              back for testing. Only then may it set the buckets, and its accept and reject lines
              are worked out from that held-back set rather than chosen by hand.
            </p>
            <p>
              The model can only learn from what it has been told. The earlier grouping was made
              almost entirely on the name, so it cannot teach the model when two people with one
              name are different people. Only new answers that say two records are not the same can
              do that.
            </p>
          </Stage>

          <Stage
            n="7"
            tone="var(--ti-red)"
            kicker="Review"
            title="You answer the pairs the tool is unsure about"
            labelsHere="your answers are saved here"
          >
            <p>
              In go the scored pairs. You mark a pair as a match or as not a match. Your answer
              always wins. It overrides the score, and it overrides every rule.
            </p>
            <p>
              Answers are never overwritten. If you answer the same pair again, the new answer
              takes effect and the old one stays on record with the name of the person who gave it.
              You can add a note and a source link to any answer, which is where evidence from
              outside the data belongs.
            </p>
            <p>
              Labels imported from earlier manual work are treated carefully. If both sides of a
              pair already carry the same earlier ID, the pair is accepted. If the two sides carry
              different earlier IDs, the pair keeps the bucket its score gave it and is flagged so
              you can find it. That flag is a signal for sorting. It never decides a pair.
            </p>
            <p>
              Out come your saved answers, which you can read in the <Click>Label library</Click>.
            </p>
          </Stage>

          <Stage
            n="8"
            tone="var(--violet)"
            kicker="Clusters"
            title="Join the accepted pairs into groups"
            tech="Config → Thresholds & Splink"
          >
            <p>
              In go the accepted pairs. Two units joined by an accepted pair sit in the same
              cluster. A unit with no accepted pair is a cluster on its own.
            </p>
            <p>
              A gate then checks every cluster of two or more units. A cluster is held back if you
              have said two of its units are not the same, if one pair inside it scores below 0.20
              and the cluster may be a chain of weak links, if it holds more than 200 units, or if
              its records carry more than one earlier ID. A held cluster is rebuilt from your
              answers and the imported labels only, and it goes to a queue for a person to decide.
            </p>
            <p>
              Out come the clusters. Each one carries a status that says whether it passed those
              checks or was held back for a person to look at.
            </p>
          </Stage>

          <Stage
            n="9"
            tone="var(--violet)"
            kicker="Entity IDs"
            title="Give each group one ID, then publish"
          >
            <p>
              In go the clusters the gate passed. Each one becomes one entity and takes an ID. If
              its records already belong to one entity, that ID is kept. If they belong to several,
              one of those IDs survives and the others are retired. If they belong to none, a new
              ID is made.
            </p>
            <p>
              A run only proposes. Nothing is written to the durable register until you publish the
              run. Publishing is a separate step, and it records what changed.
            </p>
            <p>
              Out comes the export. It is the original sheet, row for row and column for column,
              with the entity ID and a note of how each ID was decided added at the end. A second
              sheet lists every retired ID and the ID that replaced it.
            </p>
          </Stage>
        </div>
      </section>

      {/* 3 · agreement numbers */}
      <section className="mth-sec">
        <div className="mth-sec-head">
          <span className="mth-sec-n">03</span>
          <h2>How to read the agreement numbers</h2>
        </div>
        <p className="mth-p">
          The review screen and the run summary report two figures. Both compare this run with the
          grouping the team did by hand before this tool existed.
        </p>
        <p className="mth-p">
          <strong>Pair precision</strong> tells you how often the run is right when it joins two
          records. It counts only pairs the earlier manual work had judged.
        </p>
        <p className="mth-p">
          <strong>Pair recall</strong> tells you how much of the earlier manual work the run finds.
          It counts the pairs the earlier work joined, then reports the share the run joined too.
        </p>
        <p className="mth-p">
          The same two figures are worked out three ways. Each is also reported for{" "}
          {trackA.toLowerCase()} and {trackB.toLowerCase()} separately, so a figure that looks good
          overall can still be weak on one track.
        </p>
        <div className="mth-jobs">
          <JobCard tone="var(--ink)" n="1" title="Everything the run would publish">
            The exact groups plus every accepted pair. This is what the export would contain.
          </JobCard>
          <JobCard tone="var(--ti-red)" n="2" title="The scorer on its own">
            The same figures with one group of pairs left out. Those pairs were accepted because
            both sides already carried the same earlier ID.
          </JobCard>
          <JobCard tone="var(--blue)" n="3" title="The match keys on their own">
            Stage 4 alone, with no scoring. It shows how far the plain rules get you.
          </JobCard>
        </div>
        <Callout label="Which one to use when you change a rule">
          Use <strong>the scorer on its own</strong>. A pair accepted because both sides already
          carry the same earlier ID cannot show that the scorer found anything, so leaving those
          pairs in makes every change look better than it is.
        </Callout>
        <Callout accent="var(--amber)" label="What these numbers cannot tell you">
          The earlier labels were made almost entirely on the name. Reviewers merged 99.6% of
          pairs of individuals with identical names, even when the two gave to different parties.
          So the earlier labels cannot show when two people with the same name are different
          people. A high recall against them is not proof that the tool keeps such people apart.
          Only new answers that say two records are <strong>not</strong> the same can show that,
          and those come from you.
        </Callout>
      </section>

      {/* 4 · limits */}
      <section className="mth-sec">
        <div className="mth-sec-head">
          <span className="mth-sec-n">04</span>
          <h2>What the tool will not do</h2>
        </div>
        <div className="mth-planes">
          <div className="mth-plane">
            <div className="mth-plane-h">It never matches across tracks</div>
            <p>
              A record on the {trackA.toLowerCase()} track is never proposed as the same thing as a
              record on the {trackB.toLowerCase()} track. If you believe two such records are the
              same, the track rules are what to change.
            </p>
          </div>
          <div className="mth-plane">
            <div className="mth-plane-h">It never overrules your answer</div>
            <p>
              No rule and no score can undo an answer you saved. A later answer from a person
              replaces an earlier one. Nothing else does.
            </p>
          </div>
          <div className="mth-plane">
            <div className="mth-plane-h">It never rewrites a published ID quietly</div>
            <p>
              If two published entities are joined, one ID survives and the other is retired. The
              retired ID becomes an alias of the survivor, so an old ID still leads you to the
              right record.
            </p>
          </div>
          <div className="mth-plane">
            <div className="mth-plane-h">It does not read the news</div>
            <p>
              The tool only sees the data you load. If a news story or a public register is your
              evidence, save it as a source link on your answer. That way the evidence stays with
              the decision.
            </p>
          </div>
        </div>
      </section>

      {/* 5 · glossary */}
      <section className="mth-sec">
        <div className="mth-sec-head">
          <span className="mth-sec-n">05</span>
          <h2>Words this tool uses</h2>
        </div>
        <dl className="diff-meta" style={{ fontSize: 14, gap: "10px 18px", maxWidth: "64ch" }}>
          <dt>record</dt>
          <dd>One row of the file you loaded.</dd>

          <dt>unit</dt>
          <dd>
            What the scorer compares. A unit is either one merged group of records or one record on
            its own.
          </dd>

          <dt>track</dt>
          <dd>
            {trackA} or {trackB}. Records are only ever matched with records on the same track.
          </dd>

          <dt>exact key</dt>
          <dd>
            A named set of columns. Records holding the same value in every one of those columns
            are merged.
          </dd>

          <dt>guard</dt>
          <dd>
            A limit on an exact key. If a group breaks the limit, the group is not merged and a
            person decides it.
          </dd>

          <dt>held group</dt>
          <dd>A group a guard stopped. Its records stay separate until someone decides.</dd>

          <dt>blocking rule</dt>
          <dd>
            A rule that says which pairs are worth comparing at all. It saves time and it can hide
            a match if it is too tight.
          </dd>

          <dt>score</dt>
          <dd>
            A number from 0 to 1 for one pair. It reads as the chance that the two units are the
            same thing.
          </dd>

          <dt>bucket</dt>
          <dd>
            Where a scored pair lands. A pair is accepted, sent to you for review, or rejected.
          </dd>

          <dt>veto</dt>
          <dd>
            A rule about a pair that stops the tool accepting it, whatever the score says. It sends
            the pair to you or rejects it, and it says why.
          </dd>

          <dt>label</dt>
          <dd>
            Your saved yes or no on one pair. It can carry a note and a source link, and it beats
            every score.
          </dd>

          <dt>cluster</dt>
          <dd>A set of units joined by accepted pairs. A cluster is a proposal, not a decision.</dd>

          <dt>entity</dt>
          <dd>
            One real person or one real organisation, with one ID that stays the same from run to
            run.
          </dd>
        </dl>
      </section>

      <footer className="mth-foot">
        <span>Ready to try it?</span>
        <Link className="btn primary" to="/runs/new">
          Start a new run →
        </Link>
        <Link className="btn" to="/labels">
          See your saved answers
        </Link>
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
