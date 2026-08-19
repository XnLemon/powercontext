---
title: Configure OpenClaw Memory
description: Install the PowerContext memory provider as an external OpenClaw plugin.
---

# Configure OpenClaw Memory

PowerContext integrates with OpenClaw as an external memory plugin. The plugin
lives in this repository, while OpenClaw continues to own session identity,
authorization, transcripts, and lifecycle hooks. No OpenClaw source changes
are required.

## Install from a local checkout

Use OpenClaw 2026.8.1-beta.2 or newer. Earlier releases do not expose the
memory-provider plugin API used by this integration. From the PowerContext
checkout, build the plugin and link it:

~~~bash
cd integrations/openclaw/plugins/memory-powercontext
pnpm install
pnpm build
openclaw plugins install --link . --force
~~~

The link keeps the OpenClaw installation pointed at the checkout, so rebuilding
the plugin updates the linked runtime. Remove the link with:

~~~bash
openclaw plugins uninstall memory-powercontext
~~~

## Install a local npm package

Build an npm tarball when the OpenClaw host should install a managed copy:

~~~bash
cd integrations/openclaw/plugins/memory-powercontext
pnpm pack:local
openclaw plugins install npm-pack:./artifacts/oceanbase-openclaw-memory-powercontext-0.0.1.tgz --force
~~~

The package includes the compiled dist/index.js runtime and
openclaw.plugin.json. After publishing a version to npm, install it with:

~~~bash
openclaw plugins install npm:@oceanbase/openclaw-memory-powercontext
~~~

## Configure the memory slot

Set the plugin as OpenClaw's exclusive memory provider and point it at a
running PowerContext Server:

~~~json
{
  "plugins": {
    "slots": {
      "memory": "memory-powercontext"
    },
    "entries": {
      "memory-powercontext": {
        "enabled": true,
        "hooks": {
          "allowConversationAccess": true
        },
        "config": {
          "endpoint": "http://127.0.0.1:8000",
          "tokenEnv": "POWERCONTEXT_CLIENT_API_TOKEN",
          "scopeMode": "agent",
          "autoRecall": true,
          "autoCapture": true
        }
      }
    }
  }
}
~~~

Export POWERCONTEXT_CLIENT_API_TOKEN in the Gateway process when the Server
requires authentication, then restart the Gateway. The plugin keeps agent
memory isolated by default. Group, channel, and incognito sessions are not
captured or searched. `hooks.allowConversationAccess` is an explicit OpenClaw
permission for the plugin to receive conversation content; it is required for
automatic recall and source capture.

## Verify the installation

~~~bash
openclaw plugins inspect memory-powercontext --runtime --json
openclaw plugins list
~~~

The runtime inspection should show the memory capability and the four memory
tools. If the host reports an incompatible plugin API, upgrade OpenClaw or use
a plugin release matching that host version.
