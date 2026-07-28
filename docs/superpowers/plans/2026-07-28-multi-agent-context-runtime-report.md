# Multi-Agent Context Runtime Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a source-backed Chinese long report on heterogeneous multi-agent context collaboration, publish it to Yuque, and return a verified page link.

**Architecture:** Keep the repository Markdown file as the canonical source and Yuque as a published view. Build the report from current PowerContext product definitions plus independently verified primary sources, clearly separating current capabilities, candidate designs, and research hypotheses.

**Tech Stack:** Markdown, Mermaid, official papers and repositories, official Codex and Claude Code documentation, Git, Yuque web editor

---

### Task 1: Build the evidence set

**Files:**
- Read: `docs/zh/rfcs/0001_product_definition_and_vision.md`
- Read: `docs/zh/rfcs/0014_memory_layer_design.md`
- Read: `docs/zh/rfcs/0019_local_source_memory_runtime.md`
- Reference: `docs/superpowers/specs/2026-07-28-multi-agent-context-runtime-report-design.md`

- [ ] **Step 1: Verify candidate benchmarks from primary sources**

Search official paper pages and official repositories for SWE-bench Verified, SWE-bench Multilingual, SWE-EVO or equivalent long-horizon software-engineering benchmarks, ContextBench or its actual primary-source equivalent, and public multi-agent collaboration benchmarks.

Expected result: every benchmark included in the report has an official paper or repository link; unverified names are excluded or explicitly labeled unverified.

- [ ] **Step 2: Verify host-agent integration boundaries**

Check current official Codex documentation for plugin, hook, App Server, and memory-related boundaries. Check current official Claude Code documentation for hooks, MCP, subagents, and memory or project-instruction boundaries.

Expected result: the report does not claim access to private prompts, credentials, hidden state, or unsupported cross-agent APIs.

- [ ] **Step 3: Create an evidence matrix in working notes**

For each source record its supported claim, publication or update date, evaluation target, whether it is natively multi-agent, and what remains an inference.

Expected result: all time-sensitive and externally verifiable claims in the report can be traced to a primary source.

### Task 2: Write the canonical report

**Files:**
- Create: `docs/zh/research/multi_agent_context_runtime.md`

- [ ] **Step 1: Write the executive summary and problem definition**

Cover the distinction between context capacity, relevance, freshness, consistency, provenance, and handoff quality. State the central claims as research hypotheses rather than proven facts.

Expected result: the report can be understood without the referenced ChatGPT conversation.

- [ ] **Step 2: Write the failure taxonomy and runtime architecture**

Define context fragmentation, drift, redundant exploration, stale reads, propagation of errors, implicit conflicts, oversharing, and permission leakage. Map Work, Source, Artifact, Trigger, Context View, Handoff, Revision, Evidence, and Feedback to the current PowerContext model.

Expected result: existing PowerContext concepts and proposed extensions are visually and textually distinguished.

- [ ] **Step 3: Write the heterogeneous-agent handoff design**

Describe capture, normalization, artifact construction, projection, consumption feedback, and revision flows between Codex, Claude Code, humans, and external source systems. Include a Mermaid data-flow diagram.

Expected result: the design works through open adapters and artifacts without assuming shared private state.

- [ ] **Step 4: Write lifecycle and observability sections**

Define context events, lineage, revisions, conflicts, invalidation, archiving, access policy, and measurable signals. Separate directly observable events from proxy metrics such as decision influence.

Expected result: the report includes implementable event fields and metric formulas or precise definitions.

- [ ] **Step 5: Write benchmark analysis and experiment design**

Compare public benchmarks by authority, task duration, native multi-agent support, process observability, reproducibility, and execution cost. Define baselines, ablations, stress variables, outcome metrics, collaboration metrics, and safety tests.

Expected result: an engineering team can turn the section into an evaluation backlog without inventing missing experimental groups.

- [ ] **Step 6: Write the PowerContext roadmap and research agenda**

Provide 0–3 month, 3–6 month, and 6–12 month milestones. Identify industrial-paper and research-paper questions, required evidence, limitations, and open decisions.

Expected result: the near-term MVP does not depend on a full knowledge graph or automatic conflict resolution.

- [ ] **Step 7: Add citations and diagrams**

Add 3–5 Mermaid diagrams, benchmark comparison tables, metric tables, and primary-source links adjacent to supported claims.

Expected result: no precise external claim lacks a traceable source, and diagrams explain a relationship or flow.

### Task 3: Validate and commit the report

**Files:**
- Verify: `docs/zh/research/multi_agent_context_runtime.md`

- [ ] **Step 1: Scan for placeholders and unsupported language**

Run:

```bash
rg -n 'TBD|TODO|待补|有待补充|显然|必然|业界公认' docs/zh/research/multi_agent_context_runtime.md
```

Expected: no placeholder matches; any strong claim is either softened or supported by a primary source.

- [ ] **Step 2: Check Markdown and repository formatting**

Run:

```bash
git diff --check -- docs/zh/research/multi_agent_context_runtime.md
uv run prek run --files docs/zh/research/multi_agent_context_runtime.md
```

Expected: both commands exit successfully.

- [ ] **Step 3: Build documentation strictly**

Run:

```bash
make docs-test
```

Expected: the strict documentation build exits successfully without broken links or Markdown parsing errors.

- [ ] **Step 4: Commit only the canonical report**

Run:

```bash
git add docs/zh/research/multi_agent_context_runtime.md
git commit -m "docs: add multi-agent context runtime report"
```

Expected: one commit containing only the report file.

### Task 4: Publish and verify in Yuque

**Files:**
- Source: `docs/zh/research/multi_agent_context_runtime.md`
- External destination: user-authorized Yuque knowledge base

- [ ] **Step 1: Select a safe Yuque destination**

Use an existing signed-in browser session. Prefer a personal or recently used private knowledge base. If multiple team destinations are ambiguous, stop before creating a document and ask the user to choose.

Expected: the selected destination is not public or team-wide unless that scope is clearly authorized.

- [ ] **Step 2: Create the Yuque document**

Create a document titled `面向异构多 Agent 协同的 Context Runtime：研究框架、系统设计与评测方法` and import or paste the canonical Markdown content.

Expected: the full report appears in one Yuque document with headings, tables, links, code blocks, and diagrams preserved as far as the editor supports.

- [ ] **Step 3: Save and reopen the document**

Save the document, navigate away or reload, and reopen its canonical page URL.

Expected: the title and ending section are present, no large section is truncated, and the page remains accessible in the signed-in session.

- [ ] **Step 4: Return the verified link**

Copy the canonical Yuque page URL after reopening.

Expected: the final response includes the working Yuque link and the local canonical source path.
