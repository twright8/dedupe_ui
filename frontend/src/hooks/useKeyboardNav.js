/* ============================================================
   useKeyboardNav — keyboard shortcuts for diff-mode review
   ============================================================ */

import { useEffect } from "react";

/**
 * Binds keyboard shortcuts for navigating a review queue and applying labels.
 *
 * Keys:
 *   J / ArrowDown  — select next item
 *   K / ArrowUp    — select previous item
 *   T / Y          — label TRUE
 *   F / N          — label FALSE
 *   U              — clear label
 *
 * Ignores keypresses when focus is inside a textarea or input.
 *
 * @param {Object} opts
 * @param {Array}    opts.items       — filtered item list
 * @param {string}   opts.selectedId  — currently selected item ID
 * @param {Function} opts.setSelectedId — setter for selected ID
 * @param {Function} opts.onLabel     — (matchId, value) => void
 * @param {boolean}  opts.enabled     — only active when true (diff mode)
 */
export function useKeyboardNav({ items, selectedId, setSelectedId, onLabel, enabled }) {
  useEffect(() => {
    if (!enabled) return;

    function onKey(e) {
      // Do not intercept when typing in an input or textarea
      const tag = e.target.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return;

      const currentIndex = items.findIndex(m => (m.id || m.match_id) === selectedId);
      const currentItem = items[currentIndex];

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        const next = items[Math.min(items.length - 1, currentIndex + 1)];
        if (next) setSelectedId(next.id || next.match_id);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        const prev = items[Math.max(0, currentIndex - 1)];
        if (prev) setSelectedId(prev.id || prev.match_id);
      } else if (e.key === "t" || e.key === "y") {
        if (currentItem) onLabel(currentItem.id || currentItem.match_id, "TRUE");
      } else if (e.key === "f" || e.key === "n") {
        if (currentItem) onLabel(currentItem.id || currentItem.match_id, "FALSE");
      } else if (e.key === "u") {
        if (currentItem) onLabel(currentItem.id || currentItem.match_id, null);
      }
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selectedId, setSelectedId, onLabel, enabled]);
}
