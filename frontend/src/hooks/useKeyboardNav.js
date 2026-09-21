/* ============================================================
   useKeyboardNav — keyboard shortcuts for diff-mode review
   ============================================================ */

import { useEffect } from "react";

/**
 * Binds keyboard shortcuts for navigating a review queue and applying labels.
 *
 * The two answers a reviewer gives are Match and Not a match. The API still
 * takes TRUE and FALSE, so the words are mapped to those values here, at the
 * edge, and the raw values never reach the screen.
 *
 * Keys:
 *   J / ArrowDown  — select next pair
 *   K / ArrowUp    — select previous pair
 *   T / Y          — answer Match        (sent as TRUE)
 *   F / N          — answer Not a match  (sent as FALSE)
 *   U              — clear the label
 *
 * Ignores keypresses when focus is inside a textarea or input.
 *
 * @param {Object} opts
 * @param {Array}    opts.items       — filtered pair list
 * @param {string}   opts.selectedId  — currently selected pair ID
 * @param {Function} opts.setSelectedId — setter for selected ID
 * @param {Function} opts.onLabel     — (pairId, "TRUE" | "FALSE" | null) => void
 * @param {boolean}  opts.enabled     — only active when true (diff mode)
 */
export function useKeyboardNav({ items, selectedId, setSelectedId, onLabel, enabled }) {
  useEffect(() => {
    if (!enabled) return;

    function onKey(e) {
      // Do not intercept when typing in an input or textarea
      const tag = e.target.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return;

      const idOf = (m) => m.pair_id || m.id || m.match_id;
      const currentIndex = items.findIndex((m) => idOf(m) === selectedId);
      const currentItem = items[currentIndex];

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        const next = items[Math.min(items.length - 1, currentIndex + 1)];
        if (next) setSelectedId(idOf(next));
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        const prev = items[Math.max(0, currentIndex - 1)];
        if (prev) setSelectedId(idOf(prev));
      } else if (e.key === "t" || e.key === "y") {
        if (currentItem) onLabel(idOf(currentItem), "TRUE");
      } else if (e.key === "f" || e.key === "n") {
        if (currentItem) onLabel(idOf(currentItem), "FALSE");
      } else if (e.key === "u") {
        if (currentItem) onLabel(idOf(currentItem), null);
      }
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selectedId, setSelectedId, onLabel, enabled]);
}
