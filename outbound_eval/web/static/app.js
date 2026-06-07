let taskSpec = null;
let coverage = null;

const $ = (id) => document.getElementById(id);
const post = async (url, body) => {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
};

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    button.classList.add("active");
    $(button.dataset.tab).classList.add("active");
  });
});

function modelConfig() {
  return {
    provider: "openai_compatible",
    base_url: $("baseUrl").value,
    api_key: $("apiKey").value,
    model_name: $("modelName").value,
    temperature: Number($("temperature").value),
    max_tokens: Number($("maxTokens").value),
    timeout_seconds: Number($("timeoutSeconds").value),
    connection_tested: false
  };
}

$("testModel").onclick = async () => {
  $("modelResult").textContent = "testing...";
  try {
    $("modelResult").textContent = JSON.stringify(await post("/api/model/test", modelConfig()), null, 2);
  } catch (err) {
    $("modelResult").textContent = String(err);
  }
};

$("compileTask").onclick = async () => {
  $("compileResult").textContent = "compiling...";
  try {
    const result = await post("/api/compile", { instruction: $("instruction").value });
    taskSpec = result.task_spec;
    $("compileResult").textContent = JSON.stringify(result, null, 2);
  } catch (err) {
    $("compileResult").textContent = String(err);
  }
};

$("planScenarios").onclick = async () => {
  if (!taskSpec) {
    $("coverageMatrix").innerHTML = "<div class='row'>⚠️ 请先完成任务编译（TaskSpec 缺失）</div>";
    return;
  }
  $("coverageMatrix").innerHTML = "<div class='row'>生成中，请稍候...</div>";
  try {
    const rawBudget = Number($("budget").value);
    const allowed = [8, 12, 20];
    const budget = allowed.reduce((prev, cur) => Math.abs(cur - rawBudget) < Math.abs(prev - rawBudget) ? cur : prev);
    const result = await post("/api/plan", { task_spec: taskSpec, budget });
    coverage = result;
    $("coverageMatrix").innerHTML = result.scenarios.map((scn) => `
      <div class="row"><strong>${scn.scenario_type}</strong> ${scn.scenario_name}<br>
      ${scn.covered_requirement_ids.map((id) => `<code>${id}</code>`).join(" ")}</div>`).join("");
  } catch (err) {
    $("coverageMatrix").innerHTML = `<div class='row' style='color:red'>生成失败: ${String(err)}</div>`;
  }
};

$("refreshStatus").onclick = async () => {
  const res = await fetch("/api/status");
  $("statusResult").textContent = JSON.stringify(await res.json(), null, 2);
};

$("startRun").onclick = async () => {
  $("statusResult").textContent = "running...";
  try {
    const result = await post("/api/run", {
      instruction: $("instruction").value,
      target_model_config: modelConfig(),
      budget: Number($("budget").value),
      attempts: Number($("attempts").value),
      parallel: 1
    });
    $("statusResult").textContent = JSON.stringify(result, null, 2);
    if (result.run_id) {
      $("reportRunId").value = result.run_id;
      $("reportFrame").src = result.report_url;
    }
  } catch (err) {
    $("statusResult").textContent = String(err);
  }
};

$("renderReport").onclick = () => {
  const runId = $("reportRunId").value.trim();
  $("reportFrame").src = runId ? `/api/report/${encodeURIComponent(runId)}/html` : "about:blank";
};

$("loadBadcases").onclick = async () => {
  const res = await fetch("/api/status");
  const data = await res.json();
  $("badcaseList").innerHTML = (data.badcases || []).map((item) => `
    <div class="row"><strong>${item.severity}</strong> ${item.summary}<br>
    <code>${item.episode_id}</code> ${item.requirement_ids.map((id) => `<code>${id}</code>`).join(" ")}</div>`).join("");
};

$("seedGolden").onclick = async () => {
  const scenarioIds = coverage ? coverage.scenarios.map((s) => s.scenario_id) : [];
  const requirementIds = taskSpec ? taskSpec.requirements.map((r) => r.id) : [];
  const result = await post("/api/golden/seed", { task_id: taskSpec?.task_id || "task_unknown", scenario_ids: scenarioIds, requirement_ids: requirementIds });
  $("goldenResult").textContent = JSON.stringify(result, null, 2);
};
