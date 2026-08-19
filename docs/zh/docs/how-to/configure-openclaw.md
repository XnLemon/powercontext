---
title: 配置 OpenClaw Memory
description: 将 PowerContext Memory 作为外部 OpenClaw 插件安装和配置。
---

# 配置 OpenClaw Memory

PowerContext 通过外部 Memory 插件接入 OpenClaw。插件代码由本仓库维护，
OpenClaw 继续负责会话身份、授权、transcript 和生命周期钩子，不需要修改
OpenClaw 源码。

## 从本地 checkout 安装

请使用 OpenClaw 2026.8.1-beta.2 或更高版本。更早的版本未提供本集成需要的
Memory provider Plugin API。在 PowerContext checkout 中构建插件并链接到 OpenClaw：

~~~bash
cd integrations/openclaw/plugins/memory-powercontext
pnpm install
pnpm build
openclaw plugins install --link . --force
~~~

链接会让 OpenClaw 直接指向当前 checkout，重新构建后即可使用最新运行时。
移除链接：

~~~bash
openclaw plugins uninstall memory-powercontext
~~~

## 安装本地 npm 包

需要让 OpenClaw 管理一份复制后的插件时，构建本地 npm tarball：

~~~bash
cd integrations/openclaw/plugins/memory-powercontext
pnpm pack:local
openclaw plugins install npm-pack:./artifacts/oceanbase-openclaw-memory-powercontext-0.0.1.tgz --force
~~~

该包包含编译后的 dist/index.js 运行时和 openclaw.plugin.json。发布到 npm
后，可以直接安装：

~~~bash
openclaw plugins install npm:@oceanbase/openclaw-memory-powercontext
~~~

## 配置 Memory slot

将插件设为 OpenClaw 的唯一 Memory provider，并指向运行中的 PowerContext Server：

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

如果 Server 开启鉴权，请在 Gateway 进程中导出 POWERCONTEXT_CLIENT_API_TOKEN，
然后重启 Gateway。默认按 agent 隔离 Memory；群组、频道和 incognito 会话不会
被采集或搜索。`hooks.allowConversationAccess` 是 OpenClaw 对该插件读取会话内容
的显式授权；自动召回和 Source 采集都需要该授权。

## 验证安装

~~~bash
openclaw plugins inspect memory-powercontext --runtime --json
openclaw plugins list
~~~

运行时检查应显示 Memory capability 和四个 Memory 工具。如果提示 Plugin API
不兼容，请升级 OpenClaw，或选择与当前 host 版本匹配的插件版本。
