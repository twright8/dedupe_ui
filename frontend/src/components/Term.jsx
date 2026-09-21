/* ============================================================
   Term, TermHint, Provenance — the words, with their meanings
   ------------------------------------------------------------
   Three things, all reading from src/glossary.js:

     <Term name="unit" />        the word, with its definition one
                                 hover or one tab-stop away
     <TermHint name="unit" />    a small "?" for a column header or
                                 a KPI label, where the word itself
                                 cannot carry an underline
     <Provenance kind=… value=…> one chip answering "How it was
                                 decided", from any of the API's
                                 seven vocabularies

   The popover is drawn into <body>, not beside the word, for two
   reasons: a table body scrolls and would clip it, and drawing it
   in place would push the row down. It opens on hover and on
   keyboard focus, and Escape closes it.
   ============================================================ */

import { useState, useRef, useEffect, useCallback, useId } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useProfile } from "../profile";
import {
  term,
  termAnchor,
  provenanceFor,
  VALUE_BASIS,
  VALUE_BASIS_QUESTION,
  ID_ORIGIN,
  ID_ORIGIN_QUESTION,
  AGREEMENT,
  AGREEMENT_QUESTION,
  PROVENANCE_QUESTION,
} from "../glossary";

const GAP = 6; // between the word and the panel
const WIDTH = 300;

/* Where the panel goes. Below the word when there is room, above it when
   there is not, and never off either edge. Fixed coordinates, so a scrolling
   table cannot clip it and nothing on the page moves. */
function place(rect) {
  const w = typeof window === "undefined" ? 1200 : window.innerWidth;
  const h = typeof window === "undefined" ? 800 : window.innerHeight;
  const left = Math.max(8, Math.min(rect.left, w - WIDTH - 8));
  const below = rect.bottom + GAP;
  const roomBelow = h - rect.bottom;
  if (roomBelow < 140 && rect.top > 140) {
    return { left, bottom: h - rect.top + GAP, top: undefined };
  }
  return { left, top: below, bottom: undefined };
}

/* The shared behaviour: open on hover or focus, close on leave, blur or
   Escape. Returns what the trigger and the panel each need. */
function usePopover() {
  const [at, setAt] = useState(null);
  const ref = useRef(null);
  const timer = useRef(null);

  const open = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    const el = ref.current;
    if (el) setAt(place(el.getBoundingClientRect()));
  }, []);

  /* A short delay before closing, so the pointer can cross the gap between the
     word and the panel and reach the "More" link inside it. */
  const close = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setAt(null), 140);
  }, []);

  const closeNow = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    setAt(null);
  }, []);

  useEffect(() => () => timer.current && clearTimeout(timer.current), []);

  useEffect(() => {
    if (!at) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeNow();
      }
    };
    const onScroll = () => closeNow();
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [at, closeNow]);

  const triggerProps = {
    ref,
    tabIndex: 0,
    onMouseEnter: open,
    onMouseLeave: close,
    onFocus: open,
    onBlur: close,
    onClick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (at) closeNow();
      else open();
    },
    onKeyDown: (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (at) closeNow();
        else open();
      }
    },
  };

  // The panel keeps itself open while the pointer is on it.
  const panelProps = { onMouseEnter: open, onMouseLeave: close };

  return { at, open, close, closeNow, triggerProps, panelProps };
}

/* The panel itself. `heading` is the word or the label, `body` its definition,
   `note` an optional second line, `more` an anchor on How it works. */
function Panel({ id, at, heading, body, note, more, panelProps }) {
  if (!at) return null;
  if (typeof document === "undefined") return null;
  return createPortal(
    <div
      id={id}
      role="tooltip"
      className="term-pop"
      style={{ left: at.left, top: at.top, bottom: at.bottom, width: WIDTH }}
      {...panelProps}
    >
      <div className="term-pop-h">{heading}</div>
      <p className="term-pop-b">{body}</p>
      {note && <p className="term-pop-n">{note}</p>}
      {more && (
        <Link className="term-pop-more" to={more} tabIndex={-1}>
          More in How it works
        </Link>
      )}
    </div>,
    document.body
  );
}

/* ------------------------------------------------------------
   <Term>
   ------------------------------------------------------------
   Wrap the first mention of a glossary word in each region of a
   screen. `plural` picks the plural form. `cap` capitalises the
   first letter, for the start of a sentence or a heading. Children
   override the visible words entirely, for a phrase the glossary
   cannot inflect.
   ------------------------------------------------------------ */
export function Term({ name, term: termProp, plural = false, cap = false, children }) {
  const key = name || termProp;
  const profile = useProfile();
  const entry = term(key, profile);
  const id = useId();
  const { at, triggerProps, panelProps } = usePopover();

  if (!entry) return <>{children || key}</>;

  let text = children || (plural ? entry.plural : entry.term);
  if (cap && typeof text === "string") text = text.charAt(0).toUpperCase() + text.slice(1);

  return (
    <>
      <span
        {...triggerProps}
        className="term"
        role="button"
        aria-describedby={at ? id : undefined}
      >
        {text}
      </span>
      <Panel
        id={id}
        at={at}
        panelProps={panelProps}
        heading={entry.term}
        body={entry.definition}
        more={`/methodology#${termAnchor(entry.key)}`}
      />
    </>
  );
}

/* ------------------------------------------------------------
   <TermHint>
   ------------------------------------------------------------
   For a place the word cannot be underlined: a table column
   header, a chip group label, a KPI label. Renders a small "?"
   beside the text, carrying the same definition.
   ------------------------------------------------------------ */
export function TermHint({ name, term: termProp }) {
  const key = name || termProp;
  const profile = useProfile();
  const entry = term(key, profile);
  const id = useId();
  const { at, triggerProps, panelProps } = usePopover();

  if (!entry) return null;

  return (
    <>
      <span
        {...triggerProps}
        className="term-hint"
        role="button"
        aria-describedby={at ? id : undefined}
        aria-label={`What ${entry.term} means: ${entry.definition}`}
      >
        ?
      </span>
      <Panel
        id={id}
        at={at}
        panelProps={panelProps}
        heading={entry.term}
        body={entry.definition}
        more={`/methodology#${termAnchor(entry.key)}`}
      />
    </>
  );
}

/* ------------------------------------------------------------
   <Provenance>
   ------------------------------------------------------------
   One chip for "How it was decided". `kind` names the API field the
   value came from: decided_by, entity_basis, edge_source,
   provenance or vetoed_by. The same value always reads as the same
   word in the same colour, wherever it appears.

   Three other fields answer a DIFFERENT question and keep their own
   words: basis (how one value was set), id_status (where an ID came
   from) and agreement (how a group compares with the earlier
   grouping). Pass those as `kind` too and the chip says which
   question it is answering.
   ------------------------------------------------------------ */

const OTHER_QUESTIONS = {
  basis: { table: VALUE_BASIS, question: VALUE_BASIS_QUESTION },
  id_status: { table: ID_ORIGIN, question: ID_ORIGIN_QUESTION },
  agreement: { table: AGREEMENT, question: AGREEMENT_QUESTION },
};

export function Provenance({ kind, value, detail, note, size }) {
  const id = useId();
  const { at, triggerProps, panelProps } = usePopover();

  const other = OTHER_QUESTIONS[kind];
  let meta = null;
  let question = PROVENANCE_QUESTION;

  if (other) {
    meta = other.table[String(value || "").toLowerCase()] || null;
    question = other.question;
  } else {
    meta = provenanceFor(kind, value, detail);
  }

  if (!meta) return null;

  const second = other ? detail : meta.detail;

  return (
    <>
      <span
        {...triggerProps}
        className={"tag prov " + (meta.tag || "") + (size === "sm" ? " prov-sm" : "")}
        role="button"
        aria-describedby={at ? id : undefined}
        aria-label={`${question}: ${meta.label}`}
      >
        {meta.label}
        {second && <span className="prov-detail">{second}</span>}
      </span>
      <Panel
        id={id}
        at={at}
        panelProps={panelProps}
        heading={`${question}: ${meta.label}`}
        body={meta.definition}
        note={note || second || null}
      />
    </>
  );
}

/* The plain word for a provenance value, where a chip will not fit — a filter
   name, an option in a select, a sentence. */
export function provenanceLabel(kind, value) {
  const other = OTHER_QUESTIONS[kind];
  if (other) return other.table[String(value || "").toLowerCase()]?.label || null;
  return provenanceFor(kind, value)?.label || null;
}
