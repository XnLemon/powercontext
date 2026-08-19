import type { MemoryPluginRuntime } from "openclaw/plugin-sdk/memory-core-host-runtime-core";
import type { PowerContextConfig } from "./config.js";
import type { PowerContextClient } from "./http.js";
import { PowerContextMemoryManager } from "./manager.js";

export function createPowerContextMemoryRuntime(params: {
  getConfig: () => PowerContextConfig;
  client: PowerContextClient;
  isPrivateSession: (agentId: string, sessionKey: string | undefined) => boolean;
}): MemoryPluginRuntime {
  const managers = new Map<string, PowerContextMemoryManager>();
  return {
    async getMemorySearchManager({ agentId, purpose }) {
      if (!params.getConfig().endpoint) {
        return { manager: null, error: "PowerContext endpoint is not configured" };
      }
      let manager = managers.get(agentId);
      if (!manager) {
        manager = new PowerContextMemoryManager(
          agentId,
          params.getConfig,
          params.client,
          params.isPrivateSession,
        );
        managers.set(agentId, manager);
      }
      return {
        manager,
        debug: { backend: "builtin", purpose: purpose ?? "default", managerMs: 0 },
      };
    },
    resolveMemoryBackendConfig() {
      return { backend: "builtin" };
    },
    async authorizeSearchHits({ agentId, hits, requesterSessionKey }) {
      if (!params.isPrivateSession(agentId, requesterSessionKey)) {
        return [];
      }
      return hits.filter((hit) => hit.source !== "sessions");
    },
    async closeMemorySearchManager({ agentId }) {
      managers.delete(agentId);
    },
    async closeAllMemorySearchManagers() {
      managers.clear();
    },
  };
}
