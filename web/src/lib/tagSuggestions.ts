import type { DictionaryEntry } from "./api";
const normalize = (name: string) =>
  name.normalize("NFC").toLocaleLowerCase().replace(/\s+/g, "");
function distance(a: string, b: string): number {
  let row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 0; i < a.length; i++) {
    const next = [i + 1];
    for (let j = 0; j < b.length; j++)
      next.push(
        Math.min(next[j] + 1, row[j + 1] + 1, row[j] + Number(a[i] !== b[j])),
      );
    row = next;
  }
  return row[b.length];
}
export function tagSuggestions(entries: DictionaryEntry[]) {
  const pairs: {
    source: DictionaryEntry;
    target: DictionaryEntry;
    reason: string;
  }[] = [];
  for (let i = 0; i < entries.length; i++) {
    for (const other of entries.slice(i + 1)) {
      const entry = entries[i];
      const a = normalize(entry.name),
        b = normalize(other.name);
      const overlap =
        entry.aliases.some((alias) =>
          [other.name, ...other.aliases].some(
            (name) => normalize(alias) === normalize(name),
          ),
        ) || other.aliases.some((alias) => normalize(alias) === a);
      const reason =
        a === b
          ? "띄어쓰기"
          : overlap
            ? "별칭 겹침"
            : Math.min(a.length, b.length) >= 2 &&
                Math.abs(a.length - b.length) <= 1 &&
                distance(a, b) === 1
              ? "비슷한 이름"
              : "";
      if (reason) {
        const [source, target] =
          entry.usage_count >= other.usage_count
            ? [other, entry]
            : [entry, other];
        pairs.push({ source, target, reason });
      }
    }
  }
  return pairs;
}
