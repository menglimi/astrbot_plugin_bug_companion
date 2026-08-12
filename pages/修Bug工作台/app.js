(() => {
  "use strict";

  const state = {
    view: "",
    overview: null,
    reviews: { items: [], total: 0, selectedId: "", selectedIndex: 0, detail: null },
    todos: { items: [], total: 0, page: 1, pages: 1 },
    issues: { items: [], total: 0, page: 1, pages: 1 },
    selectedIssue: "",
    selectedIssueDetail: null,
    selectedIssues: new Set(),
    selectedInbox: new Set(),
    groups: [],
    settings: null,
    themePreference: "system",
    searchTimer: 0,
    toastTimer: 0,
    issueListRequest: 0,
    issueDetailRequest: 0,
    issueEditorRequest: 0,
    issueEditorDirty: false,
    issueEditorClosing: false,
    issueEditorReturnToReview: false,
    issueSubmitting: false,
    reviewQueueRequest: 0,
    reviewDetailRequest: 0,
    reviewSubmitting: false,
    reviewPresetDraft: { note: "", todo: "" },
    todoListRequest: 0,
    todoSubmitting: false,
    groupListRequest: 0,
    groupToggleRevision: 0,
    groupToggleUpdates: new Map(),
    settingsDirty: false,
    settingsLoading: false,
    settingsSaving: false,
    settingsRequest: 0,
    settingsEditRevision: 0,
    managedPlugins: [],
    managedPluginsLoaded: false,
    managedPluginsLoading: null,
    observedPlugins: [],
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const text = (value, fallback = "—") => String(value ?? "").trim() || fallback;
  const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[char]));
  const dateText = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value).slice(0, 16) : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  const statusText = { pending: "待整理", triaged: "已分诊", in_progress: "处理中", resolved: "已解决", wont_fix: "暂不处理", duplicate: "重复", archived: "已封存" };
  const reviewText = { pending: "待审批", approved: "已认可", todo: "待办跟踪", deferred: "待修改", rejected: "已驳回" };
  const TODO_SOLUTION_PRESETS = [
    {
      id: "later_version",
      label: "后续版本优化",
      note: "处置预案：将在后续版本进行优化。",
      todo: "纳入后续版本优化计划，并在发布后回归验证。",
    },
    {
      id: "upstream_fix",
      label: "等待上游修复",
      note: "处置预案：等待上游组件修复后再适配。",
      todo: "跟踪上游修复进度，适配后完成回归验证。",
    },
    {
      id: "docs_update",
      label: "补充文档说明",
      note: "处置预案：补充文档与配置说明。",
      todo: "补充文档、配置示例和验收说明。",
    },
    {
      id: "collect_evidence",
      label: "补充证据后再定",
      note: "处置预案：先补充证据，再确定正式解决办法。",
      todo: "补充日志、版本和复现步骤，重新评估解决办法。",
    },
  ];
  const reviewDecision = (issue) => {
    const value = String(issue?.review_decision || "").trim();
    if (reviewText[value]) return value;
    return issue?.reviewed_at ? (issue?.knowledge_published_at ? "approved" : "deferred") : "pending";
  };
  const kindText = { bug: "Bug", question: "问题", optimization: "优化" };
  const domainText = { plugin: "插件问题", astrbot: "AstrBot 问题", model: "模型问题", other: "其他问题" };
  const issueDomain = (value) => Object.prototype.hasOwnProperty.call(domainText, value) ? value : "other";
  const normalizePluginNames = (values) => {
    const seen = new Set();
    return (Array.isArray(values) ? values : [])
      .map((value) => String(value ?? "").replace(/\s+/g, " ").trim())
      .filter((value) => value && !seen.has(value) && seen.add(value));
  };
  const categoryText = {
    known_bug: "当前已知 Bug",
    fixed_bug: "历史已修复 Bug",
    faq: "常见问题",
    compatibility: "环境 / 第三方兼容",
    optimization: "优化建议",
    pending_feedback: "待确认反馈",
  };
  const verificationText = {
    unverified: "未验证",
    confirmed: "已确认",
    fix_pending: "修复待验证",
    verified: "已验证",
    not_reproducible: "暂未复现",
  };
  const severityText = { low: "低", medium: "中", high: "高", critical: "紧急" };
  const parseObject = (value) => {
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
    try {
      const parsed = JSON.parse(String(value || ""));
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (error) {
      return {};
    }
  };
  const localDateTimeValue = (value) => {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  };
  const isoDateTimeValue = (value) => {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  };

  function icons() {
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function availablePluginNames(extra = []) {
    return normalizePluginNames([...state.managedPlugins, ...state.observedPlugins, ...extra]);
  }

  function syncPluginFilterOptions(extra = []) {
    const select = $("#pluginFilter");
    if (!select) return;
    const current = select.value;
    const names = availablePluginNames([...extra, current]);
    select.innerHTML = `<option value="">全部插件</option>${names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("")}`;
    select.value = current;
    select.disabled = names.length === 0;
  }

  function syncIssuePluginControl({ selected, clearWhenDisabled = false } = {}) {
    const select = $("#editPluginName");
    const field = $("#editPluginField");
    if (!select || !field) return;
    const enabled = $("#editIssueDomain").value === "plugin";
    let current = selected === undefined ? select.value : String(selected || "").trim();
    if (!enabled && clearWhenDisabled) current = "";
    const names = normalizePluginNames([...state.managedPlugins, current]);
    select.innerHTML = `<option value="">未指定插件</option>${names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("")}`;
    select.value = current;
    select.disabled = !enabled;
    field.classList.toggle("is-disabled", !enabled);
    $("#editPluginHint").textContent = enabled
      ? (names.length ? "从设置中的管理插件选择" : "尚未配置插件名称，可先保持未指定")
      : "仅插件问题可指定具体插件";
  }

  function setManagedPlugins(values) {
    state.managedPlugins = normalizePluginNames(values);
    state.managedPluginsLoaded = true;
    syncPluginFilterOptions();
    syncIssuePluginControl();
    if (state.overview) renderPluginSignal(state.overview);
  }

  function syncRepositoryAiSummaryControls() {
    const enabled = $("#settingRepositoryAiSummaryEnabled").checked;
    const fields = $(".repository-summary-fields");
    fields.classList.toggle("is-disabled", !enabled);
    fields.setAttribute("aria-disabled", String(!enabled));
  }

  async function ensureManagedPlugins() {
    if (state.managedPluginsLoaded) return true;
    if (state.managedPluginsLoading) return state.managedPluginsLoading;
    state.managedPluginsLoading = (async () => {
      try {
        const settings = await requestEndpoint("GET", "settings");
        if (!state.settings && !state.settingsDirty) state.settings = settings;
        setManagedPlugins(settings?.models?.managed_plugins || []);
        return true;
      } catch (error) {
        return false;
      } finally {
        state.managedPluginsLoading = null;
      }
    })();
    return state.managedPluginsLoading;
  }

  function toast(message, kind = "") {
    const node = $("#toast");
    if (!node) return;
    window.clearTimeout(state.toastTimer);
    node.textContent = message;
    node.dataset.kind = kind;
    node.hidden = false;
    state.toastTimer = window.setTimeout(() => { node.hidden = true; }, 2800);
  }

  function requestAction({ title, message, confirmLabel = "确认", inputLabel = "", inputPlaceholder = "" }) {
    const dialog = $("#actionDialog");
    if (!dialog?.showModal || dialog.open) return Promise.resolve(null);
    $("#actionDialogTitle").textContent = title;
    $("#actionDialogMessage").textContent = message;
    $("#actionConfirm").textContent = confirmLabel;
    const inputField = $("#actionInputField");
    const input = $("#actionInput");
    const needsInput = Boolean(inputLabel);
    inputField.hidden = !needsInput;
    $("#actionInputLabel").textContent = inputLabel || "输入内容";
    input.value = "";
    input.placeholder = inputPlaceholder;
    input.required = needsInput;
    dialog.returnValue = "cancel";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        const confirmed = dialog.returnValue === "confirm";
        resolve(confirmed ? (needsInput ? input.value.trim() : true) : null);
      }, { once: true });
      dialog.showModal();
      icons();
      window.requestAnimationFrame(() => (needsInput ? input : $("#actionConfirm")).focus());
    });
  }

  function getBridge() {
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
    try {
      if (window.parent && window.parent !== window && window.parent.AstrBotPluginPage) {
        return window.parent.AstrBotPluginPage;
      }
    } catch (error) {
      // Cross-origin parents are expected outside AstrBot's plugin page host.
    }
    return null;
  }

  async function bridge() {
    for (let index = 0; index < 60; index += 1) {
      const api = getBridge();
      if (api?.apiGet && api?.apiPost) {
        if (typeof api.ready === "function") await api.ready();
        return api;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    throw new Error("请从 AstrBot 插件拓展页打开此页面");
  }

  async function requestEndpoint(method, path, body = {}) {
    const api = await bridge();
    const url = new URL(String(path || ""), "https://astrbot-plugin-page.local/");
    const endpoint = `page/${url.pathname.replace(/^\/+/, "")}`.replace(/\/+/g, "/");
    const params = Object.fromEntries(url.searchParams.entries());
    const result = method === "GET"
      ? await api.apiGet(endpoint, Object.keys(params).length ? params : undefined)
      : await api.apiPost(endpoint, body);
    if (result?.status === "error") {
      const error = new Error(result.message || "请求失败");
      error.code = result?.data?.error || result?.error || "";
      error.data = result?.data || {};
      throw error;
    }
    return result?.data ?? result;
  }

  function setConnection(kind, label) {
    const node = $("#connectionState");
    if (!node) return;
    node.className = `connection-state ${kind || ""}`;
    node.innerHTML = `<i data-lucide="${kind === "ready" ? "wifi" : kind === "error" ? "wifi-off" : "loader-circle"}"></i>${esc(label)}`;
    icons();
  }

  function readThemePreference() {
    try {
      const stored = window.localStorage.getItem("bug-companion-theme");
      return ["system", "light", "dark"].includes(stored) ? stored : state.themePreference;
    } catch (error) {
      return state.themePreference;
    }
  }

  function rememberThemePreference(theme) {
    const normalized = ["system", "light", "dark"].includes(theme) ? theme : "system";
    state.themePreference = normalized;
    try {
      window.localStorage.setItem("bug-companion-theme", normalized);
    } catch (error) {
      // AstrBot plugin pages run in an opaque sandbox where storage is unavailable.
    }
    return normalized;
  }

  function applyTheme(theme) {
    const preference = rememberThemePreference(theme);
    let resolved = preference;
    if (resolved === "system") resolved = window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.dataset.theme = resolved;
    const button = $("#themeButton");
    if (button) {
      const nextLabel = resolved === "dark" ? "切换到浅色主题" : "切换到深色主题";
      button.title = nextLabel;
      button.setAttribute("aria-label", nextLabel);
      button.innerHTML = `<i data-lucide="${resolved === "dark" ? "sun" : "moon"}"></i>`;
      icons();
    }
  }

  function loadView(view) {
    if (view === "overview") return loadOverview();
    if (view === "todos") return loadTodos();
    if (view === "issues") return loadIssueView();
    if (view === "inbox") return loadInbox();
    if (view === "answers") return loadAnswers();
    if (view === "groups") return loadGroups();
    if (view === "settings") return loadSettings();
    return Promise.resolve();
  }

  async function navigate(view, { forceReload = false } = {}) {
    const allowed = ["overview", "todos", "issues", "inbox", "answers", "groups", "settings"];
    const nextView = allowed.includes(view) ? view : "overview";
    const sameView = state.view === nextView && $("[data-view-panel].active")?.dataset.viewPanel === nextView;
    if (sameView && !forceReload) return;
    if (state.view === "settings" && nextView !== "settings" && state.settingsDirty) {
      const discard = await requestAction({
        title: "放弃未保存的设置？",
        message: "当前设置尚未保存，离开后本次修改会丢失。",
        confirmLabel: "放弃修改",
      });
      if (!discard) return;
      state.settingsDirty = false;
      if (state.settings) setSettingsForm(state.settings);
    }
    state.issueEditorRequest += 1;
    state.view = nextView;
    $$(".nav-item").forEach((button) => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    $$("[data-view-panel]").forEach((panel) => {
      const active = panel.dataset.viewPanel === state.view;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
      panel.classList.remove("is-entering");
      if (active) {
        void panel.offsetWidth;
        panel.classList.add("is-entering");
      }
    });
    await loadView(state.view);
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
  }

  function renderMetrics(data) {
    const cards = [
      ["待审批", data.pending_reviews, "模型已完成校对", "red", "data-view-link=\"overview\""],
      ["待办", data.open_todos, "未解决问题跟踪", "blue", "data-view-link=\"todos\""],
      ["知识库", data.published_knowledge, "人工认可后可供答疑", "teal", "data-review-link=\"published\""],
      ["收奏箱", data.pending_messages, "等待模型票拟", "amber", "data-view-link=\"inbox\""],
    ];
    $("#metricGrid").innerHTML = cards.map(([label, value, foot, tone, action]) => `<button class="metric-card ${tone}" type="button" ${action} aria-label="查看${label}"><span class="metric-label">${label}</span><strong class="metric-value">${Number(value || 0).toLocaleString()}</strong><span class="metric-foot">${foot}</span></button>`).join("");
  }

  function renderReviewQueue() {
    const items = state.reviews.items || [];
    $("#reviewQueueMeta").textContent = items.length ? `${state.reviews.total} 折` : "已阅毕";
    $("#reviewQueue").innerHTML = items.length ? items.map((issue, index) => `
      <button class="review-queue-item ${state.reviews.selectedId === issue.id ? "active" : ""}" type="button" data-review-issue="${esc(issue.id)}" aria-label="启阅 ${esc(issue.title)}">
        <strong>${esc(issue.title)}</strong>
        <small>${esc(issue.id)} · ${esc(dateText(issue.created_at))}</small>
        <span class="severity-pill queue-severity ${esc(issue.severity || "medium")}">${esc(severityText[issue.severity] || "中")}</span>
      </button>
    `).join("") : `<div class="memorial-empty" style="min-height:220px"><i data-lucide="badge-check"></i><strong>今日奏折已阅毕</strong></div>`;
    const index = items.findIndex((item) => item.id === state.reviews.selectedId);
    $("#reviewProgress").textContent = index >= 0
      ? `第 ${index + 1} / ${state.reviews.total || items.length} 折${items.length < state.reviews.total ? ` · 当前载入 ${items.length}` : ""}`
      : "尚未启卷";
    $("#previousReview").disabled = index <= 0;
    $("#nextReview").disabled = index < 0 || index >= items.length - 1;
    icons();
  }

  function proposalFor(issue) {
    return parseObject(issue.pre_review || issue.pre_review_json);
  }

  function renderReviewPaper(issue) {
    state.reviews.detail = issue || null;
    const paper = $("#reviewPaper");
    const actions = $("#reviewActions");
    if (!issue) {
      paper.innerHTML = `<div class="memorial-empty"><i data-lucide="badge-check"></i><strong>今日奏折已阅毕</strong><span>新问题经票拟后会呈于此处</span></div>`;
      actions.hidden = true;
      icons();
      return;
    }
    const proposal = proposalFor(issue);
    const recommendationText = { approve: "建议认可归档", todo: "建议设为待办", hold: "建议修改方案", reject: "建议驳回无用" };
    const suggestedTodos = Array.isArray(proposal.suggested_todos) ? proposal.suggested_todos.filter(Boolean) : [];
    const missing = Array.isArray(proposal.missing_information) ? proposal.missing_information.filter(Boolean) : [];
    const evidence = Array.isArray(issue.evidence) ? issue.evidence : [];
    const domain = issueDomain(issue.issue_domain);
    const pluginName = domain === "plugin" ? String(issue.plugin_name || "").trim() : "";
    const summary = issue.summary || issue.symptoms || "尚无摘要";
    const bodySections = [
      ["所奏", summary],
      ["现象", issue.symptoms && issue.symptoms !== summary ? issue.symptoms : ""],
      ["复现与环境", [issue.reproduction, issue.environment].filter(Boolean).join("\n")],
      ["已有处置", [issue.solution && `方案：${issue.solution}`, issue.workaround && `权宜：${issue.workaround}`].filter(Boolean).join("\n")],
    ].filter(([, value]) => value);
    paper.innerHTML = `
      <header class="memorial-head">
        <div class="memorial-seal-line">
          <span class="domain-pill ${domain}">${esc(domainText[domain])}</span>
          ${pluginName ? `<span class="plugin-pill">${esc(pluginName)}</span>` : ""}
          <span class="category-pill ${esc(issue.category || "pending_feedback")}">${esc(categoryText[issue.category] || "待确认反馈")}</span>
          <span class="severity-pill ${esc(issue.severity || "medium")}">${esc(severityText[issue.severity] || "中")}</span>
        </div>
        <h3 class="memorial-title">${esc(issue.title)}</h3>
        <p class="memorial-byline">案号 ${esc(issue.id)} · ${esc(issue.reporter_name || "群友")} 所陈 · ${esc(dateText(issue.created_at))}</p>
      </header>
      <div class="memorial-body">
        <div class="memorial-facts">
          <div class="memorial-fact"><span>拟议流程</span><strong>${esc(statusText[issue.status] || issue.status || "待整理")}</strong></div>
          <div class="memorial-fact"><span>拟议验证</span><strong>${esc(verificationText[issue.verification_status] || issue.verification_status || "未验证")}</strong></div>
          <div class="memorial-fact"><span>归属群聊</span><strong>${esc(issue.group_key || "手动记录")}</strong></div>
          <div class="memorial-fact"><span>证据 / 出现</span><strong>${evidence.length} 条 / ${Number(issue.occurrence_count || 1)} 次</strong></div>
        </div>
        ${bodySections.map(([label, value]) => `<section class="memorial-section"><h4>${esc(label)}</h4><p>${esc(value)}</p></section>`).join("")}
        <section class="proposal-box">
          <div class="proposal-title"><span>内阁票拟</span><span>${esc(recommendationText[proposal.recommendation] || "请御览裁定")}</span></div>
          <p>${esc(proposal.reason || "已据群聊证据整理分类、影响与处置线索。")}</p>
          ${missing.length ? `<ul class="proposal-list">${missing.map((item) => `<li>待补：${esc(item)}</li>`).join("")}</ul>` : ""}
          ${suggestedTodos.length ? `<ul class="proposal-list">${suggestedTodos.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
        </section>
        <details class="evidence-fold">
          <summary>原始证据 · ${evidence.length}</summary>
          ${evidence.length ? evidence.map((item) => `<blockquote><small>${esc(item.sender_name || item.sender_id || "未知")} · ${esc(dateText(item.created_at))}</small>\n${esc(item.quote || "")}</blockquote>`).join("") : `<blockquote>暂无证据摘录</blockquote>`}
        </details>
        <div class="detail-actions"><button class="quiet-button" type="button" data-edit-issue="${esc(issue.id)}"><i data-lucide="pencil"></i>补录案情</button></div>
      </div>`;
    actions.hidden = false;
    const editSolution = $("[data-edit-solution]", actions);
    if (editSolution) editSolution.dataset.editSolution = issue.id;
    icons();
  }

  async function selectReview(issueId) {
    const items = state.reviews.items || [];
    const index = items.findIndex((item) => item.id === issueId);
    if (index < 0) {
      state.reviews.selectedId = "";
      state.reviews.detail = null;
      renderReviewQueue();
      renderReviewPaper(null);
      return;
    }
    state.reviews.selectedId = issueId;
    state.reviews.selectedIndex = index;
    renderReviewQueue();
    $("#reviewPaper").innerHTML = `<div class="memorial-empty"><i data-lucide="loader-circle"></i><strong>正在启卷</strong></div>`;
    $("#reviewActions").hidden = true;
    icons();
    const requestId = ++state.reviewDetailRequest;
    try {
      const issue = await requestEndpoint("GET", `issues/detail?id=${encodeURIComponent(issueId)}`);
      if (requestId !== state.reviewDetailRequest || state.reviews.selectedId !== issueId) return;
      renderReviewPaper(issue);
    } catch (error) {
      if (requestId !== state.reviewDetailRequest) return;
      renderReviewPaper(null);
      toast(error.message || "无法启阅奏折", "error");
    }
  }

  async function applyReviewQueue(data) {
    const items = Array.isArray(data?.items) ? data.items : [];
    const previousId = state.reviews.selectedId;
    const preferredIndex = Math.min(state.reviews.selectedIndex || 0, Math.max(0, items.length - 1));
    state.reviews.items = items;
    state.reviews.total = Number(data?.total ?? items.length);
    state.reviews.selectedId = items.some((item) => item.id === previousId) ? previousId : (items[preferredIndex]?.id || "");
    $("#reviewCount").textContent = state.reviews.total.toLocaleString();
    renderReviewQueue();
    await selectReview(state.reviews.selectedId);
  }

  function moveReview(offset) {
    const index = state.reviews.items.findIndex((item) => item.id === state.reviews.selectedId);
    const target = state.reviews.items[index + offset];
    if (target) selectReview(target.id);
  }

  function openReviewDialog(decision) {
    const issue = state.reviews.detail;
    if (!issue || !["approve", "todo", "reject"].includes(decision)) return;
    const content = {
      approve: ["认可问题与解决办法", "认可当前的问题描述和解决办法，并归档为可审核使用的知识。", "确认认可"],
      todo: ["设为待办", "保留这项问题并创建后续处理事项；在问题与方案被认可前，不用于群内答疑。", "确认设为待办"],
      reject: ["驳回无用记录", "认定这项记录无用或不成立，不发布为答疑知识；原始证据仍会保留。", "确认驳回"],
    }[decision];
    const proposal = proposalFor(issue);
    const suggested = Array.isArray(proposal.suggested_todos) ? proposal.suggested_todos[0] : "";
    $("#reviewIssueId").value = issue.id;
    $("#reviewDecision").value = decision;
    $("#reviewDialogTitle").textContent = content[0];
    $("#reviewDialogMessage").textContent = `${issue.id} · ${issue.title}\n${content[1]}`;
    $("#reviewNote").value = "";
    $("#reviewTodo").value = decision === "todo" ? (suggested || "") : "";
    $("#reviewDue").value = "";
    $("#reviewPreset").value = "";
    state.reviewPresetDraft = { note: "", todo: suggested || "" };
    $("#reviewPresetField").hidden = decision !== "todo";
    $("#reviewTodoField").hidden = decision !== "todo";
    $("#reviewDueField").hidden = decision !== "todo";
    $("#reviewTodo").required = decision === "todo";
    $("#reviewPreset").value = decision === "todo" && issue.category === "optimization" && !issue.solution
      ? "later_version"
      : "";
    if ($("#reviewPreset").value) applyTodoPreset($("#reviewPreset").value);
    $("#reviewConfirm").innerHTML = `<i data-lucide="${decision === "todo" ? "list-plus" : decision === "reject" ? "ban" : "badge-check"}"></i>${esc(content[2])}`;
    const dialog = $("#reviewDialog");
    dialog.showModal();
    icons();
    window.requestAnimationFrame(() => (decision === "todo" ? $("#reviewTodo") : $("#reviewNote")).focus());
  }

  function applyTodoPreset(presetId) {
    const preset = TODO_SOLUTION_PRESETS.find((item) => item.id === presetId);
    const note = $("#reviewNote");
    const todo = $("#reviewTodo");
    const previous = state.reviewPresetDraft || { note: "", todo: "" };
    if (!preset) {
      if (note.value.trim() === previous.note) note.value = "";
      if (todo.value.trim() === previous.todo) todo.value = "";
      state.reviewPresetDraft = { note: "", todo: "" };
      return;
    }
    if (!note.value.trim() || note.value.trim() === previous.note) note.value = preset.note;
    if (!todo.value.trim() || todo.value.trim() === previous.todo) todo.value = preset.todo;
    state.reviewPresetDraft = { note: preset.note, todo: preset.todo };
  }

  async function submitReview(event) {
    if (event.submitter?.value !== "confirm") return;
    event.preventDefault();
    if (state.reviewSubmitting || !$("#reviewForm").reportValidity()) return;
    const decision = $("#reviewDecision").value;
    const payload = {
      issue_id: $("#reviewIssueId").value,
      action: decision,
      note: $("#reviewNote").value.trim(),
      todo: decision === "todo" ? {
        content: $("#reviewTodo").value.trim(),
        due_at: isoDateTimeValue($("#reviewDue").value),
      } : "",
      expected_updated_at: state.reviews.detail?.updated_at || "",
    };
    state.reviewSubmitting = true;
    $("#reviewConfirm").disabled = true;
    $$("[data-review-decision]").forEach((button) => { button.disabled = true; });
    try {
      await requestEndpoint("POST", "review/decide", payload);
      $("#reviewDialog").close();
      state.reviews.selectedId = "";
      toast({ approve: "已认可问题与解决办法", todo: "已设为待办", reject: "已驳回无用记录" }[decision]);
      await loadOverview();
    } catch (error) {
      if (error.code === "conflict") {
        const latest = error.data?.issue;
        const reviewedElsewhere = Boolean(latest?.reviewed_at);
        if (latest?.id === payload.issue_id) state.reviews.detail = latest;
        if (reviewedElsewhere || !latest) $("#reviewDialog").close();
        await loadOverview();
        toast(
          reviewedElsewhere
            ? "此折已在其他页面批阅，御案已刷新"
            : "案情已被更新，已载入最新版；请核对后再次确认",
          "info",
        );
      } else {
        toast(error.message || "批阅未能保存", "error");
      }
    } finally {
      state.reviewSubmitting = false;
      $("#reviewConfirm").disabled = false;
      $$("[data-review-decision]").forEach((button) => { button.disabled = false; });
    }
  }

  function renderStatusBars(data) {
    const statuses = ["pending", "triaged", "in_progress", "resolved", "wont_fix", "duplicate"];
    const colors = { pending: "var(--amber)", triaged: "var(--blue)", in_progress: "var(--blue)", resolved: "var(--accent)", wont_fix: "var(--faint)", duplicate: "var(--red)" };
    const counts = data.by_status || {};
    const max = Math.max(1, ...statuses.map((key) => Number(counts[key] || 0)));
    $("#statusBars").innerHTML = statuses.map((key) => `<div class="status-row"><span>${statusText[key]}</span><div class="bar-track"><div class="bar-fill" style="--bar-color:${colors[key]};width:${Math.round(Number(counts[key] || 0) / max * 100)}%"></div></div><strong>${Number(counts[key] || 0)}</strong></div>`).join("");
    $("#issueMixMeta").textContent = `${data.total || 0} 条记录`;
  }

  function renderRecent(items = []) {
    $("#recentList").innerHTML = items.length ? items.map((issue) => `<button class="compact-item" type="button" data-open-issue="${esc(issue.id)}"><span class="compact-id">${esc(issue.id)}</span><span class="compact-title">${esc(issue.title)}</span><span class="status-pill ${esc(issue.status)}">${esc(statusText[issue.status] || issue.status)}</span></button>`).join("") : `<div class="empty-state" style="min-height:150px"><span>还没有问题记录</span></div>`;
  }

  function renderPluginSignal(data) {
    const counts = data.by_plugin_name || {};
    const observed = Object.keys(counts)
      .map((name) => name.trim())
      .filter(Boolean)
      .sort((left, right) => left.localeCompare(right, "zh-CN"));
    state.observedPlugins = normalizePluginNames(observed);
    syncPluginFilterOptions();
    syncIssuePluginControl();
    const names = availablePluginNames();
    const namedTotal = Object.entries(counts).reduce((total, [name, count]) => (
      name.trim() ? total + Number(count || 0) : total
    ), 0);
    const pluginTotal = Number(data.by_issue_domain?.plugin || 0);
    const unspecified = Object.prototype.hasOwnProperty.call(counts, "")
      ? Number(counts[""] || 0)
      : Math.max(0, pluginTotal - namedTotal);
    const items = names.map((name) => ({ name, count: Number(counts[name] || 0) }));
    $("#pluginSignalGrid").innerHTML = `${items.map((item) => `<button class="signal-item plugin-signal-item" type="button" data-plugin-link="${esc(item.name)}" title="${esc(item.name)}" aria-label="查看 ${esc(item.name)} 的插件问题"><span>${esc(item.name)}</span><strong>${item.count.toLocaleString()}</strong></button>`).join("")}<div class="signal-item plugin-signal-item signal-item-static" title="尚未指定具体插件的插件问题"><span>未指定插件</span><strong>${unspecified.toLocaleString()}</strong></div>`;
  }

  function renderSignal(data) {
    const domains = data.by_issue_domain || {};
    const categories = data.by_category || {};
    $("#domainSignalGrid").innerHTML = [
      ["插件问题", domains.plugin || 0, "plugin"],
      ["AstrBot 问题", domains.astrbot || 0, "astrbot"],
      ["模型问题", domains.model || 0, "model"],
      ["其他问题", domains.other || 0, "other"],
    ].map(([label, value, key]) => `<button class="signal-item" type="button" data-domain-link="${key}" aria-label="查看${label}"><span>${label}</span><strong data-domain-signal="${key}">${Number(value).toLocaleString()}</strong></button>`).join("");
    renderPluginSignal(data);
    $("#signalGrid").innerHTML = [
      ["当前已知 Bug", categories.known_bug || 0, "known_bug"],
      ["历史已修复", categories.fixed_bug || 0, "fixed_bug"],
      ["常见问题", categories.faq || 0, "faq"],
      ["兼容问题", categories.compatibility || 0, "compatibility"],
      ["优化建议", categories.optimization || 0, "optimization"],
      ["待确认反馈", categories.pending_feedback || 0, "pending_feedback"],
    ].map(([label, value, key]) => `<button class="signal-item" type="button" data-category-link="${key}" aria-label="查看${label}"><span>${label}</span><strong data-signal="${key}">${Number(value).toLocaleString()}</strong></button>`).join("");
  }

  async function loadOverview() {
    const requestId = ++state.reviewQueueRequest;
    try {
      const [data, queue] = await Promise.all([
        requestEndpoint("GET", "overview"),
        requestEndpoint("GET", "review/queue?limit=500"),
      ]);
      if (requestId !== state.reviewQueueRequest) return;
      state.overview = data;
      renderMetrics(data);
      renderStatusBars(data);
      renderRecent(data.recent || []);
      renderSignal(data);
      $("#inboxCount").textContent = Number(data.pending_messages || 0).toLocaleString();
      $("#todoCount").textContent = Number(data.open_todos || 0).toLocaleString();
      $("#overviewDate").textContent = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
      await applyReviewQueue(queue);
      setConnection("ready", "已连接");
    } catch (error) {
      if (requestId !== state.reviewQueueRequest) return;
      setConnection("error", "连接失败");
      const fallback = { total: 0, open: 0, resolved: 0, pending_reviews: 0, open_todos: 0, published_knowledge: 0, pending_messages: 0, enabled_groups: 0, answers_sent: 0, by_status: {}, by_kind: {}, by_category: {}, by_issue_domain: {}, by_plugin_name: {}, recent: [] };
      renderMetrics(fallback);
      renderStatusBars(fallback);
      renderRecent([]);
      renderSignal(fallback);
      state.reviews = { items: [], total: 0, selectedId: "", selectedIndex: 0, detail: null };
      renderReviewQueue();
      renderReviewPaper(null);
      toast(error.message || "无法读取工作台数据", "error");
    }
  }

  function todoIsCompleted(todo) {
    return todo.status === "completed" || todo.completed === true || Number(todo.completed || 0) === 1;
  }

  function renderTodos(data) {
    const items = Array.isArray(data?.items) ? data.items : [];
    state.todos = {
      items,
      total: Number(data?.total ?? items.length),
      page: Math.max(1, Number(data?.page || 1)),
      pages: Math.max(1, Number(data?.pages || 1)),
    };
    const showingOpen = ($("#todoFilter")?.value || "open") === "open";
    const openCount = Number(data?.open ?? data?.open_count ?? (showingOpen ? items.length : state.overview?.open_todos || 0));
    if (showingOpen || data?.open !== undefined || data?.open_count !== undefined) $("#todoCount").textContent = openCount.toLocaleString();
    $("#todoMeta").textContent = `${state.todos.total.toLocaleString()} 项 · ${openCount.toLocaleString()} 项未办`;
    $("#todoList").innerHTML = items.length ? items.map((todo) => {
      const completed = todoIsCompleted(todo);
      const due = todo.due_at ? dateText(todo.due_at) : "未定限期";
      return `<article class="todo-item ${completed ? "completed" : ""}">
        <label class="todo-check-control" title="${completed ? "恢复待办" : "勾销待办"}"><input class="todo-check" type="checkbox" data-todo-toggle="${esc(todo.id)}" ${completed ? "checked" : ""} aria-label="${completed ? "恢复" : "完成"}待办" /></label>
        <div class="todo-main"><span class="todo-content">${esc(todo.content)}</span><button class="todo-issue" type="button" data-open-issue="${esc(todo.issue_id)}">${esc(todo.issue_id)} · ${esc(todo.issue_title || "关联案牍")}</button><small class="todo-mobile-meta">${esc(due)} · ${completed ? "已勾销" : "待办理"}</small></div>
        <span class="todo-date">${esc(due)}</span>
        <span class="review-pill todo-state ${completed ? "approved" : "pending"}">${completed ? "已勾销" : "待办理"}</span>
        <button class="icon-button" type="button" data-open-issue="${esc(todo.issue_id)}" title="查阅案牍" aria-label="查阅案牍"><i data-lucide="file-search"></i></button>
      </article>`;
    }).join("") : `<div class="empty-state" style="min-height:360px"><i data-lucide="list-checks"></i><strong>册中暂无此类待办</strong></div>`;
    const current = state.todos.page;
    const pages = state.todos.pages;
    $("#todoPager").innerHTML = pages > 1 ? `<button type="button" aria-label="上一页" data-todo-page="${Math.max(1, current - 1)}" ${current <= 1 ? "disabled" : ""}>‹</button>${Array.from({ length: Math.min(7, pages) }, (_, index) => { const page = pages <= 7 ? index + 1 : Math.max(1, Math.min(pages - 6, current - 3)) + index; return `<button type="button" aria-label="第 ${page} 页" ${page === current ? "class=\"active\" aria-current=\"page\"" : ""} data-todo-page="${page}">${page}</button>`; }).join("")}<button type="button" aria-label="下一页" data-todo-page="${Math.min(pages, current + 1)}" ${current >= pages ? "disabled" : ""}>›</button>` : "";
    icons();
  }

  async function loadTodos(page = state.todos.page || 1) {
    const requestId = ++state.todoListRequest;
    try {
      const status = $("#todoFilter")?.value || "open";
      const params = new URLSearchParams({ page: String(Math.max(1, Number(page) || 1)), limit: "50" });
      if (status !== "all") params.set("status", status);
      const data = await requestEndpoint("GET", `todos?${params}`);
      if (requestId !== state.todoListRequest) return;
      renderTodos(data);
    } catch (error) {
      if (requestId === state.todoListRequest) {
        renderTodos({ items: [], total: 0, open: 0, page: 1, pages: 1 });
        toast(error.message || "无法读取待办册", "error");
      }
    }
  }

  function openTodoDialog(issueId = "") {
    $("#todoIssueId").value = issueId;
    $("#todoContent").value = "";
    $("#todoDue").value = "";
    $("#todoDialog").showModal();
    window.requestAnimationFrame(() => (issueId ? $("#todoContent") : $("#todoIssueId")).focus());
  }

  async function submitTodo(event) {
    if (event.submitter?.value !== "confirm") return;
    event.preventDefault();
    if (state.todoSubmitting || !$("#todoForm").reportValidity()) return;
    state.todoSubmitting = true;
    $("#todoConfirm").disabled = true;
    try {
      await requestEndpoint("POST", "todos/create", {
        issue_id: $("#todoIssueId").value.trim(),
        content: $("#todoContent").value.trim(),
        due_at: isoDateTimeValue($("#todoDue").value),
      });
      $("#todoDialog").close();
      toast("已列入待办");
      await Promise.all([loadTodos(), loadOverview()]);
      if (state.view === "issues" && state.selectedIssue) await loadIssueDetail(state.selectedIssue);
    } catch (error) {
      toast(error.message || "待办未能保存", "error");
    } finally {
      state.todoSubmitting = false;
      $("#todoConfirm").disabled = false;
    }
  }

  async function toggleTodo(todoId, completed, control) {
    control.disabled = true;
    try {
      const result = await requestEndpoint("POST", "todos/update", { id: todoId, status: completed ? "completed" : "open" });
      toast(
        result?.review_reopened
          ? "待办已办结，问题已重呈审批"
          : completed
            ? "待办已办结"
            : "待办已恢复",
      );
      await loadTodos();
      if (state.overview) await loadOverview();
    } catch (error) {
      control.checked = !completed;
      toast(error.message || "待办状态未能保存", "error");
    } finally {
      control.disabled = false;
    }
  }

  function issueQuery() {
    const params = new URLSearchParams();
    const q = $("#issueSearch").value.trim();
    const domain = $("#domainFilter").value;
    const plugin = $("#pluginFilter").value;
    const review = $("#reviewFilter").value;
    const status = $("#statusFilter").value;
    const category = $("#categoryFilter").value;
    const verification = $("#verificationFilter").value;
    const sort = $("#sortFilter").value;
    if (q) params.set("q", q);
    if (plugin) {
      params.set("issue_domain", "plugin");
      params.set("plugin_name", plugin);
    } else if (domain) params.set("issue_domain", domain);
    if (review) params.set("review_decision", review);
    if (status) params.set("status", status);
    if (category) params.set("category", category);
    if (verification) params.set("verification_status", verification);
    if (sort) params.set("sort", sort);
    params.set("page", String(state.issues.page || 1));
    params.set("page_size", "30");
    return params.toString();
  }

  function issueFilterState() {
    return {
      q: $("#issueSearch").value.trim(),
      domain: $("#domainFilter").value,
      plugin: $("#pluginFilter").value,
      review: $("#reviewFilter").value,
      status: $("#statusFilter").value,
      category: $("#categoryFilter").value,
      verification: $("#verificationFilter").value,
      sort: $("#sortFilter").value,
    };
  }

  function updateIssueFilterUI() {
    const filters = issueFilterState();
    const active = [filters.q, filters.plugin || filters.domain, filters.review, filters.status, filters.category, filters.verification, filters.sort !== "updated_desc" ? filters.sort : ""].filter(Boolean);
    const meta = $("#activeFilterMeta");
    meta.hidden = active.length === 0;
    meta.textContent = active.length ? `已启用 ${active.length} 项条件` : "";
    const reset = $("#clearIssueFilters");
    reset.disabled = active.length === 0;
    reset.title = active.length ? "清除搜索、筛选与排序条件" : "当前没有筛选条件";
  }

  function resetIssueFilters() {
    window.clearTimeout(state.searchTimer);
    $("#issueSearch").value = "";
    $("#domainFilter").value = "";
    $("#pluginFilter").value = "";
    $("#reviewFilter").value = "";
    $("#statusFilter").value = "";
    $("#categoryFilter").value = "";
    $("#verificationFilter").value = "";
    $("#sortFilter").value = "updated_desc";
    state.issues.page = 1;
    updateIssueFilterUI();
  }

  function openIssueFilter({ domain = "", plugin = "", review = "", status = "", category = "" } = {}) {
    resetIssueFilters();
    syncPluginFilterOptions([plugin]);
    $("#domainFilter").value = plugin ? "plugin" : domain;
    $("#pluginFilter").value = plugin;
    $("#reviewFilter").value = review;
    $("#statusFilter").value = status;
    $("#categoryFilter").value = category;
    updateIssueFilterUI();
    return navigate("issues", { forceReload: true });
  }

  function updateBulkUI() {
    const bulk = $("#bulkActions");
    bulk.hidden = state.selectedIssues.size === 0;
    $("#bulkSelectionMeta").textContent = `已选择 ${state.selectedIssues.size} 条`;
  }

  function issueDomainBadge(issue) {
    const domain = issueDomain(issue.issue_domain);
    const pluginName = domain === "plugin" ? String(issue.plugin_name || "").trim() : "";
    const pluginLabel = pluginName || "未指定插件";
    const title = domain === "plugin" ? `${domainText[domain]} · ${pluginLabel}` : domainText[domain];
    return `<span class="domain-stack" title="${esc(title)}"><span class="domain-pill ${domain}">${esc(domainText[domain])}</span>${domain === "plugin" ? `<small class="plugin-name-inline">${esc(pluginLabel)}</small>` : ""}</span>`;
  }

  function renderIssueTable(data) {
    state.issues = data;
    $("#issueResultMeta").textContent = `共 ${Number(data.total || 0).toLocaleString()} 条`;
    const items = data.items || [];
    const selectableIds = new Set(
      items
        .filter((issue) => issue.status !== "duplicate" && !issue.duplicate_of)
        .map((issue) => String(issue.id)),
    );
    state.selectedIssues = new Set(
      [...state.selectedIssues].filter((id) => selectableIds.has(String(id))),
    );
    $("#issueTable").innerHTML = items.length ? `<div class="issue-row issue-header"><span></span><span>ID</span><span>标题</span><span>归属</span><span>状态</span><span>知识分类</span><span>验证</span><span>优先级</span><span>出现</span></div>${items.map((issue) => {
      const selectable = issue.status !== "duplicate" && !issue.duplicate_of;
      return `<div class="issue-row ${state.selectedIssue === issue.id ? "selected" : ""}" data-open-issue="${esc(issue.id)}" data-issue-row="${esc(issue.id)}"><span>${selectable ? `<label class="issue-check-control"><input class="issue-check" type="checkbox" data-issue-id="${esc(issue.id)}" ${state.selectedIssues.has(issue.id) ? "checked" : ""} aria-label="选择 ${esc(issue.id)}" /></label>` : ""}</span><span class="issue-id">${esc(issue.id)}</span><span class="issue-title-cell"><button class="issue-title-button" type="button" data-open-issue="${esc(issue.id)}" aria-label="打开问题 ${esc(issue.title)}"><strong>${esc(issue.title)}</strong><small>${esc(issue.summary || issue.symptoms || "暂无摘要")}</small></button></span>${issueDomainBadge(issue)}<span class="status-pill ${esc(issue.status)}">${esc(statusText[issue.status] || issue.status)}</span><span class="category-pill ${esc(issue.category || "pending_feedback")}">${esc(categoryText[issue.category] || issue.category || "待确认反馈")}</span><span class="verification-pill ${esc(issue.verification_status || "unverified")}">${esc(verificationText[issue.verification_status] || issue.verification_status || "未验证")}</span><span class="severity-pill ${esc(issue.severity)}">${esc(severityText[issue.severity] || issue.severity)}</span><span class="issue-id">${Number(issue.occurrence_count || 1)}</span></div>`;
    }).join("")}` : `<div class="empty-state" style="min-height:310px"><i data-lucide="search-x"></i><strong>没有匹配的问题</strong><span>尝试调整筛选条件或新建一条记录</span></div>`;
    updateIssueFilterUI();
    updateBulkUI();
    const pages = Math.max(1, Number(data.pages || 1));
    const current = Number(data.page || 1);
    $("#issuePager").innerHTML = pages > 1 ? `<button type="button" aria-label="上一页" data-page="${Math.max(1, current - 1)}" ${current <= 1 ? "disabled" : ""}>‹</button>${Array.from({ length: Math.min(7, pages) }, (_, index) => { const page = pages <= 7 ? index + 1 : Math.max(1, Math.min(pages - 6, current - 3)) + index; return `<button type="button" aria-label="第 ${page} 页" ${page === current ? "class=\"active\" aria-current=\"page\"" : ""} data-page="${page}">${page}</button>`; }).join("")}<button type="button" aria-label="下一页" data-page="${Math.min(pages, current + 1)}" ${current >= pages ? "disabled" : ""}>›</button>` : "";
    icons();
  }

  async function loadIssues() {
    const requestId = ++state.issueListRequest;
    try {
      const data = await requestEndpoint("GET", `issues?${issueQuery()}`);
      if (requestId !== state.issueListRequest) return;
      renderIssueTable(data);
      if (state.selectedIssue) await loadIssueDetail(state.selectedIssue);
    } catch (error) {
      if (requestId === state.issueListRequest) toast(error.message || "无法读取案牍库", "error");
    }
  }

  async function loadIssueView() {
    await ensureManagedPlugins();
    await loadIssues();
  }

  async function loadIssueDetail(issueId, reveal = false) {
    const requestId = ++state.issueDetailRequest;
    state.selectedIssue = issueId;
    try {
      const issue = await requestEndpoint("GET", `issues/detail?id=${encodeURIComponent(issueId)}`);
      if (requestId !== state.issueDetailRequest || state.selectedIssue !== issueId) return;
      renderIssueDetail(issue);
      $$("[data-issue-row]").forEach((node) => node.classList.toggle("selected", node.dataset.issueRow === issueId));
      if (reveal && window.matchMedia?.("(max-width: 1100px)").matches) {
        const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
        window.requestAnimationFrame(() => $("#issueDetailPanel").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" }));
      }
    } catch (error) {
      if (requestId === state.issueDetailRequest) toast(error.message || "无法读取问题详情", "error");
    }
  }

  function renderIssueDetail(issue) {
    state.selectedIssueDetail = issue;
    const panel = $("#issueDetailPanel");
    const isDuplicate = issue.status === "duplicate" || Boolean(issue.duplicate_of);
    const domain = issueDomain(issue.issue_domain);
    const pluginName = domain === "plugin" ? String(issue.plugin_name || "").trim() : "";
    const pluginLabel = pluginName || "未指定插件";
    const decision = reviewDecision(issue);
    const section = (label, value) => value ? `<div class="detail-section"><h4>${label}</h4><p>${esc(value)}</p></div>` : "";
    const evidence = (issue.evidence || []).map((item) => `<div class="evidence-item"><small>${esc(item.sender_name || item.sender_id || "未知发送者")} · ${esc(dateText(item.created_at))} · #${esc(item.message_id || item.raw_message_id || "")}</small><p>${esc(item.quote)}</p></div>`).join("");
    const similar = (issue.relations || []).map((item) => `<div class="similar-item"><span><strong>${esc(item.target_issue_id)} · ${esc(item.target_title || "相似问题")}</strong><small>${esc(item.target_status || "")} · ${Math.round(Number(item.score || 0) * 100)}%</small></span><button class="text-button" type="button" data-open-issue="${esc(item.target_issue_id)}">打开</button></div>`).join("");
    const audit = (issue.audit || []).slice(0, 8).map((item) => `<div class="similar-item"><span><strong>${esc(item.action || "变更")}</strong><small>${esc(item.actor || "system")} · ${esc(dateText(item.created_at))}</small></span></div>`).join("");
    const todos = (issue.todos || []).map((item) => `<div class="similar-item"><span><strong>${esc(item.content)}</strong><small>${item.status === "completed" ? "已勾销" : "待办理"}${item.due_at ? ` · 限期 ${esc(dateText(item.due_at))}` : ""}</small></span></div>`).join("");
    const versionFacts = [
      issue.introduced_version && `出现版本：${issue.introduced_version}`,
      issue.fixed_version && `修复版本：${issue.fixed_version}`,
      issue.applicable_scope && `适用范围：${issue.applicable_scope}`,
      issue.last_confirmed_at && `最后确认：${dateText(issue.last_confirmed_at)}`,
    ].filter(Boolean).join("\n");
    const tags = (issue.tags || []).length ? `标签：${issue.tags.join(" / ")}` : "";
    panel.innerHTML = `
      <div class="detail-head">
        <div>
          <span class="issue-id">${esc(issue.id)}</span>
          <h3>${esc(issue.title)}</h3>
          <div class="detail-meta">
            <span class="domain-pill ${domain}">${esc(domainText[domain])}</span>
            ${domain === "plugin" ? `<span class="plugin-pill" title="所属插件：${esc(pluginLabel)}">${esc(pluginLabel)}</span>` : ""}
            <span class="status-pill ${esc(issue.status)}">${esc(statusText[issue.status] || issue.status)}</span>
            <span class="review-pill ${esc(decision)}">${esc(reviewText[decision])}</span>
            <span class="category-pill ${esc(issue.category || "pending_feedback")}">${esc(categoryText[issue.category] || issue.category || "待确认反馈")}</span>
            <span class="verification-pill ${esc(issue.verification_status || "unverified")}">${esc(verificationText[issue.verification_status] || issue.verification_status || "未验证")}</span>
            <span class="severity-pill ${esc(issue.severity)}">${esc(severityText[issue.severity] || issue.severity)}</span>
            <span class="issue-id">${Number(issue.occurrence_count || 1)} 次出现</span>
          </div>
        </div>
        <div class="detail-actions">
          ${["deferred", "rejected"].includes(decision) && !isDuplicate ? `<button class="icon-button" type="button" title="重呈御案" aria-label="重呈御案" data-reopen-review="${esc(issue.id)}"><i data-lucide="undo-2"></i></button>` : ""}
          ${isDuplicate ? "" : `<button class="icon-button" type="button" title="合并到其他问题" data-merge-issue="${esc(issue.id)}"><i data-lucide="git-merge"></i></button>`}
          ${isDuplicate ? "" : `<button class="icon-button" type="button" title="添加待办" data-add-todo="${esc(issue.id)}"><i data-lucide="list-plus"></i></button>`}
          <button class="icon-button" type="button" title="编辑" data-edit-issue="${esc(issue.id)}"><i data-lucide="pencil"></i></button>
          ${isDuplicate ? "" : issue.status === "archived" ? `<button class="icon-button" type="button" title="解除封存" aria-label="解除封存" data-restore-issue="${esc(issue.id)}"><i data-lucide="archive-restore"></i></button>` : `<button class="icon-button" type="button" title="封存" aria-label="封存" data-archive-issue="${esc(issue.id)}"><i data-lucide="archive"></i></button>`}
        </div>
      </div>
      ${isDuplicate && issue.duplicate_of ? `<div class="detail-section"><h4>重复关联</h4><p>该记录已合并到 <button class="text-button" type="button" data-open-issue="${esc(issue.duplicate_of)}">${esc(issue.duplicate_of)}</button></p></div>` : ""}
      ${section("摘要", issue.summary)}
      ${section("版本与验证", versionFacts)}
      ${section("现象", issue.symptoms)}
      ${section("复现条件", issue.reproduction)}
      ${section("期望 / 实际", [issue.expected && `期望：${issue.expected}`, issue.actual && `实际：${issue.actual}`].filter(Boolean).join("\n"))}
      ${section("环境与影响", [issue.environment, issue.impact].filter(Boolean).join("\n"))}
      ${section("原因", issue.cause)}
      ${section("解决方案", issue.solution)}
      ${section("临时方案", issue.workaround)}
      ${section("御批与入典", [
        `批阅：${reviewText[decision]}`,
        issue.review_note && `朱批：${issue.review_note}`,
        issue.reviewed_at && `批阅时间：${dateText(issue.reviewed_at)}`,
        issue.knowledge_published_at && `入典时间：${dateText(issue.knowledge_published_at)}`,
      ].filter(Boolean).join("\n"))}
      ${section("案牍信息", [`问题归属：${domainText[domain]}`, domain === "plugin" && `所属插件：${pluginLabel}`, `粗类型：${kindText[issue.kind] || issue.kind || "Bug"}`, issue.group_key && `归属群聊：${issue.group_key}`, tags].filter(Boolean).join("\n"))}
      ${todos ? `<div class="detail-section"><h4>待办 · ${issue.todos.length}</h4>${todos}</div>` : ""}
      <div class="detail-section"><h4>原始证据 · ${issue.evidence?.length || 0}</h4>${evidence || "<p>暂无证据摘录</p>"}</div>
      ${similar ? `<div class="detail-section"><h4>相似问题</h4>${similar}</div>` : ""}
      <div class="detail-section"><h4>审计 · ${issue.audit?.length || 0}</h4>${audit || "<p>暂无变更记录</p>"}<p class="issue-id" style="margin-top:9px">创建于 ${esc(dateText(issue.created_at))} · 更新于 ${esc(dateText(issue.updated_at))} · 来源 ${esc(issue.source || "model")}</p></div>`;
    icons();
  }

  async function reopenReview(issueId) {
    let issue = state.selectedIssueDetail?.id === issueId ? state.selectedIssueDetail : null;
    if (!issue) {
      try {
        issue = await requestEndpoint("GET", `issues/detail?id=${encodeURIComponent(issueId)}`);
      } catch (error) {
        toast(error.message || "无法读取案牍", "error");
        return;
      }
    }
    if (!["deferred", "rejected"].includes(reviewDecision(issue))) {
      toast("该案牍已不是待修改或已驳回状态，页面已刷新", "info");
      await loadIssueDetail(issueId);
      return;
    }
    const confirmed = await requestAction({
      title: "重呈御案",
      message: "该案牍将回到待批奏折。原批阅与朱批仍保留在审计记录中。",
      confirmLabel: "确认重呈",
    });
    if (!confirmed) return;
    try {
      await requestEndpoint("POST", "review/reopen", {
        issue_id: issueId,
        expected_updated_at: issue.updated_at || "",
      });
      state.reviews.selectedId = issueId;
      toast("案牍已重呈御案");
      await navigate("overview", { forceReload: true });
    } catch (error) {
      if (error.code === "conflict" && error.data?.issue) {
        renderIssueDetail(error.data.issue);
        toast("案情已在其他页面更新，请核对后再重呈", "info");
      } else {
        toast(error.message || "重呈失败", "error");
      }
    }
  }

  function selectEditorTab(tabName, { focus = false } = {}) {
    const allowed = ["basics", "evidence", "resolution"];
    const selected = allowed.includes(tabName) ? tabName : "basics";
    $$('[data-editor-tab]').forEach((button) => {
      const active = button.dataset.editorTab === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    $$('[data-editor-panel]').forEach((panel) => {
      const active = panel.dataset.editorPanel === selected;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    });
    $(".issue-dialog-body")?.scrollTo({ top: 0, behavior: "auto" });
  }

  async function closeIssueDialog() {
    const dialog = $("#issueDialog");
    if (!dialog?.open || state.issueEditorClosing) return;
    if (state.issueEditorDirty) {
      state.issueEditorClosing = true;
      try {
        const discard = await requestAction({
          title: "放弃未保存的修改？",
          message: "当前奏折尚未保存，关闭后本次修改会丢失。",
          confirmLabel: "放弃修改",
        });
        if (!discard) return;
      } finally {
        state.issueEditorClosing = false;
      }
    }
    state.issueEditorDirty = false;
    dialog.close("cancel");
  }

  function fillIssueForm(issue = {}) {
    $("#editIssueId").value = issue.id || "";
    $("#dialogTitle").textContent = issue.id ? `补录 ${issue.id}` : "亲拟一折";
    ["Title", "Summary", "Symptoms", "Reproduction", "Expected", "Actual", "Environment", "Impact", "Cause", "Solution", "Workaround"].forEach((field) => { const node = $(`#edit${field}`); if (node) node.value = issue[field.toLowerCase()] || ""; });
    $("#editKind").value = issue.kind || "bug";
    $("#editIssueDomain").value = issueDomain(issue.issue_domain);
    syncIssuePluginControl({ selected: issue.plugin_name || "", clearWhenDisabled: true });
    $("#editCategory").value = issue.category || "pending_feedback";
    $("#editVerification").value = issue.verification_status || "unverified";
    $("#editSeverity").value = issue.severity || "medium";
    $("#editStatus").value = issue.status || "pending";
    $("#editStatus").disabled = issue.status === "duplicate" || Boolean(issue.duplicate_of);
    $("#editIntroducedVersion").value = issue.introduced_version || "";
    $("#editFixedVersion").value = issue.fixed_version || "";
    $("#editApplicableScope").value = issue.applicable_scope || "";
    $("#editLastConfirmedAt").value = localDateTimeValue(issue.last_confirmed_at);
    $("#editTags").value = (issue.tags || []).join(", ");
    const groupSelect = $("#editGroup");
    const currentGroup = String(issue.group_key || "");
    const options = [{ group_key: "", display_name: "不绑定群聊", enabled: false }, ...state.groups];
    if (currentGroup && !options.some((item) => String(item.group_key || "") === currentGroup)) {
      options.push({ group_key: currentGroup, display_name: currentGroup, enabled: false });
    }
    groupSelect.innerHTML = options.map((group) => {
      const key = String(group.group_key || "");
      const label = key
        ? `${group.display_name || group.group_id || key}${group.enabled ? " · 已启用" : ""}`
        : "不绑定群聊";
      return `<option value="${esc(key)}">${esc(label)}</option>`;
    }).join("");
    const enabledGroups = state.groups.filter((group) => group.enabled);
    groupSelect.value = currentGroup || (!issue.id && enabledGroups.length === 1 ? String(enabledGroups[0].group_key || "") : "");
    groupSelect.disabled = Boolean(issue.id);
    $("#editTitle").removeAttribute("aria-invalid");
    selectEditorTab("basics");
    state.issueEditorDirty = false;
  }

  async function openIssueEditor(issueId = "", { initialTab = "basics", returnToReview = false } = {}) {
    const requestId = ++state.issueEditorRequest;
    state.issueEditorReturnToReview = Boolean(issueId && returnToReview);
    const pluginsPromise = ensureManagedPlugins();
    if (!state.groups.length) {
      try {
        const data = await requestEndpoint("GET", "groups");
        if (requestId !== state.issueEditorRequest) return;
        state.groups = data.items || [];
      } catch (error) {
        if (requestId !== state.issueEditorRequest) return;
        toast(error.message || "无法读取群配置，新问题将不绑定群聊", "error");
      }
    }
    const pluginsReady = await pluginsPromise;
    if (requestId !== state.issueEditorRequest) return;
    if (!pluginsReady) toast("无法读取管理的插件名称，仍可将具体插件留空", "info");
    if (issueId) {
      try {
        const issue = await requestEndpoint("GET", `issues/detail?id=${encodeURIComponent(issueId)}`);
        if (requestId !== state.issueEditorRequest) return;
        fillIssueForm(issue);
      } catch (error) {
        if (requestId === state.issueEditorRequest) toast(error.message, "error");
        return;
      }
    } else fillIssueForm();
    if (requestId !== state.issueEditorRequest) return;
    const dialog = $("#issueDialog");
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    selectEditorTab(initialTab);
    icons();
    const focusTarget = initialTab === "resolution" ? $("#editSolution") : $("#editTitle");
    window.requestAnimationFrame(() => focusTarget?.focus());
  }

  async function saveIssue(event) {
    event.preventDefault();
    if (state.issueSubmitting) return;
    const id = $("#editIssueId").value.trim();
    const payload = {
      id,
      title: $("#editTitle").value.trim(),
      kind: $("#editKind").value,
      issue_domain: $("#editIssueDomain").value,
      plugin_name: $("#editIssueDomain").value === "plugin" ? $("#editPluginName").value : "",
      category: $("#editCategory").value,
      verification_status: $("#editVerification").value,
      severity: $("#editSeverity").value,
      status: $("#editStatus").value,
      introduced_version: $("#editIntroducedVersion").value.trim(),
      fixed_version: $("#editFixedVersion").value.trim(),
      applicable_scope: $("#editApplicableScope").value,
      last_confirmed_at: isoDateTimeValue($("#editLastConfirmedAt").value),
      summary: $("#editSummary").value,
      symptoms: $("#editSymptoms").value,
      reproduction: $("#editReproduction").value,
      expected: $("#editExpected").value,
      actual: $("#editActual").value,
      environment: $("#editEnvironment").value,
      impact: $("#editImpact").value,
      cause: $("#editCause").value,
      solution: $("#editSolution").value,
      workaround: $("#editWorkaround").value,
      tags: $("#editTags").value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    };
    if (!id) payload.group_key = $("#editGroup").value;
    if (id && payload.status === "duplicate") delete payload.status;
    if (!payload.title) {
      selectEditorTab("basics");
      $("#editTitle").setAttribute("aria-invalid", "true");
      window.requestAnimationFrame(() => $("#editTitle").focus());
      toast("请填写案牍标题", "error");
      return;
    }
    const button = $("#saveIssue");
    state.issueSubmitting = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const result = id ? await requestEndpoint("POST", "issues/update", payload) : await requestEndpoint("POST", "issues/create", payload);
      const resultId = result.id || id;
      const returnToReview = state.issueEditorReturnToReview && Boolean(resultId);
      state.selectedIssue = resultId;
      state.issueEditorDirty = false;
      $("#issueDialog").close();
      if (returnToReview) {
        state.reviews.selectedId = resultId;
        toast("解决办法已更新，请继续审批");
        await navigate("overview", { forceReload: true });
      } else {
        toast(id ? "案牍已补录" : "新奏折已保存");
        await navigate("issues", { forceReload: true });
      }
    } catch (error) {
      toast(error.message || "保存失败", "error");
    } finally {
      state.issueSubmitting = false;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  async function setIssueArchived(issueId, archived) {
    const confirmed = await requestAction({
      title: archived ? "封存案牍" : "解除封存",
      message: archived ? "封存后会保留原始证据与审计，但不再参与正常检索或群内答疑。" : "该案牍将恢复为待整理状态。",
      confirmLabel: archived ? "确认封存" : "确认恢复",
    });
    if (!confirmed) return;
    try {
      await requestEndpoint("POST", "issues/update", { id: issueId, status: archived ? "archived" : "pending" });
      toast(archived ? "案牍已封存" : "已解除封存");
      state.selectedIssue = "";
      state.issueDetailRequest += 1;
      await Promise.all([loadIssues(), loadOverview()]);
      $("#issueDetailPanel").innerHTML = `<div class="empty-state"><i data-lucide="${archived ? "archive" : "archive-restore"}"></i><strong>${archived ? "案牍已封存" : "案牍已恢复"}</strong><span>${archived ? "可通过“已封存”筛选查看" : "可继续补录与处理"}</span></div>`;
      icons();
    } catch (error) {
      toast(error.message || (archived ? "封存失败" : "恢复失败"), "error");
    }
  }

  async function loadInbox() {
    try {
      const data = await requestEndpoint("GET", "inbox?limit=250");
      const items = data.items || [];
      const total = Math.max(items.length, Number(data.total || 0));
      const visibleIds = new Set(items.map((item) => String(item.id)));
      [...state.selectedInbox].forEach((id) => { if (!visibleIds.has(String(id))) state.selectedInbox.delete(String(id)); });
      $("#inboxMeta").textContent = total > items.length ? `${total} 条待票拟消息 · 显示前 ${items.length} 条` : `${total} 条待票拟消息`;
      $("#inboxList").innerHTML = items.length ? items.map((item) => {
        const sourceLabel = item.source === "qq_history"
          ? (item.processing_approved ? "QQ 历史" : "QQ 历史 · 待确认整理")
          : "实时群聊";
        return `<label class="inbox-item"><input type="checkbox" data-inbox-id="${esc(item.id)}" ${state.selectedInbox.has(String(item.id)) ? "checked" : ""} /><span class="inbox-sender"><strong>${esc(item.sender_name || item.sender_id)}</strong><small>${sourceLabel}</small></span><span class="inbox-time">${esc(dateText(item.created_at))}</span><span class="inbox-content">${esc(item.content)}</span>${item.process_error ? `<span class="inbox-error">${esc(item.process_error)}</span>` : ""}</label>`;
      }).join("") : `<div class="empty-state" style="min-height:280px"><i data-lucide="check-check"></i><strong>待整理队列为空</strong><span>新的开发者群消息会出现在这里</span></div>`;
      $("#inboxCount").textContent = String(total);
      updateInboxSelectionUI();
      icons();
    } catch (error) { toast(error.message || "无法读取收奏箱", "error"); }
  }

  function updateInboxSelectionUI() {
    const checkboxes = $$("[data-inbox-id]");
    const selectedOnPage = checkboxes.filter((node) => state.selectedInbox.has(String(node.dataset.inboxId))).length;
    const checkAll = $("#inboxCheckAll");
    checkAll.checked = checkboxes.length > 0 && selectedOnPage === checkboxes.length;
    checkAll.indeterminate = selectedOnPage > 0 && selectedOnPage < checkboxes.length;
    const meta = $("#inboxSelectionMeta");
    meta.hidden = selectedOnPage === 0;
    meta.textContent = selectedOnPage ? `已选择 ${selectedOnPage} 条` : "";
    $("#reprocessLabel").textContent = selectedOnPage ? `票拟选中（${selectedOnPage}）` : "交内阁票拟";
  }

  async function reprocessInbox() {
    const ids = [...state.selectedInbox];
    try {
      const data = await requestEndpoint("POST", "inbox/reprocess", { message_ids: ids, all_pending: ids.length === 0 });
      if (data.ok === false) toast(data.message || "整理未完成，证据仍在待整理队列中", "error");
      else if (ids.length && !data.reset) toast("所选消息已由后台处理，无需重复整理", "info");
      else toast(ids.length ? `已呈交 ${data.reset || 0} 条消息` : "收奏箱已呈交票拟");
      state.selectedInbox.clear();
      await loadInbox();
      await loadOverview();
    } catch (error) { toast(error.message || "重新整理失败", "error"); }
  }

  async function loadAnswers() {
    try {
      const data = await requestEndpoint("GET", "answers?limit=200");
      const items = data.items || [];
      $("#answerList").innerHTML = items.length ? items.map((item) => `<article class="answer-item"><div class="answer-side"><strong>${esc(item.user_name || item.user_id || "未知用户")}</strong><span>${esc(dateText(item.created_at))}</span><br /><span>${item.sent ? "已发送" : "仅记录"}</span></div><div><div class="answer-query">${esc(item.query)}</div><div class="answer-text">${esc(item.answer)}</div><div class="detail-meta">${(item.issue_ids || []).map((id) => `<span class="kind-pill">${esc(id)}</span>`).join("")}</div></div><div class="answer-confidence"><span class="confidence-ring">${Math.round(Number(item.confidence || 0) * 100)}%</span><small>置信度</small></div></article>`).join("") : `<div class="empty-state" style="min-height:320px"><i data-lucide="message-square-off"></i><strong>还没有答疑记录</strong><span>明确求助后，答复与依据会留在这里</span></div>`;
      icons();
    } catch (error) { toast(error.message || "无法读取答疑记录", "error"); }
  }

  async function loadGroups() {
    const requestId = ++state.groupListRequest;
    const toggleRevision = state.groupToggleRevision;
    try {
      const data = await requestEndpoint("GET", "groups");
      if (
        requestId !== state.groupListRequest
        || toggleRevision !== state.groupToggleRevision
        || state.groupToggleUpdates.size
      ) return;
      state.groups = data.items || [];
      $("#groupList").innerHTML = state.groups.length ? state.groups.map((group) => `<div class="group-item"><div class="group-name"><strong>${esc(group.display_name || group.group_id || "未命名群")}</strong><small>${esc(group.umo || group.group_key)}</small></div><div class="group-stat">${Number(group.open_count || 0)} 个开放问题</div><div class="group-stat">${Number(group.issue_count || 0)} 个问题</div><label class="switch" title="切换收集"><input type="checkbox" aria-label="${esc(group.enabled ? "停用" : "启用")} ${esc(group.display_name || group.group_id || group.group_key)} 的问题收集" data-group-toggle="${esc(group.group_key)}" ${group.enabled ? "checked" : ""} /><span></span></label></div>`).join("") : `<div class="empty-state" style="min-height:300px"><i data-lucide="messages-square"></i><strong>还没有已配置的群聊</strong><span>在设置中填写目标群，或在目标群使用管理员启用命令</span></div>`;
      icons();
    } catch (error) {
      if (requestId === state.groupListRequest) toast(error.message || "无法读取群配置", "error");
    }
  }

  function setGroupEnabledState(groupKey, enabled) {
    const group = state.groups.find((item) => String(item.group_key || "") === groupKey);
    if (group) group.enabled = Boolean(enabled);
  }

  function currentGroupToggle(groupKey) {
    return $$('[data-group-toggle]').find((node) => String(node.dataset.groupToggle || "") === groupKey) || null;
  }

  function syncGroupToggleControl(groupKey, enabled) {
    const control = currentGroupToggle(groupKey);
    if (!control) return;
    const group = state.groups.find((item) => String(item.group_key || "") === groupKey);
    const label = group?.display_name || group?.group_id || groupKey;
    control.checked = Boolean(enabled);
    control.setAttribute("aria-label", `${enabled ? "停用" : "启用"} ${label} 的问题收集`);
  }

  async function queueGroupToggle(control) {
    const groupKey = String(control.dataset.groupToggle || "");
    if (!groupKey) return;
    const desired = Boolean(control.checked);
    const knownGroup = state.groups.find((item) => String(item.group_key || "") === groupKey);
    const previous = knownGroup ? Boolean(knownGroup.enabled) : !desired;
    state.groupToggleRevision += 1;
    setGroupEnabledState(groupKey, desired);
    const active = state.groupToggleUpdates.get(groupKey);
    if (active) {
      active.desired = desired;
      return;
    }
    const entry = { desired, confirmed: previous };
    state.groupToggleUpdates.set(groupKey, entry);
    try {
      while (true) {
        const requested = entry.desired;
        let failure = null;
        try {
          const result = await requestEndpoint("POST", "groups/update", {
            group_key: groupKey,
            enabled: requested,
          });
          entry.confirmed = Boolean(result.enabled);
        } catch (error) {
          failure = error;
        }
        if (entry.desired !== requested) continue;
        const latestControl = currentGroupToggle(groupKey);
        if (failure) {
          setGroupEnabledState(groupKey, entry.confirmed);
          if (latestControl) syncGroupToggleControl(groupKey, entry.confirmed);
          toast(failure.message || "无法更新群聊收集状态", "error");
        } else {
          setGroupEnabledState(groupKey, entry.confirmed);
          if (latestControl) syncGroupToggleControl(groupKey, entry.confirmed);
          toast(entry.confirmed ? "已启用收集" : "已停用收集");
        }
        break;
      }
    } finally {
      state.groupToggleUpdates.delete(groupKey);
    }
  }

  function setSettingsForm(data) {
    const collection = data.collection || {};
    const models = data.models || {};
    const answers = data.answers || {};
    const repositoryUpdates = data.repository_updates || {};
    const appearance = data.appearance || {};
    const retention = data.retention || {};
    $("#settingCollectionEnabled").checked = Boolean(collection.enabled);
    $("#settingTargetGroups").value = (collection.target_groups || []).join("\n");
    $("#settingQuietSeconds").value = collection.quiet_seconds ?? 12;
    $("#settingBatchMessages").value = collection.batch_max_messages ?? 40;
    $("#settingBatchChars").value = collection.batch_max_chars ?? 10000;
    $("#settingIgnoredSenders").value = (collection.ignored_sender_ids || []).join("\n");
    $("#settingProvider").value = models.organizer_provider_id || "";
    $("#settingFallbackProvider").value = models.fallback_provider_id || "";
    $("#settingManagedPlugins").value = normalizePluginNames(models.managed_plugins || []).join("\n");
    $("#settingTimeout").value = models.timeout_seconds ?? 60;
    $("#settingMaxCandidates").value = models.max_candidates_per_batch ?? 6;
    $("#settingGuidance").value = models.custom_organizer_guidance || "";
    $("#settingAutoAnswer").checked = Boolean(answers.auto_answer);
    $("#settingConfidence").value = answers.answer_min_confidence ?? 72;
    $("#confidenceOutput").value = answers.answer_min_confidence ?? 72;
    $("#confidenceOutput").textContent = answers.answer_min_confidence ?? 72;
    $("#settingCooldown").value = answers.group_cooldown_seconds ?? 120;
    $("#settingMaxReferences").value = answers.max_reference_issues ?? 4;
    $("#settingRepositoryUpdatesEnabled").checked = Boolean(repositoryUpdates.enabled);
    $("#settingRepositories").value = (repositoryUpdates.repositories || []).join("\n");
    $("#settingRepositoryTargetGroups").value = (repositoryUpdates.target_groups || []).join("\n");
    $("#settingRepositoryInterval").value = repositoryUpdates.interval_minutes ?? 60;
    $("#settingNotifyCommits").checked = repositoryUpdates.notify_commits !== false;
    $("#settingNotifyReleases").checked = repositoryUpdates.notify_releases !== false;
    $("#settingRepositoryAiSummaryEnabled").checked = Boolean(
      repositoryUpdates.ai_summary_enabled ?? repositoryUpdates.ai_summary,
    );
    $("#settingRepositoryAiSummaryProvider").value = repositoryUpdates.ai_summary_provider_id || "";
    $("#settingRepositoryAiSummaryMaxChars").value = repositoryUpdates.ai_summary_max_chars ?? 700;
    syncRepositoryAiSummaryControls();
    $("#settingRepositoryMaxItems").value = repositoryUpdates.max_items_per_check ?? 5;
    $("#settingKeepContext").checked = retention.store_non_candidate_context !== false;
    $("#settingRawDays").value = retention.raw_message_days ?? 90;
    $("#settingTheme").value = ["system", "light", "dark"].includes(appearance.theme) ? appearance.theme : "system";
    applyTheme($("#settingTheme").value);
    setManagedPlugins(models.managed_plugins || []);
    state.settingsDirty = false;
  }

  async function loadSettings({ replaceDirty = false } = {}) {
    if (state.settingsLoading || state.settingsSaving || (state.settingsDirty && !replaceDirty)) return false;
    const requestId = ++state.settingsRequest;
    const editRevision = state.settingsEditRevision;
    state.settingsLoading = true;
    try {
      const settings = await requestEndpoint("GET", "settings");
      if (
        requestId !== state.settingsRequest
        || editRevision !== state.settingsEditRevision
        || (state.settingsDirty && !replaceDirty)
      ) return false;
      state.settings = settings;
      setSettingsForm(settings);
      return true;
    } catch (error) {
      if (requestId === state.settingsRequest) toast(error.message || "无法读取设置", "error");
      return false;
    } finally {
      state.settingsLoading = false;
    }
  }

  async function saveSettings() {
    if (state.settingsSaving || !$("#settingsForm").reportValidity()) return false;
    const lines = (selector) => $(selector).value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    const managedPlugins = normalizePluginNames(lines("#settingManagedPlugins"));
    if (managedPlugins.length > 50) {
      toast("管理的插件名称最多填写 50 个", "error");
      return false;
    }
    if (managedPlugins.some((name) => [...name].length > 120)) {
      toast("每个插件名称不能超过 120 个字符", "error");
      return false;
    }
    const payload = {
      revision: state.settings?.revision || "",
      collection: {
        enabled: $("#settingCollectionEnabled").checked,
        target_groups: lines("#settingTargetGroups"),
        quiet_seconds: Number($("#settingQuietSeconds").value),
        batch_max_messages: Number($("#settingBatchMessages").value),
        batch_max_chars: Number($("#settingBatchChars").value),
        ignored_sender_ids: lines("#settingIgnoredSenders"),
      },
      models: {
        organizer_provider_id: $("#settingProvider").value.trim(),
        fallback_provider_id: $("#settingFallbackProvider").value.trim(),
        managed_plugins: managedPlugins,
        timeout_seconds: Number($("#settingTimeout").value),
        max_candidates_per_batch: Number($("#settingMaxCandidates").value),
        custom_organizer_guidance: $("#settingGuidance").value.trim(),
      },
      answers: {
        auto_answer: $("#settingAutoAnswer").checked,
        answer_min_confidence: Number($("#settingConfidence").value),
        group_cooldown_seconds: Number($("#settingCooldown").value),
        max_reference_issues: Number($("#settingMaxReferences").value),
      },
      repository_updates: {
        enabled: $("#settingRepositoryUpdatesEnabled").checked,
        repositories: lines("#settingRepositories"),
        target_groups: lines("#settingRepositoryTargetGroups"),
        interval_minutes: Number($("#settingRepositoryInterval").value),
        notify_commits: $("#settingNotifyCommits").checked,
        notify_releases: $("#settingNotifyReleases").checked,
        ai_summary_enabled: $("#settingRepositoryAiSummaryEnabled").checked,
        ai_summary_provider_id: $("#settingRepositoryAiSummaryProvider").value.trim(),
        ai_summary_max_chars: Number($("#settingRepositoryAiSummaryMaxChars").value),
        max_items_per_check: Number($("#settingRepositoryMaxItems").value),
      },
      appearance: { theme: $("#settingTheme").value },
      retention: {
        raw_message_days: Number($("#settingRawDays").value),
        store_non_candidate_context: $("#settingKeepContext").checked,
      },
    };
    const requestId = ++state.settingsRequest;
    const editRevision = state.settingsEditRevision;
    const button = $("#saveSettings");
    state.settingsSaving = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const settings = await requestEndpoint("POST", "settings/save", payload);
      if (requestId !== state.settingsRequest) return false;
      state.settings = settings;
      setManagedPlugins(settings?.models?.managed_plugins || []);
      state.groupListRequest += 1;
      state.groups = [];
      if (editRevision === state.settingsEditRevision) {
        setSettingsForm(settings);
        toast("设置已保存");
      } else {
        state.settingsDirty = true;
        toast("提交时的设置已保存，当前修改尚未保存", "info");
      }
      return true;
    } catch (error) {
      toast(error.message || "保存设置失败", "error");
      return false;
    } finally {
      state.settingsSaving = false;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  async function exportData() {
    try {
      const data = await requestEndpoint("GET", "export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `bug-companion-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      toast("导出已开始");
    } catch (error) { toast(error.message || "导出失败", "error"); }
  }

  async function runMaintenance() {
    if (!$("#settingsForm").reportValidity()) return;
    const days = Number($("#settingRawDays").value);
    if (days === 0) {
      toast("保留天数为 0，当前设置不会清理原始消息", "info");
      return;
    }
    const confirmed = await requestAction({
      title: "清理原始消息",
      message: `将先保存当前设置，再删除超过 ${days} 天、且未被问题证据引用的已整理原始消息。问题库不会删除。`,
      confirmLabel: "保存并清理",
    });
    if (!confirmed) return;
    const button = $("#maintenanceButton");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      if (!await saveSettings()) return;
      const data = await requestEndpoint("POST", "maintenance");
      const archiveCount = Number(data.qq_history_archives_removed || 0);
      toast(`清理完成：移除 ${Number(data.raw_messages_removed || 0).toLocaleString()} 条原始消息${archiveCount ? `、${archiveCount.toLocaleString()} 个 QQ 历史归档` : ""}`);
      await loadOverview();
    } catch (error) {
      toast(error.message || "清理失败", "error");
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("click", async (event) => {
    const nav = event.target.closest("[data-view]");
    if (nav) { await navigate(nav.dataset.view); return; }
    const link = event.target.closest("[data-view-link]");
    if (link) { await navigate(link.dataset.viewLink); return; }
    const statusLink = event.target.closest("[data-status-link]");
    if (statusLink) { await openIssueFilter({ status: statusLink.dataset.statusLink }); return; }
    const reviewLink = event.target.closest("[data-review-link]");
    if (reviewLink) { await openIssueFilter({ review: reviewLink.dataset.reviewLink }); return; }
    const categoryLink = event.target.closest("[data-category-link]");
    if (categoryLink) { await openIssueFilter({ category: categoryLink.dataset.categoryLink }); return; }
    const domainLink = event.target.closest("[data-domain-link]");
    if (domainLink) { await openIssueFilter({ domain: domainLink.dataset.domainLink }); return; }
    const pluginLink = event.target.closest("[data-plugin-link]");
    if (pluginLink) { await openIssueFilter({ domain: "plugin", plugin: pluginLink.dataset.pluginLink }); return; }
    const addTodo = event.target.closest("[data-add-todo]");
    if (addTodo) { openTodoDialog(addTodo.dataset.addTodo); return; }
    const reopen = event.target.closest("[data-reopen-review]");
    if (reopen) { await reopenReview(reopen.dataset.reopenReview); return; }
    const todoPage = event.target.closest("[data-todo-page]");
    if (todoPage && !todoPage.disabled) { await loadTodos(Number(todoPage.dataset.todoPage)); return; }
    const reviewIssue = event.target.closest("[data-review-issue]");
    if (reviewIssue) { await selectReview(reviewIssue.dataset.reviewIssue); return; }
    const reviewAction = event.target.closest("[data-review-decision]");
    if (reviewAction) { openReviewDialog(reviewAction.dataset.reviewDecision); return; }
    const editSolution = event.target.closest("[data-edit-solution]");
    if (editSolution) {
      await openIssueEditor(editSolution.dataset.editSolution, { initialTab: "resolution", returnToReview: true });
      return;
    }
    const open = event.target.closest("[data-open-issue]");
    if (open) {
      const id = open.dataset.openIssue;
      if (state.view !== "issues") await navigate("issues");
      if (state.view === "issues") await loadIssueDetail(id, true);
      return;
    }
    const edit = event.target.closest("[data-edit-issue]");
    if (edit) { await openIssueEditor(edit.dataset.editIssue); return; }
    const archive = event.target.closest("[data-archive-issue]");
    if (archive) { await setIssueArchived(archive.dataset.archiveIssue, true); return; }
    const restore = event.target.closest("[data-restore-issue]");
    if (restore) { await setIssueArchived(restore.dataset.restoreIssue, false); return; }
    const merge = event.target.closest("[data-merge-issue]");
    if (merge) {
      const targetId = await requestAction({
        title: "合并重复问题",
        message: "当前问题会标记为重复，证据和出现次数将归入目标问题。",
        confirmLabel: "确认合并",
        inputLabel: "目标问题 ID",
        inputPlaceholder: "例如 BUG-000123",
      });
      if (!targetId) return;
      try {
        const result = await requestEndpoint("POST", "issues/merge", { source_ids: [merge.dataset.mergeIssue], target_id: targetId });
        toast(`已合并到 ${result.id}`);
        state.selectedIssue = result.id;
        await loadIssues();
        await loadIssueDetail(result.id);
      } catch (error) { toast(error.message || "合并失败", "error"); }
      return;
    }
    const page = event.target.closest("[data-page]");
    if (page && !page.disabled) { state.issues.page = Number(page.dataset.page); await loadIssues(); return; }
    const action = event.target.closest("[data-action]");
    if (action?.dataset.action === "new-issue") { await openIssueEditor(); return; }
    if (action?.dataset.action === "show-group-help") { toast("可填写完整 UMO，例如 aiocqhttp:GroupMessage:123456，也可直接填写群号。", "info"); return; }
    const groupToggle = event.target.closest("[data-group-toggle]");
    if (groupToggle) { await queueGroupToggle(groupToggle); return; }
  });

  $("#refreshButton").addEventListener("click", async () => {
    let replaceDirtySettings = false;
    if (state.view === "settings" && state.settingsDirty) {
      const discard = await requestAction({
        title: "重新加载设置？",
        message: "当前设置尚未保存，刷新后本次修改会丢失。",
        confirmLabel: "放弃并刷新",
      });
      if (!discard) return;
      replaceDirtySettings = true;
    }
    const currentView = state.view || "overview";
    await Promise.all([
      loadOverview(),
      currentView === "overview"
        ? Promise.resolve()
        : currentView === "settings"
          ? loadSettings({ replaceDirty: replaceDirtySettings })
          : loadView(currentView),
    ]);
  });
  $("#themeButton").addEventListener("click", () => { const current = document.documentElement.dataset.theme; applyTheme(current === "dark" ? "light" : "dark"); });
  $("#exportButton").addEventListener("click", exportData);
  $("#settingsExportButton").addEventListener("click", exportData);
  $("#maintenanceButton").addEventListener("click", runMaintenance);
  $("#issueForm").addEventListener("submit", saveIssue);
  $("#reviewForm").addEventListener("submit", submitReview);
  $("#reviewPreset").addEventListener("change", (event) => applyTodoPreset(event.target.value));
  $("#todoForm").addEventListener("submit", submitTodo);
  $("#previousReview").addEventListener("click", () => moveReview(-1));
  $("#nextReview").addEventListener("click", () => moveReview(1));
  $("#newTodoButton").addEventListener("click", () => openTodoDialog());
  $("#todoFilter").addEventListener("change", () => {
    state.todos.page = 1;
    loadTodos(1);
  });
  $("#todoList").addEventListener("change", (event) => {
    const control = event.target.closest("[data-todo-toggle]");
    if (control) toggleTodo(control.dataset.todoToggle, control.checked, control);
  });
  $("#issueTable").addEventListener("click", (event) => {
    const checkControl = event.target.closest(".issue-check-control");
    if (checkControl) event.stopPropagation();
    const checkbox = event.target.closest(".issue-check");
    if (!checkbox) return;
    if (checkbox.checked) state.selectedIssues.add(checkbox.dataset.issueId); else state.selectedIssues.delete(checkbox.dataset.issueId);
    updateBulkUI();
  });
  $("#issueTable").addEventListener("keydown", async (event) => {
    if (!["Enter", " "].includes(event.key) || event.target.closest("input, button, select, textarea")) return;
    const row = event.target.closest("[data-open-issue]");
    if (!row) return;
    event.preventDefault();
    await loadIssueDetail(row.dataset.openIssue, true);
  });
  $("#bulkActions").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-bulk-status]");
    if (!button || !state.selectedIssues.size) return;
    const submittedIds = [...state.selectedIssues];
    if (button.dataset.bulkStatus === "archived") {
      const confirmed = await requestAction({
        title: "批量封存",
        message: `将封存当前选择的 ${state.selectedIssues.size} 条案牍。记录仍可通过“已封存”筛选找回。`,
        confirmLabel: "确认封存",
      });
      if (!confirmed) return;
    }
    try {
      const data = await requestEndpoint("POST", "issues/bulk", { ids: submittedIds, changes: { status: button.dataset.bulkStatus } });
      const updated = Math.max(0, Math.min(submittedIds.length, Number(data.updated) || 0));
      const submittedSet = new Set(submittedIds);
      const updatedIds = Array.isArray(data.updated_ids)
        ? data.updated_ids.filter((id) => submittedSet.has(String(id)))
        : (updated === submittedIds.length ? submittedIds : []);
      updatedIds.forEach((id) => state.selectedIssues.delete(String(id)));
      if (updated === submittedIds.length) {
        toast(button.dataset.bulkStatus === "resolved"
          ? `已标记解决 ${updated} 条`
          : `已封存 ${updated} 条`);
      } else {
        toast(`仅更新 ${updated}/${submittedIds.length} 条，未成功项已保留选择`, "info");
      }
      await loadIssues();
      await loadOverview();
    } catch (error) { toast(error.message || "批量操作失败", "error"); }
  });
  $("#issueSearch").addEventListener("input", () => { updateIssueFilterUI(); window.clearTimeout(state.searchTimer); state.searchTimer = window.setTimeout(() => { state.issues.page = 1; loadIssues(); }, 260); });
  $("#domainFilter").addEventListener("change", () => {
    if ($("#domainFilter").value !== "plugin") $("#pluginFilter").value = "";
    updateIssueFilterUI();
    state.issues.page = 1;
    loadIssues();
  });
  $("#pluginFilter").addEventListener("change", () => {
    if ($("#pluginFilter").value) $("#domainFilter").value = "plugin";
    updateIssueFilterUI();
    state.issues.page = 1;
    loadIssues();
  });
  ["#reviewFilter", "#statusFilter", "#categoryFilter", "#verificationFilter", "#sortFilter"].forEach((selector) => $(selector).addEventListener("change", () => { updateIssueFilterUI(); state.issues.page = 1; loadIssues(); }));
  $("#clearIssueFilters").addEventListener("click", () => { resetIssueFilters(); loadIssues(); });
  $("#reprocessSelected").addEventListener("click", reprocessInbox);
  $("#inboxCheckAll").addEventListener("change", (event) => { $$("[data-inbox-id]").forEach((node) => { node.checked = event.target.checked; if (node.checked) state.selectedInbox.add(node.dataset.inboxId); else state.selectedInbox.delete(node.dataset.inboxId); }); updateInboxSelectionUI(); });
  $("#inboxList").addEventListener("change", (event) => { const node = event.target.closest("[data-inbox-id]"); if (!node) return; if (node.checked) state.selectedInbox.add(node.dataset.inboxId); else state.selectedInbox.delete(node.dataset.inboxId); updateInboxSelectionUI(); });
  $("#saveSettings").addEventListener("click", saveSettings);
  ["input", "change"].forEach((eventName) => $("#settingsForm").addEventListener(eventName, () => {
    state.settingsEditRevision += 1;
    state.settingsDirty = true;
  }));
  $("#settingConfidence").addEventListener("input", (event) => { $("#confidenceOutput").textContent = event.target.value; });
  $("#settingRepositoryAiSummaryEnabled").addEventListener("change", syncRepositoryAiSummaryControls);
  $("#settingTheme").addEventListener("change", (event) => applyTheme(event.target.value));
  $("#editIssueDomain").addEventListener("change", () => syncIssuePluginControl({ clearWhenDisabled: true }));
  $("#editTitle").addEventListener("input", () => $("#editTitle").removeAttribute("aria-invalid"));
  $(".editor-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-editor-tab]");
    if (tab) selectEditorTab(tab.dataset.editorTab);
  });
  $(".editor-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = $$('[data-editor-tab]');
    const current = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    const target = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    selectEditorTab(tabs[target].dataset.editorTab, { focus: true });
  });
  $("#actionCancel").addEventListener("click", () => $("#actionDialog").close("cancel"));
  $("#actionDialogClose").addEventListener("click", () => $("#actionDialog").close("cancel"));
  $("#reviewCancel").addEventListener("click", () => $("#reviewDialog").close("cancel"));
  $("#reviewDialogClose").addEventListener("click", () => $("#reviewDialog").close("cancel"));
  $("#todoCancel").addEventListener("click", () => $("#todoDialog").close("cancel"));
  $("#todoDialogClose").addEventListener("click", () => $("#todoDialog").close("cancel"));
  $("#issueDialogCancel").addEventListener("click", closeIssueDialog);
  $("#issueDialogClose").addEventListener("click", closeIssueDialog);
  $("#issueForm").addEventListener("input", () => { state.issueEditorDirty = true; });
  $("#issueForm").addEventListener("change", () => { state.issueEditorDirty = true; });
  $("#issueDialog").addEventListener("click", (event) => {
    if (event.target === $("#issueDialog")) closeIssueDialog();
  });
  $("#issueDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeIssueDialog();
  });
  $("#issueDialog").addEventListener("close", () => {
    state.issueEditorRequest += 1;
    state.issueEditorDirty = false;
    state.issueEditorClosing = false;
    state.issueEditorReturnToReview = false;
  });
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if (state.themePreference === "system") applyTheme("system");
  });

  async function loadInitialAppearance() {
    const requestId = state.settingsRequest;
    const editRevision = state.settingsEditRevision;
    try {
      const settings = await requestEndpoint("GET", "settings");
      if (
        requestId === state.settingsRequest
        && editRevision === state.settingsEditRevision
        && !state.settingsDirty
      ) {
        state.settings = settings;
        setManagedPlugins(settings?.models?.managed_plugins || []);
        const configured = settings?.appearance?.theme;
        if (["system", "light", "dark"].includes(configured)) applyTheme(configured);
      }
    } catch (error) {
      // The overview request reports bridge failures; theme lookup is best effort.
    }
  }

  applyTheme(readThemePreference());
  updateIssueFilterUI();
  icons();
  navigate("overview");
  loadInitialAppearance();
})();
