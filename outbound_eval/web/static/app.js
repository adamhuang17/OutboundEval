/* OutboundEval OS — 前端主逻辑 */
"use strict";

// ===== 全局状态 =====
const state = {
  modelsOk: { compiler: false, simulator: false, judge: false },
  understanding: null,
  scenarioSet: null,
  selectedScenarios: new Set(),
  currentRunId: null,
  importedRunId: null,
  reportData: null,
  chatEpisodes: {},   // episodeId -> {turns, judgeResult, scenario}
  activeChatEpisode: null,
};

// ===== 工具函数 =====
const $ = (id) => document.getElementById(id);
const post = async (url, body) => {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
};
const get = async (url) => {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
};
const fmt = (obj) => JSON.stringify(obj, null, 2);

function showTab(tabName) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.querySelector(`.tab[data-tab="${tabName}"]`)?.classList.add("active");
  $(tabName)?.classList.add("active");
}

// 初始化 tab 切换
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

// ===== 模型配置读取 =====
function getModelConfig(role) {
  const ids = {
    compiler: ["compilerUrl", "compilerModel", "compilerKey", "compilerTemp", "compilerMaxTokens", "compilerTimeout"],
    simulator: ["simulatorUrl", "simulatorModel", "simulatorKey", "simulatorTemp", "simulatorMaxTokens", "simulatorTimeout"],
    judge: ["judgeUrl", "judgeModel", "judgeKey", "judgeTemp", "judgeMaxTokens", "judgeTimeout"],
  };
  const [url, model, key, temp, maxTokens, timeout] = ids[role].map((id) => $(id)?.value || "");
  return {
    provider: "openai_compatible",
    base_url: url,
    model_name: model,
    api_key: key,
    temperature: parseFloat(temp) || 0.1,
    max_tokens: parseInt(maxTokens) || 512,
    timeout_seconds: parseInt(timeout) || 30,
    connection_tested: state.modelsOk[role],
  };
}

function getPersona() {
  return {
    identity: $("pIdentity")?.value || "",
    relationship_to_task: $("pRelationship")?.value || "",
    motivation: $("pMotivation")?.value || "",
    attitude: $("pAttitude")?.value || "",
    communication_style: $("pStyle")?.value || "",
    initial_focus: $("pFocus")?.value || "",
    decision_rule: $("pDecision")?.value || "",
    inconvenience_context: $("pInconvenience")?.value || "",
    extra_notes: $("pNotes")?.value || "",
  };
}

// ===== 测试单个模型 =====
async function testSingleModel(role) {
  const config = getModelConfig(role);
  const statusEl = $(`status${role.charAt(0).toUpperCase() + role.slice(1)}`);
  const cardEl = $(`card${role.charAt(0).toUpperCase() + role.slice(1)}`);
  statusEl.textContent = "测试中...";
  statusEl.className = "model-status";
  cardEl.className = "model-card";
  try {
    const result = await post("/api/model/test", config);
    if (result.ok) {
      statusEl.textContent = `✓ 已连接 (${result.latency_ms}ms)`;
      statusEl.className = "model-status ok";
      cardEl.classList.add("connected");
      state.modelsOk[role] = true;
    } else {
      statusEl.textContent = `✗ 失败: ${result.error_message || result.error_type}`;
      statusEl.className = "model-status fail";
      cardEl.classList.add("failed");
      state.modelsOk[role] = false;
    }
  } catch (err) {
    statusEl.textContent = `✗ 错误: ${err.message}`;
    statusEl.className = "model-status fail";
    state.modelsOk[role] = false;
  }
}

// ===== 测试全部模型 =====
async function testAllModels() {
  const btn = $("btnTestAll");
  btn.textContent = "测试中...";
  btn.disabled = true;
  const resultDiv = $("testAllResult");
  resultDiv.innerHTML = "";
  try {
    const result = await post("/api/models/test-all", {
      configs: {
        compiler_model: getModelConfig("compiler"),
        simulator_model: getModelConfig("simulator"),
        judge_model: getModelConfig("judge"),
      },
    });
    const roleNames = { compiler: "编译模型", simulator: "模拟模型", judge: "评判模型" };
    const roleKeys = ["compiler", "simulator", "judge"];
    result.details.forEach((d, i) => {
      const key = roleKeys[i];
      state.modelsOk[key] = d.ok;
      const el = document.createElement("div");
      el.className = `test-badge ${d.ok ? "ok" : "fail"}`;
      el.textContent = `${d.ok ? "✓" : "✗"} ${roleNames[key]}${d.latency_ms ? ` (${d.latency_ms}ms)` : ""}`;
      resultDiv.appendChild(el);
      // 更新单卡状态
      const statusEl = $(`status${key.charAt(0).toUpperCase() + key.slice(1)}`);
      const cardEl = $(`card${key.charAt(0).toUpperCase() + key.slice(1)}`);
      if (statusEl) {
        statusEl.textContent = d.ok ? `✓ 已连接${d.latency_ms ? ` (${d.latency_ms}ms)` : ""}` : `✗ ${d.error || "失败"}`;
        statusEl.className = `model-status ${d.ok ? "ok" : "fail"}`;
      }
      if (cardEl) {
        cardEl.className = `model-card ${d.ok ? "connected" : "failed"}`;
      }
    });
    if (result.ok) {
      const ok = document.createElement("div");
      ok.className = "test-badge ok";
      ok.style.fontWeight = "700";
      ok.textContent = "✓ 全部通过，可以开始评测";
      resultDiv.appendChild(ok);
    }
  } catch (err) {
    resultDiv.innerHTML = `<span style="color:var(--red)">测试失败: ${err.message}</span>`;
  } finally {
    btn.textContent = "🔌 测试全部连接";
    btn.disabled = false;
  }
}

// ===== 检查模型是否全部通过 =====
function checkModelsReady() {
  if (!state.modelsOk.compiler || !state.modelsOk.simulator || !state.modelsOk.judge) {
    alert("请先在「配置」页面测试全部三个 LLM 连接，确保全部通过后再继续。");
    showTab("setup");
    return false;
  }
  return true;
}

// ===== compile 子 tab 切换 =====
function switchCompileTab(tabName) {
  document.querySelectorAll(".ctab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".ctab-panel").forEach((p) => p.classList.remove("active"));
  document.querySelector(`.ctab[data-ctab="${tabName}"]`)?.classList.add("active");
  $(`ctab-${tabName}`)?.classList.add("active");
}

// ===== LLM 编译任务 =====
async function compileLLM() {
  if (!checkModelsReady()) return;
  const instruction = $("instruction").value.trim();
  if (!instruction) {
    alert("请输入任务说明");
    return;
  }
  const btn = $("btnCompileLLM");
  btn.textContent = "⏳ 编译中...";
  btn.disabled = true;
  $("specResult").textContent = "正在编译，请稍候...";

  try {
    const result = await post("/api/task/understand", {
      instruction,
      llm_config: getModelConfig("compiler"),
    });
    if (!result.ok) {
      $("specResult").textContent = `编译失败: ${result.error}`;
      return;
    }
    state.understanding = result.understanding;
    const u = result.understanding;
    const ts = u.task_spec || {};

    // TaskSpec tab
    $("specResult").textContent = fmt({
      task_id: ts.task_id,
      task_name: ts.task_name,
      role: ts.role,
      objective: ts.objective,
      opening_line: ts.opening_line,
      requirements_count: (ts.requirements || []).length,
      flow_nodes_count: (ts.flow_nodes || []).length,
      constraints_count: (ts.constraints || []).length,
      forbidden_behaviors_count: (ts.forbidden_behaviors || []).length,
      termination_rules_count: (ts.termination_rules || []).length,
      requirements: ts.requirements,
    });

    // JudgePlan tab
    const jp = u.judge_plan || {};
    $("judgeResult").textContent = fmt(jp);

    // RiskPlan tab
    $("riskResult").textContent = fmt(u.risk_plan || {});

    // Knowledge tab
    renderKnowledgeFacts(u.knowledge_facts || []);

    // Findings tab
    renderFindings(u.compile_findings || []);

    switchCompileTab("spec");
  } catch (err) {
    $("specResult").textContent = `错误: ${err.message}`;
  } finally {
    btn.textContent = "🤖 LLM 编译任务";
    btn.disabled = false;
  }
}

function renderKnowledgeFacts(facts) {
  const container = $("knowledgeCards");
  if (!facts.length) { container.innerHTML = "<p style='color:var(--muted)'>无知识点</p>"; return; }
  container.innerHTML = facts.map((kf) => `
    <div class="kf-card">
      <span class="kf-tag ${kf.fact_type || 'other'}">${kf.fact_type || 'other'}</span>
      <div style="font-size:13px;font-weight:600;margin-bottom:4px">${escHtml(kf.text)}</div>
      ${kf.answer ? `<div style="font-size:12px;color:var(--muted)"><strong>答：</strong>${escHtml(kf.answer)}</div>` : ""}
      ${kf.source_text ? `<div style="font-size:11px;color:var(--muted);margin-top:4px">来源：${escHtml(kf.source_text.slice(0, 80))}</div>` : ""}
    </div>`).join("");
}

function renderFindings(findings) {
  const container = $("findingsCards");
  if (!findings.length) { container.innerHTML = "<p style='color:var(--muted)'>无发现</p>"; return; }
  container.innerHTML = findings.map((f) => `
    <div class="finding-card ${f.severity || 'minor'}">
      <div class="finding-code">${escHtml(f.code)}</div>
      <div style="font-size:13px;margin:4px 0">${escHtml(f.message)}</div>
      ${f.suggestion ? `<div style="font-size:12px;color:var(--accent)">建议：${escHtml(f.suggestion)}</div>` : ""}
      ${f.blocking ? `<div style="font-size:11px;color:var(--red);font-weight:700;margin-top:2px">BLOCKING</div>` : ""}
    </div>`).join("");
}

// ===== 构建场景 =====
async function buildScenarios() {
  if (!checkModelsReady()) return;
  if (!state.understanding) {
    alert("请先编译任务（在「任务编译」页面）");
    return;
  }
  const btn = $("btnBuildScenarios");
  btn.textContent = "⏳ 构建中...";
  btn.disabled = true;
  $("scenarioStatus").textContent = "正在构建测试场景，请稍候...";
  $("scenarioCards").innerHTML = "";
  state.selectedScenarios.clear();

  try {
    const count = parseInt($("scenarioCount").value) || 6;
    const result = await post("/api/scenarios/build", {
      understanding: state.understanding,
      persona: getPersona(),
      scenario_count: count,
      llm_config: getModelConfig("compiler"),
    });
    if (!result.ok) {
      $("scenarioStatus").textContent = `构建失败: ${result.error}`;
      return;
    }
    state.scenarioSet = result.scenario_set;
    const scenarios = state.scenarioSet.scenarios || [];
    $("scenarioStatus").textContent = `已生成 ${scenarios.length} 个场景，点击卡片右上角可选择/取消选择`;
    renderScenarioCards(scenarios);
  } catch (err) {
    $("scenarioStatus").textContent = `错误: ${err.message}`;
  } finally {
    btn.textContent = "🏗 构建场景";
    btn.disabled = false;
  }
}

function renderScenarioCards(scenarios) {
  const container = $("scenarioCards");
  container.innerHTML = "";
  scenarios.forEach((scn, idx) => {
    const card = document.createElement("div");
    card.className = "scenario-card";
    card.dataset.idx = idx;
    const jpChips = (scn.linked_judge_point_ids || []).map((id) => `<span class="chip judge">${escHtml(id)}</span>`).join("");
    const reqChips = (scn.covered_requirement_ids || []).slice(0, 3).map((id) => `<span class="chip">${escHtml(id)}</span>`).join("");
    card.innerHTML = `
      <button class="scenario-select-btn" title="选择/取消选择此场景" onclick="toggleScenario(${idx})">✓</button>
      <span class="scenario-type-badge">${scn.scenario_type || "main_flow"}</span>
      <div class="scenario-title">${escHtml(scn.title || "无标题")}</div>
      <div class="scenario-field"><strong>用户目标：</strong>${escHtml(scn.user_goal || "-")}</div>
      <div class="scenario-field"><strong>初始话术：</strong><em>${escHtml(scn.initial_user_utterance || "-")}</em></div>
      <div class="scenario-field"><strong>画像：</strong>${escHtml([scn.persona?.identity, scn.persona?.attitude].filter(Boolean).join(" / ") || "-")}</div>
      <div class="scenario-chips">${jpChips}${reqChips}</div>
      <div class="scenario-field" style="margin-top:8px;font-size:11px;color:var(--muted)">
        最多 ${scn.max_turns || 8} 轮 | ${(scn.dialogue_direction || []).length} 个推进方向
      </div>`;
    container.appendChild(card);
    // 默认全选
    toggleScenario(idx, true);
  });
}

function toggleScenario(idx, forceSelect) {
  const scenarios = state.scenarioSet?.scenarios || [];
  if (idx >= scenarios.length) return;
  const scnId = scenarios[idx].scenario_id;
  if (forceSelect === true) {
    state.selectedScenarios.add(idx);
  } else if (state.selectedScenarios.has(idx)) {
    state.selectedScenarios.delete(idx);
  } else {
    state.selectedScenarios.add(idx);
  }
  const card = document.querySelector(`.scenario-card[data-idx="${idx}"]`);
  if (card) {
    card.classList.toggle("selected", state.selectedScenarios.has(idx));
  }
}

// ===== 开始评测运行 =====
async function startRun() {
  if (!checkModelsReady()) return;
  if (!state.understanding) {
    alert("请先编译任务");
    return;
  }
  if (!state.scenarioSet || !state.scenarioSet.scenarios?.length) {
    alert("请先在「场景构建」页面生成测试场景");
    return;
  }
  const selectedScenarios = state.scenarioSet.scenarios.filter((_, i) => state.selectedScenarios.has(i));
  if (!selectedScenarios.length) {
    alert("请选择至少一个测试场景（点击卡片右上角 ✓）");
    return;
  }

  const btn = $("btnStartRun");
  btn.textContent = "运行中...";
  btn.disabled = true;
  resetChatUI();
  $("runProgressFill").style.width = "0%";
  $("runProgressBar").style.display = "block";
  $("runStageInfo").textContent = "正在启动评测...";

  // 渲染场景列表
  renderChatScenarioList(selectedScenarios);

  try {
    const result = await post("/api/run/start", {
      instruction: $("instruction")?.value || "",
      understanding: state.understanding,
      scenarios: selectedScenarios,
      compiler_model: getModelConfig("compiler"),
      simulator_model: getModelConfig("simulator"),
      judge_model: getModelConfig("judge"),
    });
    if (!result.ok) {
      $("runStageInfo").textContent = `启动失败: ${result.error || "未知错误"}`;
      return;
    }
    state.currentRunId = result.run_id;
    $("runBadge").style.display = "block";
    $("runBadge").textContent = `运行中: ${result.run_id}`;
    startSSE(result.run_id, selectedScenarios);
  } catch (err) {
    $("runStageInfo").textContent = `错误: ${err.message}`;
    btn.textContent = "▶ 开始评测";
    btn.disabled = false;
  }
}

function resetChatUI() {
  $("chatMessages").innerHTML = "";
  $("chatScenarioList").innerHTML = "";
  $("chatEpisodeHeader").textContent = "等待运行...";
  state.chatEpisodes = {};
  state.activeChatEpisode = null;
}

function renderChatScenarioList(scenarios) {
  const container = $("chatScenarioList");
  container.innerHTML = scenarios.map((scn, i) => `
    <div class="chat-scenario-item" id="chatScn_${scn.scenario_id}" onclick="switchChatEpisode('${scn.scenario_id}')">
      <div>${escHtml(scn.title?.slice(0, 28) || "场景 " + (i+1))}</div>
      <span class="chat-score" id="scoreLabel_${scn.scenario_id}"></span>
    </div>`).join("");
}

function switchChatEpisode(scenarioId) {
  document.querySelectorAll(".chat-scenario-item").forEach((el) => el.classList.remove("active"));
  const item = $(`chatScn_${scenarioId}`);
  if (item) item.classList.add("active");

  const ep = Object.values(state.chatEpisodes).find((e) => e.scenarioId === scenarioId);
  if (!ep) return;
  state.activeChatEpisode = ep.episodeId;
  renderChatMessages(ep.turns);
  if (ep.title) $("chatEpisodeHeader").textContent = ep.title;
}

function renderChatMessages(turns) {
  const container = $("chatMessages");
  container.innerHTML = "";
  turns.forEach((t) => appendChatMessage(t.role, t.content, t.id));
  container.scrollTop = container.scrollHeight;
}

function appendChatMessage(role, content, id) {
  const container = $("chatMessages");
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.id = `turn_${id || ""}`;
  div.innerHTML = `
    <div class="chat-role-label">${role === "user" ? "👤 模拟用户" : "🤖 对话模型"}</div>
    <div class="chat-bubble">${escHtml(content)}</div>
    <div class="chat-msg-meta">${role === "user" ? "simulator" : "target"}</div>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

// ===== SSE 监听 =====
function startSSE(runId, scenarios) {
  const es = new EventSource(`/api/run/${runId}/events`);
  let completedEpisodes = 0;
  const totalEpisodes = scenarios.length;

  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    handleSSEEvent(ev, totalEpisodes, () => { completedEpisodes++; });
    if (ev.type === "completed" || ev.type === "error") {
      es.close();
      const btn = $("btnStartRun");
      btn.textContent = "▶ 开始评测";
      btn.disabled = false;
      $("runBadge").textContent = `完成: ${runId}`;
      if (ev.type === "completed") {
        $("runProgressFill").style.width = "100%";
        showReportAfterRun(runId);
      }
    }
  };
  es.onerror = () => {
    es.close();
    const btn = $("btnStartRun");
    btn.textContent = "▶ 开始评测";
    btn.disabled = false;
  };
}

function handleSSEEvent(ev, totalEpisodes, onEpDone) {
  if (ev.type === "stage") {
    $("runStageInfo").textContent = `[${ev.stage}] ${ev.message || ""}`;
    return;
  }
  if (ev.type === "episode_start") {
    const { episode_id, scenario_id, scenario_title, scn_index, total } = ev;
    state.chatEpisodes[episode_id] = { episodeId: episode_id, scenarioId: scenario_id, title: scenario_title, turns: [] };
    // 激活当前场景
    document.querySelectorAll(".chat-scenario-item").forEach((el) => el.classList.remove("active"));
    $(`chatScn_${scenario_id}`)?.classList.add("active");
    $("chatEpisodeHeader").textContent = `场景 ${scn_index+1}/${total}: ${scenario_title || ""}`;
    state.activeChatEpisode = episode_id;
    $("chatMessages").innerHTML = "";
    const pct = (scn_index / total) * 100;
    $("runProgressFill").style.width = `${pct}%`;
    $("runStageInfo").textContent = `场景 ${scn_index+1}/${total}: ${scenario_title || ""}`;
    return;
  }
  if (ev.type === "turn") {
    const ep = state.chatEpisodes[ev.episode_id];
    if (ep) {
      ep.turns.push({ id: ev.turn_id, role: ev.role, content: ev.content });
      if (state.activeChatEpisode === ev.episode_id) {
        $("chatTyping").style.display = "none";
        appendChatMessage(ev.role, ev.content, ev.turn_id);
        // typing indicator for next turn
        if (ev.role === "user") $("chatTyping").style.display = "flex";
      }
    }
    return;
  }
  if (ev.type === "episode_end") {
    $("chatTyping").style.display = "none";
    onEpDone();
    return;
  }
  if (ev.type === "judge_result") {
    const ep = Object.values(state.chatEpisodes).find((e) => e.episodeId === ev.episode_id);
    if (ep) {
      ep.judgeResult = ev;
      const scoreLabel = $(`scoreLabel_${ep.scenarioId}`);
      if (scoreLabel) {
        const score = ev.total_score;
        scoreLabel.className = `chat-score ${score >= 70 ? "good" : "bad"}`;
        scoreLabel.textContent = `${score}分`;
        $(`chatScn_${ep.scenarioId}`)?.classList.add("done");
      }
    }
    return;
  }
  if (ev.type === "judge_error") {
    $("runStageInfo").textContent = `评分错误: ${ev.error}`;
    return;
  }
  if (ev.type === "completed") {
    $("runProgressFill").style.width = "100%";
    $("runStageInfo").textContent = `✓ 评测完成！平均分: ${(ev.avg_score || 0).toFixed(1)}`;
    return;
  }
  if (ev.type === "error") {
    $("runStageInfo").textContent = `✗ 运行错误: ${ev.error}`;
    return;
  }
}

// ===== 完成后显示报告 =====
async function showReportAfterRun(runId) {
  try {
    const result = await get(`/api/run/${runId}/result`);
    if (result.report) {
      state.reportData = result.report;
      state.reportData._runId = runId;
      renderReport(result.report);
      showTab("report");
      $("btnExportReport").style.display = "inline-flex";
      $("btnExportHtml").style.display = "inline-flex";
    }
  } catch (err) {
    console.error("获取报告失败", err);
  }
}

// ===== 渲染评测报告 =====
function renderReport(data) {
  const container = $("reportContent");
  if (!data) { container.innerHTML = "<div class='empty-state'>无报告数据</div>"; return; }

  const judgeResults = data.judge_results || [];
  const judgePoints = (data.judge_plan?.judge_points || []);

  // 计算维度分数
  const dimScores = {};
  const dimCounts = {};
  judgeResults.forEach((jr) => {
    (jr.item_results || []).forEach((item) => {
      const jp = judgePoints.find((p) => p.id === item.judge_point_id);
      const dim = jp?.dimension || "unknown";
      if (!dimScores[dim]) { dimScores[dim] = 0; dimCounts[dim] = 0; }
      dimScores[dim] += item.score || 0;
      dimCounts[dim]++;
    });
  });
  const dimAvg = Object.fromEntries(
    Object.keys(dimScores).map((d) => [d, dimCounts[d] > 0 ? Math.round((dimScores[d] / dimCounts[d]) * 100) : 0])
  );

  const dimNames = {
    task_completion: "任务完成", flow_following: "流程遵循",
    knowledge_correctness: "知识正确", constraint_following: "约束遵守",
    exception_handling: "异常处理", user_experience: "用户体验",
    safety_compliance: "安全合规",
  };

  const avgScore = data.avg_score || 0;
  const scoreClass = avgScore >= 80 ? "high" : avgScore >= 60 ? "mid" : "low";

  // 汇总卡
  const summaryHtml = `
    <div class="report-summary">
      <div class="report-summary-row">
        <div>
          <div class="report-score-big ${scoreClass}">${avgScore.toFixed(1)}</div>
          <div class="report-score-label">平均综合得分 / 100</div>
        </div>
        <div class="report-summary-stats">
          <div class="report-stat">任务名：<span>${escHtml(data.task_name || "-")}</span></div>
          <div class="report-stat">测试场景数：<span>${data.total_scenarios || judgeResults.length}</span></div>
          <div class="report-stat">Run ID：<span style="font-family:monospace;font-size:12px">${escHtml(data.run_id || "-")}</span></div>
        </div>
      </div>
    </div>`;

  // 维度分
  const dimHtml = `
    <div class="dimension-scores">
      ${Object.entries(dimAvg).map(([d, s]) => `
        <div class="dim-card">
          <div class="dim-name">${dimNames[d] || d}</div>
          <div class="dim-score ${s >= 80 ? "high" : s >= 60 ? "mid" : "low"}">${s}</div>
        </div>`).join("")}
    </div>`;

  // 各 episode
  const episodesHtml = judgeResults.map((jr, idx) => {
    const ep = (data.episodes || []).find((e) => e.episode_id === jr.episode_id);
    const epScore = jr.total_score || 0;
    const epScoreClass = epScore >= 80 ? "high" : epScore >= 60 ? "mid" : "low";

    const criticalHtml = (jr.critical_failures || []).length > 0 ? `
      <div class="critical-failures">
        <div class="critical-failures-title">⚠ 严重失败项</div>
        ${(jr.critical_failures || []).map((c) => `<div class="critical-failure-item">• ${escHtml(c)}</div>`).join("")}
      </div>` : "";

    const transcriptHtml = ep ? `
      <div class="transcript-mini">
        ${(ep.turns || []).slice(0, 6).map((t) => `
          <div class="t-msg">
            <span class="t-role ${t.role}">${t.role === "user" ? "用户" : "模型"}</span>
            <span class="t-content">${escHtml(t.content?.slice(0, 120) || "")}</span>
          </div>`).join("")}
        ${(ep.turns || []).length > 6 ? `<div style="font-size:11px;color:var(--muted);padding:0 4px">... 共 ${ep.turns.length} 轮</div>` : ""}
      </div>` : "";

    const itemsHtml = (jr.item_results || []).map((item) => {
      const jp = judgePoints.find((p) => p.id === item.judge_point_id);
      const evidenceHtml = (item.evidence_quotes || []).length > 0
        ? `<div class="judge-evidence">${item.evidence_quotes.map(escHtml).join("\n")}</div>` : "";
      const fixHtml = item.suggested_fix ? `<div class="judge-fix">💡 建议：${escHtml(item.suggested_fix)}</div>` : "";
      return `
        <div class="judge-item ${item.verdict || "not_applicable"}">
          <div class="judge-item-header">
            <span class="judge-verdict ${item.verdict}">${verdictLabel(item.verdict)}</span>
            <span class="judge-dim">${dimNames[jp?.dimension || ""] || jp?.dimension || ""}</span>
            <span class="judge-criterion">${escHtml(jp?.criterion || item.judge_point_id)}</span>
            <span style="font-size:12px;font-weight:700;color:${item.verdict === "fail" ? "var(--red)" : item.verdict === "pass" ? "var(--green)" : "var(--yellow)"}">${Math.round((item.score || 0) * 100)}分</span>
          </div>
          <div class="judge-reason">${escHtml(item.reason || "")}</div>
          ${evidenceHtml}${fixHtml}
        </div>`;
    }).join("");

    const scenTitle = jr.scenario_id || `场景 ${idx + 1}`;
    return `
      <div class="episode-section">
        <div class="episode-header" onclick="toggleEpisode('epBody_${idx}')">
          <span class="episode-title">${escHtml(scenTitle)}</span>
          <div style="display:flex;align-items:center;gap:10px">
            <span style="font-size:12px;color:var(--muted)">${(jr.item_results || []).length} 评测点</span>
            <span class="episode-score-badge ${epScoreClass}">${epScore.toFixed(1)} 分</span>
          </div>
        </div>
        <div class="episode-body" id="epBody_${idx}">
          ${criticalHtml}
          <div style="font-size:13px;color:var(--muted);margin-bottom:12px">${escHtml(jr.overall_summary || "")}</div>
          <h3 style="font-size:13px;font-weight:600;margin-bottom:8px">对话回放</h3>
          ${transcriptHtml}
          <h3 style="font-size:13px;font-weight:600;margin-bottom:8px">评分详情</h3>
          ${itemsHtml || "<p style='color:var(--muted);font-size:13px'>无评分数据</p>"}
        </div>
      </div>`;
  }).join("");

  container.innerHTML = summaryHtml + dimHtml + episodesHtml;
}

function toggleEpisode(bodyId) {
  const el = $(bodyId);
  if (el) el.classList.toggle("open");
}

function verdictLabel(v) {
  return { pass: "✓ 通过", partial: "~ 部分通过", fail: "✗ 不通过", not_applicable: "— 不适用" }[v] || v;
}

// ===== 导出报告 =====
async function exportReport() {
  if (!state.currentRunId && !state.reportData?._runId) { alert("无报告可导出"); return; }
  const runId = state.currentRunId || state.reportData._runId;
  window.open(`/api/run/${runId}/export`, "_blank");
}

function exportReportHtml() {
  if (!state.reportData) { alert("无报告可导出"); return; }
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>评测报告</title>
<style>body{font-family:Arial,sans-serif;max-width:900px;margin:0 auto;padding:24px}
.dim-card{display:inline-block;margin:6px;padding:12px 20px;background:#f3f4f6;border-radius:8px;text-align:center}
.dim-score{font-size:24px;font-weight:800}
.judge-item{margin:8px 0;padding:10px 14px;border-left:4px solid #e2e8f0;border-radius:4px}
.judge-item.fail{border-left-color:#dc2626;background:#fee2e2}
.judge-item.pass{border-left-color:#16a34a;background:#dcfce7}
.judge-item.partial{border-left-color:#d97706;background:#fef3c7}
</style></head><body>
<h1>评测报告 — ${escHtml(state.reportData.task_name || "")}</h1>
<p>平均分：<strong>${state.reportData.avg_score?.toFixed(1)}</strong> | Run ID: ${escHtml(state.reportData.run_id || "")}</p>
<pre>${escHtml(JSON.stringify(state.reportData, null, 2))}</pre>
</body></html>`;
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `eval_report_${state.reportData.run_id || "unknown"}.html`;
  a.click();
}

// ===== 导入对话 =====
async function downloadTemplate() {
  try {
    const result = await get("/api/conversation/template");
    const content = JSON.stringify(result.template, null, 2);
    const blob = new Blob([content], { type: "application/json;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "outbound_eval_import_template.json";
    a.click();
  } catch (err) {
    alert(`下载失败: ${err.message}`);
  }
}

async function importConversation() {
  const raw = $("importJson").value.trim();
  if (!raw) { alert("请粘贴对话 JSON"); return; }
  let parsed;
  try { parsed = JSON.parse(raw); } catch { alert("JSON 格式错误，请检查"); return; }
  $("importResult").textContent = "导入中...";
  try {
    const result = await post("/api/conversation/import", parsed);
    $("importResult").textContent = fmt(result);
    if (result.ok) {
      state.importedRunId = result.run_id;
      $("btnRejudge").style.display = "inline-flex";
    }
  } catch (err) {
    $("importResult").textContent = `导入失败: ${err.message}`;
  }
}

async function rejudgeImported() {
  if (!state.importedRunId) { alert("请先导入对话"); return; }
  if (!state.modelsOk.judge) {
    alert("请先在「配置」页面测试评判模型连接");
    return;
  }
  $("importJudgeResult").innerHTML = "<p style='color:var(--muted)'>评分中...</p>";
  try {
    const result = await post("/api/conversation/rejudge-imported", {
      run_id: state.importedRunId,
      judge_model: getModelConfig("judge"),
    });
    if (!result.ok) {
      $("importJudgeResult").innerHTML = `<p style='color:var(--red)'>评分失败: ${result.error}</p>`;
      return;
    }
    const jr = result.judge_result;
    const scoreHtml = `
      <div style="background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px">
        <div style="font-size:28px;font-weight:800;color:var(--accent)">${(jr.total_score || 0).toFixed(1)} 分</div>
        <div style="font-size:13px;color:var(--muted);margin-bottom:12px">${escHtml(jr.overall_summary || "")}</div>
        ${(jr.item_results || []).map((item) => `
          <div class="judge-item ${item.verdict}" style="margin-bottom:8px">
            <div class="judge-item-header">
              <span class="judge-verdict ${item.verdict}">${verdictLabel(item.verdict)}</span>
              <span class="judge-criterion">${escHtml(item.judge_point_id)}</span>
              <span style="font-size:12px;font-weight:700">${Math.round((item.score || 0) * 100)}分</span>
            </div>
            <div class="judge-reason">${escHtml(item.reason || "")}</div>
            ${item.suggested_fix ? `<div class="judge-fix">💡 ${escHtml(item.suggested_fix)}</div>` : ""}
          </div>`).join("")}
      </div>`;
    $("importJudgeResult").innerHTML = scoreHtml;
  } catch (err) {
    $("importJudgeResult").innerHTML = `<p style='color:var(--red)'>错误: ${err.message}</p>`;
  }
}

// ===== 停止运行 =====
function stopRun() {
  // SSE 没有原生 stop，关闭在 startSSE 内处理
  alert("请等待当前轮次完成，不支持强制中止。");
}

// ===== 工具 =====
function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

