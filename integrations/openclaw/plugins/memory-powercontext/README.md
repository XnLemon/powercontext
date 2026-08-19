# PowerContext Memory for OpenClaw

This directory contains an external OpenClaw memory plugin. It is intentionally
owned by the PowerContext repository: installing it does not require changing
the OpenClaw source tree or bundling a fork of OpenClaw.

The plugin uses OpenClaw's public plugin SDK to register the exclusive memory
provider, while PowerContext remains the HTTP memory backend. OpenClaw remains
the authority for agent/session identity, private-session checks, prompt
lifecycle, and session transcript storage.

It requires OpenClaw 2026.8.1-beta.2 or newer. Earlier releases do not expose
the memory-provider plugin API used by this integration.

## Local development install

From this directory, install the build dependencies and build the runtime
entry. The OpenClaw SDK is supplied by the OpenClaw host at runtime; it is not
bundled into this package:

```bash
pnpm install
pnpm build
```

Then link the package into the OpenClaw installation:

```bash
openclaw plugins install --link .
```

The link is local and reversible. Remove it with:

```bash
openclaw plugins uninstall memory-powercontext
```

To test the managed npm installation path, create a local tarball:

```bash
pnpm pack:local
openclaw plugins install npm-pack:./artifacts/oceanbase-openclaw-memory-powercontext-0.0.1.tgz --force
```

The tarball contains `dist/index.js` and `openclaw.plugin.json`; it can be
installed by a released OpenClaw CLI without a checkout of this repository.
Use `npm:@oceanbase/openclaw-memory-powercontext` after publishing a release.

## OpenClaw configuration

Set the memory slot and configure the PowerContext Server in `openclaw.json`:

```json
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
          "tokenEnv": "POWERCONTEXT_CLIENT_API_TOKEN"
        }
      }
    }
  }
}
```

Start PowerContext separately, export `POWERCONTEXT_CLIENT_API_TOKEN` when
server authentication is enabled, and restart the OpenClaw Gateway.
`hooks.allowConversationAccess` is required for automatic recall and source
capture because those hooks receive conversation content. Group and channel
sessions are not captured or searched. Protected transcript recall is not
emulated by this plugin.
