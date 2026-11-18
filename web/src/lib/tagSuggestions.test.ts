import { expect, it } from "vitest";
import type { DictionaryEntry, TagKind } from "./api";
import { tagSuggestions } from "./tagSuggestions";
const entry = (
  name: string,
  kind: TagKind = "project",
  aliases: string[] = [],
): DictionaryEntry => ({
  name,
  kind,
  aliases,
  usage_count: 0,
  human_edited: true,
});
it("never suggests short person names, organizations or kind mismatches", () => {
  expect(
    tagSuggestions([
      entry("민수", "person"),
      entry("민서", "person"),
      entry("민지", "person"),
    ]),
  ).toEqual([]);
  expect(
    tagSuggestions([
      entry("합성 회사", "organization"),
      entry("합성 회서", "organization"),
    ]),
  ).toEqual([]);
  expect(
    tagSuggestions([
      entry("프로젝트알파", "project"),
      entry("프로젝트알퍼", "area"),
    ]),
  ).toEqual([]);
  expect(tagSuggestions([entry("알파"), entry("알퍼")])).toEqual([]);
});
it("allows spacing and overlapping aliases at any length and groups chains", () => {
  expect(
    tagSuggestions([entry("민 수", "person"), entry("민수", "person")]),
  ).toHaveLength(1);
  expect(
    tagSuggestions([
      entry("A", "topic", ["겹침"]),
      entry("B", "topic", ["겹침"]),
    ]),
  ).toHaveLength(1);
  const groups = tagSuggestions([
    entry("프로젝트알파"),
    entry("프로젝트알퍼"),
    entry("프로젝트알퍼 "),
  ]);
  expect(groups).toHaveLength(1);
  expect(groups[0].entries).toHaveLength(3);
});
it("suppresses dismissed pairs regardless of order and whitespace", () => {
  expect(
    tagSuggestions(
      [entry("민 수", "person"), entry("민수", "person")],
      [["민수", "민 수"]],
    ),
  ).toEqual([]);
});
it("buckets 2000 synthetic entries without quadratic full comparisons", () => {
  const entries = Array.from({ length: 2000 }, (_, i) =>
    entry(
      `project-${i.toString(36).padStart(5, "0")}-${(i * 17).toString(36)}`,
    ),
  );
  const start = performance.now();
  const groups = tagSuggestions(entries);
  expect(performance.now() - start).toBeLessThan(1500);
  expect(Array.isArray(groups)).toBe(true);
});
