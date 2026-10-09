const state = {
  days: [],
  selected: null,
  range: null,
  period: "week",
  pricing: null,
};
let activeLoadJobId = null;
let dayRequestGeneration = 0;
let periodRequestGeneration = 0;
let chartResizeTimer = null;
const SVG_NS = "http://www.w3.org/2000/svg";

window.addEventListener("pywebviewready", async () => {
  bindActions();
  await loadInitial();
});

async function loadInitial() {
  showLoading({
    state: "running",
    percent: 1,
    stage: "连接本机 Codex 日志",
    detail: "准备读取 SQLite 和 rollout",
  });
  try {
    const starter = await window.pywebview.api.start_initial_load();
    activeLoadJobId = starter.job_id;
    await pollInitialLoad(starter.job_id);
  } catch (error) {
    showLoadingError(error?.message || String(error || "加载失败"));
  }
}

function bindActions() {
  window.addEventListener("resize", () => {
    window.clearTimeout(chartResizeTimer);
    chartResizeTimer = window.setTimeout(renderChart, 120);
  });
  document.getElementById("todayButton").addEventListener("click", async () => {
    if (!state.days.length) return;
    await selectDay(state.days[0].date);
  });
  document.getElementById("refreshButton").addEventListener("click", loadInitial);
  document.getElementById("loadingRetryButton").addEventListener("click", loadInitial);
  document.querySelectorAll(".period").forEach((button) => {
    button.addEventListener("click", async () => {
      const requestedPeriod = button.dataset.period;
      const requestGeneration = ++periodRequestGeneration;
      state.period = requestedPeriod;
      document.querySelectorAll(".period").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      const range = await window.pywebview.api.get_range(requestedPeriod);
      if (requestGeneration !== periodRequestGeneration || state.period !== requestedPeriod) return;
      state.range = range;
      renderChart();
    });
  });
}

async function pollInitialLoad(jobId) {
  while (true) {
    const status = await window.pywebview.api.get_load_status(jobId);
    if (jobId !== activeLoadJobId) return;
    showLoading(status);
    if (status.state === "done") {
      state.days = status.result?.days || [];
      state.selected = status.result?.selected;
      state.range = status.result?.range;
      state.pricing = status.result?.pricing;
      renderAll();
      hideLoading();
      return;
    }
    if (status.state === "failed") {
      showLoadingError(status.error || "加载失败，请重试");
      return;
    }
    await sleep(120);
  }
}

function showLoading(status) {
  const percent = Math.max(0, Math.min(100, Math.round(status.percent || 0)));
  const overlay = document.getElementById("loadingOverlay");
  overlay.classList.add("visible");
  overlay.classList.remove("done", "failed");
  document.getElementById("loadingProgressFill").style.width = `${percent}%`;
  document.getElementById("loadingPercent").textContent = `${percent}%`;
  document.getElementById("loadingStage").textContent = status.stage || "正在加载";
  document.getElementById("loadingDetail").textContent = status.detail || "";
  document.querySelector(".loading-progress").setAttribute("aria-valuenow", String(percent));
}

function hideLoading() {
  const overlay = document.getElementById("loadingOverlay");
  overlay.classList.add("done");
  overlay.classList.remove("failed");
}

function showLoadingError(message) {
  const overlay = document.getElementById("loadingOverlay");
  overlay.classList.add("visible", "failed");
  overlay.classList.remove("done");
  document.getElementById("loadingProgressFill").style.width = "100%";
  document.getElementById("loadingPercent").textContent = "--";
  document.getElementById("loadingStage").textContent = "加载失败";
  document.getElementById("loadingDetail").textContent = message;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function selectDay(date) {
  const requestGeneration = ++dayRequestGeneration;
  const selected = await window.pywebview.api.get_day(date);
  if (requestGeneration !== dayRequestGeneration) return;
  state.selected = selected;
  renderReceipt();
  renderConversations();
  renderDayList();
}

function renderAll() {
  const prices = state.pricing;
  const date = prices?.fetched_at ? new Date(prices.fetched_at * 1000).toLocaleDateString() : "--";
  const mode = prices?.using_snapshot ? "离线价格快照" : "官网价格";
  document.getElementById("pricingStatus").textContent =
    `API 等价估算 · ${mode} ${date}${prices?.update_failed ? " · 同步失败，使用缓存" : " · 每日自动更新"}`;
  renderDayList();
  renderReceipt();
  renderConversations();
  renderChart();
}

function renderDayList() {
  const list = document.getElementById("dayList");
  const selectedDate = state.selected?.date;
  list.innerHTML = "";
  state.days.forEach((day) => {
    const button = document.createElement("button");
    button.className = `day-item${day.date === selectedDate ? " active" : ""}`;
    button.type = "button";
    button.appendChild(cell(shortDate(day.date), "day-date"));
    button.appendChild(cell(day.total_tokens_text, "day-tokens"));
    button.appendChild(cell(day.cost_text, "day-cost"));
    button.addEventListener("click", () => selectDay(day.date));
    list.appendChild(button);
  });
}

function renderReceipt() {
  const receipt = state.selected;
  if (!receipt) return;
  document.getElementById("receiptDate").textContent = `${receipt.date} 用量账单`;
  const incompleteNote = receipt.incomplete ? " · 数据可能不完整" : "";
  const priceNote = receipt.has_unknown_prices ? " · 部分模型未定价" : "";
  document.getElementById("receiptMeta").textContent =
    `事件 ${receipt.events} · 线程 ${receipt.threads}${incompleteNote}${priceNote}`;
  document.getElementById("totalTokens").textContent = `${receipt.total_tokens_text} tokens`;
  document.getElementById("totalCost").textContent = receipt.cost_text;

  const splitBar = document.getElementById("splitBar");
  splitBar.innerHTML = "";
  receipt.categories.forEach((category) => {
    const split = document.createElement("div");
    split.className = `split ${category.key}`;
    split.style.width = `${Math.max(0, category.percent)}%`;
    split.title = `${category.label} ${category.percent.toFixed(1)}%`;
    splitBar.appendChild(split);
  });

  const rows = document.getElementById("categoryRows");
  rows.innerHTML = "";
  receipt.categories.forEach((category) => {
    const row = document.createElement("div");
    row.className = "category-row";
    const name = document.createElement("div");
    name.className = "category-name";
    const mark = document.createElement("span");
    mark.className = `mark ${category.key}`;
    name.appendChild(mark);
    name.appendChild(cell(category.label));
    row.appendChild(name);
    row.appendChild(cell(`${category.tokens_text} tokens`, "category-tokens"));
    row.appendChild(cell(`${category.percent.toFixed(1)}%`, "category-percent"));
    row.appendChild(cell(category.cost_text, "category-cost"));
    rows.appendChild(row);
  });
}

function renderConversations() {
  const receipt = state.selected;
  const rows = document.getElementById("conversationRows");
  document.getElementById("conversationDate").textContent = receipt?.date || "--";
  rows.innerHTML = "";

  const conversations = receipt?.conversations || [];
  if (!conversations.length) {
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "当天没有对话 token 记录";
    rows.appendChild(empty);
    return;
  }

  conversations.forEach((conversation) => {
    const row = document.createElement("div");
    row.className = "conversation-row";
    row.appendChild(cell(conversation.title, "conversation-title", conversation.full_title));
    row.appendChild(cell(conversation.model, "mono clip", conversation.model));
    row.appendChild(cell(conversation.cwd_label, "mono clip", conversation.cwd_label));
    row.appendChild(cell(conversation.tokens_text, "mono number"));
    row.appendChild(cell(conversation.cost_text, "mono cost"));
    row.appendChild(cell(conversation.ico_text, "mono muted-cell", "Input / Cached / Output"));
    row.appendChild(cell(conversation.message_text, "mono number"));
    rows.appendChild(row);
  });
}

function cell(text, className = "", title = "") {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = text || "--";
  if (title) node.title = title;
  return node;
}

function renderChart() {
  const range = state.range;
  if (!range) return;
  const incompleteNote = range.incomplete ? " · 数据可能不完整" : "";
  document.getElementById("rangeMeta").textContent =
    `${range.start_date} - ${range.end_date} · ${range.total_tokens_text} · ${range.cost_text}${incompleteNote}`;
  const chart = document.getElementById("chart");
  chart.className = `chart ${range.period || state.period}`;
  chart.innerHTML = "";
  const buckets = range.buckets || [];
  chart.setAttribute(
    "aria-label",
    `${range.start_date} 至 ${range.end_date} token 用量趋势`,
  );
  if (!buckets.length) {
    const empty = document.createElement("div");
    empty.className = "chart-empty";
    empty.textContent = "当前范围没有 token 记录";
    chart.appendChild(empty);
    return;
  }

  const width = Math.max(640, Math.round(chart.clientWidth || 0));
  const height = Math.max(170, Math.round(chart.clientHeight || 208));
  const margin = { top: 18, right: 24, bottom: 34, left: 58 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const baseline = margin.top + plotHeight;
  const maxTokens = Math.max(0, ...buckets.map((bucket) => bucket.total_tokens || 0));
  const scaleMax = Math.max(1, maxTokens * 1.08);
  const peak = Math.max(0, ...buckets.map((bucket) => bucket.total_tokens || 0));

  const svg = svgNode("svg", {
    class: "trend-svg",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-hidden": "true",
  });
  const definitions = svgNode("defs");
  const gradient = svgNode("linearGradient", {
    id: "trend-area-fill",
    x1: "0",
    y1: "0",
    x2: "0",
    y2: "1",
  });
  gradient.appendChild(svgNode("stop", { offset: "0%", "stop-color": "#76d9e6", "stop-opacity": "0.22" }));
  gradient.appendChild(svgNode("stop", { offset: "100%", "stop-color": "#76d9e6", "stop-opacity": "0" }));
  definitions.appendChild(gradient);
  svg.appendChild(definitions);

  for (let step = 0; step <= 4; step += 1) {
    const ratio = step / 4;
    const y = margin.top + plotHeight * ratio;
    const value = scaleMax * (1 - ratio);
    svg.appendChild(svgNode("line", {
      class: step === 4 ? "chart-baseline" : "chart-grid",
      x1: margin.left,
      y1: y,
      x2: width - margin.right,
      y2: y,
    }));
    const label = svgNode("text", {
      class: "chart-axis-label",
      x: margin.left - 10,
      y: y + 3,
      "text-anchor": "end",
    });
    label.textContent = formatAxisTokens(value);
    svg.appendChild(label);
  }

  const points = buckets.map((bucket, index) => {
    const x = buckets.length === 1
      ? margin.left + plotWidth / 2
      : margin.left + (plotWidth * index) / (buckets.length - 1);
    const y = baseline - ((bucket.total_tokens || 0) / scaleMax) * plotHeight;
    return { bucket, index, x, y };
  });
  const linePath = points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
  const areaPath = `${linePath} L ${points.at(-1).x} ${baseline} L ${points[0].x} ${baseline} Z`;
  svg.appendChild(svgNode("path", { class: "chart-area", d: areaPath }));
  svg.appendChild(svgNode("path", { class: "chart-line", d: linePath }));

  const guide = svgNode("line", {
    class: "chart-guide",
    y1: margin.top,
    y2: baseline,
  });
  svg.appendChild(guide);

  const period = range.period || state.period;
  const labelIndexes = axisLabelIndexes(buckets.length, period === "month" ? 7 : 12);
  points.forEach(({ bucket, index, x, y }) => {
    if (labelIndexes.has(index)) {
      const label = svgNode("text", {
        class: "chart-axis-label",
        x,
        y: height - 10,
        "text-anchor": "middle",
      });
      label.textContent = formatChartLabel(bucket.label, period);
      svg.appendChild(label);
    }

    const group = svgNode("g", {
      class: `line-point${bucket.total_tokens === peak && peak > 0 ? " peak" : ""}`,
      tabindex: "0",
      role: "img",
      "aria-label": `${bucket.label}: ${bucket.total_tokens_text}, ${bucket.cost_text}`,
    });
    group.appendChild(svgNode("circle", { class: "chart-point", cx: x, cy: y, r: 3.5 }));
    group.appendChild(svgNode("circle", { class: "chart-hit", cx: x, cy: y, r: 13 }));
    const show = () => showChartTooltip(chart, guide, bucket, x, y, width);
    const hide = () => hideChartTooltip(chart, guide);
    group.addEventListener("mouseenter", show);
    group.addEventListener("focus", show);
    group.addEventListener("mouseleave", hide);
    group.addEventListener("blur", hide);
    svg.appendChild(group);
  });

  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  chart.appendChild(svg);
  chart.appendChild(tooltip);
}

function svgNode(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, String(value)));
  return node;
}

function axisLabelIndexes(count, maximum) {
  if (count <= maximum) return new Set(Array.from({ length: count }, (_, index) => index));
  const indexes = new Set();
  for (let slot = 0; slot < maximum; slot += 1) {
    indexes.add(Math.round((slot * (count - 1)) / (maximum - 1)));
  }
  return indexes;
}

function formatChartLabel(label, period) {
  if (!label) return "--";
  if (period === "year" && /^\d{4}\/\d{2}$/.test(label)) return `${label.slice(-2)}月`;
  return label.slice(-5);
}

function formatAxisTokens(value) {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(0)}K`;
  return String(Math.round(value));
}

function showChartTooltip(chart, guide, bucket, x, y, width) {
  const tooltip = chart.querySelector(".chart-tooltip");
  if (!tooltip) return;
  guide.setAttribute("x1", String(x));
  guide.setAttribute("x2", String(x));
  guide.classList.add("visible");
  const heading = document.createElement("strong");
  heading.textContent = bucket.label || "--";
  const detail = document.createElement("span");
  detail.textContent = `${bucket.total_tokens_text} · ${bucket.cost_text}`;
  tooltip.replaceChildren(heading, detail);
  tooltip.style.left = `${Math.max(86, Math.min(width - 86, x))}px`;
  tooltip.style.top = `${y < 72 ? y + 14 : y - 10}px`;
  tooltip.classList.toggle("below", y < 72);
  tooltip.classList.add("visible");
}

function hideChartTooltip(chart, guide) {
  guide.classList.remove("visible");
  chart.querySelector(".chart-tooltip")?.classList.remove("visible", "below");
}

function shortDate(value) {
  if (!value || value.length < 10) return value || "--";
  return value.slice(5).replace("-", "/");
}
