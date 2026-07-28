import { describe, expect, it } from "vitest";

import styles from "./styles.css?raw";

function declarations(selector: string): string {
  const start = styles.indexOf(`${selector} {`);
  expect(start, `missing CSS rule for ${selector}`).toBeGreaterThanOrEqual(0);
  const end = styles.indexOf("}", start);
  expect(end, `unterminated CSS rule for ${selector}`).toBeGreaterThan(start);
  return styles.slice(start, end);
}

function minimumHeight(selector: string): number {
  const match = declarations(selector).match(/min-height:\s*(\d+)px/);
  expect(match, `${selector} must declare a pixel min-height`).not.toBeNull();
  return Number(match?.[1]);
}

describe("interactive target sizing", () => {
  it.each([".filter-field select", ".text-button", ".task-link"])(
    "keeps %s at least 44px high",
    (selector) => {
      expect(minimumHeight(selector)).toBeGreaterThanOrEqual(44);
    },
  );

  it.each([".text-button", ".task-link"])("uses inline-flex alignment for %s", (selector) => {
    const rule = declarations(selector);
    expect(rule).toMatch(/display:\s*inline-flex/);
    expect(rule).toMatch(/align-items:\s*center/);
  });
});
