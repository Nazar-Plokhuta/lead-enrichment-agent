const TEXT_ENTRY_OR_OVERLAY =
  "input, textarea, select, [role='dialog'], [role='menu'], [role='listbox']"
const NATIVELY_ACTIVATABLE = "button, a[href], summary"

/**
 * True when a keystroke belongs to the element it was typed into (or to an
 * open overlay), so global single-key shortcuts must stay out of the way.
 * Buttons are deliberately *not* blocked: after clicking a filter, `j`/`k`
 * should still navigate the table.
 */
export function isShortcutBlocked(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return true
  const target = event.target
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || target.closest(TEXT_ENTRY_OR_OVERLAY) !== null
}

/** `Enter` on a focused button/link activates it natively; don't also hijack it. */
export function isNativelyActivatable(event: KeyboardEvent): boolean {
  return event.target instanceof HTMLElement && event.target.closest(NATIVELY_ACTIVATABLE) !== null
}
