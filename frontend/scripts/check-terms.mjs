/* ============================================================
   check-terms — a guard against drift
   ------------------------------------------------------------
   The tool was built fast by many hands and ended up with several
   names for one thing. src/glossary.js now holds one name per
   thing. This script makes sure the old names cannot come back.

   It reads RETIRED from src/glossary.js and scans every .js and
   .jsx file under src for:
     • a retired word in text a user can read
     • a bare snake_case token printed as text (an internal name
       leaking onto the screen)

   To keep false positives low it looks only at
     • JSX text nodes
     • string literals given to a text prop (title, label,
       placeholder, aria-label, and this codebase's own help, sub,
       lab, kicker, tech and heading)
   Everything else — field names, state keys, API values, comments —
   is left alone.

   Run: npm run check:terms. It also runs before npm run build, and
   a hit fails the build.
   ============================================================ */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join, relative, sep } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const srcDir = join(root, "src");

const { RETIRED, CHECK_ALLOW_FILES } = await import(
  pathToFileURL(join(srcDir, "glossary.js")).href
);

/* The props in this codebase that carry words a user reads. Anything else is
   a key, an id or a value. */
const TEXT_PROPS = [
  "title",
  "label",
  "placeholder",
  "aria-label",
  "help",
  "sub",
  "lab",
  "kicker",
  "heading",
  "note",
];

/* snake_case the screen is allowed to show. Each is a real file on disk or a
   column a reviewer asked for by that name. */
const SNAKE_ALLOWED = new Set([
  "records.parquet",
  "pairs.parquet",
  "units.parquet",
  "clusters.parquet",
  "entities.parquet",
  "exact_groups.parquet",
  "records_raw.parquet",
  "uk_name_frequencies.parquet",
  "entity_report.json",
  "linkage_settings.json",
  "score_eval.json",
  "match_keys",
  "record_id",
  "entity_id",
]);

// ---------- reading the files ----------

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(js|jsx)$/.test(name)) out.push(full);
  }
  return out;
}

/* Comments are not on screen, so they are removed before anything is scanned.
   Quoted text is kept, because a string literal may be a label. */
function stripComments(text) {
  let out = "";
  let i = 0;
  let quote = null;
  while (i < text.length) {
    const c = text[i];
    const next = text[i + 1];
    if (quote) {
      if (c === "\\") {
        out += "  ";
        i += 2;
        continue;
      }
      if (c === quote) quote = null;
      out += c;
      i += 1;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      quote = c;
      out += c;
      i += 1;
      continue;
    }
    if (c === "/" && next === "*") {
      const end = text.indexOf("*/", i + 2);
      const stop = end === -1 ? text.length : end + 2;
      for (let k = i; k < stop; k += 1) out += text[k] === "\n" ? "\n" : " ";
      i = stop;
      continue;
    }
    if (c === "/" && next === "/") {
      while (i < text.length && text[i] !== "\n") {
        out += " ";
        i += 1;
      }
      continue;
    }
    out += c;
    i += 1;
  }
  return out;
}

function lineOf(text, index) {
  let line = 1;
  for (let i = 0; i < index && i < text.length; i += 1) if (text[i] === "\n") line += 1;
  return line;
}

/* A run of JSX text between two tags. Anything that reads as code — an
   operator, a call, a brace — is dropped, because a `>` in an expression can
   look like the end of a tag. */
function looksLikeCode(s) {
  return /&&|\|\||=>|[{}();]|==|=\s|\+\+|\.\w+\(/.test(s);
}

function extractPieces(file, source) {
  const text = stripComments(source);
  const pieces = [];

  // JSX text nodes
  const jsxText = />([^<>{}]+)</g;
  let m;
  while ((m = jsxText.exec(text)) !== null) {
    const raw = m[1];
    if (!/[A-Za-z]/.test(raw)) continue;
    if (looksLikeCode(raw)) continue;
    pieces.push({ text: raw, index: m.index + 1, prop: null });
  }

  // text props: prop="..." / prop={"..."} / prop={`...`}
  const propRe = new RegExp(
    `\\b(${TEXT_PROPS.join("|")})\\s*=\\s*\\{?\\s*(["'\`])((?:[^\\\\]|\\\\.)*?)\\2`,
    "g"
  );
  while ((m = propRe.exec(text)) !== null) {
    pieces.push({ text: m[3], index: m.index, prop: m[1] });
  }

  // this codebase's own label tables: { label: "...", help: "..." }
  const objRe = new RegExp(`\\b(${TEXT_PROPS.join("|")})\\s*:\\s*(["'\`])((?:[^\\\\]|\\\\.)*?)\\2`, "g");
  while ((m = objRe.exec(text)) !== null) {
    pieces.push({ text: m[3], index: m.index, prop: m[1] });
  }

  return { text, pieces };
}

// ---------- the checks ----------

function wordRe(phrase) {
  const escaped = phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "[\\s\\u00a0]+");
  return new RegExp(`(^|[^\\w-])(${escaped})(?![\\w-])`, "i");
}

const SNAKE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g;

function checkFile(file, rel) {
  const source = readFileSync(file, "utf8");
  const { text, pieces } = extractPieces(file, source);
  const hits = [];

  for (const piece of pieces) {
    for (const entry of RETIRED) {
      if (entry.files && !entry.files.some((f) => rel.endsWith(f))) continue;
      if (entry.props && (!piece.prop || !entry.props.includes(piece.prop))) continue;
      const re = wordRe(entry.bad);
      const found = re.exec(piece.text);
      if (!found) continue;
      hits.push({
        line: lineOf(text, piece.index),
        what: `retired term "${found[2]}" — use ${entry.use}`,
        sample: piece.text.trim().slice(0, 90),
      });
    }

    let s;
    SNAKE.lastIndex = 0;
    while ((s = SNAKE.exec(piece.text)) !== null) {
      if (SNAKE_ALLOWED.has(s[0])) continue;
      hits.push({
        line: lineOf(text, piece.index),
        what: `internal name "${s[0]}" shown as text`,
        sample: piece.text.trim().slice(0, 90),
      });
    }
  }

  return hits;
}

// ---------- run ----------

const files = walk(srcDir).sort();
let total = 0;

for (const file of files) {
  const rel = relative(root, file).split(sep).join("/");
  if (CHECK_ALLOW_FILES.some((a) => rel.endsWith(a))) continue;
  const hits = checkFile(file, rel);
  const seen = new Set();
  for (const hit of hits) {
    const id = `${hit.line}:${hit.what}`;
    if (seen.has(id)) continue;
    seen.add(id);
    total += 1;
    console.log(`${rel}:${hit.line}  ${hit.what}`);
    console.log(`    ${hit.sample}`);
  }
}

if (total > 0) {
  console.log(`\ncheck-terms: ${total} problem${total === 1 ? "" : "s"}.`);
  console.log("Every word a user reads comes from src/glossary.js. See docs/GLOSSARY.md.");
  process.exit(1);
}

console.log(`check-terms: ${files.length} files, no retired terms.`);
