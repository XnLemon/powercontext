import { describe, expect, it } from "vitest";
import { decodeCitation, encodeCitation } from "./types.js";

describe("PowerContext citations", () => {
  it("round trips exact citations", () => {
    const citation = {
      memory_ref: { family: "memory", artifact_id: "memory-1", revision: 3 },
      entry_id: "entry-1",
      entry_version_id: "entry-1-v2",
    };
    expect(decodeCitation(encodeCitation(citation))).toEqual(citation);
  });

  it("rejects model-authored arbitrary citation strings", () => {
    expect(() => decodeCitation("../MEMORY.md")).toThrow(/exact powercontext citation/u);
  });
});
