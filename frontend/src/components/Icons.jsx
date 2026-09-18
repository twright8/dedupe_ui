/* ============================================================
   Icons — stroke-based SVG icon set
   ============================================================ */

export function Icon({ d, size = 16, fill = "none", stroke = "currentColor", sw = 1.6, children }) {
  return (
    <svg
      className="i"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={fill}
      stroke={stroke}
      strokeWidth={sw}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {d ? <path d={d} /> : children}
    </svg>
  );
}

export const Icons = {
  runs:      (p) => <Icon {...p}><path d="M5 4v16l14-8z"/></Icon>,
  upload:    (p) => <Icon {...p}><path d="M12 4v12"/><path d="M7 9l5-5 5 5"/><path d="M4 20h16"/></Icon>,
  review:    (p) => <Icon {...p}><path d="M4 6h16M4 12h16M4 18h10"/><circle cx="19" cy="18" r="2.5"/></Icon>,
  ambiguous: (p) => <Icon {...p}><path d="M9 8a3 3 0 1 1 4.24 2.74C12.4 11.2 12 11.8 12 13"/><circle cx="12" cy="17.5" r="0.9" fill="currentColor" stroke="none"/></Icon>,
  config:    (p) => <Icon {...p}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1A1.7 1.7 0 0 0 4.6 9 1.7 1.7 0 0 0 4.3 7.2l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1c0 .68.4 1.3 1 1.5a1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.82 1 1.5 1H21a2 2 0 0 1 0 4h-.1c-.68 0-1.3.4-1.5 1z"/></Icon>,
  audit:     (p) => <Icon {...p}><path d="M5 4h11l3 3v13H5z"/><path d="M9 13h6M9 16h4M9 10h2"/></Icon>,
  search:    (p) => <Icon {...p}><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-3.5-3.5"/></Icon>,
  filter:    (p) => <Icon {...p}><path d="M4 5h16l-6 8v6l-4-2v-4z"/></Icon>,
  download:  (p) => <Icon {...p}><path d="M12 4v12M7 11l5 5 5-5M4 20h16"/></Icon>,
  play:      (p) => <Icon {...p}><path d="M6 4v16l14-8z"/></Icon>,
  check:     (p) => <Icon {...p}><path d="M5 12l4 4 10-10"/></Icon>,
  x:         (p) => <Icon {...p}><path d="M6 6l12 12M6 18L18 6"/></Icon>,
  arrowR:    (p) => <Icon {...p}><path d="M5 12h14M13 6l6 6-6 6"/></Icon>,
  arrowD:    (p) => <Icon {...p}><path d="M6 9l6 6 6-6"/></Icon>,
  arrowU:    (p) => <Icon {...p}><path d="M6 15l6-6 6 6"/></Icon>,
  more:      (p) => <Icon {...p}><circle cx="5" cy="12" r="1.5" fill="currentColor"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><circle cx="19" cy="12" r="1.5" fill="currentColor"/></Icon>,
  file:      (p) => <Icon {...p}><path d="M14 3H6v18h12V8z"/><path d="M14 3v5h4"/></Icon>,
  zip:       (p) => <Icon {...p}><path d="M14 3H6v18h12V8z"/><path d="M14 3v5h4"/><path d="M10 12v2M10 16v2M10 20v1" stroke="currentColor"/></Icon>,
  clock:     (p) => <Icon {...p}><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></Icon>,
  user:      (p) => <Icon {...p}><circle cx="12" cy="9" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/></Icon>,
  alert:     (p) => <Icon {...p}><path d="M12 4l9 16H3z"/><path d="M12 11v4"/><circle cx="12" cy="18" r="0.7" fill="currentColor" stroke="none"/></Icon>,
  bolt:      (p) => <Icon {...p}><path d="M13 3L4 14h7l-2 7 9-11h-7z"/></Icon>,
  branch:    (p) => <Icon {...p}><circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 8v8M8 8h2a4 4 0 0 1 4 4v2"/></Icon>,
  link:      (p) => <Icon {...p}><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 1 0-5.6-5.6L11.5 7"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 1 0 5.6 5.6L12.5 17"/></Icon>,
  plus:      (p) => <Icon {...p}><path d="M12 5v14M5 12h14"/></Icon>,
  dot:       (p) => <Icon {...p}><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/></Icon>,
  cmd:       (p) => <Icon {...p}><path d="M9 9V6a2 2 0 1 0-2 2h10a2 2 0 1 0-2-2v3m0 0v6m0 0v3a2 2 0 1 0 2-2H7a2 2 0 1 0 2 2v-3m0-6h6"/></Icon>,
  doc:       (p) => <Icon {...p}><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h6M9 9h3"/></Icon>,
  table:     (p) => <Icon {...p}><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M3 10h18M3 16h18M9 4v16M15 4v16"/></Icon>,
  diff:      (p) => <Icon {...p}><path d="M4 4h7v16H4zM13 4h7v16h-7z"/><path d="M7 9h1M7 12h1M7 15h1M16 8h1M16 11h1M16 14h1M16 17h1"/></Icon>,
  refresh:   (p) => <Icon {...p}><path d="M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5"/></Icon>,
  export:    (p) => <Icon {...p}><path d="M12 4v12M7 9l5-5 5 5"/><path d="M20 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4"/></Icon>,
  history:   (p) => <Icon {...p}><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 7v5l4 2"/></Icon>,
  spark:     (p) => <Icon {...p}><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M5 19l3-3M16 8l3-3"/></Icon>,
};
