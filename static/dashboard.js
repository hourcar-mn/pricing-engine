/*
  Browser controller for the EVIE demand dashboard.

  The backend owns the demand model, kernel construction, forecasting, and
  optimization. This file collects form inputs, calls the JSON API, and renders
  tables/charts from the returned data. It deliberately uses plain JavaScript
  so the folder can be handed off without a frontend build step.
*/

const state = {
  options: null,
  basePricing: null,
  optimizationPricing: null,
  optimizationTimer: null,
};

const pricingParameterLabels = {
  trip_start_fee: "Trip start fee",
  per_minute: "Per minute",
  per_hour: "Per hour",
  per_day: "Per day",
  included_miles: "Included miles",
  extra_mile: "Extra-mile fee",
  monthly_fee: "Monthly fee",
  monthly_credit: "Monthly credit",
};

const pricingParameterOrder = [
  "trip_start_fee",
  "per_minute",
  "per_hour",
  "per_day",
  "included_miles",
  "extra_mile",
  "monthly_fee",
  "monthly_credit",
];

const fmtMoney = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const fmtMoney2 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const fmtNum = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
});

const colors = {
  actual: "#1f6f78",
  predicted: "#c84b31",
  noPrice: "#6f5aa7",
  current: "#1f6f78",
  whatIf: "#c84b31",
  totalActual: "#263238",
  totalPredicted: "#c84b31",
  freeActual: "#1f6f78",
  freePredicted: "#7aa6ac",
  paidActual: "#5b5f97",
  paidPredicted: "#c08497",
  grid: "#d8dee4",
  text: "#263238",
};

// Page initialization and form wiring.
document.addEventListener("DOMContentLoaded", async () => {
  bindTabs();
  bindForms();
  lockCountKernelControls();
  setDefaultKernelControls();
  try {
    await loadOptions();
  } catch (error) {
    setStatus("Error loading data");
    const output = document.getElementById("validationOutput");
    renderError(output, error);
  }
});

function lockCountKernelControls() {
  ["val", "what", "opt"].forEach((prefix) => {
    const count = document.getElementById(`${prefix}KernelCount`);
    if (count) {
      count.checked = true;
      count.disabled = true;
      const label = count.closest("label");
      if (label) label.classList.add("locked");
    }
    [1, 2, 3, 4].forEach((idx) => {
      const select = document.getElementById(`${prefix}KernelRelax${idx}`);
      if (!select) return;
      [...select.options].forEach((option) => {
        if (option.value === "count_bin") option.remove();
      });
    });
    const fourth = document.getElementById(`${prefix}KernelRelax4`);
    if (fourth && fourth.closest("label")) {
      fourth.closest("label").style.display = "none";
    }
  });
}

function setDefaultKernelControls() {
  ["val", "what", "opt"].forEach((prefix) => {
    ["Segment", "Plan", "Month"].forEach((suffix) => {
      const input = document.getElementById(`${prefix}Kernel${suffix}`);
      if (input) input.checked = false;
    });
  });
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      button.classList.add("active");
      document.getElementById(button.dataset.tab).classList.add("active");
    });
  });
}

function bindForms() {
  document.getElementById("runValidation").addEventListener("click", async () => {
    await runValidation();
  });

  document.getElementById("runWhatIf").addEventListener("click", async () => {
    await runWhatIf();
  });

  document.getElementById("runOptimization").addEventListener("click", async () => {
    await runOptimization();
  });

  document.getElementById("loadPricing").addEventListener("click", async () => {
    await loadBasePricing();
  });

  document.getElementById("loadOptBounds").addEventListener("click", async () => {
    await loadOptimizationBounds();
  });

  document.getElementById("selectAllOptParams").addEventListener("click", () => {
    document.querySelectorAll("#optimizationBounds input[data-role='enabled']").forEach((input) => {
      input.checked = true;
    });
  });

  document.getElementById("clearOptParams").addEventListener("click", () => {
    document.querySelectorAll("#optimizationBounds input[data-role='enabled']").forEach((input) => {
      input.checked = false;
    });
  });

  document.getElementById("whatSegment").addEventListener("change", () => {
    state.basePricing = null;
    document.getElementById("pricingControls").innerHTML = "";
  });

  document.getElementById("whatTrainEnd").addEventListener("change", () => {
    state.basePricing = null;
    document.getElementById("pricingControls").innerHTML = "";
  });

  document.getElementById("optSegment").addEventListener("change", () => {
    state.optimizationPricing = null;
    document.getElementById("optimizationBounds").innerHTML = "";
  });

  document.getElementById("optTrainEnd").addEventListener("change", () => {
    state.optimizationPricing = null;
    document.getElementById("optimizationBounds").innerHTML = "";
  });
}

// API calls triggered by each dashboard tab.
async function loadOptions() {
  setStatus("Loading data...");
  const options = await apiGet("/api/options");
  state.options = options;

  ["valSegment", "whatSegment", "optSegment"].forEach((id) => {
    const select = document.getElementById(id);
    select.innerHTML = "";
    options.segments.forEach((segmentInfo) => {
      const option = document.createElement("option");
      option.value = segmentInfo.segment;
      option.textContent = segmentInfo.segment;
      select.appendChild(option);
    });
  });

  const defaults = options.defaults;
  document.getElementById("valSegment").value = "Regular";
  document.getElementById("valTrainStart").value = defaults.train_start;
  document.getElementById("valTrainEnd").value = defaults.train_end;
  document.getElementById("valTestStart").value = defaults.test_start;
  document.getElementById("valTestEnd").value = defaults.test_end;

  document.getElementById("whatSegment").value = "Regular";
  document.getElementById("whatTrainStart").value = defaults.train_start;
  document.getElementById("whatTrainEnd").value = defaults.what_if_train_end;
  document.getElementById("forecastStart").value = defaults.forecast_start;
  document.getElementById("horizonMonths").value = 6;
  document.getElementById("marketMultiplier").value = 1;

  document.getElementById("optSegment").value = "Regular";
  document.getElementById("optTrainStart").value = defaults.train_start;
  document.getElementById("optTrainEnd").value = defaults.what_if_train_end;
  document.getElementById("optForecastStart").value = defaults.forecast_start;
  document.getElementById("optHorizonMonths").value = 6;
  document.getElementById("optMarketMultiplier").value = 1;
  document.getElementById("optMaxDemandDropPct").value = 5;
  document.getElementById("optMaxEvaluations").value = 500;
  document.getElementById("optNumSeeds").value = 3;
  document.getElementById("optSeed").value = 123;

  await loadBasePricing();
  await loadOptimizationBounds();
  setStatus("Ready");
}

async function loadBasePricing() {
  setStatus("Loading pricing...");
  const segment = document.getElementById("whatSegment").value;
  const trainEnd = document.getElementById("whatTrainEnd").value;
  const dropLatest = document.getElementById("whatDropLatest").checked;
  const params = new URLSearchParams({
    segment,
    train_end: trainEnd,
    drop_latest: String(dropLatest),
  });
  state.basePricing = await apiGet(`/api/base-pricing?${params.toString()}`);
  renderPricingControls(state.basePricing);
  setStatus("Ready");
}

async function loadOptimizationBounds() {
  setStatus("Loading optimization bounds...");
  const segment = document.getElementById("optSegment").value;
  const trainEnd = document.getElementById("optTrainEnd").value;
  const dropLatest = document.getElementById("optDropLatest").checked;
  const params = new URLSearchParams({
    segment,
    train_end: trainEnd,
    drop_latest: String(dropLatest),
  });
  state.optimizationPricing = await apiGet(`/api/base-pricing?${params.toString()}`);
  renderOptimizationBounds(state.optimizationPricing);
  setStatus("Ready");
}

async function runValidation() {
  const output = document.getElementById("validationOutput");
  setLoading(output, "Estimating validation model...");

  let payload;
  try {
    payload = {
      segment: document.getElementById("valSegment").value,
      train_start: document.getElementById("valTrainStart").value,
      train_end: document.getElementById("valTrainEnd").value,
      test_start: document.getElementById("valTestStart").value,
      test_end: document.getElementById("valTestEnd").value,
      drop_latest: document.getElementById("valDropLatest").checked,
      model_spec: collectModelSpec("val"),
      bin_spec: collectBinSpec("val"),
      kernel_spec: collectKernelSpec("val"),
    };
  } catch (error) {
    renderError(output, error);
    setStatus("Error");
    return;
  }

  try {
    setStatus("Running validation...");
    const result = await apiPost("/api/validate", payload);
    renderValidation(result);
    setStatus("Ready");
  } catch (error) {
    renderError(output, error);
    setStatus("Error");
  }
}

async function runWhatIf() {
  const output = document.getElementById("whatIfOutput");
  setLoading(output, "Estimating model and forecasting what-if scenario...");

  if (!state.basePricing) {
    await loadBasePricing();
  }

  let payload;
  try {
    payload = {
      segment: document.getElementById("whatSegment").value,
      train_start: document.getElementById("whatTrainStart").value,
      train_end: document.getElementById("whatTrainEnd").value,
      forecast_start: document.getElementById("forecastStart").value,
      horizon_months: Number(document.getElementById("horizonMonths").value),
      market_multiplier: Number(document.getElementById("marketMultiplier").value),
      drop_latest: document.getElementById("whatDropLatest").checked,
      model_spec: collectModelSpec("what"),
      bin_spec: collectBinSpec("what"),
      kernel_spec: collectKernelSpec("what"),
      pricing: collectPricingInputs(),
    };
  } catch (error) {
    renderError(output, error);
    setStatus("Error");
    return;
  }

  try {
    setStatus("Running what-if...");
    const result = await apiPost("/api/what-if", payload);
    renderWhatIf(result);
    setStatus("Ready");
  } catch (error) {
    renderError(output, error);
    setStatus("Error");
  }
}

async function runOptimization() {
  const output = document.getElementById("optimizationOutput");
  setLoading(output, "Running constrained pricing optimization...");

  if (!state.optimizationPricing) {
    await loadOptimizationBounds();
  }

  let payload;
  try {
    payload = {
      segment: document.getElementById("optSegment").value,
      train_start: document.getElementById("optTrainStart").value,
      train_end: document.getElementById("optTrainEnd").value,
      forecast_start: document.getElementById("optForecastStart").value,
      horizon_months: Number(document.getElementById("optHorizonMonths").value),
      market_multiplier: Number(document.getElementById("optMarketMultiplier").value),
      max_demand_drop: Number(document.getElementById("optMaxDemandDropPct").value) / 100,
      max_evaluations: Number(document.getElementById("optMaxEvaluations").value),
      num_seeds: Number(document.getElementById("optNumSeeds").value),
      seed: Number(document.getElementById("optSeed").value),
      enforce_monthly_demand: document.getElementById("optMonthlyDemandConstraint").checked,
      tie_time_rates: document.getElementById("optTieTimeRates").checked,
      use_scipy_polish: document.getElementById("optUseScipy").checked,
      drop_latest: document.getElementById("optDropLatest").checked,
      model_spec: collectModelSpec("opt"),
      bin_spec: collectBinSpec("opt"),
      kernel_spec: collectKernelSpec("opt"),
      variables: collectOptimizationVariables(),
    };
  } catch (error) {
    renderError(output, error);
    setStatus("Error");
    return;
  }

  try {
    setStatus("Optimizing...");
    const stopProgress = startOptimizationProgress(output, payload);
    const started = await apiPost("/api/optimize/start", payload);
    const result = await waitForOptimizationJob(started.job_id);
    stopProgress();
    renderOptimization(result);
    setStatus("Ready");
  } catch (error) {
    stopOptimizationProgress();
    renderError(output, error);
    setStatus("Error");
  }
}

async function waitForOptimizationJob(jobId) {
  if (!jobId) throw new Error("Optimization job did not return a job id.");
  while (true) {
    const status = await apiGet(`/api/optimize/status?job_id=${encodeURIComponent(jobId)}`);
    updateOptimizationProgress(status.progress || {});
    if (status.status === "complete") {
      return status.result;
    }
    if (status.status === "error") {
      throw new Error(status.error || "Optimization failed.");
    }
    await delay(750);
  }
}

// Renderers for the three dashboard workflows.
function renderValidation(result) {
  const output = document.getElementById("validationOutput");
  const monthly = mergeNoPriceMonthly(result.monthly, result.monthly_no_price);
  const productRows = sortProducts(ensureValidationNoUseRow(result.product_summary, monthly));
  const paidPlan = paidPlanName(result);
  const predictedRevenueLabel = result.model_spec && result.model_spec.include_price ? "Predicted with price" : "Predicted";

  output.innerHTML = "";
  output.appendChild(renderRunHeader(result));
  output.appendChild(renderWarnings(result.warnings));
  output.appendChild(
    renderKpis([
      ["Train revenue MAE", fmtMoney.format(result.metrics.train.user_payment_revenue_mae)],
      ["Test revenue MAE", fmtMoney.format(result.metrics.test.user_payment_revenue_mae)],
      ["Train Free Plan trips MAE", fmtNum.format(result.metrics.train.free_plan_trips_mae)],
      ["Test Free Plan trips MAE", fmtNum.format(result.metrics.test.free_plan_trips_mae)],
      [`Train ${paidPlan} trips MAE`, fmtNum.format(result.metrics.train.paid_plan_trips_mae)],
      [`Test ${paidPlan} trips MAE`, fmtNum.format(result.metrics.test.paid_plan_trips_mae)],
      ["Train total trips MAE", fmtNum.format(result.metrics.train.total_trips_mae)],
      ["Test total trips MAE", fmtNum.format(result.metrics.test.total_trips_mae)],
    ])
  );
  output.appendChild(renderSection("Model Specification", renderModelSpec(result.model_spec)));
  output.appendChild(renderSection("Estimation Fit Summary", renderFitSummary(result.fit_summary)));
  output.appendChild(renderSection("Bins and Kernel", renderBinningKernelInfo(result)));
  output.appendChild(renderSection("Kernel Distribution Explorer", renderKernelExplorer(result.kernel_details)));
  output.appendChild(renderSection("Estimated Utility Parameters", renderCoefficientTable(result.coefficients)));
  output.appendChild(renderSection("Fit Metrics", renderMetricsTable(result.metrics, paidPlan)));

  output.appendChild(
    renderSection(
      "Monthly User-Payment Revenue",
      lineChart({
        data: monthly,
        xKey: "YearMonth",
        splitKey: "split",
        series: [
          { key: "actual_user_payment_revenue", label: "Actual", color: colors.actual },
          { key: "predicted_user_payment_revenue", label: predictedRevenueLabel, color: colors.predicted },
        ],
        yFormatter: shortMoney,
        height: 330,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Monthly Trips By Plan",
      lineChart({
        data: monthly,
        xKey: "YearMonth",
        splitKey: "split",
        series: [
          { key: "actual_total_trips", label: "Actual total", color: colors.totalActual },
          { key: "predicted_total_trips", label: "Predicted total", color: colors.totalPredicted, dash: "7 4" },
          { key: "actual_free_plan_trips", label: "Actual Free Plan", color: colors.freeActual },
          { key: "predicted_free_plan_trips", label: "Predicted Free Plan", color: colors.freePredicted, dash: "7 4" },
          { key: "actual_paid_plan_trips", label: `Actual ${paidPlan}`, color: colors.paidActual },
          { key: "predicted_paid_plan_trips", label: `Predicted ${paidPlan}`, color: colors.paidPredicted, dash: "7 4" },
        ],
        yFormatter: shortNumber,
        height: 390,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Product Choices on Testing Months: Actual vs Predicted",
      groupedBarChart({
        data: productRows,
        labelKey: "product",
        series: [
          { key: "actual_test_positive_users", label: "Actual testing choices", color: colors.actual },
          { key: "predicted_test_positive_users", label: "Predicted testing choices", color: colors.predicted },
        ],
        yFormatter: shortNumber,
        height: 560,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Expected User Payment By Product",
      renderExpectedPriceChart(productRows, [
        { key: "expected_user_payment", label: "Expected user payment", color: colors.predicted },
      ], "Bars are averages over the testing months. For each product-month, the dashboard reprices the empirical kernel using that month&apos;s observed tariff; if the kernel conditions on month-of-year, those month-specific expected payments are computed first and then averaged for this chart.")
    )
  );
}

function renderWhatIf(result) {
  const output = document.getElementById("whatIfOutput");
  const current = result.monthly.filter((row) => row.scenario === "Current pricing");
  const whatIf = result.monthly.filter((row) => row.scenario === "What-if pricing");
  const paidPlan = paidPlanName(result);
  const monthly = current.map((row) => {
    const alt = whatIf.find((candidate) => candidate.YearMonth === row.YearMonth) || {};
    return {
      YearMonth: row.YearMonth,
      current_revenue: row.predicted_user_payment_revenue,
      what_if_revenue: alt.predicted_user_payment_revenue,
      current_total_trips: row.predicted_trips,
      what_if_total_trips: alt.predicted_trips,
      current_free_plan_trips: row.predicted_free_plan_trips,
      what_if_free_plan_trips: alt.predicted_free_plan_trips,
      current_paid_plan_trips: row.predicted_paid_plan_trips,
      what_if_paid_plan_trips: alt.predicted_paid_plan_trips,
    };
  });

  const currentTotals = scenarioTotals(current);
  const whatIfTotals = scenarioTotals(whatIf);
  const deltaRevenue = whatIfTotals.revenue - currentTotals.revenue;
  const deltaTrips = whatIfTotals.trips - currentTotals.trips;
  const deltaFreeTrips = whatIfTotals.freeTrips - currentTotals.freeTrips;
  const deltaPaidTrips = whatIfTotals.paidTrips - currentTotals.paidTrips;
  const avgCurrentTrips = currentTotals.trips / Math.max(current.length, 1);
  const avgTripChange = deltaTrips / Math.max(current.length, 1);
  const avgCurrentFreeTrips = currentTotals.freeTrips / Math.max(current.length, 1);
  const avgFreeTripChange = deltaFreeTrips / Math.max(current.length, 1);
  const avgCurrentPaidTrips = currentTotals.paidTrips / Math.max(current.length, 1);
  const avgPaidTripChange = deltaPaidTrips / Math.max(current.length, 1);
  const whatIfProductRows = sortProducts(ensureWhatIfNoUseRow(result.product_summary, result.monthly));
  const whatIfPlanTripRows = planTripRows(
    whatIfProductRows,
    "current_predicted_trips",
    "what_if_predicted_trips",
    "change_predicted_trips",
    ["current_predicted_trips", "what_if_predicted_trips"]
  );

  output.innerHTML = "";
  output.appendChild(renderRunHeader(result));
  output.appendChild(renderWarnings(result.warnings));
  output.appendChild(
    renderKpis([
      ["Price coefficient", formatCoef(result.price_coefficient)],
      ["Current forecast revenue", fmtMoney.format(currentTotals.revenue)],
      ["What-if forecast revenue", fmtMoney.format(whatIfTotals.revenue)],
      ["Revenue change", signedMoney(deltaRevenue)],
      ["Current avg monthly trips", fmtNum.format(avgCurrentTrips)],
      ["Avg monthly trip change", signedNumber(avgTripChange)],
      ["Current Free Plan avg trips", fmtNum.format(avgCurrentFreeTrips)],
      ["Free Plan avg trip change", signedNumber(avgFreeTripChange)],
      [`Current ${paidPlan} avg trips`, fmtNum.format(avgCurrentPaidTrips)],
      [`${paidPlan} avg trip change`, signedNumber(avgPaidTripChange)],
    ])
  );

  output.appendChild(renderSection("Forecast Market Size", renderMarketSizeInfo(result, current)));
  output.appendChild(renderSection("Model Specification", renderModelSpec(result.model_spec)));
  output.appendChild(renderSection("Estimation Fit Summary", renderFitSummary(result.fit_summary)));
  output.appendChild(renderSection("Bins and Kernel", renderBinningKernelInfo(result)));
  output.appendChild(renderSection("Kernel Distribution Explorer", renderKernelExplorer(result.kernel_details)));
  output.appendChild(renderSection("Estimated Utility Parameters", renderCoefficientTable(result.coefficients)));
  output.appendChild(renderSection("Pricing Used In Forecast", renderPricingComparison(result.pricing)));

  output.appendChild(
    renderSection(
      "Forecast User-Payment Revenue",
      lineChart({
        data: monthly,
        xKey: "YearMonth",
        series: [
          { key: "current_revenue", label: "Current pricing", color: colors.current },
          { key: "what_if_revenue", label: "What-if pricing", color: colors.whatIf },
        ],
        yFormatter: shortMoney,
        height: 330,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Forecast Trips By Plan",
      lineChart({
        data: monthly,
        xKey: "YearMonth",
        series: [
          { key: "current_total_trips", label: "Current total", color: colors.totalActual },
          { key: "what_if_total_trips", label: "What-if total", color: colors.totalPredicted, dash: "7 4" },
          { key: "current_free_plan_trips", label: "Current Free Plan", color: colors.freeActual },
          { key: "what_if_free_plan_trips", label: "What-if Free Plan", color: colors.freePredicted, dash: "7 4" },
          { key: "current_paid_plan_trips", label: `Current ${paidPlan}`, color: colors.paidActual },
          { key: "what_if_paid_plan_trips", label: `What-if ${paidPlan}`, color: colors.paidPredicted, dash: "7 4" },
        ],
        yFormatter: shortNumber,
        height: 390,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Trips By Plan: Current vs What-if Pricing",
      groupedBarChart({
        data: whatIfPlanTripRows,
        labelKey: "plan",
        series: [
          { key: "current_predicted_trips", label: "Current pricing", color: colors.current },
          { key: "what_if_predicted_trips", label: "What-if pricing", color: colors.whatIf },
        ],
        yFormatter: shortNumber,
        height: 260,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Product Choices: Current vs What-if Pricing",
      groupedBarChart({
        data: whatIfProductRows,
        labelKey: "product",
        series: [
          { key: "current_positive_users", label: "Current pricing", color: colors.current },
          { key: "what_if_positive_users", label: "What-if pricing", color: colors.whatIf },
        ],
        yFormatter: shortNumber,
        height: 560,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Product Choice Reallocation",
      divergingBarChart({
        data: demandChangeRows(whatIfProductRows, "change_positive_users"),
        labelKey: "product",
        valueKey: "change_positive_users",
        yFormatter: shortNumber,
        positiveLabel: "Choice gain",
        negativeLabel: "Choice loss",
        height: 560,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Expected User Payment By Product",
      renderExpectedPriceChart(whatIfProductRows, [
        { key: "current_expected_user_payment", label: "Current pricing", color: colors.current },
        { key: "what_if_expected_user_payment", label: "What-if pricing", color: colors.whatIf },
      ], "Bars are forecast-month averages. For each product-month, the dashboard reprices the empirical kernel under the current or what-if tariff; if the kernel conditions on month-of-year, those month-specific expected payments are computed first and then averaged over the displayed forecast horizon.")
    )
  );
}

function renderOptimization(result) {
  const output = document.getElementById("optimizationOutput");
  const baseline = result.monthly.filter((row) => row.scenario === "Baseline pricing");
  const optimized = result.monthly.filter((row) => row.scenario === "Optimized pricing");
  const paidPlan = paidPlanName(result);
  const monthly = baseline.map((row) => {
    const alt = optimized.find((candidate) => candidate.YearMonth === row.YearMonth) || {};
    return {
      YearMonth: row.YearMonth,
      baseline_revenue: row.predicted_user_payment_revenue,
      optimized_revenue: alt.predicted_user_payment_revenue,
      baseline_total_trips: row.predicted_trips,
      optimized_total_trips: alt.predicted_trips,
      baseline_free_plan_trips: row.predicted_free_plan_trips,
      optimized_free_plan_trips: alt.predicted_free_plan_trips,
      baseline_paid_plan_trips: row.predicted_paid_plan_trips,
      optimized_paid_plan_trips: alt.predicted_paid_plan_trips,
    };
  });
  const baselineTotals = scenarioTotals(baseline);
  const optimizedTotals = scenarioTotals(optimized);
  const deltaTrips = optimizedTotals.trips - baselineTotals.trips;
  const deltaFreeTrips = optimizedTotals.freeTrips - baselineTotals.freeTrips;
  const deltaPaidTrips = optimizedTotals.paidTrips - baselineTotals.paidTrips;
  const horizon = Math.max(baseline.length, 1);
  const optimizationProductRows = sortProducts(ensureOptimizationNoUseRow(result.product_summary, result.monthly));
  const optimizationPlanTripRows = planTripRows(
    optimizationProductRows,
    "baseline_predicted_trips",
    "optimized_predicted_trips",
    "change_predicted_trips",
    ["baseline_predicted_trips", "optimized_predicted_trips"]
  );

  output.innerHTML = "";
  output.appendChild(renderRunHeader(result));
  output.appendChild(renderWarnings(result.warnings));
  output.appendChild(
    renderKpis([
      ["Price coefficient", formatCoef(result.price_coefficient)],
      ["Baseline revenue", fmtMoney.format(result.summary.baseline_user_payment_revenue)],
      ["Optimized revenue", fmtMoney.format(result.summary.optimized_user_payment_revenue)],
      ["Revenue lift", signedMoney(result.summary.revenue_lift)],
      ["Total trip change", signedNumber(deltaTrips)],
      ["Avg monthly trip change", signedNumber(deltaTrips / horizon)],
      ["Free Plan trip change", signedNumber(deltaFreeTrips)],
      [`${paidPlan} trip change`, signedNumber(deltaPaidTrips)],
      ["Candidate prices satisfying trip constraint", `${result.optimization_status.feasible_evaluations} / ${result.optimization_status.evaluations}`],
      ["Seeds", String(result.config.num_seeds)],
    ])
  );

  output.appendChild(renderSection("Model Specification", renderModelSpec(result.model_spec)));
  output.appendChild(renderSection("Estimation Fit Summary", renderFitSummary(result.fit_summary)));
  output.appendChild(renderSection("Bins and Kernel", renderBinningKernelInfo(result)));
  output.appendChild(renderSection("Kernel Distribution Explorer", renderKernelExplorer(result.kernel_details)));
  output.appendChild(renderSection("Estimated Utility Parameters", renderCoefficientTable(result.coefficients)));
  output.appendChild(renderSection("Trip-Volume Constraint", renderConstraintTable(result.constraint)));
  output.appendChild(renderSection("Optimizer Diagnostics", renderOptimizerDiagnostics(result)));
  output.appendChild(renderSection("Optimized Pricing Parameters", renderOptimizationParameterTable(result.parameter_rows)));

  if (result.convergence && result.convergence.length) {
    output.appendChild(
      renderSection(
        "Best Feasible Revenue Lift During Search",
        lineChart({
          data: result.convergence,
          xKey: "evaluation",
          series: [
            { key: "best_feasible_lift", label: "Best feasible lift", color: colors.whatIf },
          ],
          yFormatter: shortMoney,
          height: 300,
        })
      )
    );
  }

  output.appendChild(
    renderSection(
      "Forecast User-Payment Revenue",
      lineChart({
        data: monthly,
        xKey: "YearMonth",
        series: [
          { key: "baseline_revenue", label: "Baseline pricing", color: colors.current },
          { key: "optimized_revenue", label: "Optimized pricing", color: colors.whatIf },
        ],
        yFormatter: shortMoney,
        height: 330,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Forecast Trips By Plan",
      lineChart({
        data: monthly,
        xKey: "YearMonth",
        series: [
          { key: "baseline_total_trips", label: "Baseline total", color: colors.totalActual },
          { key: "optimized_total_trips", label: "Optimized total", color: colors.totalPredicted, dash: "7 4" },
          { key: "baseline_free_plan_trips", label: "Baseline Free Plan", color: colors.freeActual },
          { key: "optimized_free_plan_trips", label: "Optimized Free Plan", color: colors.freePredicted, dash: "7 4" },
          { key: "baseline_paid_plan_trips", label: `Baseline ${paidPlan}`, color: colors.paidActual },
          { key: "optimized_paid_plan_trips", label: `Optimized ${paidPlan}`, color: colors.paidPredicted, dash: "7 4" },
        ],
        yFormatter: shortNumber,
        height: 390,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Trips By Plan: Baseline vs Optimized Pricing",
      groupedBarChart({
        data: optimizationPlanTripRows,
        labelKey: "plan",
        series: [
          { key: "baseline_predicted_trips", label: "Baseline pricing", color: colors.current },
          { key: "optimized_predicted_trips", label: "Optimized pricing", color: colors.whatIf },
        ],
        yFormatter: shortNumber,
        height: 260,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Product Choices: Baseline vs Optimized Pricing",
      groupedBarChart({
        data: optimizationProductRows,
        labelKey: "product",
        series: [
          { key: "baseline_positive_users", label: "Baseline pricing", color: colors.current },
          { key: "optimized_positive_users", label: "Optimized pricing", color: colors.whatIf },
        ],
        yFormatter: shortNumber,
        height: 560,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Average Monthly Product Choice Reallocation",
      divergingBarChart({
        data: demandChangeRows(optimizationProductRows, "change_positive_users"),
        labelKey: "product",
        valueKey: "change_positive_users",
        yFormatter: shortNumber,
        positiveLabel: "Choice gain",
        negativeLabel: "Choice loss",
        height: 560,
      })
    )
  );

  output.appendChild(
    renderSection(
      "Expected User Payment By Product",
      renderExpectedPriceChart(optimizationProductRows, [
        { key: "baseline_expected_user_payment", label: "Baseline pricing", color: colors.current },
        { key: "optimized_expected_user_payment", label: "Optimized pricing", color: colors.whatIf },
      ], "Bars are forecast-month averages. For each product-month, the dashboard reprices the empirical kernel under baseline or optimized pricing; if the kernel conditions on month-of-year, those month-specific expected payments are computed first and then averaged over the displayed forecast horizon.")
    )
  );

  if (result.sensitivity && result.sensitivity.length) {
    output.appendChild(renderSection("Local Sensitivity Around Reported Solution", renderSensitivityTable(result.sensitivity)));
  }
}

// Dynamic input controls for pricing and optimization bounds.
function renderPricingControls(basePricing) {
  const container = document.getElementById("pricingControls");
  container.innerHTML = "";

  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent =
    "Edit absolute pricing parameters. Leaving a field equal to its loaded value means the current real-world value is used.";
  container.appendChild(help);

  Object.entries(basePricing.pricing).forEach(([plan, values]) => {
    const card = document.createElement("div");
    card.className = "pricing-card";
    card.dataset.plan = plan;

    const title = document.createElement("h3");
    title.textContent = plan;
    card.appendChild(title);

    const grid = document.createElement("div");
    grid.className = "pricing-grid";

    [
      ["trip_start_fee", "Trip start fee"],
      ["per_minute", "Per minute"],
      ["per_hour", "Per hour"],
      ["per_day", "Per day"],
      ["included_miles", "Included miles"],
      ["extra_mile", "Extra-mile fee"],
      ["monthly_fee", "Monthly fee"],
      ["monthly_credit", "Monthly credit"],
    ].forEach(([key, label]) => {
      const wrap = document.createElement("label");
      wrap.className = "pricing-field";
      wrap.innerHTML = `<span>${label}</span>`;
      const input = document.createElement("input");
      input.type = "number";
      input.step = key === "included_miles" ? "1" : "0.01";
      input.value = Number(values[key] ?? 0);
      input.dataset.plan = plan;
      input.dataset.param = key;
      wrap.appendChild(input);
      grid.appendChild(wrap);
    });

    card.appendChild(grid);
    container.appendChild(card);
  });
}

function renderOptimizationBounds(basePricing) {
  const container = document.getElementById("optimizationBounds");
  const rows = [];
  Object.entries(basePricing.pricing).forEach(([plan, values]) => {
    pricingParameterOrder.forEach((param) => {
      const current = Number(values[param] ?? 0);
      const [lower, upper] = defaultBounds(param, current);
      rows.push({ plan, param, current, lower, upper });
    });
  });

  const tableEl = document.createElement("table");
  tableEl.className = "bounds-grid-table";
  tableEl.innerHTML = `
    <thead>
      <tr>
        <th>Optimize</th>
        <th>Plan</th>
        <th>Parameter</th>
        <th>Current</th>
        <th>Lower bound</th>
        <th>Upper bound</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = tableEl.querySelector("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" data-role="enabled" data-plan="${escapeHtml(row.plan)}" data-param="${escapeHtml(row.param)}"></td>
      <td>${escapeHtml(row.plan)}</td>
      <td>${escapeHtml(pricingParameterLabels[row.param] || row.param)}</td>
      <td>${fixed(row.current, row.param === "included_miles" ? 0 : 3)}</td>
      <td><input type="number" step="${row.param === "included_miles" ? "1" : "0.01"}" data-role="lower" data-plan="${escapeHtml(row.plan)}" data-param="${escapeHtml(row.param)}" value="${row.lower}"></td>
      <td><input type="number" step="${row.param === "included_miles" ? "1" : "0.01"}" data-role="upper" data-plan="${escapeHtml(row.plan)}" data-param="${escapeHtml(row.param)}" value="${row.upper}"></td>
    `;
    tbody.appendChild(tr);
  });

  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent =
    "Check the pricing components to optimize. Unchecked non-time components stay at current pricing. Equal lower and upper bounds fix a checked component. If proportional time rates is on, selecting any time-rate row for a plan optimizes one multiplier for that plan's per-minute, per-hour, and per-day rates.";
  container.innerHTML = "";
  container.appendChild(help);
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  wrap.appendChild(tableEl);
  container.appendChild(wrap);
}

function defaultBounds(param, current) {
  if (param === "included_miles") {
    const lower = Math.max(0, Math.floor(current * 0.5));
    const upper = Math.max(lower + 1, Math.ceil(Math.max(current * 1.5, current + 25)));
    return [lower, upper];
  }
  const increments = {
    trip_start_fee: 3,
    per_minute: 0.2,
    per_hour: 10,
    per_day: 50,
    extra_mile: 1,
    monthly_fee: 10,
    monthly_credit: 10,
  };
  if (param === "monthly_credit" || param === "monthly_fee") {
    const lower = Math.max(0, roundTo(current * 0.5, 2));
    const upper = roundTo(Math.max(current * 1.5, current + increments[param]), 2);
    return [lower, upper];
  }
  const lower = Math.max(0, roundTo(current * 0.5, 2));
  const upper = roundTo(Math.max(current * 1.5, current + (increments[param] || 1)), 2);
  return [lower, upper];
}

// Form collectors and client-side validation.
function collectPricingInputs() {
  const pricing = {};
  document.querySelectorAll("#pricingControls input").forEach((input) => {
    const plan = input.dataset.plan;
    const param = input.dataset.param;
    if (!pricing[plan]) pricing[plan] = {};
    pricing[plan][param] = Number(input.value);
  });
  return pricing;
}

function collectModelSpec(prefix) {
  return {
    include_intercept: document.getElementById(`${prefix}SpecIntercept`).checked,
    include_price: document.getElementById(`${prefix}SpecPrice`).checked,
    include_count_fe: document.getElementById(`${prefix}SpecCountFe`).checked,
    include_plan_fe: document.getElementById(`${prefix}SpecPlanFe`).checked,
    include_month_fe: document.getElementById(`${prefix}SpecMonthFe`).checked,
  };
}

function collectBinSpec(prefix) {
  const text = document.getElementById(`${prefix}BinSpec`).value || "";
  const items = text
    .replace(/,/g, "\n")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  validateBinSpec(items);
  return items;
}

function validateBinSpec(items) {
  if (!items.length) {
    throw new Error("Trip-count bins: enter at least one bin.");
  }
  if (items.length > 40) {
    throw new Error("Trip-count bins: use 40 or fewer bins.");
  }
  const bins = items.map(parseBinItem);
  const labels = new Set();
  bins.forEach((bin) => {
    if (labels.has(bin.label)) throw new Error(`Trip-count bins: duplicate label ${bin.label}.`);
    labels.add(bin.label);
  });
  if (bins[0].lo !== 1) {
    throw new Error("Trip-count bins must start at 1.");
  }
  let prevHi = 0;
  bins.forEach((bin, index) => {
    if (prevHi === null) {
      throw new Error("Only the final trip-count bin can be open-ended.");
    }
    if (bin.lo <= prevHi) {
      throw new Error(`Trip-count bin ${bin.label} overlaps with the previous bin.`);
    }
    if (bin.lo > prevHi + 1) {
      throw new Error(`Trip-count bin ${bin.label} leaves a gap after ${prevHi}.`);
    }
    if (bin.hi === null) {
      if (index !== bins.length - 1) {
        throw new Error("Only the final trip-count bin can be open-ended.");
      }
      prevHi = null;
    } else {
      prevHi = bin.hi;
    }
  });
  if (bins[bins.length - 1].hi !== null) {
    throw new Error("The final trip-count bin must be open-ended, e.g. 21+.");
  }
}

function parseBinItem(raw) {
  const label = String(raw).trim().replace(/\s+/g, "");
  let lo;
  let hi;
  if (label.endsWith("+")) {
    lo = Number(label.slice(0, -1));
    hi = null;
  } else if (label.includes("-")) {
    const parts = label.split("-");
    if (parts.length !== 2) throw new Error(`Invalid trip-count bin ${label}.`);
    lo = Number(parts[0]);
    hi = Number(parts[1]);
  } else {
    lo = Number(label);
    hi = lo;
  }
  if (!Number.isInteger(lo) || lo < 1) {
    throw new Error(`Invalid trip-count bin ${label}: lower bound must be a positive integer.`);
  }
  if (hi !== null && (!Number.isInteger(hi) || hi < lo)) {
    throw new Error(`Invalid trip-count bin ${label}: upper bound must be an integer at least as large as the lower bound.`);
  }
  return { label, lo, hi };
}

function collectKernelSpec(prefix) {
  return {
    fallback_style: "ranked",
    condition_segment: document.getElementById(`${prefix}KernelSegment`).checked,
    condition_plan: document.getElementById(`${prefix}KernelPlan`).checked,
    condition_count_bin: true,
    condition_month_of_year: document.getElementById(`${prefix}KernelMonth`).checked,
    relax_order: collectKernelRelaxOrder(prefix),
    allow_low_support: false,
    min_support: Number(document.getElementById(`${prefix}KernelMinSupport`).value),
  };
}

function collectKernelRelaxOrder(prefix) {
  const values = [1, 2, 3].map((i) => document.getElementById(`${prefix}KernelRelax${i}`).value);
  const order = [];
  values.forEach((value) => {
    if (value && !order.includes(value)) order.push(value);
  });
  ["plan", "segment", "month_of_year"].forEach((value) => {
    if (!order.includes(value)) order.push(value);
  });
  return order;
}

function collectOptimizationVariables() {
  const rows = [];
  document.querySelectorAll("#optimizationBounds input[data-role='enabled']").forEach((enabledInput) => {
    const plan = enabledInput.dataset.plan;
    const param = enabledInput.dataset.param;
    const lowerInput = document.querySelector(
      `#optimizationBounds input[data-role='lower'][data-plan="${cssEscape(plan)}"][data-param="${cssEscape(param)}"]`
    );
    const upperInput = document.querySelector(
      `#optimizationBounds input[data-role='upper'][data-plan="${cssEscape(plan)}"][data-param="${cssEscape(param)}"]`
    );
    rows.push({
      plan,
      param,
      enabled: enabledInput.checked,
      lower: Number(lowerInput.value),
      upper: Number(upperInput.value),
    });
  });
  return rows;
}

// Shared rendering helpers for summaries, warnings, tables, and charts.
function renderRunHeader(result) {
  const box = document.createElement("div");
  box.className = "run-header";
  box.innerHTML = `
    <div>
      <span class="eyebrow">Segment</span>
      <strong>${escapeHtml(result.config.segment)}</strong>
    </div>
    <div>
      <span class="eyebrow">Training months</span>
      <strong>${escapeHtml(result.config.train_start)} to ${escapeHtml(result.config.train_end)}</strong>
    </div>
    ${
      result.config.test_start
        ? `<div><span class="eyebrow">Testing months</span><strong>${escapeHtml(result.config.test_start)} to ${escapeHtml(
            result.config.test_end
          )}</strong></div>`
        : `<div><span class="eyebrow">Forecast months</span><strong>${escapeHtml(result.config.forecast_start)} onward</strong></div>`
    }
  `;
  return box;
}

function renderWarnings(warnings) {
  const wrap = document.createElement("div");
  wrap.className = "warnings";
  warnings.forEach((warning) => {
    const item = document.createElement("div");
    item.className = `warning ${warning.level || "info"}`;
    item.textContent = warning.message;
    wrap.appendChild(item);
  });
  return wrap;
}

function renderKpis(items) {
  const grid = document.createElement("div");
  grid.className = "kpi-grid";
  items.forEach(([label, value]) => {
    const card = document.createElement("div");
    card.className = "kpi-card";
    card.innerHTML = `<span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong>`;
    grid.appendChild(card);
  });
  return grid;
}

function renderSection(title, content) {
  const section = document.createElement("section");
  section.className = "result-section";
  const h2 = document.createElement("h2");
  h2.textContent = title;
  section.appendChild(h2);
  section.appendChild(content);
  return section;
}

function renderModelSpec(spec) {
  const safeSpec = spec || {};
  const terms = safeSpec.terms || [];
  const references = safeSpec.references || {};
  const rows = [
    ["Outside option", references.outside_option || "No use / 0 trips has utility normalized to 0"],
    ["Epsilon", fixed(safeSpec.epsilon, 3)],
    ["Market", "segment-month"],
    ["Product", "plan x trip-count bin"],
    ["Included terms", terms.length ? terms.join(" + ") : "None selected"],
  ];
  if (references.count_bin) rows.push(["Omitted count bin", references.count_bin]);
  if (references.plan) rows.push(["Omitted plan", references.plan]);
  if (references.month_of_year) rows.push(["Omitted month", references.month_of_year]);

  const wrap = document.createElement("div");
  wrap.className = "model-spec-card";
  const equation = document.createElement("div");
  equation.className = "model-equation";
  equation.textContent = `Utility index: ${terms.length ? terms.join(" + ") : "None selected"}`;
  wrap.appendChild(equation);
  wrap.appendChild(table(["Component", "Value"], rows));
  return wrap;
}

function renderFitSummary(summary) {
  const item = summary || {};
  return table(
    ["Metric", "Value"],
    [
      ["Dependent variable", item.dependent_variable || ""],
      ["Estimator", item.estimator || ""],
      ["Weights", item.weight || ""],
      ["Standard errors", item.cluster_se || ""],
      ["Training months", item.training_months ?? ""],
      ["Inside products", item.inside_products ?? ""],
      ["Training observations", item.training_observations ?? ""],
      ["Regressors", item.regressors ?? ""],
      ["R-squared", fixed(item.r2, 4)],
      ["Adjusted R-squared", fixed(item.adjusted_r2, 4)],
    ]
  );
}

function renderMarketSizeInfo(result, forecastRows) {
  const cfg = result.config || {};
  const sizes = (forecastRows || [])
    .map((row) => Number(row.market_size))
    .filter((value) => Number.isFinite(value));
  const avgSize = mean(sizes);
  const minSize = sizes.length ? Math.min(...sizes) : 0;
  const maxSize = sizes.length ? Math.max(...sizes) : 0;
  const wrap = document.createElement("div");
  wrap.className = "model-spec-card";
  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent =
    "For months with observed data, market size is the number of in-market users in the selected segment-month. For future what-if months, the dashboard uses the average training market size for the same calendar month, then multiplies it by the future market size multiplier. If that calendar month is not present in training, it falls back to the overall average training market size.";
  wrap.appendChild(help);
  wrap.appendChild(
    table(
      ["Quantity", "Value"],
      [
        ["Forecast period", `${cfg.forecast_start || ""} to ${cfg.forecast_end || ""}`],
        ["Future market size multiplier", fixed(cfg.market_multiplier, 3)],
        ["Average forecast market size", fixed(avgSize, 1)],
        ["Forecast market size range", `${fixed(minSize, 1)} to ${fixed(maxSize, 1)}`],
      ]
    )
  );
  return wrap;
}

function renderBinningKernelInfo(result) {
  const wrap = document.createElement("div");
  wrap.className = "model-spec-card";

  const kernelRule = result.kernel_rule || {};
  const note = document.createElement("div");
  note.className = "model-equation";
  note.textContent = kernelRule.description || "Expected costs are computed from repriced empirical baskets. Count bin is locked because it defines the product.";
  wrap.appendChild(note);

  const grid = document.createElement("div");
  grid.className = "info-grid";
  const binBox = document.createElement("div");
  binBox.appendChild(sectionLabel("Trip-count bins"));
  binBox.appendChild(
    table(
      ["Bin", "Trips"],
      (result.binning || []).map((row) => [row.label, row.trip_range])
    )
  );

  const kernelBox = document.createElement("div");
  kernelBox.appendChild(sectionLabel("Kernel fallback rule"));
  const support = document.createElement("p");
  support.className = "field-help";
  support.textContent = `Minimum support: ${kernelRule.min_support || ""} baskets. A basket is one positive user-month with its realized trips. Minimum support is the number of historical baskets required before using a kernel source without broadening it; if support is too low, segment, plan, and month can be relaxed, but count bin is always preserved.`;
  kernelBox.appendChild(support);
  kernelBox.appendChild(
    table(
      ["Setting", "Value"],
      [
        ["Condition on segment", yesNo(kernelRule.condition_segment)],
        ["Condition on plan", yesNo(kernelRule.condition_plan)],
        ["Condition on count bin", "Yes, locked"],
        ["Condition on month-of-year", yesNo(kernelRule.condition_month_of_year)],
        ["Relaxation order", (kernelRule.relax_order_labels || []).join(" -> ")],
      ]
    )
  );
  kernelBox.appendChild(
    table(
      ["Priority", "Source"],
      (kernelRule.fallback_order || []).map((source, index) => [index + 1, source])
    )
  );

  const summaryBox = document.createElement("div");
  summaryBox.appendChild(sectionLabel("Kernel sources used"));
  summaryBox.appendChild(renderKernelTable(result.kernel_summary || []));

  grid.appendChild(binBox);
  grid.appendChild(kernelBox);
  grid.appendChild(summaryBox);
  wrap.appendChild(grid);
  return wrap;
}

function renderKernelExplorer(details) {
  const rows = uniqueKernelExplorerRows(details || []);
  const wrap = document.createElement("div");
  wrap.className = "model-spec-card";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "field-help";
    empty.textContent = "No kernel details were returned for this run.";
    wrap.appendChild(empty);
    return wrap;
  }

  const controls = document.createElement("div");
  controls.className = "kernel-explorer-controls";
  const label = document.createElement("label");
  label.textContent = "Kernel distribution";
  const select = document.createElement("select");
  rows.forEach((row, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = `${row.explorer_label} (${row.kernel_source})`;
    select.appendChild(option);
  });
  label.appendChild(select);
  controls.appendChild(label);
  wrap.appendChild(controls);

  const detailBox = document.createElement("div");
  wrap.appendChild(detailBox);

  const draw = () => {
    const row = rows[Number(select.value)] || rows[0];
    detailBox.innerHTML = "";
    detailBox.appendChild(
      table(
        ["Field", "Value"],
        [
          ["Kernel source", row.kernel_source],
          ["Conditioning used", row.condition_description],
          ["Products using this kernel", row.explorer_product_label],
          ["Historical training months in kernel", row.source_calendar_months || ""],
          ["Source baskets", row.source_baskets],
          ["Source trip rows", row.source_trip_rows],
        ]
      )
    );
    detailBox.appendChild(renderKernelStats(row));
    const chartGrid = document.createElement("div");
    chartGrid.className = "kernel-chart-grid";
    chartGrid.appendChild(histogramChart("Trip-count distribution", row.trip_count_hist, colors.actual));
    chartGrid.appendChild(histogramChart("Trip-duration distribution", row.duration_hist, colors.predicted));
    chartGrid.appendChild(histogramChart("Trip-distance distribution", row.distance_hist, colors.current));
    detailBox.appendChild(chartGrid);
  };

  select.addEventListener("change", draw);
  draw();
  return wrap;
}

function uniqueKernelExplorerRows(details) {
  const grouped = new Map();
  details.forEach((row) => {
    const monthOfYear = monthOfYearLabel(row.YearMonth);
    const usesMoy = String(row.condition_description || "").includes("month-of-year");
    const monthKey = usesMoy ? monthOfYear : "all-months";
    const key = [
      row.count_bin || "",
      monthKey,
      row.kernel_source,
      row.condition_description,
      row.source_baskets,
      row.source_trip_rows,
      row.source_calendar_months || "",
    ].join("||");
    if (!grouped.has(key)) {
      grouped.set(key, {
        ...row,
        explorer_month_label: usesMoy ? monthOfYear : "All months pooled",
        explorer_months_list: [],
        explorer_products_list: [],
      });
    }
    const item = grouped.get(key);
    item.explorer_months_list.push(row.YearMonth);
    item.explorer_products_list.push(row.product);
  });

  return [...grouped.values()]
    .map((row) => {
      const months = [...new Set(row.explorer_months_list)].sort();
      const products = [...new Set(row.explorer_products_list)].sort((a, b) => String(a).localeCompare(String(b)));
      const productLabel = summarizeProductList(products);
      const label = row.explorer_month_label === "All months pooled"
        ? `${productLabel} | all months`
        : `${productLabel} | ${row.explorer_month_label}`;
      return {
        ...row,
        explorer_product_label: productLabel,
        explorer_label: label,
        explorer_months: summarizeMonthList(months),
      };
    })
    .sort((a, b) => {
      const productCmp = String(a.explorer_product_label).localeCompare(String(b.explorer_product_label));
      if (productCmp !== 0) return productCmp;
      return String(a.explorer_month_label).localeCompare(String(b.explorer_month_label));
    });
}

function monthOfYearLabel(yearMonth) {
  const month = Number(String(yearMonth || "").slice(-2));
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  if (!Number.isInteger(month) || month < 1 || month > 12) return "";
  return names[month - 1];
}

function summarizeProductList(products) {
  if (!products.length) return "";
  if (products.length <= 5) return products.join(", ");
  return `${products.slice(0, 5).join(", ")} (+${products.length - 5} more)`;
}

function summarizeMonthList(months) {
  if (!months.length) return "";
  if (months.length <= 4) return months.join(", ");
  return `${months[0]} to ${months[months.length - 1]} (${months.length} months)`;
}

function renderKernelStats(row) {
  const stats = [
    ["Trip count", row.trip_count_summary],
    ["Trip duration", row.duration_summary],
    ["Trip distance", row.distance_summary],
  ];
  return table(
    ["Metric", "N", "Mean", "P25", "Median", "P75"],
    stats.map(([label, values]) => [
      label,
      values?.n ?? 0,
      fixed(values?.mean, 2),
      fixed(values?.p25, 2),
      fixed(values?.median, 2),
      fixed(values?.p75, 2),
    ])
  );
}

function histogramChart(title, rows, color) {
  const wrap = document.createElement("div");
  wrap.className = "histogram-card";
  const h3 = document.createElement("h3");
  h3.textContent = title;
  wrap.appendChild(h3);
  const data = (rows || []).map((row) => ({ label: row.label, count: row.count }));
  if (!data.length) {
    const empty = document.createElement("p");
    empty.className = "field-help";
    empty.textContent = "No observations.";
    wrap.appendChild(empty);
    return wrap;
  }
  wrap.appendChild(
    groupedBarChart({
      data,
      labelKey: "label",
      series: [{ key: "count", label: "Observations", color }],
      yFormatter: shortNumber,
      height: 280,
    })
  );
  return wrap;
}

function sectionLabel(text) {
  const label = document.createElement("div");
  label.className = "subsection-label";
  label.textContent = text;
  return label;
}

function renderCoefficientTable(rows) {
  return table(
    ["Variable", "Coefficient", "Cluster SE", "z-stat", "p-value"],
    rows.map((row) => [
      row.variable,
      fixed(row.coefficient, 5),
      fixed(row.cluster_se, 5),
      fixed(row.z_stat, 2),
      fixed(row.p_value, 3),
    ])
  );
}

function renderMetricsTable(metrics, paidPlan = "Paid plan") {
  const metricRows = [
    ["User-payment revenue", metrics.train.user_payment_revenue_mae, metrics.train.user_payment_revenue_rmse, metrics.train.user_payment_revenue_mape, metrics.test.user_payment_revenue_mae, metrics.test.user_payment_revenue_rmse, metrics.test.user_payment_revenue_mape, "money"],
    ["Free Plan trips", metrics.train.free_plan_trips_mae, metrics.train.free_plan_trips_rmse, metrics.train.free_plan_trips_mape, metrics.test.free_plan_trips_mae, metrics.test.free_plan_trips_rmse, metrics.test.free_plan_trips_mape, "number"],
    [`${paidPlan} trips`, metrics.train.paid_plan_trips_mae, metrics.train.paid_plan_trips_rmse, metrics.train.paid_plan_trips_mape, metrics.test.paid_plan_trips_mae, metrics.test.paid_plan_trips_rmse, metrics.test.paid_plan_trips_mape, "number"],
    ["Total trips", metrics.train.total_trips_mae, metrics.train.total_trips_rmse, metrics.train.total_trips_mape, metrics.test.total_trips_mae, metrics.test.total_trips_rmse, metrics.test.total_trips_mape, "number"],
  ];
  const wrap = document.createElement("div");
  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent =
    "Trip metrics compare monthly actual trip counts to model-implied trips. Predicted trips equal predicted product choices multiplied by the kernel expected trip count for that product.";
  wrap.appendChild(help);
  wrap.appendChild(
    table(
      ["Metric", "Train MAE", "Train RMSE", "Train MAPE", "Test MAE", "Test RMSE", "Test MAPE"],
      metricRows.map(([label, trainMae, trainRmse, trainMape, testMae, testRmse, testMape, kind]) => [
        label,
        kind === "money" ? fmtMoney.format(trainMae) : fixed(trainMae, 2),
        kind === "money" ? fmtMoney.format(trainRmse) : fixed(trainRmse, 2),
        percent(trainMape),
        kind === "money" ? fmtMoney.format(testMae) : fixed(testMae, 2),
        kind === "money" ? fmtMoney.format(testRmse) : fixed(testRmse, 2),
        percent(testMape),
      ])
    )
  );
  return wrap;
}

function renderProductTable(rows) {
  return table(
    ["Product", "Expected price", "Actual test choices", "Predicted test choices", "Error"],
    rows.map((row) => [
      row.product,
      fmtMoney2.format(row.expected_user_payment),
      fixed(row.actual_test_positive_users, 2),
      fixed(row.predicted_test_positive_users, 2),
      fixed(row.prediction_error, 2),
    ])
  );
}

function renderWhatIfProductTable(rows) {
  return table(
    ["Product", "Current choices", "What-if choices", "Choice change", "Current trips", "What-if trips", "Trip change", "Current revenue", "What-if revenue", "Revenue change"],
    rows.map((row) => [
      row.product,
      fixed(row.current_positive_users, 2),
      fixed(row.what_if_positive_users, 2),
      signedNumber(row.change_positive_users),
      fixed(row.current_predicted_trips, 2),
      fixed(row.what_if_predicted_trips, 2),
      signedNumber(row.change_predicted_trips),
      fmtMoney.format(row.current_user_payment_revenue),
      fmtMoney.format(row.what_if_user_payment_revenue),
      signedMoney(row.change_user_payment_revenue),
    ])
  );
}

function renderKernelTable(rows) {
  return table(
    ["Source", "Products", "Baskets", "Trip rows"],
    rows.map((row) => [row.source, row.products, fixed(row.baskets, 1), fixed(row.trip_rows, 1)])
  );
}

function renderExpectedPriceChart(
  rows,
  series,
  note = "Bars are monthly averages over the displayed horizon. Each product-month expected payment is computed by repricing the empirical kernel under the relevant tariff, then averaging those product-month values for the chart."
) {
  const products = sortProducts(rows).filter((row) => !isOutsideProduct(row));
  const wrap = document.createElement("div");
  const help = document.createElement("p");
  help.className = "field-help";
  help.innerHTML = note;
  wrap.appendChild(help);
  wrap.appendChild(
    groupedBarChart({
      data: products,
      labelKey: "product",
      series,
      yFormatter: shortMoney,
      height: 560,
    })
  );
  return wrap;
}

function renderPricingComparison(pricing) {
  const rows = [];
  Object.entries(pricing.current).forEach(([plan, currentValues]) => {
    const whatIfValues = pricing.what_if[plan] || {};
    [
      ["trip_start_fee", "Trip start fee"],
      ["per_minute", "Per minute"],
      ["per_hour", "Per hour"],
      ["per_day", "Per day"],
      ["included_miles", "Included miles"],
      ["extra_mile", "Extra-mile fee"],
      ["monthly_fee", "Monthly fee"],
      ["monthly_credit", "Monthly credit"],
    ].forEach(([key, label]) => {
      rows.push([
        plan,
        label,
        fixed(currentValues[key], 2),
        fixed(whatIfValues[key], 2),
        signedNumber((whatIfValues[key] || 0) - (currentValues[key] || 0)),
      ]);
    });
  });
  return table(["Plan", "Parameter", "Current", "What-if", "Change"], rows);
}

function renderConstraintTable(constraint) {
  const quantity = constraint.constraint_quantity || "predicted trips";
  const wrap = document.createElement("div");
  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent =
    "The optimization constraint is based on predicted trip volume, not the number of inside product choices. If the monthly constraint is off, the limit applies to total predicted trips over the full forecast horizon; if it is on, the same percentage drop limit is applied separately in every forecast month.";
  wrap.appendChild(help);
  wrap.appendChild(
    table(
      ["Constraint", "Value"],
      [
        ["Constraint quantity", quantity],
        ["Maximum allowed trip-volume drop", `${fixed(constraint.max_demand_drop * 100, 2)}%`],
        ["Monthly constraint enforced", constraint.monthly_constraint_enforced ? "Yes" : "No"],
        ["Baseline predicted trips", fixed(constraint.baseline_predicted_trips, 2)],
        ["Minimum allowed predicted trips", fixed(constraint.minimum_allowed_predicted_trips, 2)],
        ["Optimized predicted trips", fixed(constraint.optimized_predicted_trips, 2)],
        ["Trip-volume change", signedNumber(constraint.trip_change ?? constraint.demand_change)],
        ["Actual trip-volume drop", `${fixed((constraint.trip_drop_fraction ?? constraint.demand_drop_fraction) * 100, 2)}%`],
        ["Baseline inside product choices", fixed(constraint.baseline_inside_demand, 2)],
        ["Optimized inside product choices", fixed(constraint.optimized_inside_demand, 2)],
        ["Max monthly trip-volume violation", fixed(constraint.max_monthly_violation, 4)],
      ]
    )
  );
  return wrap;
}

function renderOptimizerDiagnostics(result) {
  const wrap = document.createElement("div");
  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent =
    `${result.optimization_status.procedure ||
      "The optimizer uses multi-seed direct search with a global sampling stage and local coordinate refinement."} Feasible candidate prices are candidate price vectors that satisfy the predicted trip-volume constraint. Direct-search budget is the requested maximum for the seeded direct search; actual stored evaluations can be lower if local search converges early, or higher if a polish candidate is added. Fixed overrides are checked parameters with equal lower and upper bounds. SciPy polish, if used, is shown as a separate stage because it is a local solver run after the direct-search seeds.`;
  wrap.appendChild(help);
  const statusRows = [
    ["Solver", result.optimization_status.solver],
    ["Elapsed runtime", `${fixed(result.optimization_status.elapsed_seconds, 1)} seconds`],
    ["Direct-search budget", `${result.optimization_status.direct_search_budget} planned candidate evaluations`],
    ["Direct-search evaluations completed", result.optimization_status.direct_search_evaluations ?? result.optimization_status.evaluations],
    ["Candidate prices satisfying trip constraint", result.optimization_status.feasible_evaluations],
    ["Actual stored candidate evaluations", result.optimization_status.evaluations],
    ["Free optimization variables", result.optimization_status.selected_variables],
    ["Fixed overrides (checked with equal bounds)", result.optimization_status.fixed_overrides],
    ["SciPy polish", result.optimization_status.scipy?.message || ""],
  ];
  wrap.appendChild(table(["Diagnostic", "Value"], statusRows));
  if (result.run_diagnostics && result.run_diagnostics.length) {
    wrap.appendChild(
      table(
        ["Stage", "Run", "Seed", "Candidate evaluations", "Satisfy trip constraint", "Best revenue", "Revenue lift", "Trip-volume change"],
        result.run_diagnostics.map((row) => [
          row.stage || "Direct search",
          row.run_id,
          row.seed,
          row.evaluations,
          row.feasible_evaluations,
          fmtMoney.format(row.best_revenue),
          signedMoney(row.revenue_lift),
          signedNumber(row.demand_change),
        ])
      )
    );
  }
  return wrap;
}

function optimizationBoundaryLabel(row) {
  if (!row.selected || row.lower === null || row.lower === undefined || row.upper === null || row.upper === undefined) {
    return "";
  }
  const value = Number(row.optimized);
  const lower = Number(row.lower);
  const upper = Number(row.upper);
  if (!Number.isFinite(value) || !Number.isFinite(lower) || !Number.isFinite(upper)) {
    return "";
  }
  const width = Math.abs(upper - lower);
  if (width <= 1e-10) {
    return "";
  }
  const tolerance = Math.max(1e-6, width * 1e-5);
  if (Math.abs(value - lower) <= tolerance) {
    return "At lower bound";
  }
  if (Math.abs(value - upper) <= tolerance) {
    return "At upper bound";
  }
  return "";
}

function renderOptimizationParameterTable(rows) {
  const headers = ["Plan", "Parameter", "Optimized?", "Group", "Current", "Lower", "Upper", "Optimized", "Change", "Boundary"];
  const boundaryRows = rows.filter((row) => optimizationBoundaryLabel(row));

  const outer = document.createElement("div");
  if (boundaryRows.length) {
    const note = document.createElement("div");
    note.className = "boundary-note";
    note.textContent = `${boundaryRows.length} optimized parameter${boundaryRows.length === 1 ? "" : "s"} landed on a search bound. Consider widening bounds or adding a business constraint if that is not intended.`;
    outer.appendChild(note);
  }

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const tableEl = document.createElement("table");
  tableEl.className = "optimization-parameter-table";
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  headers.forEach((header) => {
    const th = document.createElement("th");
    th.textContent = header;
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  tableEl.appendChild(thead);

  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const boundary = optimizationBoundaryLabel(row);
    const bodyTr = document.createElement("tr");
    if (boundary) {
      bodyTr.className = "boundary-row";
    }
    const decimals = row.param === "included_miles" ? 0 : 3;
    const values = [
      row.plan,
      pricingParameterLabels[row.param] || row.param,
      row.selected ? "Yes" : "No",
      row.group || "",
      fixed(row.current, decimals),
      row.lower === null || row.lower === undefined ? "" : fixed(row.lower, decimals),
      row.upper === null || row.upper === undefined ? "" : fixed(row.upper, decimals),
      fixed(row.optimized, decimals),
      signedNumber(row.change),
      boundary,
    ];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (boundary && (headers[index] === "Optimized" || headers[index] === "Boundary")) {
        td.className = "boundary-cell";
      }
      bodyTr.appendChild(td);
    });
    tbody.appendChild(bodyTr);
  });
  tableEl.appendChild(tbody);
  wrap.appendChild(tableEl);
  outer.appendChild(wrap);
  return outer;
}

function renderSensitivityTable(rows) {
  return table(
    ["Variable", "Direction", "Step", "Candidate value", "Revenue change", "Trip-volume change", "Feasible"],
    rows.map((row) => [
      row.variable,
      row.direction,
      fixed(row.step, 4),
      fixed(row.candidate_value, 4),
      signedMoney(row.revenue_change),
      signedNumber(row.demand_change),
      row.feasible ? "Yes" : "No",
    ])
  );
}

function table(headers, rows) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const tableEl = document.createElement("table");
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  headers.forEach((header) => {
    const th = document.createElement("th");
    th.textContent = header;
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  tableEl.appendChild(thead);

  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const bodyTr = document.createElement("tr");
    row.forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      bodyTr.appendChild(td);
    });
    tbody.appendChild(bodyTr);
  });
  tableEl.appendChild(tbody);
  wrap.appendChild(tableEl);
  return wrap;
}

// Low-level SVG chart builders. These avoid external charting dependencies.
function lineChart({ data, xKey, splitKey, series, yFormatter, height }) {
  const width = 980;
  const margin = { top: 30, right: 32, bottom: 78, left: 82 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const values = [];

  data.forEach((row) => {
    series.forEach((item) => {
      const val = Number(row[item.key]);
      if (Number.isFinite(val)) values.push(val);
    });
  });

  const minY = Math.min(0, ...values);
  const maxY = Math.max(1, ...values);
  const padding = (maxY - minY) * 0.08;
  const y0 = minY;
  const y1 = maxY + padding;
  const xStep = data.length > 1 ? innerW / (data.length - 1) : innerW;
  const x = (i) => margin.left + i * xStep;
  const y = (v) => margin.top + innerH - ((v - y0) / (y1 - y0 || 1)) * innerH;
  const ticks = 5;
  const tickVals = Array.from({ length: ticks }, (_, i) => y0 + ((y1 - y0) * i) / (ticks - 1));

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "chart");
  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="white"></rect>
    ${tickVals
      .map(
        (tick) => `
      <line x1="${margin.left}" x2="${width - margin.right}" y1="${y(tick)}" y2="${y(tick)}" stroke="${colors.grid}" stroke-width="1"></line>
      <text x="${margin.left - 10}" y="${y(tick) + 4}" text-anchor="end" class="axis-label">${yFormatter(tick)}</text>
    `
      )
      .join("")}
    <line x1="${margin.left}" x2="${width - margin.right}" y1="${height - margin.bottom}" y2="${height - margin.bottom}" stroke="#9aa5ad"></line>
    <line x1="${margin.left}" x2="${margin.left}" y1="${margin.top}" y2="${height - margin.bottom}" stroke="#9aa5ad"></line>
  `;

  if (splitKey) {
    const firstSplit = data[0]?.[splitKey];
    const splitIndex = data.findIndex((row) => row[splitKey] !== firstSplit);
    if (splitIndex > 0) {
      const boundary = (x(splitIndex - 1) + x(splitIndex)) / 2;
      const band = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      band.setAttribute("x", boundary);
      band.setAttribute("y", margin.top);
      band.setAttribute("width", width - margin.right - boundary);
      band.setAttribute("height", innerH);
      band.setAttribute("fill", "#f6f1e6");
      band.setAttribute("opacity", "0.55");
      svg.insertBefore(band, svg.children[1]);

      const divider = document.createElementNS("http://www.w3.org/2000/svg", "line");
      divider.setAttribute("x1", boundary);
      divider.setAttribute("x2", boundary);
      divider.setAttribute("y1", margin.top);
      divider.setAttribute("y2", height - margin.bottom);
      divider.setAttribute("stroke", "#6b7280");
      divider.setAttribute("stroke-width", "2");
      divider.setAttribute("stroke-dasharray", "6 5");
      svg.appendChild(divider);

      const trainLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      trainLabel.setAttribute("x", margin.left + 8);
      trainLabel.setAttribute("y", margin.top + 16);
      trainLabel.setAttribute("class", "split-label");
      trainLabel.textContent = "Training";
      svg.appendChild(trainLabel);

      const testLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      testLabel.setAttribute("x", boundary + 8);
      testLabel.setAttribute("y", margin.top + 16);
      testLabel.setAttribute("class", "split-label");
      testLabel.textContent = "Testing";
      svg.appendChild(testLabel);
    }
  }

  series.forEach((item) => {
    const points = data
      .map((row, i) => {
        const val = Number(row[item.key]);
        return Number.isFinite(val) ? `${x(i)},${y(val)}` : null;
      })
      .filter(Boolean);
    const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", points.join(" "));
    polyline.setAttribute("fill", "none");
    polyline.setAttribute("stroke", item.color);
    polyline.setAttribute("stroke-width", "3");
    polyline.setAttribute("stroke-linecap", "round");
    polyline.setAttribute("stroke-linejoin", "round");
    if (item.dash) {
      polyline.setAttribute("stroke-dasharray", item.dash);
    }
    svg.appendChild(polyline);

    data.forEach((row, i) => {
      const val = Number(row[item.key]);
      if (!Number.isFinite(val)) return;
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", x(i));
      circle.setAttribute("cy", y(val));
      circle.setAttribute("r", "4");
      circle.setAttribute("fill", item.color);
      svg.appendChild(circle);
    });
  });

  data.forEach((row, i) => {
    if (i % Math.ceil(data.length / 10) !== 0 && i !== data.length - 1) return;
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", x(i));
    label.setAttribute("y", height - margin.bottom + 28);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("transform", `rotate(-35 ${x(i)} ${height - margin.bottom + 28})`);
    label.setAttribute("class", "axis-label");
    label.textContent = row[xKey];
    svg.appendChild(label);
  });

  const legend = document.createElementNS("http://www.w3.org/2000/svg", "g");
  legend.setAttribute("transform", `translate(${margin.left}, ${height - 36})`);
  const legendCols = series.length > 3 ? 3 : series.length;
  series.forEach((item, i) => {
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("transform", `translate(${(i % legendCols) * 280}, ${Math.floor(i / legendCols) * 18})`);
    g.innerHTML = `
      <line x1="0" x2="24" y1="0" y2="0" stroke="${item.color}" stroke-width="4" ${item.dash ? `stroke-dasharray="${escapeHtml(item.dash)}"` : ""}></line>
      <text x="32" y="5" class="legend-label">${escapeHtml(item.label)}</text>
    `;
    legend.appendChild(g);
  });
  svg.appendChild(legend);

  const wrap = document.createElement("div");
  wrap.className = "chart-wrap";
  wrap.appendChild(svg);
  return wrap;
}

function groupedBarChart({ data, labelKey, series, yFormatter, height }) {
  const width = 1040;
  const rowH = 24;
  const chartHeight = Math.max(height, 96 + data.length * rowH);
  const margin = { top: 34, right: 28, bottom: 48, left: 245 };
  const innerW = width - margin.left - margin.right;
  const values = [];
  data.forEach((row) => series.forEach((s) => values.push(Number(row[s.key]) || 0)));
  const maxX = Math.max(1, ...values) * 1.08;
  const x = (v) => margin.left + (v / maxX) * innerW;
  const ticks = 5;
  const tickVals = Array.from({ length: ticks }, (_, i) => (maxX * i) / (ticks - 1));

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${chartHeight}`);
  svg.setAttribute("class", "chart");
  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${chartHeight}" fill="white"></rect>
    ${tickVals
      .map(
        (tick) => `
      <line x1="${x(tick)}" x2="${x(tick)}" y1="${margin.top}" y2="${chartHeight - margin.bottom}" stroke="${colors.grid}" stroke-width="1"></line>
      <text x="${x(tick)}" y="${chartHeight - margin.bottom + 24}" text-anchor="middle" class="axis-label">${yFormatter(tick)}</text>
    `
      )
      .join("")}
    <line x1="${margin.left}" x2="${margin.left}" y1="${margin.top}" y2="${chartHeight - margin.bottom}" stroke="#9aa5ad"></line>
  `;

  data.forEach((row, i) => {
    const baseY = margin.top + i * rowH;
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", margin.left - 10);
    label.setAttribute("y", baseY + rowH * 0.62);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", isOutsideProduct(row) ? "axis-label strong" : "axis-label");
    label.textContent = row[labelKey];
    svg.appendChild(label);

    series.forEach((item, j) => {
      const val = Number(row[item.key]) || 0;
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const barH = 8;
      const barEnd = x(val);
      rect.setAttribute("x", margin.left);
      rect.setAttribute("y", baseY + 3 + j * (barH + 2));
      rect.setAttribute("width", Math.max(0, barEnd - margin.left));
      rect.setAttribute("height", barH);
      rect.setAttribute("rx", "2");
      rect.setAttribute("fill", item.color);
      svg.appendChild(rect);
      if (val > 0) {
        const valueLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
        const nearRightEdge = barEnd > width - margin.right - 52;
        valueLabel.setAttribute("x", nearRightEdge ? barEnd - 5 : barEnd + 5);
        valueLabel.setAttribute("y", baseY + 10 + j * (barH + 2));
        valueLabel.setAttribute("text-anchor", nearRightEdge ? "end" : "start");
        valueLabel.setAttribute("class", nearRightEdge ? "bar-value inside" : "bar-value");
        valueLabel.textContent = shortNumber(val);
        svg.appendChild(valueLabel);
      }
    });
  });

  const legend = document.createElementNS("http://www.w3.org/2000/svg", "g");
  legend.setAttribute("transform", `translate(${margin.left}, ${chartHeight - 14})`);
  series.forEach((item, i) => {
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("transform", `translate(${i * 210}, 0)`);
    g.innerHTML = `
      <rect x="0" y="-10" width="18" height="12" fill="${item.color}"></rect>
      <text x="28" y="1" class="legend-label">${escapeHtml(item.label)}</text>
    `;
    legend.appendChild(g);
  });
  svg.appendChild(legend);

  const wrap = document.createElement("div");
  wrap.className = "chart-wrap wide-chart";
  wrap.appendChild(svg);
  return wrap;
}

function divergingBarChart({ data, labelKey, valueKey, yFormatter, positiveLabel, negativeLabel, height }) {
  const rows = data || [];
  const width = 1040;
  const rowH = 24;
  const chartHeight = Math.max(height, 104 + rows.length * rowH);
  const margin = { top: 36, right: 88, bottom: 54, left: 275 };
  const innerW = width - margin.left - margin.right;
  const center = margin.left + innerW / 2;
  const maxAbs = Math.max(1, ...rows.map((row) => Math.abs(Number(row[valueKey]) || 0))) * 1.12;
  const x = (value) => center + (value / maxAbs) * (innerW / 2);
  const tickVals = [-maxAbs, -maxAbs / 2, 0, maxAbs / 2, maxAbs];

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${chartHeight}`);
  svg.setAttribute("class", "chart");
  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${chartHeight}" fill="white"></rect>
    ${tickVals
      .map(
        (tick) => `
      <line x1="${x(tick)}" x2="${x(tick)}" y1="${margin.top}" y2="${chartHeight - margin.bottom}" stroke="${tick === 0 ? "#9aa5ad" : colors.grid}" stroke-width="${tick === 0 ? 1.5 : 1}"></line>
      <text x="${x(tick)}" y="${chartHeight - margin.bottom + 24}" text-anchor="middle" class="axis-label">${yFormatter(tick)}</text>
    `
      )
      .join("")}
    <text x="${margin.left}" y="22" class="legend-label">${escapeHtml(negativeLabel || "Demand loss")}</text>
    <text x="${width - margin.right}" y="22" text-anchor="end" class="legend-label">${escapeHtml(positiveLabel || "Demand gain")}</text>
  `;

  rows.forEach((row, i) => {
    const value = Number(row[valueKey]) || 0;
    const baseY = margin.top + i * rowH;
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", margin.left - 10);
    label.setAttribute("y", baseY + rowH * 0.62);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", isOutsideProduct(row) ? "axis-label strong" : "axis-label");
    label.textContent = row[labelKey];
    svg.appendChild(label);

    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    const startX = x(Math.min(0, value));
    const endX = x(Math.max(0, value));
    rect.setAttribute("x", startX);
    rect.setAttribute("y", baseY + 6);
    rect.setAttribute("width", Math.max(0, endX - startX));
    rect.setAttribute("height", 10);
    rect.setAttribute("rx", "2");
    rect.setAttribute("fill", value >= 0 ? colors.current : colors.whatIf);
    svg.appendChild(rect);

    if (Math.abs(value) > 0) {
      const valueLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      valueLabel.setAttribute("x", value >= 0 ? endX + 5 : startX - 5);
      valueLabel.setAttribute("y", baseY + 15);
      valueLabel.setAttribute("text-anchor", value >= 0 ? "start" : "end");
      valueLabel.setAttribute("class", "bar-value");
      valueLabel.textContent = signedNumber(value);
      svg.appendChild(valueLabel);
    }
  });

  const wrap = document.createElement("div");
  wrap.className = "chart-wrap wide-chart";
  wrap.appendChild(svg);

  const note = document.createElement("p");
  note.className = "field-help";
  note.textContent = "Positive bars are product-choice gains and negative bars are losses relative to the baseline. This is an aggregate reallocation view, not person-by-person tracking.";
  wrap.appendChild(note);
  return wrap;
}

// Small data-shaping helpers used by the renderers.
function mergeNoPriceMonthly(monthly, noPrice) {
  const byMonth = new Map((noPrice || []).map((row) => [row.YearMonth, row]));
  return monthly.map((row) => {
    const np = byMonth.get(row.YearMonth) || {};
    return {
      ...row,
      predicted_user_payment_revenue_no_price: np.predicted_user_payment_revenue,
      predicted_positive_users_no_price: np.predicted_positive_users,
    };
  });
}

function sortProducts(rows) {
  return [...rows].sort((a, b) => {
    const aOutside = isOutsideProduct(a);
    const bOutside = isOutsideProduct(b);
    if (aOutside && !bOutside) return -1;
    if (!aOutside && bOutside) return 1;
    const planCmp = String(a.plan).localeCompare(String(b.plan));
    if (planCmp !== 0) return planCmp;
    return (a.bin_sort || 0) - (b.bin_sort || 0);
  });
}

function demandChangeRows(rows, key) {
  return [...rows].sort((a, b) => {
    const aValue = Number(a[key]) || 0;
    const bValue = Number(b[key]) || 0;
    if (bValue !== aValue) return bValue - aValue;
    return String(a.product).localeCompare(String(b.product));
  });
}

function planTripRows(rows, baselineKey, alternativeKey, changeKey, numericKeys) {
  const grouped = new Map();
  rows.forEach((row) => {
    if (isOutsideProduct(row)) return;
    const plan = row.plan || "Plan";
    if (!grouped.has(plan)) {
      grouped.set(plan, { plan });
      numericKeys.forEach((key) => {
        grouped.get(plan)[key] = 0;
      });
    }
    const item = grouped.get(plan);
    numericKeys.forEach((key) => {
      item[key] += Number(row[key]) || 0;
    });
  });
  return [...grouped.values()]
    .map((row) => ({
      ...row,
      [changeKey]: (Number(row[alternativeKey]) || 0) - (Number(row[baselineKey]) || 0),
    }))
    .sort((a, b) => String(a.plan).localeCompare(String(b.plan)));
}

function ensureValidationNoUseRow(rows, monthly) {
  if (rows.some((row) => isOutsideProduct(row))) return rows;
  const testRows = monthly.filter((row) => row.split === "test");
  if (!testRows.length) return rows;
  const actual = mean(testRows.map((row) => Number(row.market_size) - Number(row.observed_positive_users)));
  const predicted = mean(testRows.map((row) => Number(row.market_size) - Number(row.predicted_positive_users)));
  return [
    {
      plan: "Outside option",
      count_bin: "00",
      product: "No use / outside option [0 trips]",
      actual_test_positive_users: actual,
      predicted_test_positive_users: predicted,
      prediction_error: predicted - actual,
      expected_user_payment: 0,
      expected_trip_count: 0,
      bin_sort: 0,
    },
    ...rows,
  ];
}

function ensureWhatIfNoUseRow(rows, monthly) {
  if (rows.some((row) => isOutsideProduct(row))) return rows;
  const current = monthly.filter((row) => row.scenario === "Current pricing");
  const whatIf = monthly.filter((row) => row.scenario === "What-if pricing");
  if (!current.length || !whatIf.length) return rows;
  const currentNoUse = mean(current.map((row) => Number(row.market_size) - Number(row.predicted_positive_users)));
  const whatIfNoUse = mean(whatIf.map((row) => Number(row.market_size) - Number(row.predicted_positive_users)));
  return [
    {
      plan: "Outside option",
      count_bin: "00",
      product: "No use / outside option [0 trips]",
      current_positive_users: currentNoUse,
      what_if_positive_users: whatIfNoUse,
      change_positive_users: whatIfNoUse - currentNoUse,
      current_predicted_trips: 0,
      what_if_predicted_trips: 0,
      change_predicted_trips: 0,
      current_user_payment_revenue: 0,
      what_if_user_payment_revenue: 0,
      change_user_payment_revenue: 0,
      bin_sort: 0,
    },
    ...rows,
  ];
}

function ensureOptimizationNoUseRow(rows, monthly) {
  if (rows.some((row) => isOutsideProduct(row))) return rows;
  const baseline = monthly.filter((row) => row.scenario === "Baseline pricing");
  const optimized = monthly.filter((row) => row.scenario === "Optimized pricing");
  if (!baseline.length || !optimized.length) return rows;
  const baselineNoUse = mean(baseline.map((row) => Number(row.market_size) - Number(row.predicted_positive_users)));
  const optimizedNoUse = mean(optimized.map((row) => Number(row.market_size) - Number(row.predicted_positive_users)));
  return [
    {
      plan: "Outside option",
      count_bin: "00",
      product: "No use / outside option [0 trips]",
      baseline_positive_users: baselineNoUse,
      optimized_positive_users: optimizedNoUse,
      change_positive_users: optimizedNoUse - baselineNoUse,
      baseline_predicted_trips: 0,
      optimized_predicted_trips: 0,
      change_predicted_trips: 0,
      baseline_user_payment_revenue: 0,
      optimized_user_payment_revenue: 0,
      change_user_payment_revenue: 0,
      baseline_expected_user_payment: 0,
      optimized_expected_user_payment: 0,
      bin_sort: 0,
    },
    ...rows,
  ];
}

function isOutsideProduct(row) {
  const product = String(row.product || "");
  return row.count_bin === "00" || product.startsWith("Outside") || product.startsWith("No use");
}

function mean(values) {
  const finite = values.filter((value) => Number.isFinite(value));
  if (!finite.length) return 0;
  return finite.reduce((sum, value) => sum + value, 0) / finite.length;
}

function scenarioTotals(rows) {
  return rows.reduce(
    (acc, row) => {
      acc.revenue += Number(row.predicted_user_payment_revenue) || 0;
      acc.demand += Number(row.predicted_positive_users) || 0;
      acc.trips += Number(row.predicted_trips) || 0;
      acc.freeTrips += Number(row.predicted_free_plan_trips) || 0;
      acc.paidTrips += Number(row.predicted_paid_plan_trips) || 0;
      return acc;
    },
    { revenue: 0, demand: 0, trips: 0, freeTrips: 0, paidTrips: 0 }
  );
}

// API, progress, and formatting utilities.
function setLoading(container, message) {
  container.innerHTML = `<div class="loading">${escapeHtml(message)}</div>`;
}

function startOptimizationProgress(container, payload) {
  stopOptimizationProgress();
  const started = Date.now();
  const directBudget = Math.max(0, Number(payload.max_evaluations) || 0) * Math.max(1, Number(payload.num_seeds) || 1);
  const selectedVariables = (payload.variables || []).filter((row) => row.enabled && Number(row.upper) !== Number(row.lower)).length;
  container.innerHTML = `
    <div class="loading">
      <strong>Running constrained pricing optimization...</strong>
      <div class="progress-grid">
        <span>Stage</span><strong id="optProgressPhase">Starting</strong>
        <span>Elapsed time</span><strong id="optElapsedTime">0s</strong>
        <span>Completed direct-search evaluations</span><strong id="optCompletedEvaluations">0 / ${escapeHtml(String(directBudget))}</strong>
        <span>Feasible candidate prices</span><strong id="optFeasibleEvaluations">0</strong>
        <span>Current run / seed</span><strong id="optCurrentSeed">Preparing</strong>
        <span>Free variables</span><strong>${escapeHtml(String(selectedVariables))}</strong>
      </div>
    </div>
  `;
  const update = () => {
    const elapsed = Math.floor((Date.now() - started) / 1000);
    const node = document.getElementById("optElapsedTime");
    if (node) node.textContent = `${elapsed}s`;
  };
  state.optimizationTimer = window.setInterval(update, 1000);
  update();
  return () => stopOptimizationProgress();
}

function updateOptimizationProgress(progress) {
  const phase = progress.phase || progress.stage || "Running";
  const completed = Number(progress.completed_evaluations) || 0;
  const budget = Number(progress.direct_search_budget) || 0;
  const feasible = Number(progress.feasible_evaluations) || 0;
  const run = progress.current_run;
  const seed = progress.current_seed;
  const runEval = progress.run_evaluations;
  const runBudget = progress.run_budget;
  const phaseNode = document.getElementById("optProgressPhase");
  const completedNode = document.getElementById("optCompletedEvaluations");
  const feasibleNode = document.getElementById("optFeasibleEvaluations");
  const seedNode = document.getElementById("optCurrentSeed");
  if (phaseNode) phaseNode.textContent = phase;
  if (completedNode) completedNode.textContent = budget ? `${completed} / ${budget}` : String(completed);
  if (feasibleNode) feasibleNode.textContent = String(feasible);
  if (seedNode) {
    if (run && seed !== undefined && runEval !== undefined && runBudget !== undefined) {
      seedNode.textContent = `Run ${run}, seed ${seed}, ${runEval} / ${runBudget}`;
    } else if (run && seed !== undefined) {
      seedNode.textContent = `Run ${run}, seed ${seed}`;
    } else {
      seedNode.textContent = "Preparing";
    }
  }
  setStatus(`Optimizing: ${phase}`);
}

function stopOptimizationProgress() {
  if (state.optimizationTimer) {
    window.clearInterval(state.optimizationTimer);
    state.optimizationTimer = null;
  }
}

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function renderError(container, error) {
  container.innerHTML = `<div class="error-box">${escapeHtml(error.message || String(error))}</div>`;
}

async function apiGet(path) {
  const response = await fetch(path);
  return parseApiResponse(response);
}

async function apiPost(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return parseApiResponse(response);
}

async function parseApiResponse(response) {
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new Error(text || response.statusText);
  }
  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  if (data && Object.prototype.hasOwnProperty.call(data, "ok")) {
    if (!data.ok) {
      throw new Error(data.error || response.statusText);
    }
    return data.data;
  }
  return data;
}

function setStatus(message) {
  const status = document.getElementById("status");
  if (status) status.textContent = message;
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(value);
  }
  return String(value).replace(/"/g, '\\"');
}

function roundTo(value, digits) {
  const scale = 10 ** digits;
  return Math.round(Number(value) * scale) / scale;
}

function fixed(value, digits) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return n.toFixed(digits);
}

function yesNo(value) {
  return value ? "Yes" : "No";
}

function formatCoef(value) {
  if (value === null || value === undefined || value === "") return "not estimated";
  const n = Number(value);
  if (!Number.isFinite(n)) return "not estimated";
  return n.toFixed(5);
}

function percent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return `${(n * 100).toFixed(1)}%`;
}

function shortMoney(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (Math.abs(n) >= 1000000) return `$${(n / 1000000).toFixed(1)}M`;
  if (Math.abs(n) >= 1000) return `$${(n / 1000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}

function shortNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return n.toFixed(0);
}

function signedMoney(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmtMoney.format(n)}`;
}

function signedNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmtNum.format(n)}`;
}

function paidPlanName(result) {
  const plans = result?.plans || [];
  return plans.find((plan) => plan !== "Free Plan") || "Paid plan";
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
