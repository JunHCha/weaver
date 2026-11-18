import type { DictionaryEntry } from "./api";
export const normalizeTag = (name: string) =>
  name.normalize("NFC").toLocaleLowerCase().replace(/\s+/g, "");
export const pairKey = (a: string, b: string) =>
  JSON.stringify([normalizeTag(a), normalizeTag(b)].sort());
export interface SuggestionPair {
  source: DictionaryEntry;
  target: DictionaryEntry;
  reason: string;
}
export interface SuggestionGroup {
  entries: DictionaryEntry[];
  pairs: SuggestionPair[];
}
/** Deletion signatures bound edit-distance candidates; never compare every pair. */
export function tagSuggestions(
  entries: DictionaryEntry[],
  dismissed: [string, string][] = [],
): SuggestionGroup[] {
  const ignored = new Set(dismissed.map(([a, b]) => pairKey(a, b)));
  const labels = entries.map((entry) => {
    const names = [entry.name, ...entry.aliases].map(normalizeTag);
    return { entry, name: names[0], labels: names };
  });
  const exact = new Map<string, number[]>(),
    signatures = new Map<string, number[]>();
  const candidates = new Map<string, string>();
  const propose = (a: number, b: number, reason: string) => {
    if (
      a === b ||
      entries[a].kind !== entries[b].kind ||
      ignored.has(JSON.stringify([labels[a].name, labels[b].name].sort()))
    )
      return;
    const key = [a, b].sort((x, y) => x - y).join(":");
    if (!candidates.has(key) || reason !== "비슷한 이름")
      candidates.set(key, reason);
  };
  labels.forEach(({ entry, name, labels: names }, i) => {
    for (const label of new Set(names)) {
      const key = entry.kind + ":" + label;
      for (const j of exact.get(key) || [])
        propose(i, j, name === labels[j].name ? "띄어쓰기" : "별칭 겹침");
      exact.set(key, [...(exact.get(key) || []), i]);
    }
    if (["person", "organization"].includes(entry.kind) || name.length < 4)
      return;
    const variants = new Set([
      name,
      ...Array.from(name, (_, at) => name.slice(0, at) + name.slice(at + 1)),
    ]);
    for (const variant of variants) {
      const key = entry.kind + ":" + variant;
      for (const j of signatures.get(key) || []) {
        const other = labels[j].name;
        if (oneEdit(name, other)) propose(i, j, "비슷한 이름");
      }
      signatures.set(key, [...(signatures.get(key) || []), i]);
    }
  });
  const parent = entries.map((_, i) => i);
  const root = (i: number): number =>
    parent[i] === i ? i : (parent[i] = root(parent[i]));
  const pairs: { a: number; b: number; pair: SuggestionPair }[] = [];
  for (const [key, reason] of candidates) {
    const [a, b] = key.split(":").map(Number);
    parent[root(a)] = root(b);
    const [source, target] =
      entries[a].usage_count >= entries[b].usage_count
        ? [entries[b], entries[a]]
        : [entries[a], entries[b]];
    pairs.push({ a, b, pair: { source, target, reason } });
  }
  const groups = new Map<number, SuggestionGroup>();
  for (const { a, b, pair } of pairs) {
    const key = root(a),
      group = groups.get(key) || { entries: [], pairs: [] };
    group.pairs.push(pair);
    for (const entry of [entries[a], entries[b]])
      if (!group.entries.includes(entry)) group.entries.push(entry);
    groups.set(key, group);
  }
  return [...groups.values()];
}
function oneEdit(a: string, b: string): boolean {
  if (a === b || Math.abs(a.length - b.length) > 1) return false;
  if (a.length > b.length) return oneEdit(b, a);
  let i = 0,
    j = 0,
    edits = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      i++;
      j++;
    } else {
      if (++edits > 1) return false;
      if (a.length === b.length) i++;
      j++;
    }
  }
  return edits + (b.length - j) + (a.length - i) === 1;
}
