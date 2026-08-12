// -*- coding: utf-8 -*-
"use strict";

const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright");

const workspace = path.resolve(__dirname, "..");
const pageUrl = pathToFileURL(
  path.join(workspace, "pages", "修Bug工作台", "index.html"),
).href;
const screenshotDir = process.env.BUG_COMPANION_UI_ARTIFACTS || __dirname;
const edgePath = process.env.EDGE_PATH
  || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const issue = {
  id: "BUG-20260801-001",
  title: "插件重载后设置未生效",
  kind: "bug",
  category: "known_bug",
  issue_domain: "plugin",
  plugin_name: "示例插件",
  status: "triaged",
  severity: "high",
  verification_status: "confirmed",
  group_key: "aiocqhttp:GroupMessage:10001",
  created_at: "2026-08-01T01:00:00+08:00",
  updated_at: "2026-08-01T01:00:00+08:00",
  source: "model",
  summary: "用户在群内反馈重载后配置仍使用旧值。",
  symptoms: "保存设置并重载插件后，行为没有变化。",
  evidence: [{
    sender_name: "开发者",
    created_at: "2026-08-01T01:00:00+08:00",
    quote: "重载了但配置没生效",
  }],
  pre_review: {
    recommendation: "todo",
    reason: "现象明确，但缺少运行日志。",
    missing_information: ["补充重载日志"],
    suggested_todos: ["采集重载前后的配置快照"],
  },
};

const overview = {
  total: 255,
  open: 183,
  resolved: 50,
  pending_reviews: 153,
  open_todos: 4,
  published_knowledge: 12,
  pending_messages: 6,
  enabled_groups: 2,
  answers_sent: 8,
  by_status: { pending: 183, triaged: 20, in_progress: 2, resolved: 50 },
  by_kind: { bug: 180, question: 60, optimization: 15 },
  by_category: {
    known_bug: 20,
    fixed_bug: 30,
    faq: 20,
    compatibility: 15,
    optimization: 17,
    pending_feedback: 153,
  },
  by_issue_domain: { plugin: 200, astrbot: 30, model: 15, other: 10 },
  by_plugin_name: { "示例插件": 200 },
  recent: [issue],
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function installBridge(context, theme) {
  await context.addInitScript(({ overviewData, issueData, activeTheme }) => {
    window.AstrBotPluginPage = {
      ready: async () => {},
      apiGet: async (endpoint) => {
        if (endpoint === "page/overview") {
          return { status: "ok", data: overviewData };
        }
        if (endpoint === "page/review/queue") {
          return { status: "ok", data: { items: [issueData], total: 153 } };
        }
        if (endpoint === "page/issues/detail") {
          return { status: "ok", data: issueData };
        }
        if (endpoint === "page/settings") {
          return {
            status: "ok",
            data: {
              revision: "r1",
              collection: {},
              models: { managed_plugins: ["示例插件"] },
              answers: {},
              repository_updates: {
                enabled: true,
                repositories: ["https://github.com/menglimi/astrbot_plugin_bug_companion"],
                target_groups: ["aiocqhttp:GroupMessage:123456"],
                interval_minutes: 60,
                notify_commits: true,
                notify_releases: true,
                ai_summary_enabled: true,
                ai_summary_provider_id: "repository-summary-model",
                ai_summary_max_chars: 700,
                max_items_per_check: 5,
              },
              appearance: { theme: activeTheme },
              retention: {},
            },
          };
        }
        return { status: "ok", data: { items: [], total: 0, page: 1, pages: 1 } };
      },
      apiPost: async (endpoint, body = {}) => {
        if (endpoint === "page/issues/update") {
          return { status: "ok", data: { ...issueData, ...body, id: body.id || issueData.id } };
        }
        if (endpoint === "page/issues/create") {
          return { status: "ok", data: { ...body, id: "BUG-20260801-NEW" } };
        }
        return { status: "ok", data: { ok: true } };
      },
    };
  }, { overviewData: overview, issueData: issue, activeTheme: theme });
}

async function openEditor(page) {
  await page.locator('[data-action="new-issue"]').first().click();
  await page.locator("#issueDialog[open]").waitFor();
}

async function checkClosePaths(page) {
  await openEditor(page);
  await page.locator("#issueDialogClose").click();
  assert(!(await page.locator("#issueDialog").evaluate((node) => node.open)), "X 未关闭编辑器");

  await openEditor(page);
  await page.locator("#issueDialogCancel").click();
  assert(!(await page.locator("#issueDialog").evaluate((node) => node.open)), "取消未关闭编辑器");

  await openEditor(page);
  await page.keyboard.press("Escape");
  assert(!(await page.locator("#issueDialog").evaluate((node) => node.open)), "Escape 未关闭编辑器");

  await openEditor(page);
  await page.mouse.click(2, 2);
  assert(!(await page.locator("#issueDialog").evaluate((node) => node.open)), "点击遮罩未关闭编辑器");
}

async function checkDirtyProtection(page) {
  await openEditor(page);
  await page.locator("#editTitle").fill("尚未保存的奏折");
  await page.locator("#issueDialogClose").click();
  await page.locator("#actionDialog[open]").waitFor();
  await page.locator("#actionCancel").click();
  assert(await page.locator("#issueDialog").evaluate((node) => node.open), "继续编辑却关闭了奏折");
  assert(await page.locator("#editTitle").inputValue() === "尚未保存的奏折", "继续编辑后内容丢失");

  await page.locator("#issueDialogClose").click();
  await page.locator("#actionDialog[open]").waitFor();
  await page.locator("#actionConfirm").click();
  await page.waitForFunction(() => !document.querySelector("#issueDialog")?.open);
}

async function checkReviewActions(page) {
  const labels = await page.locator("#reviewActions .review-button").allInnerTexts();
  assert(
    JSON.stringify(labels) === JSON.stringify(["设为待办", "驳回无用", "认可问题与方案", "修改解决办法"]),
    `审批动作不符合六阶段流程：${labels.join(" / ")}`,
  );

  for (const [decision, title] of [
    ["todo", "设为待办"],
    ["reject", "驳回无用记录"],
    ["approve", "认可问题与解决办法"],
  ]) {
    await page.locator(`[data-review-decision="${decision}"]`).click();
    await page.locator("#reviewDialog[open]").waitFor();
    assert(await page.locator("#reviewDialogTitle").innerText() === title, `${title}确认文案不正确`);
    if (decision === "todo") {
      assert(await page.locator("#reviewPresetField").isVisible(), "待办确认未显示处置方案预设");
      await page.locator("#reviewPreset").selectOption("later_version");
      assert((await page.locator("#reviewNote").inputValue()).includes("后续版本进行优化"), "预设未填入处置说明");
      assert((await page.locator("#reviewTodo").inputValue()).includes("后续版本优化计划"), "预设未填入待办事项");
      await page.locator("#reviewPreset").selectOption("");
    }
    await page.locator("#reviewCancel").click();
  }

  await page.locator('[data-edit-solution]').click();
  await page.locator("#issueDialog[open]").waitFor();
  assert(await page.locator("#editorPanelResolution").isVisible(), "修改方案未直达研判处置");
  await page.waitForFunction(() => document.activeElement?.id === "editSolution");
  await page.locator("#editSolution").fill("根据日志修正后的解决办法");
  await page.locator("#saveIssue").click();
  await page.waitForFunction(() => !document.querySelector("#issueDialog")?.open);
  assert(await page.locator('[data-view-panel="overview"]').isVisible(), "修改方案后未返回当前御案");
  assert(await page.locator('[data-review-decision="approve"]').isVisible(), "修改方案后奏折不再可审批");
}

async function checkEditorBehavior(page) {
  await openEditor(page);
  await page.locator("#editorTabEvidence").click();
  assert(await page.locator("#editorPanelEvidence").isVisible(), "点击无法切换到复现影响");
  await page.locator("#editorTabEvidence").press("ArrowRight");
  assert(await page.locator("#editorPanelResolution").isVisible(), "方向键无法切换页签");

  await page.locator("#saveIssue").click();
  assert(await page.locator("#editorPanelBasics").isVisible(), "空标题保存后未返回基本案情");
  assert(await page.locator("#editTitle").getAttribute("aria-invalid") === "true", "空标题未显示错误状态");
  await page.waitForFunction(() => document.activeElement?.id === "editTitle");

  const layout = await page.locator("#issueDialog").evaluate((dialog) => {
    const form = dialog.querySelector("form");
    const body = dialog.querySelector(".issue-dialog-body");
    const footer = dialog.querySelector(".issue-dialog-actions");
    return {
      dialogOverflow: getComputedStyle(dialog).overflow,
      formOverflow: getComputedStyle(form).overflow,
      bodyOverflowY: getComputedStyle(body).overflowY,
      dialogScrolls: dialog.scrollHeight > dialog.clientHeight + 1,
      formScrolls: form.scrollHeight > form.clientHeight + 1,
      footerVisible: footer.getBoundingClientRect().bottom <= dialog.getBoundingClientRect().bottom + 1,
      bodyWidth: body.scrollWidth,
      bodyClientWidth: body.clientWidth,
    };
  });
  assert(layout.dialogOverflow === "hidden", "对话框外层未锁定滚动");
  assert(layout.formOverflow === "hidden", "表单外层未锁定滚动");
  assert(layout.bodyOverflowY === "auto", "编辑器主体不是唯一自动滚动区");
  assert(!layout.dialogScrolls && !layout.formScrolls, "编辑器出现双滚动条");
  assert(layout.footerVisible, "底部保存栏不可见");
  assert(layout.bodyWidth <= layout.bodyClientWidth + 1, "编辑器主体横向溢出");
  return layout;
}

async function checkRepositorySettings(page) {
  await page.locator('[data-view="settings"]').click();
  await page.locator('[data-view-panel="settings"]:visible').waitFor();
  const card = page.locator(".repository-settings-card");
  assert(await card.isVisible(), "仓库动态设置卡不可见");
  assert(await page.locator("#settingRepositoryUpdatesEnabled").isChecked(), "仓库动态开关未回填");
  assert((await page.locator("#settingRepositories").inputValue()).includes("bug_companion"), "仓库地址未回填");
  assert((await page.locator("#settingRepositoryTargetGroups").inputValue()).includes("GroupMessage"), "固定群 UMO 未回填");
  assert(await page.locator("#settingRepositoryAiSummaryEnabled").isChecked(), "AI 摘要开关未回填");
  assert(await page.locator("#settingRepositoryAiSummaryProvider").inputValue() === "repository-summary-model", "摘要模型未回填");
  assert(await page.locator("#settingRepositoryAiSummaryMaxChars").inputValue() === "700", "摘要字数未回填");
  await card.scrollIntoViewIfNeeded();
  const layout = await card.evaluate((node) => ({
    scrollWidth: node.scrollWidth,
    clientWidth: node.clientWidth,
    right: node.getBoundingClientRect().right,
    viewportWidth: document.documentElement.clientWidth,
  }));
  assert(layout.scrollWidth <= layout.clientWidth + 1, "仓库动态设置卡横向溢出");
  assert(layout.right <= layout.viewportWidth + 1, "仓库动态设置卡超出视口");
  return layout;
}

async function runViewport(browser, config) {
  const context = await browser.newContext({
    viewport: config.viewport,
    colorScheme: config.theme,
  });
  await installBridge(context, config.theme);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => browserErrors.push(`page: ${error.message}`));
  await page.goto(pageUrl);
  await page.waitForFunction(() => document.querySelector("#connectionState")?.textContent.includes("已连接"));

  const pageWidth = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  assert(pageWidth.scrollWidth <= pageWidth.clientWidth + 1, "工作台横向溢出");
  await checkReviewActions(page);
  await checkClosePaths(page);
  await checkDirtyProtection(page);
  const layout = await checkEditorBehavior(page);
  const screenshot = path.join(screenshotDir, `bug-companion-${config.name}.png`);
  await page.screenshot({ path: screenshot, fullPage: false });
  await page.locator("#editTitle").fill("可正常保存的奏折");
  await page.locator("#saveIssue").click();
  await page.waitForFunction(() => !document.querySelector("#issueDialog")?.open);
  assert(!(await page.locator("#actionDialog").evaluate((node) => node.open)), "保存成功后仍弹出放弃确认");
  const settingsLayout = await checkRepositorySettings(page);
  const settingsScreenshot = path.join(screenshotDir, `bug-companion-settings-${config.name}.png`);
  await page.screenshot({ path: settingsScreenshot, fullPage: false });
  assert(browserErrors.length === 0, browserErrors.join("\n"));
  await context.close();
  return { name: config.name, pageWidth, layout, screenshot, settingsLayout, settingsScreenshot };
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: edgePath });
  try {
    const results = [];
    results.push(await runViewport(browser, {
      name: "desktop-dark",
      viewport: { width: 1440, height: 1000 },
      theme: "dark",
    }));
    results.push(await runViewport(browser, {
      name: "mobile-light",
      viewport: { width: 390, height: 844 },
      theme: "light",
    }));
    process.stdout.write(`${JSON.stringify({ ok: true, results }, null, 2)}\n`);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
