# Evie Demand Dashboard User Guide

## 1. Purpose of the Dashboard

The Evie demand dashboard is a local analysis tool for:

1. validating the structural demand model against historical data;
2. running what-if pricing scenarios; and
3. searching for revenue-improving pricing parameters subject to trip-volume constraints.

The dashboard is not a fully automated price-setting system. It is a decision support tool. Results should be reviewed alongside operating constraints, equity goals, customer communication considerations, and business judgment.

## 2. Required Data

The dashboard reads four CSV files from its data folder:

- `clean_trips_with_invoice_plan_and_segment.csv`
- `user_month_regular.csv`
- `user_month_student.csv`
- `user_month_access.csv`

To recreate these files from new raw company exports, see [`data-creation-guide.md`](data-creation-guide.md).

## 3. Opening the Dashboard

From the project folder, run:

```powershell
python app.py
```

Then open:

```text
http://127.0.0.1:8766
```

The dashboard runs locally on the user's machine. It does not upload data or call external services. Keep the Python process running while using the dashboard. Stop the process when finished.

## 4. Common Concepts Used Across Tabs

### 4.1 Segment

The segment selector chooses which user group to analyze. The dashboard fits a separate model for each segment:

- Regular users: Everyday paid plan and Free Plan.
- Access users: Access paid plan and Free Plan.
- Student users: Student paid plan and Free Plan.

### 4.2 Training and Testing Months

Training months are used to estimate the demand model and build empirical kernels. Testing months are used only in the Model Validation tab to evaluate out-of-sample or holdout fit. What-if and Optimization do not require a testing period because they forecast future or hypothetical outcomes after fitting the model.

### 4.3 Exclude Latest Month

The latest month can be incomplete. If `Exclude latest month` is checked, the dashboard removes the most recent month from the data before estimation, validation, or forecasting. This is usually recommended unless the latest month is known to be complete.

### 4.4 Model Specification Controls

The model specification controls determine which terms enter deterministic utility:

- **Expected payment**: includes the expected monthly user payment coefficient. This is required for what-if and optimization because demand must respond to price. In Model Validation it can be unchecked as a diagnostic comparison.
- **Intercept**: shifts all inside Evie products relative to the no-use outside option.
- **Count-bin fixed effects**: controls for average preference for each trip-count category.
- **Plan fixed effects**: controls for average preference for the paid plan versus Free Plan.
- **Month-of-year fixed effects**: controls for seasonality.

Recommended default: keep all specification terms checked. If the expected payment coefficient is positive, counterfactual pricing results should not be used without further review.

### 4.5 Product Trip-Count Bins

Products are plan-by-trip-count bins. The default bins are:

```text
01
02
03
04
05
06
07
08
09-12
13-20
21+
```

Each line is one bin. Bins must:

- start at 1;
- be ordered;
- be non-overlapping;
- be contiguous, with no gaps;
- end in an open-ended `+` bin.

Examples of valid bins are `01`, `09-12`, and `21+`. Changing bins changes the product definition, so validation should be rerun after any bin change.

### 4.6 Kernel Conditioning

The kernel is the empirical basket pool used to compute expected product payment and expected trips. A basket is one positive-use user-month with its trip count and all trip durations and distances.

The dashboard always conditions on trip-count bin because count bin defines the product. The user may also condition on:

- **Segment**: use baskets from the same segment only. If not checked, the kernel may pool baskets across Regular, Access, and Student.
- **Plan**: use baskets from the same plan group. If segment is not checked but plan is checked, Free Plan baskets are pooled together and paid-plan baskets are pooled together.
- **Month-of-year**: use baskets from the same calendar month, for example all Januaries.

**Min support** is the minimum number of historical baskets required for a kernel source before broadening the source. If the selected conditions have too few baskets, the dashboard relaxes conditions in the selected order. Count bin stays locked.

Recommended default: use count-bin only and min support 30 unless there is a specific reason to require segment, plan, or month-of-year conditioning. More conditioning can better match a target product, but it can also make kernels sparse.

### 4.7 Common Output Sections

Several output sections appear in multiple tabs:

- **Run header**: shows selected segment, training period, testing or forecast period, and any excluded latest month.
- **Warnings**: flags important issues such as a positive price coefficient or sparse kernels.
- **Model Specification**: shows the outside option, epsilon, market definition, product definition, included utility terms, and omitted reference categories.
- **Estimation Fit Summary**: shows dependent variable, estimator, weights, clustered standard errors, number of training months, number of inside products, training observations, regressors, R-squared, and adjusted R-squared.
- **Bins and Kernel**: shows the trip-count bins, selected kernel rule, fallback order, and number of products/baskets/trip rows by kernel source.
- **Kernel Distribution Explorer**: allows inspection of the empirical source baskets used by kernels. It reports which products use a kernel, which historical months are included, basket count, trip-row count, and distributions of trip count, duration, and distance.
- **Estimated Utility Parameters**: reports coefficients, clustered standard errors, z-statistics, and p-values.

## 5. Model Validation Tab

### 5.1 Purpose

The Model Validation tab checks whether the model can reproduce observed historical revenue and demand. The user chooses training months and testing months. The model is estimated on training months and evaluated on both training and testing months.

### 5.2 Inputs

| Input | Meaning and recommendation |
| --- | --- |
| Segment | Select Regular, Access, or Student. |
| Train start/end | Months used to estimate the model. Use a period before the testing period. |
| Test start/end | Months used to evaluate holdout fit. These should not overlap training months. |
| Exclude latest month | Usually keep checked unless the latest month is complete. |
| Model specification | Use all terms checked by default. Uncheck expected payment only to see a no-price diagnostic fit. |
| Trip-count bins | Default bins are recommended unless testing an alternative product definition. |
| Kernel conditioning | Default is count-bin only. Tighten conditioning only if there is enough support. |

### 5.3 Top Validation Metrics

The top cards report MAE for:

- user-payment revenue;
- Free Plan trips;
- paid-plan trips;
- total trips.

Each is shown separately for training and testing months.

For any monthly outcome $`y_t`$ and prediction $`\widehat{y}_t`$,

```math
\text{MAE} = \frac{1}{T} \sum_{t=1}^{T} |y_t - \widehat{y}_t|,
```

```math
\text{RMSE} = \sqrt{\frac{1}{T} \sum_{t=1}^{T} (y_t - \widehat{y}_t)^2},
```

and

```math
\text{MAPE} = \frac{1}{T} \sum_{t=1}^{T} \left| \frac{y_t - \widehat{y}_t}{y_t} \right|.
```

MAPE is undefined when the actual value is zero; the dashboard skips zero denominators.

### 5.4 Fit Metrics Table

The Fit Metrics table gives MAE, RMSE, and MAPE for train and test periods. Revenue metrics compare actual monthly user-payment revenue to predicted monthly user-payment revenue. Trip metrics compare actual monthly trip counts to model-implied trips. Predicted trips equal predicted product choices multiplied by the kernel expected trip count for each product.

### 5.5 Monthly User-Payment Revenue Plot

This line plot compares actual monthly user-payment revenue to predicted monthly user-payment revenue. If the expected payment coefficient is included, the predicted line is labeled "Predicted with price." The chart marks the training/testing split.

### 5.6 Monthly Trips By Plan Plot

This plot compares actual and predicted trips over time for:

- total trips;
- Free Plan trips;
- paid-plan trips.

There are six lines: actual and predicted for each of the three quantities.

### 5.7 Average Monthly Product Choices

This bar chart compares average actual and predicted product choices over the testing months. Products include Free Plan bins, paid-plan bins, and the outside option. The outside option is no use, or zero trips.

### 5.8 Expected User Payment By Product

This chart shows the expected monthly user payment for each product. Bars are averages over the testing months. For each product-month, the dashboard reprices the empirical kernel using that month's observed tariff. If the kernel conditions on month-of-year, month-specific expected payments are computed first and then averaged for the chart.

## 6. What-if Analysis Tab

### 6.1 Purpose

The What-if Analysis tab estimates the model on historical training data, then forecasts demand and revenue under current pricing and under user-edited pricing. It is used to evaluate specific pricing scenarios, not to search over all possible prices.

### 6.2 Inputs

| Input | Meaning and recommendation |
| --- | --- |
| Segment | User segment to analyze. |
| Train start/end | Historical months used to estimate the model. |
| Forecast start | First month to forecast. |
| Forecast months | Number of forecast months. |
| Future market size multiplier | Multiplies the forecast market size. Use 1.0 unless there is a reason to assume growth or contraction. |
| Exclude latest month | Usually keep checked. |
| Model specification | Expected payment is required and locked on. Other default terms are recommended. |
| Bins and kernel | Same meaning as in Model Validation. |
| Load current pricing | Loads observed latest pricing parameters as the starting point. |
| Run what-if | Runs the forecast under current and edited pricing. |

### 6.3 Future Market Size

For months with observed data, market size is the number of in-market users in the selected segment-month. For future months, the dashboard uses the average training market size for the same calendar month, then multiplies it by the future market size multiplier. If that calendar month is absent from training, it falls back to the overall average training market size.

### 6.4 Pricing Inputs

The pricing controls show each plan and each tariff parameter:

- per-minute rate;
- per-hour rate;
- per-day rate;
- trip start fee;
- included miles;
- extra-mile fee;
- monthly fee;
- monthly credit.

Edit these values to create the what-if tariff. The dashboard forecasts current pricing and what-if pricing over the same forecast months.

### 6.5 Top What-if Metrics

The top cards report:

- fitted price coefficient;
- current forecast revenue;
- what-if forecast revenue;
- revenue change;
- current average monthly trips;
- average monthly trip change;
- current Free Plan average trips and change;
- current paid-plan average trips and change.

### 6.6 Forecast Plots and Tables

The What-if tab includes:

- **Forecast Market Size**: forecast period, market-size multiplier, average forecast market size, and market-size range.
- **Pricing Used In Forecast**: current and what-if tariff values.
- **Forecast User-Payment Revenue**: monthly current versus what-if revenue.
- **Forecast Trips By Plan**: monthly total, Free Plan, and paid-plan trips under current and what-if pricing.
- **Average Monthly Trips By Plan**: average trips under current and what-if pricing.
- **Average Monthly Product Choices**: average choices by product, including outside option.
- **Average Monthly Product Choice Reallocation**: which products gain or lose choices under the what-if tariff.
- **Expected User Payment By Product**: expected payment under current and what-if pricing, averaged over forecast months.

## 7. Optimization Tab

### 7.1 Purpose

The Optimization tab searches over selected pricing parameters to maximize predicted user-payment revenue subject to a trip-volume constraint. It is a search tool for candidate pricing policies. It does not certify a global optimum.

### 7.2 Inputs

| Input | Meaning and recommendation |
| --- | --- |
| Segment | User segment to optimize for. |
| Train start/end | Historical months used to estimate the demand model. |
| Forecast start/months | Future months over which objective and constraints are evaluated. |
| Future market size multiplier | Same definition as in What-if Analysis. |
| Maximum trip-volume drop (%) | Maximum allowed reduction in predicted trips relative to baseline pricing. |
| Evaluations per seed | Candidate-price evaluations for each random seed. Larger values search more thoroughly but take longer. |
| Number of seeds | Number of repeated random starts. More seeds reduce dependence on one random draw. |
| Random seed | Starting seed for reproducibility. |
| Exclude latest month | Usually keep checked. |
| Enforce demand constraint each month | If checked, the trip-volume drop limit must hold in every forecast month, not just in total. |
| Keep time rates proportional | If checked, each selected plan's per-minute, per-hour, and per-day rates move together by one multiplier. |
| SciPy polish | If checked and SciPy is installed, run a local SLSQP polish after direct search. |
| Model specification | Expected payment is required and locked on. Other default terms are recommended. |
| Bins and kernel | Same meaning as in Model Validation. |
| Load pricing bounds | Loads current pricing and default lower/upper bounds. |
| Select all parameters | Selects all tariff parameters for optimization. |
| Clear selected parameters | Deselects all tariff parameters. |
| Run optimization | Starts the search. |

### 7.3 Optimization Bounds Table

Each row is one plan-parameter combination. Columns are:

- **Optimize**: whether the parameter is included in the search.
- **Plan**: Free Plan or the segment paid plan.
- **Parameter**: tariff component.
- **Current**: observed baseline value.
- **Lower**: lower search bound.
- **Upper**: upper search bound.

If `Lower=Upper` and the parameter is checked, the parameter is treated as a fixed override rather than a free variable.

### 7.4 Progress Panel

During optimization, the dashboard shows:

- elapsed time;
- current optimization phase;
- completed direct-search evaluations;
- feasible candidate prices;
- current run and seed.

Feasible candidate prices are candidate price vectors that satisfy the predicted trip-volume constraint.

### 7.5 Trip-Volume Constraint

The optimization constraint is based on predicted trips, not merely the number of users choosing an inside product. If the monthly constraint is off, the limit applies to total predicted trips over the full forecast horizon. If the monthly constraint is on, the same percentage drop limit is applied separately in every forecast month.

### 7.6 Top Optimization Metrics

The top cards report:

- fitted price coefficient;
- baseline revenue;
- optimized revenue;
- revenue lift;
- total trip change;
- average monthly trip change;
- Free Plan trip change;
- paid-plan trip change;
- candidate prices satisfying the trip constraint;
- number of seeds.

### 7.7 Optimizer Diagnostics

The diagnostics section reports:

- solver/procedure;
- elapsed runtime;
- direct-search budget;
- direct-search evaluations completed;
- candidate prices satisfying the trip constraint;
- actual stored candidate evaluations;
- number of free optimization variables;
- number of fixed overrides;
- SciPy polish status.

The run-diagnostics table gives each direct-search or polish stage, run, seed, candidate evaluations, feasible evaluations, best revenue, revenue lift, and trip-volume change.

### 7.8 Optimized Pricing Parameters

The optimized-parameter table reports current values, bounds, optimized values, changes, and whether a parameter landed at a bound. A boundary solution is not automatically wrong, but it means the optimizer wanted to move as far as the allowed range. If the result is not business-plausible, consider widening bounds, narrowing bounds, tying time rates, or adding a business constraint.

### 7.9 Optimization Plots

The Optimization tab includes:

- **Best Feasible Revenue Lift During Search**: search progress over candidate evaluations.
- **Forecast User-Payment Revenue**: baseline versus optimized monthly revenue.
- **Forecast Trips By Plan**: baseline versus optimized monthly total, Free Plan, and paid-plan trips.
- **Average Monthly Trips By Plan**: average trip volume by plan.
- **Average Monthly Product Choices**: average choices by product, including outside option.
- **Average Monthly Product Choice Reallocation**: which products gain or lose choices.
- **Expected User Payment By Product**: baseline and optimized expected payments.
- **Local Sensitivity Around Reported Solution**: one-step checks around the selected solution showing revenue and trip-volume changes.

## 8. Warnings and Practical Interpretation

### 8.1 Positive Price Coefficient

A positive price coefficient means the fitted model estimates higher expected payment as increasing utility. This is usually not economically sensible for counterfactual pricing. If this warning appears, do not rely on what-if or optimization results without reviewing the training period, product bins, kernel settings, and model specification.

### 8.2 Sparse Kernels

Sparse kernel warnings mean some expected prices or expected trips are based on few historical baskets. Sparse kernels can make product-level predictions noisy. Consider broader kernel conditioning or coarser bins.

### 8.3 Trip Duration and Distance Do Not Respond to Price

Counterfactuals reprice historical empirical baskets. The model predicts how many users choose each product, but it does not predict how trip distances or trip durations change after a pricing change. This is one of the most important limitations. Policies expected to change trip length, destination, or miles per trip should be interpreted cautiously.

### 8.4 Optimization Is Search Based

The optimizer reports the best feasible candidate found within the selected bounds, seeds, and evaluation budget. It is not a global optimality certificate. Use multiple seeds, review diagnostics, check boundary solutions, and compare the optimized tariff to business rules before implementation.

### 8.5 Market Size Is A Forecast Assumption

Future market size is based on historical same-month-of-year averages and the user-specified multiplier. It is not a separate growth forecast. If the company expects growth, contraction, marketing campaigns, or service-area changes, the market-size multiplier should be chosen deliberately.

### 8.6 Recommended Workflow

For routine use:

1. Refresh input data if new raw data are available.
2. Run Model Validation for the relevant segment.
3. Check price coefficient, revenue fit, trip fit, and kernel support.
4. Run What-if Analysis for specific business proposals.
5. Use Optimization only after the validation fit is acceptable.
6. Review optimized parameters for boundary solutions and business plausibility.
7. Treat dashboard output as evidence for a pricing discussion, not as automatic approval to change prices.
