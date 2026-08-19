import { isIncognitoSessionKey, parseAgentSessionKey } from "openclaw/plugin-sdk/routing";

export type PrivateSessionContext = {
  sessionKey?: string;
  chatType?: string;
};

export function isEligiblePrivateSession(
  input: string | undefined | PrivateSessionContext,
): boolean {
  const sessionKey = typeof input === "object" ? input.sessionKey : input;
  const chatType = typeof input === "object" ? input.chatType?.trim().toLowerCase() : undefined;
  if (chatType === "group" || chatType === "channel") {
    return false;
  }
  if (isIncognitoSessionKey(sessionKey)) {
    return false;
  }
  const raw = sessionKey?.trim();
  if (!raw) {
    return true;
  }
  const rest = parseAgentSessionKey(raw)?.rest ?? raw;
  const tokens = rest.toLowerCase().split(":");
  if (tokens.includes("group") || tokens.includes("channel")) {
    return false;
  }
  return chatType === undefined || chatType === "direct";
}
