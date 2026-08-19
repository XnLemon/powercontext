import { describe, expect, it } from "vitest";
import { isEligiblePrivateSession } from "./privacy.js";

describe("PowerContext privacy gate", () => {
  it("allows private sessions and rejects group/channel sessions", () => {
    expect(isEligiblePrivateSession("agent:main:telegram:direct:user-1")).toBe(true);
    expect(isEligiblePrivateSession("agent:main:discord:group:room-1")).toBe(false);
    expect(isEligiblePrivateSession("agent:main:slack:channel:room-1")).toBe(false);
  });

  it("rejects incognito sessions", () => {
    expect(isEligiblePrivateSession("agent:main:internal-session-effects:incognito-1")).toBe(false);
  });
});
