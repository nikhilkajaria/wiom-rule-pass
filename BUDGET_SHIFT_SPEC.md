# Channel Budget Rebalancing - Spec v1.0
Level 1: Inter-channel (Meta vs Google) | Jul 2026 | Pending SD alignment

---

## Objective

Maximize booking volume while improving blended CPBL toward the Rs 500 target. CPBL is the guardrail, not the sole objective. Do not cut spend just to hit efficiency.

---

## Scope

| | |
|---|---|
| **Meta** | BFC-Volume + Retargeting |
| **Google** | UAC + YouTube Demand Gen + Search |
| **Excluded** | ToF campaigns, Creative Testing |

Creative Testing sits outside the rebalanceable pool. Its budget is objective-based (learning, not volume) and should not be shifted in response to CPBL signals.

---

## Metric

- **What:** 7-day rolling Branch-attributed CPBL per channel
- **Why 7-day:** ~30% of bookings arrive D7+ (booking lag cohort). A 3-day window systematically understates recent bookings and produces a directionally wrong signal.
- **Attribution:** Branch only. Google Search (brand pilot) excluded until cross-channel attribution is validated.

---

## Trigger

| | |
|---|---|
| **Condition** | Channel CPBL gap > 10% for 3 consecutive days |
| **Check cadence** | Daily |
| **Suspended when** | A shift is in progress OR the stabilization window is active |
| **Why 3 days** | Filters transient causes (learning phase blips, booking lag, day-of-week noise) that resolve in 1-3 days. Structural gaps persist. |
| **Why >10%** | Noise filter. Below 10%, the shift size is small enough that the disruption risk (learning phase, management overhead) exceeds the efficiency gain. |

---

## Shift Size

Both the source channel (shifter) and destination channel (shiftee) are capped at 15% of their own current budget per step:

```
shift_Rs = min( min(gap% / 2, 15%) x losing_budget,  15% x winning_budget )
```

| | |
|---|---|
| **First term** | Caps the take from the losing channel: at most min(gap/2, 15%) of its own budget |
| **Second term** | Caps the add to the winning channel: at most 15% of its own budget |
| **Binding constraint** | Whichever channel hits 15% first. The smaller channel is always binding. |
| **Why 15%** | Practitioner consensus threshold below which Meta and Google UAC do not re-enter learning phase on a budget change. Conservative on both platforms. |

**Example:** Meta Rs 100k (losing), Google Rs 30k (winning), gap 40%
shift = min(15k, 4.5k) = **Rs 4.5k** [Meta: -4.5%, Google: +15%]

---

## Stagger and Stabilization

| | |
|---|---|
| **Step cadence** | One shift step every 3 days |
| **Order** | Complete all steps first, then enter stabilization. Do not interleave shifting and reading. |
| **Per-step check** | Re-evaluate the gap before each step. If the gap has closed below the trigger threshold (10%), stop. Do not over-shift. |
| **Stabilization** | 7-day read after the final step before the next trigger is evaluated |
| **Lockout period** | Single-step shift: 3 days hold + 7 days stabilization = 10 days minimum between triggers |

---

## Daily Monitoring During Shift

Check all three metrics daily. A flag requires the metric to be breaching **both** D-o-D and vs same calendar day last week. These are signals for human judgment, not automated actions - other factors (creative changes, day-of-week effects, external events) may explain the movement.

| Metric | Flag if... | Threshold |
|---|---|---|
| Spend / budget utilisation | Dips D-o-D AND vs same day last week | > 20% drop on both |
| Blended CPBL | Rises D-o-D AND vs same day last week | > 20% rise on both |
| Booking volume | Dips D-o-D AND vs same day last week | > 20% drop on both |

**On flag:** Raise to the channel owner for review. Assess whether the movement is explained by the shift or by other factors before deciding to pause, continue, or reverse.

---

## Operationalization

**Owner:** Nikhil (Traffic block)

**Daily check (2 min):**
1. Pull 7-day rolling CPBL per channel from the Growth Dashboard (`/api/war_room`, `meta_cpbl` and `google_cpbl` fields)
2. Compute gap: `(expensive_cpbl - cheap_cpbl) / expensive_cpbl`
3. If gap > 10% for the 3rd consecutive day: initiate shift (see below)
4. If a shift is in progress: run the monitoring checks (spend, CPBL, bookings D-o-D and WoW)

**Initiating a shift:**
1. Compute `shift_Rs` using the formula above
2. Append a row to `budget_shift_log.csv`: date, trigger gap%, shift_Rs, source, destination, step N of M
3. Get approval (Nikhil)
4. Execute manually: adjust daily budget in Meta Ads Manager and Google Ads console
5. Fill in execution_confirmed and execution_time in the log row

**During the shift (at each 3-day step):**
1. Re-pull 7-day rolling CPBL per channel
2. If gap < 10%: stop, do not execute next step, enter stabilization
3. If gap >= 10% and no monitoring flags: execute next step
4. If monitoring flag raised: hold, review with SD before proceeding

**After the final step:**
1. Enter 7-day stabilization window - no new shifts
2. At day 7: pull CPBL per channel, assess whether gap has closed, document outcome
3. If gap persists after stabilization: re-evaluate trigger conditions

**Log:** `budget_shift_log.csv` in this repo. One row per step. Fields:
- date, trigger_gap_pct, shift_rs, source_channel, destination_channel, step_n, total_steps, execution_confirmed, execution_time, monitoring_flags, outcome_note

---

## Caveats and Discipline

1. **Learning phase:** Meta and Google UAC can re-enter learning on budget changes above ~20%. The 15% per-step cap prevents this on both the shifter and shiftee. Never skip the cap.
2. **No trigger during shift:** Layering a new shift on an in-flight one produces corrupted CPBL signals and risks ping-ponging both channels into permanent learning phase instability.
3. **No within-channel reallocation at this level:** Level 1 moves total Meta and Google envelopes only. How each channel allocates internally is Level 2.
4. **Gap closure mid-shift:** If the gap closes below 10% before all steps execute, stop immediately. Do not complete remaining steps.
5. **Monitoring flags are not auto-stops:** They are inputs to human judgment. Other factors may explain the movement - assess before acting.
6. **Decision aid, not auto-executor:** All budget changes are manual. System surfaces the recommendation; human approves and executes.

---

## Deferred

| | |
|---|---|
| **Level 2** | Within-channel allocation: Meta split across BFC-Volume and Retargeting; Google split across UAC, YouTube Demand Gen, and Search. Same framework logic, thresholds need separate calibration. |
| **Incrementality** | Run a geo holdout to validate whether Google CPBL is truly incremental vs Meta-assisted before treating channel CPBLs as fully independent signals. |
| **Creative Testing budget** | CT budget governance (how much, what triggers a change) is a separate question from channel rebalancing. |
