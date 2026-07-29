import { copyFile } from "node:fs/promises";

import { expect, test, type Page } from "@playwright/test";

interface BatchTaskItem {
  status: string;
}

interface BatchTaskPage {
  items: BatchTaskItem[];
}

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

test("runs a serial multi-task batch and drills from aggregate facts into exact injections", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "报告导航" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "总体报告" })).toBeVisible();
  await expect(page.getByRole("button", { name: "运行完整评测" })).toBeVisible();
  await expect(page.getByLabel("PowerContext 版本")).toHaveValue("latest");
  await expect(page.getByLabel("测试实例")).toHaveCount(0);

  await page.getByRole("button", { name: "运行完整评测" }).click();
  await expect(page).toHaveURL(/\/report\/batch-/);
  const batchId = decodeURIComponent(new URL(page.url()).pathname.split("/")[2] ?? "");
  expect(batchId).toMatch(/^batch-/);

  await expect.poll(
    async () => page.evaluate(async (id) => {
      const response = await fetch(`/api/batches/${encodeURIComponent(id)}/tasks?limit=100`);
      const body = await response.json() as BatchTaskPage;
      return {
        running: body.items.filter((item) => item.status === "running").length,
        queued: body.items.filter((item) => item.status === "queued").length,
      };
    }, batchId),
    { intervals: [30] },
  ).toEqual({ running: 1, queued: 5 });

  await page.getByRole("link", { name: "任务详细报告" }).click();
  await page.reload();
  await expect(page.getByText("运行中")).toHaveCount(1);
  await expect(page.getByText("排队中")).toHaveCount(5);

  await expect.poll(
    async () => page.evaluate(async (id) => {
      const response = await fetch(`/api/batches/${encodeURIComponent(id)}`);
      return (await response.json() as { status: string }).status;
    }, batchId),
    { intervals: [50], timeout: 10_000 },
  ).toBe("completed");

  await page.getByRole("navigation", { name: "报告导航" }).getByRole("link", { name: "总体报告" }).click();
  await page.reload();
  const correctness = page.getByLabel("正确性汇总");
  await expect(correctness.getByText("6", { exact: true })).toBeVisible();
  await expect(correctness.getByText("50%")).toHaveCount(2);
  await expect(correctness.getByText("3 / 6 个任务")).toHaveCount(2);
  await expect(page.getByText("可比较任务 5 / 6")).toBeVisible();
  await expect(page.getByRole("link", { name: /OFF 未通过.*ON 通过.*1/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /OFF 通过.*ON 未通过.*1/ })).toBeVisible();
  await expect(page.getByText("评测执行失败 1")).toBeVisible();
  await expect(page.getByText("455")).toBeVisible();
  await expect(page.getByText("475")).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/提升|退化|验收有效|验收无效|优先分析/);

  await page.getByRole("link", { name: /OFF 通过.*ON 未通过.*1/ }).click();
  await expect(page).toHaveURL(new RegExp(`/report/${encodeURIComponent(batchId)}/tasks\\?category=off_pass_on_fail$`));
  await expect(page.getByText("e2e/repo-b")).toBeVisible();
  await expect(page.getByText("e2e/repo-a")).toHaveCount(0);
  const negativeRow = page.locator("tr", { hasText: "e2e/repo-b" });
  const taskId = (await negativeRow.locator(".task-cell-id").textContent())?.trim() ?? "";
  expect(taskId).toMatch(/^run-/);
  await negativeRow.getByRole("link", { name: `查看 ${taskId}` }).click();

  await expect(page.getByRole("heading", { name: "单任务详情" })).toBeVisible();
  await expect(page.getByText("OFF 通过")).toBeVisible();
  await expect(page.getByText("ON 未通过")).toBeVisible();
  await expect(page.getByText("FAIL_TO_PASS 0 / 1")).toBeVisible();
  await expect(page.getByRole("button", { name: /#3.*PowerContext 注入/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /#4.*PowerContext 注入/ })).toBeVisible();
  await page.getByRole("button", { name: /#3.*PowerContext 注入/ }).click();
  const eventDetail = page.getByLabel("事件详情");
  await expect(eventDetail.getByText("e2e/repo-b architecture")).toBeVisible();
  await expect(eventDetail.getByText(/memory:\/\/architecture\/1/)).toBeVisible();
  await expect(eventDetail.getByText("PowerContext recalled the repository service boundary.")).toBeVisible();

  await page.getByRole("button", { name: "OFF 时间线" }).click();
  await expect(page.getByRole("button", { name: /PowerContext 注入/ })).toHaveCount(0);
  await page.getByRole("button", { name: "ON 时间线" }).click();
  await expect(page.getByRole("button", { name: /#3.*PowerContext 注入/ })).toBeVisible();

  await page.getByRole("link", { name: "返回任务详细报告" }).click();
  await expect(page).toHaveURL(new RegExp(`category=off_pass_on_fail$`));
  await expect(page.getByText("e2e/repo-b")).toBeVisible();
  await page.reload();
  await expect(page.getByText("e2e/repo-b")).toBeVisible();
  await expect(page.getByText("e2e/repo-a")).toHaveCount(0);

  for (const width of [1440, 960]) {
    await page.setViewportSize({ width, height: 1000 });
    const dimensions = await page.evaluate(() => {
      window.scrollTo({ left: 10_000, top: 0 });
      const table = document.querySelector<HTMLElement>(".batch-table-wrap");
      const value = {
        pageScrollX: window.scrollX,
        tableClient: table?.clientWidth ?? 0,
        tableScroll: table?.scrollWidth ?? 0,
      };
      window.scrollTo({ left: 0, top: 0 });
      return value;
    });
    expect(dimensions.pageScrollX).toBe(0);
    if (width === 960) expect(dimensions.tableScroll).toBeGreaterThan(dimensions.tableClient);
  }
  const reviewScreenshot = testInfo.outputPath("batch-task-report-review.png");
  await page.screenshot({ path: reviewScreenshot, fullPage: true });
  await copyFile(reviewScreenshot, "/tmp/powercontext-batch-evaluation-console.png");
});

test("keeps task detail contextual at desktop width", async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 900 });
  await page.goto("/");
  const navigation = page.getByRole("navigation", { name: "报告导航" });
  await expect(navigation.getByRole("link", { name: "总体报告" })).toHaveAttribute("aria-current", "page");
  await expect(navigation.getByRole("link", { name: "任务详细报告" })).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("link", { name: /单任务详情/ })).toHaveCount(0);
  const layout = await page.evaluate(() => {
    window.scrollTo({ left: 10_000, top: 0 });
    const value = {
      sidebar: getComputedStyle(document.querySelector(".sidebar")!).position,
      pageScrollX: window.scrollX,
    };
    window.scrollTo({ left: 0, top: 0 });
    return value;
  });
  expect(layout.sidebar).toBe("fixed");
  expect(layout.pageScrollX).toBe(0);
});
