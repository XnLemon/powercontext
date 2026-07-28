import { copyFile } from "node:fs/promises";

import { expect, test, type Page } from "@playwright/test";

const submit = (page: Page) => page.getByRole("button", { name: "提交测试任务" }).click();

test.beforeEach(async ({ page }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
  (page as Page & { browserErrors?: string[] }).browserErrors = browserErrors;
});

test.afterEach(async ({ page }) => {
  expect((page as Page & { browserErrors?: string[] }).browserErrors ?? []).toEqual([]);
});

test("submits a task and opens its generated report", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "评测工作台" })).toBeVisible();
  await expect(page.getByLabel("PowerContext 版本")).toHaveValue("latest");

  await submit(page);
  const submissionStatus = page.locator('.form-feedback[aria-live="polite"]');
  const taskId = (await submissionStatus.locator("strong").textContent())?.trim();
  expect(taskId).toMatch(/^run-/);
  await expect(submissionStatus.getByText(`已提交任务 ${taskId} · 队列位置：1`, { exact: true })).toBeVisible();
  const observedPhases = new Set<string>();
  const phaseLabels = new Set(["准备环境", "验证 Gold", "OFF 执行", "ON 执行", "官方评测", "生成报告"]);
  const phaseValue = page.locator(".phase-value");
  const reportLink = page.getByRole("link", { name: "查看验收报告" });
  await expect
    .poll(async () => {
      if ((await phaseValue.count()) === 1) {
        const phase = (await phaseValue.textContent())?.trim();
        if (phase && phaseLabels.has(phase)) observedPhases.add(phase);
      }
      return reportLink.count();
    }, { intervals: [40] })
    .toBe(1);
  expect([...observedPhases]).toEqual(
    expect.arrayContaining(["OFF 执行", "ON 执行", "官方评测", "生成报告"]),
  );
  await expect(page.locator("body")).not.toContainText("%");
  await expect(reportLink).toBeVisible();

  await reportLink.click();
  await expect(page).toHaveURL(`/reports/${taskId}`);
  await expect(page.getByText("验收有效")).toBeVisible();
  await expect(page.getByRole("heading", { name: /OFF · RESOLVED/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: /ON · RESOLVED/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "OFF / ON 指标对照" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "处理证据" })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看原始 Markdown" })).toHaveAttribute(
    "href",
    `/api/tasks/${taskId}/report.md`,
  );

  await page.reload();
  await expect(page.getByText("验收有效")).toBeVisible();
  await page.getByRole("link", { name: "测试任务" }).click();
  await expect(page).toHaveURL("/tasks");
  await page.goBack();
  await expect(page).toHaveURL(`/reports/${taskId}`);
  await expect(page.getByText("验收有效")).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL("/tasks");

  const desktopSize = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(desktopSize.scroll).toBeLessThanOrEqual(desktopSize.client);
  const controls = page.locator("nav a, button, input, select");
  for (let index = 0; index < (await controls.count()); index += 1) {
    const box = await controls.nth(index).boundingBox();
    if (box) expect(box.height).toBeGreaterThanOrEqual(44);
  }

  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto("/");
  const narrowLayout = await page.evaluate(() => {
    const grid = document.querySelector(".workbench-grid");
    const columns = grid === null ? "" : getComputedStyle(grid).gridTemplateColumns;
    return {
      columns,
      client: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
    };
  });
  expect(narrowLayout.columns.trim().split(/\s+/)).toHaveLength(1);
  expect(narrowLayout.scroll).toBeLessThanOrEqual(narrowLayout.client);
  const reviewScreenshot = testInfo.outputPath("console-review.png");
  await page.screenshot({ path: reviewScreenshot, fullPage: true });
  await copyFile(reviewScreenshot, "/tmp/powercontext-evaluation-console.png");
});

test("keeps exactly one task running while a second task is queued and cancellable", async ({ page }) => {
  await page.goto("/");
  await submit(page);
  const firstId = (await page.locator(".success-message strong").textContent())?.trim();
  expect(firstId).toMatch(/^run-/);

  await page.getByLabel("PowerContext 版本").fill(`commit:${"b".repeat(40)}`);
  await submit(page);
  await expect(page.locator(".success-message strong")).not.toHaveText(firstId ?? "");
  const secondId = (await page.locator(".success-message strong").textContent())?.trim();
  expect(secondId).toMatch(/^run-/);
  expect(secondId).not.toBe(firstId);

  await page.getByRole("link", { name: "测试任务" }).click();
  const firstRow = page.locator("tr", { hasText: firstId });
  const secondRow = page.locator("tr", { hasText: secondId });
  await expect(firstRow.getByText("运行中")).toBeVisible();
  await expect(secondRow.getByText("排队中")).toBeVisible();
  await expect(secondRow.getByText("队列第 1 位")).toBeVisible();
  await expect(page.locator("tr.task-row--running")).toHaveCount(1);

  page.once("dialog", (dialog) => dialog.accept());
  await secondRow.getByRole("button", { name: `取消 ${secondId}` }).click();
  await expect(secondRow.getByText("已取消")).toBeVisible();
  await expect(page.locator("tr.task-row--running")).toHaveCount(1);
});
