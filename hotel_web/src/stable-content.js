// Replay frames may arrive several times a second. Keep unchanged nodes (and
// their focus/animation state) and preserve scroll when content does change.
const lastMarkup = new WeakMap();
export function setStableContent(element, markup) {
  if (lastMarkup.get(element) === markup) return false;
  const scroll = element.scrollTop;
  element.innerHTML = markup;
  lastMarkup.set(element, markup);
  element.scrollTop = scroll;
  return true;
}
