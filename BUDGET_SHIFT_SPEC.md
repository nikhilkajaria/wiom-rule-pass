# Channel Budget Rebalancing - Spec v1.0
Level 1: Inter-channel (Meta vs Google) | Jul 2026 | Pending SD alignment

---

## Objective

Maximize booking volume while improving blended CPBL toward the Rs 500 target. CPBL is the guardrail, not the sole objective. Do not cut spend just to hit efficiency.

---

## Scope

| | |
|---|---|
| **Meta** | BFC-Volume + Creative Testing + Retargeting (total channel envelope) |
| **Google** | UAC + YouTube (total channel envelope) |
| **Excluded** | ToF campaigns |
| **Level 2** | Within-channel allocation (how Meta or Google splits internally) is deferred |

---

## Metric

- **What:** 7-day rolling Branch-attributed CPBL per channel
- **Why 7-day:** ~30% of bookings arrive D7+ (booking lag cohort). A 3-day window systematically understates recent bookings and produces a directionally wrong signal.
- **Attribution:** Branch only. Google Search (brand pilot) excluded until attribution is validated.

---

## Trigger

| | |
|---|---|
| **Condition** | Channel CPBL gap > 20% for 3 consecutive days |
| **Check cadence** | Daily |
| **Suspended when** | A shift is in progress OR the stabilization window is active |
| **Why 3 days** | Filters transient causes (learning phase blips, booking lag, day-of-week noise) that resolve in 1-3 days. Structural gaps persist. |
| **Why >20%** | Noise filter. Small gaps produce tiny shifts that still risk a learning reset for near-zero gain. |

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
shift = min(15k, 4.5k) = **Rs 4.5k**  [Meta: -4.5%, Google: +15%]

---

## Stagger and Stabilization

| | |
|---|---|
| **Step cadence** | One shift step every 3 days |
| **Order** | Complete all steps first, then enter stabilization. Do not interleave shifting and reading. |
| **Per-step check** | Re-evaluate the gap at each step. If gap has closed (<20%), stop. Do not over-shift. |
| **Stabilization** | 7-day read after the final step before the next trigger is evaluated |
| **Lockout period** | Single-step shift: 3 days hold + 7 days stabilization = 10 days minimum between triggers |

---

## Daily Monitoring During Shift

Check all three metrics daily against the same calendar day last week:

| Metric | Flag if... | Threshold |
|---|---|---|
| Spend / budget utilisation | Dips vs same day last week | > 20% drop |
| Blended CPBL | Rises vs same day last week | > 20% rise |
| Booking volume | Dips vs same day last week | > 20% drop |

- **Any ONE metric flagging is sufficient to pause. Logic is OR, not AND.**
- On flag: freeze all remaining shift steps, investigate, resume only after root cause is clear.

---

## Caveats and Discipline

1. **Learning phase:** Meta and Google UAC can re-enter learning on budget changes above ~20%. The 15% per-step cap prevents this on both the shifter and shiftee. Never skip the cap.
2. **No trigger during shift:** Layering a new shift on an in-flight one produces corrupted CPBL signals and risks ping-ponging both channels into permanent learning phase instability.
3. **No within-channel reallocation at this level:** Level 1 moves total Meta and Google envelopes only. How each channel allocates internally is Level 2.
4. **Gap closure mid-shift:** If the gap closes (<20%) before all steps execute, stop immediately. Do not complete remaining steps.
5. **Monitoring is OR logic:** One metric breach is a flag. Requiring all three defeats the purpose.
6. **Decision aid, not auto-executor:** All budget changes are manual. System surfaces the recommendation; human approves and executes in Ads Manager.

---

## Deferred

| | |
|---|---|
| **Level 2** | Within-channel allocation: Meta split across BFC-Volume, CT, Retargeting; Google split across UAC and YouTube. Same framework logic, thresholds need separate calibration. |
| **Incrementality** | Run a geo holdout to validate whether Google CPBL is truly incremental vs Meta-assisted before treating channel CPBLs as independent signals. |
