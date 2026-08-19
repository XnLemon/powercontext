import { describe, expect, it } from "vitest";
import { resolvePowerContextConfig, resolvePowerContextScope } from "./config.js";

describe("PowerContext scope resolution", () => {
  it("isolates agents without using a raw session key", () => {
    const config = resolvePowerContextConfig(undefined, { scopeMode: "agent" });
    expect(resolvePowerContextScope("Research Agent", config)).toBe("openclaw:agent:Research%20Agent");
  });

  it("uses a stable opaque project identity only for one trusted project", () => {
    const config = resolvePowerContextConfig(undefined, { scopeMode: "project" });
    const first = resolvePowerContextScope("main", config, ["/workspace/project"]);
    const second = resolvePowerContextScope("main", config, ["/workspace/project"]);
    expect(first).toBe(second);
    expect(first).toMatch(/^openclaw:agent:main:project:[0-9a-f]{32}$/u);
    expect(resolvePowerContextScope("main", config, ["/a", "/b"])).toBe("openclaw:agent:main");
  });
});
